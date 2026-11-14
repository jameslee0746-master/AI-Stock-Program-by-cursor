"""HFT 1단계 수집기 단위 테스트. 키움/Qt 불필요. 기존 테스트를 변경하지 않는다."""
from __future__ import annotations

import csv
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scalp.collector import HOGA_FIDS, SCREEN_HOGA, SCREEN_TICK  # noqa: E402
from scalp.collector.clock import clock_offset_sec, parse_hhmmss, recv_iso_us, recv_ns  # noqa: E402
from scalp.collector.data_writer import CsvWriter  # noqa: E402
from scalp.collector.orderbook_collector import OrderbookCollector  # noqa: E402
from scalp.collector.realtime_buffer import RealtimeBuffer  # noqa: E402
from scalp.collector.session import CollectSession  # noqa: E402
from scalp.collector.stats import CollectorStats  # noqa: E402
from scalp.collector.tick_collector import TickCollector  # noqa: E402


class FakeSignal:
    def __init__(self) -> None:
        self.slots = []

    def connect(self, fn) -> None:
        self.slots.append(fn)

    def emit(self, *a) -> None:
        for fn in list(self.slots):
            fn(*a)


class FidMap:
    def __init__(self, mapping=None) -> None:
        self.mapping = dict(mapping or {})
        self.calls = []

    def __call__(self, code: str, fid: int) -> str:
        self.calls.append((code, fid))
        return str(self.mapping.get(fid, ""))


def _tick_env(maxlen=50, fids=None):
    stats = CollectorStats()
    buf = RealtimeBuffer(maxlen=maxlen)
    sess = CollectSession()
    sess.begin()
    getter = FidMap(
        fids
        or {
            10: "+199500",
            13: "1000000",
            15: "12",
            20: "090134",
            228: "82.87",
        }
    )
    col = TickCollector(getter, buf, stats, sess)
    return col, buf, stats, sess, getter


class ClockTests(unittest.TestCase):
    def test_perf_counter_ns_and_iso_microseconds(self):
        ns = recv_ns()
        self.assertIsInstance(ns, int)
        iso = recv_iso_us()
        self.assertIn(".", iso.split("T", 1)[-1])
        self.assertGreaterEqual(len(iso.split(".")[-1]), 6)

    def test_fid20_raw_parse_and_offset_is_not_named_delay(self):
        self.assertEqual(parse_hhmmss("090134"), 9 * 3600 + 1 * 60 + 34)
        self.assertIsNone(parse_hhmmss("xx"))
        self.assertIsNone(parse_hhmmss("256000"))
        off = clock_offset_sec(9 * 3600, 9 * 3600 + 94)
        self.assertEqual(off, -94.0)


class BufferTests(unittest.TestCase):
    def test_overflow_counts_lost_records(self):
        buf = RealtimeBuffer(maxlen=3)
        for i in range(5):
            buf.push({"i": i, "recv_ns": i})
        self.assertEqual(len(buf), 3)
        self.assertEqual(buf.dropped_overflow, 2)
        kept = buf.pop_batch(10)
        self.assertEqual([r["i"] for r in kept], [2, 3, 4])

    def test_memory_cap_after_many_pushes(self):
        buf = RealtimeBuffer(maxlen=5000)
        stats = CollectorStats()
        for i in range(50000):
            buf.push({"i": i, "recv_ns": i})
            if buf.dropped_overflow > stats.dropped_overflow:
                stats.dropped_overflow = buf.dropped_overflow
        self.assertEqual(len(buf), 5000)
        self.assertEqual(buf.dropped_overflow, 45000)


