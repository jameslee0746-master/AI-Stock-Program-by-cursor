"""
0단계 안전장치 — 키움 주문 중앙 게이트 (Qt/키움 OCX 비의존, 순수 파이썬).

목적
  1. 모든 SendOrder 가 단 하나의 게이트(``guarded_send_order``)를 통과하도록 한다.
  2. 실계좌 주문은 ``LIVE_ORDERS_ENABLED=1`` 이고 서버 구분이 확인될 때만 허용한다(기본 0 = 차단).
  3. API 연결 단절 시 주문을 막고, 복구 후에도 잔고·미체결 재확인 전에는 막는다.
  4. SendOrder 반환값을 검사한다. 0 = "접수 요청 성공" 일 뿐 체결이 아니다.
  5. 신규매수 차단(A) / 신규주문 전체 차단(B) / 긴급정지(C) 스위치.
  6. 중복 실행 방지(Windows 명명 뮤텍스).

이 모듈은 매매 전략·진입/청산 조건을 일절 건드리지 않는다.
"""

from __future__ import annotations

import datetime
import json
import os
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# 상수
# ---------------------------------------------------------------------------
ENV_LIVE_ORDERS = "LIVE_ORDERS_ENABLED"

KIND_BUY = "BUY"
KIND_SELL = "SELL"
KIND_CANCEL = "CANCEL"

# 키움 SendOrder 주문유형
ORDER_TYPE_BUY = 1
ORDER_TYPE_SELL = 2
ORDER_TYPE_CANCEL_BUY = 3
ORDER_TYPE_CANCEL_SELL = 4

_KIND_BY_ORDER_TYPE = {
    ORDER_TYPE_BUY: KIND_BUY,
    ORDER_TYPE_SELL: KIND_SELL,
    ORDER_TYPE_CANCEL_BUY: KIND_CANCEL,
    ORDER_TYPE_CANCEL_SELL: KIND_CANCEL,
}

# 게이트 거부 사유 코드
R_LIVE_DISABLED = "LIVE_DISABLED"
R_SERVER_UNKNOWN = "SERVER_UNKNOWN"
R_DISCONNECTED = "DISCONNECTED"
R_RESYNC_PENDING = "RESYNC_PENDING"
R_BLOCK_ALL = "BLOCK_ALL"
R_BLOCK_BUY = "BLOCK_BUY"
R_EMERGENCY = "EMERGENCY"
R_ERROR_BACKOFF = "ERROR_BACKOFF"
R_INVALID = "INVALID_ARGS"

# SendResult.status
S_ACCEPTED = "ACCEPTED_REQUEST"  # 반환값 0: 접수 "요청" 성공 (체결 아님)
S_BLOCKED = "BLOCKED"            # 게이트가 막음 — SendOrder 호출 안 함
S_API_ERROR = "API_ERROR"        # 반환값 != 0
S_EXCEPTION = "EXCEPTION"        # 호출 중 예외
S_INVALID = "INVALID"            # 인자 오류 — SendOrder 호출 안 함

# 전송 오류 후 같은 (종류, 종목) 재시도 억제 시간(초). 손절 매도는 짧게.
ERROR_BACKOFF_SEC = {KIND_BUY: 30.0, KIND_SELL: 5.0, KIND_CANCEL: 5.0}

# 키움 OpenAPI+ 에러코드 (문서 기준. 일부는 환경에 따라 다를 수 있으므로 사람이 최종 확인)
SEND_ORDER_CODES: Dict[int, str] = {
    0: "정상처리(접수 요청 성공 — 체결 아님)",
    -10: "실패",
    -100: "사용자정보교환 실패",
    -101: "서버 접속 실패",
    -102: "버전처리 실패",
    -103: "개인방화벽 실패",
    -104: "메모리보호 실패",
    -105: "함수 입력값 오류",
    -106: "통신 연결 종료",
    -200: "시세조회 과부하",
    -201: "전문작성 초기화 실패",
    -202: "전문작성 입력값 오류",
    -203: "데이터 없음",
    -300: "주문 입력값 오류",
    -301: "계좌비밀번호 미입력",
    -302: "타인 계좌 사용 오류",
    -303: "주문가격이 20억원 초과",
    -304: "주문가격이 50억원 초과",
    -305: "주문수량이 총발행주수의 1% 초과",
    -306: "주문수량이 총발행주수의 3% 초과",
    -307: "주문 전송 실패",
    -308: "주문 전송 과부하",
    -309: "주문수량 300계약 초과",
    -310: "주문수량 500계약 초과",
    -340: "계좌정보 없음",
    -500: "종목코드 없음",
}


