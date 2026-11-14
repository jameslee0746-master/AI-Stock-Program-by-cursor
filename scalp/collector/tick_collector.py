"""주식체결 수집. 콜백에서는 FID 복사와 버퍼 push 만 한다."""
from __future__ import annotations

from collections import deque
from typing import Any, Callable, Deque, Dict, Optional, Tuple

from scalp.collector import FID_ACCUM_VOL, FID_PRICE, FID_STRENGTH, FID_TIME, FID_TRADE_VOL
from scalp.collector.clock import clock_offset_sec, parse_hhmmss, recv_ns, stamp
from scalp.collector.realtime_buffer import RealtimeBuffer
from scalp.collector.session import CollectSession
from scalp.collector.stats import CollectorStats

FidGet = Callable[[str, int], str]
TICK_FIELDS = (
    "recv_ns", "recv_iso", "code", "real_type", "epoch", "phase",
    "fid10_raw", "fid10", "fid10_ok",
    "fid13_raw", "fid13", "fid13_ok",
    "fid15_raw", "fid15", "fid15_ok",
    "fid20_raw", "fid20_sod", "fid20_ok",
    "fid228_raw", "fid228", "fid228_ok",
    "clock_offset_sec", "seq",
)


def _parse_int(raw: str) -> Optional[int]:
    t = str(raw or "").strip().replace(",", "").replace("+", "")
    if t.startswith("-"):
        body = t[1:]
        if body.isdigit():
            return -int(body)
        return None
    if t.isdigit():
        return int(t)
    try:
        return int(float(t))
    except (TypeError, ValueError):
        return None


def _parse_float(raw: str) -> Optional[float]:
    t = str(raw or "").strip().replace(",", "").replace("+", "")
    if not t:
        return None
    try:
        return float(t)
    except (TypeError, ValueError):
        return None


def fid10_ok(raw: str, parsed: Optional[int]) -> bool:
    t = str(raw or "").strip()
    return parsed is not None and t not in ("", "0")


class TickCollector:
    def __init__(
        self,
        fid_get: FidGet,
        buffer: RealtimeBuffer,
        stats: CollectorStats,
        session: CollectSession,
        *,
        dup_window: int = 256,
        sink: Any = None,
    ) -> None:
        self.fid_get = fid_get
        self.buffer = buffer
        self.stats = stats
        self.session = session
        self.sink = sink
        self._recent: Deque[Tuple[Any, ...]] = deque(maxlen=max(8, int(dup_window)))

    def handle(self, code: str, real_type: str, _real_data: str = "") -> Optional[Dict[str, Any]]:
        t0 = recv_ns()
        entered = False
        try:
            if str(real_type or "").strip() != "주식체결":
                return None
            self.stats.incr("callback_total")
            if self.sink is not None:
                if not self.sink.begin_handle():
                    self.stats.incr("rejected_after_close")
                    return None
                entered = True
            return self._handle(code, "주식체결")
        except Exception:
            self.stats.incr("handler_exceptions")
            return None
        finally:
            if entered and self.sink is not None:
                self.sink.end_handle()
            self.stats.note_handle_ns(recv_ns() - t0)

    def _handle(self, code: str, real_type: str) -> Optional[Dict[str, Any]]:
        rt = str(real_type or "").strip()
        if rt != "주식체결":
            return None
        code = str(code or "").strip()
        recv_ns, recv_iso, local_sod = stamp()
        raw10 = str(self.fid_get(code, FID_PRICE) or "").strip()
        raw13 = str(self.fid_get(code, FID_ACCUM_VOL) or "").strip()
        raw15 = str(self.fid_get(code, FID_TRADE_VOL) or "").strip()
        raw20 = str(self.fid_get(code, FID_TIME) or "").strip()
        raw228 = str(self.fid_get(code, FID_STRENGTH) or "").strip()
        v10 = _parse_int(raw10)
        v13 = _parse_int(raw13)
        v15 = _parse_int(raw15)
        v228 = _parse_float(raw228)
        sod20 = parse_hhmmss(raw20)
        ok10 = fid10_ok(raw10, v10)
        ok13 = v13 is not None and raw13 not in ("",)
        ok15 = v15 is not None and raw15 not in ("",)
        ok20 = sod20 is not None
        ok228 = v228 is not None and raw228 not in ("",)
        fp = (code, rt, raw20, raw15, raw10)
        if fp in self._recent:
            self.stats.incr("dup_suspect")
        self._recent.append(fp)
        rec = {
            "recv_ns": recv_ns,
            "recv_iso": recv_iso,
            "code": code,
            "real_type": rt,
            "epoch": self.session.epoch,
            "phase": self.session.phase(recv_ns),
            "fid10_raw": raw10,
            "fid10": v10,
            "fid10_ok": ok10,
            "fid13_raw": raw13,
            "fid13": v13,
            "fid13_ok": ok13,
            "fid15_raw": raw15,
            "fid15": v15,
            "fid15_ok": ok15,
            "fid20_raw": raw20,
            "fid20_sod": sod20,
            "fid20_ok": ok20,
            "fid228_raw": raw228,
            "fid228": v228,
            "fid228_ok": ok228,
            "clock_offset_sec": clock_offset_sec(local_sod, sod20),
            "seq": self.stats.next_seq(),
        }
        before = self.buffer.overflow_count()
        before_he = self.buffer.history_evicted_count()
        if not self.buffer.push(rec):
            self.stats.incr("rejected_after_close")
            return rec
        self.stats.incr("accepted_records")
        self.stats.note_max("tick_buffer_high_water", self.buffer.high_water)
        self.stats.incr("tick_callbacks")
        if ok10:
            self.stats.incr("fid10_ok")
        if ok20:
            self.stats.incr("fid20_ok")
        if ok228:
            self.stats.incr("fid228_ok")
        self.stats.note_fid13(ok13)
        self.stats.note_fid15(ok15)
        added = self.buffer.overflow_count() - before
        if added:
            self.stats.incr("dropped_overflow", added)
        he_add = self.buffer.history_evicted_count() - before_he
        if he_add:
            self.stats.incr("buffer_history_evicted", he_add)
        if self.sink is not None:
            self.sink.offer("tick", rec)
        self.stats.note_max(
            "tick_salvage_pending_high_water", self.buffer.pending_unqueued()
        )
        return rec
