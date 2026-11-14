"""HFT 2단계-D COLLECT_ONLY 가짜 객체 테스트. 키움 로그인/OCX/실제 주문 없음."""
from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scalp.collector import HOGA_FIDS_STR, SCREEN_HOGA, SCREEN_TICK, TICK_FIDS  # noqa: E402
from tests.test_scalp_bridge import _regs  # noqa: E402
from tests.test_stage2c_safe_runner import (  # noqa: E402
    EnvIsolatedTestCase,
    PreparedKiwoom,
    RecordingApi,
    _WatchTimer,
    _login_loop_ok,
    mock_patch_attr,
)
from tools.stage2c_login_only_audit import AuditInitError, LoginOnlyAudit  # noqa: E402
from tools.stage2c_safe_runner import (  # noqa: E402
    ATTR_ARMED,
    COLLECT_ONLY_LOGIN_RESULT,
    COLLECT_SCREEN_HOGA,
    COLLECT_SCREEN_TICK,
    ENV_COLLECT_ONLY,
    ENV_LIVE,
    ENV_LOGIN_ONLY,
    ENV_SAFE,
    ENV_SCALP,
    ENV_STAGE2D_CODES,
    MUST_PASS_PREFIXES,
    CollectCodesError,
    CollectOnlyBlockedError,
    LoginOnlyBlockedError,
    SendOrderBlockedError,
    SessionAbortError,
    apply_safe_mode_env,
    assert_collect_only_env,
    assert_ready_for_connect,
    collect_only_enabled,
    is_collect_only_allowed_method,
    load_collect_codes,
    parse_collect_codes,
    prepare_api,
    shutdown_session,
)

_TICK_CODES_5 = "005930;000660;035420;051910;068270"
_TICK_CODES_6 = _TICK_CODES_5 + ";105560"
_REG = "SetRealReg(QString, QString, QString, QString)"
_REM = "SetRealRemove(QString, QString)"
_REAL = "GetCommRealData(QString, int)"
_TICK_FIDS_ALLOW = (10, 13, 15, 20, 228)


class _ProductApi(RecordingApi):
    """제품 핸들러가 Guard 를 우회하지 못하는지 확인하기 위한 Fake."""

    def _on_receive_real_data(self, code, real_type, real_data=""):
        fid = int(real_data) if str(real_data).lstrip("-").isdigit() else 10
        return self.dynamicCall(_REAL, code, fid)


def _orig_names(api):
    return [str(c[0]) for c in api.calls]


class CollectOnlyEnvTests(EnvIsolatedTestCase):
    """QApplication 을 만들지 않는 env 조합 게이트."""

    def test_collect_only_requires_safe_mode(self):
        os.environ[ENV_COLLECT_ONLY] = "1"
        os.environ.pop(ENV_SAFE, None)
        with self.assertRaises(SessionAbortError) as ctx:
            assert_collect_only_env()
        self.assertIn("STAGE2C_SAFE_MODE=1", str(ctx.exception))

        os.environ[ENV_SAFE] = "0"
        with self.assertRaises(SessionAbortError) as ctx2:
            assert_collect_only_env()
        self.assertIn("STAGE2C_SAFE_MODE=1", str(ctx2.exception))

        os.environ[ENV_SAFE] = "1"
        assert_collect_only_env()
        self.assertTrue(collect_only_enabled())

    def test_collect_only_and_login_only_rejected(self):
        os.environ[ENV_COLLECT_ONLY] = "1"
        os.environ[ENV_LOGIN_ONLY] = "1"
        os.environ[ENV_SAFE] = "1"
        with self.assertRaises(SessionAbortError):
            assert_collect_only_env()
        with self.assertRaises(SessionAbortError):
            apply_safe_mode_env()

    def test_collect_only_forces_live_off_scalp_on(self):
        os.environ[ENV_COLLECT_ONLY] = "1"
        os.environ[ENV_SAFE] = "1"
        os.environ.pop(ENV_LOGIN_ONLY, None)
        os.environ[ENV_LIVE] = "1"
        os.environ[ENV_SCALP] = "0"
        got = apply_safe_mode_env()
        self.assertEqual(got[ENV_LIVE], "0")
        self.assertEqual(got[ENV_SCALP], "1")
        self.assertEqual(os.environ[ENV_LIVE], "0")
        self.assertEqual(os.environ[ENV_SCALP], "1")
        self.assertEqual(os.environ[ENV_SAFE], "1")

    def test_login_only_path_when_collect_only_unset(self):
        os.environ[ENV_LOGIN_ONLY] = "1"
        os.environ[ENV_SAFE] = "1"
        os.environ.pop(ENV_COLLECT_ONLY, None)
        os.environ[ENV_SCALP] = "1"
        os.environ[ENV_LIVE] = "1"
        got = apply_safe_mode_env()
        self.assertEqual(got[ENV_SCALP], "0")
        self.assertEqual(got[ENV_LIVE], "0")
        self.assertNotEqual(str(os.environ.get(ENV_COLLECT_ONLY, "") or "").strip(), "1")

    def test_safe_mode_neither_flag_unchanged(self):
        os.environ[ENV_SAFE] = "1"
        os.environ.pop(ENV_LOGIN_ONLY, None)
        os.environ.pop(ENV_COLLECT_ONLY, None)
        os.environ[ENV_LIVE] = "1"
        os.environ[ENV_SCALP] = "0"
        got = apply_safe_mode_env()
        self.assertEqual(got[ENV_SCALP], "1")
        self.assertEqual(got[ENV_LIVE], "0")

    def test_collect_only_zero_is_noop(self):
        os.environ[ENV_COLLECT_ONLY] = "0"
        os.environ.pop(ENV_SAFE, None)
        assert_collect_only_env()
        self.assertFalse(collect_only_enabled())


