# -*- coding: utf-8 -*-
"""
order_event_log.csv → 매수 체결·매도 체결을 종목별 FIFO로 짝지어 보유 기간 등 파생 CSV 생성.

입력: data/order_event_log.csv (컬럼 timestamp,event,side,code,qty,price,note)
출력: data/order_hold_roundtrips.csv (기본값, --output 으로 변경 가능)

실행 예:
  python derive_order_hold_roundtrips.py
  python derive_order_hold_roundtrips.py --input data/order_event_log.csv --output data/order_hold_roundtrips.csv
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import re
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Deque, Dict, List, Optional, Tuple


def _parse_ts(s: str) -> Optional[datetime]:
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s[:19], fmt)
        except ValueError:
            continue
    return None


def _safe_int(x: Any) -> int:
    try:
        return int(float(str(x).replace(",", "").strip() or 0))
    except (TypeError, ValueError):
        return 0


def _safe_float(x: Any) -> float:
    try:
        v = float(str(x).replace(",", "").strip() or 0)
        return v if math.isfinite(v) else 0.0
    except (TypeError, ValueError):
        return 0.0


_REASON_RE = re.compile(r"reason=([^\\s]+)")


def _parse_sell_reason(note: str) -> str:
    m = _REASON_RE.search(note or "")
    return (m.group(1).strip() if m else "").strip()


def _weekday_span_inclusive(d0, d1) -> int:
    """휴장 미반영: 두 날짜(포함) 사이의 월~금 일수 근사."""
    if d1 < d0:
        d0, d1 = d1, d0
    n = 0
    d = d0
    while d <= d1:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n


@dataclass
class Lot:
    ts: datetime
    qty: int
    price: float
    note: str


def _iter_filled_rows(path: str) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    if not os.path.isfile(path):
        return rows
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        r = csv.DictReader(f)
        for row in r:
            if not row:
                continue
            if str(row.get("event", "")).strip() != "ORDER_FILLED":
                continue
            side = str(row.get("side", "")).strip().upper()
            if side not in ("BUY", "SELL"):
                continue
            code = str(row.get("code", "")).strip()
            qty = _safe_int(row.get("qty"))
            if not code or qty <= 0:
                continue
            ts = _parse_ts(str(row.get("timestamp", "") or ""))
            if ts is None:
                continue
            rows.append(
                {
                    "timestamp": row.get("timestamp", ""),
                    "_ts": ts,
                    "side": side,
                    "code": code,
                    "qty": str(qty),
                    "price": str(row.get("price", "0")),
                    "note": str(row.get("note", "") or ""),
                }
            )
    rows.sort(key=lambda x: x["_ts"])  # type: ignore[arg-type]
    return rows


def derive_roundtrips(
    rows: List[Dict[str, str]],
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    종목별 FIFO로 매칭.
    반환: (매칭 행 목록, 경고 문자열 목록)
    """
    warns: List[str] = []
    out: List[Dict[str, Any]] = []
    queues: Dict[str, Deque[Lot]] = {}

    for row in rows:
        ts: datetime = row["_ts"]
        side = row["side"]
        code = row["code"]
        qty = _safe_int(row["qty"])
        price = _safe_float(row["price"])
        note = row.get("note", "")
        q = queues.setdefault(code, deque())

        if side == "BUY":
            q.append(Lot(ts=ts, qty=qty, price=price, note=note))
            continue

        rem = qty
        sell_reason = _parse_sell_reason(note)

        while rem > 0:
            if not q:
                warns.append(f"{ts} SELL {code} qty={rem}: 매칭할 매수 체결 없음(로그 이전 포지션·누락 가능)")
                break
            lot = q[0]
            take = min(lot.qty, rem)
            delta = ts - lot.ts
            hold_sec = max(0, int(delta.total_seconds()))
            bd = lot.ts.date()
            sd = ts.date()
            cal_days = (sd - bd).days

            out.append(
                {
                    "sell_timestamp": ts.strftime("%Y-%m-%d %H:%M:%S"),
                    "buy_timestamp": lot.ts.strftime("%Y-%m-%d %H:%M:%S"),
                    "code": code,
                    "matched_qty": take,
                    "buy_price": round(lot.price, 4),
                    "sell_price": round(price, 4),
                    "hold_seconds": hold_sec,
                    "hold_calendar_days": cal_days,
                    "hold_weekday_days": _weekday_span_inclusive(bd, sd),
                    "sell_reason": sell_reason,
                    "buy_note": lot.note,
                    "sell_note": note,
                }
            )

            lot.qty -= take
            rem -= take
            if lot.qty <= 0:
                q.popleft()

        if rem > 0:
            warns.append(f"{ts} SELL {code}: 남은 {rem}주 미매칭(매수 부족)")

    for code, q in queues.items():
        rest = sum(l.qty for l in q)
        if rest > 0:
            warns.append(f"종목 {code}: 로그 종료 시점 미청산 매수 잔량 합계 {rest}주 (아직 매도 전)")

    return out, warns


def main() -> int:
    base = os.path.dirname(os.path.abspath(__file__))
    default_in = os.path.join(base, "data", "order_event_log.csv")
    default_out = os.path.join(base, "data", "order_hold_roundtrips.csv")

    ap = argparse.ArgumentParser(description="order_event_log.csv 에서 매수–매도 FIFO 보유구간 파생")
    ap.add_argument("--input", default=default_in, help="order_event_log.csv 경로")
    ap.add_argument("--output", default=default_out, help="출력 CSV 경로")
    args = ap.parse_args()

    filled = _iter_filled_rows(args.input)
    if not filled:
        print(f"[hold] 입력에 ORDER_FILLED BUY/SELL 행이 없거나 파일 없음: {args.input}")
        return 1

    trips, warns = derive_roundtrips(filled)

    fields = [
        "sell_timestamp",
        "buy_timestamp",
        "code",
        "matched_qty",
        "buy_price",
        "sell_price",
        "hold_seconds",
        "hold_calendar_days",
        "hold_weekday_days",
        "sell_reason",
        "buy_note",
        "sell_note",
    ]

    os.makedirs(os.path.dirname(os.path.abspath(args.output)) or ".", exist_ok=True)
    with open(args.output, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in trips:
            w.writerow(row)

    print(f"[hold] 매칭 행 수: {len(trips)} → {args.output}")
    if warns:
        print("[hold] 참고:")
        for wtxt in warns[:30]:
            print(f"  - {wtxt}")
        if len(warns) > 30:
            print(f"  ... 외 {len(warns) - 30}건")

    if trips:
        secs = [_safe_int(r["hold_seconds"]) for r in trips]
        med = sorted(secs)[len(secs) // 2]
        wdays = [_safe_int(r["hold_weekday_days"]) for r in trips]
        wmed = sorted(wdays)[len(wdays) // 2]
        print(f"[hold] 보유 hold_seconds 중앙값~{med}s, hold_weekday_days 중앙값~{wmed}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
