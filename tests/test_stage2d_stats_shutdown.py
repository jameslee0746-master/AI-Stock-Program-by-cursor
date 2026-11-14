"""Stage2D 통계 의미·종료 순서 Fake 테스트. 실제 OCX/로그인/수집 없음."""
from __future__ import annotations

import csv
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

from scalp.bridge import attach_bridge  # noqa: E402
from scalp.collector import SCREEN_HOGA, SCREEN_TICK  # noqa: E402
from scalp.collector.orderbook_collector import OrderbookCollector  # noqa: E402
from scalp.collector.realtime_buffer import RealtimeBuffer  # noqa: E402
from scalp.collector.session import CollectSession  # noqa: E402
from scalp.collector.stats import (  # noqa: E402
    BUFFER_ROLE_SALVAGE_HISTORY,
    BRIDGE_CLOSED,
    WRITER_QUEUE_ROLE_BACKLOG,
    CollectorStats,
)
from scalp.collector.tick_collector import TickCollector  # noqa: E402
from scalp.collector.writer_service import WriterService  # noqa: E402
from tests.test_scalp_bridge import EnvGuard, FakeKiwoom  # noqa: E402
from tests.test_scalp_ops_shutdown import FidMap, _tick_fids  # noqa: E402
from tests.test_stage2c_safe_runner import (  # noqa: E402
    EnvIsolatedTestCase,
    PreparedKiwoom,
    RecordingApi,
    _WatchTimer,
    _audit_names,
)
from tools.stage2c_login_only_audit import load_events  # noqa: E402
from tools.stage2c_safe_runner import (  # noqa: E402
    ENV_COLLECT_ONLY,
    ENV_LOGIN_ONLY,
    ENV_SAFE,
    CollectOnlyBlockedError,
    LoginOnlyBlockedError,
    SendOrderBlockedError,
    bind_watch_timer,
    classify_orig_ocx_method,
    install_sendorder_guard,
    prepare_api,
    shutdown_session,
)


def _svc(td, queue_size=50, maxlen=50, start=True, join_timeout_sec=2.0):
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
        date_str="20261113",
        join_timeout_sec=join_timeout_sec,
    )
    col = TickCollector(FidMap(_tick_fids()), tb, stats, sess, sink=svc)
    hob = OrderbookCollector(FidMap({}), hb, stats, sess, sink=svc)
    if start:
        svc.start()
    return svc, col, hob, stats, tb, hb, sess


