"""HFT 2단계-C 안전 실행기 가짜 객체 테스트. 키움 로그인/OCX/실제 주문 없음."""
from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import order_safety as osf  # noqa: E402
from scalp.bridge import attach_bridge  # noqa: E402
from scalp.collector import SCREEN_HOGA, SCREEN_TICK  # noqa: E402
from tests.test_scalp_bridge import EnvGuard, FakeKiwoom, _regs  # noqa: E402
from tools.stage2c_login_only_audit import (  # noqa: E402
    ENV_AUDIT_DIR,
    ENV_HEARTBEAT_SEC,
    AuditInitError,
    LoginOnlyAudit,
    emit_safe,
    load_events,
    public_reason,
    sanitize_reason,
    still_sensitive,
)
from tools.stage2c_safe_runner import (  # noqa: E402
    ATTR_ABORTED,
    ATTR_ARMED,
    ATTR_MOCK_CONFIRMED,
    ATTR_TIMER_STOPPED,
    ENV_COLLECT_ONLY,
    ENV_LIVE,
    ENV_LOGIN_ONLY,
    ENV_SAFE,
    ENV_SCALP,
    ENV_STAGE2D_CODES,
    LOGIN_ONLY_GETLOGININFO_ALLOW,
    LOGIN_ONLY_LOGIN_RESULT,
    LOGIN_ONLY_MOCK_GUBUN,
    MUST_PASS_PREFIXES,
    REASON_METHOD_BLOCKED,
    GuardInstallError,
    LoginOnlyBlockedError,
    SendOrderBlockedError,
    SendOrderGuard,
    SessionAbortError,
    abort_session,
    apply_safe_mode_env,
    apply_session_env,
    arm_engine_timer_stop,
    assert_guard_intact,
    assert_login_only_env,
    assert_ready_for_connect,
    bind_watch_timer,
    block_timer_restart,
    confirm_login_only_mock_server,
    confirm_timer_stopped,
    install_sendorder_guard,
    is_blocked_method,
    is_login_only_allowed_method,
    login_only_info_tag,
    live_login_forbidden,
    login_only_enabled,
    main as runner_main,
    method_base,
    on_guard_watch_tick,
    prepare_api,
    safe_mode_enabled,
    shutdown_session,
    stop_engine_timer,
    wrap_engine_start_stop_timer,
)

_STAGE2C_ENV_KEYS = (
    ENV_SAFE,
    ENV_LIVE,
    ENV_SCALP,
    ENV_LOGIN_ONLY,
    ENV_COLLECT_ONLY,
    ENV_STAGE2D_CODES,
    ENV_AUDIT_DIR,
    ENV_HEARTBEAT_SEC,
)
_MODULE_ENV_SNAP = {k: os.environ.get(k) for k in _STAGE2C_ENV_KEYS}


def tearDownModule():
    for k, v in _MODULE_ENV_SNAP.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


class RecordingApi:
    def __init__(self) -> None:
        self.calls = []
        self.connect_state = 1
        self.login_gubun = "1"
        self.server_gubun = "1"
        self._tr_responses = {}
        self._last_error = None
        self.real = {"005930": {10: "70000", 13: "10", 15: "1", 20: "090015", 228: "55"}}
        self.orig_send_hits = 0
        self.orig_cancel_hits = 0

    def isNull(self):
        return False

    def send_order_market(self, account, code, qty, order_type):
        self.orig_send_hits += 1
        self.calls.append(("send_order_market", account, code, qty, order_type))
        return "ORIG_SEND"

    def cancel_order(self, account, order):
        self.orig_cancel_hits += 1
        self.calls.append(("cancel_order", account, getattr(order, "code", "")))
        return "ORIG_CANCEL"

    def dynamicCall(self, name, *args):
        self.calls.append((str(name),) + tuple(args))
        n = str(name)
        if n.startswith("GetConnectState"):
            return self.connect_state
        if n.startswith("GetLoginInfo"):
            return self.login_gubun
        if n.startswith("GetCommRealData"):
            code = args[0] if args else ""
            fid = int(args[1]) if len(args) > 1 else 0
            return str(self.real.get(str(code), {}).get(fid, ""))
        if n.startswith("SetRealReg") or n.startswith("SetRealRemove"):
            return 0
        if n.startswith("CommRqData"):
            return 0
        if n.startswith("GetCommData"):
            return "100"
        return 0


class FrozenDynamicCall:
    def __init__(self) -> None:
        object.__setattr__(self, "calls", [])
        object.__setattr__(self, "dynamicCall", self._orig)

    def _orig(self, name, *args):
        self.calls.append(str(name))
        return 0

    def __setattr__(self, key, value):
        if key == "dynamicCall":
            return
        object.__setattr__(self, key, value)


class FakeTimer:
    def __init__(self) -> None:
        self._active = False
        self.start_count = 0
        self.stop_count = 0

    def start(self, *a, **k):
        self._active = True
        self.start_count += 1

    def stop(self):
        self._active = False
        self.stop_count += 1

    def isActive(self):
        return self._active


class FakeEngine:
    def __init__(self) -> None:
        self.timer = FakeTimer()
        self.ui_timer = FakeTimer()
        self.on_tick_count = 0

    def start(self, account_password=""):
        self.ui_timer.start()
        self.timer.start()

    def on_tick(self):
        self.on_tick_count += 1


class EnvRestore:
    def __init__(self, *keys: str) -> None:
        self.keys = keys
        self._old = {k: os.environ.get(k) for k in keys}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class EnvIsolatedTestCase(unittest.TestCase):
    """각 테스트는 STAGE2C/SCALP/LIVE 키 없이 시작하고, 종료 시 프로세스 값을 복구한다."""

    def setUp(self):
        self._env_iso = EnvRestore(*_STAGE2C_ENV_KEYS)
        self._env_iso.__enter__()
        for k in _STAGE2C_ENV_KEYS:
            os.environ.pop(k, None)
        self._audit_tmp = tempfile.TemporaryDirectory(prefix="s2c_audit_")
        os.environ[ENV_AUDIT_DIR] = self._audit_tmp.name

    def tearDown(self):
        try:
            self._audit_tmp.cleanup()
        except Exception:
            pass
        self._env_iso.__exit__(None, None, None)


class MethodNameTests(EnvIsolatedTestCase):
    def test_sendorder_variants_are_blocked(self):
        self.assertEqual(method_base("SendOrder(QString, QString)"), "SendOrder")
        self.assertTrue(is_blocked_method("SendOrder(QString, QString, QString, int, QString, int, int, QString, QString)"))
        self.assertTrue(is_blocked_method("SendOrderFO(QString, QString)"))
        self.assertTrue(is_blocked_method("CommTerminate()"))
        self.assertFalse(is_blocked_method("GetCommRealData(QString, int)"))
        self.assertFalse(is_blocked_method("SetRealReg(QString, QString, QString, QString)"))
        self.assertFalse(is_blocked_method("CommRqData(QString, QString, int, QString)"))


class SendOrderGuardTests(EnvIsolatedTestCase):
    def _install(self):
        api = RecordingApi()
        g = install_sendorder_guard(api)
        orig_n = len(api.calls)
        return api, g, orig_n

    def test_sendorder_does_not_reach_orig(self):
        api, g, orig_n = self._install()
        with self.assertRaises(SendOrderBlockedError):
            api.dynamicCall(
                "SendOrder(QString, QString, QString, int, QString, int, int, QString, QString)",
                ["주문", "0101", "A", 1, "005930", 1, 0, "03", ""],
            )
        self.assertEqual(len(api.calls), orig_n)
        self.assertGreaterEqual(g.send_order_blocked, 2)

    def test_sendorderfo_does_not_reach_orig(self):
        api, g, orig_n = self._install()
        with self.assertRaises(SendOrderBlockedError):
            api.dynamicCall("SendOrderFO(QString, QString)", ["FO", "x"])
        self.assertEqual(len(api.calls), orig_n)
        self.assertTrue(any(x["base"] == "SendOrderFO" for x in g.blocked))

    def test_blocked_args_and_counter_recorded(self):
        api, g, _ = self._install()
        before = g.send_order_blocked
        payload = ["주문", "0101", "ACCT", 2, "000660", 3, 0, "03", ""]
        with self.assertRaises(SendOrderBlockedError):
            api.dynamicCall("SendOrder(QString)", payload)
        rec = g.blocked[-1]
        self.assertEqual(g.send_order_blocked, before + 1)
        self.assertIn("ts", rec)
        self.assertTrue(rec["method"].startswith("SendOrder"))
        self.assertNotIn("args", rec)
        dumped = str(rec)
        self.assertNotIn("000660", dumped)
        self.assertNotIn("ACCT", dumped)
        self.assertNotIn("0101", dumped)

    def test_getcommrealdata_passthrough(self):
        api, g, orig_n = self._install()
        v = api.dynamicCall("GetCommRealData(QString, int)", "005930", 10)
        self.assertEqual(v, "70000")
        self.assertGreater(len(api.calls), orig_n)
        self.assertTrue(str(api.calls[-1][0]).startswith("GetCommRealData"))
        self.assertGreaterEqual(g.passthrough, 1)

    def test_setrealreg_passthrough(self):
        api, _g, orig_n = self._install()
        ret = api.dynamicCall(
            "SetRealReg(QString, QString, QString, QString)",
            "8101",
            "005930",
            "10;13;15;20;228",
            "0",
        )
        self.assertEqual(ret, 0)
        self.assertGreater(len(api.calls), orig_n)
        self.assertTrue(str(api.calls[-1][0]).startswith("SetRealReg"))

    def test_tr_passthrough(self):
        api, _g, orig_n = self._install()
        rq = api.dynamicCall("CommRqData(QString, QString, int, QString)", "조회", "opt10001", 0, "0404")
        data = api.dynamicCall("GetCommData(QString, QString, int, QString)", "opt10001", "조회", 0, "현재가")
        st = api.dynamicCall("GetConnectState()")
        self.assertEqual(rq, 0)
        self.assertEqual(data, "100")
        self.assertEqual(st, 1)
        self.assertGreater(len(api.calls), orig_n + 2)
        names = [str(c[0]) for c in api.calls[orig_n:]]
        self.assertTrue(any(n.startswith("CommRqData") for n in names))
        self.assertTrue(any(n.startswith("GetCommData") for n in names))
        self.assertTrue(any(n.startswith("GetConnectState") for n in names))

    def test_commterminate_blocked(self):
        api, g, orig_n = self._install()
        with self.assertRaises(SendOrderBlockedError):
            api.dynamicCall("CommTerminate()")
        self.assertEqual(len(api.calls), orig_n)
        self.assertGreaterEqual(g.comm_terminate_blocked, 1)

    def test_assignment_failure_aborts(self):
        frozen = FrozenDynamicCall()
        with self.assertRaises(GuardInstallError):
            install_sendorder_guard(frozen)
        frozen.dynamicCall("SendOrder(QString)", ["x"])
        self.assertEqual(frozen.calls, ["SendOrder(QString)"])

    def test_second_install_is_idempotent(self):
        api = RecordingApi()
        g1 = install_sendorder_guard(api)
        g2 = install_sendorder_guard(api)
        self.assertIs(g1, g2)
        assert_guard_intact(api)

    def test_unwrap_is_detected(self):
        api, g, _ = self._install()
        orig = g.orig
        api.dynamicCall = orig
        with self.assertRaises(GuardInstallError):
            assert_guard_intact(api)
        with self.assertRaises(GuardInstallError):
            install_sendorder_guard(api)

    def test_delete_wrapper_is_bypass_and_detected(self):
        api, _g, _ = self._install()
        del api.dynamicCall
        with self.assertRaises(GuardInstallError):
            assert_guard_intact(api)
        api.dynamicCall("SendOrder(QString)", ["bypass"])
        self.assertTrue(any(str(c[0]).startswith("SendOrder") for c in api.calls))

    def test_guarded_send_order_on_mock_does_not_hit_orig(self):
        """LIVE_ORDERS_ENABLED=0 이어도 모의는 게이트를 통과한다. 래퍼가 OCX 를 막는다."""
        api = RecordingApi()
        api.login_gubun = "1"
        api.server_gubun = "1"
        gate = osf.OrderSafetyGate(env={osf.ENV_LIVE_ORDERS: "0"})
        api.safety_gate = gate
        install_sendorder_guard(api)
        res = osf.guarded_send_order(
            api,
            rqname="주문",
            screen_no="0101",
            account="A",
            order_type=osf.ORDER_TYPE_BUY,
            code="005930",
            qty=1,
            price=0,
            hoga="03",
            gate=gate,
        )
        self.assertFalse(res.accepted)
        self.assertEqual(res.reason, "SENDORDER_EXCEPTION")
        self.assertFalse(any(str(c[0]).startswith("SendOrder") for c in api.calls))
        self.assertTrue(any(str(c[0]).startswith("GetConnectState") for c in api.calls))


