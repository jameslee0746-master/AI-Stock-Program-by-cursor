"""본관찰과 등록 해제 이후를 시각 기준으로 나눈다. 태그를 먼저 바꾸지 않는다."""
from __future__ import annotations

from typing import Optional

from scalp.collector.clock import recv_ns


class CollectSession:
    def __init__(self) -> None:
        self.epoch: int = 0
        self.unreg_at_mono: Optional[int] = None

    @property
    def unreg_at_ns(self) -> Optional[int]:
        return self.unreg_at_mono

    def begin(self) -> int:
        self.epoch += 1
        self.unreg_at_mono = None
        return self.epoch

    def mark_unreg(self, at_ns: Optional[int] = None) -> int:
        """등록 해제 시각(monotonic ns). 이 시각 이상 수신은 after_unreg."""
        self.unreg_at_mono = int(at_ns if at_ns is not None else recv_ns())
        return self.unreg_at_mono

    def phase(self, event_ns: int) -> str:
        if self.unreg_at_mono is None or int(event_ns) < self.unreg_at_mono:
            return "observe"
        return "after_unreg"
