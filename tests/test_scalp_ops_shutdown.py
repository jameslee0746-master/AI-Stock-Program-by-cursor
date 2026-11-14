"""HFT 1단계 필수 결함 수정 테스트. 키움/Qt 불필요."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scalp.collector.orderbook_collector import OrderbookCollector  # noqa: E402
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


def _tick_fids():
    return {10: "70000", 13: "1", 15: "1", 20: "090000", 228: "1"}


def _svc(td, queue_size=50, maxlen=50, start=True, join_timeout_sec=2.0):
    stats = CollectorStats()
    tb, hb = RealtimeBuffer(maxlen=maxlen), RealtimeBuffer(maxlen=maxlen)
    sess = CollectSession()
    sess.begin()
    svc = WriterService(
        td, tick_buffer=tb, hoga_buffer=hb, stats=stats,
        queue_size=queue_size, date_str="20261111",
        join_timeout_sec=join_timeout_sec,
    )
    col = TickCollector(FidMap(_tick_fids()), tb, stats, sess, sink=svc)
    hob = OrderbookCollector(FidMap({}), hb, stats, sess, sink=svc)
    if start:
        svc.start()
    return svc, col, hob, stats, tb, hb, sess


class CloseGateTests(unittest.TestCase):
    def test_handle_rejected_after_close(self):
        with tempfile.TemporaryDirectory() as td:
            svc, col, _h, stats, tb, _hb, _s = _svc(td)
            col.handle("005930", "주식체결")
            svc.close()
            n = len(tb)
            rec = col.handle("005930", "주식체결")
            self.assertIsNone(rec)
            self.assertEqual(len(tb), n)
            self.assertGreaterEqual(stats.rejected_after_close, 1)
            self.assertFalse(svc.accepting)

    def test_offer_rejected_after_close(self):
        with tempfile.TemporaryDirectory() as td:
            svc, _c, _h, stats, _tb, _hb, _s = _svc(td)
            svc.close()
            ok = svc.offer("tick", {"code": "005930", "phase": "observe", "recv_ns": 1})
            self.assertFalse(ok)
            self.assertGreaterEqual(stats.rejected_after_close, 1)

    def test_close_waits_for_in_flight_callback(self):
        started = threading.Event()
        release = threading.Event()

        def fid_get(code, fid):
            started.set()
            release.wait(2.0)
            return "1"

        with tempfile.TemporaryDirectory() as td:
            stats = CollectorStats()
            tb, hb = RealtimeBuffer(maxlen=50), RealtimeBuffer(maxlen=50)
            sess = CollectSession()
            sess.begin()
            svc = WriterService(
                td, tick_buffer=tb, hoga_buffer=hb, stats=stats, date_str="20261111",
            )
            col = TickCollector(fid_get, tb, stats, sess, sink=svc)
            svc.start()
            closer_done = threading.Event()

            def run_handle():
                col.handle("005930", "주식체결")

            def run_close():
                started.wait(1.0)
                svc.close(timeout=2.0)
                closer_done.set()

            t1 = threading.Thread(target=run_handle)
            t2 = threading.Thread(target=run_close)
            t1.start()
            t2.start()
            self.assertTrue(started.wait(1.0))
            time.sleep(0.05)
            self.assertFalse(closer_done.is_set())
            release.set()
            t1.join(2.0)
            t2.join(2.0)
            self.assertTrue(closer_done.is_set())
            self.assertEqual(stats.accepted_records, 1)
            self.assertEqual(stats.write_ok, 1)
            self.assertEqual(stats.rejected_after_close, 0)
            self.assertEqual(stats.shutdown_status, "COMPLETE")


class BufferConcurrencyTests(unittest.TestCase):
    def test_concurrent_push_pop_no_indexerror(self):
        buf = RealtimeBuffer(maxlen=32)
        errors = []

        def pusher():
            try:
                for i in range(4000):
                    buf.push({"i": i, "recv_ns": i})
            except Exception as e:
                errors.append(e)

        def popper():
            try:
                for _ in range(4000):
                    buf.pop_batch(5)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=pusher) for _ in range(3)]
        threads += [threading.Thread(target=popper) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        buf.pop_batch(1000)

    def test_push_while_buffer_emptied(self):
        buf = RealtimeBuffer(maxlen=8)
        errors = []
        stop = threading.Event()

        def pusher():
            i = 0
            try:
                while not stop.is_set():
                    buf.push({"i": i})
                    i += 1
            except Exception as e:
                errors.append(e)

        def drainer():
            try:
                for _ in range(2000):
                    buf.flush_all()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=pusher) for _ in range(4)]
        d = threading.Thread(target=drainer)
        for t in threads:
            t.start()
        d.start()
        d.join()
        stop.set()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])


class AccountingTests(unittest.TestCase):
    def test_queue_full_then_salvage_is_not_loss(self):
        with tempfile.TemporaryDirectory() as td:
            svc, col, _h, stats, tb, _hb, _s = _svc(td, queue_size=1, start=False)
            for _ in range(5):
                col.handle("005930", "주식체결")
            self.assertGreaterEqual(stats.writer_queue_full_attempts, 1)
            self.assertEqual(stats.dropped_writer_queue, 0)
            self.assertEqual(len(tb), 5)
            svc.close()
            self.assertEqual(stats.write_ok, 5)
            self.assertEqual(stats.dropped_overflow, 0)
            self.assertTrue(stats.identity_ok())

    def test_enqueued_evicted_from_buffer_is_not_overflow_loss(self):
        with tempfile.TemporaryDirectory() as td:
            svc, col, _h, stats, tb, _hb, _s = _svc(td, queue_size=50, maxlen=2, start=False)
            for _ in range(5):
                col.handle("005930", "주식체결")
            self.assertEqual(len(tb), 2)
            self.assertEqual(stats.dropped_overflow, 0)
            self.assertEqual(tb.dropped_overflow, 0)
            svc.close()
            self.assertEqual(stats.write_ok, 5)
            self.assertEqual(stats.accepted_records, 5)
            self.assertTrue(stats.identity_ok())

    def test_true_overflow_only_counts_unqueued_eviction(self):
        stats = CollectorStats()
        buf = RealtimeBuffer(maxlen=2)
        sess = CollectSession()
        sess.begin()
        col = TickCollector(FidMap(_tick_fids()), buf, stats, sess)
        for _ in range(5):
            col.handle("005930", "주식체결")
        self.assertEqual(stats.accepted_records, 5)
        self.assertEqual(stats.dropped_overflow, 3)
        self.assertEqual(len(buf), 2)

    def test_write_fail_only_dropped_write(self):
        with tempfile.TemporaryDirectory() as td:
            svc, col, _h, stats, _tb, _hb, _s = _svc(td, start=False)
            col.handle("005930", "주식체결")

            def fail_write(item):
                rec = item[1]
                rec["_write_failed"] = True
                stats.incr("write_fail")
                stats.incr("dropped_write")

            svc._write_item = fail_write  # type: ignore
            svc.close()
            self.assertEqual(stats.dropped_write, 1)
            self.assertEqual(stats.write_ok, 0)
            self.assertEqual(stats.dropped_overflow, 0)
            self.assertEqual(stats.accepted_records, 1)
            self.assertTrue(stats.identity_ok())

    def test_accepted_equals_write_ok_plus_terminal_plus_pending(self):
        with tempfile.TemporaryDirectory() as td:
            svc, col, hob, stats, _tb, _hb, _s = _svc(td)
            for _ in range(7):
                col.handle("005930", "주식체결")
            hob.handle("005930", "주식호가잔량")
            svc.close()
            d = stats.as_dict()
            self.assertTrue(d["identity_ok"])
            self.assertEqual(
                d["accepted_records"],
                d["write_ok"] + d["terminal_losses"] + d["pending_records"],
            )
            self.assertEqual(d["pending_records"], 0)
            self.assertEqual(d["accepted_records"], 8)
            self.assertEqual(d["write_ok"], 8)
            self.assertEqual(d["callback_total"], 8)
            self.assertEqual(d["rejected_after_close"], 0)


class CloseSafetyTests(unittest.TestCase):
    def test_close_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            svc, col, _h, stats, _tb, _hb, _s = _svc(td)
            col.handle("005930", "주식체결")
            svc.close()
            first = stats.shutdown_status
            write_ok = stats.write_ok
            svc.close()
            svc.close()
            self.assertEqual(stats.shutdown_status, first)
            self.assertEqual(stats.write_ok, write_ok)
            with self.assertRaises(RuntimeError):
                svc.start()

    def test_writer_join_timeout_sticky(self):
        with tempfile.TemporaryDirectory() as td:
            svc, _c, _h, stats, _tb, _hb, _s = _svc(td, start=False, join_timeout_sec=0.05)

            def hang():
                svc.writer_thread_id = threading.get_ident()
                time.sleep(2)

            svc._run = hang  # type: ignore
            svc.start()
            time.sleep(0.02)
            svc.close(timeout=0.05)
            self.assertTrue(stats.writer_join_timeout)
            self.assertEqual(stats.shutdown_status, "INCOMPLETE")
            svc.close()
            self.assertTrue(stats.writer_join_timeout)
            self.assertEqual(stats.shutdown_status, "INCOMPLETE")

    def test_stats_json_concurrent_access(self):
        stats = CollectorStats()
        errors = []

        def inc():
            try:
                for _ in range(2000):
                    stats.incr("accepted_records")
                    stats.incr("write_ok")
                    stats.as_dict()
            except Exception as e:
                errors.append(e)

        def dumper():
            try:
                with tempfile.TemporaryDirectory() as td:
                    path = os.path.join(td, "s.json")
                    for _ in range(200):
                        stats.dump_json(path)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=inc) for _ in range(4)]
        threads.append(threading.Thread(target=dumper))
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        d = stats.as_dict()
        self.assertEqual(d["accepted_records"], d["write_ok"])

    def test_stats_dump_failure_is_not_complete(self):
        with tempfile.TemporaryDirectory() as td:
            svc, col, _h, stats, _tb, _hb, _s = _svc(td)
            col.handle("005930", "주식체결")
            stats.dump_json = lambda path: False  # type: ignore
            svc.close()
            self.assertFalse(stats.stats_dump_ok)
            self.assertEqual(stats.shutdown_status, "STATS_DUMP_FAILED")
            self.assertNotEqual(stats.shutdown_status, "COMPLETE")

    def test_handles_not_closed_before_writer_exit(self):
        with tempfile.TemporaryDirectory() as td:
            svc, col, _h, stats, _tb, _hb, _s = _svc(td, start=False, join_timeout_sec=0.05)
            block = threading.Event()
            orig = svc._write_item

            def blocked(item):
                block.wait(2.0)
                orig(item)

            svc._write_item = blocked  # type: ignore
            svc.start()
            col.handle("005930", "주식체결")
            time.sleep(0.05)
            svc.close(timeout=0.05)
            try:
                self.assertTrue(stats.writer_join_timeout)
                self.assertEqual(stats.shutdown_status, "INCOMPLETE")
                self.assertFalse(svc.tick_writer._closed)
                self.assertFalse(svc.hoga_writer._closed)
            finally:
                block.set()
                svc._stop.set()
                if svc._thread is not None:
                    svc._thread.join(1.0)
                svc.tick_writer.close()
                svc.hoga_writer.close()

    def test_terminal_incomplete_is_not_complete(self):
        with tempfile.TemporaryDirectory() as td:
            svc, col, _h, stats, _tb, _hb, _s = _svc(td)
            col.handle("005930", "주식체결")
            svc.close(terminal="INCOMPLETE")
            self.assertEqual(stats.shutdown_status, "INCOMPLETE")
            self.assertNotEqual(stats.shutdown_status, "COMPLETE")
            svc.close(terminal="COMPLETE")
            self.assertEqual(stats.shutdown_status, "INCOMPLETE")

    def test_incomplete_close_reports_pending(self):
        with tempfile.TemporaryDirectory() as td:
            svc, col, _h, stats, _tb, _hb, _s = _svc(td, start=False, join_timeout_sec=0.05)
            for _ in range(4):
                col.handle("005930", "주식체결")

            def hang():
                time.sleep(2)

            svc._run = hang  # type: ignore
            svc.start()
            time.sleep(0.02)
            svc.close(timeout=0.05)
            self.assertEqual(stats.shutdown_status, "INCOMPLETE")
            self.assertGreater(stats.pending_queue + stats.pending_buffer + stats.pending_inflight, 0)
            d = stats.as_dict()
            self.assertTrue(d["identity_ok"])
            self.assertEqual(
                d["accepted_records"],
                d["write_ok"] + d["terminal_losses"] + d["pending_records"],
            )
            path = os.path.join(td, "collector_stats.json")
            self.assertTrue(os.path.isfile(path))
            with open(path, encoding="utf-8") as f:
                dumped = json.load(f)
            self.assertEqual(dumped["shutdown_status"], "INCOMPLETE")
            self.assertIn("writer_join_timeout", dumped)
            self.assertIn("rejected_after_close", dumped)
            self.assertIn("handler_exceptions", dumped)
            self.assertIn("tick_buffer_pending", dumped)


class SeqAndRssTests(unittest.TestCase):
    def test_seq_assigned_and_unique(self):
        col_stats = CollectorStats()
        buf = RealtimeBuffer(maxlen=20)
        sess = CollectSession()
        sess.begin()
        col = TickCollector(FidMap(_tick_fids()), buf, col_stats, sess)
        recs = [col.handle("005930", "주식체결") for _ in range(5)]
        seqs = [r["seq"] for r in recs]
        self.assertEqual(seqs, [1, 2, 3, 4, 5])


if __name__ == "__main__":
    unittest.main()
