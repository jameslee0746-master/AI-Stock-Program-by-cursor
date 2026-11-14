"""Stage3.1.6 Fake 장시간 처리. OCX/main.py/실제 6.5시간 대기 없음."""
from __future__ import annotations

import csv
import json
import os
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scalp.collector.orderbook_collector import HOGA_FIDS, OrderbookCollector  # noqa: E402
from scalp.collector.realtime_buffer import RealtimeBuffer  # noqa: E402
from scalp.collector.session import CollectSession  # noqa: E402
from scalp.collector.stats import CollectorStats  # noqa: E402
from scalp.collector.tick_collector import TickCollector  # noqa: E402
from scalp.collector.writer_service import WriterService  # noqa: E402


class FidMap:
    def __init__(self, mapping=None) -> None:
        self.mapping = dict(mapping or {})

    def __call__(self, code: str, fid: int) -> str:
        return str(self.mapping.get(fid, "1"))


def _tick_fids(i: int):
    return {
        10: str(70000 + (i % 50)),
        13: str(1 + (i % 7)),
        15: str(1 + (i % 3)),
        20: "%02d%02d%02d" % (9 + (i % 6), (i % 60), (i % 60)),
        228: "1",
    }


def _svc(td, queue_size=1000, maxlen=5000, start=True, join_timeout_sec=5.0):
    stats = CollectorStats()
    tb, hb = RealtimeBuffer(maxlen=maxlen), RealtimeBuffer(maxlen=maxlen)
    sess = CollectSession()
    sess.begin()
    svc = WriterService(
        td,
        tick_buffer=tb,
        hoga_buffer=hb,
        stats=stats,
        queue_size=queue_size,
        date_str="20261116",
        join_timeout_sec=join_timeout_sec,
    )
    col = TickCollector(FidMap(_tick_fids(0)), tb, stats, sess, sink=svc)
    hob = OrderbookCollector(FidMap({f: "1" for f in HOGA_FIDS}), hb, stats, sess, sink=svc)
    if start:
        svc.start()
    return svc, col, hob, stats, tb, hb, sess


def _csv_rows(td, prefix):
    names = [n for n in os.listdir(td) if n.startswith(prefix) and n.endswith(".csv")]
    total = 0
    for name in names:
        with open(os.path.join(td, name), encoding="utf-8", newline="") as fh:
            total += sum(1 for _ in csv.DictReader(fh))
    return total


class FakeLongRunTests(unittest.TestCase):
    def test_burst_input_then_normal_close_flush(self):
        n = 2500
        t0 = time.perf_counter()
        with tempfile.TemporaryDirectory() as td:
            svc, col, hob, stats, tb, hb, sess = _svc(td)
            for i in range(n):
                col.fid_get = FidMap(_tick_fids(i))
                col.handle("005930", "주식체결")
                hob.handle("005930", "주식호가잔량")
            svc.close()
            elapsed = time.perf_counter() - t0
            self.assertLess(elapsed, 30.0)
            self.assertFalse(svc.accepting)
            self.assertEqual(stats.pending_records(), 0)
            self.assertEqual(stats.terminal_losses(), 0)
            self.assertEqual(stats.write_ok, n * 2)
            self.assertEqual(_csv_rows(td, "tick_"), n)
            self.assertEqual(_csv_rows(td, "orderbook_"), n)
            dumped = json.load(open(os.path.join(td, "collector_stats.json"), encoding="utf-8"))
            self.assertIs(dumped["stats_dump_ok"], True)
            self.assertEqual(dumped["shutdown_status"], "COMPLETE")
            self.assertEqual(dumped["terminal_losses"], 0)
            self.assertIn("dup_suspect", dumped)
            self.assertEqual(dumped["dup_suspect"], stats.dup_suspect)

    def test_writer_queue_saturation_is_not_loss(self):
        with tempfile.TemporaryDirectory() as td:
            svc, col, hob, stats, tb, hb, sess = _svc(td, queue_size=4, maxlen=80, start=False)
            for i in range(40):
                col.fid_get = FidMap(_tick_fids(i))
                col.handle("005930", "주식체결")
            self.assertGreaterEqual(stats.writer_queue_full_attempts, 1)
            self.assertEqual(stats.dropped_writer_queue, 0)
            self.assertEqual(stats.terminal_losses(), 0)
            svc.start()
            svc.close()
            self.assertEqual(stats.write_ok, 40)
            self.assertEqual(stats.pending_records(), 0)

    def test_buffer_overflow_counts_as_terminal_loss(self):
        stats = CollectorStats()
        buf = RealtimeBuffer(maxlen=8)
        sess = CollectSession()
        sess.begin()
        col = TickCollector(FidMap(_tick_fids(0)), buf, stats, sess)
        for i in range(40):
            col.fid_get = FidMap(_tick_fids(i))
            col.handle("005930", "주식체결")
        self.assertEqual(stats.dropped_overflow, 32)
        self.assertEqual(stats.terminal_losses(), 32)
        self.assertEqual(stats.write_ok, 0)
        self.assertEqual(stats.dropped_write, 0)

    def test_writer_delay_then_flush(self):
        with tempfile.TemporaryDirectory() as td:
            svc, col, hob, stats, tb, hb, sess = _svc(td, queue_size=50, maxlen=80)
            orig = svc._write_item

            def slow(item):
                time.sleep(0.002)
                return orig(item)

            svc._write_item = slow  # type: ignore
            for i in range(40):
                col.fid_get = FidMap(_tick_fids(i))
                col.handle("005930", "주식체결")
            svc.close()
            self.assertEqual(stats.write_ok, 40)
            self.assertEqual(stats.pending_records(), 0)
            self.assertEqual(stats.terminal_losses(), 0)

    def test_stats_dump_failure_on_close(self):
        with tempfile.TemporaryDirectory() as td:
            svc, col, hob, stats, tb, hb, sess = _svc(td)
            col.handle("005930", "주식체결")
            stats.dump_json = lambda path: False  # type: ignore
            svc.close()
            self.assertEqual(stats.shutdown_status, "STATS_DUMP_FAILED")
            self.assertIs(stats.stats_dump_ok, False)
            self.assertNotEqual(stats.shutdown_status, "COMPLETE")

    def test_halt_rejects_further_input(self):
        with tempfile.TemporaryDirectory() as td:
            svc, col, hob, stats, tb, hb, sess = _svc(td)
            col.handle("005930", "주식체결")
            svc.close()
            before = stats.write_ok
            for _ in range(20):
                col.handle("005930", "주식체결")
                hob.handle("005930", "주식호가잔량")
            self.assertEqual(stats.write_ok, before)
            self.assertGreaterEqual(stats.rejected_after_close, 20)

    def test_virtual_session_span_without_wall_wait(self):
        """정규장 6.5시간을 sleep 하지 않고 가상 입력만 처리한다."""
        n = 390
        t0 = time.perf_counter()
        with tempfile.TemporaryDirectory() as td:
            svc, col, hob, stats, tb, hb, sess = _svc(td)
            for i in range(n):
                col.fid_get = FidMap(_tick_fids(i))
                col.handle("005930", "주식체결")
                hob.handle("005930", "주식호가잔량")
            svc.close()
            elapsed = time.perf_counter() - t0
            self.assertLess(elapsed, 20.0)
            self.assertEqual(stats.write_ok, n * 2)
            self.assertEqual(stats.pending_records(), 0)
            self.assertEqual(stats.terminal_losses(), 0)


if __name__ == "__main__":
    unittest.main()