class TimerStopTests(EnvIsolatedTestCase):
    def test_start_then_timer_stopped_other_timers_untouched(self):
        arm_engine_timer_stop(FakeEngine)
        eng = FakeEngine()
        eng.ui_timer.start()
        eng.start()
        self.assertTrue(getattr(eng, ATTR_TIMER_STOPPED))
        self.assertFalse(eng.timer.isActive())
        self.assertGreaterEqual(eng.timer.stop_count, 1)
        self.assertTrue(eng.ui_timer.isActive())
        self.assertEqual(eng.on_tick_count, 0)

    def test_stop_engine_timer_direct(self):
        eng = FakeEngine()
        eng.timer.start()
        eng.ui_timer.start()
        stop_engine_timer(eng)
        self.assertFalse(eng.timer.isActive())
        self.assertTrue(eng.ui_timer.isActive())


class BridgeAndWriterTests(EnvIsolatedTestCase):
    def test_scalp_enabled_keeps_product_and_hft_handlers(self):
        with EnvGuard("1"):
            k = FakeKiwoom()
            product = k._on_receive_real_data
            g = install_sendorder_guard(k)
            with tempfile.TemporaryDirectory() as td:
                b = attach_bridge(k, ["005930"], td)
                self.assertIsNotNone(b)
                self.assertIn(product, k.OnReceiveRealData.slots)
                self.assertEqual(len(k.OnReceiveRealData.slots), 2)
                self.assertTrue(any(s is not product for s in k.OnReceiveRealData.slots))
                self.assertTrue(_regs(k.calls, SCREEN_TICK))
                self.assertTrue(_regs(k.calls, SCREEN_HOGA))
                k.OnReceiveRealData.emit("005930", "주식체결", "")
                self.assertEqual(k.product_got[-1], ("005930", "주식체결"))
                self.assertGreaterEqual(b.stats.tick_callbacks, 1)
                assert_guard_intact(k)
                self.assertFalse(any(str(c[0]).startswith("SendOrder") for c in k.calls))
                b.close()
            self.assertGreaterEqual(g.passthrough, 1)

    def test_scalp_disabled_does_not_attach(self):
        with EnvGuard("0"):
            k = FakeKiwoom()
            product = k._on_receive_real_data
            install_sendorder_guard(k)
            with tempfile.TemporaryDirectory() as td:
                b = attach_bridge(k, ["005930"], td)
                self.assertIsNone(b)
            self.assertEqual(k.OnReceiveRealData.slots, [product])
            self.assertFalse(any(str(c[0]).startswith("SetRealReg") for c in k.calls))

    def test_writer_flush_on_close(self):
        with EnvGuard("1"):
            k = FakeKiwoom()
            install_sendorder_guard(k)
            with tempfile.TemporaryDirectory() as td:
                b = attach_bridge(k, ["005930"], td)
                self.assertIsNotNone(b)
                k.OnReceiveRealData.emit("005930", "주식체결", "")
                k.OnReceiveRealData.emit("005930", "주식호가잔량", "")
                b.close()
                names = os.listdir(td)
                ticks = [n for n in names if n.startswith("tick_") and n.endswith(".csv")]
                self.assertTrue(ticks, names)
                self.assertTrue(os.path.isfile(os.path.join(td, "collector_stats.json")))
                self.assertIn(b.stats.shutdown_status, ("COMPLETE", "STATS_DUMP_FAILED"))
                self.assertTrue(b.closed)


class RunnerCliTests(EnvIsolatedTestCase):
    def test_live_flags_forbidden(self):
        self.assertTrue(live_login_forbidden(["--login"]))
        self.assertTrue(live_login_forbidden(["--live"]))
        self.assertTrue(live_login_forbidden(["--ocx"]))
        self.assertFalse(live_login_forbidden(["--self-test"]))

    def test_main_refuses_login(self):
        rc = runner_main(["--login"])
        self.assertEqual(rc, 2)

    def test_apply_session_env(self):
        with EnvRestore(ENV_LIVE, ENV_SCALP):
            os.environ[ENV_LIVE] = "1"
            os.environ[ENV_SCALP] = "0"
            got = apply_session_env()
            self.assertEqual(got[ENV_LIVE], "0")
            self.assertEqual(got[ENV_SCALP], "1")


class PreparedKiwoom(FakeKiwoom):
    def __init__(self) -> None:
        super().__init__()
        self.orig_send_hits = 0
        self.orig_cancel_hits = 0

    def isNull(self):
        return False

    def send_order_market(self, account, code, qty, order_type):
        self.orig_send_hits += 1
        self.calls.append(("send_order_market", account, code, qty, order_type))
        return "ORIG_SEND"

    def cancel_order(self, account, order):
        self.orig_cancel_hits += 1
        self.calls.append(("cancel_order", account, getattr(order, "code", "")))
        return "ORIG_CANCEL"


class _WatchTimer:
    def __init__(self) -> None:
        self.interval = 0
        self.started = False
        self.stopped = False
        self.timeout = FakeSignal() if False else types.SimpleNamespace(connect=self._connect)
        self._cb = None

    def _connect(self, fn):
        self._cb = fn

    def setInterval(self, ms):
        self.interval = int(ms)

    def start(self, *a, **k):
        self.started = True
        self.stopped = False

    def stop(self):
        self.stopped = True
        self.started = False

    def fire(self):
        if self._cb:
            self._cb()