def describe_send_order_code(ret: Optional[int]) -> str:
    if ret is None:
        return "반환값 없음(None)"
    return SEND_ORDER_CODES.get(int(ret), f"알 수 없는 오류코드({int(ret)})")


def live_orders_enabled(env: Optional[Dict[str, str]] = None) -> bool:
    """LIVE_ORDERS_ENABLED 가 명시적으로 켜진 값일 때만 True. 미설정/그 외 = False."""
    e = os.environ if env is None else env
    v = str(e.get(ENV_LIVE_ORDERS, "0") or "0").strip().lower()
    return v in ("1", "true", "yes", "y", "on")


# ---------------------------------------------------------------------------
# 결과 객체
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    reason: str = ""
    detail: str = ""


@dataclass
class SendResult:
    """SendOrder(또는 게이트 거부) 결과.

    accepted=True 는 '증권사 API 가 주문 요청을 받았다' 는 뜻일 뿐이다.
    체결 확정은 OnReceiveChejanData 로만 판단한다 (fill_confirmed 는 항상 False).
    """

    accepted: bool
    status: str
    kind: str = ""
    code: str = ""
    qty: int = 0
    ret_code: Optional[int] = None
    reason: str = ""
    detail: str = ""
    fill_confirmed: bool = False

    @property
    def blocked(self) -> bool:
        return self.status in (S_BLOCKED, S_INVALID)

    def summary(self) -> str:
        if self.status == S_ACCEPTED:
            return f"{self.kind} {self.code} x{self.qty}: 접수요청 성공(ret=0, 체결 미확정)"
        if self.status == S_API_ERROR:
            return f"{self.kind} {self.code} x{self.qty}: SendOrder 오류 ret={self.ret_code} ({self.detail})"
        return f"{self.kind} {self.code} x{self.qty}: {self.status}/{self.reason} {self.detail}".strip()


def result_accepted(res: Any) -> bool:
    """호출부 호환용. None(구형 목/가짜 객체)은 '결과 정보 없음 = 기존 동작 유지'로 취급한다.
    실제 KiwoomOpenAPI 는 항상 SendResult 를 돌려주므로 실운영 경로에서는 None 이 나오지 않는다."""
    if res is None:
        return True
    return bool(getattr(res, "accepted", False))


