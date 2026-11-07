"""
0.5단계 — 실제 키움 OCX(모의투자) 실시간 데이터 / 미체결 구조 / 네트워크 단절·복구 검증 하네스.

절대 원칙 (검증 전용 — 자동매매 기능을 추가하지 않는다)
  * SendOrder 는 어떤 종류(매수/매도/취소)도 호출하지 않는다.
    - 이 하네스의 dynamicCall 래퍼가 SendOrder* 를 무조건 예외로 막고 시도 횟수를 기록한다.
    - 하네스는 send_order_market / cancel_order / guarded_send_order 를 호출하지 않는다.
  * LIVE_ORDERS_ENABLED=0 강제. 서버 구분이 모의("1")가 아니면 즉시 중단.
  * TradingEngine 은 (disconnect 단계에서만) 만들지만 start()/timer 는 시작하지 않는다.
  * main.py / 전략 / watchdog_runner.py 는 건드리지 않는다. 결과는 log/stage0_verify/ 에만 쓴다.

단계(--phases 콤마 구분, 기본 "realtime,orders")
  realtime   : 005930 화면9001에 FID 10;20;228 신규("0"), 화면5202에 호가 FID 신규("0").
               본관찰 중 SetRealRemove 금지. 주식체결과 주식예상체결을 분리하고,
               콜백 건수와 FID 유효값 건수를 따로 센 뒤 관찰 종료 후 종합 판정.
               장외 수신은 NOT VERIFIED (정식 장중 PASS 금지).
  orders     : opt10075 원시 필드 + 제품 파서(normalize_open_order) 비교. 0건이면 파싱 PASS 로 보지 않는다.
  disconnect : 사용자가 실제로 네트워크를 끊고/복구한다. 화면 배너와 콘솔이 시점을 안내한다.

사용:
  python -X utf8 tools\\stage05_kiwoom_verify.py                       # 장중: realtime + orders
  python -X utf8 tools\\stage05_kiwoom_verify.py --phases disconnect   # 사용자가 네트워크 조작
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import statistics
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

os.environ["LIVE_ORDERS_ENABLED"] = "0"  # 무슨 일이 있어도 0

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PASS, FAIL, NV = "PASS", "FAIL", "NOT VERIFIED"

# 실시간 FID 정의 (키움 OpenAPI+ 실시간 목록)
FID_PRICE, FID_TIME, FID_STRENGTH = 10, 20, 228
FIDS_ASK_PRICE = [41, 42, 43, 44, 45]      # 매도호가1~5
FIDS_BID_PRICE = [51, 52, 53, 54, 55]      # 매수호가1~5
FIDS_ASK_QTY = [61, 62, 63, 64, 65]        # 매도호가수량1~5
FIDS_BID_QTY = [71, 72, 73, 74, 75]        # 매수호가수량1~5
FIDS_TOTAL_QTY = [121, 125]                # 매도/매수 호가총잔량
FID_HOGA_TIME = 21
HOGA_FIDS = [FID_HOGA_TIME] + FIDS_ASK_PRICE + FIDS_BID_PRICE + FIDS_ASK_QTY + FIDS_BID_QTY + FIDS_TOTAL_QTY

OPEN_ORDER_FIELDS = [
    "주문번호", "종목코드", "종목명", "주문상태", "주문수량", "주문가격", "미체결수량",
    "주문구분", "매매구분", "매도수구분", "시간", "주문/체결시간", "원주문번호", "현재가",
]
OPEN_ORDER_REQUIRED = ["주문번호", "종목코드", "주문수량", "미체결수량", "주문가격", "매매구분"]


# ---------------------------------------------------------------------------
# 순수 함수 (Qt/키움 없이 테스트 가능)
# ---------------------------------------------------------------------------
def is_regular_session(now: Optional[datetime.datetime] = None) -> bool:
    """평일 09:00~15:30 (휴장일은 알 수 없으므로 '추정')."""
    now = now or datetime.datetime.now()
    if now.weekday() >= 5:
        return False
    hm = now.hour * 60 + now.minute
    return 9 * 60 <= hm < 15 * 60 + 30


def parse_hhmmss(s: str) -> Optional[int]:
    """'093015' / '09:30:15' → 초(0~86399). 형식이 틀리면 None."""
    t = str(s or "").strip().replace(":", "")
    if len(t) != 6 or not t.isdigit():
        return None
    h, mi, se = int(t[:2]), int(t[2:4]), int(t[4:])
    if h > 23 or mi > 59 or se > 59:
        return None
    return h * 3600 + mi * 60 + se


def _pct(vals: Sequence[float], p: float) -> float:
    if not vals:
        return 0.0
    v = sorted(vals)
    k = max(0, min(len(v) - 1, int(round((len(v) - 1) * p))))
    return float(v[k])


def analyze_ticks(
    events: Sequence[Dict[str, Any]], *, gap_warn_sec: float = 10.0, delay_warn_sec: float = 3.0
) -> Dict[str, Any]:
    """체결 이벤트 목록 → 건수 / 수신 간격 / FID20 정합성 / 지연 통계.

    events 원소: {"t": 단조시계(초), "wall": 로컬 시각 기준 하루초(0~86399), "fid10": str, "fid20": str, "fid228": str}
    """
    n = len(events)
    res: Dict[str, Any] = {"count": n}
    if n == 0:
        return res
    ts = [float(e["t"]) for e in events]
    span = max(ts) - min(ts)
    res["span_sec"] = round(span, 2)
    res["rate_per_sec"] = round(n / span, 3) if span > 0 else None
    gaps = [b - a for a, b in zip(ts, ts[1:])]
    if gaps:
        res["gap_mean"] = round(statistics.mean(gaps), 3)
        res["gap_median"] = round(statistics.median(gaps), 3)
        res["gap_p95"] = round(_pct(gaps, 0.95), 3)
        res["gap_max"] = round(max(gaps), 3)
        res["gaps_over_warn"] = sorted(((round(g, 2), i) for i, g in enumerate(gaps) if g > gap_warn_sec), reverse=True)[:10]
    # FID10 / FID20 / FID228 수신
    res["fid10_nonempty"] = sum(1 for e in events if str(e.get("fid10", "")).strip() not in ("", "0"))
    res["fid20_nonempty"] = sum(1 for e in events if str(e.get("fid20", "")).strip())
    res["fid228_nonempty"] = sum(1 for e in events if str(e.get("fid228", "")).strip())
    f20 = [(parse_hhmmss(e.get("fid20", "")), e) for e in events]
    valid20 = [(s, e) for s, e in f20 if s is not None]
    res["fid20_valid_format"] = len(valid20)
    res["fid20_bad_format"] = sum(1 for s, e in f20 if s is None and str(e.get("fid20", "")).strip())
    back = 0
    jump = []
    prev = None
    for s, _e in valid20:
        if prev is not None:
            if s < prev:
                back += 1
            elif s - prev > gap_warn_sec:
                jump.append(s - prev)
        prev = s
    res["fid20_backwards"] = back          # 체결시각이 거꾸로 감(비정상/순서 뒤바뀜)
    res["fid20_jumps_over_warn"] = len(jump)  # 체결시각이 크게 건너뜀 → 누락 의심(거래가 없었을 수도 있음)
    delays = [float(e["wall"]) - float(s) for s, e in valid20]
    if delays:
        res["delay_median"] = round(statistics.median(delays), 2)
        res["delay_p95"] = round(_pct(delays, 0.95), 2)
        res["delay_max"] = round(max(delays), 2)
        res["delay_note"] = "수신 로컬시각 − FID20(거래소 체결시각). PC 시계 오차가 그대로 섞인다."
    res["gap_warn_sec"] = gap_warn_sec
    res["delay_warn_sec"] = delay_warn_sec
    return res


def analyze_hoga(events: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """호가잔량 이벤트 → FID 별 수신(비어있지 않은) 건수."""
    per: Dict[int, int] = {f: 0 for f in HOGA_FIDS}
    for e in events:
        for f in HOGA_FIDS:
            if str(e.get("fids", {}).get(f, "")).strip() not in ("",):
                per[f] += 1
    return {"count": len(events), "per_fid_nonempty": per}


def reg_ok(ret: Any) -> Optional[bool]:
    """SetRealReg 반환값 → True(0=성공) / False / None(알 수 없음)."""
    if ret is None:
        return None
    try:
        return int(ret) == 0
    except Exception:
        return None


SCREEN_UNKNOWN = "unknown"
SCREEN_AMBIGUOUS = "ambiguous"


def fid10_is_valid(raw: str) -> bool:
    return str(raw or "").strip() not in ("", "0")


def fid20_is_valid(raw: str) -> bool:
    return parse_hhmmss(raw) is not None


def fid228_is_valid(raw: str) -> bool:
    return bool(str(raw or "").strip())


def fids_for_real_type(rtype: str) -> Optional[set]:
    """real_type 이 쓰는 FID 집합. 모르면 None (화면을 추정하지 않음)."""
    rt = str(rtype or "").strip()
    if rt in ("주식체결", "주식예상체결"):
        return {FID_PRICE, FID_TIME, FID_STRENGTH}
    if rt == "주식호가잔량":
        return set(HOGA_FIDS)
    if rt.startswith("VI"):
        return {9068}
    return None


def infer_screen(rtype: str, screens: Dict[str, Dict[str, Any]]) -> Tuple[str, bool]:
    """화면번호는 이벤트에 없으므로, 등록 FID 교집합으로만 붙인다.

    유일 매칭 → 그 화면번호. 2개 이상 → ambiguous. 없으면 unknown.
    활성 화면이 없고 제거된 화면만 맞으면 after_remove=True, 화면은 unknown (추정 금지).
    """
    needed = fids_for_real_type(rtype)
    active = [(s, inf) for s, inf in screens.items() if not inf.get("removed_at")]
    removed = [(s, inf) for s, inf in screens.items() if inf.get("removed_at")]

    def matched(items: List[Tuple[str, Dict[str, Any]]]) -> List[str]:
        if needed is None:
            return []
        out: List[str] = []
        for s, inf in items:
            fids = inf.get("fids") or set()
            if fids & needed:
                out.append(s)
        return out

    hit = matched(active)
    if len(hit) == 1:
        return hit[0], False
    if len(hit) > 1:
        return SCREEN_AMBIGUOUS, False
    if matched(removed):
        return SCREEN_UNKNOWN, True
    return SCREEN_UNKNOWN, False


def sample_raw(values: Sequence[str], n: int = 10) -> Dict[str, Any]:
    vals = [str(v) for v in values]
    return {"count": len(vals), "sample": vals[:n], "sample_short": len(vals) < n}


def judge_fid_receive(*, session: bool, callbacks: int, expected: int, hoga: int, valid: int) -> str:
    """관찰 종료 후 종합 판정. 장외는 유효값이 있어도 PASS 금지."""
    if not session:
        return NV
    if valid >= 1:
        return PASS
    if callbacks >= 1:
        return FAIL
    if expected > 0 or hoga > 0:
        return NV
    return FAIL


def parse_setreal_args(name: str, args: Sequence[Any]) -> Dict[str, str]:
    """SetRealReg(screen, code, fids, real_type) / SetRealRemove(screen, code) 인자 분해."""
    a = [str(x) for x in (args or [])]
    n = str(name or "")
    if n.startswith("SetRealReg"):
        return {
            "screen": a[0] if len(a) > 0 else "",
            "code": a[1] if len(a) > 1 else "",
            "fids": a[2] if len(a) > 2 else "",
            "real_type": a[3] if len(a) > 3 else "",
        }
    return {
        "screen": a[0] if len(a) > 0 else "",
        "code": a[1] if len(a) > 1 else "",
        "fids": "",
        "real_type": "",
    }


def fids_set_to_str(fids: Any) -> str:
    if isinstance(fids, str):
        return fids
    if not fids:
        return ""
    return ";".join(str(x) for x in sorted(fids))


def make_setreal_entry(
    op: str,
    *,
    screen: str = "",
    code: str = "",
    fids: str = "",
    real_type: str = "",
    ret: Any = None,
    args: Optional[Sequence[Any]] = None,
) -> Dict[str, Any]:
    return {
        "op": op,
        "at": time.time(),
        "wall": datetime.datetime.now().isoformat(timespec="seconds"),
        "screen": str(screen),
        "code": str(code),
        "fids": str(fids),
        "real_type": str(real_type),
        "ret": ret,
        "args": [str(x) for x in (args if args is not None else (screen, code, fids, real_type))],
    }


class FakeSignal:
    """PyQt 시그널 대신 쓰는 테스트용. slots 로 연결 상태를 확인한다."""

    def __init__(self) -> None:
        self.slots: List[Any] = []

    def connect(self, fn: Any) -> None:
        if fn in self.slots:
            raise RuntimeError("already connected")
        self.slots.append(fn)

    def disconnect(self, fn: Any) -> None:
        if fn not in self.slots:
            raise RuntimeError("not connected")
        self.slots.remove(fn)


class RealSlotGuard:
    """OnReceiveRealData 를 제품 슬롯 하나 / 하네스 슬롯 하나만 유지한다."""

    def __init__(self, signal: Any, product_slot: Any, log: Optional[Callable[[str], None]] = None) -> None:
        self.signal = signal
        self.product_slot = product_slot
        self.harness_slot: Any = None
        self._product_ours = True
        self._harness_ours = False
        self.log = log or (lambda _m: None)

    def _known_slots(self) -> Optional[List[Any]]:
        slots = getattr(self.signal, "slots", None)
        return list(slots) if isinstance(slots, list) else None

    def _is_connected(self, fn: Any) -> Optional[bool]:
        known = self._known_slots()
        if known is None:
            return None
        return fn in known

    def _disconnect(self, fn: Any) -> bool:
        try:
            self.signal.disconnect(fn)
            return True
        except Exception as e:
            self.log(f"disconnect 실패: {type(e).__name__}: {e}")
            return False

    def attach(self, harness_slot: Any) -> Tuple[bool, str]:
        if self._harness_ours and self.harness_slot is harness_slot and not self._product_ours:
            if self._is_connected(harness_slot) is False:
                pass
            else:
                return True, "이미 하네스만 연결됨 — 재연결하지 않음"
        if self._harness_ours and self.harness_slot is not None:
            self._disconnect(self.harness_slot)
            self._harness_ours = False
            self.harness_slot = None
        if self._product_ours:
            prod_on = self._is_connected(self.product_slot)
            if prod_on is False:
                self._product_ours = False
            else:
                if not self._disconnect(self.product_slot):
                    return False, "제품 핸들러 disconnect 실패 — 중복 방지를 위해 하네스를 연결하지 않음"
                self._product_ours = False
        h_on = self._is_connected(harness_slot)
        if h_on is True:
            self.harness_slot = harness_slot
            self._harness_ours = True
            return True, "하네스 이미 연결"
        try:
            self.signal.connect(harness_slot)
        except Exception as e:
            return False, f"하네스 connect 실패: {e}"
        self.harness_slot = harness_slot
        self._harness_ours = True
        return True, "하네스 연결, 제품 핸들러 분리"

    def restore(self) -> Tuple[bool, List[str]]:
        msgs: List[str] = []
        ok = True
        if self._harness_ours and self.harness_slot is not None:
            h_on = self._is_connected(self.harness_slot)
            if h_on is False:
                msgs.append("하네스 슬롯 이미 없음")
            elif not self._disconnect(self.harness_slot):
                ok = False
                msgs.append("하네스 disconnect 실패")
            else:
                msgs.append("하네스 분리")
            self._harness_ours = False
            self.harness_slot = None
        p_on = self._is_connected(self.product_slot)
        if p_on is True or (self._product_ours and not self._harness_ours and p_on is not False):
            if p_on is True:
                msgs.append("제품 핸들러 이미 연결 — 재연결 생략")
            self._product_ours = True
        else:
            if p_on is None:
                if self._disconnect(self.product_slot):
                    msgs.append("제품 핸들러 잔여 연결 분리")
            try:
                self.signal.connect(self.product_slot)
                self._product_ours = True
                msgs.append("제품 핸들러 재연결")
            except Exception as e:
                ok = False
                msgs.append(f"제품 핸들러 재연결 실패: {e}")
        return ok, msgs


# ---------------------------------------------------------------------------
# 이하 실제 키움 실행부
# ---------------------------------------------------------------------------
STAMP = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
OUT_DIR = os.path.join(ROOT, "log", "stage0_verify")
LOG_PATH = os.path.join(OUT_DIR, f"stage05_verify_{STAMP}.txt")
JSON_PATH = os.path.join(OUT_DIR, f"stage05_verify_{STAMP}.json")
RESULTS: List[Dict[str, Any]] = []
SENDORDER_ATTEMPTS: List[Any] = []
EXTRA: Dict[str, Any] = {}
_RESTORE_HOOK: Optional[Callable[[], Tuple[bool, str]]] = None


def out(msg: str) -> None:
    line = f"[{datetime.datetime.now():%H:%M:%S}] {msg}"
    print(line, flush=True)
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def rec(item: str, status: str, detail: str = "") -> None:
    RESULTS.append({"item": item, "status": status, "detail": detail})
    out(f"## {status:12s} {item} :: {detail}")
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump({"results": RESULTS, "extra": EXTRA, "sendorder_attempts": SENDORDER_ATTEMPTS},
                  f, ensure_ascii=False, indent=2, default=str)


def mask(s: str) -> str:
    s = str(s or "")
    return s[:4] + "*" * max(0, len(s) - 4)


def snapshot_files() -> dict:
    snap = {}
    for dp, dn, fn in os.walk(ROOT):
        dn[:] = [d for d in dn if d not in (".git", "__pycache__", "node_modules") and not d.startswith("backup")]
        if os.path.relpath(dp, ROOT).startswith(os.path.join("log", "stage0_verify")):
            continue
        for f in fn:
            p = os.path.join(dp, f)
            try:
                st = os.stat(p)
                snap[os.path.relpath(p, ROOT)] = (st.st_size, int(st.st_mtime))
            except OSError:
                pass
    return snap


def main() -> int:  # noqa: C901
    global _RESTORE_HOOK
    ap = argparse.ArgumentParser()
    ap.add_argument("--phases", default="realtime,orders")
    ap.add_argument("--code", default="005930")
    ap.add_argument("--rt-seconds", type=int, default=180, help="본관찰 시간(초). 기본 180. 중간에 FAIL 하지 않음")
    ap.add_argument("--aux-seconds", type=int, default=30, help="FID228 / 호가 각 관찰 시간(초)")
    ap.add_argument("--gap-warn", type=float, default=10.0)
    ap.add_argument("--login-timeout", type=int, default=120)
    ap.add_argument("--disconnect-wait", type=int, default=600, help="사용자가 네트워크를 끊을 때까지 최대 대기(초)")
    ap.add_argument("--restore-wait", type=int, default=600, help="사용자가 네트워크를 복구할 때까지 최대 대기(초)")
    ap.add_argument("--state-grace", type=int, default=60, help="네트워크 단절 후 GetConnectState 변화를 기다리는 시간(초)")
    ap.add_argument("--force-offhours", action="store_true", help="장외에도 realtime 단계를 실행(등록 성공 여부만 의미 있음)")
    ap.add_argument(
        "--wait-for-open",
        action="store_true",
        help="장 시작 전에 로그인만 하고 09:00까지 대기한 뒤 실시간 검증을 시작한다. 장외 수신을 성공으로 치지 않는다.",
    )
    ap.add_argument(
        "--login-check",
        action="store_true",
        help="모의투자 로그인·서버 구분만 확인하고 종료한다. 실시간 검증을 하지 않는다.",
    )
    args = ap.parse_args()
    phases = [p.strip() for p in args.phases.split(",") if p.strip()]

    os.chdir(ROOT)
    sys.path.insert(0, ROOT)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    from PyQt5.QtCore import QEventLoop, QTimer, Qt
    from PyQt5.QtWidgets import QApplication, QLabel, QWidget, QVBoxLayout

    app = QApplication(sys.argv)  # noqa: F841
    try:
        import pythoncom  # type: ignore

        pythoncom.CoInitialize()
    except Exception:
        pass

    def pump(ms: int) -> None:
        loop = QEventLoop()
        QTimer.singleShot(int(ms), loop.quit)
        loop.exec_()

    class Banner(QWidget):
        """항상 위에 떠 있는 안내창 — 네트워크를 끊고/복구할 시점을 알려준다."""

        def __init__(self) -> None:
            super().__init__()
            self.setWindowTitle("0.5단계 검증 안내")
            self.setWindowFlags(Qt.WindowStaysOnTopHint | Qt.Window)
            lay = QVBoxLayout(self)
            self.lbl = QLabel("대기")
            self.lbl.setWordWrap(True)
            self.lbl.setStyleSheet("font-size:20px;font-weight:bold;padding:16px;")
            lay.addWidget(self.lbl)
            self.resize(640, 220)

        def say(self, text: str, color: str = "#003366", beep: bool = False) -> None:
            self.lbl.setText(text)
            self.lbl.setStyleSheet(f"font-size:20px;font-weight:bold;padding:16px;color:{color};")
            self.show()
            self.raise_()
            if beep:
                try:
                    import winsound

                    winsound.Beep(1200, 400)
                except Exception:
                    pass
            pump(50)

    before = snapshot_files()
    out(f"=== 0.5단계 키움 모의투자 검증 시작 {STAMP} phases={phases} (LIVE_ORDERS_ENABLED={os.environ['LIVE_ORDERS_ENABLED']}) ===")
    now0 = datetime.datetime.now()
    session = is_regular_session(now0)
    EXTRA["start"] = str(now0)
    EXTRA["regular_session_estimated"] = session
    out(f"현재 {now0:%Y-%m-%d %H:%M:%S} → 정규장 추정={session} (휴장일은 알 수 없음)")

    import order_safety as osf
    import main as m

    # ---- 로그인 / 서버 구분 ----------------------------------------------------------
    kiwoom = m.KiwoomOpenAPI()
    if kiwoom.isNull():
        rec("키움 OCX 로드", FAIL, "KHOPENAPI 컨트롤 로딩 실패")
        return 3

    setreg_log: List[Dict[str, Any]] = []
    screens: Dict[str, Dict[str, Any]] = {}
    orig_dc = kiwoom.dynamicCall

    def guarded(name, *a):
        n = str(name)
        if n.startswith("SendOrder"):
            SENDORDER_ATTEMPTS.append(list(a[0]) if a else [])
            raise RuntimeError("HARNESS BLOCKED SendOrder (0.5단계는 어떤 주문도 보내지 않는다)")
        if n.startswith("CommTerminate"):
            raise RuntimeError("HARNESS BLOCKED CommTerminate")
        r = orig_dc(name, *a)
        if n.startswith("SetRealReg") or n.startswith("SetRealRemove"):
            args_ = list(a[0]) if a and isinstance(a[0], (list, tuple)) else list(a)
            parsed = parse_setreal_args(n, args_)
            info = screens.get(parsed["screen"], {})
            fids = parsed["fids"] or fids_set_to_str(info.get("fids"))
            rtype = parsed["real_type"] or str(info.get("real_type") or "")
            entry = make_setreal_entry(
                "SetRealReg" if n.startswith("SetRealReg") else "SetRealRemove",
                screen=parsed["screen"], code=parsed["code"], fids=fids,
                real_type=rtype, ret=r, args=args_,
            )
            if n.startswith("SetRealReg"):
                setreg_log.append(entry)
                EXTRA.setdefault("setrealreg_log", []).append(entry)
            else:
                EXTRA.setdefault("setrealremove_log", []).append(entry)
        return r

    kiwoom.dynamicCall = guarded
    kiwoom.on_real_price = None
    gate = osf.reset_gate_for_tests(osf.OrderSafetyGate())
    kiwoom.safety_gate = gate

    out(f"로그인 시도(최대 {args.login_timeout}s) — 로그인 창이 뜨면 모의투자로 로그인해 주세요")
    try:
        account = kiwoom.login(timeout_ms=args.login_timeout * 1000)
    except Exception as e:  # noqa: BLE001
        rec("키움 로그인", NV, f"로그인 완료 못함: {type(e).__name__}: {e}")
        return 4
    state = int(kiwoom.dynamicCall("GetConnectState()") or 0)
    gubun = str(kiwoom.dynamicCall("GetLoginInfo(QString)", "GetServerGubun") or "").strip()
    rec("키움 로그인", PASS if state == 1 else FAIL, f"GetConnectState={state}, 계좌={mask(account)}")
    if gubun != "1":
        rec("서버 구분", FAIL, f"GetServerGubun='{gubun}' — 모의 아님. 이후 검증 전체 중단(실계좌 보호)")
        return 5
    rec("서버 구분", PASS, "GetServerGubun='1'(모의투자)")
    if args.login_check:
        rec("로그인 확인만 수행", PASS, "요청에 따라 실시간·opt10075·단절 검증은 하지 않고 종료. SendOrder 0")
        rec("SendOrder 호출 횟수(하네스 감지)", PASS if not SENDORDER_ATTEMPTS else FAIL,
            f"{len(SENDORDER_ATTEMPTS)}건 {SENDORDER_ATTEMPTS}")
        rec("LIVE_ORDERS_ENABLED", PASS if os.environ.get("LIVE_ORDERS_ENABLED") == "0" else FAIL,
            f"={os.environ.get('LIVE_ORDERS_ENABLED')}")
        return 0
    code = args.code

    # 장 시작 전 로그인: 09:00까지 이벤트 루프를 유지한다. 장외 수신을 PASS로 쓰지 않는다.
    if args.wait_for_open and not is_regular_session():
        now_w = datetime.datetime.now()
        if now_w.weekday() >= 5 or (now_w.hour * 60 + now_w.minute) >= 15 * 60 + 30:
            rec("정규장 대기", FAIL, f"오늘 정규장을 기다릴 수 없는 시각({now_w:%H:%M}). 다음날 장 시작 전에 실행하세요")
            return 6
        try:
            import ctypes

            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001 | 0x00000002)
        except Exception:
            pass
        banner_wait = Banner()
        out("로그인 완료 — 정규장 09:00까지 대기. PC 절전을 끄고 이 창을 닫지 마세요. 8시 MyStock 자동시작은 중복 실행으로 실패하는 것이 정상입니다.")
        lost = False
        while not is_regular_session():
            now_w = datetime.datetime.now()
            if (now_w.hour * 60 + now_w.minute) >= 15 * 60 + 30:
                rec("정규장 대기", FAIL, "09:00을 만나지 못한 채 15:30이 지남")
                return 6
            try:
                st_w = int(orig_dc("GetConnectState()") or 0)
            except Exception:
                st_w = 0
            if st_w != 1:
                lost = True
                break
            left = (9 * 60) - (now_w.hour * 60 + now_w.minute)
            banner_wait.say(
                f"정규장 대기 중\n지금 {now_w:%H:%M:%S}\n약 {max(0, left)}분 후 09:00에 검증 시작\n창을 닫지 마세요. 주문은 보내지 않습니다.",
                "#0d47a1",
            )
            pump(1000)
        try:
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
        except Exception:
            pass
        if lost:
            rec("정규장 대기", FAIL, "대기 중 GetConnectState≠1. 자리에 없어 재로그인할 수 없음")
            return 6
        session = is_regular_session()
        EXTRA["regular_session_estimated"] = session
        rec("정규장 대기", PASS if session else FAIL, f"09:00 도달, GetConnectState=1, 정규장={session}")
        if not session:
            return 6

    # ---- 실시간 수신 수집기 ------------------------------------------------------------
    SCREEN_CHE, SCREEN_HOGA = "9001", "5202"
    collector: Dict[str, Any] = {
        "tag": "", "listening": False, "tick": [], "hoga": [], "events": [],
        "types": {}, "overflow": 0, "real_connected": False,
    }
    EV_CAP = 8000

    def _fid(c: str, fid: int) -> str:
        try:
            return str(orig_dc("GetCommRealData(QString, int)", c, fid) or "").strip()
        except Exception:
            return ""

    def _now_wall() -> Tuple[datetime.datetime, int]:
        now = datetime.datetime.now()
        return now, now.hour * 3600 + now.minute * 60 + now.second

    def on_real(rcode, rtype, _rdata):
        rcode = str(rcode).strip()
        rtype = str(rtype).strip()
        if rcode != code:
            return
        tag = collector["tag"] or "unregistered"
        scr, after_rm = infer_screen(rtype, screens)
        now, wall = _now_wall()
        rec_ev: Dict[str, Any] = {
            "t": time.monotonic(), "wall": wall, "iso": now.isoformat(timespec="seconds"),
            "code": rcode, "real_type": rtype, "screen": scr, "after_remove": after_rm,
            "tag": tag, "fid10": "", "fid20": "", "fid228": "",
        }
        if rtype in ("주식체결", "주식예상체결"):
            rec_ev["fid10"] = _fid(rcode, FID_PRICE)
            rec_ev["fid20"] = _fid(rcode, FID_TIME)
            rec_ev["fid228"] = _fid(rcode, FID_STRENGTH)
        elif rtype == "주식호가잔량":
            rec_ev["fids"] = {f: _fid(rcode, f) for f in HOGA_FIDS}
        if len(collector["events"]) < EV_CAP:
            collector["events"].append(rec_ev)
        else:
            collector["overflow"] += 1
        collector["types"][rtype] = collector["types"].get(rtype, 0) + 1
        if rtype == "주식체결":
            collector["tick"].append(rec_ev)
        elif rtype == "주식호가잔량":
            collector["hoga"].append(rec_ev)

    slot_guard = RealSlotGuard(kiwoom.OnReceiveRealData, kiwoom._on_receive_real_data, log=out)
    _restored = {"done": False}

    def attach_real_slot() -> None:
        ok_att, msg_att = slot_guard.attach(on_real)
        collector["real_connected"] = bool(slot_guard.harness_slot)
        out(f"[SAFETY] OnReceiveRealData attach: {msg_att}")
        if not ok_att:
            rec("OnReceiveRealData 하네스 연결", FAIL, msg_att)

    def restore_product_real_slot() -> Tuple[bool, str]:
        if _restored["done"]:
            return True, "이미 복구 완료"
        try:
            ok_r, msgs_r = slot_guard.restore()
            detail = "; ".join(msgs_r) or "복구 시도"
            out(f"[SAFETY] OnReceiveRealData 복구: {detail}")
            collector["real_connected"] = bool(slot_guard.harness_slot)
            rec("OnReceiveRealData 제품 핸들러 복구", PASS if ok_r else FAIL, detail)
            if not ok_r:
                out(f"[SAFETY] OnReceiveRealData 복구 실패: {detail}")
            _restored["done"] = True
            return ok_r, detail
        except Exception as e:  # noqa: BLE001
            detail = f"복구 예외: {type(e).__name__}: {e}"
            out(f"[SAFETY] OnReceiveRealData 복구 실패: {detail}")
            rec("OnReceiveRealData 제품 핸들러 복구", FAIL, detail)
            _restored["done"] = True
            return False, detail

    def ticks_of(tag: str) -> List[Dict[str, Any]]:
        return [e for e in collector["tick"] if e.get("tag") == tag]

    def hoga_of(tag: str) -> List[Dict[str, Any]]:
        return [e for e in collector["hoga"] if e.get("tag") == tag]

    def register_screen(screen: str, fids: str, real_type: str = "0") -> Optional[bool]:
        """real_type '0' = 해당 화면 신규 등록. 다른 화면 추가가 아님."""
        ret = orig_dc("SetRealReg(QString, QString, QString, QString)", screen, code, fids, real_type)
        entry = make_setreal_entry(
            "SetRealReg", screen=screen, code=code, fids=fids, real_type=real_type, ret=ret,
            args=(screen, code, fids, real_type),
        )
        EXTRA.setdefault("setrealreg_log", []).append(entry)
        fidset = {int(x) for x in fids.split(";") if x.strip().isdigit()}
        screens[screen] = {
            "fids": fidset, "registered_at": entry["wall"], "removed_at": None, "real_type": real_type,
        }
        out(f"SetRealReg screen={screen} code={code} fids={fids} type={real_type} ret={ret} at={entry['wall']}")
        return reg_ok(ret)

    def remove_screen(screen: str) -> None:
        info = screens.get(screen, {})
        fids = fids_set_to_str(info.get("fids"))
        rtype = str(info.get("real_type") or "")
        ret: Any = None
        try:
            ret = orig_dc("SetRealRemove(QString, QString)", screen, code)
        except Exception as e:  # noqa: BLE001
            out(f"SetRealRemove 예외 {screen}: {e}")
            ret = f"exception:{type(e).__name__}"
        entry = make_setreal_entry(
            "SetRealRemove", screen=screen, code=code, fids=fids, real_type=rtype, ret=ret,
            args=(screen, code),
        )
        EXTRA.setdefault("setrealremove_log", []).append(entry)
        if screen in screens:
            screens[screen]["removed_at"] = entry["wall"]
        out(f"SetRealRemove screen={screen} code={code} fids={fids} type={rtype} ret={ret} at={entry['wall']}")

    def setreg_direct(screen: str, fids: str, real_type: str = "0") -> Optional[bool]:
        return register_screen(screen, fids, real_type)

    def remove(screen: str) -> None:
        remove_screen(screen)

    def watch(tag: str, seconds: int) -> None:
        collector["tag"] = tag
        collector["listening"] = True
        end = time.monotonic() + seconds
        last = 0
        while time.monotonic() < end:
            pump(1000)
            if int(time.monotonic()) % 15 == 0 and int(time.monotonic()) != last:
                last = int(time.monotonic())
                main_ev = [e for e in collector["events"] if e.get("tag") == tag and not e.get("after_remove")]
                n_che = sum(1 for e in main_ev if e["real_type"] == "주식체결")
                n_exp = sum(1 for e in main_ev if e["real_type"] == "주식예상체결")
                n_hoga = sum(1 for e in main_ev if e["real_type"] == "주식호가잔량")
                n10 = sum(1 for e in main_ev if e["real_type"] == "주식체결" and fid10_is_valid(e.get("fid10", "")))
                n20 = sum(1 for e in main_ev if e["real_type"] == "주식체결" and fid20_is_valid(e.get("fid20", "")))
                n228 = sum(1 for e in main_ev if e["real_type"] == "주식체결" and fid228_is_valid(e.get("fid228", "")))
                out(f"  [{tag}] 주식체결콜백={n_che} 예상체결={n_exp} 호가={n_hoga} "
                    f"FID10유효={n10} FID20유효={n20} FID228유효={n228}")
        collector["tag"] = ""

    def _run_verify_phases() -> int:
        # ======================================================================
        # 실시간 단계
        # ======================================================================
        if "realtime" in phases:
            if not session and not args.force_offhours:
                rec("② 실시간 단계 전체", NV,
                    f"현재 {datetime.datetime.now():%H:%M} 장외 추정 — 수신 검증 불가. 평일 09:00~15:30 에 다시 실행하거나 "
                    "--force-offhours 로 '등록 성공 여부'만 확인하세요")
            else:
                # 본관찰: 9001(10;20;228, 신규0) + 5202(호가, 신규0). 중간에 SetRealRemove 금지.
                hoga_fid_str = ";".join(str(f) for f in HOGA_FIDS)
                ok_che = register_screen(SCREEN_CHE, f"{FID_PRICE};{FID_TIME};{FID_STRENGTH}", "0")
                rec("② 화면9001 등록(FID10;20;228, 신규 type=0)",
                    PASS if ok_che else (FAIL if ok_che is False else NV),
                    f"SetRealReg 반환={screens.get(SCREEN_CHE)}")
                ok_hg = register_screen(SCREEN_HOGA, hoga_fid_str, "0")
                rec("③ 화면5202 등록(호가 FID, 신규 type=0)",
                    PASS if ok_hg else (FAIL if ok_hg is False else NV),
                    f"SetRealReg 반환={screens.get(SCREEN_HOGA)}")
                out(f"본관찰 {args.rt_seconds}초 (중간 FAIL 없음, 종료 후 종합 판정) …")
                watch("main", args.rt_seconds)

                main_ev = [e for e in collector["events"] if e.get("tag") == "main" and not e.get("after_remove")]
                n_che = sum(1 for e in main_ev if e["real_type"] == "주식체결")
                n_exp = sum(1 for e in main_ev if e["real_type"] == "주식예상체결")
                n_hoga = sum(1 for e in main_ev if e["real_type"] == "주식호가잔량")
                che_ev = [e for e in main_ev if e["real_type"] == "주식체결"]
                exp_ev = [e for e in main_ev if e["real_type"] == "주식예상체결"]
                n10 = sum(1 for e in che_ev if fid10_is_valid(e.get("fid10", "")))
                n20 = sum(1 for e in che_ev if fid20_is_valid(e.get("fid20", "")))
                n228 = sum(1 for e in che_ev if fid228_is_valid(e.get("fid228", "")))
                n10_exp = sum(1 for e in exp_ev if fid10_is_valid(e.get("fid10", "")))

                by_screen: Dict[str, Dict[str, int]] = {}
                for e in main_ev:
                    s = str(e.get("screen") or SCREEN_UNKNOWN)
                    d = by_screen.setdefault(s, {"callback": 0, "fid10_valid": 0, "fid20_valid": 0, "fid228_valid": 0})
                    d["callback"] += 1
                    if e["real_type"] in ("주식체결", "주식예상체결"):
                        d["fid10_valid"] += int(fid10_is_valid(e.get("fid10", "")))
                        d["fid20_valid"] += int(fid20_is_valid(e.get("fid20", "")))
                        d["fid228_valid"] += int(fid228_is_valid(e.get("fid228", "")))

                EXTRA["real_types_seen"] = dict(collector["types"])
                EXTRA["counts"] = {
                    "callback_chegyul": n_che, "callback_expected": n_exp, "callback_hoga": n_hoga,
                    "fid10_valid_chegyul": n10, "fid20_valid_chegyul": n20, "fid228_valid_chegyul": n228,
                    "fid10_valid_expected_ref_only": n10_exp, "overflow": collector["overflow"],
                }
                EXTRA["counts_by_screen"] = by_screen
                EXTRA["fid_samples"] = {
                    "fid10": sample_raw([e["fid10"] for e in che_ev if fid10_is_valid(e.get("fid10", ""))]),
                    "fid20": sample_raw([e["fid20"] for e in che_ev if fid20_is_valid(e.get("fid20", ""))]),
                    "fid228": sample_raw([e["fid228"] for e in che_ev if fid228_is_valid(e.get("fid228", ""))]),
                }
                EXTRA["events_sample"] = {
                    "주식체결": che_ev[:20],
                    "주식예상체결": exp_ev[:20],
                    "주식호가잔량": [e for e in main_ev if e["real_type"] == "주식호가잔량"][:20],
                }
                EXTRA["tick_stats_chegyul"] = analyze_ticks(che_ev, gap_warn_sec=args.gap_warn)
                hoga_main = [e for e in main_ev if e["real_type"] == "주식호가잔량"]
                EXTRA["hoga_stats"] = analyze_hoga(hoga_main)
                if hoga_main:
                    EXTRA["hoga_sample"] = hoga_main[-1].get("fids")

                sess_now = is_regular_session()
                j10 = judge_fid_receive(session=sess_now, callbacks=n_che, expected=n_exp, hoga=n_hoga, valid=n10)
                j20 = judge_fid_receive(session=sess_now, callbacks=n_che, expected=n_exp, hoga=n_hoga, valid=n20)
                j228 = judge_fid_receive(session=sess_now, callbacks=n_che, expected=n_exp, hoga=n_hoga, valid=n228)
                if not sess_now:
                    note_off = "장외 리허설 — 정식 장중 PASS로 기록하지 않음"
                else:
                    note_off = ""
                rec("② 주식체결 vs 주식예상체결 콜백",
                    NV if not sess_now else (PASS if (n_che + n_exp + n_hoga) > 0 else FAIL),
                    f"주식체결={n_che} 주식예상체결={n_exp} 호가={n_hoga} {note_off}".strip())
                rec("② FID10(체결가) 실제 값 수신", j10,
                    f"주식체결콜백={n_che} FID10유효={n10} 예상체결FID10참고={n10_exp} {note_off}".strip())
                rec("② FID20(체결시각) 실제 값 수신", j20,
                    f"주식체결콜백={n_che} FID20유효={n20} {note_off}".strip())
                rec("③ FID228(체결강도) 실제 값 수신", j228,
                    f"주식체결콜백={n_che} FID228유효={n228} {note_off}".strip())
                EXTRA["judgment"] = {"session": sess_now, "fid10": j10, "fid20": j20, "fid228": j228, "note": note_off}

                groups = [
                    ("매도호가 1~5(FID41~45)", FIDS_ASK_PRICE), ("매수호가 1~5(FID51~55)", FIDS_BID_PRICE),
                    ("매도호가수량 1~5(FID61~65)", FIDS_ASK_QTY), ("매수호가수량 1~5(FID71~75)", FIDS_BID_QTY),
                    ("총잔량 매도/매수(FID121,125)", FIDS_TOTAL_QTY),
                ]
                per = EXTRA["hoga_stats"]["per_fid_nonempty"]
                for label, fids in groups:
                    if not hoga_main:
                        rec(f"③ {label} 실제 수신", NV if not sess_now else FAIL,
                            f"호가 이벤트 0건 {note_off}".strip())
                    else:
                        got = {f: per[f] for f in fids}
                        rec(f"③ {label} 실제 수신",
                            NV if not sess_now else (PASS if all(v > 0 for v in got.values()) else FAIL),
                            f"호가 이벤트 {len(hoga_main)}건, FID별 값 있음 {got} {note_off}".strip())

                # 관찰 종료 후에만 제거. 이후 짧은 구간은 after_remove 로만 기록.
                collector["tag"] = "after_remove"
                remove_screen(SCREEN_CHE)
                remove_screen(SCREEN_HOGA)
                pump(3000)
                after_ev = [e for e in collector["events"] if e.get("tag") == "after_remove" or e.get("after_remove")]
                EXTRA["after_remove"] = {
                    "count": len(after_ev),
                    "types": {},
                    "sample": after_ev[:10],
                }
                for e in after_ev:
                    EXTRA["after_remove"]["types"][e["real_type"]] = EXTRA["after_remove"]["types"].get(e["real_type"], 0) + 1
                rec("② 화면 제거 후 이벤트", PASS,
                    f"{len(after_ev)}건 (본관찰 판정에 포함하지 않음) types={EXTRA['after_remove']['types']}")
                collector["tag"] = ""

        # ======================================================================
        # 미체결(opt10075) 구조 단계
        # ======================================================================
        if "orders" in phases:
            def raw_open_orders(timeout_ms: int = 15000) -> Optional[List[Dict[str, str]]]:
                rq = "S05_OPEN_ORDERS"
                holder: Dict[str, Any] = {}
                loop = QEventLoop()

                def on_tr(screen_no, rqname, trcode, record, prev_next, *rest):
                    if str(rqname) != rq:
                        return
                    try:
                        cnt = int(orig_dc("GetRepeatCnt(QString, QString)", trcode, rqname) or 0)
                        rows = []
                        for i in range(cnt):
                            rows.append({
                                f: str(orig_dc("GetCommData(QString, QString, int, QString)", trcode, rqname, i, f) or "").strip()
                                for f in OPEN_ORDER_FIELDS
                            })
                        holder.update(ok=True, rows=rows, cnt=cnt, prev_next=str(prev_next).strip())
                    except Exception as e:  # noqa: BLE001
                        holder.update(ok=False, err=str(e))
                    loop.quit()

                kiwoom.OnReceiveTrData.connect(on_tr)
                try:
                    orig_dc("SetInputValue(QString, QString)", "계좌번호", account)
                    orig_dc("SetInputValue(QString, QString)", "전체종목구분", "0")
                    orig_dc("SetInputValue(QString, QString)", "매매구분", "0")
                    orig_dc("SetInputValue(QString, QString)", "종목코드", "")
                    orig_dc("SetInputValue(QString, QString)", "체결구분", "1")
                    ret = orig_dc("CommRqData(QString, QString, int, QString)", rq, "opt10075", 0, m.SCREEN_NO_ACCOUNT_EVAL)
                    if ret is not None and int(ret) != 0:
                        holder.update(ok=False, err=f"CommRqData ret={ret}")
                    else:
                        QTimer.singleShot(timeout_ms, loop.quit)
                        loop.exec_()
                finally:
                    try:
                        kiwoom.OnReceiveTrData.disconnect(on_tr)
                    except Exception:
                        pass
                return holder.get("rows") if holder.get("ok") else None

            raw = raw_open_orders()
            if raw is None:
                rec("④ opt10075 조회 응답", FAIL, "응답 없음/오류 (조회 실패)")
            else:
                rec("④ opt10075 조회 응답", PASS, f"응답 수신, 반복 데이터 {len(raw)}건")
                rec("④ 반복 데이터 개수", PASS, f"{len(raw)}건")
                if not raw:
                    for fld in OPEN_ORDER_REQUIRED:
                        rec(f"④ 필드 '{fld}'", NV, "미체결 0건 → 구조/파싱 확인 불가 (파싱 성공으로 판정하지 않음). 실제 미체결이 있을 때 재검증")
                    rec("④ 제품 파서(normalize_open_order) 일치", NV, "미체결 0건")
                else:
                    for i, r in enumerate(raw):
                        out(f"   미체결[{i}] " + ", ".join(f"{k}={mask(v) if k == '계좌번호' else v}" for k, v in r.items() if v))
                    for fld in OPEN_ORDER_REQUIRED:
                        miss = [i for i, r in enumerate(raw) if not r.get(fld)]
                        rec(f"④ 필드 '{fld}'", PASS if not miss else FAIL,
                            f"{len(raw)}행 모두 값 있음, 예 {raw[0].get(fld)!r}" if not miss else f"값 없는 행 {miss}")
                    prod = kiwoom.request_open_orders(account)
                    if prod is None:
                        rec("④ 제품 파서(normalize_open_order) 일치", FAIL, "제품 request_open_orders 가 None")
                    else:
                        raw_keys = sorted((r["주문번호"], r["종목코드"].lstrip("A"), abs(int(r["미체결수량"].replace(",", "") or 0)))
                                          for r in raw if r.get("주문번호"))
                        prod_keys = sorted((o.order_no, o.code, o.open_qty) for o in prod)
                        same = raw_keys == prod_keys
                        sides = [(o.order_no, o.side) for o in prod]
                        empty_time = sum(1 for o in prod if not o.time_str)
                        rec("④ 제품 파서(normalize_open_order) 일치", PASS if same and all(s for _n, s in sides) else FAIL,
                            f"원시 {len(raw_keys)}건 vs 제품 {len(prod_keys)}건 일치={same}, side 판정 {sides}, "
                            f"제품 time_str 비어있음 {empty_time}건 (필드 '시간' vs '주문/체결시간' 중 값 있는 쪽: "
                            f"{[(r.get('시간'), r.get('주문/체결시간')) for r in raw[:2]]})")
            rec("④ 취소 주문", NV, "0.5단계는 취소 주문을 보내지 않는다(금지). 취소 검증은 별도 승인 후")

        # ======================================================================
        # 네트워크 단절/복구 단계
        # ======================================================================
        if "disconnect" in phases:
            import socket

            def net_ok() -> bool:
                for host in (("8.8.8.8", 53), ("1.1.1.1", 53)):
                    try:
                        s = socket.create_connection(host, timeout=1.0)
                        s.close()
                        return True
                    except OSError:
                        continue
                return False

            banner = Banner()
            engine = m.TradingEngine(kiwoom, account, [])
            vdir = os.path.join(OUT_DIR, "engine_artifacts_05")
            os.makedirs(vdir, exist_ok=True)
            engine.order_event_csv_path = os.path.join(vdir, "order_event_log_verify.csv")
            engine._write_log_file = lambda msg: open(LOG_PATH, "a", encoding="utf-8").write(f"[ENGINE] {msg}\n")
            pw = os.environ.get("KIWOOM_ACCOUNT_PASSWORD", "")
            engine._account_password = pw
            engine.on_log = lambda s: out(f"[ENGINE] {s}")
            engine.timer = QTimer()   # 더미 — on_tick 에 연결하지 않는다(매매 로직 미실행)

            # 재동기화 판정 기록용 래퍼
            resync_calls: List[Dict[str, Any]] = []
            real_complete = gate.complete_resync

            def complete_wrapper(token, *, holdings_ok, open_orders_ok):
                before_req = gate.resync_required
                r = real_complete(token, holdings_ok=holdings_ok, open_orders_ok=open_orders_ok)
                resync_calls.append({"at": time.time(), "resync_required_before": before_req,
                                     "holdings_ok": holdings_ok, "open_orders_ok": open_orders_ok, "released": r})
                return r

            gate.complete_resync = complete_wrapper

            def decide(kind: str):
                return gate.authorize(kind, code, server_is_mock=osf.probe_server_is_mock(kiwoom),
                                      connected=osf.probe_connected(kiwoom))

            # 기준 상태
            engine._safety_on_tick(osf.probe_connected(kiwoom))
            d0 = decide(osf.KIND_BUY)
            rec("⑤ 단절 전 기준 상태", PASS if d0.allowed else FAIL, f"연결=1, 게이트 허용={d0.allowed}/{d0.reason} (주문은 보내지 않음)")

            rt_on = session
            if rt_on:
                kiwoom.set_real_reg([code], f"{FID_PRICE};{FID_TIME}")
                collector["tag"] = "D_pre"
                pump(8000)
                out(f"단절 전 실시간 체결 {len(ticks_of('D_pre'))}건")

            banner.say("지금부터 네트워크를 끊어 주세요\n(Wi-Fi 끄기 또는 랜선 뽑기)\n끊는 순간부터 자동으로 감지합니다.", "#b00020", beep=True)
            out(">>> 지금 네트워크를 끊어 주세요 (Wi-Fi 끄기 / 랜선 뽑기). 최대 "
                f"{args.disconnect_wait}초 대기. 키움 로그인 창/오류창이 떠도 그대로 두세요.")
            if rt_on:
                collector["tag"] = "D_pre"

            t_start = time.monotonic()
            t_net_down: Optional[float] = None
            t_state0: Optional[float] = None
            t_gate_blocked: Optional[float] = None
            tr_probe_fail = 0
            last_probe = 0.0
            while time.monotonic() - t_start < args.disconnect_wait:
                pump(900)
                n_ok = net_ok()
                st = int(orig_dc("GetConnectState()") or 0)
                engine._safety_on_tick(st == 1)
                if (not n_ok) and t_net_down is None:
                    t_net_down = time.monotonic()
                    collector["tag"] = "D_out" if rt_on else ""
                    out(f"네트워크 단절 감지(소켓 점검 실패) — GetConnectState={st}")
                    banner.say("네트워크 단절 확인 — 키움 상태 변화를 기다리는 중…\n(네트워크는 아직 복구하지 마세요)", "#e65100")
                if st == 0 and t_state0 is None:
                    t_state0 = time.monotonic()
                    out(f"GetConnectState → 0 (단절 후 {t_state0 - (t_net_down or t_state0):.1f}s)")
                if t_state0 is not None:
                    if t_gate_blocked is None and not decide(osf.KIND_BUY).allowed:
                        t_gate_blocked = time.monotonic()
                    break
                if t_net_down is not None:
                    since = time.monotonic() - t_net_down
                    if since > args.state_grace and time.monotonic() - last_probe > 15:
                        last_probe = time.monotonic()
                        r = kiwoom.request_open_orders(account, timeout_ms=5000)
                        if r is None:
                            tr_probe_fail += 1
                            out(f"능동 TR 점검(opt10075) 실패 {tr_probe_fail}회 — GetConnectState 는 아직 {st}")
                    if since > args.state_grace + 45:
                        out("단절 후 상태 변화 없이 한도 경과 — 관찰 종료")
                        break

            EXTRA["disconnect"] = {
                "net_down_detected": t_net_down is not None,
                "state0_detected": t_state0 is not None,
                "state0_after_net_down_sec": None if (t_state0 is None or t_net_down is None) else round(t_state0 - t_net_down, 1),
                "tr_probe_failures": tr_probe_fail,
            }
            if t_net_down is None:
                rec("⑤ 실제 네트워크 단절", NV, f"{args.disconnect_wait}초 안에 네트워크 단절을 감지하지 못함(사용자가 끊지 않음)")
                banner.say("네트워크 단절을 못 봤습니다. 검증 종료.", "#555555")
            else:
                rec("⑤ 실제 네트워크 단절", PASS, "소켓 점검으로 단절 확인")
                if t_state0 is None:
                    rec("⑤ 키움 연결 상태 변화(GetConnectState)", FAIL,
                        f"단절 후 {args.state_grace}s+ 동안 GetConnectState 가 1 에서 변하지 않음. 능동 TR 점검 실패 {tr_probe_fail}회 — "
                        "앱 게이트는 GetConnectState 만 보므로 이 상태에서는 단절을 인지하지 못함(발견 사항, 수정 전 보고)")
                    d = decide(osf.KIND_BUY)
                    rec("⑤ 단절 중 신규 주문 차단", FAIL if d.allowed else PASS,
                        f"GetConnectState=1 인 채 게이트 허용={d.allowed}/{d.reason}")
                else:
                    rec("⑤ 키움 연결 상태 변화(GetConnectState)", PASS,
                        f"네트워크 단절 후 {EXTRA['disconnect']['state0_after_net_down_sec']}s 에 GetConnectState=0")
                    db, ds = decide(osf.KIND_BUY), decide(osf.KIND_SELL)
                    ok_block = (not db.allowed and not ds.allowed and db.reason == osf.R_DISCONNECTED and gate.resync_required)
                    rec("⑤ 단절 중 신규 주문 차단", PASS if ok_block else FAIL,
                        f"매수→{db.allowed}/{db.reason}, 매도→{ds.allowed}/{ds.reason}, resync_required={gate.resync_required} (판정만, SendOrder 호출 0)")
                if rt_on:
                    n_out = len(ticks_of("D_out"))
                    rec("⑤ 단절 중 실시간 수신", PASS if n_out == 0 else NV, f"단절 구간 체결 수신 {n_out}건")

                # ---- 복구 ------------------------------------------------------------------------------
                banner.say("이제 네트워크를 복구해 주세요\n(Wi-Fi 켜기 / 랜선 연결)", "#1b5e20", beep=True)
                out(f">>> 지금 네트워크를 복구해 주세요. 최대 {args.restore_wait}초 대기. 복구 후 키움 로그인 창이 뜨면 모의투자로 로그인하세요.")
                t_wait = time.monotonic()
                consecutive = 0
                while time.monotonic() - t_wait < args.restore_wait and consecutive < 3:
                    pump(900)
                    consecutive = consecutive + 1 if net_ok() else 0
                    engine._safety_on_tick(int(orig_dc("GetConnectState()") or 0) == 1)
                if consecutive < 3:
                    rec("⑤ 네트워크 복구", NV, f"{args.restore_wait}초 안에 복구를 확인하지 못함")
                else:
                    t_net_up = time.monotonic()
                    rec("⑤ 네트워크 복구", PASS, "소켓 점검 3회 연속 성공")
                    st_now = int(orig_dc("GetConnectState()") or 0)
                    if st_now != 1:
                        pump(5000)
                        st_now = int(orig_dc("GetConnectState()") or 0)
                    if st_now == 1:
                        rec("⑤ 키움 연결 복구", PASS if t_state0 is not None else NV,
                            "GetConnectState=1 (자동 복구)" if t_state0 is not None
                            else "GetConnectState 는 처음부터 1 이었음 — 단절이 감지되지 않았던 경우")
                    else:
                        banner.say("키움 재로그인이 필요합니다.\n로그인 창에서 '모의투자'로 로그인해 주세요.", "#0d47a1", beep=True)
                        try:
                            kiwoom.login(timeout_ms=args.login_timeout * 1000)
                        except Exception as e:  # noqa: BLE001
                            rec("⑤ 키움 재로그인", FAIL, f"{type(e).__name__}: {e}")
                        st_now = int(orig_dc("GetConnectState()") or 0)
                        g2 = str(orig_dc("GetLoginInfo(QString)", "GetServerGubun") or "").strip() if st_now == 1 else ""
                        if st_now == 1 and g2 != "1":
                            rec("⑤ 키움 재로그인", FAIL, f"재로그인 서버 구분='{g2}' — 모의 아님. 이후 검증 중단")
                            st_now = -1
                        elif st_now == 1:
                            rec("⑤ 키움 재로그인", PASS, f"GetConnectState=1, 서버 구분 '1', 복구까지 {time.monotonic() - t_net_up:.0f}s")
                    if st_now == 1:
                        # 재동기화 전 차단 확인(첫 on_tick 이전)
                        if t_state0 is not None:
                            d = decide(osf.KIND_BUY)
                            rec("⑤ 재연결 직후 동기화 전 주문 차단", PASS if (not d.allowed and d.reason == osf.R_RESYNC_PENDING) else FAIL,
                                f"매수→{d.allowed}/{d.reason}, resync_required={gate.resync_required}")
                        h0 = float(getattr(kiwoom, "last_holdings_ok_at", 0.0) or 0.0)
                        # 엔진의 실제 on_tick 경로로 재동기화 유도(타이머는 쓰지 않고 수동 호출)
                        t_rs = time.monotonic()
                        while time.monotonic() - t_rs < 90:
                            engine._safety_on_tick(True)
                            pump(1000)
                            if not gate.resync_required:
                                break
                        h1 = float(getattr(kiwoom, "last_holdings_ok_at", 0.0) or 0.0)
                        EXTRA["resync_calls"] = resync_calls
                        if t_state0 is None:
                            rec("⑤ 재동기화(RESYNC_PENDING) 해제", NV, "단절이 앱에 감지되지 않아 재동기화 절차가 시작되지 않음")
                        else:
                            done = [c for c in resync_calls if c["released"]]
                            premature = [c for c in resync_calls if c["released"] and not (c["holdings_ok"] and c["open_orders_ok"])]
                            rec("⑦ 재연결 후 잔고 조회 성공", PASS if h1 > h0 else FAIL, f"last_holdings_ok_at 갱신={h1 > h0}")
                            rec("⑦ 재연결 후 미체결 조회 성공",
                                PASS if any(c["open_orders_ok"] for c in resync_calls) else FAIL,
                                f"complete_resync 호출 기록 {resync_calls}")
                            both = bool(done) and done[-1]["holdings_ok"] and done[-1]["open_orders_ok"] and done[-1]["resync_required_before"]
                            rec("⑦ 두 조회 성공 후에만 RESYNC_PENDING 해제",
                                PASS if (both and not premature and not gate.resync_required) else FAIL,
                                f"해제 {len(done)}회, 조기 해제 {len(premature)}회, resync_required={gate.resync_required}")
                            d = decide(osf.KIND_BUY)
                            rec("⑦ 해제 후 게이트 상태", PASS if d.allowed else FAIL, f"매수 판정 allowed={d.allowed}/{d.reason} (판정만)")
                        if rt_on:
                            collector["tag"] = "D_post_noreg"
                            pump(12000)
                            n_no = len(ticks_of("D_post_noreg"))
                            rec("⑤ 재연결 후 실시간 수신(재등록 없이)", PASS if n_no > 0 else NV,
                                f"12초간 {n_no}건 — 0건이면 재연결 후 SetRealReg 재등록이 필요하다는 뜻(앱은 FEED_RECOVER 로 재등록)")
                            kiwoom.set_real_reg([code], f"{FID_PRICE};{FID_TIME}")
                            collector["tag"] = "D_post_rereg"
                            pump(12000)
                            n_re = len(ticks_of("D_post_rereg"))
                            rec("⑤ 재연결 후 실시간 수신(재등록 후)", PASS if n_re > 0 else FAIL, f"12초간 {n_re}건")
                            collector["tag"] = ""
                        banner.say("단절·복구 검증 완료. 이 창은 닫아도 됩니다.", "#1b5e20")

        # ======================================================================
        rec("SendOrder 호출 횟수(하네스 감지)", PASS if not SENDORDER_ATTEMPTS else FAIL,
            f"{len(SENDORDER_ATTEMPTS)}건 {SENDORDER_ATTEMPTS}")
        rec("LIVE_ORDERS_ENABLED", PASS if os.environ.get("LIVE_ORDERS_ENABLED") == "0" else FAIL,
            f"={os.environ.get('LIVE_ORDERS_ENABLED')}")
        after = snapshot_files()
        changed = sorted(k for k in after if k not in before or after[k] != before[k])
        removed = sorted(k for k in before if k not in after)
        rec("파일 변화(기존 데이터/로그 삭제 여부)", PASS if not removed else FAIL,
            f"삭제 {len(removed)}건 {removed[:5]}, 신규/변경 {len(changed)}건 {changed[:25]}")
        EXTRA["setrealreg_calls_all"] = EXTRA.get("setrealreg_log") or setreg_log
        return 0

    _RESTORE_HOOK = restore_product_real_slot
    attach_real_slot()
    try:
        return _run_verify_phases()
    finally:
        try:
            restore_product_real_slot()
        except Exception as e:  # noqa: BLE001
            rec("OnReceiveRealData 제품 핸들러 복구", FAIL, f"복구 예외: {type(e).__name__}: {e}")
            out(f"[SAFETY] OnReceiveRealData 복구 실패: {type(e).__name__}: {e}")
        _RESTORE_HOOK = None




if __name__ == "__main__":
    rc = 1
    try:
        rc = main()
    except Exception:  # noqa: BLE001
        import traceback

        rec("하네스 예외", FAIL, traceback.format_exc()[-800:])
    finally:
        if _RESTORE_HOOK is not None:
            try:
                _RESTORE_HOOK()
            except Exception as e:  # noqa: BLE001
                rec("OnReceiveRealData 제품 핸들러 복구", FAIL, f"복구 예외: {type(e).__name__}: {e}")
                out(f"[SAFETY] OnReceiveRealData 복구 실패: {type(e).__name__}: {e}")
        out(f"=== 종료 rc={rc} 결과 {len(RESULTS)}건 → {JSON_PATH} ===")
        os._exit(rc if isinstance(rc, int) else 1)
