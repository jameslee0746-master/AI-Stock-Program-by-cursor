"""LOGIN_ONLY 실행 감사 로그. 계좌·USER_ID 등 개인정보를 기록하지 않는다.

실제 키움 로그인·OCX 호출은 이 모듈이 수행하지 않는다.
초기화 실패는 예외로 올려 CommConnect 를 막는다.
런타임 기록 실패는 Guard 를 우회하지 않고 healthy=False 로 남긴다.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_AUDIT_DIR = "STAGE2C_AUDIT_DIR"
ENV_HEARTBEAT_SEC = "STAGE2C_AUDIT_HEARTBEAT_SEC"
HEARTBEAT_SEC_DEFAULT = 10.0

_DIGIT_RUN = re.compile(r"\d{8,}")
_HYPHEN_ACCT = re.compile(r"(?<!\d)\d{3}-\d{2,4}-\d{3,5}(?!\d)")
_SENSITIVE_KV = re.compile(
    r"(?i)\b(USER_ID|ACCNO|ACCLIST|ACCOUNT_CNT|PASSWORD|PASSWD|"
    r"계좌비번|계좌비밀번호|비밀번호)\s*[=:]\s*\S+"
)
_SENSITIVE_TOKEN = re.compile(
    r"(?i)\b(USER_ID|ACCNO|ACCLIST|ACCOUNT_CNT|PASSWORD|PASSWD|"
    r"계좌비번|계좌비밀번호|비밀번호|계좌번호)\b"
)

EVENTS = (
    "SESSION_START",
    "GUARD_INSTALLED",
    "LOGIN_SUCCEEDED",
    "MOCK_SERVER_CONFIRMED",
    "WATCH_TIMER_STARTED",
    "WATCH_HEARTBEAT",
    "GUARD_BLOCKED",
    "SESSION_ABORTED",
    "SHUTDOWN_STARTED",
    "WATCH_TIMER_STOPPED",
    "SESSION_FINISHED",
    "SESSION_INCOMPLETE",
)
TERMINAL_EVENTS = ("SESSION_FINISHED", "SESSION_INCOMPLETE")
REASON_CODES = {
    "AUDIT_INIT_FAILED": "감사 로그 초기화 실패",
    "AUDIT_WRITE_FAILED": "감사 로그 기록 실패",
    "GUARD_DAMAGED": "Guard 훼손 감지",
    "MOCK_SERVER": "모의투자 서버 확인 실패",
    "SESSION_ABORTED": "세션 중단",
}


class AuditInitError(RuntimeError):
    """LOGIN_ONLY 감사 로그를 준비하지 못함. CommConnect 금지."""


def _iso() -> str:
    return datetime.datetime.now().isoformat(timespec="microseconds")


def new_session_id() -> str:
    """세션 ID 는 여기서만 생성한다. stats/Bridge 는 이 값을 전달받는다."""
    return "s2c-" + uuid.uuid4().hex[:12]


def sanitize_reason(text: Any) -> str:
    raw = str(text or "")
    raw = _SENSITIVE_KV.sub("#", raw)
    raw = _HYPHEN_ACCT.sub("#", raw)
    raw = _DIGIT_RUN.sub("#", raw)
    raw = _SENSITIVE_TOKEN.sub("#", raw)
    return raw[:240]


def still_sensitive(text: Any) -> bool:
    s = str(text or "")
    if _DIGIT_RUN.search(s) or _HYPHEN_ACCT.search(s):
        return True
    if _SENSITIVE_TOKEN.search(s) or _SENSITIVE_KV.search(s):
        return True
    return False


def public_reason(reason: Any, default_code: str = "SESSION_ABORTED") -> Tuple[str, str]:
    """오류 코드 + 일반화된 메시지만 반환. 원문·PII 없음."""
    raw = str(reason or "")
    code = default_code
    up = raw.upper()
    if "AUDIT_INIT" in up or "초기화" in raw:
        code = "AUDIT_INIT_FAILED"
    elif "AUDIT_WRITE" in up or "기록 실패" in raw or "WRITE_FAIL" in up:
        code = "AUDIT_WRITE_FAILED"
    elif "GUARD" in up or "가드" in raw:
        code = "GUARD_DAMAGED"
    elif (
        "모의" in raw
        or "서버 구분" in raw
        or "MOCK_SERVER" in up
        or "GETSERVERGUBUN" in up
        or "SERVER_GUBUN" in up
    ):
        code = "MOCK_SERVER"
    msg = REASON_CODES.get(code, REASON_CODES["SESSION_ABORTED"])
    cleaned = sanitize_reason(raw)
    if cleaned and not still_sensitive(cleaned) and cleaned not in raw:
        # sanitized text that no longer contains the original secrets is unused;
        # always prefer the generic code message.
        pass
    return code, msg


def heartbeat_sec_from_env() -> float:
    raw = str(os.environ.get(ENV_HEARTBEAT_SEC, "") or "").strip()
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
    return HEARTBEAT_SEC_DEFAULT


def default_audit_dir() -> str:
    override = str(os.environ.get(ENV_AUDIT_DIR, "") or "").strip()
    if override:
        return override
    return os.path.join(ROOT, "logs")


def _channel() -> str:
    if str(os.environ.get(ENV_AUDIT_DIR, "") or "").strip():
        return "fake_test"
    return "runtime"


def _temp_dir() -> Optional[str]:
    raw = str(os.environ.get("TEMP") or os.environ.get("TMP") or "").strip()
    return raw or None


class LoginOnlyAudit:
    """세션별 jsonl. 250ms 틱마다 파일을 쓰지 않고 heartbeat_sec 간격으로만 누적 기록한다."""

    def __init__(
        self,
        path: str,
        session_id: Optional[str] = None,
        *,
        heartbeat_sec: float = HEARTBEAT_SEC_DEFAULT,
        channel: Optional[str] = None,
    ) -> None:
        self.path = path
        self.session_id = str(session_id or "").strip()
        self.session_start = ""
        self.session_finished = ""
        self.heartbeat_sec = float(heartbeat_sec)
        self.channel = channel or _channel()
        self.write_failures = 0
        self.ready = False
        self.healthy = True
        self.fallback = False
        self.block_events_enabled = False
        self.probe_blocked = 0
        self.watch_ticks = 0
        self.watch_ok = 0
        self.watch_damage = 0
        self.last_ok_at: Optional[str] = None
        self._last_hb_mono = time.monotonic()
        self._watch_stop_emitted = False
        self._finished = False
        self.last_error_code = ""

    @classmethod
    def start_session(cls, *, audit_dir: Optional[str] = None) -> "LoginOnlyAudit":
        channel = _channel()
        primary = audit_dir or default_audit_dir()
        candidates = [primary]
        # runtime only: TEMP fallback. fake_test (STAGE2C_AUDIT_DIR) never mixes into TEMP.
        if channel == "runtime":
            tmp = _temp_dir()
            if tmp and os.path.normcase(os.path.abspath(tmp)) != os.path.normcase(os.path.abspath(primary)):
                candidates.append(tmp)
        last_err = "unknown"
        sid = new_session_id()
        start_ts = _iso()
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        for idx, folder in enumerate(candidates):
            try:
                os.makedirs(folder, exist_ok=True)
                path = os.path.join(folder, f"stage2c_login_only_{ts}_{sid[4:]}.jsonl")
                audit = cls(
                    path,
                    session_id=sid,
                    heartbeat_sec=heartbeat_sec_from_env(),
                    channel=channel,
                )
                audit.session_start = start_ts
                extra: Dict[str, Any] = {"status": "start", "path": path, "ts": start_ts}
                if idx > 0:
                    extra["fallback"] = True
                    extra["primary"] = primary
                    audit.fallback = True
                if not audit._emit_must("SESSION_START", **extra):
                    last_err = "SESSION_START write/flush failed"
                    continue
                if not _session_start_on_disk(path):
                    last_err = "SESSION_START not found on disk"
                    continue
                audit.ready = True
                audit.healthy = True
                if audit.fallback:
                    print(
                        f"[STAGE2C] 감사 로그 TEMP fallback 사용. 실제 경로={path}",
                        flush=True,
                    )
                return audit
            except Exception as e:
                last_err = f"{type(e).__name__}"
                continue
        raise AuditInitError(
            "LOGIN_ONLY 감사 로그 초기화 실패 — CommConnect 거부"
        ) from None

    def counters_from(self, guard: Any) -> Dict[str, int]:
        if guard is None:
            base = {
                "orig_ocx_calls": 0,
                "guard_blocked_calls": 0,
                "sendorder_blocked": 0,
                "commterminate_blocked": 0,
                "commrqdata_blocked": 0,
                "setrealreg_blocked": 0,
                "setrealremove_blocked": 0,
                "getlogininfo_allowed": 0,
                "getlogininfo_blocked": 0,
                "orig_ocx_by_method": {},
                "orig_ocx_setrealremove": {},
            }
        else:
            send_b = int(getattr(guard, "send_order_blocked", 0) or 0)
            term_b = int(getattr(guard, "comm_terminate_blocked", 0) or 0)
            lo_b = int(getattr(guard, "login_only_blocked", 0) or 0)
            by_method = getattr(guard, "orig_ocx_by_method_payload", None)
            setrealremove = getattr(guard, "orig_setrealremove_payload", None)
            base = {
                "orig_ocx_calls": int(getattr(guard, "orig_calls", 0) or 0),
                "guard_blocked_calls": send_b + term_b + lo_b,
                "sendorder_blocked": send_b,
                "commterminate_blocked": term_b,
                "commrqdata_blocked": int(getattr(guard, "commrqdata_blocked", 0) or 0),
                "setrealreg_blocked": int(getattr(guard, "setrealreg_blocked", 0) or 0),
                "setrealremove_blocked": int(getattr(guard, "setrealremove_blocked", 0) or 0),
                "getlogininfo_allowed": int(getattr(guard, "getlogininfo_allowed", 0) or 0),
                "getlogininfo_blocked": int(getattr(guard, "getlogininfo_blocked", 0) or 0),
                "orig_ocx_by_method": by_method() if callable(by_method) else {},
                "orig_ocx_setrealremove": setrealremove() if callable(setrealremove) else {},
            }
        base["watch_ticks"] = int(self.watch_ticks)
        base["watch_ok"] = int(self.watch_ok)
        base["watch_damage"] = int(self.watch_damage)
        base["probe_blocked"] = int(self.probe_blocked)
        base["write_failures"] = int(self.write_failures)
        return base

    def _line(self, event: str, **fields: Any) -> str:
        rec: Dict[str, Any] = {
            "ts": _iso(),
            "session_id": self.session_id,
            "event": str(event),
            "channel": self.channel,
            "ready": bool(self.ready),
            "healthy": bool(self.healthy),
        }
        for k, v in fields.items():
            if v is None:
                continue
            rec[k] = v
        return json.dumps(rec, ensure_ascii=False, separators=(",", ":"))

    def _write_line(self, line: str) -> bool:
        try:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
                try:
                    os.fsync(fh.fileno())
                except Exception:
                    pass
            return True
        except Exception:
            self.write_failures += 1
            self.healthy = False
            self.last_error_code = "AUDIT_WRITE_FAILED"
            return False

    def _emit_must(self, event: str, **fields: Any) -> bool:
        return self._write_line(self._line(event, **fields))

    def emit(self, event: str, **fields: Any) -> None:
        if self._finished:
            return
        if not self._write_line(self._line(event, **fields)):
            self.healthy = False

    def emit_guard(self, event: str, sess: Any, **extra: Any) -> None:
        if self._finished:
            return
        guard = getattr(sess, "guard", None) if sess is not None else None
        api = getattr(sess, "api", None) if sess is not None else None
        intact = False
        try:
            intact = bool(guard is not None and api is not None and guard.is_intact(api))
        except Exception:
            intact = False
        payload: Dict[str, Any] = {
            "status": extra.pop("status", "ok"),
            "counters": self.counters_from(guard),
            "engine": 1 if getattr(sess, "engine", None) is not None else 0,
            "bridge": 1 if getattr(api, "_scalp_bridge", None) is not None else 0,
            "guard_intact": intact,
            "watch_stopped": bool(getattr(sess, "watch_stop", False)) if sess is not None else False,
        }
        if self.last_ok_at:
            payload["last_ok_at"] = self.last_ok_at
        payload.update(extra)
        self.emit(event, **payload)

    def mark_unhealthy(self, code: str = "AUDIT_WRITE_FAILED") -> None:
        self.healthy = False
        self.last_error_code = str(code or "AUDIT_WRITE_FAILED")
        self.write_failures = max(int(self.write_failures), 1)

    def note_watch_ok(self, sess: Any) -> None:
        self.watch_ticks += 1
        self.watch_ok += 1
        self.last_ok_at = _iso()
        now = time.monotonic()
        if (now - self._last_hb_mono) >= self.heartbeat_sec:
            self._last_hb_mono = now
            self.emit_guard("WATCH_HEARTBEAT", sess, status="ok")

    def note_watch_damage(self, sess: Any, reason: str = "") -> None:
        self.watch_ticks += 1
        self.watch_damage += 1
        code, msg = public_reason(reason, "GUARD_DAMAGED")
        self.emit_guard("WATCH_HEARTBEAT", sess, status="damage", reason_code=code, reason=msg)

    def note_watch_stopped(self, sess: Any) -> None:
        if self._watch_stop_emitted or self._finished:
            return
        if sess is not None and not bool(getattr(sess, "watch_started", False)):
            return
        self._watch_stop_emitted = True
        self.emit_guard("WATCH_TIMER_STOPPED", sess, status="stopped")

    def ensure_session_finished(self) -> str:
        if not str(self.session_finished or "").strip():
            self.session_finished = _iso()
        return self.session_finished

    def note_finished(self, sess: Any, terminal: str) -> None:
        if self._finished:
            return
        aborted = bool(sess is not None and getattr(sess, "aborted", False))
        term = str(terminal or "COMPLETE")
        sid = str(self.session_id or "").strip()
        stats = _stats_of(sess)
        stats_sid = str(getattr(stats, "session_id", "") or "").strip() if stats is not None else ""
        dump_failed = bool(
            stats is not None
            and (
                getattr(stats, "stats_dump_ok", None) is not True
                or str(getattr(stats, "shutdown_status", "") or "") == "STATS_DUMP_FAILED"
            )
        )
        mismatch = stats is not None and (not sid or not stats_sid or stats_sid != sid)
        if aborted or not self.healthy or term != "COMPLETE" or not sid or mismatch or dump_failed:
            event = "SESSION_INCOMPLETE"
            status = "INCOMPLETE"
        else:
            event = "SESSION_FINISHED"
            status = "COMPLETE"
        ts = self.ensure_session_finished()
        self.emit_guard(event, sess, status=status, ts=ts)
        self._finished = True

    def allow_connect(self) -> bool:
        return bool(self.ready and self.healthy and not self._finished)


def _session_start_on_disk(path: str) -> bool:
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                return rec.get("event") == "SESSION_START"
    except Exception:
        return False
    return False


def audit_of(sess: Any) -> Optional[LoginOnlyAudit]:
    if sess is None:
        return None
    a = getattr(sess, "audit", None)
    return a if isinstance(a, LoginOnlyAudit) else None


def _stats_of(sess: Any) -> Any:
    if sess is None:
        return None
    api = getattr(sess, "api", None)
    if api is None:
        return None
    br = getattr(api, "_scalp_bridge", None)
    return getattr(br, "stats", None) if br is not None else None


def emit_safe(sess: Any, event: str, **extra: Any) -> None:
    a = audit_of(sess)
    if a is None or a._finished:
        return
    try:
        a.emit_guard(event, sess, **extra)
    except Exception:
        try:
            a.mark_unhealthy("AUDIT_WRITE_FAILED")
        except Exception:
            pass


def load_events(path: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            out.append(json.loads(line))
    return out