class _CollectPrepared(EnvIsolatedTestCase):
    def _env(self):
        os.environ[ENV_SAFE] = "1"
        os.environ[ENV_COLLECT_ONLY] = "1"
        os.environ.pop(ENV_LOGIN_ONLY, None)

    def _prepare(self, api=None):
        self._env()
        api = api or RecordingApi()
        sess = prepare_api(api)
        return api, sess

    def _assert_zero_send_term(self, api):
        names = _orig_names(api)
        self.assertFalse(any(n.startswith("SendOrder") for n in names))
        self.assertFalse(any(n.startswith("CommTerminate") for n in names))


class CollectOnlySetRealRegScreenTests(_CollectPrepared):
    def test_allow_8101_and_8102(self):
        api, sess = self._prepare()
        g = sess.guard
        orig = int(g.orig_calls)
        self.assertEqual(
            api.dynamicCall(_REG, COLLECT_SCREEN_TICK, "005930", TICK_FIDS, "0"),
            0,
        )
        self.assertEqual(
            api.dynamicCall(_REG, COLLECT_SCREEN_HOGA, "005930", HOGA_FIDS_STR, "0"),
            0,
        )
        self.assertEqual(int(g.orig_calls), orig + 2)
        self.assertEqual(
            api.dynamicCall(_REG, 8101, "000660", TICK_FIDS, 0),
            0,
        )
        self.assertEqual(int(g.orig_calls), orig + 3)

    def test_block_unapproved_screens(self):
        api, sess = self._prepare()
        g = sess.guard
        orig = int(g.orig_calls)
        blocked = (
            ("9001", "005930", TICK_FIDS, "0"),
            ("9051", "005930", TICK_FIDS, "0"),
            ("ALL", "005930", TICK_FIDS, "0"),
            ("0101", "005930", TICK_FIDS, "0"),
            ("5202", "005930", TICK_FIDS, "0"),
        )
        for args in blocked:
            with self.subTest(screen=args[0]):
                with self.assertRaises(CollectOnlyBlockedError):
                    api.dynamicCall(_REG, *args)
        self.assertEqual(int(g.orig_calls), orig)
        self._assert_zero_send_term(api)


class CollectOnlyCodeCountTests(_CollectPrepared):
    def test_allow_one_to_five_codes(self):
        api, sess = self._prepare()
        g = sess.guard
        orig = int(g.orig_calls)
        one = "005930"
        two = "005930;000660"
        self.assertEqual(api.dynamicCall(_REG, "8101", one, TICK_FIDS, "0"), 0)
        self.assertEqual(api.dynamicCall(_REG, "8101", two, TICK_FIDS, "0"), 0)
        self.assertEqual(api.dynamicCall(_REG, "8101", _TICK_CODES_5, TICK_FIDS, "0"), 0)
        self.assertEqual(int(g.orig_calls), orig + 3)
        self.assertEqual(g.collect_codes, set(_TICK_CODES_5.split(";")))

    def test_block_six_empty_all_short_letters_extra(self):
        api, sess = self._prepare()
        g = sess.guard
        orig = int(g.orig_calls)
        cases = (
            (_TICK_CODES_6, TICK_FIDS, "0"),
            ("", TICK_FIDS, "0"),
            ("ALL", TICK_FIDS, "0"),
            ("00593", TICK_FIDS, "0"),
            ("00593A", TICK_FIDS, "0"),
            ("ABCDEF", TICK_FIDS, "0"),
        )
        for codes, fids, opt in cases:
            with self.subTest(codes=codes):
                with self.assertRaises(CollectOnlyBlockedError):
                    api.dynamicCall(_REG, "8101", codes, fids, opt)
        with self.assertRaises(CollectOnlyBlockedError):
            api.dynamicCall(_REG, "8101", "005930", TICK_FIDS, "0", "extra")
        with self.assertRaises(CollectOnlyBlockedError):
            api.dynamicCall(_REG, "8101", "005930", TICK_FIDS)
        self.assertEqual(int(g.orig_calls), orig)


