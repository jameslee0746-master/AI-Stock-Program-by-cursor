"""기존 키움 제품과 HFT 수집기 사이의 독립 Bridge.

제품 OnReceiveRealData 슬롯을 disconnect 하지 않는다.
SetRealRemove 의 전체 해제(첫 인자 ALL) 는 사용하지 않는다.
수집 종목은 engine.stock_codes 순서에서 유효 6자리 앞 5개만 사용한다.
"""
from __future__ import annotations

import os
import threading
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Sequence, Tuple

from scalp.collector import HOGA_FIDS_STR, SCREEN_HOGA, SCREEN_TICK, TICK_FIDS
from scalp.collector.orderbook_collector import OrderbookCollector
from scalp.collector.realtime_buffer import RealtimeBuffer
from scalp.collector.session import CollectSession
from scalp.collector.stats import (
    BRIDGE_ATTACHING,
    BRIDGE_CLOSED,
    BRIDGE_DEGRADED,
    BRIDGE_DETACHED,
    BRIDGE_ERROR,
    BRIDGE_RUNNING,
    CollectorStats,
)
from scalp.collector.tick_collector import TickCollector
from scalp.collector.writer_service import WriterService

ENV_SCALP = "SCALP_ENABLED"
MAX_CODES = 5
MAX_LAZY_ATTACH = 1
_ATTR_BOUND = "_scalp_bridge"
_ATTR_STAGE2C_SESSION = "_stage2c_session"
_ATTR_SESSION_CLOSED = "_scalp_session_closed"
_ATTR_LAZY_USED = "_scalp_lazy_attach_attempted"
_ATTR_ATTACH_COUNT = "_scalp_attach_attempt_count"
_ATTR_ATTACH_STATUS = "_scalp_attach_status"
_ATTR_LAST_ATTACH_ERROR = "_scalp_last_attach_error"
_ATTR_ATTACHING = "_scalp_attaching"
_ATTACH_LOCK = threading.RLock()

ST_NOT_REGISTERED = "NOT_REGISTERED"
ST_REGISTERING = "REGISTERING"
ST_ACTIVE = "ACTIVE"
ST_ERROR = "ERROR"
_SCREEN_ORDER = (SCREEN_TICK, SCREEN_HOGA)
_SCREEN_FIDS = {
    SCREEN_TICK: TICK_FIDS,
    SCREEN_HOGA: HOGA_FIDS_STR,
}


def scalp_enabled(env: Optional[dict] = None) -> bool:
    src = env if env is not None else os.environ
    raw = src.get(ENV_SCALP)
    if raw is None:
        return False
    return str(raw).strip() == "1"


def select_codes(codes: Sequence[str], limit: int = MAX_CODES) -> Tuple[List[str], List[str]]:
    """유효 6자리 코드를 입력 순서대로 최대 limit 개 선정. 초과분은 excluded."""
    selected: List[str] = []
    excluded: List[str] = []
    seen = set()
    for c in codes or []:
        s = str(c).replace(".KS", "").replace(".KQ", "").strip()
        if len(s) != 6 or not s.isdigit() or s in seen:
            continue
        seen.add(s)
        if len(selected) < limit:
            selected.append(s)
        else:
            excluded.append(s)
    return selected, excluded


def _normalize_codes(codes: Sequence[str], limit: int = MAX_CODES) -> List[str]:
    selected, _excluded = select_codes(codes, limit=limit)
    return selected


def _meta_get(kiwoom: Any, attr: str, default: Any) -> Any:
    try:
        return getattr(kiwoom, attr, default)
    except Exception:
        return default


def _meta_set(kiwoom: Any, attr: str, value: Any) -> None:
    try:
        setattr(kiwoom, attr, value)
    except Exception:
        pass


