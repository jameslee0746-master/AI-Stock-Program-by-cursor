"""수집 카운터. overflow / queue-full / write 실패를 분리한다. 시계열은 누적하지 않는다."""
from __future__ import annotations

import json
import os
import threading
from collections import deque
from typing import Any, Dict, Optional

from scalp.collector.clock import recv_iso_us, recv_ns

ERROR_HISTORY_MAX = 32
BRIDGE_DETACHED = "DETACHED"
BRIDGE_ATTACHING = "ATTACHING"
BRIDGE_RUNNING = "RUNNING"
BRIDGE_DEGRADED = "DEGRADED"
BRIDGE_ERROR = "ERROR"
BRIDGE_CLOSED = "CLOSED"
_SHUTDOWN_ALLOWED = ("RUNNING", "COMPLETE", "INCOMPLETE", "STATS_DUMP_FAILED")
_SHUTDOWN_TERMINAL = ("COMPLETE", "INCOMPLETE", "STATS_DUMP_FAILED")
BUFFER_ROLE_SALVAGE_HISTORY = "salvage_history"
WRITER_QUEUE_ROLE_BACKLOG = "write_backlog"
BUFFER_STATS_NOTE = {
    "tick_buffer_high_water": (
        "RealtimeBuffer occupancy including already-enqueued copies; not writer wait"
    ),
    "orderbook_buffer_high_water": (
        "RealtimeBuffer occupancy including already-enqueued copies; not writer wait"
    ),
    "writer_queue_high_water": "actual Writer Queue backlog",
    "tick_salvage_pending_high_water": (
        "max unqueued records waiting for Writer salvage/write"
    ),
    "orderbook_salvage_pending_high_water": (
        "max unqueued records waiting for Writer salvage/write"
    ),
    "dropped_overflow": "data loss: unqueued records dropped on buffer overflow",
    "buffer_history_evicted": (
        "already-enqueued records dropped from salvage/history occupancy; not data loss"
    ),
    "terminal_losses": (
        "actual data loss (dropped_overflow + dropped_write + dropped_writer_queue)"
    ),
}


def process_rss_bytes() -> Optional[int]:
    """Windows 32bit WorkingSet. 실패하면 None."""
    try:
        import ctypes
        from ctypes import wintypes

        class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        GetCurrentProcess = ctypes.windll.kernel32.GetCurrentProcess
        GetCurrentProcess.restype = wintypes.HANDLE
        pmc = PROCESS_MEMORY_COUNTERS()
        pmc.cb = ctypes.sizeof(pmc)
        ok = ctypes.windll.psapi.GetProcessMemoryInfo(GetCurrentProcess(), ctypes.byref(pmc), pmc.cb)
        if not ok:
            return None
        return int(pmc.WorkingSetSize)
    except Exception:
        return None