def _load_stats(td):
    path = os.path.join(td, "collector_stats.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


class BufferStatsMeaningTests(unittest.TestCase):
    def test_enqueued_occupancy_is_not_writer_wait(self):
        with tempfile.TemporaryDirectory() as td:
            svc, col, _h, stats, tb, _hb, _s = _svc(td, queue_size=100, maxlen=8, start=False)
            for _ in range(5):
                col.handle("005930", "주식체결")
            self.assertEqual(stats.writer_queue_high_water, 5)
            self.assertEqual(stats.tick_buffer_high_water, 5)
            self.assertEqual(stats.tick_salvage_pending_high_water, 0)
            self.assertEqual(tb.dropped_overflow, 0)
            self.assertEqual(stats.dropped_overflow, 0)
            svc.close()
            d = _load_stats(td)
            self.assertEqual(d["tick_buffer_role"], BUFFER_ROLE_SALVAGE_HISTORY)
            self.assertEqual(d["writer_queue_role"], WRITER_QUEUE_ROLE_BACKLOG)
            self.assertEqual(d["writer_queue_high_water"], 5)
            self.assertEqual(d["data_loss"], 0)
            self.assertEqual(d["terminal_losses"], 0)
            self.assertIn("not writer wait", d["buffer_stats_note"]["tick_buffer_high_water"])

    def test_queue_full_is_salvage_pending_not_loss(self):
        with tempfile.TemporaryDirectory() as td:
            svc, col, _h, stats, tb, _hb, _s = _svc(td, queue_size=2, maxlen=20, start=False)
            for _ in range(6):
                col.handle("005930", "주식체결")
            self.assertEqual(stats.writer_queue_high_water, 2)
            self.assertGreaterEqual(stats.writer_queue_full_attempts, 1)
            self.assertGreaterEqual(stats.tick_salvage_pending_high_water, 1)
            self.assertEqual(stats.dropped_overflow, 0)
            self.assertEqual(len(tb), 6)
            svc.close()
            self.assertEqual(stats.write_ok, 6)
            self.assertEqual(stats.terminal_losses(), 0)
            self.assertTrue(stats.identity_ok())

    def test_history_evicted_is_not_data_loss(self):
        with tempfile.TemporaryDirectory() as td:
            svc, col, _h, stats, tb, _hb, _s = _svc(td, queue_size=50, maxlen=2, start=False)
            for _ in range(5):
                col.handle("005930", "주식체결")
            self.assertEqual(len(tb), 2)
            self.assertEqual(stats.dropped_overflow, 0)
            self.assertEqual(tb.dropped_overflow, 0)
            self.assertEqual(stats.buffer_history_evicted, 3)
            svc.close()
            self.assertEqual(stats.write_ok, 5)
            self.assertEqual(stats.terminal_losses(), 0)

    def test_true_overflow_is_data_loss(self):
        stats = CollectorStats()
        buf = RealtimeBuffer(maxlen=2)
        sess = CollectSession()
        sess.begin()
        col = TickCollector(FidMap(_tick_fids()), buf, stats, sess)
        for _ in range(5):
            col.handle("005930", "주식체결")
        self.assertEqual(stats.dropped_overflow, 3)
        self.assertEqual(stats.buffer_history_evicted, 0)
        self.assertEqual(stats.tick_salvage_pending_high_water, 2)
        self.assertEqual(stats.terminal_losses(), 3)

    def test_csv_order_preserved(self):
        with tempfile.TemporaryDirectory() as td:
            svc, col, hob, stats, _tb, _hb, _s = _svc(td)
            for i in range(4):
                col.handle("005930", "주식체결")
            hob.handle("005930", "주식호가잔량")
            col.handle("005930", "주식체결")
            svc.close()
            ticks = [n for n in os.listdir(td) if n.startswith("tick_") and n.endswith(".csv")]
            path = os.path.join(td, ticks[0])
            with open(path, encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            seqs = [int(r["seq"]) for r in rows]
            self.assertEqual(seqs, sorted(seqs))
            self.assertEqual(len(rows), 5)
            self.assertEqual(stats.write_ok, 6)


class ShutdownOrderTests(EnvIsolatedTestCase):
    def test_bridge_final_json_is_closed_and_unregistered(self):
        with EnvGuard("1"):
            kiwoom = FakeKiwoom()
            with tempfile.TemporaryDirectory() as td:
                b = attach_bridge(kiwoom, ["005930"], td)
                kiwoom.OnReceiveRealData.emit("005930", "주식체결", "")
                kiwoom.OnReceiveRealData.emit("005930", "주식호가잔량", "")
                b.close()
                d = _load_stats(td)
                self.assertEqual(d["shutdown_status"], "COMPLETE")
                self.assertEqual(d["bridge_status"], BRIDGE_CLOSED)
                self.assertEqual(d["screen_state"]["8101"], "NOT_REGISTERED")
                self.assertEqual(d["screen_state"]["8102"], "NOT_REGISTERED")
                self.assertFalse(d["screens_active"])
                self.assertEqual(d["pending_records"], 0)
                self.assertEqual(d["pending_queue"], 0)
                self.assertEqual(d["pending_inflight"], 0)
                self.assertEqual(d["terminal_losses"], 0)
                self.assertEqual(d["write_ok"], 2)
                trace = d["shutdown_trace"]
                self.assertEqual(trace[0], "halt_input")
                self.assertLess(trace.index("unregister_8101"), trace.index("unregister_8102"))
                self.assertLess(trace.index("unregister_8102"), trace.index("writer_close"))
                self.assertLess(trace.index("writer_close"), trace.index("bridge_closed"))
                self.assertLess(trace.index("bridge_closed"), trace.index("stats_dump"))
                rems = [c for c in kiwoom.calls if str(c[0]).startswith("SetRealRemove")]
                self.assertEqual(str(rems[0][1]), SCREEN_TICK)
                self.assertEqual(str(rems[1][1]), SCREEN_HOGA)
                self.assertFalse(any(str(c[1]) == "ALL" and str(c[2]) == "ALL" for c in rems))
                names = [str(c[0]) for c in kiwoom.calls]
                self.assertFalse(any(n.startswith("SendOrder") for n in names))
                self.assertFalse(any(n.startswith("CommTerminate") for n in names))

    def test_shutdown_session_does_not_preclose_writer(self):
        with EnvGuard("1"):
            os.environ[ENV_SAFE] = "1"
            os.environ[ENV_COLLECT_ONLY] = "1"
            os.environ.pop(ENV_LOGIN_ONLY, None)
            api = PreparedKiwoom()
            sess = prepare_api(api)
            with tempfile.TemporaryDirectory() as td:
                b = attach_bridge(api, ["005930"], td)
                self.assertIsNotNone(b)
                api._scalp_bridge = b
                t = _WatchTimer()
                bind_watch_timer(sess, t)
                shutdown_session(sess, terminal="COMPLETE")
                d = _load_stats(td)
                self.assertEqual(d["shutdown_status"], "COMPLETE")
                self.assertEqual(d["bridge_status"], BRIDGE_CLOSED)
                self.assertFalse(d["screens_active"])
                names = _audit_names(sess)
                self.assertIn("SHUTDOWN_STARTED", names)
                self.assertIn("SESSION_FINISHED", names)
                self.assertNotIn("SESSION_INCOMPLETE", names)
                self.assertLess(names.index("SHUTDOWN_STARTED"), names.index("SESSION_FINISHED"))
                self.assertTrue(b.writer._closed)
                shutdown_session(sess, terminal="COMPLETE")
                self.assertEqual(_audit_names(sess).count("SESSION_FINISHED"), 1)


class ShutdownFailureTests(EnvIsolatedTestCase):
    def test_unreg_8101_failure_is_incomplete(self):
        with EnvGuard("1"):
            os.environ[ENV_SAFE] = "1"
            os.environ[ENV_COLLECT_ONLY] = "1"
            os.environ.pop(ENV_LOGIN_ONLY, None)
            api = PreparedKiwoom()
            sess = prepare_api(api)
            with tempfile.TemporaryDirectory() as td:
                b = attach_bridge(api, ["005930"], td)
                api._scalp_bridge = b

                orig = api.dynamicCall

                def boom(name, *args):
                    if str(name).startswith("SetRealRemove"):
                        flat = args[0] if args else ""
                        if isinstance(flat, (list, tuple)):
                            screen = str(flat[0])
                        else:
                            screen = str(flat)
                        if screen == SCREEN_TICK:
                            raise RuntimeError("unreg 8101")
                    return orig(name, *args)

                api.dynamicCall = boom
                t = _WatchTimer()
                bind_watch_timer(sess, t)
                shutdown_session(sess, terminal="COMPLETE")
                self.assertEqual(b.stats.shutdown_status, "INCOMPLETE")
                self.assertEqual(b.stats.bridge_status, BRIDGE_CLOSED)
                names = _audit_names(sess)
                self.assertIn("SESSION_INCOMPLETE", names)
                self.assertNotIn("SESSION_FINISHED", names)

    def test_unreg_8102_failure_is_incomplete(self):
        with EnvGuard("1"):
            kiwoom = FakeKiwoom()
            kiwoom.fail_unreg_screens = {SCREEN_HOGA}
            with tempfile.TemporaryDirectory() as td:
                b = attach_bridge(kiwoom, ["005930"], td)
                b.close()
                d = _load_stats(td)
                self.assertEqual(d["shutdown_status"], "INCOMPLETE")
                self.assertEqual(d["bridge_status"], BRIDGE_CLOSED)
                self.assertEqual(d["screen_state"]["8101"], "NOT_REGISTERED")
                self.assertEqual(d["screen_state"]["8102"], "ERROR")
                self.assertFalse(d["screens_active"])
                self.assertIn("unregister_8102_failed", d["shutdown_trace"])

    def test_writer_join_timeout_is_incomplete(self):
        with EnvGuard("1"):
            kiwoom = FakeKiwoom()
            with tempfile.TemporaryDirectory() as td:
                b = attach_bridge(kiwoom, ["005930"], td)
                block = threading.Event()
                orig_write = b.writer._write_item

                def blocked(item):
                    block.wait(2.0)
                    orig_write(item)

                b.writer._write_item = blocked  # type: ignore
                kiwoom.OnReceiveRealData.emit("005930", "주식체결", "")
                time.sleep(0.05)
                try:
                    b.close(timeout=0.05)
                    self.assertTrue(b.stats.writer_join_timeout)
                    self.assertEqual(b.stats.shutdown_status, "INCOMPLETE")
                    self.assertEqual(b.stats.bridge_status, BRIDGE_CLOSED)
                    d = _load_stats(td)
                    self.assertEqual(d["shutdown_status"], "INCOMPLETE")
                    self.assertEqual(d["bridge_status"], BRIDGE_CLOSED)
                finally:
                    block.set()
                    b.writer._stop.set()
                    if b.writer._thread is not None:
                        b.writer._thread.join(1.0)
                    try:
                        b.writer.tick_writer.close()
                        b.writer.hoga_writer.close()
                    except Exception:
                        pass

    def test_csv_flush_failure_is_incomplete(self):
        with EnvGuard("1"):
            kiwoom = FakeKiwoom()
            with tempfile.TemporaryDirectory() as td:
                b = attach_bridge(kiwoom, ["005930"], td)

                def boom_close():
                    raise RuntimeError("flush fail")

                b.writer.tick_writer.close = boom_close  # type: ignore
                b.close()
                d = _load_stats(td)
                self.assertEqual(d["shutdown_status"], "INCOMPLETE")
                self.assertEqual(d["bridge_status"], BRIDGE_CLOSED)

    def test_stats_dump_failure_is_incomplete(self):
        with EnvGuard("1"):
            os.environ[ENV_SAFE] = "1"
            os.environ[ENV_COLLECT_ONLY] = "1"
            os.environ.pop(ENV_LOGIN_ONLY, None)
            api = PreparedKiwoom()
            sess = prepare_api(api)
            with tempfile.TemporaryDirectory() as td:
                b = attach_bridge(api, ["005930"], td)
                api._scalp_bridge = b
                b.stats.dump_json = lambda path: False  # type: ignore
                t = _WatchTimer()
                bind_watch_timer(sess, t)
                shutdown_session(sess, terminal="COMPLETE")
                self.assertEqual(b.stats.shutdown_status, "STATS_DUMP_FAILED")
                names = _audit_names(sess)
                self.assertIn("SESSION_INCOMPLETE", names)
                self.assertNotIn("SESSION_FINISHED", names)


class OcxCounterTests(EnvIsolatedTestCase):
    def test_method_probe_runtime_and_identity(self):
        os.environ[ENV_SAFE] = "1"
        os.environ[ENV_COLLECT_ONLY] = "1"
        os.environ.pop(ENV_LOGIN_ONLY, None)
        api = RecordingApi()
        sess = prepare_api(api)
        g = sess.guard
        self.assertEqual(g.orig_calls, g.orig_methods_sum())
        probe_connect = g.orig_ocx_by_method_payload()["GetConnectState"]["probe"]
        self.assertGreaterEqual(probe_connect, 1)
        self.assertEqual(g.orig_ocx_by_method_payload()["GetConnectState"]["runtime"], 0)
        api.dynamicCall("CommConnect()")
        api.dynamicCall("GetConnectState()")
        api.dynamicCall("GetLoginInfo(QString)", "GetServerGubun")
        runtime = g.orig_ocx_by_method_payload()
        self.assertEqual(runtime["CommConnect"]["runtime"], 1)
        self.assertEqual(runtime["GetConnectState"]["runtime"], 1)
        self.assertEqual(runtime["GetLoginInfo:GetServerGubun"]["runtime"], 1)
        blocked_before = g.orig_calls
        with self.assertRaises(SendOrderBlockedError):
            api.dynamicCall("SendOrder(QString)", ["주문"])
        with self.assertRaises(SendOrderBlockedError):
            api.dynamicCall("CommTerminate()")
        with self.assertRaises(CollectOnlyBlockedError):
            api.dynamicCall("GetLoginInfo(QString)", "ACCNO")
        with self.assertRaises(CollectOnlyBlockedError):
            api.dynamicCall("SetRealRemove(QString, QString)", ["ALL", "ALL"])
        self.assertEqual(int(g.orig_calls), blocked_before)
        self.assertEqual(g.orig_calls, g.orig_methods_sum())
        snap = str(g.snapshot())
        self.assertNotIn("ACCNO", snap)
        self.assertNotIn("PASSWORD", snap)
        counters = sess.audit.counters_from(g)
        self.assertEqual(counters["orig_ocx_calls"], g.orig_calls)
        self.assertIn("orig_ocx_by_method", counters)

    def test_setrealremove_screens_counted_on_close(self):
        os.environ[ENV_SAFE] = "1"
        os.environ[ENV_COLLECT_ONLY] = "1"
        os.environ.pop(ENV_LOGIN_ONLY, None)
        api = PreparedKiwoom()
        sess = prepare_api(api)
        g = sess.guard
        with tempfile.TemporaryDirectory() as td:
            b = attach_bridge(api, ["005930"], td)
            api._scalp_bridge = b
            before = g.orig_setrealremove_payload()
            self.assertEqual(before["8101"]["runtime"], 0)
            self.assertEqual(before["8102"]["runtime"], 0)
            t = _WatchTimer()
            bind_watch_timer(sess, t)
            shutdown_session(sess, terminal="COMPLETE")
            after = g.orig_setrealremove_payload()
            self.assertEqual(after["8101"]["runtime"], 1)
            self.assertEqual(after["8102"]["runtime"], 1)
            self.assertEqual(g.orig_calls, g.orig_methods_sum())
            d = _load_stats(td)
            self.assertEqual(d["orig_ocx_setrealremove"]["8101"]["runtime"], 1)
            self.assertEqual(d["orig_ocx_setrealremove"]["8102"]["runtime"], 1)
            fin = [e for e in load_events(sess.audit.path) if e["event"] == "SESSION_FINISHED"][0]
            self.assertEqual(fin["counters"]["orig_ocx_setrealremove"]["8101"]["runtime"], 1)
            blob = open(sess.audit.path, encoding="utf-8").read()
            self.assertNotIn("USER_ID", blob)
            self.assertNotIn("PASSWORD", blob)

    def test_unknown_is_not_hidden(self):
        api = RecordingApi()
        g = install_sendorder_guard(api)
        api.dynamicCall("CommRqData(QString, QString, int, QString)", "x", "opt10001", 0, "0101")
        payload = g.orig_ocx_by_method_payload()
        self.assertEqual(payload["unknown"]["runtime"], 1)
        self.assertEqual(g.orig_calls, g.orig_methods_sum())
        key, _screen = classify_orig_ocx_method("CommRqData(QString)", ("x",))
        self.assertEqual(key, "unknown")

    def test_login_only_blocked_not_counted_as_orig(self):
        os.environ[ENV_SAFE] = "1"
        os.environ[ENV_LOGIN_ONLY] = "1"
        os.environ.pop(ENV_COLLECT_ONLY, None)
        api = RecordingApi()
        sess = prepare_api(api)
        g = sess.guard
        orig = int(g.orig_calls)
        with self.assertRaises(LoginOnlyBlockedError):
            api.dynamicCall("SetRealReg(QString, QString, QString, QString)", "8101", "005930", "10", "0")
        self.assertEqual(int(g.orig_calls), orig)
        self.assertEqual(g.orig_calls, g.orig_methods_sum())


class RegressionSafeModeTests(EnvIsolatedTestCase):
    def test_login_only_still_blocks_hft_screens(self):
        os.environ[ENV_SAFE] = "1"
        os.environ[ENV_LOGIN_ONLY] = "1"
        os.environ.pop(ENV_COLLECT_ONLY, None)
        api = RecordingApi()
        sess = prepare_api(api)
        with self.assertRaises(LoginOnlyBlockedError):
            api.dynamicCall("SetRealRemove(QString, QString)", "8101", "ALL")
        names = [str(c[0]) for c in api.calls]
        self.assertFalse(any(n.startswith("SendOrder") for n in names))
        self.assertFalse(any(n.startswith("CommTerminate") for n in names))
        self.assertEqual(sess.guard.orig_calls, sess.guard.orig_methods_sum())

    def test_collect_only_still_blocks_all_all(self):
        os.environ[ENV_SAFE] = "1"
        os.environ[ENV_COLLECT_ONLY] = "1"
        os.environ.pop(ENV_LOGIN_ONLY, None)
        api = RecordingApi()
        sess = prepare_api(api)
        with self.assertRaises(CollectOnlyBlockedError):
            api.dynamicCall("SetRealRemove(QString, QString)", ["ALL", "ALL"])
        with self.assertRaises(SendOrderBlockedError):
            api.dynamicCall("SendOrder(QString)", ["주문"])
        with self.assertRaises(SendOrderBlockedError):
            api.dynamicCall("CommTerminate()")
        self.assertFalse(any(str(c[0]).startswith("SendOrder") for c in api.calls))
        self.assertFalse(any(str(c[0]).startswith("CommTerminate") for c in api.calls))


if __name__ == "__main__":
    unittest.main()