def attach_meta(kiwoom: Any) -> Dict[str, Any]:
    return {
        "attach_attempt_count": int(_meta_get(kiwoom, _ATTR_ATTACH_COUNT, 0) or 0),
        "lazy_attach_attempted": bool(_meta_get(kiwoom, _ATTR_LAZY_USED, False)),
        "last_attach_error": str(_meta_get(kiwoom, _ATTR_LAST_ATTACH_ERROR, "") or ""),
        "attach_status": str(_meta_get(kiwoom, _ATTR_ATTACH_STATUS, "") or "IDLE"),
        "session_closed": bool(_meta_get(kiwoom, _ATTR_SESSION_CLOSED, False)),
    }


def _audit_from_kiwoom(kiwoom: Any) -> Any:
    sess = _meta_get(kiwoom, _ATTR_STAGE2C_SESSION, None)
    return getattr(sess, "audit", None) if sess is not None else None


def _record_attach_fail(kiwoom: Any, stats: Optional[CollectorStats], msg: str) -> None:
    _meta_set(kiwoom, _ATTR_LAST_ATTACH_ERROR, msg)
    _meta_set(kiwoom, _ATTR_ATTACH_STATUS, "ERROR")
    if stats is not None:
        stats.last_attach_error = msg
        stats.attach_status = "ERROR"
        stats.record_error("attach", msg)


def _live_bridge(kiwoom: Any) -> Optional["ScalpBridge"]:
    existing = _meta_get(kiwoom, _ATTR_BOUND, None)
    if isinstance(existing, ScalpBridge) and not existing.closed:
        return existing
    return None


def _do_attach(
    kiwoom: Any,
    codes: Sequence[str],
    directory: str,
    *,
    stats: Optional[CollectorStats] = None,
    on_log: Optional[Any] = None,
    session_id: Optional[str] = None,
    session_start: Optional[str] = None,
) -> Optional["ScalpBridge"]:
    if not scalp_enabled():
        return None
    if _meta_get(kiwoom, _ATTR_SESSION_CLOSED, False):
        return None
    existing = _live_bridge(kiwoom)
    if existing is not None:
        return existing
    with _ATTACH_LOCK:
        if _meta_get(kiwoom, _ATTR_ATTACHING, False):
            return None
        existing = _live_bridge(kiwoom)
        if existing is not None:
            return existing
        _meta_set(kiwoom, _ATTR_ATTACHING, True)
        _meta_set(kiwoom, _ATTR_ATTACH_STATUS, "ATTACHING")
        count = int(_meta_get(kiwoom, _ATTR_ATTACH_COUNT, 0) or 0) + 1
        _meta_set(kiwoom, _ATTR_ATTACH_COUNT, count)
        bridge: Optional[ScalpBridge] = None
        try:
            bridge = ScalpBridge(
                kiwoom,
                codes,
                directory,
                stats=stats,
                on_log=on_log,
                session_id=session_id,
                session_start=session_start,
            )
            bridge.stats.attach_attempt_count = count
            bridge.stats.lazy_attach_attempted = bool(_meta_get(kiwoom, _ATTR_LAZY_USED, False))
            bridge.stats.attach_status = "ATTACHING"
            bridge.stats.set_bridge_status(BRIDGE_ATTACHING)
            if not bridge.attach():
                err = bridge.last_error or "attach 실패"
                _record_attach_fail(kiwoom, bridge.stats, err)
                return None
            _meta_set(kiwoom, _ATTR_BOUND, bridge)
            _meta_set(kiwoom, _ATTR_ATTACH_STATUS, "OK")
            bridge.stats.attach_status = "OK"
            bridge.stats.attach_attempt_count = count
            bridge.stats.lazy_attach_attempted = bool(_meta_get(kiwoom, _ATTR_LAZY_USED, False))
            prev = str(_meta_get(kiwoom, _ATTR_LAST_ATTACH_ERROR, "") or "")
            if prev:
                bridge.stats.last_attach_error = prev
            return bridge
        except Exception as e:
            msg = f"attach 실패: {type(e).__name__}: {e}"
            if bridge is not None:
                bridge.rollback(msg)
                _record_attach_fail(kiwoom, bridge.stats, msg)
            else:
                _record_attach_fail(kiwoom, stats, msg)
                if on_log is not None:
                    try:
                        on_log(msg)
                    except Exception as e2:
                        print(f"[SCALP] attach 실패 로그 오류: {type(e2).__name__}: {e2}", flush=True)
                else:
                    print(f"[SCALP] {msg}", flush=True)
            return None
        finally:
            _meta_set(kiwoom, _ATTR_ATTACHING, False)


