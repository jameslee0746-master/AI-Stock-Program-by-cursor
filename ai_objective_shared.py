# -*- coding: utf-8 -*-
"""
매수 AI 학습 라벨과 멀티전략 평가 시뮬이 동일한 청산·비용 규칙을 쓰도록 공유합니다.

설정값은 가능하면 main.py 사용자 구간 상수와 같게 유지하고,
전략 모듈은 from_env()로 동일 변수명의 환경변수 오버레이를 읽습니다.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import List, Tuple


def _truthy_env(raw: str) -> bool:
    return str(raw or "").strip().lower() not in ("0", "false", "no", "off", "")


@dataclass(frozen=True)
class AiObjectiveConfig:
    """실전 학습 라벨과 동일한 스탑/익절·유한 호라이즌·왕복비용 규격."""

    label_horizon_days: int
    atr_period: int
    use_atr_risk: bool
    stop_loss_pct: float
    take_profit_pct: float
    atr_stop_mult: float
    atr_take_mult: float
    atr_stop_min_pct: float
    atr_stop_max_pct: float
    atr_take_min_pct: float
    atr_take_max_pct: float
    round_trip_cost_pct: float
    slippage_pct: float

    @staticmethod
    def from_env() -> AiObjectiveConfig:
        return AiObjectiveConfig(
            label_horizon_days=int(os.environ.get("AI_LABEL_HORIZON_DAYS", "3")),
            atr_period=int(os.environ.get("ATR_PERIOD", "14")),
            use_atr_risk=_truthy_env(os.environ.get("USE_ATR_RISK", "1")),
            stop_loss_pct=float(os.environ.get("STOP_LOSS_PCT", "-0.015")),
            take_profit_pct=float(os.environ.get("TAKE_PROFIT_PCT", "0.045")),
            atr_stop_mult=float(os.environ.get("ATR_STOP_MULT", "1.2")),
            atr_take_mult=float(os.environ.get("ATR_TAKE_MULT", "2.4")),
            atr_stop_min_pct=float(os.environ.get("ATR_STOP_MIN_PCT", "0.012")),
            atr_stop_max_pct=float(os.environ.get("ATR_STOP_MAX_PCT", "0.035")),
            atr_take_min_pct=float(os.environ.get("ATR_TAKE_MIN_PCT", "0.035")),
            atr_take_max_pct=float(os.environ.get("ATR_TAKE_MAX_PCT", "0.080")),
            round_trip_cost_pct=float(os.environ.get("AI_ROUND_TRIP_COST_PCT", "0.0030")),
            slippage_pct=float(os.environ.get("AI_SLIPPAGE_PCT", "0.0015")),
        )


def compute_atr_pct(
    highs: List[float],
    lows: List[float],
    closes: List[float],
    period: int,
) -> float:
    n = min(len(highs), len(lows), len(closes))
    if n < max(2, int(period) + 1):
        return 0.0
    trs: List[float] = []
    for i in range(1, n):
        high = float(highs[i])
        low = float(lows[i])
        prev_close = float(closes[i - 1])
        if high <= 0 or low <= 0 or prev_close <= 0:
            continue
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    if not trs:
        return 0.0
    atr = sum(trs[-int(period) :]) / float(min(len(trs), int(period)))
    last_close = float(closes[n - 1])
    return (atr / last_close) if last_close > 0 else 0.0


def stop_take_abs_from_atr(
    atr_pct: float,
    cfg: AiObjectiveConfig,
) -> Tuple[float, float]:
    """스탑/익절 폭을 매수 학습 규격과 같은 방식으로 산출(단기 비율)."""
    if cfg.use_atr_risk and atr_pct > 0:
        stop_abs = min(
            float(cfg.atr_stop_max_pct),
            max(float(cfg.atr_stop_min_pct), atr_pct * float(cfg.atr_stop_mult)),
        )
        take_abs = min(
            float(cfg.atr_take_max_pct),
            max(float(cfg.atr_take_min_pct), atr_pct * float(cfg.atr_take_mult)),
        )
        return stop_abs, take_abs
    return abs(float(cfg.stop_loss_pct)), abs(float(cfg.take_profit_pct))


def resolve_exit_price(
    closes: List[float],
    highs: List[float],
    lows: List[float],
    t_entry: int,
    atr_pct: float,
    cfg: AiObjectiveConfig,
) -> float:
    """
    진입일 t_entry 종가 기준, 이후 horizon 구간에서 메인 학습과 동일한 바-바 청산.
    intraday: 저가가 손절선 먼저, 그다음 고가가 익절선(학습 루프와 동일 순서).
    """
    horizon = max(1, int(cfg.label_horizon_days))
    entry = float(closes[t_entry])
    stop_abs, take_abs = stop_take_abs_from_atr(atr_pct, cfg)
    stop_price = entry * (1.0 - stop_abs)
    take_price = entry * (1.0 + take_abs)
    last_idx = min(len(closes) - 1, t_entry + horizon)
    exit_price = float(closes[last_idx])
    for j in range(t_entry + 1, min(len(closes), t_entry + horizon + 1)):
        if float(lows[j]) <= stop_price:
            exit_price = stop_price
            break
        if float(highs[j]) >= take_price:
            exit_price = take_price
            break
    return exit_price


def net_return_after_costs(entry: float, exit_price: float, cfg: AiObjectiveConfig) -> float:
    if entry <= 0:
        return 0.0
    return (exit_price / entry - 1.0) - float(cfg.round_trip_cost_pct) - float(cfg.slippage_pct)