class WriterTests(unittest.TestCase):
    def test_roundtrip_and_batch_limit(self):
        buf = RealtimeBuffer(maxlen=50)
        stats = CollectorStats()
        for i in range(5):
            buf.push({"recv_ns": i, "v": i})
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "t.csv")
            w = CsvWriter(path, ["recv_ns", "v"], batch_limit=2, stats=stats)
            n1 = w.flush_from(buf)
            n2 = w.flush_from(buf)
            n3 = w.flush_from(buf)
            self.assertEqual((n1, n2, n3), (2, 2, 1))
            self.assertGreater(stats.flush_count, 0)
            self.assertGreaterEqual(stats.max_flush_ns, 0)
            with open(path, encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), 5)
            self.assertEqual(rows[0]["v"], "0")

    def test_write_fail_is_not_overflow_and_no_retry_loop(self):
        buf = RealtimeBuffer(maxlen=50)
        stats = CollectorStats()
        buf.push({"recv_ns": 1, "v": 1})
        with tempfile.TemporaryDirectory() as td:
            blocker = os.path.join(td, "notdir")
            with open(blocker, "w", encoding="utf-8") as f:
                f.write("x")
            w = CsvWriter(os.path.join(blocker, "x.csv"), ["recv_ns", "v"], stats=stats)
            n = w.flush_from(buf)
        self.assertEqual(n, 0)
        self.assertEqual(stats.write_fail, 1)
        self.assertEqual(stats.dropped_write, 1)
        self.assertEqual(stats.dropped_overflow, 0)
        self.assertEqual(len(buf), 0)


class TickTests(unittest.TestCase):
    def test_collects_all_requested_fids(self):
        col, buf, stats, _sess, _g = _tick_env()
        rec = col.handle("005930", "주식체결")
        self.assertEqual(len(buf), 1)
        self.assertTrue(rec["fid10_ok"] and rec["fid13_ok"] and rec["fid15_ok"])
        self.assertTrue(rec["fid20_ok"] and rec["fid228_ok"])
        self.assertEqual(rec["fid20_raw"], "090134")
        self.assertIn("clock_offset_sec", rec)
        self.assertNotIn("delay", rec)
        self.assertEqual(stats.fid13_15_presence()["fid13_ok"], 1)

    def test_missing_fid13_15_are_flagged_not_dropped(self):
        col, buf, stats, _s, _g = _tick_env(fids={10: "70000", 20: "090000", 228: "1"})
        rec = col.handle("005930", "주식체결")
        self.assertEqual(len(buf), 1)
        self.assertFalse(rec["fid13_ok"])
        self.assertFalse(rec["fid15_ok"])
        self.assertEqual(stats.fid13_missing, 1)
        self.assertEqual(stats.fid15_missing, 1)
        self.assertEqual(stats.fid13_15_presence()["fid13_ok_ratio"], 0.0)

    def test_bad_price_and_time_keep_row(self):
        col, buf, stats, _s, _g = _tick_env(fids={10: "abc", 20: "xx", 228: ""})
        rec = col.handle("005930", "주식체결")
        self.assertEqual(len(buf), 1)
        self.assertFalse(rec["fid10_ok"])
        self.assertFalse(rec["fid20_ok"])
        self.assertFalse(rec["fid228_ok"])

    def test_duplicates_are_kept_and_counted(self):
        col, buf, stats, _s, _g = _tick_env()
        col.handle("005930", "주식체결")
        col.handle("005930", "주식체결")
        self.assertEqual(len(buf), 2)
        self.assertEqual(stats.dup_suspect, 1)
        self.assertEqual(stats.tick_callbacks, 2)

    def test_overflow_from_collector_updates_stats(self):
        col, buf, stats, _s, _g = _tick_env(maxlen=2)
        for _ in range(5):
            col.handle("005930", "주식체결")
        self.assertEqual(len(buf), 2)
        self.assertEqual(stats.dropped_overflow, 3)

    def test_observe_and_after_unreg_not_mixed(self):
        col, buf, _stats, sess, _g = _tick_env()
        col.handle("005930", "주식체결")
        sess.mark_unreg()
        col.handle("005930", "주식체결")
        rows = buf.pop_batch(10)
        self.assertEqual(rows[0]["phase"], "observe")
        self.assertEqual(rows[1]["phase"], "after_unreg")


class HogaTests(unittest.TestCase):
    def test_orderbook_fields(self):
        stats = CollectorStats()
        buf = RealtimeBuffer(maxlen=10)
        sess = CollectSession()
        sess.begin()
        getter = FidMap({f: "1" for f in HOGA_FIDS})
        ob = OrderbookCollector(getter, buf, stats, sess)
        rec = ob.handle("005930", "주식호가잔량")
        self.assertEqual(stats.hoga_callbacks, 1)
        self.assertTrue(all(rec[f"fid{f}_ok"] for f in HOGA_FIDS))
        self.assertEqual(rec["fid21_raw"], "1")