# ---------------------------------------------------------------------------
# 게이트
# ---------------------------------------------------------------------------
class OrderSafetyGate:
    def __init__(
        self,
        env: Optional[Dict[str, str]] = None,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self._env = env
        self._clock = clock or time.monotonic
        self._lock = threading.RLock()

        # 사용자 스위치
        self.block_buys = False   # A: 신규 매수 차단
        self.block_all = False    # B: 신규 주문(매수·매도) 전체 차단 — 손절 매도도 막힘
        self.emergency = False    # C: 긴급정지 (매수 차단 + 미체결 취소 요청 + 계좌 기록)
        self.emergency_at: Optional[datetime.datetime] = None
        self.emergency_reason = ""

        # 연결/동기화
        self._connected: Optional[bool] = None
        self._disconnect_count = 0
        self.resync_required = False
        self.resync_reason = ""

        # 오류 백오프 / 로그 스로틀 / 통계
        self._last_error_at: Dict[Tuple[str, str], float] = {}
        self._reject_log_at: Dict[tuple, float] = {}
        self.stats: Counter = Counter()
        self.last_reject: Optional[GateDecision] = None

    # --- 환경 ---------------------------------------------------------
    def live_enabled(self) -> bool:
        return live_orders_enabled(self._env)

    # --- 연결 상태 ------------------------------------------------------
    def observe_connection(self, connected: bool) -> None:
        """연결 상태를 관찰한다. 끊김을 감지하면 즉시 '재동기화 필요' 로 전환한다.
        (복구 이벤트를 놓쳐도 안전하도록 끊김 시점에 플래그를 세운다.)"""
        with self._lock:
            connected = bool(connected)
            if connected is False and self._connected is not False:
                self._disconnect_count += 1
                self.resync_required = True
                self.resync_reason = "API 연결 단절 — 복구 후 잔고·미체결 재확인 필요"
            self._connected = connected

    @property
    def connected(self) -> Optional[bool]:
        return self._connected

    def begin_resync(self) -> int:
        """재확인 시작. 토큰을 돌려주고, 그 사이 또 끊기면 완료 처리가 무효가 된다."""
        with self._lock:
            return self._disconnect_count

    def complete_resync(self, token: int, *, holdings_ok: bool, open_orders_ok: bool) -> bool:
        """잔고(holdings_ok)와 미체결(open_orders_ok) 재확인이 모두 성공했을 때만 해제."""
        with self._lock:
            if token != self._disconnect_count:
                return False  # 확인 도중 다시 끊김
            if self._connected is not True:
                return False
            if not (holdings_ok and open_orders_ok):
                return False
            self.resync_required = False
            self.resync_reason = ""
            return True

    # --- 사용자 스위치 ---------------------------------------------------
    def set_block_buys(self, on: bool) -> None:
        with self._lock:
            self.block_buys = bool(on)

    def set_block_all(self, on: bool) -> None:
        with self._lock:
            self.block_all = bool(on)

    def activate_emergency(self, reason: str = "") -> None:
        with self._lock:
            if not self.emergency:
                self.emergency_at = datetime.datetime.now()
            self.emergency = True
            self.emergency_reason = str(reason or "")

    def clear_emergency(self) -> None:
        with self._lock:
            self.emergency = False
            self.emergency_at = None
            self.emergency_reason = ""

    # --- 판정 --------------------------------------------------------
    def authorize(
        self,
        kind: str,
        code: str,
        *,
        server_is_mock: Optional[bool],
        connected: bool,
    ) -> GateDecision:
        """주문 1건에 대한 허용 여부. 조건이 하나라도 불충분하면 거부(fail-closed)."""
        with self._lock:
            self.observe_connection(connected)
            d = self._decide(kind, code, server_is_mock, bool(connected))
            if not d.allowed:
                self.stats[d.reason] += 1
                self.last_reject = d
            else:
                self.stats["ALLOWED"] += 1
            return d

    def _decide(
        self, kind: str, code: str, server_is_mock: Optional[bool], connected: bool
    ) -> GateDecision:
        if kind not in (KIND_BUY, KIND_SELL, KIND_CANCEL):
            return GateDecision(False, R_INVALID, f"지원하지 않는 주문 종류: {kind}")

        # 1) 환경변수 + 서버 구분 (둘 다 검사)
        if server_is_mock is None:
            return GateDecision(
                False, R_SERVER_UNKNOWN, "서버 구분(GetServerGubun)을 확인할 수 없어 주문 거부"
            )
        if server_is_mock is False and not self.live_enabled():
            return GateDecision(
                False,
                R_LIVE_DISABLED,
                f"실계좌(실서버) 주문 차단: {ENV_LIVE_ORDERS}=1 이 아님",
            )

        # 2) 연결 단절
        if not connected:
            return GateDecision(False, R_DISCONNECTED, "API 연결 끊김 — 주문 차단")

        # 3) 재동기화 전 (취소는 위험을 줄이는 동작이므로 허용)
        if self.resync_required and kind != KIND_CANCEL:
            return GateDecision(
                False, R_RESYNC_PENDING, self.resync_reason or "잔고·미체결 재확인 전 주문 차단"
            )

        # 4) 사용자 스위치 (취소는 항상 허용)
        if kind != KIND_CANCEL:
            if self.block_all:
                return GateDecision(
                    False,
                    R_BLOCK_ALL,
                    "신규 주문 전체 차단 중 (손절·익절 매도 주문도 전송되지 않음)",
                )
            if kind == KIND_BUY:
                if self.emergency:
                    return GateDecision(False, R_EMERGENCY, "긴급정지 — 신규 매수 차단")
                if self.block_buys:
                    return GateDecision(False, R_BLOCK_BUY, "신규 매수 차단 중")

        # 5) 직전 전송 오류 후 재시도 억제
        key = (kind, str(code))
        last_err = self._last_error_at.get(key)
        if last_err is not None:
            wait = float(ERROR_BACKOFF_SEC.get(kind, 5.0))
            elapsed = self._clock() - last_err
            if elapsed < wait:
                return GateDecision(
                    False,
                    R_ERROR_BACKOFF,
                    f"직전 SendOrder 오류 후 {wait - elapsed:.0f}s 대기",
                )
        return GateDecision(True)

    # --- 결과 반영 / 로그 스로틀 ------------------------------------------
    def record_send_result(self, kind: str, code: str, result: SendResult) -> None:
        with self._lock:
            key = (kind, str(code))
            if result.status in (S_API_ERROR, S_EXCEPTION):
                self._last_error_at[key] = self._clock()
                self.stats["SEND_ERROR"] += 1
            elif result.status == S_ACCEPTED:
                self._last_error_at.pop(key, None)
                self.stats["SEND_ACCEPTED"] += 1

    def should_log_rejection(self, key: tuple, interval_sec: float = 60.0) -> bool:
        with self._lock:
            now = self._clock()
            last = self._reject_log_at.get(key)
            if last is not None and now - last < interval_sec:
                return False
            self._reject_log_at[key] = now
            return True

    # --- 표시용 ----------------------------------------------------------
    def status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "live_orders_enabled": self.live_enabled(),
                "block_buys": self.block_buys,
                "block_all": self.block_all,
                "emergency": self.emergency,
                "emergency_at": self.emergency_at.isoformat(sep=" ", timespec="seconds")
                if self.emergency_at
                else "",
                "connected": self._connected,
                "resync_required": self.resync_required,
                "resync_reason": self.resync_reason,
                "stats": dict(self.stats),
            }