class Stage2CSafeModeTests(EnvIsolatedTestCase):
    def setUp(self):
        super().setUp()
        os.environ[ENV_SAFE] = "1"
        os.environ[ENV_LIVE] = "0"
        os.environ[ENV_SCALP] = "1"
        os.environ.pop(ENV_LOGIN_ONLY, None)

    def test_login_blocked_before_guard(self):
        from PyQt5.QtWidgets import QApplication

        QApplication.instance() or QApplication([])
        import main as m

        api = RecordingApi()
        with self.assertRaises(SessionAbortError):
            m.KiwoomOpenAPI.login(api)
        self.assertFalse(any("CommConnect" in str(c[0]) for c in api.calls))

    def test_prepare_failure_is_fail_closed(self):
        frozen = FrozenDynamicCall()
        with self.assertRaises((GuardInstallError, SessionAbortError)):
            prepare_api(frozen)
        self.assertFalse(getattr(frozen, ATTR_ARMED, False))
        with self.assertRaises(SessionAbortError):
            assert_ready_for_connect(frozen)

    def test_guard_installed_before_connect(self):
        api = RecordingApi()
        sess = prepare_api(api)
        self.assertTrue(getattr(api, ATTR_ARMED))
        assert_ready_for_connect(api)
        self.assertIs(sess.guard, api._stage2c_guard)
        self.assertEqual(os.environ[ENV_SCALP], "1")
        self.assertEqual(os.environ[ENV_LIVE], "0")

    def test_post_login_integrity(self):
        api = RecordingApi()
        prepare_api(api)
        g = assert_guard_intact(api)
        confirm = assert_ready_for_connect(api)
        self.assertIs(g, confirm)

    def test_dynamiccall_sendorder_blocked(self):
        api = RecordingApi()
        prepare_api(api)
        n = len(api.calls)
        with self.assertRaises(SendOrderBlockedError):
            api.dynamicCall("SendOrder(QString)", ["주문", "0101"])
        self.assertEqual(len(api.calls), n)

    def test_dynamiccall_sendorderfo_blocked(self):
        api = RecordingApi()
        prepare_api(api)
        n = len(api.calls)
        with self.assertRaises(SendOrderBlockedError):
            api.dynamicCall("SendOrderFO(QString)", ["FO"])
        self.assertEqual(len(api.calls), n)

    def test_commterminate_blocked(self):
        api = RecordingApi()
        prepare_api(api)
        n = len(api.calls)
        with self.assertRaises(SendOrderBlockedError):
            api.dynamicCall("CommTerminate()")
        self.assertEqual(len(api.calls), n)

    def test_send_order_market_direct_blocked(self):
        api = RecordingApi()
        sess = prepare_api(api)
        hits = api.orig_send_hits
        res = api.send_order_market("A", "005930", 1, osf.ORDER_TYPE_BUY)
        self.assertFalse(res.accepted)
        self.assertEqual(res.reason, REASON_METHOD_BLOCKED)
        self.assertEqual(api.orig_send_hits, hits)
        self.assertFalse(any(str(c[0]).startswith("SendOrder") for c in api.calls))
        self.assertGreaterEqual(sess.guard.method_blocked, 1)

    def test_cancel_order_direct_blocked(self):
        api = RecordingApi()
        prepare_api(api)
        hits = api.orig_cancel_hits
        order = osf.OpenOrder("1", "005930", osf.KIND_BUY, 2)
        res = api.cancel_order("A", order)
        self.assertFalse(res.accepted)
        self.assertEqual(res.reason, REASON_METHOD_BLOCKED)
        self.assertEqual(api.orig_cancel_hits, hits)
        self.assertFalse(any(str(c[0]).startswith("SendOrder") for c in api.calls))

    def test_getcommrealdata_passthrough(self):
        api = RecordingApi()
        prepare_api(api)
        self.assertEqual(api.dynamicCall("GetCommRealData(QString, int)", "005930", 10), "70000")

    def test_setrealreg_passthrough(self):
        api = RecordingApi()
        prepare_api(api)
        self.assertEqual(
            api.dynamicCall("SetRealReg(QString, QString, QString, QString)", "8101", "005930", "10", "0"),
            0,
        )

    def test_commrqdata_passthrough(self):
        api = RecordingApi()
        prepare_api(api)
        self.assertEqual(
            api.dynamicCall("CommRqData(QString, QString, int, QString)", "조회", "opt10001", 0, "0404"),
            0,
        )

    def test_guard_delete_detected(self):
        api = RecordingApi()
        sess = prepare_api(api)
        del api.dynamicCall
        with self.assertRaises(GuardInstallError):
            assert_guard_intact(api)
        on_guard_watch_tick(sess)
        self.assertTrue(sess.aborted)
        self.assertTrue(getattr(api, ATTR_ABORTED))

    def test_guard_orig_reassign_detected(self):
        api = RecordingApi()
        sess = prepare_api(api)
        api.dynamicCall = sess.guard.orig
        with self.assertRaises(GuardInstallError):
            assert_guard_intact(api)
        on_guard_watch_tick(sess)
        self.assertTrue(sess.aborted)

    def test_guard_damage_stops_timer(self):
        api = RecordingApi()
        sess = prepare_api(api)
        eng = FakeEngine()
        eng.timer.start()
        sess.engine = eng
        del api.dynamicCall
        on_guard_watch_tick(sess)
        self.assertTrue(sess.aborted)
        self.assertFalse(eng.timer.isActive())

    def test_timer_restart_forbidden(self):
        api = RecordingApi()
        sess = prepare_api(api)
        eng = FakeEngine()
        wrap_engine_start_stop_timer(eng)
        eng.start()
        confirm_timer_stopped(eng)
        block_timer_restart(eng, sess)
        self.assertFalse(eng.timer.isActive())
        eng.timer.start()
        self.assertFalse(eng.timer.isActive())
        self.assertGreaterEqual(sess.timer_start_blocked, 1)

    def test_gui_resume_blocked(self):
        from PyQt5.QtWidgets import QApplication

        QApplication.instance() or QApplication([])
        import main as m

        eng = FakeEngine()
        wrap_engine_start_stop_timer(eng)
        eng.start()
        confirm_timer_stopped(eng)
        w = types.SimpleNamespace(
            engine=eng,
            logs=[],
            lbl_hint=types.SimpleNamespace(setText=lambda s: None),
        )
        w._append_log = lambda msg: w.logs.append(msg)
        w.refresh_ui = lambda: None
        m.TradingWindow._toggle_engine(w)
        self.assertFalse(eng.timer.isActive())
        eng.timer.start()
        m.TradingWindow._toggle_engine(w)
        self.assertFalse(eng.timer.isActive())
        self.assertTrue(any("재개 차단" in str(x) for x in w.logs))

    def test_hft_realtime_and_writer_kept(self):
        k = PreparedKiwoom()
        product = k._on_receive_real_data
        sess = prepare_api(k)
        with tempfile.TemporaryDirectory() as td:
            b = attach_bridge(k, ["005930"], td)
            self.assertIsNotNone(b)
            self.assertIn(product, k.OnReceiveRealData.slots)
            self.assertEqual(len(k.OnReceiveRealData.slots), 2)
            k.OnReceiveRealData.emit("005930", "주식체결", "")
            self.assertGreaterEqual(b.stats.tick_callbacks, 1)
            self.assertEqual(k.product_got[-1], ("005930", "주식체결"))
            shutdown_session(sess, terminal="COMPLETE")
            ticks = [n for n in os.listdir(td) if n.startswith("tick_") and n.endswith(".csv")]
            self.assertTrue(ticks)
            self.assertTrue(os.path.isfile(os.path.join(td, "collector_stats.json")))
            self.assertTrue(b.closed)
            self.assertIn(b.stats.shutdown_status, ("COMPLETE", "STATS_DUMP_FAILED"))

    def test_abort_incomplete_shutdown(self):
        k = PreparedKiwoom()
        sess = prepare_api(k)
        eng = FakeEngine()
        eng.timer.start()
        sess.engine = eng
        with tempfile.TemporaryDirectory() as td:
            b = attach_bridge(k, ["005930"], td)
            k.OnReceiveRealData.emit("005930", "주식체결", "")
            abort_session(sess, "테스트 훼손")
            self.assertTrue(sess.aborted)
            self.assertFalse(eng.timer.isActive())
            self.assertTrue(b.closed)
            self.assertFalse(b._accepting)
            self.assertEqual(b.stats.shutdown_status, "INCOMPLETE")
            self.assertTrue(os.path.isfile(os.path.join(td, "collector_stats.json")))

    def test_watch_timer_binds_250ms(self):
        api = RecordingApi()
        sess = prepare_api(api)
        t = _WatchTimer()
        t.timeout = types.SimpleNamespace(connect=t._connect)
        bind_watch_timer(sess, t)
        self.assertEqual(t.interval, 250)
        self.assertTrue(t.started)
        t.fire()
        self.assertFalse(sess.aborted)

    def test_safe_mode_off_is_noop(self):
        os.environ.pop(ENV_SAFE, None)
        self.assertFalse(safe_mode_enabled())
        api = RecordingApi()
        with self.assertRaises(SessionAbortError):
            prepare_api(api)
        self.assertFalse(getattr(api, ATTR_ARMED, False))
        n = len(api.calls)
        api.dynamicCall("SendOrder(QString)", ["x"])
        self.assertGreater(len(api.calls), n)
        with EnvGuard("0"):
            k = FakeKiwoom()
            product = k._on_receive_real_data
            with tempfile.TemporaryDirectory() as td:
                self.assertIsNone(attach_bridge(k, ["005930"], td))
            self.assertEqual(k.OnReceiveRealData.slots, [product])

    def test_single_sendorder_site_unchanged(self):
        import re

        hits = []
        for dirpath, dirnames, filenames in os.walk(ROOT):
            dirnames[:] = [
                d for d in dirnames
                if d not in (".git", "tests", "__pycache__", "node_modules", ".venv")
                and not d.startswith("backup")
            ]
            for fn in filenames:
                if not fn.endswith(".py"):
                    continue
                path = os.path.join(dirpath, fn)
                with open(path, encoding="utf-8", errors="replace") as f:
                    for i, line in enumerate(f, 1):
                        if re.search(r'"SendOrder\(QString', line):
                            hits.append(os.path.relpath(path, ROOT))
        self.assertEqual(hits, ["order_safety.py"], hits)