class DualSlotTests(unittest.TestCase):
    def test_product_handler_stays_connected(self):
        product_got = []

        def product(_c, _t, _d):
            product_got.append("p")

        col, buf, _stats, _s, _g = _tick_env()
        sig = FakeSignal()
        sig.connect(product)
        sig.connect(col.handle)
        sig.emit("005930", "주식체결", "")
        self.assertEqual(product_got, ["p"])
        self.assertEqual(len(buf), 1)
        self.assertEqual(len(sig.slots), 2)

    def test_multi_screen_duplicate_callbacks_are_measured_not_deleted(self):
        col, buf, stats, _s, _g = _tick_env()
        col.handle("005930", "주식체결")
        col.handle("005930", "주식체결")
        self.assertEqual(len(buf), 2)
        self.assertEqual(stats.dup_suspect, 1)


class ScreenAndSafetyTests(unittest.TestCase):
    def test_screens_8101_8102_not_product_screens(self):
        self.assertEqual(SCREEN_TICK, "8101")
        self.assertEqual(SCREEN_HOGA, "8102")
        self.assertNotIn(SCREEN_TICK, ("9001", "9051", "0101", "0404", "5202"))
        self.assertNotIn(SCREEN_HOGA, ("9001", "9051", "0101", "0404", "5202"))

    def test_scalp_source_has_no_orders(self):
        root = os.path.join(ROOT, "scalp")
        for dp, _dn, fn in os.walk(root):
            for name in fn:
                if not name.endswith(".py"):
                    continue
                with open(os.path.join(dp, name), encoding="utf-8") as f:
                    src = f.read()
                self.assertNotIn("SendOrder(", src)
                self.assertNotIn('dynamicCall("SendOrder', src)
                self.assertNotIn("send_order_market", src)
                self.assertNotIn("cancel_order", src)


class OpsFlushTests(unittest.TestCase):
    def test_flush_all_writes_remaining_buffer(self):
        buf = RealtimeBuffer(maxlen=50)
        stats = CollectorStats()
        for i in range(5):
            buf.push({"recv_ns": i, "v": i})
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "t.csv")
            w = CsvWriter(path, ["recv_ns", "v"], batch_limit=2, stats=stats)
            n = w.flush_all(buf)
            w.close()
            self.assertEqual(n, 5)
            self.assertEqual(len(buf), 0)
            with open(path, encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), 5)

    def test_write_fail_counts_unwritten(self):
        buf = RealtimeBuffer(maxlen=50)
        stats = CollectorStats()
        buf.push({"recv_ns": 1, "v": 1})
        buf.push({"recv_ns": 2, "v": 2})
        with tempfile.TemporaryDirectory() as td:
            blocker = os.path.join(td, "notdir")
            with open(blocker, "w", encoding="utf-8") as f:
                f.write("x")
            w = CsvWriter(os.path.join(blocker, "x.csv"), ["recv_ns", "v"], batch_limit=10, stats=stats)
            n = w.flush_all(buf)
            w.close()
        self.assertEqual(n, 0)
        self.assertEqual(stats.write_fail, 1)
        self.assertEqual(stats.dropped_write, 2)
        self.assertEqual(len(buf), 0)


