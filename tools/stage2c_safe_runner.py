"""HFT 2단계-C 검증 전용 안전 실행기.

실제 키움 로그인·OCX 연결·SendOrder 는 이 모듈이 수행하지 않는다.
테스트 세션에서 KiwoomOpenAPI.dynamicCall 을 가로채 SendOrder* / CommTerminate 를
원본 OCX 로 보내지 않고, TradingEngine.start() 직후 engine.timer 만 정지한다.

LIVE_ORDERS_ENABLED=0 은 실서버만 막으므로, 모의투자 주문 차단은 이 래퍼가 담당한다.

사용:
  python -X utf8 tools\\stage2c_safe_runner.py --self-test
  python -X utf8 -m unittest tests.test_stage2c_safe_runner -v
"""
from __future__ import annotations

import argparse
import datetime
import os
import sys
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ENV_LIVE = "LIVE_ORDERS_ENABLED"
ENV_SCALP = "SCALP_ENABLED"
ENV_SAFE = "STAGE2C_SAFE_MODE"
ENV_LOGIN_ONLY = "STAGE2C_LOGIN_ONLY"
ENV_COLLECT_ONLY = "STAGE2C_COLLECT_ONLY"
ENV_STAGE2D_CODES = "STAGE2D_CODES"
ATTR_GUARD = "_stage2c_guard"
ATTR_TIMER_ARMED = "_stage2c_timer_armed"
ATTR_TIMER_ORIG = "_stage2c_orig_start"
ATTR_TIMER_STOPPED = "_stage2c_timer_stopped"
ATTR_ARMED = "_stage2c_armed"
ATTR_ABORTED = "_stage2c_aborted"
ATTR_SESSION = "_stage2c_session"
ATTR_SEND_WRAP = "_stage2c_send_wrap"
ATTR_CANCEL_WRAP = "_stage2c_cancel_wrap"
WATCH_INTERVAL_MS = 250
REASON_METHOD_BLOCKED = "STAGE2C_METHOD_BLOCKED"
LOGIN_ONLY_MOCK_GUBUN = "1"
ATTR_MOCK_CONFIRMED = "_stage2c_mock_server_confirmed"

BLOCK_PREFIXES = ("SendOrder", "CommTerminate")
MUST_PASS_PREFIXES = (
    "GetCommRealData",
    "SetRealReg",
    "SetRealRemove",
    "CommRqData",
    "GetCommData",
    "GetConnectState",
)
LOGIN_ONLY_ALLOW_PREFIXES = (
    "CommConnect",
    "GetConnectState",
    "GetLoginInfo",
)
LOGIN_ONLY_GETLOGININFO_ALLOW = ("GetServerGubun",)
LOGIN_ONLY_LOGIN_RESULT = "STAGE2C_LOGIN_ONLY"
COLLECT_SCREEN_TICK = "8101"
COLLECT_SCREEN_HOGA = "8102"
COLLECT_SCREENS = (COLLECT_SCREEN_TICK, COLLECT_SCREEN_HOGA)
COLLECT_TICK_FIDS = frozenset((10, 13, 15, 20, 228))
COLLECT_HOGA_FIDS = frozenset(
    (21, 41, 42, 43, 44, 45, 51, 52, 53, 54, 55, 61, 62, 63, 64, 65, 71, 72, 73, 74, 75, 121, 125)
)
COLLECT_ALL_FIDS = COLLECT_TICK_FIDS | COLLECT_HOGA_FIDS
COLLECT_SCREEN_FIDS = {
    COLLECT_SCREEN_TICK: COLLECT_TICK_FIDS,
    COLLECT_SCREEN_HOGA: COLLECT_HOGA_FIDS,
}
COLLECT_MAX_CODES = 5
COLLECT_REG_OPT_NEW = "0"
COLLECT_ONLY_LOGIN_RESULT = "STAGE2C_COLLECT_ONLY"
DEFAULT_COLLECT_CODES_REL = os.path.join("data", "scalp", "codes.txt")


class GuardInstallError(RuntimeError):
    """래퍼를 인스턴스에 고정하지 못하면 로그인 전 단계에서 중단한다."""


class SendOrderBlockedError(RuntimeError):
    """SendOrder* / CommTerminate 가 원본 OCX 에 도달하기 전에 발생. 성공(ret=0)이 아니다."""

    def __init__(self, method: str, args: Any) -> None:
        self.method = method
        self.args = args
        super().__init__(f"STAGE2C BLOCKED {method}")


class SessionAbortError(RuntimeError):
    """타이머 정지 실패 등 세션을 진행하면 안 되는 상태."""


class LoginOnlyBlockedError(RuntimeError):
    """LOGIN_ONLY 허용 목록 외 OCX 호출. 원본에는 도달하지 않는다."""

    def __init__(self, method: str, args: Any) -> None:
        self.method = method
        self.args = args
        super().__init__(f"STAGE2C LOGIN_ONLY BLOCKED {method}")


class CollectOnlyBlockedError(RuntimeError):
    """COLLECT_ONLY 허용 목록 외 OCX 호출. 원본에는 도달하지 않는다."""

    def __init__(self, method: str, args: Any) -> None:
        self.method = method
        self.args = args
        super().__init__(f"STAGE2C COLLECT_ONLY BLOCKED {method}")


class CollectCodesError(SessionAbortError):
    """COLLECT_ONLY 종목 목록이 비었거나 유효하지 않다. CommConnect 전에 중단한다."""


def method_base(name: Any) -> str:
    raw = str(name or "").strip()
    return raw.split("(", 1)[0].strip()


def is_blocked_method(name: Any) -> bool:
    base = method_base(name)
    return any(base.startswith(p) for p in BLOCK_PREFIXES)


def is_must_pass_method(name: Any) -> bool:
    base = method_base(name)
    return any(base.startswith(p) for p in MUST_PASS_PREFIXES)


def login_only_info_tag(args: Any) -> str:
    """GetLoginInfo 첫 인자. Qt는 문자열 또는 1원소 리스트로 넘길 수 있다."""
    if args is None:
        return ""
    if isinstance(args, (str, bytes)):
        return str(args).strip()
    try:
        seq = tuple(args)
    except TypeError:
        return str(args or "").strip()
    if len(seq) == 1 and isinstance(seq[0], (list, tuple)) and not isinstance(seq[0], (str, bytes)):
        seq = tuple(seq[0])
    if not seq:
        return ""
    return str(seq[0] or "").strip()


def is_login_only_allowed_method(name: Any, args: Any = ()) -> bool:
    base = method_base(name)
    if base.startswith("GetLoginInfo"):
        return login_only_info_tag(args) in LOGIN_ONLY_GETLOGININFO_ALLOW
    return any(base.startswith(p) for p in LOGIN_ONLY_ALLOW_PREFIXES if p != "GetLoginInfo")


ORIG_OCX_METHOD_KEYS = (
    "CommConnect",
    "GetConnectState",
    "GetLoginInfo:GetServerGubun",
    "SetRealReg",
    "SetRealRemove",
    "GetCommRealData",
    "unknown",
)
ORIG_REMOVE_SCREENS = ("8101", "8102", "other")