class LoginOnlyTests(EnvIsolatedTestCase):
    def test_env_login_only_requires_safe_mode(self):
        os.environ[ENV_LOGIN_ONLY] = "1"
        os.environ[ENV_SAFE] = "1"
        assert_login_only_env()
        self.assertTrue(login_only_enabled())

        os.environ[ENV_SAFE] = "0"
        with self.assertRaises(SessionAbortError):
            assert_login_only_env()

        os.environ.pop(ENV_SAFE, None)
        with self.assertRaises(SessionAbortError):
            assert_login_only_env()

        os.environ[ENV_LOGIN_ONLY] = "0"
        os.environ.pop(ENV_SAFE, None)
        assert_login_only_env()
        self.assertFalse(login_only_enabled())

    def test_abort_helper_matches_main_gate(self):
        from PyQt5.QtWidgets import QApplication

        QApplication.instance() or QApplication([])
        import main as m

        os.environ[ENV_LOGIN_ONLY] = "1"
        os.environ.pop(ENV_SAFE, None)
        msg = m._abort_if_login_only_without_safe()
        self.assertIsNotNone(msg)
        self.assertIn("STAGE2C_SAFE_MODE=1", str(msg))

        os.environ[ENV_SAFE] = "1"
        self.assertIsNone(m._abort_if_login_only_without_safe())

        os.environ[ENV_LOGIN_ONLY] = "0"
        os.environ.pop(ENV_SAFE, None)
        self.assertIsNone(m._abort_if_login_only_without_safe())

    def test_apply_safe_mode_env_login_only_forces_scalp_off(self):
        os.environ[ENV_LOGIN_ONLY] = "1"
        os.environ[ENV_SAFE] = "1"
        os.environ[ENV_SCALP] = "1"
        os.environ[ENV_LIVE] = "1"
        got = apply_safe_mode_env()
        self.assertEqual(got[ENV_SCALP], "0")
        self.assertEqual(got[ENV_LIVE], "0")
        self.assertEqual(os.environ[ENV_SCALP], "0")

        os.environ.pop(ENV_LOGIN_ONLY, None)
        os.environ[ENV_SCALP] = "0"
        got2 = apply_safe_mode_env()
        self.assertEqual(got2[ENV_SCALP], "1")

    def test_apply_session_env_unchanged_without_login_only(self):
        os.environ.pop(ENV_LOGIN_ONLY, None)
        os.environ[ENV_SCALP] = "0"
        got = apply_session_env()
        self.assertEqual(got[ENV_SCALP], "1")

    def test_guard_allows_connect_and_blocks_tr(self):
        os.environ[ENV_SAFE] = "1"
        os.environ[ENV_LOGIN_ONLY] = "1"
        api = RecordingApi()
        sess = prepare_api(api)
        g = sess.guard
        orig_before = int(g.orig_calls)
        blocked_before = int(g.login_only_blocked)
        send_before = int(g.send_order_blocked)

        n = len(api.calls)
        api.dynamicCall("CommConnect()")
        self.assertTrue(any(str(c[0]).startswith("CommConnect") for c in api.calls[n:]))
        st = api.dynamicCall("GetConnectState()")
        self.assertEqual(st, 1)
        info = api.dynamicCall("GetLoginInfo(QString)", "GetServerGubun")
        self.assertEqual(info, "1")
        self.assertGreaterEqual(g.orig_calls, orig_before + 3)
        orig_mid = int(g.orig_calls)
        with self.assertRaises(LoginOnlyBlockedError):
            api.dynamicCall("GetLoginInfo(QString)", "ACCNO")
        with self.assertRaises(LoginOnlyBlockedError):
            api.dynamicCall("GetLoginInfo(QString)", "USER_ID")
        self.assertEqual(int(g.orig_calls), orig_mid)

        with self.assertRaises(SendOrderBlockedError):
            api.dynamicCall("SendOrder(QString)", ["주문"])
        with self.assertRaises(SendOrderBlockedError):
            api.dynamicCall("CommTerminate()")
        with self.assertRaises(LoginOnlyBlockedError):
            api.dynamicCall("CommRqData(QString, QString, int, QString)", "조회", "opt10001", 0, "0404")
        with self.assertRaises(LoginOnlyBlockedError):
            api.dynamicCall("SetRealReg(QString, QString, QString, QString)", "8101", "005930", "10", "0")
        with self.assertRaises(LoginOnlyBlockedError):
            api.dynamicCall("SetRealRemove(QString, QString)", "ALL", "ALL")

        self.assertEqual(g.orig_calls, orig_before + 3)
        self.assertGreaterEqual(g.send_order_blocked, send_before + 1)
        self.assertGreaterEqual(g.comm_terminate_blocked, 1)
        self.assertGreaterEqual(g.login_only_blocked, blocked_before + 3)
        assert_ready_for_connect(api)

    def test_login_blocked_without_guard_in_login_only(self):
        from PyQt5.QtWidgets import QApplication

        QApplication.instance() or QApplication([])
        import main as m

        os.environ[ENV_SAFE] = "1"
        os.environ[ENV_LOGIN_ONLY] = "1"
        api = RecordingApi()
        with self.assertRaises(SessionAbortError):
            m.KiwoomOpenAPI.login(api)
        self.assertFalse(any("CommConnect" in str(c[0]) for c in api.calls))

    def test_post_login_skips_engine_bridge_scan(self):
        from PyQt5.QtWidgets import QApplication

        QApplication.instance() or QApplication([])
        import inspect
        import main as m

        os.environ[ENV_SAFE] = "1"
        os.environ[ENV_LOGIN_ONLY] = "1"
        api = RecordingApi()
        sess = prepare_api(api)
        created = {"engine": 0, "attach": 0, "ensure": 0, "scan": 0}

        real_engine = m.TradingEngine

        class _BoomEngine:
            def __init__(self, *a, **k):
                created["engine"] += 1
                raise AssertionError("LOGIN_ONLY 에서 TradingEngine 생성 금지")

        def _boom_attach(*a, **k):
            created["attach"] += 1
            raise AssertionError("LOGIN_ONLY 에서 attach_bridge 금지")

        def _boom_ensure(*a, **k):
            created["ensure"] += 1
            raise AssertionError("LOGIN_ONLY 에서 ensure_bridge 금지")

        def _boom_scan(*a, **k):
            created["scan"] += 1
            raise AssertionError("LOGIN_ONLY 에서 스캔 금지")

        watch = _WatchTimer()
        logs = []
        auto = types.SimpleNamespace(text="자동시작: ON", setText=lambda s: auto.__setattr__("text", str(s)))
        win = types.SimpleNamespace(
            kiwoom=None,
            account_no="-",
            engine=None,
            scan_message=types.SimpleNamespace(emit=lambda m: logs.append(str(m))),
            lbl_hint=types.SimpleNamespace(setText=lambda s: None),
            lbl_auto_start=auto,
            btn_overseas_login=types.SimpleNamespace(
                setEnabled=lambda v: None,
                setToolTip=lambda t: None,
            ),
        )
        with mock_patch_attr(m, "TradingEngine", _BoomEngine):
            m._stage2c_finish_login_only(win, api, sess, "8137514011", watch_timer=watch)
        self.assertEqual(created["engine"], 0)
        self.assertIs(win.kiwoom, api)
        self.assertEqual(watch.interval, 250)
        self.assertTrue(watch.started)
        self.assertTrue(any("MOCK_SERVER_CONFIRMED" in x for x in logs))
        self.assertEqual(auto.text, "자동시작: LOGIN_ONLY 차단")
        self.assertTrue(sess.mock_server_confirmed)
        self.assertTrue(getattr(api, ATTR_MOCK_CONFIRMED, False))
        self.assertFalse(any("8137514011" in x for x in logs))
        self.assertTrue(any(str(c[0]).startswith("GetConnectState") for c in api.calls))
        self.assertFalse(any(str(c[0]).startswith("CommRqData") for c in api.calls))
        self.assertFalse(any(str(c[0]).startswith("SetRealReg") for c in api.calls))
        src = inspect.getsource(m._stage2c_finish_login_only)
        self.assertNotIn("TradingEngine", src)
        self.assertNotIn("attach_bridge", src)
        self.assertNotIn("ensure_bridge", src)
        self.assertNotIn("_maybe_start_scan", src)
        self.assertNotIn("_scalp_try_sync", src)
        boot = inspect.getsource(m.main)
        self.assertLess(boot.find("_stage2c_finish_login_only"), boot.find("engine = TradingEngine"))
        self.assertIs(m.TradingEngine, real_engine)
        del _boom_attach, _boom_ensure, _boom_scan

    def test_safe_mode_without_login_only_still_passes_tr(self):
        os.environ[ENV_SAFE] = "1"
        os.environ.pop(ENV_LOGIN_ONLY, None)
        api = RecordingApi()
        prepare_api(api)
        ret = api.dynamicCall(
            "SetRealReg(QString, QString, QString, QString)",
            "8101",
            "005930",
            "10",
            "0",
        )
        self.assertEqual(ret, 0)
        self.assertTrue(any(str(c[0]).startswith("SetRealReg") for c in api.calls))

    def test_overseas_login_blocked(self):
        from PyQt5.QtWidgets import QApplication

        QApplication.instance() or QApplication([])
        import main as m

        os.environ[ENV_SAFE] = "1"
        os.environ[ENV_LOGIN_ONLY] = "1"
        calls = []

        class _Ovx:
            available = True

            def login(self, timeout_ms=0):
                calls.append("CommConnect")
                return ["6110111111"]

        class _Btn:
            def __init__(self):
                self.enabled = True
                self.tip = ""

            def setEnabled(self, v):
                self.enabled = bool(v)

            def setToolTip(self, t):
                self.tip = str(t)

            def isEnabled(self):
                return self.enabled

        btn = _Btn()
        logs = []
        auto = types.SimpleNamespace(text="자동시작: ON", setText=lambda s: auto.__setattr__("text", str(s)))
        w = types.SimpleNamespace(
            overseas_api=_Ovx(),
            btn_overseas_login=btn,
            lbl_hint=types.SimpleNamespace(setText=lambda s: None),
            lbl_auto_start=auto,
            lbl_overseas_status=types.SimpleNamespace(setText=lambda s: None),
            _append_overseas_log=lambda msg: logs.append(str(msg)),
        )
        m._apply_stage2c_login_only_ui(w)
        self.assertFalse(btn.enabled)
        self.assertEqual(auto.text, "자동시작: LOGIN_ONLY 차단")
        m.TradingWindow._check_overseas_login(w)
        self.assertEqual(calls, [])
        self.assertTrue(any("LOGIN_ONLY" in x for x in logs))

    def test_allowlist_names(self):
        self.assertTrue(is_login_only_allowed_method("CommConnect()"))
        self.assertTrue(is_login_only_allowed_method("GetConnectState()"))
        self.assertTrue(is_login_only_allowed_method("GetLoginInfo(QString)", ("GetServerGubun",)))
        self.assertFalse(is_login_only_allowed_method("GetLoginInfo(QString)"))
        self.assertFalse(is_login_only_allowed_method("GetLoginInfo(QString)", ("ACCNO",)))
        self.assertFalse(is_login_only_allowed_method("CommRqData(QString)"))
        self.assertFalse(is_login_only_allowed_method("SetRealReg(QString)"))
        self.assertFalse(is_login_only_allowed_method("SendOrder(QString)"))
        self.assertEqual(LOGIN_ONLY_GETLOGININFO_ALLOW, ("GetServerGubun",))