def status_flags(st: Dict[str, Any]) -> List[str]:
    """상태 라벨에 표시할 차단 항목 (엔진 유무와 무관하게 동일 규칙)."""
    flags: List[str] = []
    if st.get("emergency"):
        flags.append("긴급정지")
    if st.get("block_all"):
        flags.append("주문전체차단")
    if st.get("block_buys"):
        flags.append("매수차단")
    if st.get("connected") is False:
        flags.append("연결끊김")
    if st.get("resync_required"):
        flags.append("잔고·미체결 재확인 대기")
    return flags


_GATE: Optional[OrderSafetyGate] = None
_GATE_LOCK = threading.Lock()


def get_gate() -> OrderSafetyGate:
    """프로세스 전역 게이트(주문 경로가 하나이므로 게이트도 하나)."""
    global _GATE
    with _GATE_LOCK:
        if _GATE is None:
            _GATE = OrderSafetyGate()
        return _GATE


def reset_gate_for_tests(gate: Optional[OrderSafetyGate] = None) -> OrderSafetyGate:
    global _GATE
    with _GATE_LOCK:
        _GATE = gate or OrderSafetyGate()
        return _GATE


# ---------------------------------------------------------------------------
# 중앙 주문 함수 — 프로그램 전체에서 SendOrder 를 부르는 유일한 곳
# ---------------------------------------------------------------------------
def probe_connected(api: Any) -> bool:
    """GetConnectState()==1 일 때만 True. 조회 실패는 '끊김'으로 취급(fail-closed)."""
    try:
        return int(api.dynamicCall("GetConnectState()") or 0) == 1
    except Exception:
        return False


def probe_server_is_mock(api: Any) -> Optional[bool]:
    """GetServerGubun: '1' 모의 / 그 외 값 실서버 / 조회 불가 None.
    로그인 시점에 저장한 값(api.server_gubun)을 보조로 쓴다."""
    gubun = ""
    try:
        gubun = str(api.dynamicCall("GetLoginInfo(QString)", "GetServerGubun") or "").strip()
    except Exception:
        gubun = ""
    if not gubun:
        gubun = str(getattr(api, "server_gubun", "") or "").strip()
    if not gubun:
        return None
    return gubun == "1"


