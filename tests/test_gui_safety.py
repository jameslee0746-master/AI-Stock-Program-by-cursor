"""
0단계 안전장치 GUI 통합 테스트 (Qt offscreen).

- 실제 TradingWindow 를 생성한다.
- 실제 키움 API 에 연결하지 않고, TradingEngine 도 생성하지 않는다.
  (엔진 없는 상태 테스트 + 엔진 '일부 메서드'를 빌린 스텁 테스트로 구분)
- SendOrder 관련 함수는 모두 '호출되면 실패' 하도록 감시한다.

실행:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import datetime
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import order_safety as osf  # noqa: E402

_main = None
_app = None
_ENV_KEYS = (
    "STAGE2C_SAFE_MODE",
    "STAGE2C_LOGIN_ONLY",
    "STAGE2C_COLLECT_ONLY",
    "STAGE2D_CODES",
    "SCALP_ENABLED",
    "LIVE_ORDERS_ENABLED",
)
_ENV_SNAP = {k: os.environ.get(k) for k in _ENV_KEYS}


def tearDownModule():
    for k, v in _ENV_SNAP.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _load_main():
    global _main, _app
    if _main is None:
        from PyQt5.QtWidgets import QApplication

        _app = QApplication.instance() or QApplication([])
        import main as m  # noqa: WPS433

        _main = m
    return _main


def _gate():
    return osf.OrderSafetyGate(env={osf.ENV_LIVE_ORDERS: "0"})


def _shot(widget, name):
    """결과 확인용 스크린샷(실패해도 테스트에 영향 없음)."""
    try:
        path = os.path.join(tempfile.gettempdir(), f"gui_safety_{name}.png")
        widget.grab().save(path)
        return path
    except Exception:
        return ""


class _GuiBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = _load_main()
        from PyQt5.QtWidgets import QApplication

        cls.app = QApplication.instance()

    def setUp(self):
        from PyQt5.QtWidgets import QMessageBox

        self._env_iso = {k: os.environ.get(k) for k in _ENV_KEYS}
        for k in _ENV_KEYS:
            os.environ.pop(k, None)
        self.QMessageBox = QMessageBox
        # 1) 슬롯 안에서 예외가 나면 PyQt5 는 abort 하므로 excepthook 으로 수집해 단언한다.
        self.exceptions = []
        self._old_hook = sys.excepthook
        sys.excepthook = lambda et, ev, tb: self.exceptions.append((et.__name__, str(ev)))
        # 2) SendOrder 로 이어질 수 있는 모든 함수를 감시: 호출되면 기록하고 예외.
        self.send_calls = []

        def _boom(*a, **k):
            self.send_calls.append((a, k))
            raise AssertionError("GUI 테스트 중 주문 함수가 호출되었다")

        self._patches = [
            mock.patch.object(osf, "guarded_send_order", _boom),
            mock.patch.object(self.m.KiwoomOpenAPI, "send_order_market", _boom),
            mock.patch.object(self.m.KiwoomOpenAPI, "cancel_order", _boom),
            mock.patch.object(self.m.TradingEngine, "buy_market", _boom),
            mock.patch.object(self.m.TradingEngine, "sell_market", _boom),
        ]
        for p in self._patches:
            p.start()
        # 3) 전역 게이트 초기화 (테스트마다 깨끗한 상태)
        self.gate = osf.reset_gate_for_tests(_gate())
        self.win = None

    def tearDown(self):
        sys.excepthook = self._old_hook
        for p in self._patches:
            p.stop()
        for k, v in self._env_iso.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if self.win is not None:
            try:
                for t in self.win.findChildren(type(self.win._ui_timer)) if hasattr(self.win, "_ui_timer") else []:
                    t.stop()
            except Exception:
                pass
            try:
                self.win.close()
                self.win.deleteLater()
            except Exception:
                pass
        osf.reset_gate_for_tests()
        self.assertEqual(self.send_calls, [], "SendOrder/주문 함수가 한 번이라도 호출되면 안 된다")
        self.assertEqual(self.exceptions, [], f"슬롯에서 예외 발생: {self.exceptions}")

    def make_window(self):
        self.win = self.m.TradingWindow()
        self.win.resize(1500, 950)
        self.win.show()
        self.app.processEvents()
        return self.win

    def click(self, button):
        button.click()
        self.app.processEvents()


# ---------------------------------------------------------------------------
# 1~6. 생성 / 기존 GUI / 안전 위젯 표시
# ---------------------------------------------------------------------------
class WindowCreationTests(_GuiBase):
    def test_window_created_without_engine_or_api(self):
        w = self.make_window()
        self.assertIsNotNone(w)
        self.assertTrue(w.isVisible())
        self.assertIsNone(w.engine, "엔진 없이 생성되어야 한다")
        self.assertFalse(hasattr(w, "kiwoom") and w.kiwoom is not None and w.kiwoom.__class__.__name__ == "KiwoomOpenAPI")

    def test_existing_gui_still_shown(self):
        w = self.make_window()
        expected_buttons = {
            "btn_backtest": "백테스트",
            "btn_stop": "정지",
            "btn_strategy_report": "전략리포트",
            "btn_dashboard": "수익 대시보드",
        }
        for name, text in expected_buttons.items():
            b = getattr(w, name)
            self.assertTrue(b.isVisible(), name)
            self.assertTrue(b.isEnabled(), name)
            self.assertEqual(b.text(), text, name)
        self.assertTrue(w.btn_force_scan.isVisible())
        self.assertTrue(w.btn_force_scan.text().startswith("테스트스캔"))
        for lbl in ("lbl_status", "lbl_market", "lbl_positions", "lbl_waiting"):
            self.assertTrue(getattr(w, lbl).isVisible(), lbl)
        self.assertEqual(w.lbl_auto_start.text(), "자동시작: ON")
        self.assertEqual(w.tabs.count(), 2)
        self.assertEqual([w.tabs.tabText(i) for i in range(2)], ["국내주식", "해외주식"])
        self.assertTrue(w.log.isVisible() or w.log.parent() is not None)

    def test_safety_widgets_visible_with_expected_text(self):
        w = self.make_window()
        w.refresh_ui()
        self.app.processEvents()
        self.assertTrue(w.btn_block_buys.isVisible())
        self.assertEqual(w.btn_block_buys.text(), "신규매수 차단")
        self.assertTrue(w.btn_block_all.isVisible())
        self.assertEqual(w.btn_block_all.text(), "주문 전체 차단")
        self.assertTrue(w.btn_emergency.isVisible())
        self.assertEqual(w.btn_emergency.text(), "긴급정지")
        for b in (w.btn_block_buys, w.btn_block_all, w.btn_emergency):
            self.assertTrue(b.isEnabled())
            self.assertTrue(b.toolTip(), "툴팁(사실 안내) 존재")
        self.assertTrue(w.lbl_safety.isVisible())
        txt = w.lbl_safety.text()
        self.assertTrue(txt.startswith("안전:"), txt)
        self.assertIn("엔진 시작 전", txt)
        self.assertIn("실주문 차단", txt)
        self.assertEqual(w.lbl_auto_start.text(), "자동시작: ON")
        _shot(w, "01_initial")

    def test_login_only_auto_start_label(self):
        w = self.make_window()
        self.assertEqual(w.lbl_auto_start.text(), "자동시작: ON")
        self.m._apply_stage2c_login_only_ui(w)
        self.assertEqual(w.lbl_auto_start.text(), "자동시작: LOGIN_ONLY 차단")
        self.assertFalse(w.btn_overseas_login.isEnabled())

    def test_safety_buttons_do_not_overlap_existing_buttons(self):
        from PyQt5.QtCore import QPoint, QRect

        w = self.make_window()
        names = ["btn_backtest", "btn_stop", "btn_force_scan", "btn_strategy_report", "btn_dashboard",
                 "btn_block_buys", "btn_block_all", "btn_emergency"]
        rects = {}
        for n in names:
            b = getattr(w, n)
            rects[n] = QRect(b.mapTo(w, QPoint(0, 0)), b.size())
            self.assertTrue(b.isVisible(), n)
            self.assertGreater(rects[n].width(), 20, n)
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                self.assertFalse(rects[a].intersects(rects[b]), f"{a} 와 {b} 가 겹침: {rects[a]} {rects[b]}")
        # 기존 버튼과 안전 버튼이 서로 다른 객체이고 신호가 각자 연결돼 있다.
        self.assertEqual(len({id(getattr(w, n)) for n in names}), len(names))
        self.assertEqual(w.btn_stop.text(), "정지")
        self.assertEqual(w.btn_emergency.text(), "긴급정지")


# ---------------------------------------------------------------------------
# 버튼별 테스트 (엔진 없음)
# ---------------------------------------------------------------------------
def _auth(gate, kind):
    # 모의서버 + 연결 정상 가정 — 게이트 판정만 보며 SendOrder 는 호출하지 않는다.
    return gate.authorize(kind, "005930", server_is_mock=True, connected=True)


class BlockBuysButtonTests(_GuiBase):
    def test_click_blocks_new_buys_keeps_sell_and_updates_label(self):
        w = self.make_window()
        self.assertTrue(_auth(self.gate, osf.KIND_BUY).allowed)
        self.click(w.btn_block_buys)
        self.assertTrue(self.gate.block_buys)
        self.assertFalse(self.gate.block_all)
        self.assertFalse(self.gate.emergency)
        d = _auth(self.gate, osf.KIND_BUY)
        self.assertEqual((d.allowed, d.reason), (False, osf.R_BLOCK_BUY))
        self.assertTrue(_auth(self.gate, osf.KIND_SELL).allowed, "매도는 기존 정책대로 허용")
        self.assertTrue(_auth(self.gate, osf.KIND_CANCEL).allowed)
        self.assertEqual(w.btn_block_buys.text(), "매수차단 해제")
        self.assertIn("매수차단", w.lbl_safety.text())
        self.assertNotIn("주문전체차단", w.lbl_safety.text())
        self.assertIn("신규 매수 차단 ON", w.log.toPlainText())
        _shot(w, "02_block_buys")

    def test_second_click_releases(self):
        w = self.make_window()
        self.click(w.btn_block_buys)
        self.click(w.btn_block_buys)
        self.assertFalse(self.gate.block_buys)
        self.assertTrue(_auth(self.gate, osf.KIND_BUY).allowed)
        self.assertEqual(w.btn_block_buys.text(), "신규매수 차단")
        self.assertNotIn("매수차단", w.lbl_safety.text())


class BlockAllButtonTests(_GuiBase):
    def test_click_blocks_buy_and_sell_and_updates_label(self):
        w = self.make_window()
        self.click(w.btn_block_all)
        self.assertTrue(self.gate.block_all)
        for kind in (osf.KIND_BUY, osf.KIND_SELL):
            d = _auth(self.gate, kind)
            self.assertEqual((d.allowed, d.reason), (False, osf.R_BLOCK_ALL), kind)
        self.assertTrue(_auth(self.gate, osf.KIND_CANCEL).allowed, "취소는 허용")
        self.assertEqual(w.btn_block_all.text(), "전체차단 해제")
        self.assertIn("주문전체차단", w.lbl_safety.text())
        log = w.log.toPlainText()
        self.assertIn("신규 주문 전체 차단 ON", log)
        self.assertIn("손절·익절 매도 주문도 나가지 않", log, "손절 매도도 막힌다는 사실 안내")
        _shot(w, "03_block_all")

    def test_release_keeps_independent_buy_block(self):
        w = self.make_window()
        self.click(w.btn_block_buys)
        self.click(w.btn_block_all)
        self.click(w.btn_block_all)  # 전체 차단만 해제
        self.assertFalse(self.gate.block_all)
        self.assertTrue(self.gate.block_buys, "매수 차단은 별도 스위치로 유지")
        self.assertEqual(_auth(self.gate, osf.KIND_BUY).reason, osf.R_BLOCK_BUY)
        self.assertTrue(_auth(self.gate, osf.KIND_SELL).allowed)


class EmergencyButtonTests(_GuiBase):
    def _answer(self, button):
        return mock.patch("PyQt5.QtWidgets.QMessageBox.question", return_value=button)

    def test_declined_confirmation_does_nothing(self):
        w = self.make_window()
        with self._answer(self.QMessageBox.No) as q:
            self.click(w.btn_emergency)
        self.assertEqual(q.call_count, 1)
        self.assertFalse(self.gate.emergency)
        self.assertEqual(w.btn_emergency.text(), "긴급정지")

    def test_emergency_without_engine_blocks_buys_only_and_updates_label(self):
        w = self.make_window()
        with self._answer(self.QMessageBox.Yes) as q:
            self.click(w.btn_emergency)
        # 확인 창에는 사실 그대로의 안내문이 들어간다.
        shown = q.call_args[0][2]
        self.assertIn("보장되지 않습니다", shown)
        self.assertNotIn("꺼졌", shown)
        self.assertTrue(self.gate.emergency)
        self.assertEqual(_auth(self.gate, osf.KIND_BUY).reason, osf.R_EMERGENCY)
        self.assertTrue(_auth(self.gate, osf.KIND_SELL).allowed, "긴급정지는 매도 로직을 끄지 않는다")
        self.assertEqual(w.btn_emergency.text(), "긴급정지 해제")
        self.assertIn("긴급정지", w.lbl_safety.text())
        self.assertIn("긴급정지(엔진 시작 전)", w.log.toPlainText())
        self.assertIn("신규 매수 차단만 적용", w.log.toPlainText(), "엔진 없이는 취소·기록을 못 했다고 사실대로 표시")
        _shot(w, "04_emergency")

    def test_repeated_emergency_is_safe_and_idempotent(self):
        w = self.make_window()
        with self._answer(self.QMessageBox.Yes):
            self.click(w.btn_emergency)
        first_at = self.gate.emergency_at
        # 이미 긴급정지 중 → 같은 버튼은 '해제' 확인창. 아니오 = 상태 유지
        with self._answer(self.QMessageBox.No) as q:
            self.click(w.btn_emergency)
            self.click(w.btn_emergency)
        self.assertEqual(q.call_count, 2)
        self.assertTrue(self.gate.emergency)
        self.assertEqual(self.gate.emergency_at, first_at)
        # 게이트 API 를 직접 중복 호출해도 시각/상태가 안정적
        self.gate.activate_emergency("again")
        self.assertEqual(self.gate.emergency_at, first_at)
        self.assertTrue(self.gate.emergency)
        # 예 = 해제
        with self._answer(self.QMessageBox.Yes):
            self.click(w.btn_emergency)
        self.assertFalse(self.gate.emergency)
        self.assertEqual(w.btn_emergency.text(), "긴급정지")

    def test_emergency_keeps_other_switches(self):
        w = self.make_window()
        self.click(w.btn_block_all)
        with self._answer(self.QMessageBox.Yes):
            self.click(w.btn_emergency)
            self.click(w.btn_emergency)  # 해제 확인(예)
        self.assertTrue(self.gate.block_all, "긴급정지 해제는 다른 스위치를 건드리지 않는다")


class EngineAbsentRobustnessTests(_GuiBase):
    def test_every_button_clickable_without_engine(self):
        w = self.make_window()
        with mock.patch("PyQt5.QtWidgets.QMessageBox.question", return_value=self.QMessageBox.Yes):
            for b in (w.btn_stop, w.btn_block_buys, w.btn_block_all, w.btn_emergency,
                      w.btn_block_buys, w.btn_block_all, w.btn_emergency):
                self.click(b)
        w.refresh_ui()
        # 정지 버튼은 엔진이 없으면 아무 일도 하지 않고, 안전 게이트도 건드리지 않는다.
        self.assertEqual(w.btn_stop.text(), "정지")

    def test_stop_button_alone_does_not_touch_safety_gate(self):
        w = self.make_window()
        before = self.gate.status()
        self.click(w.btn_stop)
        self.assertEqual(self.gate.status(), before)


# ---------------------------------------------------------------------------
# 정지 vs 긴급정지 구분 + 긴급정지 오류 처리 (엔진 스텁: TradingEngine 은 생성하지 않음)
# ---------------------------------------------------------------------------
class _StubKiwoom:
    """request_open_orders 결과/예외를 조절하는 가짜. SendOrder 는 없다."""

    def __init__(self, gate, open_orders):
        self.safety_gate = gate
        self.server_gubun = "1"
        self.is_mock_server = True
        self.last_opw00018_summary = {}
        self.open_orders = open_orders

    def dynamicCall(self, name, *args):
        if name == "GetConnectState()":
            return 1
        if name.startswith("GetLoginInfo"):
            return "1"
        return ""

    def request_open_orders(self, account, timeout_ms=0):
        if isinstance(self.open_orders, Exception):
            raise self.open_orders
        return self.open_orders

    def cancel_order(self, account, order):  # 호출되면 안 되는 경로(미체결 없음)
        raise AssertionError("cancel_order 호출됨")


def _make_stub_engine(m, gate, kiwoom, tmpdir):
    """TradingEngine 의 안전 관련 메서드만 빌려 쓰는 스텁 (TradingEngine() 생성 없음)."""
    from PyQt5.QtCore import QTimer

    class StubEngine:
        pass

    e = StubEngine()
    e.kiwoom = kiwoom
    e.account = "8137514011"
    e.positions = {"005930": {"qty": 10, "avg_price": 70000}}
    e.pending_orders = {"005930": {"side": "buy", "sent_at": datetime.datetime.now()}}
    e.pending_buy, e.pending_sell = {}, {}
    e.current_price = {"005930": 70500}
    e.realized_pnl_krw = 1234.0
    e.opw_estimated_deposit_krw = 10_000_000
    e.order_event_csv_path = os.path.join(tmpdir, "order_event_log.csv")
    e.timer = QTimer()
    e.timer.setInterval(2000)
    e.timer.start()
    e.events = []
    e.logs = []
    e._emit_log = e.logs.append
    e._append_order_event = lambda **kw: e.events.append(kw)
    e.market_session_name = lambda: "정규장"
    e._safety_gate = lambda: gate
    for name in ("safety_status_text", "safety_set_block_buys", "safety_set_block_all",
                 "safety_emergency_stop", "safety_clear_emergency", "_safety_verify_cancels"):
        setattr(e, name, types.MethodType(getattr(m.TradingEngine, name), e))
    return e


class StopVsEmergencyTests(_GuiBase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        try:
            if self.win is not None and getattr(self.win, "engine", None) is not None:
                self.win.engine.timer.stop()
        finally:
            super().tearDown()
            self.tmp.cleanup()

    def _attach(self, open_orders=()):
        """open_orders: 목록 / None(조회 실패) / 예외 인스턴스(조회 중 예외). 기본은 빈 목록."""
        w = self.make_window()
        kw = _StubKiwoom(self.gate, [] if open_orders == () else open_orders)
        eng = _make_stub_engine(self.m, self.gate, kw, self.tmp.name)
        w.engine = eng
        w.kiwoom = kw
        return w, eng

    def test_stop_button_and_emergency_button_are_distinct(self):
        w, eng = self._attach(open_orders=[])
        self.assertTrue(eng.timer.isActive())

        # 정지: 타이머(on_tick)만 멈춘다. 안전 게이트는 그대로. 손절·익절도 멈춘다는 안내.
        self.click(w.btn_stop)
        self.assertFalse(eng.timer.isActive())
        st = self.gate.status()
        self.assertFalse(st["emergency"] or st["block_all"] or st["block_buys"])
        self.assertIn("손절·익절 판단", w.log.toPlainText())
        self.assertEqual(w.btn_stop.text(), "재개")
        self.assertEqual(w.btn_emergency.text(), "긴급정지")

        # 긴급정지: 게이트만 바뀐다. 타이머 상태(정지 중)는 그대로, 정지 버튼 문구도 그대로.
        with mock.patch("PyQt5.QtWidgets.QMessageBox.question", return_value=self.QMessageBox.Yes):
            self.click(w.btn_emergency)
        self.assertTrue(self.gate.emergency)
        self.assertFalse(eng.timer.isActive(), "긴급정지가 on_tick 타이머를 켜거나 끄지 않는다")
        self.assertEqual(w.btn_stop.text(), "재개")
        self.assertEqual(w.btn_emergency.text(), "긴급정지 해제")
        self.assertIn("긴급정지", w.lbl_safety.text())

        # 재개: 긴급정지 상태는 유지(신규 매수 차단 지속)
        self.click(w.btn_stop)
        self.assertTrue(eng.timer.isActive())
        self.assertTrue(self.gate.emergency)
        self.assertEqual(_auth(self.gate, osf.KIND_BUY).reason, osf.R_EMERGENCY)
        _shot(w, "05_stub_stop_vs_emergency")

    def test_emergency_with_open_order_query_failure_keeps_safe_state(self):
        for failure in (None, RuntimeError("TR timeout")):
            with self.subTest(failure=repr(failure)):
                self.gate.clear_emergency()
                w, eng = self._attach(open_orders=failure)
                with mock.patch("PyQt5.QtWidgets.QMessageBox.question", return_value=self.QMessageBox.Yes):
                    self.click(w.btn_emergency)
                self.assertTrue(self.gate.emergency, "조회 실패여도 긴급정지 상태 유지")
                self.assertEqual(_auth(self.gate, osf.KIND_BUY).reason, osf.R_EMERGENCY)
                log = w.log.toPlainText()
                self.assertIn("미체결 조회 실패", log)
                self.assertIn("직접 확인", log)
                # 계좌·보유 상태는 그래도 기록된다.
                snap_dir = os.path.join(self.tmp.name, "safety")
                self.assertTrue(any(f.endswith(".json") for f in os.listdir(snap_dir)))
                w.engine.timer.stop()
                self.win.close()
                self.win = None

    def test_emergency_engine_exception_falls_back_to_gate_block(self):
        w, eng = self._attach(open_orders=[])

        def _explode(*a, **k):
            raise RuntimeError("snapshot build failed")

        eng.safety_emergency_stop = _explode
        with mock.patch("PyQt5.QtWidgets.QMessageBox.question", return_value=self.QMessageBox.Yes):
            self.click(w.btn_emergency)  # 예외가 슬롯 밖으로 새면 excepthook 에 잡혀 tearDown 에서 실패
        self.assertTrue(self.gate.emergency, "엔진 쪽이 실패해도 신규 매수는 반드시 차단")
        self.assertIn("긴급정지", w.log.toPlainText())
        self.assertIn("RuntimeError", w.log.toPlainText())


# ---------------------------------------------------------------------------
# 긴급정지 기록 오류 처리 (order_safety 직접)
# ---------------------------------------------------------------------------
class EmergencyRecordingErrorTests(unittest.TestCase):
    class _Api:
        def __init__(self, orders):
            self.orders = orders
            self.safety_gate = None

        def request_open_orders(self, account):
            if isinstance(self.orders, Exception):
                raise self.orders
            return self.orders

        def cancel_order(self, account, order):
            raise AssertionError("호출되면 안 됨")

    def test_snapshot_dir_unwritable_is_reported_not_raised(self):
        gate = _gate()
        with tempfile.TemporaryDirectory() as td:
            blocker = os.path.join(td, "file_not_dir")
            with open(blocker, "w") as f:
                f.write("x")
            rep = osf.run_emergency_stop(
                gate=gate, api=self._Api([]), account="A", positions={"005930": {"qty": 1}},
                pending_orders={}, snapshot_dir=os.path.join(blocker, "sub"), log=lambda m: None,
            )
        self.assertTrue(gate.emergency)
        self.assertEqual(rep.snapshot_path, "")
        self.assertIn("상태 기록 실패", rep.text())

    def test_unserializable_state_is_still_recorded(self):
        gate = _gate()
        with tempfile.TemporaryDirectory() as td:
            rep = osf.run_emergency_stop(
                gate=gate, api=self._Api([]), account="A",
                positions={"005930": {"qty": 1, "obj": object()}},
                pending_orders={"x": {"t": datetime.datetime.now(), "s": {1, 2}}},
                snapshot_dir=td, log=lambda m: None,
            )
            self.assertTrue(os.path.exists(rep.snapshot_path))

    def test_event_recorder_failure_does_not_break_emergency(self):
        gate = _gate()

        def bad_record(**kw):
            raise OSError("order_event_log.csv locked")

        with tempfile.TemporaryDirectory() as td:
            rep = osf.run_emergency_stop(
                gate=gate, api=self._Api([osf.OpenOrder("1", "005930", osf.KIND_BUY, 0, 1, 0, "체결", "+매수")]),
                account="A", positions={}, pending_orders={}, snapshot_dir=td,
                log=lambda m: None, record_event=bad_record,
            )
            self.assertTrue(gate.emergency)
            self.assertTrue(os.path.exists(rep.snapshot_path))

    def test_log_callback_failure_does_not_break_emergency(self):
        gate = _gate()

        def bad_log(msg):
            raise ValueError("log sink closed")

        with tempfile.TemporaryDirectory() as td:
            rep = osf.run_emergency_stop(gate=gate, api=self._Api([]), account="A", positions={},
                                         pending_orders={}, snapshot_dir=td, log=bad_log)
        self.assertTrue(gate.emergency)
        self.assertTrue(rep.snapshot_path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