class LoginOnlyMockServerTests(EnvIsolatedTestCase):
    ACCOUNT = "8137514011"

    def _window(self, logs):
        hints = []
        return types.SimpleNamespace(
            kiwoom=None,
            account_no="-",
            engine=None,
            scan_message=types.SimpleNamespace(emit=lambda m: logs.append(str(m))),
            lbl_hint=types.SimpleNamespace(setText=lambda s: hints.append(str(s))),
            lbl_auto_start=types.SimpleNamespace(
                text="자동시작: ON",
                setText=lambda s, h=hints: h.append(str(s)),
            ),
            btn_overseas_login=types.SimpleNamespace(
                setEnabled=lambda v: None,
                setToolTip=lambda t: None,
            ),
            _hints=hints,
        )

    def _prepare(self, gubun="1"):
        os.environ[ENV_SAFE] = "1"
        os.environ[ENV_LOGIN_ONLY] = "1"
        api = RecordingApi()
        api.login_gubun = gubun
        api.server_gubun = "1"
        logs = []
        sess = prepare_api(api, on_log=lambda m: logs.append(str(m)))
        return api, sess, logs

    def _assert_no_orig_forbidden(self, api, g, orig_before):
        with self.assertRaises(SendOrderBlockedError):
            api.dynamicCall("SendOrder(QString)", ["주문"])
        with self.assertRaises(SendOrderBlockedError):
            api.dynamicCall("CommTerminate()")
        with self.assertRaises(LoginOnlyBlockedError):
            api.dynamicCall(
                "CommRqData(QString, QString, int, QString)",
                "조회",
                "opt10075",
                0,
                "0101",
            )
        api.send_order_market(self.ACCOUNT, "005930", 1, 1)
        self.assertEqual(int(g.orig_calls), orig_before)
        self.assertEqual(api.orig_send_hits, 0)
        self.assertFalse(any(str(c[0]).startswith("SendOrder") for c in api.calls))
        self.assertFalse(any(str(c[0]).startswith("CommTerminate") for c in api.calls))
        self.assertFalse(any(str(c[0]).startswith("CommRqData") for c in api.calls))

    def test_must_pass_prefixes_unchanged(self):
        self.assertEqual(
            MUST_PASS_PREFIXES,
            (
                "GetCommRealData",
                "SetRealReg",
                "SetRealRemove",
                "CommRqData",
                "GetCommData",
                "GetConnectState",
            ),
        )

    def test_confirm_gubun_1(self):
        api = RecordingApi()
        api.login_gubun = "1"
        self.assertEqual(confirm_login_only_mock_server(api), LOGIN_ONLY_MOCK_GUBUN)

    def test_confirm_strips_whitespace(self):
        api = RecordingApi()
        api.login_gubun = " 1 "
        self.assertEqual(confirm_login_only_mock_server(api), LOGIN_ONLY_MOCK_GUBUN)

    def test_confirm_does_not_use_server_gubun_cache(self):
        api = RecordingApi()
        api.login_gubun = "0"
        api.server_gubun = "1"
        with self.assertRaises(SessionAbortError) as ctx:
            confirm_login_only_mock_server(api)
        self.assertIn("모의투자 서버가 아님", str(ctx.exception))
        self.assertIn("'0'", str(ctx.exception))

    def test_confirm_rejects_0_empty_none_unexpected(self):
        cases = (
            ("0", "모의투자 서버가 아님"),
            ("", "빈 값"),
            ("   ", "빈 값"),
            (None, "None"),
            ("2", "모의투자 서버가 아님"),
            ("real", "모의투자 서버가 아님"),
        )
        for gubun, needle in cases:
            with self.subTest(gubun=gubun):
                api = RecordingApi()
                api.login_gubun = gubun
                api.server_gubun = "1"
                with self.assertRaises(SessionAbortError) as ctx:
                    confirm_login_only_mock_server(api)
                self.assertIn(needle, str(ctx.exception))

    def test_confirm_getlogininfo_exception(self):
        class _Boom(RecordingApi):
            def dynamicCall(self, name, *args):
                n = str(name)
                if n.startswith("GetLoginInfo"):
                    self.calls.append((n,) + tuple(args))
                    raise RuntimeError("ocx GetLoginInfo failed")
                return super().dynamicCall(name, *args)

        api = _Boom()
        api.server_gubun = "1"
        with self.assertRaises(SessionAbortError) as ctx:
            confirm_login_only_mock_server(api)
        self.assertIn("GetLoginInfo 예외", str(ctx.exception))
        self.assertIn("RuntimeError", str(ctx.exception))

    def test_finish_gubun_1_confirms_mock_and_skips_engine(self):
        from PyQt5.QtWidgets import QApplication
        import main as m

        QApplication.instance() or QApplication([])
        api, sess, logs = self._prepare("1")
        created = {"engine": 0}
        watch = _WatchTimer()
        win = self._window(logs)

        class _BoomEngine:
            def __init__(self, *a, **k):
                created["engine"] += 1
                raise AssertionError("LOGIN_ONLY 에서 TradingEngine 생성 금지")

        with mock_patch_attr(m, "TradingEngine", _BoomEngine):
            m._stage2c_finish_login_only(
                win, api, sess, self.ACCOUNT, watch_timer=watch
            )
        self.assertEqual(created["engine"], 0)
        self.assertIsNone(sess.engine)
        self.assertIsNone(getattr(api, "_scalp_bridge", None))
        self.assertTrue(sess.mock_server_confirmed)
        self.assertTrue(getattr(api, ATTR_MOCK_CONFIRMED, False))
        self.assertFalse(sess.aborted)
        self.assertTrue(any("MOCK_SERVER_CONFIRMED" in x for x in logs))
        self.assertTrue(any("GetConnectState=" in x for x in logs))
        self.assertTrue(any("Guard=intact" in x for x in logs))
        self.assertTrue(any("엔진=0" in x for x in logs))
        self.assertTrue(any("Bridge=0" in x for x in logs))
        self.assertFalse(any(self.ACCOUNT in x for x in logs))
        self.assertTrue(watch.started)
        self._assert_no_orig_forbidden(api, sess.guard, int(sess.guard.orig_calls))

    def test_finish_fail_values_abort_without_engine_or_orig(self):
        from PyQt5.QtWidgets import QApplication
        import main as m

        QApplication.instance() or QApplication([])
        cases = ("0", "", None, "2", "real")
        for gubun in cases:
            with self.subTest(gubun=gubun):
                api, sess, logs = self._prepare(gubun)
                created = {"engine": 0}
                watch = _WatchTimer()
                win = self._window(logs)

                class _BoomEngine:
                    def __init__(self, *a, **k):
                        created["engine"] += 1
                        raise AssertionError("실패 후 TradingEngine 생성 금지")

                with mock_patch_attr(m, "TradingEngine", _BoomEngine):
                    with self.assertRaises(SessionAbortError):
                        m._stage2c_finish_login_only(
                            win, api, sess, self.ACCOUNT, watch_timer=watch
                        )
                self.assertEqual(created["engine"], 0)
                self.assertIsNone(sess.engine)
                self.assertIsNone(getattr(api, "_scalp_bridge", None))
                self.assertTrue(sess.aborted)
                self.assertFalse(sess.mock_server_confirmed)
                self.assertFalse(getattr(api, ATTR_MOCK_CONFIRMED, False))
                self.assertFalse(watch.started)
                self.assertTrue(any("서버 구분" in x or "모의투자 서버" in x for x in logs))
                self.assertFalse(any(self.ACCOUNT in x for x in logs))
                self.assertFalse(any("MOCK_SERVER_CONFIRMED" in x for x in logs))
                self.assertTrue(any("확인 실패" in x for x in win._hints))
                self._assert_no_orig_forbidden(
                    api, sess.guard, int(sess.guard.orig_calls)
                )

    def test_finish_getlogininfo_exception_aborts(self):
        from PyQt5.QtWidgets import QApplication
        import main as m

        QApplication.instance() or QApplication([])
        os.environ[ENV_SAFE] = "1"
        os.environ[ENV_LOGIN_ONLY] = "1"

        class _Boom(RecordingApi):
            def __init__(self):
                super().__init__()
                self.boom_logininfo = False

            def dynamicCall(self, name, *args):
                n = str(name)
                if self.boom_logininfo and n.startswith("GetLoginInfo"):
                    self.calls.append((n,) + tuple(args))
                    raise RuntimeError("ocx GetLoginInfo failed")
                return super().dynamicCall(name, *args)

        api = _Boom()
        api.login_gubun = "1"
        api.server_gubun = "1"
        logs = []
        sess = prepare_api(api, on_log=lambda msg: logs.append(str(msg)))
        api.boom_logininfo = True
        created = {"engine": 0}
        watch = _WatchTimer()
        win = self._window(logs)

        class _BoomEngine:
            def __init__(self, *a, **k):
                created["engine"] += 1
                raise AssertionError("예외 후 TradingEngine 생성 금지")

        with mock_patch_attr(m, "TradingEngine", _BoomEngine):
            with self.assertRaises(SessionAbortError) as ctx:
                m._stage2c_finish_login_only(
                    win, api, sess, self.ACCOUNT, watch_timer=watch
                )
        self.assertIn("GetLoginInfo 예외", str(ctx.exception))
        self.assertEqual(created["engine"], 0)
        self.assertIsNone(sess.engine)
        self.assertIsNone(getattr(api, "_scalp_bridge", None))
        self.assertTrue(sess.aborted)
        self.assertFalse(sess.mock_server_confirmed)
        self.assertFalse(any(self.ACCOUNT in x for x in logs))
        self._assert_no_orig_forbidden(api, sess.guard, int(sess.guard.orig_calls))


def _login_loop_ok(api):
    def _loop(*_a, **_k):
        ns = types.SimpleNamespace()

        def _exec():
            api._tr_responses["login_err"] = 0

        ns.exec_ = _exec
        ns.quit = lambda: None
        ns.exit = lambda: None
        return ns

    return _loop


def _logininfo_tags(calls):
    tags = []
    for c in calls:
        if str(c[0]).startswith("GetLoginInfo"):
            tags.append(login_only_info_tag(c[1:]))
    return tags


