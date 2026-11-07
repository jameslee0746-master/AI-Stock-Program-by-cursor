"""
0단계 안전장치 테스트.

원칙: 실제 키움 API/주문을 절대 호출하지 않는다. 모든 API 는 가짜(Fake) 객체이며,
SendOrder 는 호출 기록만 남긴다. LIVE_ORDERS_ENABLED 도 테스트별 env dict 로 주입하므로
실제 환경변수에 영향받지 않는다.

실행:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import types
import unittest
import uuid

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import order_safety as osf  # noqa: E402


# ---------------------------------------------------------------------------
# 가짜 키움 API
# ---------------------------------------------------------------------------
class FakeApi:
    """dynamicCall 만 흉내 내는 순수 가짜. SendOrder 는 기록만 하고 서버로 나가지 않는다."""

    def __init__(self, *, server_gubun="1", connected=1, send_ret=0, gate=None):
        self.server_gubun = server_gubun       # 로그인 시 저장값(보조)
        self.login_gubun = server_gubun        # GetLoginInfo 가 돌려줄 값
        self.connected = connected
        self.send_ret = send_ret
        self.send_calls = []
        self.safety_gate = gate
        self.is_mock_server = server_gubun == "1"
        self.last_opw00018_summary = {}
        self.last_holdings_ok_at = 0.0

    def dynamicCall(self, name, *args):
        if name == "GetConnectState()":
            if isinstance(self.connected, Exception):
                raise self.connected
            return self.connected
        if name.startswith("GetLoginInfo"):
            return self.login_gubun
        if name.startswith("SendOrder"):
            self.send_calls.append(args[0] if args else None)
            if isinstance(self.send_ret, Exception):
                raise self.send_ret
            return self.send_ret
        return ""


def new_gate(live="0", clock=None):
    env = {} if live is None else {osf.ENV_LIVE_ORDERS: live}
    return osf.OrderSafetyGate(env=env, clock=clock)


def send_buy(api, gate=None, code="005930", qty=1):
    return osf.guarded_send_order(
        api, rqname="주문", screen_no="0101", account="8137514011",
        order_type=osf.ORDER_TYPE_BUY, code=code, qty=qty, price=0, hoga="03", gate=gate,
    )


def send_sell(api, gate=None, code="005930", qty=1):
    return osf.guarded_send_order(
        api, rqname="주문", screen_no="0101", account="8137514011",
        order_type=osf.ORDER_TYPE_SELL, code=code, qty=qty, price=0, hoga="03", gate=gate,
    )


# ---------------------------------------------------------------------------
# 1. 실계좌 주문 차단
# ---------------------------------------------------------------------------
class LiveOrderBlockTests(unittest.TestCase):
    def test_default_env_is_disabled(self):
        self.assertFalse(osf.live_orders_enabled({}))
        for v in ("0", "", "no", "false", "2", "enabled?"):
            self.assertFalse(osf.live_orders_enabled({osf.ENV_LIVE_ORDERS: v}), v)
        for v in ("1", "true", "YES", "on"):
            self.assertTrue(osf.live_orders_enabled({osf.ENV_LIVE_ORDERS: v}), v)

    def test_live_server_with_flag_off_blocks_buy_sell_and_cancel(self):
        for live in ("0", None):  # 명시적 0, 미설정
            gate = new_gate(live)
            api = FakeApi(server_gubun="2", gate=gate)  # 실서버
            for res in (send_buy(api, gate), send_sell(api, gate)):
                self.assertFalse(res.accepted)
                self.assertEqual(res.status, osf.S_BLOCKED)
                self.assertEqual(res.reason, osf.R_LIVE_DISABLED)
            order = osf.OpenOrder("0001", "005930", osf.KIND_BUY, 3)
            res = osf.guarded_send_order(
                api, rqname="취소주문", screen_no="0101", account="A", order_type=osf.ORDER_TYPE_CANCEL_BUY,
                code="005930", qty=3, price=0, hoga="00", org_order_no=order.order_no, gate=gate,
            )
            self.assertEqual(res.reason, osf.R_LIVE_DISABLED)
            self.assertEqual(api.send_calls, [], "SendOrder 가 호출되면 안 된다")

    def test_live_server_with_flag_on_is_allowed(self):
        gate = new_gate("1")
        api = FakeApi(server_gubun="2", gate=gate)
        res = send_buy(api, gate)
        self.assertTrue(res.accepted)
        self.assertEqual(len(api.send_calls), 1)

    def test_unknown_server_is_rejected_even_if_flag_on(self):
        gate = new_gate("1")
        api = FakeApi(server_gubun="", gate=gate)
        api.login_gubun = ""
        res = send_buy(api, gate)
        self.assertEqual(res.reason, osf.R_SERVER_UNKNOWN)
        self.assertEqual(api.send_calls, [])

    def test_mock_server_is_not_affected_by_flag(self):
        gate = new_gate("0")
        api = FakeApi(server_gubun="1", gate=gate)
        self.assertTrue(send_buy(api, gate).accepted)
        self.assertTrue(send_sell(api, gate).accepted)
        self.assertEqual(len(api.send_calls), 2)

    def test_server_gubun_falls_back_to_login_value(self):
        gate = new_gate("0")
        api = FakeApi(server_gubun="1", gate=gate)
        api.login_gubun = ""  # GetLoginInfo 일시 실패 → 로그인 시 저장값("1" 모의) 사용
        self.assertTrue(send_buy(api, gate).accepted)

    def test_single_sendorder_call_site_in_project(self):
        """프로젝트 전체에서 SendOrder dynamicCall 은 order_safety.py 한 곳뿐이어야 한다."""
        hits = []
        for dirpath, dirnames, filenames in os.walk(ROOT):
            # 과거 소스 백업(backup_old_*)·테스트·캐시는 실행 코드가 아니므로 제외
            dirnames[:] = [
                d for d in dirnames
                if d not in (".git", "tests", "__pycache__", "node_modules", ".venv")
                and not d.startswith("backup")
            ]
            for fn in filenames:
                if not fn.endswith(".py"):
                    continue
                path = os.path.join(dirpath, fn)
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    for i, line in enumerate(f, 1):
                        if re.search(r'"SendOrder\(QString', line):
                            hits.append((os.path.relpath(path, ROOT), i))
        self.assertEqual([h[0] for h in hits], ["order_safety.py"], hits)


# ---------------------------------------------------------------------------
# 2. 연결 단절 / 복구 후 동기화 전 차단
# ---------------------------------------------------------------------------
class ConnectionTests(unittest.TestCase):
    def test_disconnected_blocks_orders(self):
        gate = new_gate("0")
        api = FakeApi(server_gubun="1", connected=0, gate=gate)
        for res in (send_buy(api, gate), send_sell(api, gate)):
            self.assertEqual(res.reason, osf.R_DISCONNECTED)
        self.assertEqual(api.send_calls, [])

    def test_connect_state_exception_is_treated_as_disconnected(self):
        gate = new_gate("0")
        api = FakeApi(server_gubun="1", connected=RuntimeError("ocx dead"), gate=gate)
        self.assertEqual(send_buy(api, gate).reason, osf.R_DISCONNECTED)
        self.assertEqual(api.send_calls, [])

    def test_reconnected_but_not_resynced_is_blocked(self):
        gate = new_gate("0")
        api = FakeApi(server_gubun="1", connected=1, gate=gate)
        self.assertTrue(send_buy(api, gate).accepted)           # 정상
        api.connected = 0
        self.assertEqual(send_buy(api, gate).reason, osf.R_DISCONNECTED)
        api.connected = 1                                         # 연결 복구
        res = send_buy(api, gate)
        self.assertEqual(res.reason, osf.R_RESYNC_PENDING, "복구 직후 동기화 전에는 차단")
        self.assertEqual(send_sell(api, gate).reason, osf.R_RESYNC_PENDING)
        self.assertEqual(len(api.send_calls), 1, "차단 중 추가 SendOrder 없음")

    def test_resync_requires_both_holdings_and_open_orders(self):
        gate = new_gate("0")
        api = FakeApi(server_gubun="1", connected=1, gate=gate)
        send_buy(api, gate)
        api.connected = 0
        send_buy(api, gate)
        api.connected = 1
        send_buy(api, gate)
        tok = gate.begin_resync()
        self.assertFalse(gate.complete_resync(tok, holdings_ok=True, open_orders_ok=False))
        self.assertFalse(gate.complete_resync(tok, holdings_ok=False, open_orders_ok=True))
        self.assertEqual(send_buy(api, gate).reason, osf.R_RESYNC_PENDING)
        self.assertTrue(gate.complete_resync(tok, holdings_ok=True, open_orders_ok=True))
        self.assertTrue(send_buy(api, gate).accepted)

    def test_disconnect_during_resync_invalidates_it(self):
        gate = new_gate("0")
        api = FakeApi(server_gubun="1", connected=1, gate=gate)
        send_buy(api, gate)
        api.connected = 0
        send_buy(api, gate)
        api.connected = 1
        send_buy(api, gate)
        tok = gate.begin_resync()
        api.connected = 0
        send_buy(api, gate)            # 재확인 도중 다시 끊김
        api.connected = 1
        self.assertFalse(gate.complete_resync(tok, holdings_ok=True, open_orders_ok=True))
        self.assertTrue(gate.resync_required)

    def test_cancel_allowed_while_resync_pending_but_not_disconnected(self):
        gate = new_gate("0")
        api = FakeApi(server_gubun="1", connected=1, gate=gate)
        send_buy(api, gate)
        api.connected = 0
        send_buy(api, gate)
        api.connected = 1
        d = gate.authorize(osf.KIND_CANCEL, "005930", server_is_mock=True, connected=True)
        self.assertTrue(d.allowed)
        d = gate.authorize(osf.KIND_CANCEL, "005930", server_is_mock=True, connected=False)
        self.assertFalse(d.allowed)


# ---------------------------------------------------------------------------
# 3. SendOrder 반환값
# ---------------------------------------------------------------------------
class SendResultTests(unittest.TestCase):
    def test_zero_is_request_accepted_not_filled(self):
        gate = new_gate("0")
        api = FakeApi(send_ret=0, gate=gate)
        res = send_buy(api, gate)
        self.assertTrue(res.accepted)
        self.assertEqual(res.status, osf.S_ACCEPTED)
        self.assertEqual(res.ret_code, 0)
        self.assertFalse(res.fill_confirmed, "ret=0 만으로 체결 처리 금지")
        self.assertIn("체결 미확정", res.summary())

    def test_nonzero_is_error(self):
        for ret in (-300, -302, -308, -1, 1):
            gate = new_gate("0")
            api = FakeApi(send_ret=ret, gate=gate)
            res = send_buy(api, gate)
            self.assertFalse(res.accepted, ret)
            self.assertEqual(res.status, osf.S_API_ERROR)
            self.assertEqual(res.ret_code, ret)
            self.assertFalse(res.fill_confirmed)

    def test_none_and_garbage_return_are_errors(self):
        for ret in (None, "", "abc"):
            gate = new_gate("0")
            api = FakeApi(send_ret=ret, gate=gate)
            res = send_buy(api, gate)
            self.assertFalse(res.accepted, repr(ret))
            self.assertEqual(res.status, osf.S_API_ERROR)
            self.assertIsNone(res.ret_code)

    def test_exception_is_error(self):
        gate = new_gate("0")
        api = FakeApi(send_ret=RuntimeError("COM error"), gate=gate)
        res = send_buy(api, gate)
        self.assertEqual(res.status, osf.S_EXCEPTION)
        self.assertFalse(res.accepted)

    def test_error_backoff_then_recovers(self):
        t = [100.0]
        gate = new_gate("0", clock=lambda: t[0])
        api = FakeApi(send_ret=-308, gate=gate)
        self.assertEqual(send_buy(api, gate).status, osf.S_API_ERROR)
        self.assertEqual(send_buy(api, gate).reason, osf.R_ERROR_BACKOFF)
        self.assertEqual(len(api.send_calls), 1)
        self.assertTrue(send_sell(api, gate).accepted is False)  # 매도는 별도 키(아직 오류 없음)
        t[0] += 31.0
        api.send_ret = 0
        self.assertTrue(send_buy(api, gate).accepted)

    def test_invalid_args_never_reach_api(self):
        gate = new_gate("0")
        api = FakeApi(gate=gate)
        self.assertEqual(send_buy(api, gate, qty=0).status, osf.S_INVALID)
        self.assertEqual(send_buy(api, gate, code="").status, osf.S_INVALID)
        r = osf.guarded_send_order(api, rqname="x", screen_no="0101", account="A", order_type=9,
                                   code="005930", qty=1, price=0, hoga="03", gate=gate)
        self.assertEqual(r.status, osf.S_INVALID)
        r = osf.guarded_send_order(api, rqname="x", screen_no="0101", account="A",
                                   order_type=osf.ORDER_TYPE_CANCEL_BUY, code="005930", qty=1, price=0,
                                   hoga="00", org_order_no="", gate=gate)
        self.assertEqual(r.status, osf.S_INVALID)
        self.assertEqual(api.send_calls, [])

    def test_describe_codes(self):
        self.assertIn("접수", osf.describe_send_order_code(0))
        self.assertIn("알 수 없는", osf.describe_send_order_code(-9999))


# ---------------------------------------------------------------------------
# 4. 신규매수 차단 / 전체 차단 / 긴급정지 (게이트 수준)
# ---------------------------------------------------------------------------
class SwitchTests(unittest.TestCase):
    def test_block_buys_only_blocks_buys(self):
        gate = new_gate("0")
        api = FakeApi(gate=gate)
        gate.set_block_buys(True)
        self.assertEqual(send_buy(api, gate).reason, osf.R_BLOCK_BUY)
        self.assertTrue(send_sell(api, gate).accepted)

    def test_block_all_blocks_buy_and_sell_but_not_cancel(self):
        gate = new_gate("0")
        api = FakeApi(gate=gate)
        gate.set_block_all(True)
        self.assertEqual(send_buy(api, gate).reason, osf.R_BLOCK_ALL)
        self.assertEqual(send_sell(api, gate).reason, osf.R_BLOCK_ALL)
        self.assertTrue(gate.authorize(osf.KIND_CANCEL, "005930", server_is_mock=True, connected=True).allowed)
        self.assertIn("손절", osf.NOTICE_BLOCK_ALL)  # 손절 매도도 막힌다고 사실대로 안내

    def test_emergency_blocks_new_orders_buy_side(self):
        gate = new_gate("0")
        api = FakeApi(gate=gate)
        gate.activate_emergency("test")
        self.assertEqual(send_buy(api, gate).reason, osf.R_EMERGENCY)
        self.assertEqual(api.send_calls, [])
        gate.clear_emergency()
        self.assertTrue(send_buy(api, gate).accepted)


class EmergencyStopTests(unittest.TestCase):
    class _Api(FakeApi):
        def __init__(self, orders, **kw):
            super().__init__(**kw)
            self._orders = orders
            self.cancel_requests = []

        def request_open_orders(self, account):
            return self._orders

        def cancel_order(self, account, order):
            self.cancel_requests.append(order.order_no)
            return osf.guarded_send_order(
                self, rqname="취소주문", screen_no="0101", account=account,
                order_type=order.cancel_order_type(), code=order.code, qty=order.open_qty,
                price=0, hoga="00", org_order_no=order.order_no, gate=self.safety_gate,
            )

    def test_emergency_cancels_open_orders_records_state_and_blocks_buys(self):
        gate = new_gate("0")
        orders = [
            osf.OpenOrder("111", "005930", osf.KIND_BUY, 5, 5, 0, "접수", "+매수"),
            osf.OpenOrder("222", "000660", osf.KIND_SELL, 2, 2, 0, "접수", "-매도"),
            osf.OpenOrder("333", "035720", osf.KIND_BUY, 0, 1, 0, "체결", "+매수"),       # 미체결 0 → 불필요
            osf.OpenOrder("444", "035420", osf.KIND_BUY, 1, 1, 0, "접수", "매수취소"),    # 취소주문 자체
        ]
        api = self._Api(orders, server_gubun="1", gate=gate)
        events = []
        with tempfile.TemporaryDirectory() as td:
            rep = osf.run_emergency_stop(
                gate=gate, api=api, account="8137514011",
                positions={"005930": {"qty": 10, "avg_price": 70000}},
                pending_orders={"005930": {"side": "buy", "sent_at": __import__("datetime").datetime.now()}},
                snapshot_dir=td, log=lambda m: None,
                record_event=lambda **kw: events.append(kw),
            )
            self.assertTrue(gate.emergency)
            self.assertEqual(api.cancel_requests, ["111", "222"], "취소 가능한 주문만 취소 요청")
            self.assertEqual([c["accepted"] for c in rep.cancel_results], [True, True])
            self.assertEqual([c["order_no"] for c in rep.cancel_results], ["111", "222"])
            # 취소 SendOrder 의 order_type: 매수취소=3, 매도취소=4
            self.assertEqual([c[3] for c in api.send_calls], [osf.ORDER_TYPE_CANCEL_BUY, osf.ORDER_TYPE_CANCEL_SELL])
            self.assertTrue(os.path.exists(rep.snapshot_path))
            with open(rep.snapshot_path, encoding="utf-8") as f:
                snap = json.load(f)
            self.assertEqual(snap["positions"]["005930"]["qty"], 10)
            self.assertEqual(len(snap["open_orders_from_broker"]), 4)
            self.assertIn("005930", snap["pending_orders_in_app"])
            self.assertEqual(send_buy(api, gate).reason, osf.R_EMERGENCY, "긴급정지 후 신규 매수 차단")
        names = [e["event"] for e in events]
        self.assertIn("EMERGENCY_STOP", names)
        self.assertEqual(names.count("ORDER_CANCEL_REQUESTED"), 2)

    def test_emergency_when_open_order_query_fails(self):
        gate = new_gate("0")
        api = self._Api(None, server_gubun="1", gate=gate)
        with tempfile.TemporaryDirectory() as td:
            rep = osf.run_emergency_stop(gate=gate, api=api, account="A", positions={}, pending_orders={},
                                         snapshot_dir=td, log=lambda m: None)
            self.assertTrue(gate.emergency)
            self.assertFalse(rep.open_orders_checked)
            self.assertEqual(api.cancel_requests, [])
            self.assertIn("직접 확인", rep.text())
            with open(rep.snapshot_path, encoding="utf-8") as f:
                self.assertIsNone(json.load(f)["open_orders_from_broker"])

    def test_cancel_failure_is_reported_not_hidden(self):
        gate = new_gate("0")
        api = self._Api([osf.OpenOrder("111", "005930", osf.KIND_BUY, 5, 5, 0, "접수", "+매수")],
                        server_gubun="1", gate=gate, send_ret=-302)
        with tempfile.TemporaryDirectory() as td:
            rep = osf.run_emergency_stop(gate=gate, api=api, account="A", positions={}, pending_orders={},
                                         snapshot_dir=td, log=lambda m: None)
        self.assertFalse(rep.cancel_results[0]["accepted"])
        self.assertEqual(rep.cancel_results[0]["ret_code"], -302)

    def test_notice_texts_make_no_false_claims(self):
        n = osf.NOTICE_EMERGENCY
        self.assertIn("보장되지 않습니다", n)
        self.assertNotIn("꺼졌", n)
        self.assertNotIn("자동 손절이 보장됩니다", n)
        self.assertIn("손절·익절", osf.NOTICE_STOP_PAUSE)


class OpenOrderParseTests(unittest.TestCase):
    def test_normalize(self):
        o = osf.normalize_open_order({"주문번호": "0012345", "종목코드": "A005930", "미체결수량": "7",
                                      "주문수량": "10", "주문구분": "+매수", "주문가격": "0"})
        self.assertEqual((o.code, o.side, o.open_qty, o.order_qty), ("005930", "BUY", 7, 10))
        self.assertTrue(o.cancellable())
        s = osf.normalize_open_order({"주문번호": "9", "종목코드": "000660", "미체결수량": "-3", "주문구분": "-매도"})
        self.assertEqual((s.side, s.open_qty), ("SELL", 3))
        self.assertEqual(s.cancel_order_type(), osf.ORDER_TYPE_CANCEL_SELL)
        self.assertIsNone(osf.normalize_open_order({"주문번호": "", "종목코드": "005930"}))


# ---------------------------------------------------------------------------
# 5. 중복 실행 방지
# ---------------------------------------------------------------------------
@unittest.skipUnless(sys.platform == "win32", "Windows 뮤텍스 전용")
class InstanceLockTests(unittest.TestCase):
    def setUp(self):
        self.name = f"Local\\KiwoomSafetyTest.{uuid.uuid4().hex}"

    def test_second_acquire_is_blocked_in_same_process(self):
        a = osf.InstanceLock(self.name)
        b = osf.InstanceLock(self.name)
        try:
            self.assertTrue(a.acquire())
            self.assertFalse(b.acquire(), "이미 실행 중이면 두 번째는 차단")
            a.release()
            self.assertTrue(b.acquire(), "해제 후에는 획득 가능")
        finally:
            a.release()
            b.release()

    def test_second_process_is_blocked_and_lock_dies_with_owner(self):
        code = (
            "import sys,time;sys.path.insert(0,%r);import order_safety as o;"
            "l=o.InstanceLock(%r);print(l.acquire(),flush=True);time.sleep(30)" % (ROOT, self.name)
        )
        child = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
        try:
            first = child.stdout.readline().strip()
            self.assertEqual(first, "True", "자식 프로세스가 락을 잡아야 한다")
            mine = osf.InstanceLock(self.name)
            self.assertFalse(mine.acquire(), "다른 프로세스가 실행 중이면 차단")
            child.kill()
            child.wait(timeout=10)
            time.sleep(0.3)
            self.assertTrue(mine.acquire(), "프로세스가 죽으면 락은 자동 해제")
            mine.release()
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)
            child.stdout.close()


# ---------------------------------------------------------------------------
# 6. TradingEngine 통합 + 기존 모의투자 회귀 (가짜 키움, 오프스크린 Qt)
# ---------------------------------------------------------------------------
_main = None
_qapp = None


def _load_main():
    global _main, _qapp
    if _main is None:
        from PyQt5.QtWidgets import QApplication

        _qapp = QApplication.instance() or QApplication([])
        import main as m  # noqa: WPS433

        _main = m
    return _main


class FakeKiwoom(FakeApi):
    """TradingEngine 이 요구하는 최소 인터페이스 + 실제 KiwoomOpenAPI 의 주문 메서드(바인딩)."""

    def __init__(self, m, **kw):
        super().__init__(**kw)
        self.on_real_price = None
        self.on_vi_event = None
        self.on_chejan_data = None
        self.open_orders = []          # request_open_orders 결과 (None 이면 조회 실패)
        self.send_order_market = types.MethodType(m.KiwoomOpenAPI.send_order_market, self)
        self.cancel_order = types.MethodType(m.KiwoomOpenAPI.cancel_order, self)

    def request_open_orders(self, account, timeout_ms=0):
        return self.open_orders


class EngineIntegrationTests(unittest.TestCase):
    CODE = "005930"

    def setUp(self):
        self.m = _load_main()
        self.tmp = tempfile.TemporaryDirectory()
        self.gate = new_gate("0")
        self.kiwoom = FakeKiwoom(self.m, server_gubun="1", gate=self.gate)
        self.eng = self.m.TradingEngine(self.kiwoom, "8137514011", [self.CODE])
        self.eng.order_event_csv_path = os.path.join(self.tmp.name, "order_event_log.csv")
        self.logs = []
        self.eng.on_log = self.logs.append  # 실제 log/ 파일을 건드리지 않는다
        self.eng.current_price[self.CODE] = 10000

    def tearDown(self):
        try:
            self.eng.timer.stop()
        except Exception:
            pass
        self.tmp.cleanup()

    def events(self):
        path = self.eng.order_event_csv_path
        if not os.path.exists(path):
            return []
        import csv

        with open(path, encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f))

    # --- 기존 모의투자 동작 회귀 -------------------------------------------
    def test_mock_buy_regression(self):
        ret = self.eng.buy_market(self.CODE, qty_override=3)
        self.assertIsNone(ret, "정상 경로의 반환값은 기존과 동일(None)")
        self.assertEqual(len(self.kiwoom.send_calls), 1)
        args = self.kiwoom.send_calls[0]
        self.assertEqual((args[0], args[1], args[2], args[3], args[4], args[5], args[6], args[7]),
                         ("주문", "0101", "8137514011", 1, self.CODE, 3, 0, "03"))
        po = self.eng.pending_orders[self.CODE]
        self.assertEqual((po["side"], po["qty"]), ("buy", 3))
        self.assertTrue(po["api_accepted"])
        self.assertEqual(self.eng.positions, {}, "ret=0 만으로 보유가 갱신되면 안 된다(체결 통보 전)")
        ev = [e for e in self.events() if e["event"] == "ORDER_SENT"]
        self.assertEqual(len(ev), 1)
        self.assertIn("체결 미확정", ev[0]["note"])

    def test_mock_sell_regression(self):
        self.eng.positions[self.CODE] = {"qty": 5, "avg_price": 10000}
        ret = self.eng.sell_market(self.CODE, reason="stop_loss")
        self.assertIsNone(ret)
        self.assertEqual(len(self.kiwoom.send_calls), 1)
        self.assertEqual(self.kiwoom.send_calls[0][3], 2)
        self.assertEqual(self.eng.pending_orders[self.CODE]["side"], "sell")
        self.assertEqual(self.eng.positions[self.CODE]["qty"], 5, "체결 통보 전 보유 수량 불변")
        ev = [e for e in self.events() if e["event"] == "ORDER_SENT"][0]
        self.assertIn("reason=stop_loss", ev["note"])

    def test_mock_duplicate_buy_guard_still_works(self):
        self.eng.buy_market(self.CODE, qty_override=1)
        self.eng.buy_market(self.CODE, qty_override=1)
        self.assertEqual(len(self.kiwoom.send_calls), 1, "기존 중복 주문 방지가 그대로 동작")

    def test_mock_chejan_fill_still_updates_position(self):
        """주문 접수(ret=0) 후 체결 통보가 와야 보유가 갱신되는 기존 흐름이 유지된다."""
        self.eng.buy_market(self.CODE, qty_override=3)
        self.assertEqual(self.eng.positions, {})
        self.eng._on_kiwoom_chejan({
            "gubun": "0", "9201": "8137514011", "9203": "0000001", "9001": "A" + self.CODE,
            "913": "체결", "907": "2", "905": "+매수", "910": "10000", "911": "3", "909": "T1",
            "908": "090000", "902": "0",
        })
        self.assertEqual(int(self.eng.positions.get(self.CODE, {}).get("qty", 0)), 3)

    # --- 안전장치 -----------------------------------------------------------
    def test_live_server_flag_off_blocks_engine_orders(self):
        self.kiwoom.server_gubun = "2"
        self.kiwoom.login_gubun = "2"
        self.assertIs(self.eng.buy_market(self.CODE, qty_override=3), False)
        self.eng.positions[self.CODE] = {"qty": 5, "avg_price": 10000}
        self.assertIs(self.eng.sell_market(self.CODE, reason="stop_loss"), False)
        self.assertEqual(self.kiwoom.send_calls, [])
        self.assertEqual(self.eng.pending_orders, {}, "차단된 주문은 추적 상태를 만들지 않는다")
        self.assertFalse(self.eng.pending_buy.get(self.CODE))
        self.assertFalse(self.eng.pending_sell.get(self.CODE))
        names = [e["event"] for e in self.events()]
        self.assertIn("ORDER_BLOCKED", names)
        self.assertNotIn("ORDER_SENT", names)

    def test_disconnect_blocks_engine_orders(self):
        self.kiwoom.connected = 0
        self.assertIs(self.eng.buy_market(self.CODE, qty_override=3), False)
        self.assertEqual(self.kiwoom.send_calls, [])
        self.assertEqual(self.eng.pending_orders, {})

    def test_sendorder_error_leaves_no_pending_and_backs_off(self):
        self.kiwoom.send_ret = -302
        self.assertIs(self.eng.buy_market(self.CODE, qty_override=3), False)
        self.assertEqual(self.eng.pending_orders, {})
        self.assertFalse(self.eng.pending_buy.get(self.CODE))
        self.assertIsNone(self.eng._last_global_buy_sent_at, "미전송 주문은 전역 매수 카운트에 넣지 않는다")
        self.assertEqual(len(self.eng._global_buy_timestamps), 0)
        self.assertIn("ORDER_SEND_ERROR", [e["event"] for e in self.events()])
        self.kiwoom.send_ret = 0
        self.assertIs(self.eng.buy_market(self.CODE, qty_override=3), False, "오류 직후는 백오프로 재시도 억제")
        self.assertEqual(len(self.kiwoom.send_calls), 1)

    def test_emergency_stop_blocks_new_buys_and_cancels(self):
        self.kiwoom.open_orders = [osf.OpenOrder("111", "000660", osf.KIND_BUY, 4, 4, 0, "접수", "+매수")]
        rep = self.eng.safety_emergency_stop()
        self.assertTrue(self.gate.emergency)
        self.assertEqual(len(rep.cancel_results), 1)
        self.assertTrue(rep.cancel_results[0]["accepted"])
        self.assertTrue(os.path.exists(rep.snapshot_path))
        self.assertTrue(rep.snapshot_path.startswith(self.tmp.name))
        self.assertIs(self.eng.buy_market(self.CODE, qty_override=3), False)
        self.assertEqual([c[3] for c in self.kiwoom.send_calls], [3], "취소(3)만 전송, 신규 매수는 없음")
        # 긴급정지는 매도(손절·익절) 로직을 끄지 않는다 — 문구와 동작 모두 사실대로
        self.eng.positions[self.CODE] = {"qty": 2, "avg_price": 10000}
        self.assertIsNone(self.eng.sell_market(self.CODE, reason="stop_loss"))
        self.assertEqual(self.kiwoom.send_calls[-1][3], 2)

    def test_block_all_switch_blocks_sell_too(self):
        self.eng.safety_set_block_all(True)
        self.eng.positions[self.CODE] = {"qty": 2, "avg_price": 10000}
        self.assertIs(self.eng.sell_market(self.CODE, reason="stop_loss"), False)
        self.assertIs(self.eng.buy_market(self.CODE, qty_override=1), False)
        self.assertEqual(self.kiwoom.send_calls, [])
        self.eng.safety_set_block_all(False)
        self.assertIsNone(self.eng.buy_market(self.CODE, qty_override=1))

    def test_block_buys_switch_only_blocks_buys(self):
        self.eng.safety_set_block_buys(True)
        self.assertIs(self.eng.buy_market(self.CODE, qty_override=1), False)
        self.eng.positions[self.CODE] = {"qty": 2, "avg_price": 10000}
        self.assertIsNone(self.eng.sell_market(self.CODE, reason="take_profit"))

    def test_reconnect_blocks_until_resync_then_allows(self):
        # 정상 → 끊김 → 복구
        self.eng._safety_on_tick(True)
        self.eng._safety_on_tick(False)
        self.assertTrue(self.gate.resync_required)
        self.eng._safety_on_tick(True)
        self.assertTrue(self.gate.resync_required, "연결만 돌아와서는 해제되지 않는다")
        self.assertIs(self.eng.buy_market(self.CODE, qty_override=3), False)
        self.assertEqual(self.kiwoom.send_calls, [])

        # 재확인: 잔고 TR 실패 → 계속 차단
        self.eng.sync_portfolio = lambda password="": None  # holdings 성공 시각 갱신 없음
        self.kiwoom.open_orders = []
        self.eng._run_safety_resync_deferred()
        self.assertTrue(self.gate.resync_required)

        # 재확인: 잔고 OK + 미체결 조회 실패 → 계속 차단
        def _ok_sync(password=""):
            self.kiwoom.last_holdings_ok_at = time.monotonic() + 1000.0

        self.eng.sync_portfolio = _ok_sync
        self.kiwoom.open_orders = None
        self.eng._run_safety_resync_deferred()
        self.assertTrue(self.gate.resync_required)

        # 재확인: 둘 다 OK → 허용. 앱이 모르는 미체결은 경고만(자동 취소 없음)
        self.kiwoom.last_holdings_ok_at = 0.0
        self.kiwoom.open_orders = [osf.OpenOrder("777", "000660", osf.KIND_BUY, 2, 2, 0, "접수", "+매수")]
        self.eng._run_safety_resync_deferred()
        self.assertFalse(self.gate.resync_required)
        self.assertTrue(any("추적하지 않는 미체결" in m for m in self.logs))
        self.assertEqual(self.kiwoom.send_calls, [], "재확인 과정에서 주문/취소를 임의로 보내지 않는다")
        self.assertIsNone(self.eng.buy_market(self.CODE, qty_override=3))
        self.assertEqual(len(self.kiwoom.send_calls), 1)

    def test_status_text_contains_no_false_guarantee(self):
        self.eng.safety_set_block_buys(True)
        txt = self.eng.safety_status_text()
        self.assertIn("모의서버", txt)
        self.assertIn("매수차단", txt)
        self.assertNotIn("손절 보장", txt)

    def test_real_kiwoom_methods_exist_and_return_send_result(self):
        self.assertTrue(hasattr(self.m.KiwoomOpenAPI, "cancel_order"))
        self.assertTrue(hasattr(self.m.KiwoomOpenAPI, "request_open_orders"))
        res = self.kiwoom.send_order_market("8137514011", self.CODE, 1, 1)
        self.assertIsInstance(res, osf.SendResult)
        self.assertTrue(res.accepted)
        self.assertFalse(res.fill_confirmed)


class GuiSmokeTests(unittest.TestCase):
    """실제 TradingWindow 를 오프스크린으로 만들어 안전 버튼이 엔진 없이도 동작하는지 확인."""

    def test_safety_buttons_toggle_gate(self):
        m = _load_main()
        gate = osf.reset_gate_for_tests(new_gate("0"))
        try:
            w = m.TradingWindow()
            for name in ("lbl_safety", "btn_block_buys", "btn_block_all", "btn_emergency"):
                self.assertTrue(hasattr(w, name), name)
            w.refresh_ui()
            self.assertIn("안전", w.lbl_safety.text())
            w._toggle_block_buys()
            self.assertTrue(gate.block_buys)
            w._toggle_block_all()
            self.assertTrue(gate.block_all)
            w._toggle_block_all()
            w._toggle_block_buys()
            self.assertFalse(gate.block_all or gate.block_buys)
        finally:
            osf.reset_gate_for_tests()


if __name__ == "__main__":
    unittest.main(verbosity=2)
