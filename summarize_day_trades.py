#!/usr/bin/env python3
"""
날짜별 실제 체결(매수·매도) 요약 — data/order_event_log.csv 및 선택적 trade_history*.csv.

예:
  python summarize_day_trades.py
  python summarize_day_trades.py --date 2026-05-12
"""
from __future__ import annotations

import argparse
import csv
import glob
import os
from collections import defaultdict
from datetime import date
from typing import DefaultDict, Dict, List, Tuple


def _base_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def _agg_from_order_log(path: str, day_prefix: str) -> Tuple[Dict[str, Dict], Dict[str, Dict]]:
    """ORDER_FILLED 행만 집계 (부분 체결은 수량 합산)."""
    buy: DefaultDict[str, Dict] = defaultdict(lambda: {"qty": 0, "fills": 0, "wavg_num": 0.0})
    sell: DefaultDict[str, Dict] = defaultdict(lambda: {"qty": 0, "fills": 0, "wavg_num": 0.0})
    if not os.path.isfile(path):
        return {}, {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            ts = (r.get("timestamp") or "").strip()
            if not ts.startswith(day_prefix):
                continue
            if (r.get("event") or "").strip() != "ORDER_FILLED":
                continue
            side = (r.get("side") or "").strip().upper()
            code = (r.get("code") or "").strip()
            if not code:
                continue
            try:
                qty = int(float(r.get("qty") or 0))
            except (TypeError, ValueError):
                qty = 0
            try:
                px = float(r.get("price") or 0.0)
            except (TypeError, ValueError):
                px = 0.0
            if qty <= 0:
                continue
            bucket = buy if side == "BUY" else sell
            if side not in ("BUY", "SELL"):
                continue
            e = bucket[code]
            e["qty"] += qty
            e["fills"] += 1
            e["wavg_num"] += px * qty
    out_buy: Dict[str, Dict] = {}
    out_sell: Dict[str, Dict] = {}
    for d, outp in ((buy, out_buy), (sell, out_sell)):
        for code, e in d.items():
            q = int(e["qty"])
            outp[code] = {
                "qty": q,
                "fills": int(e["fills"]),
                "avg_px": (e["wavg_num"] / q) if q else 0.0,
            }
    return out_buy, out_sell


def _rows_from_trade_history(paths: List[str], day_prefix: str) -> List[Tuple[str, str, int, float, str]]:
    """trade_history*.csv 에서 해당 일자 행."""
    rows: List[Tuple[str, str, int, float, str]] = []
    for path in paths:
        if not os.path.isfile(path):
            continue
        with open(path, newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                ts = (r.get("timestamp") or "").strip()
                day_key = ts[:10] if len(ts) >= 10 else ""
                if day_key != day_prefix:
                    continue
                code = (r.get("code") or "").strip()
                side = (r.get("side") or "").strip().upper()
                if side not in ("BUY", "SELL") or not code:
                    continue
                try:
                    qty = int(float(r.get("qty") or 0))
                    px = float(r.get("price") or 0.0)
                except (TypeError, ValueError):
                    continue
                if qty <= 0 or px <= 0:
                    continue
                note = (r.get("sell_reason") or "").strip()
                rows.append((code, side, qty, px, note))
    return rows


def main() -> int:
    base = _base_dir()
    ap = argparse.ArgumentParser(description="지정 일자의 매수·매도 체결 요약")
    ap.add_argument(
        "--date",
        metavar="YYYY-MM-DD",
        help="집계할 날짜 (미지정 시 이 PC의 로컬 오늘)",
    )
    ap.add_argument(
        "--order-log",
        default=os.path.join(base, "data", "order_event_log.csv"),
        help="order_event_log.csv 경로",
    )
    ap.add_argument(
        "--trade-history",
        nargs="*",
        default=None,
        help="trade_history CSV 경로들 (미지정 시 data/trade_history*.csv 자동 검색)",
    )
    args = ap.parse_args()
    day_s = args.date.strip() if args.date else date.today().strftime("%Y-%m-%d")

    buy_o, sell_o = _agg_from_order_log(args.order_log, day_s)

    hist_paths = args.trade_history
    if hist_paths is None:
        hist_paths = sorted(glob.glob(os.path.join(base, "data", "trade_history*.csv")))
    hist_rows = _rows_from_trade_history(list(hist_paths or []), day_s)

    print(f"=== 체결 요약 ({day_s}) ===")
    print(f"order_event_log: {args.order_log}")
    if not buy_o and not sell_o:
        print("(ORDER_FILLED 체결이 이 날짜에 없습니다.)")
    else:
        if buy_o:
            print("\n[매수 ORDER_FILLED]")
            for code in sorted(buy_o.keys()):
                e = buy_o[code]
                print(f"  {code}  총 {e['qty']}주  평균 {e['avg_px']:.0f}원  ({e['fills']}번 체결)")
        if sell_o:
            print("\n[매도 ORDER_FILLED]")
            for code in sorted(sell_o.keys()):
                e = sell_o[code]
                print(f"  {code}  총 {e['qty']}주  평균 {e['avg_px']:.0f}원  ({e['fills']}번 체결)")

    if hist_rows:
        print(f"\n[trade_history 행 {len(hist_rows)}건 - 참고]")
        for code, side, qty, px, note in hist_rows:
            extra = f" ({note})" if note else ""
            print(f"  {code} {side} {qty}주 @ {px:.0f}{extra}")
    elif hist_paths:
        print(f"\n(trade_history: 당일 행 없음 또는 파일 없음)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