class LoginOnlyPiiTests(EnvIsolatedTestCase):
    def test_login_success_skips_account_lookup(self):
        from PyQt5.QtWidgets import QApplication
        import main as m

        QApplication.instance() or QApplication([])
        os.environ[ENV_SAFE] = "1"
        os.environ[ENV_LOGIN_ONLY] = "1"
        api = RecordingApi()
        hits = {"account": 0, "list": 0, "pick": 0}

        def _boom_account():
            hits["account"] += 1
            raise AssertionError("LOGIN_ONLY 에서 _get_login_account 금지")

        def _boom_list():
            hits["list"] += 1
            raise AssertionError("LOGIN_ONLY 에서 _list_login_accounts 금지")

        def _boom_pick(*_a, **_k):
            hits["pick"] += 1
            raise AssertionError("LOGIN_ONLY 에서 _pick_account_from_list 금지")

        api._get_login_account = _boom_account
        api._list_login_accounts = _boom_list
        api._pick_account_from_list = _boom_pick
        sess = prepare_api(api)
        orig_before = int(sess.guard.orig_calls)
        with mock_patch_attr(m, "QEventLoop", _login_loop_ok(api)):
            got = m.KiwoomOpenAPI.login(api)
        self.assertEqual(got, LOGIN_ONLY_LOGIN_RESULT)
        self.assertNotEqual(got, m.DEFAULT_KIWOOM_ACCOUNT_NO)
        self.assertFalse(str(got).isdigit())
        self.assertEqual(hits["account"], 0)
        self.assertEqual(hits["list"], 0)
        self.assertEqual(hits["pick"], 0)
        tags = _logininfo_tags(api.calls)
        self.assertTrue(tags)
        self.assertTrue(all(t == "GetServerGubun" for t in tags))
        self.assertNotIn("ACCNO", tags)
        self.assertNotIn("ACCLIST", tags)
        self.assertNotIn("USER_ID", tags)
        self.assertFalse(any(str(c[0]).startswith("CommRqData") for c in api.calls))
        self.assertGreaterEqual(int(sess.guard.orig_calls), orig_before)

    def test_getlogininfo_args_allow_only_server_gubun(self):
        os.environ[ENV_SAFE] = "1"
        os.environ[ENV_LOGIN_ONLY] = "1"
        api = RecordingApi()
        sess = prepare_api(api)
        g = sess.guard
        orig_before = int(g.orig_calls)
        self.assertEqual(api.dynamicCall("GetLoginInfo(QString)", "GetServerGubun"), "1")
        self.assertEqual(
            api.dynamicCall("GetLoginInfo(QString)", ["GetServerGubun"]),
            "1",
        )
        self.assertEqual(login_only_info_tag(("GetServerGubun",)), "GetServerGubun")
        self.assertEqual(login_only_info_tag((["GetServerGubun"],)), "GetServerGubun")
        blocked = ("ACCNO", "ACCLIST", "USER_ID", "ACCOUNT_CNT", "UNKNOWN", "")
        for tag in blocked:
            with self.subTest(tag=tag):
                with self.assertRaises(LoginOnlyBlockedError):
                    api.dynamicCall("GetLoginInfo(QString)", tag)
        with self.assertRaises(LoginOnlyBlockedError):
            api.dynamicCall("GetLoginInfo(QString)")
        self.assertEqual(int(g.orig_calls), orig_before + 2)
        tags = _logininfo_tags(api.calls)
        self.assertNotIn("ACCNO", tags)
        self.assertNotIn("ACCLIST", tags)
        self.assertNotIn("USER_ID", tags)

    def test_pii_absent_from_login_only_logs_and_fail_notice(self):
        from PyQt5.QtWidgets import QApplication
        import inspect
        import main as m
        import PyQt5.QtWidgets as qw

        QApplication.instance() or QApplication([])
        os.environ[ENV_SAFE] = "1"
        os.environ[ENV_LOGIN_ONLY] = "1"
        api = RecordingApi()
        logs = []
        sess = prepare_api(api, on_log=lambda msg: logs.append(str(msg)))
        watch = _WatchTimer()
        win = types.SimpleNamespace(
            kiwoom=None,
            account_no="-",
            engine=None,
            scan_message=types.SimpleNamespace(emit=lambda msg: logs.append(str(msg))),
            lbl_hint=types.SimpleNamespace(setText=lambda s: logs.append(str(s))),
            lbl_auto_start=types.SimpleNamespace(setText=lambda s: logs.append(str(s))),
            btn_overseas_login=types.SimpleNamespace(
                setEnabled=lambda v: None,
                setToolTip=lambda t: None,
            ),
        )
        with mock_patch_attr(m, "QEventLoop", _login_loop_ok(api)):
            got = m.KiwoomOpenAPI.login(api)
        m._stage2c_finish_login_only(win, api, sess, got, watch_timer=watch)
        joined = "\n".join(logs)
        self.assertNotIn(m.DEFAULT_KIWOOM_ACCOUNT_NO, joined)
        self.assertNotIn("USER_ID", joined)
        self.assertNotIn("ACCNO", joined)
        self.assertNotIn("ACCLIST", joined)
        self.assertNotIn("계좌비번", joined)
        self.assertNotIn("8137514011", joined)
        self.assertTrue(any("MOCK_SERVER_CONFIRMED" in x for x in logs))
        boxes = []

        class _Box:
            @staticmethod
            def critical(*a, **k):
                boxes.append(" ".join(str(x) for x in a) + " " + " ".join(str(v) for v in k.values()))

        poison = (
            f"wanted={m.DEFAULT_KIWOOM_ACCOUNT_NO} USER_ID=secret "
            "ACCNO=8137514011 계좌비번 전달 상태: 설정됨"
        )
        with mock_patch_attr(qw, "QMessageBox", _Box):
            m._stage2c_login_only_fail_notice(win, poison)
        box_text = "\n".join(boxes)
        notice_text = joined + "\n" + box_text
        self.assertNotIn(m.DEFAULT_KIWOOM_ACCOUNT_NO, box_text)
        self.assertNotIn("secret", notice_text)
        self.assertNotIn("8137514011", box_text)
        self.assertNotIn("계좌비번", box_text)
        boot = inspect.getsource(m.main)
        self.assertLess(
            boot.find("if not _stage2c_login_only():"),
            boot.find("계좌비번 전달 상태"),
        )
        fail_src = inspect.getsource(m._stage2c_login_only_fail_notice)
        self.assertNotIn("DEFAULT_KIWOOM_ACCOUNT_NO", fail_src)

    def test_safe_mode_login_still_uses_get_login_account(self):
        from PyQt5.QtWidgets import QApplication
        import main as m

        QApplication.instance() or QApplication([])
        os.environ[ENV_SAFE] = "1"
        os.environ.pop(ENV_LOGIN_ONLY, None)
        api = RecordingApi()
        hits = {"account": 0}
        api._get_login_account = lambda: hits.__setitem__("account", hits["account"] + 1) or "SAFE-ACCT-MARKER"
        sess = prepare_api(api)
        acc = api.dynamicCall("GetLoginInfo(QString)", "ACCNO")
        self.assertEqual(acc, "1")
        with mock_patch_attr(m, "QEventLoop", _login_loop_ok(api)):
            got = m.KiwoomOpenAPI.login(api)
        self.assertEqual(got, "SAFE-ACCT-MARKER")
        self.assertEqual(hits["account"], 1)
        self.assertNotEqual(got, LOGIN_ONLY_LOGIN_RESULT)
        tags = _logininfo_tags(api.calls)
        self.assertIn("ACCNO", tags)
        self.assertTrue(sess.guard.is_intact(api))


_PII_FRAGMENTS = (
    "8137514011",
    "USER_ID",
    "ACCNO",
    "ACCLIST",
    "ACCOUNT_CNT",
    "계좌비번",
    "계좌번호",
)


def _audit_names(sess) -> list:
    return [e["event"] for e in load_events(sess.audit.path)]


def _audit_text(sess) -> str:
    with open(sess.audit.path, encoding="utf-8") as fh:
        return fh.read()