def classify_orig_ocx_method(name: Any, args: Any = ()) -> Tuple[str, str]:
    """원본 전달 메서드 분류. 계좌·USER_ID·비밀번호·종목코드는 반환하지 않는다."""
    base = method_base(name)
    if base.startswith("CommConnect"):
        return "CommConnect", ""
    if base.startswith("GetConnectState"):
        return "GetConnectState", ""
    if base.startswith("GetLoginInfo"):
        tag = login_only_info_tag(args)
        if tag == "GetServerGubun":
            return "GetLoginInfo:GetServerGubun", ""
        return "unknown", ""
    if base.startswith("SetRealReg"):
        return "SetRealReg", ""
    if base.startswith("SetRealRemove"):
        flat = flatten_ocx_args(args)
        screen = str(flat[0]).strip() if flat else ""
        if screen in ("8101", "8102"):
            return "SetRealRemove", screen
        return "SetRealRemove", "other"
    if base.startswith("GetCommRealData"):
        return "GetCommRealData", ""
    return "unknown", ""


def flatten_ocx_args(args: Any) -> Tuple[Any, ...]:
    """Qt dynamicCall 인자. 튜플로 맞추고 단일 중첩 list/tuple 만 한 겹 푼다."""
    if args is None:
        return ()
    if isinstance(args, (str, bytes)):
        return (args,)
    try:
        seq = tuple(args)
    except TypeError:
        return (args,)
    if len(seq) == 1 and isinstance(seq[0], (list, tuple)) and not isinstance(seq[0], (str, bytes)):
        seq = tuple(seq[0])
    return seq


def _is_six_digit_code(value: Any) -> bool:
    s = str(value if value is not None else "").strip()
    return len(s) == 6 and s.isdigit()


def _parse_collect_codes(raw: Any) -> Optional[List[str]]:
    s = str(raw if raw is not None else "").strip()
    if not s or s.upper() == "ALL":
        return None
    parts = [p.strip() for p in s.split(";")]
    if not parts or any(not _is_six_digit_code(p) for p in parts):
        return None
    if len(parts) < 1 or len(parts) > COLLECT_MAX_CODES:
        return None
    return parts


def _parse_collect_fids(raw: Any, screen: str) -> Optional[List[int]]:
    s = str(raw if raw is not None else "").strip()
    if not s:
        return None
    allowed = COLLECT_SCREEN_FIDS.get(screen)
    if not allowed:
        return None
    out: List[int] = []
    for tok in s.split(";"):
        t = tok.strip()
        if not t:
            return None
        try:
            fid = int(t)
        except (TypeError, ValueError):
            return None
        if fid not in allowed:
            return None
        out.append(fid)
    if not out:
        return None
    return out