def guarded_send_order(
    api: Any,
    *,
    rqname: str,
    screen_no: str,
    account: str,
    order_type: int,
    code: str,
    qty: int,
    price: int,
    hoga: str,
    org_order_no: str = "",
    gate: Optional[OrderSafetyGate] = None,
) -> SendResult:
    """게이트 검사 → SendOrder → 반환값 해석. 이 함수 밖에서 SendOrder 를 호출하지 않는다."""
    g = gate or getattr(api, "safety_gate", None) or get_gate()
    kind = _KIND_BY_ORDER_TYPE.get(int(order_type), "")
    code_s = str(code or "").strip()
    try:
        qty_i = int(qty)
    except Exception:
        qty_i = 0

    if not kind:
        return SendResult(False, S_INVALID, kind or "?", code_s, qty_i, reason=R_INVALID,
                          detail=f"지원하지 않는 order_type={order_type}")
    if qty_i <= 0 or not code_s or not str(account or "").strip():
        return SendResult(False, S_INVALID, kind, code_s, qty_i, reason=R_INVALID,
                          detail="수량/종목/계좌 값 오류")
    if kind == KIND_CANCEL and not str(org_order_no or "").strip():
        return SendResult(False, S_INVALID, kind, code_s, qty_i, reason=R_INVALID,
                          detail="취소 주문에 원주문번호 없음")

    decision = g.authorize(
        kind,
        code_s,
        server_is_mock=probe_server_is_mock(api),
        connected=probe_connected(api),
    )
    if not decision.allowed:
        return SendResult(False, S_BLOCKED, kind, code_s, qty_i, reason=decision.reason,
                          detail=decision.detail)

    try:
        ret = api.dynamicCall(
            "SendOrder(QString, QString, QString, int, QString, int, int, QString, QString)",
            [rqname, screen_no, account, int(order_type), code_s, qty_i, int(price), hoga,
             str(org_order_no or "")],
        )
    except Exception as e:  # noqa: BLE001
        res = SendResult(False, S_EXCEPTION, kind, code_s, qty_i, reason="SENDORDER_EXCEPTION",
                         detail=f"{type(e).__name__}: {e}")
        g.record_send_result(kind, code_s, res)
        return res

    try:
        ret_i: Optional[int] = int(ret)
    except Exception:
        ret_i = None

    if ret_i == 0:
        res = SendResult(True, S_ACCEPTED, kind, code_s, qty_i, ret_code=0,
                         reason="REQUEST_ACCEPTED", detail=describe_send_order_code(0))
    else:
        res = SendResult(False, S_API_ERROR, kind, code_s, qty_i, ret_code=ret_i,
                         reason="SENDORDER_RETURN_NONZERO", detail=describe_send_order_code(ret_i))
    g.record_send_result(kind, code_s, res)
    return res


# ---------------------------------------------------------------------------
# 미체결 주문 모델 / 취소 계획
# ---------------------------------------------------------------------------
@dataclass
class OpenOrder:
    order_no: str
    code: str
    side: str            # BUY / SELL / ""
    open_qty: int
    order_qty: int = 0
    price: int = 0
    status: str = ""
    raw_type: str = ""
    name: str = ""
    time_str: str = ""

    def cancellable(self) -> bool:
        if self.open_qty <= 0 or not self.order_no or self.side not in (KIND_BUY, KIND_SELL):
            return False
        if "취소" in self.raw_type or "정정" in self.raw_type:
            return False
        return True

    def cancel_order_type(self) -> int:
        return ORDER_TYPE_CANCEL_BUY if self.side == KIND_BUY else ORDER_TYPE_CANCEL_SELL


def _to_int(s: Any) -> int:
    txt = str(s if s is not None else "").strip().replace(",", "").lstrip("+")
    neg = txt.startswith("-")
    digits = "".join(ch for ch in txt if ch.isdigit())
    if not digits:
        return 0
    v = int(digits)
    return -v if neg else v


def normalize_open_order(row: Dict[str, Any]) -> Optional[OpenOrder]:
    """opt10075 한 행(dict) → OpenOrder. 필수값이 없으면 None."""
    order_no = str(row.get("주문번호", "") or "").strip()
    code = str(row.get("종목코드", "") or "").strip().lstrip("A")
    if not order_no or not code:
        return None
    raw_type = str(row.get("주문구분", "") or "").strip()
    side = ""
    if "매수" in raw_type:
        side = KIND_BUY
    elif "매도" in raw_type:
        side = KIND_SELL
    return OpenOrder(
        order_no=order_no,
        code=code,
        side=side,
        open_qty=abs(_to_int(row.get("미체결수량", 0))),
        order_qty=abs(_to_int(row.get("주문수량", 0))),
        price=abs(_to_int(row.get("주문가격", 0))),
        status=str(row.get("주문상태", "") or "").strip(),
        raw_type=raw_type,
        name=str(row.get("종목명", "") or "").strip(),
        time_str=str(row.get("시간", "") or "").strip(),
    )