class LoginOnlyAuditTests(EnvIsolatedTestCase):
    def _sess(self):
        os.environ[ENV_SAFE] = "1"
        os.environ[ENV_LOGIN_ONLY] = "1"
        api = RecordingApi()
        sess = prepare_api(api)
        return api, sess

    def test_session_start_and_guard_installed(self):
        api, sess = self._sess()
        names = _audit_names(sess)
        self.assertEqual(names[0], "SESSION_START")
        self.assertIn("GUARD_INSTALLED", names)
        self.assertEqual(sess.audit.channel, "fake_test")
        self.assertTrue(os.path.isfile(sess.audit.path))
        self.assertIn("stage2c_login_only_", os.path.basename(sess.audit.path))
        self.assertTrue(sess.audit.path.endswith(".jsonl"))
        inst = [e for e in load_events(sess.audit.path) if e["event"] == "GUARD_INSTALLED"][0]
        self.assertEqual(inst["engine"], 0)
        self.assertEqual(inst["bridge"], 0)
        self.assertTrue(inst["guard_intact"])
        self.assertTrue(sess.audit.ready)
        self.assertTrue(sess.audit.healthy)
        blocked = [i for i, e in enumerate(names) if e == "GUARD_BLOCKED"]
        inst_i = names.index("GUARD_INSTALLED")
        self.assertTrue(all(i > inst_i for i in blocked))

    def test_allowed_and_blocked_counters_separated(self):
        api, sess = self._sess()
        g = sess.guard
        orig_before = int(g.orig_calls)
        blocked_before = int(g.send_order_blocked) + int(g.comm_terminate_blocked) + int(g.login_only_blocked)
        allowed_info = int(g.getlogininfo_allowed)
        api.dynamicCall("CommConnect()")
        api.dynamicCall("GetConnectState()")
        api.dynamicCall("GetLoginInfo(QString)", "GetServerGubun")
        self.assertEqual(int(g.orig_calls), orig_before + 3)
        self.assertEqual(int(g.getlogininfo_allowed), allowed_info + 1)
        with self.assertRaises(SendOrderBlockedError):
            api.dynamicCall("SendOrder(QString)", ["주문"])
        with self.assertRaises(SendOrderBlockedError):
            api.dynamicCall("CommTerminate()")
        with self.assertRaises(LoginOnlyBlockedError):
            api.dynamicCall("CommRqData(QString, QString, int, QString)", "조회", "opt10001", 0, "0404")
        with self.assertRaises(LoginOnlyBlockedError):
            api.dynamicCall("SetRealReg(QString, QString, QString, QString)", "8101", "005930", "10", "0")
        with self.assertRaises(LoginOnlyBlockedError):
            api.dynamicCall("SetRealRemove(QString, QString)", "ALL", "ALL")
        with self.assertRaises(LoginOnlyBlockedError):
            api.dynamicCall("GetLoginInfo(QString)", "ACCNO")
        self.assertEqual(int(g.orig_calls), orig_before + 3)
        self.assertGreaterEqual(int(g.send_order_blocked), 2)
        self.assertGreaterEqual(int(g.comm_terminate_blocked), 1)
        self.assertGreaterEqual(int(g.commrqdata_blocked), 1)
        self.assertGreaterEqual(int(g.setrealreg_blocked), 1)
        self.assertGreaterEqual(int(g.setrealremove_blocked), 1)
        self.assertGreaterEqual(int(g.getlogininfo_blocked), 1)
        blocked_after = int(g.send_order_blocked) + int(g.comm_terminate_blocked) + int(g.login_only_blocked)
        self.assertGreater(blocked_after, blocked_before)
        self.assertNotEqual(int(g.orig_calls), blocked_after)
        counters = sess.audit.counters_from(g)
        self.assertEqual(counters["orig_ocx_calls"], int(g.orig_calls))
        self.assertEqual(counters["guard_blocked_calls"], blocked_after)
        self.assertEqual(counters["sendorder_blocked"], int(g.send_order_blocked))
        self.assertFalse(any(str(c[0]).startswith("SendOrder") for c in api.calls))
        self.assertFalse(any(str(c[0]).startswith("CommTerminate") for c in api.calls))
        names = _audit_names(sess)
        self.assertIn("GUARD_BLOCKED", names)

    def test_orig_sendorder_and_commterminate_are_zero(self):
        api, sess = self._sess()
        with self.assertRaises(SendOrderBlockedError):
            api.dynamicCall("SendOrder(QString)", ["주문"])
        with self.assertRaises(SendOrderBlockedError):
            api.dynamicCall("CommTerminate()")
        self.assertEqual(api.orig_send_hits, 0)
        orig_names = [str(c[0]) for c in api.calls]
        self.assertFalse(any(n.startswith("SendOrder") for n in orig_names))
        self.assertFalse(any(n.startswith("CommTerminate") for n in orig_names))

    def test_watch_timer_heartbeat_and_stop(self):
        api, sess = self._sess()
        t = _WatchTimer()
        bind_watch_timer(sess, t)
        self.assertEqual(t.interval, 250)
        names = _audit_names(sess)
        self.assertIn("WATCH_TIMER_STARTED", names)
        for _ in range(20):
            t.fire()
        self.assertEqual(sess.audit.watch_ticks, 20)
        self.assertEqual(sess.audit.watch_ok, 20)
        self.assertEqual(sess.audit.watch_damage, 0)
        self.assertEqual(sum(1 for n in _audit_names(sess) if n == "WATCH_HEARTBEAT"), 0)
        sess.audit.heartbeat_sec = 0.0
        sess.audit._last_hb_mono = 0.0
        t.fire()
        t.fire()
        self.assertGreaterEqual(sum(1 for n in _audit_names(sess) if n == "WATCH_HEARTBEAT"), 2)
        shutdown_session(sess, terminal="COMPLETE")
        names = _audit_names(sess)
        self.assertIn("SHUTDOWN_STARTED", names)
        self.assertIn("WATCH_TIMER_STOPPED", names)
        self.assertIn("SESSION_FINISHED", names)
        self.assertTrue(t.stopped)
        fin = [e for e in load_events(sess.audit.path) if e["event"] == "SESSION_FINISHED"][0]
        self.assertEqual(fin["engine"], 0)
        self.assertEqual(fin["bridge"], 0)
        self.assertTrue(fin["watch_stopped"])
        self.assertEqual(fin["counters"]["orig_ocx_calls"], int(sess.guard.orig_calls))
        self.assertIn("commterminate_blocked", fin["counters"])
        self.assertGreaterEqual(fin["counters"]["commterminate_blocked"], 0)

    def test_shutdown_duplicate_is_safe(self):
        api, sess = self._sess()
        t = _WatchTimer()
        bind_watch_timer(sess, t)
        shutdown_session(sess, terminal="COMPLETE")
        shutdown_session(sess, terminal="COMPLETE")
        shutdown_session(sess, terminal="INCOMPLETE")
        names = _audit_names(sess)
        self.assertEqual(names.count("SHUTDOWN_STARTED"), 1)
        self.assertEqual(names.count("WATCH_TIMER_STOPPED"), 1)
        self.assertEqual(names.count("SESSION_FINISHED"), 1)
        self.assertNotIn("SESSION_INCOMPLETE", names)

    def test_abort_then_shutdown_incomplete(self):
        api, sess = self._sess()
        abort_session(sess, "테스트 훼손 8137514011")
        names = _audit_names(sess)
        self.assertIn("SESSION_ABORTED", names)
        self.assertNotIn("WATCH_TIMER_STOPPED", names)
        blob = _audit_text(sess)
        self.assertNotIn("8137514011", blob)
        shutdown_session(sess, terminal="COMPLETE")
        names = _audit_names(sess)
        self.assertIn("SESSION_INCOMPLETE", names)
        self.assertNotIn("SESSION_FINISHED", names)
        self.assertNotIn("WATCH_TIMER_STOPPED", names)
        self.assertLess(names.index("SESSION_ABORTED"), names.index("SHUTDOWN_STARTED"))
        self.assertLess(names.index("SHUTDOWN_STARTED"), names.index("SESSION_INCOMPLETE"))

    def test_pii_absent_from_audit_file(self):
        from PyQt5.QtWidgets import QApplication
        import main as m

        QApplication.instance() or QApplication([])
        api, sess = self._sess()
        with mock_patch_attr(m, "QEventLoop", _login_loop_ok(api)):
            got = m.KiwoomOpenAPI.login(api)
        auto = types.SimpleNamespace(text="자동시작: ON", setText=lambda s: auto.__setattr__("text", str(s)))
        win = types.SimpleNamespace(
            kiwoom=None,
            account_no="-",
            engine=None,
            scan_message=types.SimpleNamespace(emit=lambda *_a, **_k: None),
            lbl_hint=types.SimpleNamespace(setText=lambda *_a, **_k: None),
            lbl_auto_start=auto,
            btn_overseas_login=types.SimpleNamespace(setEnabled=lambda *_a, **_k: None, setToolTip=lambda *_a, **_k: None),
        )
        m._stage2c_finish_login_only(win, api, sess, "8137514011", watch_timer=_WatchTimer())
        blob = _audit_text(sess)
        for frag in _PII_FRAGMENTS:
            self.assertNotIn(frag, blob, frag)
        names = _audit_names(sess)
        self.assertIn("LOGIN_SUCCEEDED", names)
        self.assertIn("MOCK_SERVER_CONFIRMED", names)
        self.assertEqual(auto.text, "자동시작: LOGIN_ONLY 차단")
        for rec in load_events(sess.audit.path):
            self.assertNotIn("args", rec)
            dumped = str(rec)
            for frag in _PII_FRAGMENTS:
                self.assertNotIn(frag, dumped)

    def test_audit_write_failure_does_not_bypass_guard(self):
        api, sess = self._sess()
        sess.audit.path = os.path.join(self._audit_tmp.name, "missing", "nope.jsonl")
        before_send = int(sess.guard.send_order_blocked)
        before_term = int(sess.guard.comm_terminate_blocked)
        orig = int(sess.guard.orig_calls)
        with self.assertRaises(SendOrderBlockedError):
            api.dynamicCall("SendOrder(QString)", ["주문"])
        with self.assertRaises(SendOrderBlockedError):
            api.dynamicCall("CommTerminate()")
        self.assertEqual(int(sess.guard.orig_calls), orig)
        self.assertEqual(int(sess.guard.send_order_blocked), before_send + 1)
        self.assertEqual(int(sess.guard.comm_terminate_blocked), before_term + 1)
        self.assertGreater(sess.audit.write_failures, 0)
        self.assertFalse(sess.audit.healthy)
        self.assertTrue(sess.guard.is_intact(api))
        with self.assertRaises(LoginOnlyBlockedError):
            api.dynamicCall("CommConnect()")
        self.assertEqual(int(sess.guard.orig_calls), orig)
        self.assertFalse(any(str(c[0]).startswith("CommConnect") for c in api.calls))

    def test_login_only_gui_label_and_normal_regression(self):
        from PyQt5.QtWidgets import QApplication
        import main as m

        QApplication.instance() or QApplication([])
        auto = types.SimpleNamespace(text="자동시작: ON", setText=lambda s: auto.__setattr__("text", str(s)))
        w = types.SimpleNamespace(
            btn_overseas_login=None,
            lbl_hint=None,
            lbl_auto_start=auto,
        )
        os.environ.pop(ENV_LOGIN_ONLY, None)
        self.assertEqual(auto.text, "자동시작: ON")
        os.environ[ENV_LOGIN_ONLY] = "1"
        os.environ[ENV_SAFE] = "1"
        m._apply_stage2c_login_only_ui(w)
        self.assertEqual(auto.text, "자동시작: LOGIN_ONLY 차단")

    def test_safe_mode_without_login_only_has_no_audit_file(self):
        os.environ[ENV_SAFE] = "1"
        os.environ.pop(ENV_LOGIN_ONLY, None)
        api = RecordingApi()
        sess = prepare_api(api)
        self.assertIsNone(sess.audit)
        self.assertEqual(os.listdir(self._audit_tmp.name), [])
        ret = api.dynamicCall(
            "SetRealReg(QString, QString, QString, QString)",
            "8101",
            "005930",
            "10",
            "0",
        )
        self.assertEqual(ret, 0)

    def test_sanitize_reason_strips_digit_runs(self):
        self.assertEqual(sanitize_reason("계좌 8137514011 중단"), "계좌 # 중단")
        self.assertNotIn("8137514011", sanitize_reason("wanted=8137514011"))
        self.assertNotIn("813-7514-011", sanitize_reason("계좌 813-7514-011"))
        self.assertNotIn("813-75-14011", sanitize_reason("813-75-14011"))
        self.assertNotIn("secretuser", sanitize_reason("USER_ID=secretuser"))
        self.assertNotIn("USER_ID", sanitize_reason("USER_ID=secretuser"))
        self.assertNotIn("8137514011", sanitize_reason("ACCNO=8137514011"))
        self.assertNotIn("password=1234", sanitize_reason("password=1234"))
        self.assertFalse(still_sensitive(sanitize_reason("USER_ID=secretuser ACCNO=8137514011")))
        code, msg = public_reason("wanted=8137514011 USER_ID=secret")
        self.assertNotIn("8137514011", code + msg)
        self.assertNotIn("secret", code + msg)

    def test_fake_channel_distinct_from_runtime(self):
        a = LoginOnlyAudit(os.path.join(self._audit_tmp.name, "x.jsonl"), channel="runtime")
        self.assertEqual(a.channel, "runtime")
        _, sess = self._sess()
        self.assertEqual(sess.audit.channel, "fake_test")
        self.assertNotEqual(sess.audit.channel, "runtime")

    def test_watch_damage_counted(self):
        api, sess = self._sess()
        t = _WatchTimer()
        bind_watch_timer(sess, t)
        api.dynamicCall = sess.guard.orig
        t.fire()
        self.assertGreaterEqual(sess.audit.watch_damage, 1)
        self.assertTrue(sess.aborted)
        names = _audit_names(sess)
        self.assertTrue(any(e.get("status") == "damage" for e in load_events(sess.audit.path) if e["event"] == "WATCH_HEARTBEAT"))
        self.assertIn("SESSION_ABORTED", names)