class CollectOnlyFidTests(_CollectPrepared):
    def test_allow_tick_and_hoga_fid_strings(self):
        api, sess = self._prepare()
        g = sess.guard
        orig = int(g.orig_calls)
        self.assertEqual(api.dynamicCall(_REG, "8101", "005930", TICK_FIDS, "0"), 0)
        self.assertEqual(api.dynamicCall(_REG, "8102", "005930", HOGA_FIDS_STR, "0"), 0)
        self.assertEqual(int(g.orig_calls), orig + 2)

    def test_block_unknown_fid_and_opt_one(self):
        api, sess = self._prepare()
        g = sess.guard
        orig = int(g.orig_calls)
        with self.assertRaises(CollectOnlyBlockedError):
            api.dynamicCall(_REG, "8101", "005930", "41", "0")
        with self.assertRaises(CollectOnlyBlockedError):
            api.dynamicCall(_REG, "8102", "005930", "10", "0")
        with self.assertRaises(CollectOnlyBlockedError):
            api.dynamicCall(_REG, "8101", "005930", "999", "0")
        with self.assertRaises(CollectOnlyBlockedError):
            api.dynamicCall(_REG, "8102", "005930", "999", "0")
        with self.assertRaises(CollectOnlyBlockedError):
            api.dynamicCall(_REG, "8101", "005930", TICK_FIDS, "1")
        self.assertEqual(int(g.orig_calls), orig)
        self.assertFalse(is_collect_only_allowed_method(_REG, ("8101", "005930", "41", "0")))
        self.assertFalse(is_collect_only_allowed_method(_REG, ("8101", "005930", TICK_FIDS, "1")))


class CollectOnlySetRealRemoveTests(_CollectPrepared):
    def test_allow_screen_all_and_block_others(self):
        api, sess = self._prepare()
        g = sess.guard
        api.dynamicCall(_REG, "8101", "005930", TICK_FIDS, "0")
        api.dynamicCall(_REG, "8102", "000660", HOGA_FIDS_STR, "0")
        orig = int(g.orig_calls)
        self.assertEqual(api.dynamicCall(_REM, "8101", "ALL"), 0)
        self.assertEqual(api.dynamicCall(_REM, "8102", "ALL"), 0)
        self.assertEqual(int(g.orig_calls), orig + 2)
        self.assertEqual(g.collect_codes, set())

        orig2 = int(g.orig_calls)
        blocked = (
            ("ALL", "ALL"),
            ("9001", "ALL"),
            ("0101", "ALL"),
            ("8101", "005930"),
            ("8102", "000660"),
        )
        for args in blocked:
            with self.subTest(args=args):
                with self.assertRaises(CollectOnlyBlockedError):
                    api.dynamicCall(_REM, *args)
        self.assertEqual(int(g.orig_calls), orig2)

    def test_remove_drops_codes_only_on_that_screen(self):
        api, sess = self._prepare()
        g = sess.guard
        api.dynamicCall(_REG, "8101", "005930", TICK_FIDS, "0")
        api.dynamicCall(_REG, "8102", "000660", HOGA_FIDS_STR, "0")
        self.assertEqual(api.dynamicCall(_REAL, "005930", 10), "70000")
        api.dynamicCall(_REM, "8101", "ALL")
        orig = int(g.orig_calls)
        with self.assertRaises(CollectOnlyBlockedError):
            api.dynamicCall(_REAL, "005930", 10)
        self.assertEqual(int(g.orig_calls), orig)
        self.assertNotIn("005930", g.collect_codes)
        self.assertIn("000660", g.collect_codes)


class CollectOnlyGetCommRealDataTests(_CollectPrepared):
    def test_allow_tick_fids_after_8101_reg(self):
        api, sess = self._prepare()
        g = sess.guard
        api.dynamicCall(_REG, "8101", "005930", TICK_FIDS, "0")
        orig = int(g.orig_calls)
        for fid in _TICK_FIDS_ALLOW:
            with self.subTest(fid=fid):
                api.dynamicCall(_REAL, "005930", fid)
        self.assertEqual(int(g.orig_calls), orig + len(_TICK_FIDS_ALLOW))

    def test_block_unapproved_fid_and_unregistered_code(self):
        api, sess = self._prepare()
        g = sess.guard
        api.dynamicCall(_REG, "8101", "005930", TICK_FIDS, "0")
        orig = int(g.orig_calls)
        with self.assertRaises(CollectOnlyBlockedError):
            api.dynamicCall(_REAL, "005930", 11)
        with self.assertRaises(CollectOnlyBlockedError):
            api.dynamicCall(_REAL, "005930", 41)
        with self.assertRaises(CollectOnlyBlockedError):
            api.dynamicCall(_REAL, "000660", 10)
        with self.assertRaises(CollectOnlyBlockedError):
            api.dynamicCall(_REAL, "", 10)
        self.assertEqual(int(g.orig_calls), orig)
        self.assertGreaterEqual(int(g.getcommrealdata_blocked), 4)

    def test_product_handler_cannot_bypass_guard(self):
        api, sess = self._prepare(_ProductApi())
        g = sess.guard
        api.dynamicCall(_REG, "8101", "005930", TICK_FIDS, "0")
        orig = int(g.orig_calls)
        self.assertEqual(api._on_receive_real_data("005930", "주식체결", "10"), "70000")
        self.assertEqual(int(g.orig_calls), orig + 1)
        with self.assertRaises(CollectOnlyBlockedError):
            api._on_receive_real_data("005930", "주식체결", "11")
        with self.assertRaises(CollectOnlyBlockedError):
            api._on_receive_real_data("005930", "주식체결", "41")
        with self.assertRaises(CollectOnlyBlockedError):
            api._on_receive_real_data("000660", "주식체결", "10")
        self.assertEqual(int(g.orig_calls), orig + 1)


