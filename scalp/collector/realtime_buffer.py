"""고정 길이 버퍼. maxlen 자동 삭제를 쓰지 않고, 초과 손실을 직접 센다.

push/pop/drain 은 한 Lock 아래에서 원자적으로 처리한다.
디스크 I/O 는 이 Lock 밖에서 수행한다.

이 버퍼는 Writer 대기열이 아니다. 성공적으로 enqueue 된 레코드도 occupancy 에
남을 수 있으며, 종료 시 salvage/history 용도이다. 실제 쓰기 대기는 Writer Queue.
"""
from __future__ import annotations

import threading
from collections import deque
from typing import Any, Deque, List


def _kept_elsewhere(record: Any) -> bool:
    if not isinstance(record, dict):
        return False
    return bool(
        record.get("_enqueued")
        or record.get("_written")
        or record.get("_write_failed")
    )


class RealtimeBuffer:
    def __init__(self, maxlen: int = 5000) -> None:
        if maxlen < 1:
            raise ValueError("maxlen must be >= 1")
        self.maxlen = int(maxlen)
        self._dq: Deque[Any] = deque()
        self._lock = threading.Lock()
        self.dropped_overflow = 0
        self.history_evicted = 0
        self.high_water = 0
        self.salvage_pending_high_water = 0
        self._pending_unqueued = 0
        self._closed = False

    def __len__(self) -> int:
        with self._lock:
            return len(self._dq)

    def _bump_salvage_hw(self) -> None:
        if self._pending_unqueued > self.salvage_pending_high_water:
            self.salvage_pending_high_water = self._pending_unqueued

    def _discard_occupancy(self, old: Any) -> None:
        if _kept_elsewhere(old):
            self.history_evicted += 1
            return
        self.dropped_overflow += 1
        self._pending_unqueued = max(0, self._pending_unqueued - 1)
        if isinstance(old, dict):
            old["_overflow_dropped"] = True

    def push(self, record: Any) -> bool:
        with self._lock:
            if self._closed:
                return False
            if len(self._dq) >= self.maxlen:
                old = self._dq.popleft()
                self._discard_occupancy(old)
            self._dq.append(record)
            if not _kept_elsewhere(record):
                self._pending_unqueued += 1
                self._bump_salvage_hw()
            n = len(self._dq)
            if n > self.high_water:
                self.high_water = n
            return True

    def mark_enqueued(self, record: Any = None) -> None:
        """offer 성공 후 salvage pending 만 내린다. occupancy 는 유지한다."""
        with self._lock:
            if self._pending_unqueued > 0:
                self._pending_unqueued -= 1

    def overflow_count(self) -> int:
        with self._lock:
            return self.dropped_overflow

    def history_evicted_count(self) -> int:
        with self._lock:
            return self.history_evicted

    def pop_batch(self, limit: int) -> List[Any]:
        n = max(0, int(limit))
        with self._lock:
            out: List[Any] = []
            while self._dq and len(out) < n:
                rec = self._dq.popleft()
                if not _kept_elsewhere(rec):
                    self._pending_unqueued = max(0, self._pending_unqueued - 1)
                out.append(rec)
            return out

    def flush_all(self) -> List[Any]:
        with self._lock:
            out = list(self._dq)
            self._dq.clear()
            self._pending_unqueued = 0
            return out

    def peek_all(self) -> List[Any]:
        with self._lock:
            return list(self._dq)

    def pending_unqueued(self) -> int:
        with self._lock:
            return int(self._pending_unqueued)

    def close(self) -> List[Any]:
        with self._lock:
            self._closed = True
            out = list(self._dq)
            self._dq.clear()
            self._pending_unqueued = 0
            return out
