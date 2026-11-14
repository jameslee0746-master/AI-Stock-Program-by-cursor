"""수신 시각과 거래소 시각을 분리한다. 보정 전 값을 네트워크 지연으로 부르지 않는다."""
from __future__ import annotations

import datetime
from typing import Optional, Tuple


def recv_ns() -> int:
    import time

    return time.perf_counter_ns()


def recv_iso_us(now: Optional[datetime.datetime] = None) -> str:
    now = now or datetime.datetime.now()
    return now.isoformat(timespec="microseconds")


def local_seconds_of_day(now: Optional[datetime.datetime] = None) -> float:
    now = now or datetime.datetime.now()
    return now.hour * 3600 + now.minute * 60 + now.second + now.microsecond / 1_000_000.0


def parse_hhmmss(raw: str) -> Optional[int]:
    t = str(raw or "").strip().replace(":", "")
    if len(t) != 6 or not t.isdigit():
        return None
    h, mi, se = int(t[:2]), int(t[2:4]), int(t[4:])
    if h > 23 or mi > 59 or se > 59:
        return None
    return h * 3600 + mi * 60 + se


def clock_offset_sec(local_sod: float, fid20_sod: Optional[int]) -> Optional[float]:
    """로컬 초 − FID20 초. PC 시계와 거래소 시각의 차이. latency 가 아니다."""
    if fid20_sod is None:
        return None
    return round(float(local_sod) - float(fid20_sod), 6)


def stamp(now: Optional[datetime.datetime] = None) -> Tuple[int, str, float]:
    return recv_ns(), recv_iso_us(now), local_seconds_of_day(now)