class CollectOnlyAlwaysBlockTests(_CollectPrepared):
    def test_always_block_and_zero_orig_send_terminate(self):
        api, sess = self._prepare()
        g = sess.guard
        orig0 = int(g.orig_calls)
        self.assertEqual(api.dynamicCall(_REG, "8101", "005930", TICK_FIDS, "0"), 0)
        self.assertEqual(api.dynamicCall(_REAL, "005930", 10), "70000")
        self.assertEqual(api.dynamicCall("GetConnectState()"), 1)
        self.assertEqual(api.dynamicCall("GetLoginInfo(QString)", "GetServerGubun"), "1")
        orig_allowed = int(g.orig_calls)
        self.assertEqual(orig_allowed, orig0 + 4)

        with self.assertRaises(SendOrderBlockedError):
            api.dynamicCall("SendOrder(QString)", ["주문"])
        with self.assertRaises(SendOrderBlockedError):
            api.dynamicCall("SendOrderFO(QString)", ["FO"])
        with self.assertRaises(SendOrderBlockedError):
            api.dynamicCall("CommTerminate()")
        with self.assertRaises(CollectOnlyBlockedError):
            api.dynamicCall("CommRqData(QString, QString, int, QString)", "조회", "opt10001", 0, "0404")
        with self.assertRaises(CollectOnlyBlockedError):
            api.dynamicCall("SetInputValue(QString, QString)", "sID", "005930")
        with self.assertRaises(CollectOnlyBlockedError):
            api.dynamicCall("GetLoginInfo(QString)", "ACCNO")
        with self.assertRaises(CollectOnlyBlockedError):
            api.dynamicCall("GetLoginInfo(QString)", "ACCLIST")
        with self.assertRaises(CollectOnlyBlockedError):
            api.dynamicCall("GetLoginInfo(QString)", "USER_ID")
        with self.assertRaises(CollectOnlyBlockedError):
            api.dynamicCall("GetCommData(QString)", "opt10001")
        with self.assertRaises(CollectOnlyBlockedError):
            api.dynamicCall("UnknownOcxMethod()")
        self.assertEqual(int(g.orig_calls), orig_allowed)
        self._assert_zero_send_term(api)
        snap = str(g.snapshot())
        self.assertNotIn("args", snap)
        self.assertNotIn("ACCNO", snap)
        self.assertNotIn("USER_ID", snap)
        self.assertNotIn("8137514011", snap)