class LoginOnlyAuditFailClosedTests(EnvIsolatedTestCase):
    """감사 초기화/기록 실패 시 CommConnect·원본 주문이 0이어야 한다."""

    def _login_env(self):
        os.environ[ENV_SAFE] = "1"
        os.environ[ENV_LOGIN_ONLY] = "1"

    def _assert_zero_orig(self, api, sess=None):
        names = [str(c[0]) for c in getattr(api, "calls", [])]
        self.assertFalse(any(n.startswith("CommConnect") for n in names))
        self.assertFalse(any(n.startswith("SendOrder") for n in names))
        self.assertFalse(any(n.startswith("CommTerminate") for n in names))
        if sess is not None:
            self.assertIsNone(sess.engine)
            self.assertIsNone(getattr(api, "_scalp_bridge", None))

    def test_start_session_failure_blocks_connect(self):
        self._login_env()
        api = RecordingApi()

        def boom(*_a, **_k):
            raise AuditInitError("LOGIN_ONLY 감사 로그 초기화 실패 — CommConnect 거부")

        import tools.stage2c_login_only_audit as aud

        with mock_patch_attr(aud.LoginOnlyAudit, "start_session", boom):
            with self.assertRaises((AuditInitError, SessionAbortError)):
                prepare_api(api)
        self.assertFalse(getattr(api, ATTR_ARMED, False))
        with self.assertRaises(SessionAbortError):
            assert_ready_for_connect(api)
        self._assert_zero_orig(api)

    def test_start_session_returns_none_blocks_connect(self):
        self._login_env()
        api = RecordingApi()
        import tools.stage2c_login_only_audit as aud

        with mock_patch_attr(aud.LoginOnlyAudit, "start_session", lambda *_a, **_k: None):
            with self.assertRaises(SessionAbortError):
                prepare_api(api)
        self.assertFalse(getattr(api, ATTR_ARMED, False))
        with self.assertRaises(SessionAbortError):
            assert_ready_for_connect(api)
        self._assert_zero_orig(api)

    def test_audit_none_after_prepare_blocks_connect(self):
        self._login_env()
        api = RecordingApi()
        sess = prepare_api(api)
        orig = int(sess.guard.orig_calls)
        sess.audit = None
        with self.assertRaises(SessionAbortError):
            assert_ready_for_connect(api)
        with self.assertRaises(LoginOnlyBlockedError):
            api.dynamicCall("CommConnect()")
        self.assertEqual(int(sess.guard.orig_calls), orig)
        self.assertFalse(any(str(c[0]).startswith("CommConnect") for c in api.calls))
        self.assertIsNone(sess.engine)
        self.assertIsNone(getattr(api, "_scalp_bridge", None))

    def test_directory_create_failure_blocks_connect(self):
        self._login_env()
        blocked = os.path.join(self._audit_tmp.name, "notdir")
        with open(blocked, "w", encoding="utf-8") as fh:
            fh.write("x")
        os.environ[ENV_AUDIT_DIR] = blocked
        api = RecordingApi()
        with self.assertRaises((AuditInitError, SessionAbortError)):
            prepare_api(api)
        self.assertFalse(getattr(api, ATTR_ARMED, False))
        with self.assertRaises(SessionAbortError):
            assert_ready_for_connect(api)
        self._assert_zero_orig(api)

    def test_session_start_write_failure_blocks_connect(self):
        self._login_env()
        api = RecordingApi()
        import tools.stage2c_login_only_audit as aud

        real_write = aud.LoginOnlyAudit._write_line

        def boom_write(self, line):
            self.write_failures += 1
            self.healthy = False
            return False

        with mock_patch_attr(aud.LoginOnlyAudit, "_write_line", boom_write):
            with self.assertRaises((AuditInitError, SessionAbortError)):
                prepare_api(api)
        self.assertFalse(getattr(api, ATTR_ARMED, False))
        self._assert_zero_orig(api)
        del real_write

    def test_temp_fallback_failure_blocks_connect(self):
        self._login_env()
        os.environ.pop(ENV_AUDIT_DIR, None)
        api = RecordingApi()
        import os as osmod
        import tools.stage2c_login_only_audit as aud

        def boom_mkdir(*_a, **_k):
            raise OSError("mkdir denied")

        with mock_patch_attr(osmod, "makedirs", boom_mkdir):
            with mock_patch_attr(aud, "default_audit_dir", lambda: os.path.join(self._audit_tmp.name, "gone")):
                with self.assertRaises((AuditInitError, SessionAbortError)):
                    prepare_api(api)
        self.assertFalse(getattr(api, ATTR_ARMED, False))
        self._assert_zero_orig(api)

    def test_runtime_temp_fallback_records_path(self):
        self._login_env()
        os.environ.pop(ENV_AUDIT_DIR, None)
        primary = os.path.join(self._audit_tmp.name, "as_file")
        with open(primary, "w", encoding="utf-8") as fh:
            fh.write("x")
        tmp = os.path.join(self._audit_tmp.name, "tempfb")
        os.makedirs(tmp, exist_ok=True)
        import tools.stage2c_login_only_audit as aud

        old_temp = os.environ.get("TEMP")
        os.environ["TEMP"] = tmp
        try:
            with mock_patch_attr(aud, "default_audit_dir", lambda: primary):
                audit = aud.LoginOnlyAudit.start_session()
            self.assertTrue(audit.ready)
            self.assertTrue(audit.fallback)
            self.assertEqual(audit.channel, "runtime")
            self.assertTrue(audit.path.startswith(tmp))
            ev = load_events(audit.path)
            self.assertEqual(ev[0]["event"], "SESSION_START")
            self.assertTrue(ev[0].get("fallback"))
        finally:
            if old_temp is None:
                os.environ.pop("TEMP", None)
            else:
                os.environ["TEMP"] = old_temp

    def test_heartbeat_write_failure_aborts_keeps_guard(self):
        self._login_env()
        api = RecordingApi()
        sess = prepare_api(api)
        t = _WatchTimer()
        bind_watch_timer(sess, t)
        orig = int(sess.guard.orig_calls)
        sess.audit.heartbeat_sec = 0.0
        sess.audit._last_hb_mono = 0.0
        sess.audit.path = os.path.join(self._audit_tmp.name, "missing", "hb.jsonl")
        t.fire()
        self.assertFalse(sess.audit.healthy)
        self.assertTrue(sess.aborted)
        self.assertTrue(sess.guard.is_intact(api))
        with self.assertRaises(SendOrderBlockedError):
            api.dynamicCall("SendOrder(QString)", ["주문"])
        with self.assertRaises(LoginOnlyBlockedError):
            api.dynamicCall("CommConnect()")
        self.assertEqual(int(sess.guard.orig_calls), orig)
        shutdown_session(sess, terminal="COMPLETE")
        names = _audit_names(sess) if os.path.isfile(sess.audit.path) else []
        self.assertNotIn("SESSION_FINISHED", names)

    def test_guard_blocked_write_failure_keeps_block(self):
        self._login_env()
        api = RecordingApi()
        sess = prepare_api(api)
        orig = int(sess.guard.orig_calls)
        sess.audit.path = os.path.join(self._audit_tmp.name, "missing", "blk.jsonl")
        with self.assertRaises(SendOrderBlockedError):
            api.dynamicCall("SendOrder(QString)", ["주문"])
        self.assertFalse(sess.audit.healthy)
        self.assertGreater(sess.audit.write_failures, 0)
        self.assertTrue(sess.guard.is_intact(api))
        with self.assertRaises(LoginOnlyBlockedError):
            api.dynamicCall("GetConnectState()")
        self.assertEqual(int(sess.guard.orig_calls), orig)

    def test_abort_and_shutdown_write_failure_incomplete(self):
        self._login_env()
        api = RecordingApi()
        sess = prepare_api(api)
        t = _WatchTimer()
        bind_watch_timer(sess, t)
        sess.audit.path = os.path.join(self._audit_tmp.name, "missing", "end.jsonl")
        abort_session(sess, "USER_ID=secretuser ACCNO=8137514011")
        shutdown_session(sess, terminal="COMPLETE")
        self.assertTrue(sess.aborted)
        self.assertFalse(sess.audit.healthy)
        self.assertTrue(sess.guard.is_intact(api))
        self.assertNotEqual(getattr(sess.audit, "_finished", False) and sess.audit.healthy, True)

    def test_pii_matrix_jsonl_console_memory(self):
        self._login_env()
        logs = []
        api = RecordingApi()
        sess = prepare_api(api, on_log=lambda m: logs.append(str(m)))
        poisons = (
            "8137514011",
            "813-7514-011",
            "813-75-14011",
            "USER_ID=secretuser",
            "ACCNO=8137514011",
            "ACCLIST=8137514011;8137514022",
            "password=1234",
            "계좌비밀번호 불일치 8137514011",
        )
        with self.assertRaises(LoginOnlyBlockedError):
            api.dynamicCall("GetLoginInfo(QString)", "ACCNO")
        abort_session(
            sess,
            "USER_ID=secretuser ACCNO=8137514011 ACCLIST=8137514011;8137514022 "
            "password=1234 813-7514-011 813-75-14011 계좌비밀번호 불일치",
        )
        shutdown_session(sess, terminal="COMPLETE")
        blob = _audit_text(sess) + "\n" + "\n".join(logs)
        for p in poisons:
            self.assertNotIn(p, blob, p)
        for frag in ("secretuser", "8137514022", "password=1234", "계좌비밀번호"):
            self.assertNotIn(frag, blob, frag)
        dumped = str(sess.guard.snapshot())
        self.assertNotIn("args", dumped)
        self.assertNotIn("8137514011", dumped)
        self.assertNotIn("secretuser", dumped)
        self.assertNotIn("ACCNO", dumped)

    def test_finished_blocks_further_events(self):
        self._login_env()
        api = RecordingApi()
        sess = prepare_api(api)
        t = _WatchTimer()
        bind_watch_timer(sess, t)
        shutdown_session(sess, terminal="COMPLETE")
        n = _audit_names(sess)
        emit_safe(sess, "GUARD_BLOCKED", status="blocked", method="SendOrder")
        emit_safe(sess, "WATCH_HEARTBEAT", status="ok")
        self.assertEqual(_audit_names(sess), n)
        self.assertIn("SESSION_FINISHED", n)
        abort_session(sess, "late")
        self.assertEqual(_audit_names(sess), n)

    def test_incomplete_blocks_further_events(self):
        self._login_env()
        api = RecordingApi()
        sess = prepare_api(api)
        abort_session(sess, "AUDIT_WRITE_FAILED")
        shutdown_session(sess, terminal="COMPLETE")
        n = _audit_names(sess)
        self.assertIn("SESSION_INCOMPLETE", n)
        self.assertNotIn("SESSION_FINISHED", n)
        emit_safe(sess, "GUARD_BLOCKED", status="blocked", method="SendOrder")
        self.assertEqual(_audit_names(sess), n)

    def test_watch_stop_only_if_started(self):
        self._login_env()
        api = RecordingApi()
        sess = prepare_api(api)
        t = _WatchTimer()
        bind_watch_timer(sess, t)
        shutdown_session(sess, terminal="COMPLETE")
        names = _audit_names(sess)
        self.assertIn("WATCH_TIMER_STARTED", names)
        self.assertIn("WATCH_TIMER_STOPPED", names)
        self.assertLess(names.index("SHUTDOWN_STARTED"), names.index("WATCH_TIMER_STOPPED"))
        self.assertLess(names.index("WATCH_TIMER_STOPPED"), names.index("SESSION_FINISHED"))

    def test_closeevent_abouttoquit_once(self):
        self._login_env()
        api = RecordingApi()
        sess = prepare_api(api)
        t = _WatchTimer()
        bind_watch_timer(sess, t)
        shutdown_session(sess, terminal="COMPLETE")
        shutdown_session(sess, terminal="COMPLETE")
        names = _audit_names(sess)
        self.assertEqual(names.count("SHUTDOWN_STARTED"), 1)
        self.assertEqual(names.count("SESSION_FINISHED"), 1)
        self.assertEqual(names.count("WATCH_TIMER_STOPPED"), 1)


def mock_patch_attr(obj, name, value):
    class _P:
        def __enter__(self):
            self._old = getattr(obj, name)
            setattr(obj, name, value)
            return self

        def __exit__(self, *exc):
            setattr(obj, name, self._old)

    return _P()


if __name__ == "__main__":
    unittest.main()
