# -*- coding: utf-8 -*-
"""
performance.py — 수익률·거래내역 집계 및 CSV 영속화

역할:
- 매수/매도 체결 레코드 저장·로드
- 전체 / 일별 / 종목별 수익률 요약
- 누적 손익·시간축 수익률 곡선용 시계열 생성

안정성:
- 데이터 없음·NaN·비정상 값은 안전하게 필터링
"""

from __future__ import annotations

import csv
import math
import os
import threading
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple


def _safe_float(x: Any, default: float = 0.0) -> float:
    """NaN/Inf 및 변환 실패 시 default 반환."""
    try:
        v = float(x)
        if not math.isfinite(v):
            return default
        return v
    except (TypeError, ValueError):
        return default


def _safe_int(x: Any, default: int = 0) -> int:
    try:
        return int(x)
    except (TypeError, ValueError):
        return default


def _parse_ts(s: str) -> Optional[datetime]:
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:19], fmt) if len(s) >= 19 else datetime.strptime(s[:10], fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass
class TradeRecord:
    """단일 체결(또는 기록) 행."""

    ts: datetime
    code: str
    side: str  # "BUY" | "SELL"
    price: float
    qty: int
    # 매도 시 실현 수익률(%). 매수는 None 또는 0.
    pnl_pct: Optional[float] = None
    # 실현 손익(원), 매도에서만 의미 있음
    realized_pnl_krw: float = 0.0
    # 매도 사유: stop_loss, ai_loss_exit, trailing_stop 등
    sell_reason: str = ""

    def to_row(self) -> Dict[str, str]:
        return {
            "timestamp": self.ts.strftime("%Y-%m-%dT%H:%M:%S"),
            "code": self.code,
            "side": self.side,
            "price": f"{self.price:.4f}",
            "qty": str(self.qty),
            "pnl_pct": "" if self.pnl_pct is None else f"{self.pnl_pct:.6f}",
            "realized_pnl_krw": f"{self.realized_pnl_krw:.2f}",
            "sell_reason": str(self.sell_reason or ""),
        }

    @staticmethod
    def from_row(row: Dict[str, str]) -> Optional["TradeRecord"]:
        ts = _parse_ts(row.get("timestamp", ""))
        if ts is None:
            return None
        code = str(row.get("code", "")).strip()
        if not code:
            return None
        side = str(row.get("side", "")).strip().upper()
        if side not in ("BUY", "SELL"):
            return None
        price = _safe_float(row.get("price"), 0.0)
        qty = _safe_int(row.get("qty"), 0)
        if qty <= 0 or price <= 0:
            return None
        pnl_raw = row.get("pnl_pct", "").strip()
        pnl_pct: Optional[float] = None
        if pnl_raw:
            pnl_pct = _safe_float(pnl_raw, float("nan"))
            if not math.isfinite(pnl_pct):
                pnl_pct = None
        realized = _safe_float(row.get("realized_pnl_krw", "0"), 0.0)
        sell_reason = str(row.get("sell_reason", "") or "").strip()
        return TradeRecord(
            ts=ts,
            code=code,
            side=side,
            price=price,
            qty=qty,
            pnl_pct=pnl_pct,
            realized_pnl_krw=realized,
            sell_reason=sell_reason,
        )


CSV_FIELDNAMES = [
    "timestamp",
    "code",
    "side",
    "price",
    "qty",
    "pnl_pct",
    "realized_pnl_krw",
    "sell_reason",
]


def filter_trade_records(
    records: List[TradeRecord],
    code: Optional[str] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
) -> List[TradeRecord]:
    """
    종목·기간으로 거래 로그 필터.
    - code: None 또는 빈 문자열이면 전체 종목
    - date_from / date_to: None이면 해당 축 필터 없음
    """
    code_c = str(code).strip() if code else ""
    out: List[TradeRecord] = []
    for r in records:
        if code_c and r.code != code_c:
            continue
        d = r.ts.date()
        if date_from is not None and d < date_from:
            continue
        if date_to is not None and d > date_to:
            continue
        out.append(r)
    return sorted(out, key=lambda x: x.ts)


def distinct_codes(records: Iterable[TradeRecord]) -> List[str]:
    """로그에 등장한 종목코드 정렬 목록."""
    s = {str(r.code).strip() for r in records if str(r.code).strip()}
    return sorted(s)


def _total_buy_cost(records: Iterable[TradeRecord]) -> float:
    return max(sum(r.price * r.qty for r in records if r.side == "BUY"), 0.0)


def _realized_cost_from_sells(records: Iterable[TradeRecord]) -> float:
    """매도 완료된 거래의 원가 합계. 미청산 매수분은 실현수익률 분모에서 제외."""
    cost = 0.0
    for r in records:
        if r.side != "SELL":
            continue
        sell_value = max(float(r.price) * int(r.qty), 0.0)
        cost += max(sell_value - float(r.realized_pnl_krw), 0.0)
    return max(cost, 0.0)


def _total_realized_pnl(records: Iterable[TradeRecord]) -> float:
    return sum(r.realized_pnl_krw for r in records if r.side == "SELL")


def _overall_return_pct(records: List[TradeRecord]) -> float:
    cost = _realized_cost_from_sells(records)
    if cost <= 0:
        return 0.0
    return (_total_realized_pnl(records) / cost) * 100.0


def _daily_pnl_krw(records: List[TradeRecord]) -> Dict[date, float]:
    out: Dict[date, float] = {}
    for r in records:
        if r.side != "SELL":
            continue
        d = r.ts.date()
        out[d] = out.get(d, 0.0) + r.realized_pnl_krw
    return out


def _daily_return_pct(records: List[TradeRecord]) -> Dict[date, float]:
    daily_realized: Dict[date, float] = {}
    daily_cost: Dict[date, float] = {}
    for r in records:
        if r.side == "SELL":
            d = r.ts.date()
            daily_realized[d] = daily_realized.get(d, 0.0) + r.realized_pnl_krw
            sell_value = max(float(r.price) * int(r.qty), 0.0)
            daily_cost[d] = daily_cost.get(d, 0.0) + max(sell_value - float(r.realized_pnl_krw), 0.0)
    out: Dict[date, float] = {}
    for d, pnl in daily_realized.items():
        denom = daily_cost.get(d, 0.0)
        out[d] = (pnl / denom) * 100.0
    return out


def _symbol_realized_pnl_krw(records: List[TradeRecord]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for r in records:
        if r.side != "SELL":
            continue
        c = r.code
        out[c] = out.get(c, 0.0) + r.realized_pnl_krw
    return out


def _symbol_return_pct(records: List[TradeRecord]) -> Dict[str, float]:
    cost_by: Dict[str, float] = {}
    for r in records:
        if r.side == "SELL":
            sell_value = max(float(r.price) * int(r.qty), 0.0)
            cost_by[r.code] = cost_by.get(r.code, 0.0) + max(
                sell_value - float(r.realized_pnl_krw),
                0.0,
            )
    sym_pnl = _symbol_realized_pnl_krw(records)
    out: Dict[str, float] = {}
    for code, pnl in sym_pnl.items():
        denom = max(cost_by.get(code, 0.0), 1e-9)
        out[code] = (pnl / denom) * 100.0
    return out


def _cumulative_realized_series(records: List[TradeRecord]) -> List[Tuple[datetime, float]]:
    out: List[Tuple[datetime, float]] = []
    cum = 0.0
    for r in sorted(records, key=lambda x: x.ts):
        if r.side == "SELL":
            cum += r.realized_pnl_krw
            out.append((r.ts, cum))
    return out


def _cumulative_return_pct_series(records: List[TradeRecord]) -> List[Tuple[datetime, float]]:
    series: List[Tuple[datetime, float]] = []
    cum = 0.0
    cum_cost = 0.0
    for r in sorted(records, key=lambda x: x.ts):
        if r.side == "SELL":
            cum += r.realized_pnl_krw
            sell_value = max(float(r.price) * int(r.qty), 0.0)
            cum_cost += max(sell_value - float(r.realized_pnl_krw), 0.0)
            series.append((r.ts, (cum / max(cum_cost, 1.0)) * 100.0))
    return series


def risk_metrics_from_records(records: List[TradeRecord]) -> Dict[str, float]:
    """매도 실현손익 기준 승률·손익비·MDD 등."""
    sells = [r for r in records if r.side == "SELL"]
    if not sells:
        return {
            "win_rate_pct": 0.0,
            "avg_profit": 0.0,
            "avg_loss": 0.0,
            "profit_loss_ratio": 0.0,
            "mdd_pct": 0.0,
        }

    pnls = [_safe_float(r.realized_pnl_krw, 0.0) for r in sells]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]

    win_rate = (len(wins) / len(pnls) * 100.0) if pnls else 0.0
    avg_profit = (sum(wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(losses) / len(losses)) if losses else 0.0
    if avg_loss < 0:
        pl_ratio = avg_profit / abs(avg_loss) if avg_profit > 0 else 0.0
    else:
        pl_ratio = 0.0

    cum = 0.0
    peak = 0.0
    mdd = 0.0
    for p in pnls:
        cum += p
        if cum > peak:
            peak = cum
        base = peak if peak > 0 else 1.0
        dd = (cum - peak) / base * 100.0
        if dd < mdd:
            mdd = dd

    return {
        "win_rate_pct": _safe_float(win_rate, 0.0),
        "avg_profit": _safe_float(avg_profit, 0.0),
        "avg_loss": _safe_float(avg_loss, 0.0),
        "profit_loss_ratio": _safe_float(pl_ratio, 0.0),
        "mdd_pct": _safe_float(mdd, 0.0),
    }


def summarize_records(records: List[TradeRecord]) -> Dict[str, Any]:
    """레코드 리스트에 대한 대시보드용 집계 (필터 결과에도 동일 적용)."""
    rs = sorted(records, key=lambda x: x.ts)
    risk = risk_metrics_from_records(rs)
    return {
        "overall_return_pct": _safe_float(_overall_return_pct(rs), 0.0),
        "total_buy_cost_krw": _safe_float(_total_buy_cost(rs), 0.0),
        "realized_cost_krw": _safe_float(_realized_cost_from_sells(rs), 0.0),
        "total_realized_pnl_krw": _safe_float(_total_realized_pnl(rs), 0.0),
        "trade_count": len(rs),
        "win_rate_pct": _safe_float(risk.get("win_rate_pct"), 0.0),
        "avg_profit": _safe_float(risk.get("avg_profit"), 0.0),
        "avg_loss": _safe_float(risk.get("avg_loss"), 0.0),
        "profit_loss_ratio": _safe_float(risk.get("profit_loss_ratio"), 0.0),
        "mdd_pct": _safe_float(risk.get("mdd_pct"), 0.0),
        "daily_pnl_krw": _daily_pnl_krw(rs),
        "daily_return_pct": _daily_return_pct(rs),
        "symbol_return_pct": _symbol_return_pct(rs),
        "cum_pnl_series": _cumulative_realized_series(rs),
        "cum_return_pct_series": _cumulative_return_pct_series(rs),
    }


def summarize_for_dashboard_filtered(
    tracker: Optional["TradeTracker"],
    code: Optional[str] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
) -> Dict[str, Any]:
    """종목·날짜 필터를 적용한 집계."""
    if tracker is None:
        return summarize_records([])
    try:
        raw = tracker.all_records()
    except Exception:
        raw = []
    try:
        filt = filter_trade_records(raw, code=code, date_from=date_from, date_to=date_to)
    except Exception:
        filt = []
    return summarize_records(filt)


class TradeTracker:
    """
    거래 내역 메모리 + CSV 저장.

    - append: 체결 시 호출
    - load/save: 재시작 시 복구
    - 요약·시계열: 대시보드용
    """

    def __init__(self, csv_path: Optional[str] = None) -> None:
        base = os.path.dirname(os.path.abspath(__file__))
        self.csv_path = csv_path or os.path.join(base, "data", "trade_history.csv")
        self.records: List[TradeRecord] = []
        self._lock = threading.Lock()

    def load(self) -> None:
        """CSV가 있으면 불러온다. 없거나 깨진 행은 건너뜀."""
        path = self.csv_path
        if not path or not os.path.isfile(path):
            return
        loaded: List[TradeRecord] = []
        try:
            with open(path, "r", encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                for raw in reader:
                    if not raw:
                        continue
                    rec = TradeRecord.from_row({k: raw.get(k, "") or "" for k in CSV_FIELDNAMES})
                    if rec is not None:
                        loaded.append(rec)
        except OSError:
            return
        with self._lock:
            self.records = sorted(loaded, key=lambda r: r.ts)

    def save(self) -> None:
        """메모리 내역을 CSV로 저장."""
        path = self.csv_path
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        except OSError:
            pass
        with self._lock:
            rows = list(self.records)
        try:
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES)
                w.writeheader()
                for r in rows:
                    w.writerow(r.to_row())
        except OSError:
            pass

    def append(
        self,
        code: str,
        side: str,
        price: float,
        qty: int,
        ts: Optional[datetime] = None,
        pnl_pct: Optional[float] = None,
        realized_pnl_krw: Optional[float] = None,
        sell_reason: str = "",
    ) -> None:
        """체결 1건 추가 (스레드 안전)."""
        price = _safe_float(price, 0.0)
        qty = _safe_int(qty, 0)
        if qty <= 0 or price <= 0:
            return
        side_u = str(side).strip().upper()
        if side_u not in ("BUY", "SELL"):
            return
        if pnl_pct is not None and not math.isfinite(_safe_float(pnl_pct, float("nan"))):
            pnl_pct = None
        if side_u == "BUY":
            pnl_pct = None
            realized = 0.0
        else:
            if realized_pnl_krw is None:
                # 수익률(%)과 매입가 역산: realized ≈ notional * pnl%/100
                # 매입 단가는 (price / (1 + pnl%/100)) 근사
                if pnl_pct is not None:
                    pc = _safe_float(pnl_pct, 0.0)
                    buy_approx = price / (1.0 + pc / 100.0) if (1.0 + pc / 100.0) != 0 else price
                    realized = (price - buy_approx) * qty
                else:
                    realized = 0.0
            else:
                realized = _safe_float(realized_pnl_krw, 0.0)

        rec = TradeRecord(
            ts=ts or datetime.now(),
            code=str(code).strip(),
            side=side_u,
            price=price,
            qty=qty,
            pnl_pct=pnl_pct,
            realized_pnl_krw=realized,
            sell_reason=str(sell_reason or "") if side_u == "SELL" else "",
        )
        with self._lock:
            self.records.append(rec)
            self.records.sort(key=lambda x: x.ts)
        self.save()

    def all_records(self) -> List[TradeRecord]:
        with self._lock:
            return list(self.records)

    # ---------- 집계 ----------

    def total_buy_cost_krw(self) -> float:
        """매수 체결에 사용한 금액 합(대략: 가격×수량)."""
        return _total_buy_cost(self.all_records())

    def total_realized_pnl_krw(self) -> float:
        """실현 손익 합계(매도 레코드의 realized)."""
        return _total_realized_pnl(self.all_records())

    def overall_return_pct(self) -> float:
        """
        전체 수익률(%): 실현손익 / 매수비용.
        매수 실적이 없으면 0.
        """
        return _overall_return_pct(list(self.all_records()))

    def daily_pnl_krw(self) -> Dict[date, float]:
        """일별 실현손익(원)."""
        return _daily_pnl_krw(list(self.all_records()))

    def daily_return_pct(self) -> Dict[date, float]:
        """
        일별 수익률(%): 해당 일의 실현손익 / (그 전까지 누적 매수비용).
        단순 근사(입금·추가증거금 미반영).
        """
        return _daily_return_pct(list(self.all_records()))

    def symbol_realized_pnl_krw(self) -> Dict[str, float]:
        """종목별 실현손익 합계."""
        return _symbol_realized_pnl_krw(list(self.all_records()))

    def symbol_return_pct(self) -> Dict[str, float]:
        """종목별 수익률(%): 해당 종목 매수비용 대비 실현손익."""
        return _symbol_return_pct(list(self.all_records()))

    # ---------- 차트용 시계열 ----------

    def cumulative_realized_series(self) -> List[Tuple[datetime, float]]:
        """시간 순 누적 실현손익(원)."""
        return _cumulative_realized_series(list(self.all_records()))

    def cumulative_return_pct_series(self) -> List[Tuple[datetime, float]]:
        """시간 순 누적 수익률(%): 누적실현 / 총매수비용."""
        return _cumulative_return_pct_series(list(self.all_records()))

    def risk_metrics(self) -> Dict[str, float]:
        """
        거래 로그(매도 실현손익) 기반 지표:
        - win_rate_pct: 승률(%)
        - avg_profit: 평균 수익(이익 거래 평균, 원)
        - avg_loss: 평균 손실(손실 거래 평균, 원, 음수)
        - profit_loss_ratio: 손익비(평균수익 / |평균손실|)
        - mdd_pct: 최대 낙폭(MDD, %, 음수)
        """
        return risk_metrics_from_records(list(self.all_records()))


def summarize_for_dashboard(tracker: TradeTracker) -> Dict[str, Any]:
    """대시보드 라벨/테이블용 요약 dict (필터 없음 = 전체)."""
    return summarize_records(tracker.all_records())