class CollectOnlyAuditFailClosedTests(_CollectPrepared):
    def _assert_zero_orig_connect(self, api, sess=None):
        names = _orig_names(api)
        self.assertFalse(any(n.startswith("CommConnect") for n in names))
        self.assertFalse(any(n.startswith("SendOrder") for n in names))
        self.assertFalse(any(n.startswith("CommTerminate") for n in names))
        if sess is not None:
            self.assertIsNone(sess.engine)
            self.assertIsNone(getattr(api, "_scalp_bridge", None))

    def test_start_session_failure_blocks_connect(self):
        self._env()
        api = RecordingApi()

        def boom(*_a, **_k):
            raise AuditInitError("COLLECT_ONLY 감사 로그 초기화 실패 — CommConnect 거부")

        import tools.stage2c_login_only_audit as aud

        with mock_patch_attr(aud.LoginOnlyAudit, "start_session", boom):
            with self.assertRaises((AuditInitError, SessionAbortError)):
                prepare_api(api)
        self.assertFalse(getattr(api, ATTR_ARMED, False))
        with self.assertRaises(SessionAbortError):
            assert_ready_for_connect(api)
        self._assert_zero_orig_connect(api)

    def test_start_session_returns_none_blocks_connect(self):
        self._env()
        api = RecordingApi()
        import tools.stage2c_login_only_audit as aud

        with mock_patch_attr(aud.LoginOnlyAudit, "start_session", lambda *_a, **_k: None):
            with self.assertRaises(SessionAbortError) as ctx:
                prepare_api(api)
        self.assertIn("COLLECT_ONLY", str(ctx.exception))
        self.assertFalse(getattr(api, ATTR_ARMED, False))
        with self.assertRaises(SessionAbortError):
            assert_ready_for_connect(api)
        self._assert_zero_orig_connect(api)

    def test_session_start_write_failure_blocks_connect(self):
        self._env()
        api = RecordingApi()
        import tools.stage2c_login_only_audit as aud

        def boom_write(self, line):
            self.write_failures += 1
            self.healthy = False
            return False

        with mock_patch_attr(aud.LoginOnlyAudit, "_write_line", boom_write):
            with self.assertRaises((AuditInitError, SessionAbortError)):
                prepare_api(api)
        self.assertFalse(getattr(api, ATTR_ARMED, False))
        self._assert_zero_orig_connect(api)

    def test_audit_none_after_prepare_blocks_connect_and_real(self):
        api, sess = self._prepare()
        orig = int(sess.guard.orig_calls)
        sess.audit = None
        with self.assertRaises(SessionAbortError) as ctx:
            assert_ready_for_connect(api)
        self.assertIn("COLLECT_ONLY", str(ctx.exception))
        with self.assertRaises(CollectOnlyBlockedError):
            api.dynamicCall("CommConnect()")
        with self.assertRaises(CollectOnlyBlockedError):
            api.dynamicCall(_REG, "8101", "005930", TICK_FIDS, "0")
        with self.assertRaises(CollectOnlyBlockedError):
            api.dynamicCall(_REAL, "005930", 10)
        with self.assertRaises(SendOrderBlockedError):
            api.dynamicCall("SendOrder(QString)", ["주문"])
        self.assertEqual(int(sess.guard.orig_calls), orig)
        self._assert_zero_orig_connect(api, sess)

    def test_unhealthy_audit_blocks_connect_and_real(self):
        api, sess = self._prepare()
        orig = int(sess.guard.orig_calls)
        sess.audit.healthy = False
        with self.assertRaises(SessionAbortError):
            assert_ready_for_connect(api)
        with self.assertRaises(CollectOnlyBlockedError):
            api.dynamicCall("CommConnect()")
        with self.assertRaises(CollectOnlyBlockedError):
            api.dynamicCall(_REG, "8101", "005930", TICK_FIDS, "0")
        with self.assertRaises(CollectOnlyBlockedError):
            api.dynamicCall(_REAL, "005930", 10)
        with self.assertRaises(SendOrderBlockedError):
            api.dynamicCall("SendOrder(QString)", ["주문"])
        self.assertEqual(int(sess.guard.orig_calls), orig)
        self._assert_zero_send_term(api)

    def test_order_guard_still_blocks_sendorder(self):
        api, sess = self._prepare()
        orig = int(sess.guard.orig_calls)
        with self.assertRaises(SendOrderBlockedError):
            api.dynamicCall("SendOrder(QString)", ["주문"])
        with self.assertRaises(SendOrderBlockedError):
            api.dynamicCall("CommTerminate()")
        self.assertEqual(int(sess.guard.orig_calls), orig)
        self.assertGreaterEqual(int(sess.guard.send_order_blocked), 1)
        self.assertGreaterEqual(int(sess.guard.comm_terminate_blocked), 1)
        self._assert_zero_send_term(api)
        _ = _WatchTimer
        _ = LoginOnlyAudit


class CollectOnlyRegressionTests(EnvIsolatedTestCase):
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

    def test_safe_mode_without_collect_only_passes_9001(self):
        os.environ[ENV_SAFE] = "1"
        os.environ.pop(ENV_COLLECT_ONLY, None)
        os.environ.pop(ENV_LOGIN_ONLY, None)
        api = RecordingApi()
        sess = prepare_api(api)
        orig = int(sess.guard.orig_calls)
        ret = api.dynamicCall(_REG, "9001", "005930", "10", "0")
        self.assertEqual(ret, 0)
        self.assertEqual(int(sess.guard.orig_calls), orig + 1)
        self.assertTrue(any(str(c[0]).startswith("SetRealReg") for c in api.calls))

    def test_login_only_still_blocks_setrealreg_8101(self):
        os.environ[ENV_SAFE] = "1"
        os.environ[ENV_LOGIN_ONLY] = "1"
        os.environ.pop(ENV_COLLECT_ONLY, None)
        api = RecordingApi()
        sess = prepare_api(api)
        orig = int(sess.guard.orig_calls)
        with self.assertRaises(LoginOnlyBlockedError):
            api.dynamicCall(_REG, "8101", "005930", TICK_FIDS, "0")
        self.assertEqual(int(sess.guard.orig_calls), orig)
        self.assertFalse(any(str(c[0]).startswith("SetRealReg") for c in api.calls))