def _is_reg_opt_new(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value == 0
    return str(value).strip() == COLLECT_REG_OPT_NEW


def is_collect_only_allowed_method(name: Any, args: Any = (), guard: Any = None) -> bool:
    base = method_base(name)
    flat = flatten_ocx_args(args)
    if base.startswith("CommConnect"):
        return True
    if base.startswith("GetConnectState"):
        return True
    if base.startswith("GetLoginInfo"):
        return login_only_info_tag(args) in LOGIN_ONLY_GETLOGININFO_ALLOW
    if base.startswith("SetRealReg"):
        if len(flat) != 4:
            return False
        screen = str(flat[0]).strip()
        if screen not in COLLECT_SCREENS:
            return False
        if _parse_collect_codes(flat[1]) is None:
            return False
        if _parse_collect_fids(flat[2], screen) is None:
            return False
        return _is_reg_opt_new(flat[3])
    if base.startswith("SetRealRemove"):
        if len(flat) != 2:
            return False
        screen = str(flat[0]).strip()
        code = str(flat[1]).strip()
        return (screen, code) in (
            (COLLECT_SCREEN_TICK, "ALL"),
            (COLLECT_SCREEN_HOGA, "ALL"),
        )
    if base.startswith("GetCommRealData"):
        if len(flat) != 2:
            return False
        code = str(flat[0]).strip()
        if not _is_six_digit_code(code):
            return False
        if guard is None:
            return False
        active = getattr(guard, "collect_codes", None) or set()
        if code not in active:
            return False
        try:
            fid = int(flat[1])
        except (TypeError, ValueError):
            return False
        screens = getattr(guard, "collect_screen_codes", None) or {}
        allowed_fids: set = set()
        if code in (screens.get(COLLECT_SCREEN_TICK) or set()):
            allowed_fids |= set(COLLECT_TICK_FIDS)
        if code in (screens.get(COLLECT_SCREEN_HOGA) or set()):
            allowed_fids |= set(COLLECT_HOGA_FIDS)
        return fid in allowed_fids
    return False


def _clip(value: Any, limit: int = 240) -> str:
    text = repr(value)
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def _safe_args(args: Sequence[Any]) -> List[str]:
    if len(args) == 1 and isinstance(args[0], (list, tuple)):
        return [_clip(x) for x in args[0]]
    return [_clip(x) for x in args]


def _block_rec(base: str) -> Dict[str, Any]:
    """메모리 차단 기록. 인자·계좌·USER_ID 를 넣지 않는다."""
    b = str(base or "")
    return {
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        "method": b,
        "base": b,
        "blocked": True,
    }


def safe_mode_enabled(env: Optional[dict] = None) -> bool:
    src = env if env is not None else os.environ
    return str(src.get(ENV_SAFE, "") or "").strip() == "1"


def login_only_enabled(env: Optional[dict] = None) -> bool:
    src = env if env is not None else os.environ
    return str(src.get(ENV_LOGIN_ONLY, "") or "").strip() == "1"


def assert_login_only_env(env: Optional[dict] = None) -> None:
    """LOGIN_ONLY=1 이면 SAFE_MODE=1 필수. QApplication 전에 호출한다."""
    if not login_only_enabled(env):
        return
    if not safe_mode_enabled(env):
        raise SessionAbortError(
            "STAGE2C_LOGIN_ONLY=1 은 STAGE2C_SAFE_MODE=1 이 필요합니다. "
            "프로그램을 시작하지 않습니다."
        )


def collect_only_enabled(env: Optional[dict] = None) -> bool:
    src = env if env is not None else os.environ
    return str(src.get(ENV_COLLECT_ONLY, "") or "").strip() == "1"


def assert_collect_only_env(env: Optional[dict] = None) -> None:
    """COLLECT_ONLY=1 이면 SAFE_MODE=1 필수, LOGIN_ONLY 와 배타. QApplication 전에 호출한다."""
    if not collect_only_enabled(env):
        return
    if not safe_mode_enabled(env):
        raise SessionAbortError(
            "STAGE2C_COLLECT_ONLY=1 은 STAGE2C_SAFE_MODE=1 이 필요합니다. "
            "프로그램을 시작하지 않습니다."
        )
    if login_only_enabled(env):
        raise SessionAbortError(
            "STAGE2C_COLLECT_ONLY=1 과 STAGE2C_LOGIN_ONLY=1 은 동시에 사용할 수 없습니다."
        )


def _split_code_tokens(raw: str) -> List[str]:
    text = str(raw or "").replace(",", " ").replace(";", " ").replace("\r", "\n")
    parts: List[str] = []
    for line in text.split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts.extend(p.strip() for p in line.split() if p.strip())
    return parts


def parse_collect_codes(raw: str, *, source: str) -> List[str]:
    """6자리 숫자만, 중복 제거·입력 순서 유지, 최대 5. 잘못된 토큰이면 중단."""
    tokens = _split_code_tokens(raw)
    bad: List[str] = []
    out: List[str] = []
    seen = set()
    for t in tokens:
        s = str(t).strip()
        if len(s) == 6 and s.isdigit():
            if s not in seen:
                seen.add(s)
                out.append(s)
            continue
        bad.append(s)
    if bad:
        raise CollectCodesError(
            f"COLLECT_ONLY 잘못된 종목코드: {','.join(bad)} (source={source})"
        )
    if len(out) > COLLECT_MAX_CODES:
        raise CollectCodesError(
            f"COLLECT_ONLY 종목 {len(out)}개 — 최대 {COLLECT_MAX_CODES}개 (source={source})"
        )
    if not out:
        raise CollectCodesError(
            f"COLLECT_ONLY 수집 종목 목록이 비어 있습니다 (source={source})"
        )
    return out


def load_collect_codes(
    *,
    env: Optional[dict] = None,
    root: Optional[str] = None,
    codes_path: Optional[str] = None,
) -> List[str]:
    """STAGE2D_CODES 우선, 없으면 data/scalp/codes.txt. 시장 스캔·보유조회 없음."""
    src = env if env is not None else os.environ
    raw_env = str(src.get(ENV_STAGE2D_CODES, "") or "").strip()
    if raw_env:
        return parse_collect_codes(raw_env, source=ENV_STAGE2D_CODES)
    base = root if root is not None else ROOT
    path = codes_path or os.path.join(base, DEFAULT_COLLECT_CODES_REL)
    if not os.path.isfile(path):
        raise CollectCodesError(f"COLLECT_ONLY 종목 파일 없음: {path}")
    try:
        with open(path, encoding="utf-8") as fh:
            raw = fh.read()
    except OSError as e:
        raise CollectCodesError(f"COLLECT_ONLY 종목 파일 읽기 실패: {e}") from e
    return parse_collect_codes(raw, source=path)


def apply_session_env() -> Dict[str, str]:
    """검증 세션 env. LIVE 의미는 바꾸지 않고 값만 0 으로 고정한다."""
    os.environ[ENV_LIVE] = "0"
    os.environ[ENV_SCALP] = "1"
    return {ENV_LIVE: os.environ[ENV_LIVE], ENV_SCALP: os.environ[ENV_SCALP]}


def apply_safe_mode_env() -> Dict[str, str]:
    """SAFE_MODE=1 일 때 LIVE=0. LOGIN_ONLY 면 SCALP=0, COLLECT_ONLY/일반은 SCALP=1."""
    os.environ[ENV_SAFE] = "1"
    os.environ[ENV_LIVE] = "0"
    lo = login_only_enabled()
    co = collect_only_enabled()
    if lo and co:
        raise SessionAbortError(
            "STAGE2C_COLLECT_ONLY=1 과 STAGE2C_LOGIN_ONLY=1 은 동시에 사용할 수 없습니다."
        )
    if lo:
        os.environ[ENV_SCALP] = "0"
    elif co:
        os.environ[ENV_SCALP] = "1"
        os.environ[ENV_LIVE] = "0"
    else:
        os.environ[ENV_SCALP] = "1"
    return {
        ENV_SAFE: os.environ[ENV_SAFE],
        ENV_LIVE: os.environ[ENV_LIVE],
        ENV_SCALP: os.environ[ENV_SCALP],
        ENV_LOGIN_ONLY: str(os.environ.get(ENV_LOGIN_ONLY, "") or ""),
        ENV_COLLECT_ONLY: str(os.environ.get(ENV_COLLECT_ONLY, "") or ""),
    }


def _audit_allows_login_only_passthrough(guard: "SendOrderGuard") -> bool:
    """LOGIN_ONLY/COLLECT_ONLY 허용 OCX 는 감사 로그가 ready·healthy 일 때만 원본에 전달한다."""
    if not login_only_enabled() and not collect_only_enabled():
        return True
    api = getattr(guard, "_api", None)
    sess = getattr(api, ATTR_SESSION, None) if api is not None else None
    if sess is None:
        return False
    a = getattr(sess, "audit", None)
    if a is None:
        return False
    try:
        return bool(a.allow_connect())
    except Exception:
        return False


def _audit_blocked(guard: "SendOrderGuard", base: str) -> None:
    """차단 이벤트만 남긴다. 인자·계좌는 기록하지 않으며, 실패해도 차단은 유지한다."""
    try:
        from tools.stage2c_login_only_audit import audit_of, emit_safe

        api = getattr(guard, "_api", None)
        sess = getattr(api, ATTR_SESSION, None) if api is not None else None
        a = audit_of(sess)
        if a is not None and not bool(getattr(a, "block_events_enabled", False)):
            a.probe_blocked += 1
            return
        emit_safe(sess, "GUARD_BLOCKED", status="blocked", method=str(base or "")[:80])
    except Exception:
        pass


class SendOrderGuard:
    """dynamicCall 가로채기. orig 는 가드 객체에만 보관하고 api 에는 올리지 않는다."""

    def __init__(self) -> None:
        self.blocked: List[Dict[str, Any]] = []
        self.send_order_blocked = 0
        self.comm_terminate_blocked = 0
        self.passthrough = 0
        self.orig: Optional[Callable[..., Any]] = None
        self.wrapper: Optional[Callable[..., Any]] = None
        self.installed = False
        self.installed_at: str = ""
        self.method_blocked = 0
        self.method_blocked_log: List[Dict[str, Any]] = []
        self.login_only_blocked = 0
        self.login_only_blocked_log: List[Dict[str, Any]] = []
        self.orig_calls = 0
        self.getlogininfo_allowed = 0
        self.getlogininfo_blocked = 0
        self.commrqdata_blocked = 0
        self.setrealreg_blocked = 0
        self.setrealremove_blocked = 0
        self.collect_only_blocked = 0
        self.collect_only_blocked_log: List[Dict[str, Any]] = []
        self.collect_codes: set = set()
        self.collect_screen_codes = {
            COLLECT_SCREEN_TICK: set(),
            COLLECT_SCREEN_HOGA: set(),
        }
        self.getcommrealdata_blocked = 0
        self._api: Any = None
        self._probe_depth = 0
        self.orig_by_method = {
            "probe": {k: 0 for k in ORIG_OCX_METHOD_KEYS},
            "runtime": {k: 0 for k in ORIG_OCX_METHOD_KEYS},
        }
        self.orig_setrealremove_by_screen = {
            "probe": {k: 0 for k in ORIG_REMOVE_SCREENS},
            "runtime": {k: 0 for k in ORIG_REMOVE_SCREENS},
        }

    def begin_probe(self) -> None:
        self._probe_depth += 1

    def end_probe(self) -> None:
        if self._probe_depth > 0:
            self._probe_depth -= 1

    def in_probe(self) -> bool:
        return self._probe_depth > 0

    def note_orig_call(self, name: Any, args: Any = ()) -> None:
        key, screen = classify_orig_ocx_method(name, args)
        if key not in ORIG_OCX_METHOD_KEYS:
            key = "unknown"
        phase = "probe" if self.in_probe() else "runtime"
        self.orig_by_method[phase][key] = int(self.orig_by_method[phase].get(key, 0)) + 1
        if key == "SetRealRemove":
            slot = screen if screen in ("8101", "8102") else "other"
            self.orig_setrealremove_by_screen[phase][slot] = (
                int(self.orig_setrealremove_by_screen[phase].get(slot, 0)) + 1
            )

    def orig_ocx_by_method_payload(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for key in ORIG_OCX_METHOD_KEYS:
            probe = int(self.orig_by_method["probe"].get(key, 0))
            runtime = int(self.orig_by_method["runtime"].get(key, 0))
            out[key] = {"probe": probe, "runtime": runtime, "total": probe + runtime}
        return out

    def orig_setrealremove_payload(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for screen in ORIG_REMOVE_SCREENS:
            probe = int(self.orig_setrealremove_by_screen["probe"].get(screen, 0))
            runtime = int(self.orig_setrealremove_by_screen["runtime"].get(screen, 0))
            out[screen] = {"probe": probe, "runtime": runtime, "total": probe + runtime}
        return out

    def orig_methods_sum(self) -> int:
        n = 0
        for phase in ("probe", "runtime"):
            for key in ORIG_OCX_METHOD_KEYS:
                n += int(self.orig_by_method[phase].get(key, 0))
        return n

    def _count_login_only_block(self, base: str) -> None:
        self.login_only_blocked += 1
        if base.startswith("GetLoginInfo"):
            self.getlogininfo_blocked += 1
        elif base.startswith("CommRqData"):
            self.commrqdata_blocked += 1
        elif base.startswith("SetRealReg"):
            self.setrealreg_blocked += 1
        elif base.startswith("SetRealRemove"):
            self.setrealremove_blocked += 1

    def _count_collect_only_block(self, base: str) -> None:
        self.collect_only_blocked += 1
        if base.startswith("GetLoginInfo"):
            self.getlogininfo_blocked += 1
        elif base.startswith("CommRqData"):
            self.commrqdata_blocked += 1
        elif base.startswith("SetRealReg"):
            self.setrealreg_blocked += 1
        elif base.startswith("SetRealRemove"):
            self.setrealremove_blocked += 1
        elif base.startswith("GetCommRealData"):
            self.getcommrealdata_blocked += 1

    def _refresh_collect_codes(self) -> None:
        union: set = set()
        for codes in self.collect_screen_codes.values():
            union |= set(codes)
        self.collect_codes = union

    def _note_collect_reg(self, args: Any) -> None:
        flat = flatten_ocx_args(args)
        if len(flat) != 4:
            return
        screen = str(flat[0]).strip()
        codes = _parse_collect_codes(flat[1])
        if codes is None or screen not in self.collect_screen_codes:
            return
        self.collect_screen_codes[screen] = set(codes)
        self._refresh_collect_codes()

    def _note_collect_remove(self, args: Any) -> None:
        flat = flatten_ocx_args(args)
        if len(flat) != 2:
            return
        screen = str(flat[0]).strip()
        if screen in self.collect_screen_codes:
            self.collect_screen_codes[screen] = set()
        self._refresh_collect_codes()

    def dispatch(self, name: Any, args: Tuple[Any, ...], orig: Callable[..., Any]) -> Any:
        n = str(name)
        base = method_base(n)
        if is_blocked_method(n):
            rec = _block_rec(base)
            self.blocked.append(rec)
            if base.startswith("SendOrder"):
                self.send_order_blocked += 1
            else:
                self.comm_terminate_blocked += 1
            _audit_blocked(self, base)
            raise SendOrderBlockedError(n, args)
        login_only = login_only_enabled()
        collect_only = collect_only_enabled()
        if (login_only or collect_only) and not _audit_allows_login_only_passthrough(self):
            rec = _block_rec(base)
            self.blocked.append(rec)
            if login_only:
                self._count_login_only_block(base)
                self.login_only_blocked_log.append(rec)
                _audit_blocked(self, base)
                raise LoginOnlyBlockedError(n, args)
            self._count_collect_only_block(base)
            self.collect_only_blocked_log.append(rec)
            _audit_blocked(self, base)
            raise CollectOnlyBlockedError(n, args)
        if login_only and not is_login_only_allowed_method(n, args):
            rec = _block_rec(base)
            self.blocked.append(rec)
            self._count_login_only_block(base)
            self.login_only_blocked_log.append(rec)
            _audit_blocked(self, base)
            raise LoginOnlyBlockedError(n, args)
        if collect_only and not is_collect_only_allowed_method(n, args, self):
            rec = _block_rec(base)
            self.blocked.append(rec)
            self._count_collect_only_block(base)
            self.collect_only_blocked_log.append(rec)
            _audit_blocked(self, base)
            raise CollectOnlyBlockedError(n, args)
        if collect_only and base.startswith("SetRealReg"):
            self._note_collect_reg(args)
        elif collect_only and base.startswith("SetRealRemove"):
            self._note_collect_remove(args)
        if base.startswith("GetLoginInfo"):
            self.getlogininfo_allowed += 1
        self.passthrough += 1
        return orig(n, *args)

    def is_intact(self, api: Any) -> bool:
        if not self.installed or self.wrapper is None:
            return False
        if getattr(api, ATTR_GUARD, None) is not self:
            return False
        return getattr(api, "dynamicCall", None) is self.wrapper

    def snapshot(self) -> Dict[str, Any]:
        return {
            "installed": self.installed,
            "send_order_blocked": self.send_order_blocked,
            "comm_terminate_blocked": self.comm_terminate_blocked,
            "passthrough": self.passthrough,
            "blocked": list(self.blocked),
            "method_blocked": self.method_blocked,
            "method_blocked_log": list(self.method_blocked_log),
            "login_only_blocked": self.login_only_blocked,
            "login_only_blocked_log": list(self.login_only_blocked_log),
            "orig_calls": self.orig_calls,
            "getlogininfo_allowed": self.getlogininfo_allowed,
            "getlogininfo_blocked": self.getlogininfo_blocked,
            "commrqdata_blocked": self.commrqdata_blocked,
            "setrealreg_blocked": self.setrealreg_blocked,
            "setrealremove_blocked": self.setrealremove_blocked,
            "collect_only_blocked": self.collect_only_blocked,
            "collect_only_blocked_log": list(self.collect_only_blocked_log),
            "getcommrealdata_blocked": self.getcommrealdata_blocked,
            "orig_ocx_by_method": self.orig_ocx_by_method_payload(),
            "orig_ocx_setrealremove": self.orig_setrealremove_payload(),
        }


def _passthrough_counter(orig: Callable[..., Any]) -> Tuple[Callable[..., Any], Dict[str, int]]:
    hits = {"n": 0}

    def counted(name: Any, *args: Any) -> Any:
        hits["n"] += 1
        return orig(name, *args)

    return counted, hits


def install_sendorder_guard(api: Any, *, guard: Optional[SendOrderGuard] = None) -> SendOrderGuard:
    """api.dynamicCall 을 인스턴스 속성으로 덮어 주문 메서드를 차단한다.

    할당이 고정되지 않으면 GuardInstallError. 로그인 단계로 넘어가면 안 된다.
    """
    existing = getattr(api, ATTR_GUARD, None)
    if isinstance(existing, SendOrderGuard) and existing.is_intact(api):
        return existing
    if isinstance(existing, SendOrderGuard) and existing.installed and not existing.is_intact(api):
        raise GuardInstallError("기존 SendOrder 가드가 해제·우회됨 — 세션 중단")

    g = guard or SendOrderGuard()
    try:
        orig = api.dynamicCall
    except Exception as e:
        raise GuardInstallError(f"dynamicCall 조회 실패: {type(e).__name__}: {e}") from e
    if orig is None or not callable(orig):
        raise GuardInstallError("dynamicCall 이 없거나 호출할 수 없음 — 세션 중단")

    counted_orig, orig_hits = _passthrough_counter(orig)

    def counted_with_guard(name: Any, *args: Any) -> Any:
        g.orig_calls += 1
        g.note_orig_call(name, args)
        return counted_orig(name, *args)

    def wrapper(name: Any, *args: Any) -> Any:
        return g.dispatch(name, args, counted_with_guard)

    try:
        api.dynamicCall = wrapper
    except Exception as e:
        raise GuardInstallError(
            f"QAxWidget.dynamicCall 가로채기 실패: {type(e).__name__}: {e}"
        ) from e

    if getattr(api, "dynamicCall", None) is not wrapper:
        raise GuardInstallError("dynamicCall 래퍼가 인스턴스에 고정되지 않음 — 로그인 중단")

    g.orig = orig
    g.wrapper = wrapper
    g.installed = True
    g.installed_at = datetime.datetime.now().isoformat(timespec="seconds")
    g._api = api
    setattr(api, ATTR_GUARD, g)

    before = orig_hits["n"]
    probe_name = "Send" + "Order" + "(" + "QString, QString, QString, int, QString, int, int, QString, QString)"
    try:
        api.dynamicCall(
            probe_name,
            ["STAGE2C_PROBE", "0000", "X", 1, "000000", 1, 0, "03", ""],
        )
        raise GuardInstallError("SendOrder 프로브가 예외 없이 반환됨 — 로그인 중단")
    except SendOrderBlockedError:
        pass
    if orig_hits["n"] != before:
        raise GuardInstallError("SendOrder 프로브가 원본 dynamicCall 에 도달 — 로그인 중단")
    if g.send_order_blocked < 1:
        raise GuardInstallError("SendOrder 차단 카운터가 증가하지 않음 — 로그인 중단")
    return g


def assert_guard_intact(api: Any) -> SendOrderGuard:
    g = getattr(api, ATTR_GUARD, None)
    if not isinstance(g, SendOrderGuard) or not g.is_intact(api):
        raise GuardInstallError("SendOrder 가드가 없거나 해제됨 — 세션 중단")
    return g


def stop_engine_timer(engine: Any) -> None:
    """engine.timer 만 정지. GUI·스캔 등 다른 QTimer 는 건드리지 않는다."""
    timer = getattr(engine, "timer", None)
    if timer is None:
        raise SessionAbortError("TradingEngine.timer 없음 — 세션 중단")
    stop = getattr(timer, "stop", None)
    if not callable(stop):
        raise SessionAbortError("engine.timer.stop 없음 — 세션 중단")
    stop()
    is_active = getattr(timer, "isActive", None)
    if callable(is_active) and is_active():
        raise SessionAbortError("engine.timer 가 stop 이후에도 활성 — 세션 중단")
    setattr(engine, ATTR_TIMER_STOPPED, True)


def arm_engine_timer_stop(engine_cls: Any) -> None:
    """TradingEngine.start 완료 직후 engine.timer.stop() 을 강제한다."""
    if getattr(engine_cls, ATTR_TIMER_ARMED, False):
        return
    orig_start = engine_cls.start

    def start_then_stop(self: Any, *args: Any, **kwargs: Any) -> Any:
        result = orig_start(self, *args, **kwargs)
        stop_engine_timer(self)
        return result

    engine_cls.start = start_then_stop
    setattr(engine_cls, ATTR_TIMER_ARMED, True)
    setattr(engine_cls, ATTR_TIMER_ORIG, orig_start)


class Stage2CSession:
    """검증 세션 상태. abort 이후에는 정상 실행으로 복귀하지 않는다."""

    def __init__(self, api: Any, on_log: Optional[Callable[[str], None]] = None) -> None:
        self.api = api
        self.engine: Any = None
        self.window: Any = None
        self.guard: Optional[SendOrderGuard] = None
        self.on_log = on_log
        self.aborted = False
        self.abort_reason = ""
        self.watch_stop = False
        self.watch_started = False
        self.shutdown_done = False
        self.timer_start_blocked = 0
        self.mock_server_confirmed = False
        self.audit: Any = None
        self._watch_timer: Any = None
        self._timer_orig_start: Optional[Callable[..., Any]] = None

    def log(self, msg: str) -> None:
        line = f"[STAGE2C] {msg}"
        if self.on_log is not None:
            try:
                self.on_log(line)
                return
            except Exception:
                pass
        print(line, flush=True)


def _blocked_send_result(kind: str, code: str, qty: int, detail: str) -> Any:
    import order_safety as osf

    return osf.SendResult(
        False,
        osf.S_BLOCKED,
        kind=kind or "?",
        code=str(code or ""),
        qty=int(qty or 0),
        reason=REASON_METHOD_BLOCKED,
        detail=detail,
    )


def install_order_method_blocks(api: Any, sess: Stage2CSession) -> None:
    """send_order_market / cancel_order 를 인스턴스에서 차단. guarded_send_order 로 들어가지 않는다."""
    send = getattr(api, "send_order_market", None)
    cancel = getattr(api, "cancel_order", None)
    if not callable(send) or not callable(cancel):
        raise GuardInstallError("send_order_market/cancel_order 없음 — 세션 중단")

    import order_safety as osf

    send_hits = {"n": 0}
    cancel_hits = {"n": 0}

    def orig_send(*a: Any, **k: Any) -> Any:
        send_hits["n"] += 1
        return send(*a, **k)

    def orig_cancel(*a: Any, **k: Any) -> Any:
        cancel_hits["n"] += 1
        return cancel(*a, **k)

    def blocked_send(account: Any, code: Any, qty: Any, order_type: Any) -> Any:
        g = sess.guard
        rec = {
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "method": "send_order_market",
            "blocked": True,
        }
        if g is not None:
            g.method_blocked += 1
            g.method_blocked_log.append(rec)
        kind = osf._KIND_BY_ORDER_TYPE.get(int(order_type or 0), "")
        return _blocked_send_result(kind, str(code or ""), int(qty or 0), "send_order_market 차단")

    def blocked_cancel(account: Any, order: Any) -> Any:
        g = sess.guard
        code = str(getattr(order, "code", "") or "")
        qty = int(getattr(order, "open_qty", 0) or 0)
        rec = {
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "method": "cancel_order",
            "blocked": True,
        }
        if g is not None:
            g.method_blocked += 1
            g.method_blocked_log.append(rec)
        return _blocked_send_result(osf.KIND_CANCEL, code, qty, "cancel_order 차단")

    api.send_order_market = blocked_send
    api.cancel_order = blocked_cancel
    setattr(api, ATTR_SEND_WRAP, blocked_send)
    setattr(api, ATTR_CANCEL_WRAP, blocked_cancel)

    if getattr(api, "send_order_market", None) is not blocked_send:
        raise GuardInstallError("send_order_market 래퍼가 고정되지 않음 — 세션 중단")
    if getattr(api, "cancel_order", None) is not blocked_cancel:
        raise GuardInstallError("cancel_order 래퍼가 고정되지 않음 — 세션 중단")

    res = api.send_order_market("PROBE", "000000", 1, osf.ORDER_TYPE_BUY)
    if getattr(res, "accepted", True) or getattr(res, "reason", "") != REASON_METHOD_BLOCKED:
        raise GuardInstallError("send_order_market 프로브가 차단 결과를 반환하지 않음")
    if send_hits["n"] != 0:
        raise GuardInstallError("send_order_market 프로브가 원본에 도달 — 세션 중단")

    probe_order = osf.OpenOrder("0", "000000", osf.KIND_BUY, 1)
    cres = api.cancel_order("PROBE", probe_order)
    if getattr(cres, "accepted", True) or getattr(cres, "reason", "") != REASON_METHOD_BLOCKED:
        raise GuardInstallError("cancel_order 프로브가 차단 결과를 반환하지 않음")
    if cancel_hits["n"] != 0:
        raise GuardInstallError("cancel_order 프로브가 원본에 도달 — 세션 중단")


def confirm_install(api: Any, sess: Stage2CSession) -> None:
    assert_guard_intact(api)
    if getattr(api, "send_order_market", None) is not getattr(api, ATTR_SEND_WRAP, None):
        raise GuardInstallError("send_order_market 차단 래퍼 손상")
    if getattr(api, "cancel_order", None) is not getattr(api, ATTR_CANCEL_WRAP, None):
        raise GuardInstallError("cancel_order 차단 래퍼 손상")
    if not getattr(api, ATTR_ARMED, False):
        raise GuardInstallError("STAGE2C 세션이 armed 가 아님")
    if sess.aborted:
        raise SessionAbortError(sess.abort_reason or "세션 중단 상태")


def prepare_api(api: Any, on_log: Optional[Callable[[str], None]] = None) -> Stage2CSession:
    """KiwoomOpenAPI 생성 직후, login/CommConnect 전에 호출. 실패 시 예외."""
    if not safe_mode_enabled():
        raise SessionAbortError("STAGE2C_SAFE_MODE≠1 — 안전 모드 prepare 거부")
    apply_safe_mode_env()
    sess = Stage2CSession(api, on_log=on_log)
    try:
        sess.guard = install_sendorder_guard(api)
        assert_guard_intact(api)
        install_order_method_blocks(api, sess)
        setattr(api, ATTR_ARMED, True)
        setattr(api, ATTR_SESSION, sess)
        if login_only_enabled() or collect_only_enabled():
            from tools.stage2c_login_only_audit import AuditInitError, LoginOnlyAudit

            try:
                sess.audit = LoginOnlyAudit.start_session()
            except AuditInitError:
                sess.audit = None
                raise
            if sess.audit is None or not bool(sess.audit.ready) or not bool(sess.audit.healthy):
                sess.audit = None
                if login_only_enabled():
                    raise SessionAbortError("LOGIN_ONLY 감사 로그 미준비 — CommConnect 거부")
                raise SessionAbortError("COLLECT_ONLY 감사 로그 미준비 — CommConnect 거부")
        confirm_install(api, sess)
        if login_only_enabled():
            _probe_login_only_allowlist(api, sess.guard)
            sess.log(
                "LOGIN_ONLY OCX 허용목록 적용 "
                "(CommConnect/GetConnectState/GetLoginInfo:GetServerGubun)"
            )
            from tools.stage2c_login_only_audit import emit_safe

            emit_safe(sess, "GUARD_INSTALLED", status="installed")
            if sess.audit is None or not bool(sess.audit.healthy):
                raise SessionAbortError("LOGIN_ONLY 감사 로그 기록 실패 — CommConnect 거부")
            sess.audit.block_events_enabled = True
        elif collect_only_enabled():
            _probe_collect_only_allowlist(api, sess.guard)
            sess.log(
                "COLLECT_ONLY OCX 허용목록 적용 "
                "(CommConnect/GetConnectState/GetLoginInfo:GetServerGubun/"
                "SetRealReg:8101,8102/SetRealRemove:ALL/GetCommRealData)"
            )
            from tools.stage2c_login_only_audit import emit_safe

            emit_safe(sess, "GUARD_INSTALLED", status="installed")
            if sess.audit is None or not bool(sess.audit.healthy):
                raise SessionAbortError("COLLECT_ONLY 감사 로그 기록 실패 — CommConnect 거부")
            sess.audit.block_events_enabled = True
    except Exception:
        setattr(api, ATTR_ARMED, False)
        raise
    sess.log("Guard·주문메서드 차단 설치 완료 (로그인 전)")
    return sess


def _probe_login_only_allowlist(api: Any, g: SendOrderGuard) -> None:
    """LOGIN_ONLY: 허용 외 호출은 원본에 도달하면 안 된다."""
    orig_before = int(g.orig_calls)
    g.begin_probe()
    try:
        _probe_login_only_allowlist_inner(api, g, orig_before)
    finally:
        g.end_probe()


def _probe_login_only_allowlist_inner(api: Any, g: SendOrderGuard, orig_before: int) -> None:
    for probe in (
        "CommRqData(QString, QString, int, QString)",
        "SetRealReg(QString, QString, QString, QString)",
        "SetRealRemove(QString, QString)",
    ):
        try:
            api.dynamicCall(probe, ["X", "X", 0, "X"] if "CommRqData" in probe else ["X", "X"])
            raise GuardInstallError(f"LOGIN_ONLY {method_base(probe)} 프로브가 예외 없이 반환됨")
        except LoginOnlyBlockedError:
            pass
        except SendOrderBlockedError:
            pass
    for tag in ("ACCNO", "ACCLIST", "USER_ID", "ACCOUNT_CNT"):
        try:
            api.dynamicCall("GetLoginInfo(QString)", tag)
            raise GuardInstallError(f"LOGIN_ONLY GetLoginInfo({tag}) 프로브가 예외 없이 반환됨")
        except LoginOnlyBlockedError:
            pass
    if int(g.orig_calls) != orig_before:
        raise GuardInstallError("LOGIN_ONLY 차단 프로브가 원본 OCX 에 도달 — 세션 중단")
    api.dynamicCall("GetConnectState()")
    api.dynamicCall("GetLoginInfo(QString)", "GetServerGubun")
    if int(g.orig_calls) < orig_before + 2:
        raise GuardInstallError("LOGIN_ONLY GetConnectState/GetLoginInfo 가 원본에 전달되지 않음")


def _probe_collect_only_allowlist(api: Any, g: SendOrderGuard) -> None:
    """COLLECT_ONLY: 허용 외 호출은 원본에 도달하면 안 된다. 유효 SetRealReg 는 호출하지 않는다."""
    orig_before = int(g.orig_calls)
    g.begin_probe()
    try:
        _probe_collect_only_allowlist_inner(api, g, orig_before)
    finally:
        g.end_probe()


def _probe_collect_only_allowlist_inner(api: Any, g: SendOrderGuard, orig_before: int) -> None:
    probes = (
        ("CommRqData(QString, QString, int, QString)", ["X", "X", 0, "X"]),
        ("SetInputValue(QString, QString)", ["X", "X"]),
        ("GetLoginInfo(QString)", "ACCNO"),
        ("GetLoginInfo(QString)", "ACCLIST"),
        ("GetLoginInfo(QString)", "USER_ID"),
        (
            "SetRealReg(QString, QString, QString, QString)",
            ["9001", "005930", "10;13;15;20;228", "0"],
        ),
        (
            "SetRealReg(QString, QString, QString, QString)",
            ["8101", "005930", "10;13;15;20;228", "1"],
        ),
        ("SetRealRemove(QString, QString)", ["ALL", "ALL"]),
        ("SetRealRemove(QString, QString)", ["9001", "ALL"]),
        ("GetCommRealData(QString, int)", ["000660", 10]),
    )
    for probe, payload in probes:
        try:
            api.dynamicCall(probe, payload)
            raise GuardInstallError(f"COLLECT_ONLY {method_base(probe)} 프로브가 예외 없이 반환됨")
        except CollectOnlyBlockedError:
            pass
        except SendOrderBlockedError:
            pass
    if int(g.orig_calls) != orig_before:
        raise GuardInstallError("COLLECT_ONLY 차단 프로브가 원본 OCX 에 도달 — 세션 중단")
    api.dynamicCall("GetConnectState()")
    api.dynamicCall("GetLoginInfo(QString)", "GetServerGubun")
    if int(g.orig_calls) < orig_before + 2:
        raise GuardInstallError("COLLECT_ONLY GetConnectState/GetLoginInfo 가 원본에 전달되지 않음")


def confirm_login_only_mock_server(api: Any) -> str:
    """LOGIN_ONLY 전용. 프로젝트 기준 GetServerGubun=='1' 만 모의투자.

    캐시(api.server_gubun) fallback 은 쓰지 않는다. 빈값·None·예외·그 외는 실패.
    """
    try:
        raw = api.dynamicCall("GetLoginInfo(QString)", "GetServerGubun")
    except Exception as e:
        raise SessionAbortError(
            f"LOGIN_ONLY 서버 구분 확인 실패: GetLoginInfo 예외 ({type(e).__name__})"
        ) from e
    if raw is None:
        raise SessionAbortError("LOGIN_ONLY 서버 구분 확인 실패: GetServerGubun=None")
    gubun = str(raw).strip()
    if gubun == LOGIN_ONLY_MOCK_GUBUN:
        return gubun
    if not gubun:
        raise SessionAbortError("LOGIN_ONLY 서버 구분 확인 실패: GetServerGubun 빈 값")
    raise SessionAbortError(
        f"LOGIN_ONLY 모의투자 서버가 아님: GetServerGubun={gubun!r}"
    )


def assert_ready_for_connect(api: Any) -> SendOrderGuard:
    """login/CommConnect 직전. Guard 없으면 예외 — Fail Closed."""
    if not safe_mode_enabled():
        raise SessionAbortError("STAGE2C_SAFE_MODE≠1")
    if getattr(api, ATTR_ABORTED, False):
        raise SessionAbortError("STAGE2C 세션 중단 상태 — 로그인 금지")
    if not getattr(api, ATTR_ARMED, False):
        raise SessionAbortError("Guard 미설치 상태에서 CommConnect/로그인 금지")
    g = assert_guard_intact(api)
    sess = getattr(api, ATTR_SESSION, None)
    if isinstance(sess, Stage2CSession):
        confirm_install(api, sess)
    if login_only_enabled() or collect_only_enabled():
        prefix = "LOGIN_ONLY" if login_only_enabled() else "COLLECT_ONLY"
        if not isinstance(sess, Stage2CSession):
            raise SessionAbortError(f"{prefix} 세션 없음 — CommConnect 거부")
        a = getattr(sess, "audit", None)
        if a is None or not bool(getattr(a, "ready", False)) or not bool(getattr(a, "healthy", False)):
            raise SessionAbortError(f"{prefix} 감사 로그 미준비 — CommConnect 거부")
        if getattr(a, "_finished", False):
            raise SessionAbortError(f"{prefix} 세션 종료 상태 — CommConnect 거부")
    return g


def wrap_engine_start_stop_timer(engine: Any) -> None:
    """해당 엔진 인스턴스의 start() 직후 timer.stop() 만 강제. 클래스 전역 패치 아님."""
    orig_start = engine.start

    def start_then_stop(*args: Any, **kwargs: Any) -> Any:
        result = orig_start(*args, **kwargs)
        stop_engine_timer(engine)
        return result

    engine.start = start_then_stop


def confirm_timer_stopped(engine: Any) -> None:
    timer = getattr(engine, "timer", None)
    if timer is None:
        raise SessionAbortError("TradingEngine.timer 없음")
    is_active = getattr(timer, "isActive", None)
    if callable(is_active) and is_active():
        stop_engine_timer(engine)
        if is_active():
            raise SessionAbortError("engine.timer 정지 실패")
    setattr(engine, ATTR_TIMER_STOPPED, True)


def block_timer_restart(engine: Any, sess: Stage2CSession) -> None:
    """이후 engine.timer.start() 는 무시. 다른 QTimer 는 그대로."""
    timer = getattr(engine, "timer", None)
    if timer is None:
        raise SessionAbortError("TradingEngine.timer 없음")
    orig_start = timer.start
    sess._timer_orig_start = orig_start

    def guarded_start(*args: Any, **kwargs: Any) -> Any:
        if sess.aborted or getattr(engine, ATTR_TIMER_STOPPED, False):
            sess.timer_start_blocked += 1
            sess.log("engine.timer.start 거부 (HFT 검증 모드)")
            return None
        return orig_start(*args, **kwargs)

    timer.start = guarded_start


def halt_hft_collect(api: Any) -> None:
    b = getattr(api, "_scalp_bridge", None)
    if b is None:
        return
    try:
        b._accepting = False
    except Exception:
        pass


def _writer_fallback_close(api: Any, terminal: str) -> None:
    writer = getattr(api, "_scalp_writer", None)
    if writer is None or bool(getattr(writer, "_close_started", False)):
        return
    wanted = terminal if terminal in ("COMPLETE", "INCOMPLETE", "STATS_DUMP_FAILED") else "INCOMPLETE"
    writer.close(terminal=wanted)


def _close_collect_owner(sess: Stage2CSession, terminal: str) -> str:
    """Bridge 가 있으면 Writer 종료를 Bridge 가 소유한다. 중복 close 금지."""
    wanted = terminal if terminal in ("COMPLETE", "INCOMPLETE", "STATS_DUMP_FAILED") else "INCOMPLETE"
    api = getattr(sess, "api", None)
    b = getattr(api, "_scalp_bridge", None) if api is not None else None
    if b is not None:
        if not bool(getattr(b, "closed", False)):
            try:
                close = getattr(b, "close", None)
                if callable(close):
                    close(terminal=wanted)
            except TypeError:
                try:
                    b.close()
                except Exception as e:
                    sess.log(f"Bridge 종료 실패: {type(e).__name__}: {e}")
                    wanted = "INCOMPLETE"
            except Exception as e:
                sess.log(f"Bridge 종료 실패: {type(e).__name__}: {e}")
                wanted = "INCOMPLETE"
                try:
                    stats = getattr(b, "stats", None)
                    if stats is not None:
                        stats.set_shutdown("INCOMPLETE")
                except Exception:
                    pass
        stats = getattr(b, "stats", None)
        status = str(getattr(stats, "shutdown_status", "") or wanted)
        a = getattr(sess, "audit", None)
        if a is not None:
            sid = str(getattr(a, "session_id", "") or "").strip()
            st_sid = str(getattr(stats, "session_id", "") or "").strip() if stats is not None else ""
            dump_ok = getattr(stats, "stats_dump_ok", None) if stats is not None else None
            if not sid or not st_sid or sid != st_sid or dump_ok is not True:
                try:
                    if stats is not None:
                        stats.set_shutdown("STATS_DUMP_FAILED" if dump_ok is not True else "INCOMPLETE")
                        dump_now = getattr(b, "writer", None)
                        if dump_now is not None and callable(getattr(dump_now, "dump_stats_now", None)):
                            dump_now.dump_stats_now()
                except Exception:
                    pass
                return "INCOMPLETE"
        if status != "COMPLETE":
            return "INCOMPLETE"
        return "COMPLETE"
    if api is not None:
        try:
            _writer_fallback_close(api, wanted)
        except Exception as e:
            sess.log(f"Writer fallback 종료 실패: {type(e).__name__}: {e}")
            return "INCOMPLETE"
    return wanted


def abort_session(sess: Stage2CSession, reason: str) -> None:
    if sess.aborted:
        return
    sess.aborted = True
    sess.abort_reason = "SESSION_ABORTED"
    setattr(sess.api, ATTR_ABORTED, True)
    try:
        from tools.stage2c_login_only_audit import emit_safe, public_reason

        code, msg = public_reason(reason)
        sess.abort_reason = code
        sess.log(f"ERROR 검증 세션 중단: {msg}")
        emit_safe(sess, "SESSION_ABORTED", status="aborted", reason_code=code, reason=msg)
    except Exception:
        sess.abort_reason = "SESSION_ABORTED"
        sess.log("ERROR 검증 세션 중단: 세션 중단")
    try:
        if sess.engine is not None:
            stop_engine_timer(sess.engine)
    except Exception as e:
        sess.log(f"timer stop 실패: {type(e).__name__}: {e}")
    halt_hft_collect(sess.api)
    stop_watch(sess, emit=False)
    _close_collect_owner(sess, terminal="INCOMPLETE")


def on_guard_watch_tick(sess: Stage2CSession) -> None:
    if sess.aborted or sess.watch_stop:
        return
    try:
        from tools.stage2c_login_only_audit import audit_of

        a = audit_of(sess)
        if a is not None and not bool(getattr(a, "healthy", True)):
            abort_session(sess, "AUDIT_WRITE_FAILED")
            return
    except Exception:
        abort_session(sess, "AUDIT_WRITE_FAILED")
        return
    try:
        confirm_install(sess.api, sess)
        try:
            from tools.stage2c_login_only_audit import audit_of

            a = audit_of(sess)
            if a is not None:
                a.note_watch_ok(sess)
                if not bool(getattr(a, "healthy", True)):
                    abort_session(sess, "AUDIT_WRITE_FAILED")
                    return
        except Exception:
            abort_session(sess, "AUDIT_WRITE_FAILED")
            return
        eng = sess.engine
        timer = getattr(eng, "timer", None) if eng is not None else None
        is_active = getattr(timer, "isActive", None) if timer is not None else None
        if callable(is_active) and is_active():
            abort_session(sess, "engine.timer 재시작 감지")
            return
    except (GuardInstallError, SessionAbortError) as e:
        try:
            from tools.stage2c_login_only_audit import audit_of

            a = audit_of(sess)
            if a is not None:
                a.note_watch_damage(sess, str(e))
        except Exception:
            pass
        abort_session(sess, str(e))
    except Exception as e:
        abort_session(sess, f"감시 예외: {type(e).__name__}: {e}")


def bind_watch_timer(sess: Stage2CSession, timer: Any, interval_ms: int = WATCH_INTERVAL_MS) -> None:
    timer.setInterval(int(interval_ms))
    timeout = getattr(timer, "timeout", None)
    connect = getattr(timeout, "connect", None) if timeout is not None else None
    if not callable(connect):
        raise SessionAbortError("Guard 감시 QTimer.timeout.connect 없음")
    connect(lambda: on_guard_watch_tick(sess))
    sess._watch_timer = timer
    sess.watch_started = True
    start = getattr(timer, "start", None)
    if callable(start):
        start()
    try:
        from tools.stage2c_login_only_audit import emit_safe

        emit_safe(sess, "WATCH_TIMER_STARTED", status="started", interval_ms=int(interval_ms))
    except Exception:
        pass


def stop_watch(sess: Stage2CSession, *, emit: bool = True) -> None:
    sess.watch_stop = True
    t = sess._watch_timer
    if t is not None:
        stop = getattr(t, "stop", None)
        if callable(stop):
            try:
                stop()
            except Exception:
                pass
    if not emit:
        return
    try:
        from tools.stage2c_login_only_audit import audit_of

        a = audit_of(sess)
        if a is not None:
            a.note_watch_stopped(sess)
    except Exception:
        pass


def shutdown_session(sess: Stage2CSession, terminal: str = "COMPLETE") -> None:
    if sess.shutdown_done:
        return
    sess.shutdown_done = True
    sess.watch_stop = True
    try:
        from tools.stage2c_login_only_audit import emit_safe

        emit_safe(sess, "SHUTDOWN_STARTED", status="started")
    except Exception:
        pass
    try:
        if sess.engine is not None:
            confirm_timer_stopped(sess.engine)
    except Exception as e:
        sess.log(f"종료 시 timer 확인 실패: {type(e).__name__}: {e}")
        terminal = "INCOMPLETE"
    stop_watch(sess)
    halt_hft_collect(sess.api)
    actual = _close_collect_owner(sess, terminal)
    try:
        from tools.stage2c_login_only_audit import audit_of

        a = audit_of(sess)
        if a is not None:
            a.note_finished(sess, "INCOMPLETE" if sess.aborted else actual)
    except Exception:
        pass


def live_login_forbidden(argv: Optional[Sequence[str]] = None) -> bool:
    args = list(sys.argv[1:] if argv is None else argv)
    flags = {"--live", "--login", "--ocx", "--connect"}
    return any(a in flags or a.startswith("--live") for a in args)


def _run_self_test() -> int:
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    import unittest

    loader = unittest.TestLoader()
    suite = loader.loadTestsFromName("tests.test_stage2c_safe_runner")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="HFT 2단계-C 검증 전용 안전 실행기 (키움 로그인 없음)",
    )
    parser.add_argument("--self-test", action="store_true", help="가짜 객체 단위 테스트 실행")
    parser.add_argument("--live", action="store_true", help="거부됨: 실제 키움 로그인")
    parser.add_argument("--login", action="store_true", help="거부됨: 실제 키움 로그인")
    ns = parser.parse_args(list(argv) if argv is not None else None)
    if ns.live or ns.login or live_login_forbidden(argv):
        print(
            "실제 키움 로그인·OCX 연결은 별도 승인 전까지 금지입니다. "
            "이번 실행기는 가짜 객체 검증만 수행합니다.",
            flush=True,
        )
        return 2
    apply_session_env()
    if ns.self_test:
        return _run_self_test()
    print(
        "stage2c_safe_runner: 준비 전용. 키움에 접속하지 않습니다.\n"
        f"  env {ENV_SCALP}={os.environ.get(ENV_SCALP)} "
        f"{ENV_LIVE}={os.environ.get(ENV_LIVE)}\n"
        "  SendOrder* / CommTerminate 차단 래퍼와 engine.timer.stop 훅이 구현되어 있습니다.\n"
        "  검증: python -X utf8 tools\\stage2c_safe_runner.py --self-test\n"
        "  실제 모의투자 연동은 별도 승인 후입니다.",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
