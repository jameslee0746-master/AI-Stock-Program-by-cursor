"""주식호가잔량 수집. 콜백에서는 FID 복사와 버퍼 push 만 한다."""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from scalp.collector import HOGA_FIDS
from scalp.collector.clock import recv_ns, stamp
from scalp.collector.realtime_buffer import RealtimeBuffer
from scalp.collector.session import CollectSession
from scalp.collector.stats import CollectorStats

FidGet = Callable[[str, int], str]

HOGA_FIELDS = (
    "recv_ns", "recv_iso", "code", "real_type", "epoch", "phase",
) + tuple(f"fid{f}_raw" for f in HOGA_FIDS) + tuple(f"fid{f}_ok" for f in HOGA_FIDS) + ("seq",)


class OrderbookCollector:
    def __init__(
        self,
        fid_get: FidGet,
        buffer: RealtimeBuffer,
        stats: CollectorStats,
        session: CollectSession,
        sink: Any = None,
    ) -> None:
        self.fid_get = fid_get
        self.buffer = buffer
        self.stats = stats
        self.session = session
        self.sink = sink

    def handle(self, code: str, real_type: str, _real_data: str = "") -> Optional[Dict[str, Any]]:
        t0 = recv_ns()
        entered = False
        try:
            if str(real_type or "").strip() != "주식호가잔량":
                return None
            self.stats.incr("callback_total")
            if self.sink is not None:
                if not self.sink.begin_handle():
                    self.stats.incr("rejected_after_close")
                    return None
                entered = True
            return self._handle(code, "주식호가잔량")
        except Exception:
            self.stats.incr("handler_exceptions")
            return None
        finally:
            if entered and self.sink is not None:
                self.sink.end_handle()
            self.stats.note_handle_ns(recv_ns() - t0)

    def _handle(self, code: str, real_type: str) -> Optional[Dict[str, Any]]:
        rt = str(real_type or "").strip()
        if rt != "주식호가잔량":
            return None
        code = str(code or "").strip()
        event_ns, recv_iso, _sod = stamp()
        rec: Dict[str, Any] = {
            "recv_ns": event_ns,
            "recv_iso": recv_iso,
            "code": code,
            "real_type": rt,
            "epoch": self.session.epoch,
            "phase": self.session.phase(event_ns),
            "seq": self.stats.next_seq(),
        }
        for fid in HOGA_FIDS:
            raw = str(self.fid_get(code, fid) or "").strip()
            rec[f"fid{fid}_raw"] = raw
            rec[f"fid{fid}_ok"] = raw not in ("",)
        before = self.buffer.overflow_count()
        before_he = self.buffer.history_evicted_count()
        if not self.buffer.push(rec):
            self.stats.incr("rejected_after_close")
            return rec
        self.stats.incr("accepted_records")
        self.stats.note_max("orderbook_buffer_high_water", self.buffer.high_water)
        self.stats.incr("hoga_callbacks")
        added = self.buffer.overflow_count() - before
        if added:
            self.stats.incr("dropped_overflow", added)
        he_add = self.buffer.history_evicted_count() - before_he
        if he_add:
            self.stats.incr("buffer_history_evicted", he_add)
        if self.sink is not None:
            self.sink.offer("hoga", rec)
        self.stats.note_max(
            "orderbook_salvage_pending_high_water", self.buffer.pending_unqueued()
        )
        return rec