class CollectorStats:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._seq = 0
        self.start_ns = recv_ns()
        self.callback_total = 0
        self.accepted_records = 0
        self.tick_callbacks = 0
        self.hoga_callbacks = 0
        self.dropped_overflow = 0
        self.dropped_writer_queue = 0
        self.writer_queue_full_attempts = 0
        self.rejected_after_close = 0
        self.write_fail = 0
        self.dropped_write = 0
        self.write_ok = 0
        self.dup_suspect = 0
        self.fid10_ok = 0
        self.fid13_ok = 0
        self.fid15_ok = 0
        self.fid20_ok = 0
        self.fid228_ok = 0
        self.fid13_missing = 0
        self.fid15_missing = 0
        self.flush_count = 0
        self.last_flush_ns = 0
        self.max_flush_ns = 0
        self.flush_ns_sum = 0
        self.max_save_lag_ns = 0
        self.handler_exceptions = 0
        self.handle_ns_sum = 0
        self.handle_ns_count = 0
        self.handle_ns_max = 0
        self.tick_buffer_high_water = 0
        self.orderbook_buffer_high_water = 0
        self.writer_queue_high_water = 0
        self.tick_salvage_pending_high_water = 0
        self.orderbook_salvage_pending_high_water = 0
        self.buffer_history_evicted = 0
        self.orig_ocx_calls = 0
        self.orig_ocx_by_method: Dict[str, Any] = {}
        self.orig_ocx_setrealremove: Dict[str, Any] = {}
        self.shutdown_trace: list = []
        self.writer_join_timeout = False
        self.session_id = ""
        self.session_start = ""
        self.session_finished = ""
        self.shutdown_status = "RUNNING"
        self.bridge_status = BRIDGE_DETACHED
        self.last_error = ""
        self.error_history: deque = deque(maxlen=ERROR_HISTORY_MAX)
        self.screen_state: Dict[str, str] = {"8101": "NOT_REGISTERED", "8102": "NOT_REGISTERED"}
        self.screen_error: Dict[str, str] = {"8101": "", "8102": ""}
        self.attach_attempt_count = 0
        self.lazy_attach_attempted = False
        self.last_attach_error = ""
        self.attach_status = "IDLE"
        self.stats_dump_ok: Optional[bool] = None
        self.pending_buffer = 0
        self.pending_queue = 0
        self.pending_inflight = 0
        self.tick_buffer_pending = 0
        self.orderbook_buffer_pending = 0
        self.writer_queue_pending = 0
        self.writer_inflight = 0
        self.files: list = []

    def incr(self, name: str, n: int = 1) -> None:
        with self._lock:
            setattr(self, name, getattr(self, name) + n)

    def note_max(self, name: str, value: int) -> None:
        with self._lock:
            cur = getattr(self, name)
            if value > cur:
                setattr(self, name, value)

    def next_seq(self) -> int:
        with self._lock:
            self._seq += 1
            return self._seq

    def note_join_timeout(self) -> None:
        with self._lock:
            self.writer_join_timeout = True

    def bind_session(self, session_id: str, session_start: str = "") -> None:
        """JSONL 과 같은 session_id 를 받는다. 여기서 UUID 를 만들지 않는다."""
        sid = str(session_id or "").strip()
        if not sid:
            return
        start = str(session_start or "").strip()
        with self._lock:
            if self.session_id and self.session_id != sid:
                return
            self.session_id = sid
            if start and not self.session_start:
                self.session_start = start

    def set_session_finished(self, ts: str) -> None:
        finished = str(ts or "").strip()
        if not finished:
            return
        with self._lock:
            if not self.session_finished:
                self.session_finished = finished

    def set_shutdown(self, status: str) -> None:
        if status not in _SHUTDOWN_ALLOWED:
            return
        with self._lock:
            cur = self.shutdown_status
            if cur in _SHUTDOWN_TERMINAL and status == "COMPLETE" and cur != "COMPLETE":
                return
            self.shutdown_status = status

    def set_bridge_status(self, status: str) -> None:
        with self._lock:
            self.bridge_status = str(status)

    def record_error(self, kind: str, message: str, screen: str = "") -> None:
        msg = str(message or "")
        entry = {
            "ts": recv_iso_us(),
            "kind": str(kind or ""),
            "screen": str(screen or ""),
            "message": msg,
        }
        with self._lock:
            self.last_error = msg
            self.error_history.append(entry)

    def set_screen_runtime(self, screen: str, state: str, error: Optional[str] = None) -> None:
        key = str(screen)
        with self._lock:
            self.screen_state[key] = str(state)
            if error:
                self.screen_error[key] = str(error)

    def append_files(self, rows: list) -> None:
        with self._lock:
            self.files = list(self.files) + list(rows)

    def note_shutdown_step(self, step: str) -> None:
        name = str(step or "").strip()
        if not name:
            return
        with self._lock:
            self.shutdown_trace.append(name)

    def set_orig_ocx(
        self,
        *,
        orig_calls: int = 0,
        by_method: Optional[Dict[str, Any]] = None,
        setrealremove: Optional[Dict[str, Any]] = None,
    ) -> None:
        with self._lock:
            self.orig_ocx_calls = int(orig_calls or 0)
            self.orig_ocx_by_method = dict(by_method or {})
            self.orig_ocx_setrealremove = dict(setrealremove or {})

    def mark_dump_failed(self) -> None:
        with self._lock:
            self.stats_dump_ok = False
            if self.shutdown_status == "COMPLETE":
                self.shutdown_status = "STATS_DUMP_FAILED"

    def set_pending(
        self,
        *,
        tick_buffer_pending: int,
        orderbook_buffer_pending: int,
        writer_queue_pending: int,
        writer_inflight: int,
    ) -> None:
        with self._lock:
            self.tick_buffer_pending = int(tick_buffer_pending)
            self.orderbook_buffer_pending = int(orderbook_buffer_pending)
            self.pending_buffer = self.tick_buffer_pending + self.orderbook_buffer_pending
            self.writer_queue_pending = int(writer_queue_pending)
            self.pending_queue = self.writer_queue_pending
            self.writer_inflight = int(writer_inflight)
            self.pending_inflight = self.writer_inflight

    def note_fid13(self, ok: bool) -> None:
        with self._lock:
            if ok:
                self.fid13_ok += 1
            else:
                self.fid13_missing += 1

    def note_fid15(self, ok: bool) -> None:
        with self._lock:
            if ok:
                self.fid15_ok += 1
            else:
                self.fid15_missing += 1

    def note_handle_ns(self, dt: int) -> None:
        if dt < 0:
            return
        with self._lock:
            self.handle_ns_sum += dt
            self.handle_ns_count += 1
            if dt > self.handle_ns_max:
                self.handle_ns_max = dt

    def note_flush_ns(self, dt: int) -> None:
        with self._lock:
            self.flush_count += 1
            self.last_flush_ns = dt
            self.flush_ns_sum += dt
            if dt > self.max_flush_ns:
                self.max_flush_ns = dt

    def elapsed_sec(self) -> float:
        return max(1e-9, (recv_ns() - self.start_ns) / 1_000_000_000.0)

    def terminal_losses(self) -> int:
        return self.dropped_overflow + self.dropped_write + self.dropped_writer_queue

    def pending_records(self) -> int:
        return self.pending_buffer + self.pending_queue + self.pending_inflight

    def identity_ok(self) -> bool:
        return self.accepted_records == self.write_ok + self.terminal_losses() + self.pending_records()

    def fid13_15_presence(self) -> Dict[str, Any]:
        ticks = self.tick_callbacks
        return {
            "tick_callbacks": ticks,
            "fid13_ok": self.fid13_ok,
            "fid13_missing": self.fid13_missing,
            "fid15_ok": self.fid15_ok,
            "fid15_missing": self.fid15_missing,
            "fid13_ok_ratio": (self.fid13_ok / ticks) if ticks else None,
            "fid15_ok_ratio": (self.fid15_ok / ticks) if ticks else None,
            "note": "비율 0 이면 GetCommRealData 빈값. SetRealReg 8101 의 TICK_FIDS 에 13;15 포함 여부를 확인.",
        }

    def as_dict(self) -> Dict[str, Any]:
        with self._lock:
            elapsed = self.elapsed_sec()
            rss = process_rss_bytes()
            handle_avg = (self.handle_ns_sum / self.handle_ns_count) if self.handle_ns_count else 0
            flush_avg = (self.flush_ns_sum / self.flush_count) if self.flush_count else 0
            terminal = self.terminal_losses()
            pending = self.pending_records()
            return {
                "session_id": self.session_id,
                "session_start": self.session_start,
                "session_finished": self.session_finished,
                "shutdown_status": self.shutdown_status,
                "bridge_status": self.bridge_status,
                "elapsed_sec": round(elapsed, 6),
                "ticks_per_sec": round(self.tick_callbacks / elapsed, 6),
                "hoga_per_sec": round(self.hoga_callbacks / elapsed, 6),
                "callback_total": self.callback_total,
                "accepted_records": self.accepted_records,
                "tick_callbacks": self.tick_callbacks,
                "hoga_callbacks": self.hoga_callbacks,
                "handle_ns_avg": round(handle_avg, 3),
                "handle_ns_max": self.handle_ns_max,
                "csv_flush_ns_avg": round(flush_avg, 3),
                "csv_flush_ns_max": self.max_flush_ns,
                "tick_buffer_role": BUFFER_ROLE_SALVAGE_HISTORY,
                "orderbook_buffer_role": BUFFER_ROLE_SALVAGE_HISTORY,
                "writer_queue_role": WRITER_QUEUE_ROLE_BACKLOG,
                "tick_buffer_high_water": self.tick_buffer_high_water,
                "orderbook_buffer_high_water": self.orderbook_buffer_high_water,
                "writer_queue_high_water": self.writer_queue_high_water,
                "tick_salvage_pending_high_water": self.tick_salvage_pending_high_water,
                "orderbook_salvage_pending_high_water": self.orderbook_salvage_pending_high_water,
                "buffer_history_evicted": self.buffer_history_evicted,
                "data_loss": terminal,
                "buffer_stats_note": dict(BUFFER_STATS_NOTE),
                "writer_queue_full_attempts": self.writer_queue_full_attempts,
                "dropped_overflow": self.dropped_overflow,
                "dropped_writer_queue": self.dropped_writer_queue,
                "dropped_write": self.dropped_write,
                "rejected_after_close": self.rejected_after_close,
                "handler_exceptions": self.handler_exceptions,
                "write_ok": self.write_ok,
                "write_fail": self.write_fail,
                "dup_suspect": self.dup_suspect,
                "tick_buffer_pending": self.tick_buffer_pending,
                "orderbook_buffer_pending": self.orderbook_buffer_pending,
                "writer_queue_pending": self.writer_queue_pending,
                "writer_inflight": self.writer_inflight,
                "pending_buffer": self.pending_buffer,
                "pending_queue": self.pending_queue,
                "pending_inflight": self.pending_inflight,
                "terminal_losses": terminal,
                "pending_records": pending,
                "identity_ok": self.accepted_records == self.write_ok + terminal + pending,
                "rss_bytes": rss,
                "rss_ok": rss is not None,
                "rss_status": "ok" if rss is not None else "unavailable",
                "writer_join_timeout": self.writer_join_timeout,
                "last_error": self.last_error,
                "error_history": list(self.error_history),
                "screen_state": dict(self.screen_state),
                "screen_error": dict(self.screen_error),
                "screens_active": all(v == "ACTIVE" for v in self.screen_state.values()),
                "attach_attempt_count": self.attach_attempt_count,
                "lazy_attach_attempted": self.lazy_attach_attempted,
                "last_attach_error": self.last_attach_error,
                "attach_status": self.attach_status,
                "stats_dump_ok": self.stats_dump_ok,
                "files": list(self.files),
                "fid13_15": self.fid13_15_presence(),
                "shutdown_trace": list(self.shutdown_trace),
                "orig_ocx_calls": self.orig_ocx_calls,
                "orig_ocx_by_method": dict(self.orig_ocx_by_method),
                "orig_ocx_setrealremove": dict(self.orig_ocx_setrealremove),
            }

    def dump_json(self, path: str) -> bool:
        try:
            with self._lock:
                self.stats_dump_ok = True
            payload = self.as_dict()
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
            return True
        except Exception:
            with self._lock:
                self.stats_dump_ok = False
            return False
