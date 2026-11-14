"""CSV 전용 저장 스레드. 키움 API 및 FID 조회를 이 스레드에서 하지 않는다."""
from __future__ import annotations

import os
import queue
import threading
import time
from typing import Any, Dict, Optional, Sequence, Tuple

from scalp.collector.data_writer import DEFAULT_MAX_BYTES, PhaseRotatingWriter
from scalp.collector.orderbook_collector import HOGA_FIELDS
from scalp.collector.realtime_buffer import RealtimeBuffer
from scalp.collector.stats import CollectorStats
from scalp.collector.tick_collector import TICK_FIELDS

KindRec = Tuple[str, Dict[str, Any]]


def _skip_salvage(rec: Dict[str, Any]) -> bool:
    return bool(
        rec.get("_enqueued")
        or rec.get("_written")
        or rec.get("_write_failed")
        or rec.get("_overflow_dropped")
    )


class WriterService:
    def __init__(
        self,
        directory: str,
        *,
        tick_buffer: RealtimeBuffer,
        hoga_buffer: RealtimeBuffer,
        stats: CollectorStats,
        queue_size: int = 1000,
        max_bytes: int = DEFAULT_MAX_BYTES,
        date_str: Optional[str] = None,
        join_timeout_sec: float = 2.0,
        tick_fields: Sequence[str] = TICK_FIELDS,
        hoga_fields: Sequence[str] = HOGA_FIELDS,
    ) -> None:
        self.directory = directory
        self.tick_buffer = tick_buffer
        self.hoga_buffer = hoga_buffer
        self.stats = stats
        self.join_timeout_sec = float(join_timeout_sec)
        self._q: queue.Queue = queue.Queue(maxsize=max(1, int(queue_size)))
        self._stop = threading.Event()
        self._write_lock = threading.RLock()
        self._gate = threading.Condition()
        self._close_lock = threading.Lock()
        self._accepting = True
        self._callback_inflight = 0
        self._write_inflight = 0
        self._close_started = False
        self._closed = False
        self.tick_writer = PhaseRotatingWriter(
            directory, "tick", tick_fields, max_bytes=max_bytes, date_str=date_str, stats=stats
        )
        self.hoga_writer = PhaseRotatingWriter(
            directory, "orderbook", hoga_fields, max_bytes=max_bytes, date_str=date_str, stats=stats
        )
        self._thread: Optional[threading.Thread] = None
        self._started = False
        self.writer_thread_id: Optional[int] = None
        self.ocx_calls_from_writer = 0

    @property
    def accepting(self) -> bool:
        return self._accepting

    def start(self) -> None:
        if self._close_started or self._closed:
            raise RuntimeError("WriterService is closed")
        if not self._started:
            self._thread = threading.Thread(target=self._run, name="scalp-csv-writer", daemon=True)
            self._thread.start()
            self._started = True

    def begin_handle(self) -> bool:
        with self._gate:
            if not self._accepting:
                return False
            self._callback_inflight += 1
            return True

    def end_handle(self) -> None:
        with self._gate:
            if self._callback_inflight > 0:
                self._callback_inflight -= 1
            self._gate.notify_all()

    def offer(self, kind: str, rec: Dict[str, Any]) -> bool:
        with self._gate:
            if not self._accepting and self._callback_inflight <= 0:
                self.stats.incr("rejected_after_close")
                return False
        try:
            self._q.put_nowait((kind, rec))
        except queue.Full:
            rec["_enqueued"] = False
            self.stats.incr("writer_queue_full_attempts")
            return False
        rec["_enqueued"] = True
        if kind == "hoga":
            self.hoga_buffer.mark_enqueued(rec)
        else:
            self.tick_buffer.mark_enqueued(rec)
        qn = self._q.qsize()
        self.stats.note_max("writer_queue_high_water", qn)
        return True

    def _write_item(self, item: KindRec) -> None:
        with self._gate:
            self._write_inflight += 1
        try:
            kind, rec = item
            with self._write_lock:
                if kind == "hoga":
                    self.hoga_writer.write_record(rec)
                else:
                    self.tick_writer.write_record(rec)
        finally:
            with self._gate:
                self._write_inflight -= 1

    def _drain_queue(self, block: bool = False, timeout: float = 0.05) -> int:
        n = 0
        while True:
            try:
                if block:
                    item = self._q.get(timeout=timeout)
                else:
                    item = self._q.get_nowait()
            except queue.Empty:
                break
            self._write_item(item)
            n += 1
        return n

    def _run(self) -> None:
        self.writer_thread_id = threading.get_ident()
        while not self._stop.is_set():
            self._drain_queue(block=True, timeout=0.05)
        self._drain_queue(block=False)

    def flush_all(self) -> int:
        n = self._drain_queue(block=False)
        n += self._salvage_buffers()
        return n

    def _salvage_buffers(self) -> int:
        n = 0
        for rec in self.tick_buffer.flush_all():
            if not _skip_salvage(rec):
                self._write_item(("tick", rec))
                n += 1
        for rec in self.hoga_buffer.flush_all():
            if not _skip_salvage(rec):
                self._write_item(("hoga", rec))
                n += 1
        return n

    def _refresh_pending(self) -> None:
        self.stats.set_pending(
            tick_buffer_pending=self.tick_buffer.pending_unqueued(),
            orderbook_buffer_pending=self.hoga_buffer.pending_unqueued(),
            writer_queue_pending=self._q.qsize(),
            writer_inflight=self._write_inflight,
        )

    def _dump_stats(self) -> None:
        self._refresh_pending()
        path = os.path.join(self.directory, "collector_stats.json")
        ok = False
        try:
            os.makedirs(self.directory, exist_ok=True)
            ok = bool(self.stats.dump_json(path))
        except Exception:
            ok = False
        if not ok:
            self.stats.mark_dump_failed()

    def dump_stats_now(self) -> None:
        """최종 상태 반영 후 한 번 저장. Writer flush 와 분리한다."""
        self._dump_stats()

    def close(
        self,
        timeout: Optional[float] = None,
        terminal: str = "COMPLETE",
        *,
        dump_stats: bool = True,
    ) -> None:
        to = self.join_timeout_sec if timeout is None else float(timeout)
        wanted = str(terminal or "COMPLETE")
        if wanted not in ("COMPLETE", "INCOMPLETE", "STATS_DUMP_FAILED"):
            wanted = "INCOMPLETE"
        with self._close_lock:
            if self._close_started:
                return
            self._close_started = True
            self._close_impl(to, wanted, dump_stats=dump_stats)

    def _finish_close(self, wanted: str, dump_stats: bool) -> None:
        self._refresh_pending()
        if dump_stats:
            if wanted == "COMPLETE":
                self.stats.set_shutdown("COMPLETE")
            else:
                self.stats.set_shutdown(wanted)
            self._dump_stats()
            return
        if wanted != "COMPLETE":
            self.stats.set_shutdown(wanted)

    def _close_impl(self, to: float, wanted: str, dump_stats: bool = True) -> None:
        with self._gate:
            self._accepting = False
            deadline = time.monotonic() + max(0.0, to)
            while self._callback_inflight > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._gate.wait(remaining)
            callbacks_done = self._callback_inflight == 0

        if not callbacks_done:
            self.stats.set_shutdown("INCOMPLETE")
            if dump_stats:
                self._dump_stats()
            else:
                self._refresh_pending()
            return

        self._stop.set()
        if self._started and self._thread is not None:
            self._thread.join(to)
            if self._thread.is_alive():
                self.stats.note_join_timeout()
                self.stats.set_shutdown("INCOMPLETE")
                if dump_stats:
                    self._dump_stats()
                else:
                    self._refresh_pending()
                return

        try:
            with self._write_lock:
                self._drain_queue(block=False)
                self._salvage_buffers()
                self.tick_writer.close()
                self.hoga_writer.close()
        except Exception:
            self._closed = True
            self.stats.set_shutdown("INCOMPLETE")
            if dump_stats:
                self._dump_stats()
            else:
                self._refresh_pending()
            return
        self._closed = True
        self._finish_close(wanted, dump_stats)