class OpsWriterServiceTests(unittest.TestCase):
    def _svc(self, td, queue_size=50, max_bytes=200 * 1024 * 1024, start=True):
        from scalp.collector.writer_service import WriterService

        stats = CollectorStats()
        tb, hb = RealtimeBuffer(maxlen=50), RealtimeBuffer(maxlen=50)
        sess = CollectSession()
        sess.begin()
        svc = WriterService(
            td, tick_buffer=tb, hoga_buffer=hb, stats=stats,
            queue_size=queue_size, max_bytes=max_bytes, date_str="20261111",
        )
        getter = FidMap({10: "70000", 13: "1", 15: "1", 20: "090000", 228: "1"})
        col = TickCollector(getter, tb, stats, sess, sink=svc)
        hob = OrderbookCollector(FidMap({f: "1" for f in HOGA_FIDS}), hb, stats, sess, sink=svc)
        if start:
            svc.start()
        return svc, col, hob, stats, tb, hb, sess

    def test_close_drains_writer_queue(self):
        with tempfile.TemporaryDirectory() as td:
            svc, col, _h, stats, _tb, _hb, _s = self._svc(td)
            for _ in range(8):
                col.handle("005930", "주식체결")
            svc.close()
            ticks = [n for n in os.listdir(td) if n.startswith("tick_") and n.endswith(".csv")]
            self.assertTrue(ticks)
            path = os.path.join(td, ticks[0])
            with open(path, encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), 8)
            self.assertTrue(os.path.isfile(os.path.join(td, "collector_stats.json")))

    def test_observe_after_unreg_separate_files(self):
        with tempfile.TemporaryDirectory() as td:
            svc, col, _h, _st, _tb, _hb, sess = self._svc(td)
            col.handle("005930", "주식체결")
            sess.mark_unreg()
            col.handle("005930", "주식체결")
            svc.close()
            names = os.listdir(td)
            obs = [n for n in names if "observe" in n and n.endswith(".csv")]
            aft = [n for n in names if "after_unreg" in n and n.endswith(".csv")]
            self.assertEqual(len(obs), 1)
            self.assertEqual(len(aft), 1)
            with open(os.path.join(td, obs[0]), encoding="utf-8") as f:
                self.assertEqual(len(list(csv.DictReader(f))), 1)
            with open(os.path.join(td, aft[0]), encoding="utf-8") as f:
                self.assertEqual(len(list(csv.DictReader(f))), 1)

    def test_unreg_at_mono_boundary(self):
        sess = CollectSession()
        sess.begin()
        sess.mark_unreg(at_ns=1000)
        self.assertEqual(sess.unreg_at_mono, 1000)
        self.assertEqual(sess.phase(999), "observe")
        self.assertEqual(sess.phase(1000), "after_unreg")
        self.assertEqual(sess.phase(1001), "after_unreg")

    def test_rotation_and_header(self):
        from scalp.collector.data_writer import PhaseRotatingWriter
        from scalp.collector.tick_collector import TICK_FIELDS

        stats = CollectorStats()
        with tempfile.TemporaryDirectory() as td:
            w = PhaseRotatingWriter(td, "tick", TICK_FIELDS, max_bytes=80, date_str="20261111", stats=stats)
            rec = {
                "recv_ns": 1, "recv_iso": "x", "code": "005930", "real_type": "주식체결",
                "epoch": 1, "phase": "observe",
                "fid10_raw": "1", "fid10": 1, "fid10_ok": True,
                "fid13_raw": "1", "fid13": 1, "fid13_ok": True,
                "fid15_raw": "1", "fid15": 1, "fid15_ok": True,
                "fid20_raw": "090000", "fid20_sod": 32400, "fid20_ok": True,
                "fid228_raw": "1", "fid228": 1, "fid228_ok": True,
                "clock_offset_sec": 0,
            }
            for _ in range(30):
                w.write_record(rec)
            inv = w.file_inventory()
            w.close()
            csvs = sorted(n for n in os.listdir(td) if n.endswith(".csv"))
            self.assertGreaterEqual(len(csvs), 2)
            self.assertTrue(any("part001" in n for n in csvs))
            self.assertTrue(any("part002" in n for n in csvs))
            for name in csvs:
                with open(os.path.join(td, name), encoding="utf-8") as f:
                    header = f.readline().strip().split(",")
                self.assertEqual(header[0], "recv_ns")
                self.assertIn("fid10_raw", header)
            self.assertTrue(all(x["rows"] > 0 for x in inv))
            self.assertFalse(os.path.isfile(os.path.join(td, "tick_005930_observe.csv")))

    def test_writer_queue_overflow_does_not_block(self):
        with tempfile.TemporaryDirectory() as td:
            svc, col, _h, stats, tb, _hb, _s = self._svc(td, queue_size=2, start=False)
            for _ in range(10):
                col.handle("005930", "주식체결")
            self.assertGreaterEqual(stats.writer_queue_full_attempts, 1)
            self.assertEqual(stats.dropped_writer_queue, 0)
            self.assertEqual(len(tb), 10)
            svc.close()

    def test_writer_join_timeout_recorded(self):
        import threading
        import time
        from scalp.collector.writer_service import WriterService

        stats = CollectorStats()
        with tempfile.TemporaryDirectory() as td:
            svc = WriterService(
                td, tick_buffer=RealtimeBuffer(), hoga_buffer=RealtimeBuffer(),
                stats=stats, join_timeout_sec=0.05,
            )

            def hang():
                svc.writer_thread_id = threading.get_ident()
                time.sleep(2)

            svc._run = hang  # type: ignore
            svc.start()
            time.sleep(0.02)
            svc.close(timeout=0.05)
            self.assertTrue(stats.writer_join_timeout)

    def test_writer_thread_never_calls_ocx(self):
        import threading

        ids = []

        def fid_get(code, fid):
            ids.append(threading.get_ident())
            return "1"

        with tempfile.TemporaryDirectory() as td:
            svc, _c, _h, stats, tb, _hb, sess = self._svc(td)
            col = TickCollector(fid_get, tb, stats, sess, sink=svc)
            col.handle("005930", "주식체결")
            svc.close()
            self.assertTrue(ids)
            self.assertNotEqual(ids[0], svc.writer_thread_id)
            with open(os.path.join(ROOT, "scalp", "collector", "writer_service.py"), encoding="utf-8") as f:
                src = f.read()
            self.assertNotIn("dynamicCall", src)
            self.assertNotIn("GetCommRealData", src)