class CollectFakeApi(PreparedKiwoom):
    def __init__(self) -> None:
        super().__init__()
        self.connect_state = 1
        self.login_gubun = "1"
        self._tr_responses = {}
        self._last_error = None
        self.real = {
            "005930": {10: "70000", 13: "10", 15: "1", 20: "090015", 228: "55", 21: "090015"},
        }

    def dynamicCall(self, name, *args):
        n = str(name)
        if n.startswith("GetConnectState"):
            self.calls.append((n,) + tuple(args))
            return self.connect_state
        if n.startswith("GetLoginInfo"):
            self.calls.append((n,) + tuple(args))
            return self.login_gubun
        if n.startswith("GetCommRealData"):
            self.calls.append((n,) + tuple(args))
            code = str(args[0]) if args else ""
            try:
                fid = int(args[1]) if len(args) > 1 else 0
            except (TypeError, ValueError):
                fid = 0
            if code in self.real:
                return str(self.real[code].get(fid, self.fids.get(fid, "")))
            return str(self.fids.get(fid, ""))
        return super().dynamicCall(name, *args)


def _collect_window(logs):
    auto = types.SimpleNamespace(text="자동시작: ON", setText=lambda s: auto.__setattr__("text", str(s)))
    hint = types.SimpleNamespace(text="", setText=lambda s: hint.__setattr__("text", str(s)))
    return types.SimpleNamespace(
        kiwoom=None,
        account_no="-",
        engine="SENTINEL",
        scan_message=types.SimpleNamespace(emit=lambda m: logs.append(str(m))),
        lbl_hint=hint,
        lbl_auto_start=auto,
        btn_overseas_login=types.SimpleNamespace(
            setEnabled=lambda v: None,
            setToolTip=lambda t: None,
        ),
    )


class CollectOnlyCodeLoaderTests(EnvIsolatedTestCase):
    def test_env_codes_order_dedupe_cap(self):
        os.environ[ENV_STAGE2D_CODES] = "005930,000660,005930,035420"
        self.assertEqual(
            load_collect_codes(),
            ["005930", "000660", "035420"],
        )

    def test_five_ok_six_aborts(self):
        os.environ[ENV_STAGE2D_CODES] = "005930,000660,035420,051910,068270"
        self.assertEqual(len(load_collect_codes()), 5)
        os.environ[ENV_STAGE2D_CODES] = "005930,000660,035420,051910,068270,105560"
        with self.assertRaises(CollectCodesError) as ctx:
            load_collect_codes()
        self.assertIn("최대 5", str(ctx.exception))

    def test_invalid_and_empty_abort(self):
        os.environ[ENV_STAGE2D_CODES] = "00593"
        with self.assertRaises(CollectCodesError):
            load_collect_codes()
        os.environ[ENV_STAGE2D_CODES] = "005930.KS"
        with self.assertRaises(CollectCodesError):
            load_collect_codes()
        os.environ[ENV_STAGE2D_CODES] = "ALL"
        with self.assertRaises(CollectCodesError):
            load_collect_codes()
        os.environ[ENV_STAGE2D_CODES] = "   "
        with tempfile.TemporaryDirectory() as td:
            missing = os.path.join(td, "codes.txt")
            with self.assertRaises(CollectCodesError) as ctx:
                load_collect_codes(env={}, root=td, codes_path=missing)
            self.assertIn("없음", str(ctx.exception))
        with self.assertRaises(CollectCodesError):
            parse_collect_codes("", source="empty")

    def test_file_used_when_env_unset(self):
        os.environ.pop(ENV_STAGE2D_CODES, None)
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "codes.txt")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("# comment\n005930\n000660\n")
            self.assertEqual(load_collect_codes(env={}, codes_path=path), ["005930", "000660"])