def attach_bridge(
    kiwoom: Any,
    codes: Sequence[str],
    directory: str,
    *,
    stats: Optional[CollectorStats] = None,
    on_log: Optional[Any] = None,
    session_id: Optional[str] = None,
    session_start: Optional[str] = None,
) -> Optional["ScalpBridge"]:
    """SCALP_ENABLED=1 일 때만 Bridge 를 만들고 슬롯·화면·Writer 를 시작한다."""
    return _do_attach(
        kiwoom,
        codes,
        directory,
        stats=stats,
        on_log=on_log,
        session_id=session_id,
        session_start=session_start,
    )


def ensure_bridge(
    kiwoom: Any,
    codes: Sequence[str],
    directory: str,
    *,
    stats: Optional[CollectorStats] = None,
    on_log: Optional[Any] = None,
    session_id: Optional[str] = None,
    session_start: Optional[str] = None,
) -> Optional["ScalpBridge"]:
    """첫 유효 종목 목록에서 지연 attach 1회. close 이후·중복 생성 금지."""
    if not scalp_enabled():
        return None
    selected, _excluded = select_codes(codes)
    if not selected:
        return None
    with _ATTACH_LOCK:
        if _meta_get(kiwoom, _ATTR_SESSION_CLOSED, False):
            return None
        existing = _live_bridge(kiwoom)
        if existing is not None:
            return existing
        if _meta_get(kiwoom, _ATTR_ATTACHING, False):
            return None
        if bool(_meta_get(kiwoom, _ATTR_LAZY_USED, False)):
            return None
        _meta_set(kiwoom, _ATTR_LAZY_USED, True)
        return _do_attach(
            kiwoom,
            codes,
            directory,
            stats=stats,
            on_log=on_log,
            session_id=session_id,
            session_start=session_start,
        )