# ---------------------------------------------------------------------------
# 안내 문구 — 손절·익절/자동손절 보장에 대해 사실과 다른 표시를 하지 않는다
# ---------------------------------------------------------------------------
NOTICE_STOP_PAUSE = (
    "자동매매 일시 정지: 신규 진입뿐 아니라 손절·익절 판단(on_tick)도 실행되지 않습니다."
)
NOTICE_BLOCK_ALL = (
    "신규 주문 전체 차단: 매수·매도 주문이 모두 전송되지 않습니다. "
    "손절·익절 매도 주문도 나가지 않으니 보유 종목을 직접 확인하세요."
)
NOTICE_EMERGENCY = (
    "긴급정지: 신규 매수 차단 + 미체결 취소 요청 + 계좌 상태 기록을 수행했습니다. "
    "손절·익절 로직의 켜짐/꺼짐 상태는 이 기능이 바꾸지 않습니다. "
    "앱 종료·연결 단절·정지 상태에서는 자동 손절이 보장되지 않습니다."
)


# ---------------------------------------------------------------------------
# 긴급정지
# ---------------------------------------------------------------------------
@dataclass
class EmergencyReport:
    started_at: str
    open_orders_checked: bool = False
    open_orders: List[Dict[str, Any]] = field(default_factory=list)
    cancel_results: List[Dict[str, Any]] = field(default_factory=list)
    snapshot_path: str = ""
    lines: List[str] = field(default_factory=list)

    def text(self) -> str:
        return "\n".join(self.lines)


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (datetime.datetime, datetime.date)):
        return obj.isoformat(sep=" ") if isinstance(obj, datetime.datetime) else obj.isoformat()
    if isinstance(obj, (int, float, str, bool)) or obj is None:
        return obj
    return str(obj)