class BufferIndependenceAndStatsTests(unittest.TestCase):
    def test_tick_and_hoga_buffers_independent(self):
        tb, hb = RealtimeBuffer(maxlen=2), RealtimeBuffer(maxlen=10)
        stats = CollectorStats()
        sess = CollectSession()
        sess.begin()
        tick = TickCollector(FidMap({10: "1", 13: "1", 15: "1", 20: "090000", 228: "1"}), tb, stats, sess)
        hoga = OrderbookCollector(FidMap({f: "1" for f in HOGA_FIDS}), hb, stats, sess)
        for _ in range(8):
            tick.handle("005930", "주식체결")
        hoga.handle("005930", "주식호가잔량")
        self.assertEqual(len(tb), 2)
        self.assertEqual(len(hb), 1)
        self.assertGreaterEqual(stats.dropped_overflow, 6)
        self.assertEqual(hb.dropped_overflow, 0)

    def test_ops_stats_accumulate(self):
        col, buf, stats, _s, _g = _tick_env()
        for _ in range(10):
            col.handle("005930", "주식체결")
        d = stats.as_dict()
        self.assertEqual(d["tick_callbacks"], 10)
        self.assertGreater(d["ticks_per_sec"], 0)
        self.assertGreaterEqual(d["tick_buffer_high_water"], 1)
        self.assertGreaterEqual(d["handle_ns_max"], 0)
        self.assertIn("elapsed_sec", d)
        self.assertIn("dropped_overflow", d)
        self.assertIn("dropped_write", d)
        self.assertIn("dropped_writer_queue", d)

    def test_rss_unavailable_is_recorded(self):
        import scalp.collector.stats as st

        old = st.process_rss_bytes
        st.process_rss_bytes = lambda: None
        try:
            d = CollectorStats().as_dict()
            self.assertIsNone(d["rss_bytes"])
            self.assertFalse(d["rss_ok"])
            self.assertEqual(d["rss_status"], "unavailable")
        finally:
            st.process_rss_bytes = old

    def test_rss_success_or_graceful_fail(self):
        d = CollectorStats().as_dict()
        self.assertIn("rss_ok", d)
        if d["rss_ok"]:
            self.assertIsInstance(d["rss_bytes"], int)
            self.assertGreater(d["rss_bytes"], 0)
        else:
            self.assertIsNone(d["rss_bytes"])


if __name__ == "__main__":
    unittest.main()