class CollectOnlyMainWireTests(EnvIsolatedTestCase):
    def _env(self):
        os.environ[ENV_SAFE] = "1"
        os.environ[ENV_COLLECT_ONLY] = "1"
        os.environ.pop(ENV_LOGIN_ONLY, None)
        os.environ[ENV_STAGE2D_CODES] = "005930"

    def test_abort_helper_before_qapp(self):
        from PyQt5.QtWidgets import QApplication
        import main as m

        QApplication.instance() or QApplication([])
        os.environ[ENV_COLLECT_ONLY] = "1"
        os.environ.pop(ENV_SAFE, None)
        os.environ.pop(ENV_LOGIN_ONLY, None)
        msg = m._abort_if_collect_only_env()
        self.assertIsNotNone(msg)
        self.assertIn("STAGE2C_SAFE_MODE=1", str(msg))
        os.environ[ENV_SAFE] = "1"
        os.environ[ENV_LOGIN_ONLY] = "1"
        msg2 = m._abort_if_collect_only_env()
        self.assertIsNotNone(msg2)
        self.assertIn("동시에", str(msg2))
        os.environ.pop(ENV_LOGIN_ONLY, None)
        self.assertIsNone(m._abort_if_collect_only_env())
        os.environ[ENV_COLLECT_ONLY] = "0"
        os.environ.pop(ENV_SAFE, None)
        self.assertIsNone(m._abort_if_collect_only_env())

    def test_login_skips_get_login_account(self):
        from PyQt5.QtWidgets import QApplication
        import main as m

        QApplication.instance() or QApplication([])
        self._env()
        api = RecordingApi()
        hits = {"account": 0}

        def _boom_account():
            hits["account"] += 1
            raise AssertionError("COLLECT_ONLY 에서 _get_login_account 금지")

        api._get_login_account = _boom_account
        sess = prepare_api(api)
        with mock_patch_attr(m, "QEventLoop", _login_loop_ok(api)):
            got = m.KiwoomOpenAPI.login(api)
        self.assertEqual(got, COLLECT_ONLY_LOGIN_RESULT)
        self.assertEqual(hits["account"], 0)
        tags = []
        for c in api.calls:
            if str(c[0]).startswith("GetLoginInfo"):
                from tools.stage2c_safe_runner import login_only_info_tag

                tags.append(login_only_info_tag(c[1:]))
        self.assertTrue(tags)
        self.assertTrue(all(t == "GetServerGubun" for t in tags))
        self.assertNotIn("ACCNO", tags)
        self.assertNotIn("ACCLIST", tags)
        self.assertNotIn("USER_ID", tags)
        self.assertFalse(any(str(c[0]).startswith("CommRqData") for c in api.calls))
        self.assertFalse(any(str(c[0]).startswith("SendOrder") for c in api.calls))
        self.assertTrue(sess.guard.is_intact(api))

    def test_mock_server_only(self):
        from PyQt5.QtWidgets import QApplication
        import main as m

        QApplication.instance() or QApplication([])
        self._env()
        api = CollectFakeApi()
        api.login_gubun = "0"
        sess = prepare_api(api)
        logs = []
        win = _collect_window(logs)
        created = {"engine": 0}
        watch = _WatchTimer()

        class _BoomEngine:
            def __init__(self, *a, **k):
                created["engine"] += 1
                raise AssertionError("COLLECT_ONLY 에서 TradingEngine 생성 금지")

        with tempfile.TemporaryDirectory() as td:
            with mock_patch_attr(m, "TradingEngine", _BoomEngine):
                with self.assertRaises(SessionAbortError) as ctx:
                    m._stage2c_finish_collect_only(
                        win, api, sess, COLLECT_ONLY_LOGIN_RESULT,
                        watch_timer=watch, codes=["005930"], directory=td,
                    )
        self.assertIn("모의투자 서버가 아님", str(ctx.exception))
        self.assertEqual(created["engine"], 0)
        self.assertIsNone(sess.engine)
        self.assertIsNone(getattr(api, "_scalp_bridge", None))
        self.assertTrue(sess.aborted)

    def test_finish_attaches_bridge_no_engine(self):
        from PyQt5.QtWidgets import QApplication
        import inspect
        import main as m

        QApplication.instance() or QApplication([])
        self._env()
        api = CollectFakeApi()
        product = api._on_receive_real_data
        sess = prepare_api(api)
        logs = []
        win = _collect_window(logs)
        created = {"engine": 0}
        watch = _WatchTimer()

        class _BoomEngine:
            def __init__(self, *a, **k):
                created["engine"] += 1
                raise AssertionError("COLLECT_ONLY 에서 TradingEngine 생성 금지")

        with tempfile.TemporaryDirectory() as td:
            with mock_patch_attr(m, "TradingEngine", _BoomEngine):
                m._stage2c_finish_collect_only(
                    win, api, sess, COLLECT_ONLY_LOGIN_RESULT,
                    watch_timer=watch, codes=["005930"], directory=td,
                )
            self.assertEqual(created["engine"], 0)
            self.assertIsNone(win.engine)
            self.assertIs(win.kiwoom, api)
            br = getattr(api, "_scalp_bridge", None)
            self.assertIsNotNone(br)
            self.assertTrue(br.attached)
            self.assertTrue(br.writer._started)
            self.assertIn(product, api.OnReceiveRealData.slots)
            self.assertEqual(len(api.OnReceiveRealData.slots), 2)
            self.assertTrue(_regs(api.calls, SCREEN_TICK))
            self.assertTrue(_regs(api.calls, SCREEN_HOGA))
            self.assertFalse(_regs(api.calls, "9001"))
            self.assertFalse(_regs(api.calls, "9051"))
            orig = int(sess.guard.orig_calls)
            with self.assertRaises(CollectOnlyBlockedError):
                api.dynamicCall(_REG, "9001", "005930", TICK_FIDS, "0")
            with self.assertRaises(CollectOnlyBlockedError):
                api.dynamicCall(_REG, "9051", "005930", TICK_FIDS, "0")
            with self.assertRaises(CollectOnlyBlockedError):
                api.dynamicCall(_REM, "ALL", "ALL")
            with self.assertRaises(SendOrderBlockedError):
                api.dynamicCall("SendOrder(QString)", ["주문"])
            with self.assertRaises(SendOrderBlockedError):
                api.dynamicCall("CommTerminate()")
            with self.assertRaises(CollectOnlyBlockedError):
                api.dynamicCall("CommRqData(QString, QString, int, QString)", "x", "opt10075", 0, "0101")
            self.assertEqual(int(sess.guard.orig_calls), orig)
            api.OnReceiveRealData.emit("005930", "주식체결", "")
            api.OnReceiveRealData.emit("005930", "주식호가잔량", "")
            self.assertGreaterEqual(br.stats.tick_callbacks, 1)
            self.assertGreaterEqual(br.stats.hoga_callbacks, 1)
            self.assertEqual(api.product_got[-2], ("005930", "주식체결"))
            with self.assertRaises(CollectOnlyBlockedError):
                api.dynamicCall(_REAL, "000660", 10)
            self.assertEqual(watch.interval, 250)
            self.assertTrue(watch.started)
            shutdown_session(sess, terminal="COMPLETE")
            self.assertTrue(watch.stopped)
            ticks = [n for n in os.listdir(td) if n.startswith("tick_") and n.endswith(".csv")]
            hogas = [n for n in os.listdir(td) if n.startswith("orderbook_") and n.endswith(".csv")]
            self.assertTrue(ticks)
            self.assertTrue(hogas)
            self.assertTrue(br.closed)
            self.assertIn(br.stats.shutdown_status, ("COMPLETE", "STATS_DUMP_FAILED"))
            names = [str(c[0]) for c in api.calls]
            self.assertFalse(any(n.startswith("SendOrder") for n in names))
            self.assertFalse(any(n.startswith("CommTerminate") for n in names))
            self.assertFalse(any(n.startswith("CommRqData") for n in names))
            removes = [
                c for c in api.calls
                if str(c[0]).startswith("SetRealRemove")
            ]
            self.assertTrue(any(len(c) > 2 and str(c[1]) == "8101" and str(c[2]) == "ALL" for c in removes))
            self.assertTrue(any(len(c) > 2 and str(c[1]) == "8102" and str(c[2]) == "ALL" for c in removes))
            self.assertFalse(any(len(c) > 2 and str(c[1]) == "ALL" and str(c[2]) == "ALL" for c in removes))
            self.assertEqual(win.lbl_auto_start.text, "수집 전용: 자동매매 차단")
            self.assertIn("엔진 미생성", win.lbl_hint.text)
        src = inspect.getsource(m._stage2c_finish_collect_only)
        self.assertNotIn("TradingEngine", src)
        self.assertNotIn("scan_market", src)
        self.assertNotIn("engine.start", src)
        self.assertNotIn("_maybe_start_scan", src)
        self.assertNotIn('SetRealRemove(QString, QString)", "ALL", "ALL"', src)
        boot = inspect.getsource(m.main)
        self.assertLess(boot.find("_stage2c_finish_collect_only"), boot.find("engine = TradingEngine"))
        self.assertIn("_abort_if_collect_only_env", boot)
        self.assertIn("load_collect_codes", boot)

    def test_audit_unhealthy_blocks_register(self):
        from PyQt5.QtWidgets import QApplication
        import main as m

        QApplication.instance() or QApplication([])
        self._env()
        api = CollectFakeApi()
        sess = prepare_api(api)
        orig = int(sess.guard.orig_calls)
        sess.audit.healthy = False
        logs = []
        win = _collect_window(logs)
        created = {"engine": 0}

        class _BoomEngine:
            def __init__(self, *a, **k):
                created["engine"] += 1
                raise AssertionError("감사 실패 후 TradingEngine 금지")

        with tempfile.TemporaryDirectory() as td:
            with mock_patch_attr(m, "TradingEngine", _BoomEngine):
                with self.assertRaises((CollectOnlyBlockedError, SessionAbortError)):
                    m._stage2c_finish_collect_only(
                        win, api, sess, COLLECT_ONLY_LOGIN_RESULT,
                        watch_timer=_WatchTimer(), codes=["005930"], directory=td,
                    )
        self.assertEqual(created["engine"], 0)
        self.assertIsNone(getattr(api, "_scalp_bridge", None))
        self.assertEqual(int(sess.guard.orig_calls), orig)
        with self.assertRaises(CollectOnlyBlockedError):
            api.dynamicCall(_REG, "8101", "005930", TICK_FIDS, "0")
        with self.assertRaises(SendOrderBlockedError):
            api.dynamicCall("SendOrder(QString)", ["주문"])
        self.assertFalse(any(str(c[0]).startswith("SendOrder") for c in api.calls))
        self.assertFalse(any(str(c[0]).startswith("SetRealReg") for c in api.calls if "8101" in str(c)))


if __name__ == "__main__":
    unittest.main()