def run_emergency_stop(
    *,
    gate: OrderSafetyGate,
    api: Any,
    account: str,
    positions: Dict[str, Any],
    pending_orders: Dict[str, Any],
    snapshot_dir: str,
    reason: str = "사용자 긴급정지",
    log: Optional[Callable[[str], None]] = None,
    record_event: Optional[Callable[..., None]] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> EmergencyReport:
    """긴급정지 실행.

    1) 신규 매수 즉시 차단  2) 미체결 조회  3) 취소 가능한 주문 취소 *요청*
    4) 보유·계좌 상태 기록(JSON)
    취소 요청 성공은 취소 완료가 아니다. 이 함수는 손절·익절 로직을 켜거나 끄지 않는다.
    """
    now = datetime.datetime.now()
    rep = EmergencyReport(started_at=now.isoformat(sep=" ", timespec="seconds"))

    # 로그/이벤트 기록 콜백이 실패해도 긴급정지 절차(차단·취소·상태기록)는 끝까지 수행한다.
    def emit(msg: str) -> None:
        if log is None:
            return
        try:
            log(msg)
        except Exception:  # noqa: BLE001
            pass

    def rec(**kw: Any) -> None:
        if record_event is None:
            return
        try:
            record_event(**kw)
        except Exception as e:  # noqa: BLE001
            rep.lines.append(f"[긴급정지] 이벤트 기록 실패({kw.get('event', '')}): {type(e).__name__}: {e}")

    gate.activate_emergency(reason)
    rep.lines.append("[긴급정지] 신규 매수 차단 적용")
    rec(event="EMERGENCY_STOP", code="", side="", qty=0, price=0.0, note=f"reason={reason}")

    orders: Optional[List[OpenOrder]] = None
    try:
        orders = api.request_open_orders(account)
    except Exception as e:  # noqa: BLE001
        rep.lines.append(f"[긴급정지] 미체결 조회 예외: {type(e).__name__}: {e}")
    if orders is None:
        rep.lines.append("[긴급정지] 미체결 조회 실패 — 취소 요청을 보내지 못함 (HTS/MTS에서 직접 확인 필요)")
    else:
        rep.open_orders_checked = True
        rep.open_orders = [o.__dict__.copy() for o in orders]
        rep.lines.append(f"[긴급정지] 미체결 {len(orders)}건 확인")
        for o in orders:
            if not o.cancellable():
                rep.lines.append(
                    f"  - 취소 불가/불필요: {o.code} {o.raw_type or '-'} 주문번호={o.order_no} 미체결={o.open_qty}"
                )
                continue
            try:
                res = api.cancel_order(account, o)
            except Exception as e:  # noqa: BLE001
                res = SendResult(False, S_EXCEPTION, KIND_CANCEL, o.code, o.open_qty,
                                 reason="CANCEL_EXCEPTION", detail=f"{type(e).__name__}: {e}")
            rep.cancel_results.append(
                {
                    "order_no": o.order_no,
                    "code": o.code,
                    "side": o.side,
                    "qty": o.open_qty,
                    "status": res.status,
                    "ret_code": res.ret_code,
                    "accepted": res.accepted,
                    "detail": res.detail,
                }
            )
            rep.lines.append(
                f"  - 취소 요청 {'접수' if res.accepted else '실패'}: {o.code} {o.side} "
                f"주문번호={o.order_no} 수량={o.open_qty} ({res.status} {res.detail})"
            )
            rec(
                event="ORDER_CANCEL_REQUESTED" if res.accepted else "ORDER_CANCEL_FAILED",
                code=o.code,
                side=o.side,
                qty=o.open_qty,
                price=float(o.price),
                note=f"order_no={o.order_no} status={res.status} ret={res.ret_code} 취소완료 아님(요청)",
            )

    snapshot = {
        "created_at": rep.started_at,
        "reason": reason,
        "account": account,
        "gate": gate.status(),
        "positions": positions,
        "pending_orders_in_app": pending_orders,
        "open_orders_from_broker": rep.open_orders if rep.open_orders_checked else None,
        "cancel_results": rep.cancel_results,
        "extra": extra or {},
        "notice": NOTICE_EMERGENCY,
    }
    try:
        os.makedirs(snapshot_dir, exist_ok=True)
        path = os.path.join(snapshot_dir, f"emergency_stop_{now.strftime('%Y%m%d_%H%M%S')}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(_jsonable(snapshot), f, ensure_ascii=False, indent=2)
        rep.snapshot_path = path
        rep.lines.append(f"[긴급정지] 보유·계좌 상태 기록: {path}")
    except Exception as e:  # noqa: BLE001
        rep.lines.append(f"[긴급정지] 상태 기록 실패: {type(e).__name__}: {e}")

    rep.lines.append(NOTICE_EMERGENCY)
    for ln in rep.lines:
        emit(ln)
    return rep


# ---------------------------------------------------------------------------
# 중복 실행 방지 (Windows 명명 뮤텍스)
# ---------------------------------------------------------------------------
DEFAULT_INSTANCE_MUTEX = "Local\\KiwoomAutoTrader.main.single_instance.v1"
_ERROR_ALREADY_EXISTS = 183
_ERROR_ACCESS_DENIED = 5


class InstanceLock:
    """프로세스가 죽으면 OS 가 뮤텍스를 정리하므로 '죽은 락 파일' 문제가 없다."""

    def __init__(self, name: str = DEFAULT_INSTANCE_MUTEX) -> None:
        self.name = name
        self._handle: Any = None
        self.held = False

    def acquire(self) -> bool:
        if sys.platform != "win32":
            # 이 프로그램은 Windows(키움 OCX) 전용이다. 비Windows 에서는 검사 불가 → 실패로 취급.
            return False
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = wintypes.HANDLE
        k32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        ctypes.set_last_error(0)
        handle = k32.CreateMutexW(None, False, self.name)
        err = ctypes.get_last_error()
        if not handle:
            # 생성 실패(ACCESS_DENIED = 같은 이름의 뮤텍스가 다른 권한으로 이미 존재 포함)
            # → 중복 실행 여부를 확신할 수 없으므로 실행 차단(fail-closed).
            return False
        if err == _ERROR_ALREADY_EXISTS:
            k32.CloseHandle(handle)
            return False
        self._handle = handle
        self.held = True
        return True

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            import ctypes
            from ctypes import wintypes

            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.CloseHandle.argtypes = [wintypes.HANDLE]
            k32.CloseHandle(self._handle)
        finally:
            self._handle = None
            self.held = False

    def __enter__(self) -> "InstanceLock":
        self.acquire()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.release()