class ScalpBridge:
    def __init__(
        self,
        kiwoom: Any,
        codes: Sequence[str],
        directory: str,
        *,
        stats: Optional[CollectorStats] = None,
        on_log: Optional[Any] = None,
        session_id: Optional[str] = None,
        session_start: Optional[str] = None,
    ) -> None:
        self.kiwoom = kiwoom
        self.directory = directory
        self.on_log = on_log
        self.stats = stats if stats is not None else CollectorStats()
        self._bind_session_identity(session_id, session_start)
        selected, excluded = select_codes(codes)
        self.codes = selected
        self.excluded_codes = excluded
        self.session = CollectSession()
        self.session.begin()
        self.tick_buffer = RealtimeBuffer()
        self.hoga_buffer = RealtimeBuffer()
        self.writer = WriterService(
            directory,
            tick_buffer=self.tick_buffer,
            hoga_buffer=self.hoga_buffer,
            stats=self.stats,
        )
        self.tick = TickCollector(self._fid_get, self.tick_buffer, self.stats, self.session, sink=self.writer)
        self.hoga = OrderbookCollector(self._fid_get, self.hoga_buffer, self.stats, self.session, sink=self.writer)
        self.ocx_calls: List[Tuple[Any, ...]] = []
        self.dup_diag: Deque[Tuple[Any, ...]] = deque(maxlen=32)
        self._accepting = False
        self._attached = False
        self._closed = False
        self._slot_connected = False
        self._slot = self._on_receive_real_data
        self.last_error = ""
        self.screen_state: Dict[str, str] = {
            SCREEN_TICK: ST_NOT_REGISTERED,
            SCREEN_HOGA: ST_NOT_REGISTERED,
        }
        self.screen_error: Dict[str, str] = {SCREEN_TICK: "", SCREEN_HOGA: ""}
        self.stats.set_bridge_status(BRIDGE_DETACHED)
        self._sync_screens_to_stats()

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def attached(self) -> bool:
        return self._attached

    @property
    def screens_active(self) -> bool:
        return all(self.screen_state.get(s) == ST_ACTIVE for s in _SCREEN_ORDER)

    @property
    def bridge_status(self) -> str:
        return str(self.stats.bridge_status)

    def _bind_session_identity(
        self,
        session_id: Optional[str] = None,
        session_start: Optional[str] = None,
    ) -> None:
        sid = str(session_id or "").strip()
        start = str(session_start or "").strip()
        if not sid:
            audit = _audit_from_kiwoom(self.kiwoom)
            if audit is not None:
                sid = str(getattr(audit, "session_id", "") or "").strip()
                start = str(getattr(audit, "session_start", "") or "").strip()
        if sid:
            self.stats.bind_session(sid, start)

    def _stamp_session_finished(self) -> None:
        audit = _audit_from_kiwoom(self.kiwoom)
        if audit is not None:
            self._bind_session_identity(
                getattr(audit, "session_id", ""),
                getattr(audit, "session_start", ""),
            )
            ensure = getattr(audit, "ensure_session_finished", None)
            ts = ensure() if callable(ensure) else str(getattr(audit, "session_finished", "") or "")
            self.stats.set_session_finished(ts)
            self.stats.note_shutdown_step("session_finished")
            return
        if str(self.stats.session_id or "").strip() and not str(self.stats.session_finished or "").strip():
            import datetime as _dt

            self.stats.set_session_finished(_dt.datetime.now().isoformat(timespec="microseconds"))
            self.stats.note_shutdown_step("session_finished")

    def _sync_screens_to_stats(self) -> None:
        for screen in _SCREEN_ORDER:
            self.stats.set_screen_runtime(
                screen,
                self.screen_state.get(screen, ST_NOT_REGISTERED),
                self.screen_error.get(screen) or None,
            )

    def _set_screen(self, screen: str, state: str, error: Optional[str] = None) -> None:
        self.screen_state[screen] = state
        if error:
            self.screen_error[screen] = error
        self.stats.set_screen_runtime(screen, state, error)

    def _record_error(self, kind: str, message: str, screen: str = "") -> None:
        self.last_error = message
        self.stats.record_error(kind, message, screen=screen)

    def _refresh_runtime_status(self) -> None:
        if self._closed:
            return
        if any(self.screen_state.get(s) == ST_ERROR for s in _SCREEN_ORDER):
            self.stats.set_bridge_status(BRIDGE_DEGRADED)
        elif self._attached:
            self.stats.set_bridge_status(BRIDGE_RUNNING)

    def _mark_screens_not_registered(self) -> None:
        for screen in _SCREEN_ORDER:
            self._set_screen(screen, ST_NOT_REGISTERED)

    def _screens_needing_register(self) -> List[str]:
        return [
            s
            for s in _SCREEN_ORDER
            if self.screen_state.get(s) in (ST_NOT_REGISTERED, ST_ERROR)
        ]

    def _register_one_screen(self, screen: str) -> None:
        self._set_screen(screen, ST_REGISTERING)
        try:
            self._ocx(
                "SetRealReg(QString, QString, QString, QString)",
                screen,
                ";".join(self.codes),
                _SCREEN_FIDS[screen],
                "0",
            )
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            self._set_screen(screen, ST_ERROR, err)
            self._record_error("screen_reg", err, screen=screen)
            self._note(f"화면 등록 실패 screen={screen}: {err}")
            raise
        self._set_screen(screen, ST_ACTIVE)

    def _fid_get(self, code: str, fid: int) -> str:
        dc = getattr(self.kiwoom, "dynamicCall", None)
        if dc is None:
            return ""
        return str(dc("GetCommRealData(QString, int)", code, int(fid)) or "").strip()

    def _ocx(self, name: str, *args: Any) -> Any:
        self.ocx_calls.append((name,) + args)
        dc = getattr(self.kiwoom, "dynamicCall", None)
        if dc is None:
            return None
        return dc(name, *args)

    def _note(self, msg: str) -> None:
        line = str(msg)
        if self.on_log is not None:
            try:
                self.on_log(line)
                return
            except Exception as e:
                print(f"[SCALP] 로그 콜백 실패: {type(e).__name__}: {e} | {line}", flush=True)
                return
        print(f"[SCALP] {line}", flush=True)

    def rollback(self, reason: str) -> None:
        """부분 초기화 해제. 제품 슬롯·9001/9051 은 건드리지 않는다. 세션 close 가 아니다."""
        self.last_error = reason
        self._record_error("rollback", reason)
        self._accepting = False
        self.stats.set_bridge_status(BRIDGE_ERROR)
        self._note(reason)
        sig = getattr(self.kiwoom, "OnReceiveRealData", None)
        if self._slot_connected and sig is not None:
            disconnect = getattr(sig, "disconnect", None)
            if disconnect is not None:
                try:
                    disconnect(self._slot)
                except Exception as e:
                    self._note(f"수집 슬롯 분리 실패: {type(e).__name__}: {e}")
        self._slot_connected = False
        self._attached = False
        try:
            self.unregister_hft_screens()
        except Exception as e:
            self._note(f"8101/8102 정리 실패: {type(e).__name__}: {e}")
        try:
            if not self.writer._close_started:
                self.writer.close(terminal="INCOMPLETE", dump_stats=True)
        except Exception as e:
            self._note(f"Writer 종료 실패: {type(e).__name__}: {e}")
            try:
                self.stats.set_shutdown("INCOMPLETE")
            except Exception as e2:
                self._note(f"INCOMPLETE 기록 실패: {type(e2).__name__}: {e2}")
        self.stats.set_bridge_status(BRIDGE_ERROR)
        try:
            if getattr(self.kiwoom, _ATTR_BOUND, None) is self:
                setattr(self.kiwoom, _ATTR_BOUND, None)
        except Exception as e:
            self._note(f"브리지 바인딩 해제 실패: {type(e).__name__}: {e}")
        self._closed = True

    def attach(self) -> bool:
        if self._closed or self._attached:
            return False
        if not scalp_enabled():
            return False
        sig = getattr(self.kiwoom, "OnReceiveRealData", None)
        connect = getattr(sig, "connect", None) if sig is not None else None
        if connect is None:
            self.rollback("attach 실패: OnReceiveRealData.connect 없음")
            return False
        try:
            self.stats.set_bridge_status(BRIDGE_ATTACHING)
            connect(self._slot)
            self._slot_connected = True
            self._accepting = True
            self.writer.start()
            self.register_hft_screens()
            self._attached = True
            self._refresh_runtime_status()
            self._note(
                "수집 종목 선정 기준=입력 순서(스캔 다음 보유)의 유효 6자리 앞 "
                f"{MAX_CODES}개 selected={self.codes} excluded={self.excluded_codes}"
            )
            return True
        except Exception as e:
            self.rollback(f"attach 실패: {type(e).__name__}: {e}")
            return False

    def _on_receive_real_data(self, code: str, real_type: str, real_data: str = "") -> None:
        if not self._accepting:
            return
        try:
            rt = str(real_type or "").strip()
            if rt == "주식체결":
                rec = self.tick.handle(code, rt, real_data)
                if rec is not None:
                    self.dup_diag.append(
                        (rec.get("seq"), rec.get("code"), rec.get("fid20_raw"), rec.get("fid10_raw"), rec.get("fid15_raw"))
                    )
            elif rt == "주식호가잔량":
                self.hoga.handle(code, rt, real_data)
        except Exception as e:
            self.stats.incr("handler_exceptions")
            self._note(f"수집 콜백 예외: {type(e).__name__}: {e}")

    def register_hft_screens(self, screens: Optional[Sequence[str]] = None) -> None:
        if not self.codes:
            return
        if screens is None:
            targets = list(_SCREEN_ORDER)
        else:
            wanted = set(screens)
            targets = [s for s in _SCREEN_ORDER if s in wanted]
        failed: List[str] = []
        first_exc: Optional[BaseException] = None
        for screen in targets:
            try:
                self._register_one_screen(screen)
            except Exception as e:
                failed.append(screen)
                if first_exc is None:
                    first_exc = e
        self._refresh_runtime_status()
        if failed:
            raise RuntimeError(
                "HFT 화면 등록 실패: "
                + "; ".join(f"{s}={self.screen_error.get(s, '')}" for s in failed)
            ) from first_exc

    def unregister_hft_screens(self) -> None:
        failed: List[str] = []
        first_exc: Optional[BaseException] = None
        for screen in _SCREEN_ORDER:
            try:
                self._ocx("SetRealRemove(QString, QString)", screen, "ALL")
                self._set_screen(screen, ST_NOT_REGISTERED)
                if self._closed:
                    self.stats.note_shutdown_step("unregister_%s" % screen)
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                self._set_screen(screen, ST_ERROR, err)
                self._record_error("screen_unreg", err, screen=screen)
                failed.append(screen)
                if first_exc is None:
                    first_exc = e
                self._note(f"화면 해제 실패 screen={screen}: {err}")
                if self._closed:
                    self.stats.note_shutdown_step("unregister_%s_failed" % screen)
        if failed:
            raise RuntimeError(
                "HFT 화면 해제 실패: "
                + "; ".join(f"{s}={self.screen_error.get(s, '')}" for s in failed)
            ) from first_exc

    def reregister_hft_screens(self) -> None:
        """제품 reregister_real (ALL,ALL) 이후 호출. 8101/8102 만 다시 등록한다."""
        if self._closed or not self._accepting:
            return
        self._mark_screens_not_registered()
        try:
            self.register_hft_screens()
        except Exception as e:
            msg = f"제품 재등록 후 HFT 재등록 실패: {type(e).__name__}: {e}"
            self._record_error("reregister", msg)
            self._refresh_runtime_status()
            self._note(msg)
            raise

    def sync_targets(self, codes: Sequence[str]) -> None:
        """update_targets 이후 수집 종목만 맞춘다. 제품 화면은 건드리지 않는다."""
        if self._closed or not self._accepting:
            return
        new_codes, excluded = select_codes(codes)
        codes_changed = new_codes != self.codes
        need = self._screens_needing_register()
        if not codes_changed and not need:
            self.excluded_codes = excluded
            return
        try:
            if codes_changed:
                self.unregister_hft_screens()
                self.codes = new_codes
                self.excluded_codes = excluded
                self.register_hft_screens()
            else:
                self.excluded_codes = excluded
                self.register_hft_screens(screens=need)
            self._refresh_runtime_status()
            self._note(
                f"종목 동기화 selected={self.codes} excluded={self.excluded_codes} "
                f"screens={dict(self.screen_state)} bridge={self.stats.bridge_status}"
            )
        except Exception as e:
            msg = f"종목 동기화 실패: {type(e).__name__}: {e}"
            self._record_error("sync", msg)
            self._refresh_runtime_status()
            self._note(msg)
            raise

    def _copy_guard_ocx_into_stats(self) -> None:
        g = getattr(self.kiwoom, "_stage2c_guard", None)
        if g is None:
            return
        by_method = getattr(g, "orig_ocx_by_method_payload", None)
        setrealremove = getattr(g, "orig_setrealremove_payload", None)
        try:
            self.stats.set_orig_ocx(
                orig_calls=int(getattr(g, "orig_calls", 0) or 0),
                by_method=by_method() if callable(by_method) else None,
                setrealremove=setrealremove() if callable(setrealremove) else None,
            )
        except Exception as e:
            self._note(f"OCX 카운터 복사 실패: {type(e).__name__}: {e}")

    def _final_stats_dump(self) -> None:
        self.stats.note_shutdown_step("stats_dump")
        try:
            self.writer.dump_stats_now()
        except Exception as e:
            msg = f"최종 통계 저장 실패: {type(e).__name__}: {e}"
            self._record_error("stats_dump", msg)
            self._note(msg)
            self.stats.mark_dump_failed()

    def close(self, terminal: str = "COMPLETE", timeout: Optional[float] = None) -> None:
        if self._closed:
            return
        self._closed = True
        self._accepting = False
        self.stats.note_shutdown_step("halt_input")
        _meta_set(self.kiwoom, _ATTR_SESSION_CLOSED, True)
        _meta_set(self.kiwoom, _ATTR_ATTACH_STATUS, "CLOSED")
        sig = getattr(self.kiwoom, "OnReceiveRealData", None)
        if self._slot_connected and sig is not None:
            disconnect = getattr(sig, "disconnect", None)
            if disconnect is not None:
                try:
                    disconnect(self._slot)
                except Exception as e:
                    self._note(f"수집 슬롯 분리 실패: {type(e).__name__}: {e}")
        self._slot_connected = False
        wanted = str(terminal or "COMPLETE")
        if wanted not in ("COMPLETE", "INCOMPLETE", "STATS_DUMP_FAILED"):
            wanted = "INCOMPLETE"
        try:
            self.unregister_hft_screens()
        except Exception as e:
            msg = f"8101/8102 해제 실패: {type(e).__name__}: {e}"
            self._record_error("close_unreg", msg)
            self._note(msg)
            self.stats.incr("handler_exceptions")
            wanted = "INCOMPLETE"
        try:
            if not self.writer._close_started:
                self.writer.close(terminal=wanted, dump_stats=False, timeout=timeout)
            elif wanted != "COMPLETE":
                self.stats.set_shutdown("INCOMPLETE")
            self.stats.note_shutdown_step("writer_close")
        except Exception as e:
            self.stats.incr("handler_exceptions")
            msg = f"Writer 종료 실패: {type(e).__name__}: {e}"
            self._record_error("close_writer", msg)
            self._note(msg)
            wanted = "INCOMPLETE"
            try:
                self.stats.set_shutdown("INCOMPLETE")
            except Exception as e2:
                self._note(f"INCOMPLETE 기록 실패: {type(e2).__name__}: {e2}")
            self.stats.note_shutdown_step("writer_close_failed")
        self.stats.set_bridge_status(BRIDGE_CLOSED)
        self.stats.attach_status = "CLOSED"
        self.stats.note_shutdown_step("bridge_closed")
        if self.stats.shutdown_status == "RUNNING":
            self.stats.set_shutdown("COMPLETE" if wanted == "COMPLETE" else "INCOMPLETE")
        elif wanted != "COMPLETE" and self.stats.shutdown_status == "COMPLETE":
            self.stats.set_shutdown("INCOMPLETE")
        self._copy_guard_ocx_into_stats()
        self._stamp_session_finished()
        audit = _audit_from_kiwoom(self.kiwoom)
        if audit is not None:
            a_sid = str(getattr(audit, "session_id", "") or "").strip()
            s_sid = str(self.stats.session_id or "").strip()
            if not a_sid or not s_sid or a_sid != s_sid:
                self.stats.set_shutdown("INCOMPLETE")
        self._final_stats_dump()
        try:
            bound = getattr(self.kiwoom, _ATTR_BOUND, None)
            if bound is self:
                setattr(self.kiwoom, _ATTR_BOUND, None)
        except Exception as e:
            self._note(f"브리지 바인딩 해제 실패: {type(e).__name__}: {e}")
        try:
            self._note(
                f"종료 shutdown={self.stats.shutdown_status} bridge={self.stats.bridge_status}"
            )
        except Exception as e:
            print(f"[SCALP] 종료 상태 로그 실패: {type(e).__name__}: {e}", flush=True)
