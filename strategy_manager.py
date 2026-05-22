# -*- coding: utf-8 -*-
"""
strategy_manager.py — 멀티 전략 비교·성과 수집·AI(RandomForest) 선택

구성
----
- 3개 전략: trend(MA 골든크로스 등), volume(거래량 강세), ai(추세+거래량 복합, 실전 ML과 유사)
- yfinance OHLC 일봉으로 종목당 시뮬; **청산 규격은 매수 AI 라벨과 동일**(ai_objective_shared: 호라이즌 스탑/익절·왕복비용)
- 점수: score = 수익률(%) - STRATEGY_MDD_WEIGHT * |MDD(%)|  (기본 가중 0.5)

시장 국면(상승/하락/횡보)
------------------------
- KOSPI(^KS11) 등 지수 일봉으로 모멘텀·MA20 이격·MA20 기울기를 결합해 국면 분류.
- 지수 실패 시: 이번 평가에 로드된 종목들의 **동일 길이 구간 등가 지수(종가 평균)** 사용.
- 국면별로 전략 score에 소폭 바이어스를 더해 규칙/라벨 기반 선택에 반영.
- RandomForest 입력 벡터 끝에 국면 원-핫 + 연속 지표(6차원)를 붙임.

AI 전략 선택
------------
- strategy_ai_selector.StrategyAISelector 가 RandomForestClassifier로 다음 구간 전략 예측
- 학습 행이 MIN_TRAINING_SAMPLES(기본 10) 미만이면 **활성 전략 변경 없음**
- OHLCV 를 한 종목도 못 불러오면 **이번 주기 전략 변경 없음** (데이터 부족)

주기
----
- STRATEGY_EVAL_INTERVAL_SEC (기본 6시간). 하루 1회로 쓰려면 86400 으로 설정.
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    import yfinance as yf  # type: ignore
except Exception:
    yf = None  # type: ignore

from strategy_ai_selector import StrategyAISelector

import ai_objective_shared as aos

STRATEGY_OBJECTIVE_CFG = aos.AiObjectiveConfig.from_env()

# 전략 재평가 주기(초). 하루 1회: STRATEGY_EVAL_INTERVAL_SEC=86400
STRATEGY_EVAL_INTERVAL_SEC = int(os.environ.get("STRATEGY_EVAL_INTERVAL_SEC", str(6 * 3600)))
STRATEGY_EVAL_LOOKBACK = int(os.environ.get("STRATEGY_EVAL_LOOKBACK", "50"))
STRATEGY_EVAL_MAX_CODES = int(os.environ.get("STRATEGY_EVAL_MAX_CODES", "5"))
STRATEGY_MDD_WEIGHT = float(os.environ.get("STRATEGY_MDD_WEIGHT", "0.5"))
STRATEGY_DATA_DIR = os.environ.get(
    "STRATEGY_DATA_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "strategy_data"),
)

STRATEGY_TREND = "trend"
STRATEGY_VOLUME = "volume"
STRATEGY_AI = "ai"
# 실전 엔진(scan 로테이션+시총허용종목) 전용: 전략 선택기(StrategyAISelector)에는 포함하지 않음.
STRATEGY_QUALITY_DIP = "quality_dip"
STRATEGY_IDS = (STRATEGY_TREND, STRATEGY_VOLUME, STRATEGY_AI)

# 시장 지수(콤마 구분 시 순차 시도). 기본: 코스피 → 코스닥
_STRATEGY_MARKET_INDEX_RAW = os.environ.get("STRATEGY_MARKET_INDEX", "^KS11,^KQ11")
STRATEGY_MARKET_INDEX_CANDIDATES: Tuple[str, ...] = tuple(
    x.strip() for x in _STRATEGY_MARKET_INDEX_RAW.split(",") if x.strip()
)

# 국면 분류: 모멘텀 = 0.45*ret20 + 0.35*dist_ma20 + 0.20*ma20_slope(%p)
MARKET_REGIME_MOMENTUM_BULL = float(os.environ.get("MARKET_REGIME_MOMENTUM_BULL", "1.2"))
MARKET_REGIME_MOMENTUM_BEAR = float(os.environ.get("MARKET_REGIME_MOMENTUM_BEAR", "-1.2"))


def _safe_float(x: object, default: float = 0.0) -> float:
    try:
        v = float(x)
        return v if math.isfinite(v) else default
    except (TypeError, ValueError):
        return default


def _to_yf_ticker(code: str) -> List[str]:
    s = str(code).strip()
    if not s:
        return []
    if s.endswith(".KS") or s.endswith(".KQ"):
        return [s]
    if len(s) == 6 and s.isdigit():
        return [f"{s}.KS", f"{s}.KQ"]
    return [s]


def _load_ohlcv(code: str) -> Optional[Tuple[List[float], List[float], List[float], List[float]]]:
    """yfinance로 종가·고가·저가·거래량 시계열 (오래된→최신). 실패 시 None."""
    if yf is None:
        return None
    for ticker in _to_yf_ticker(code):
        try:
            df = yf.download(
                ticker,
                period="6mo",
                interval="1d",
                auto_adjust=False,
                progress=False,
                threads=False,
            )
            if df is None or getattr(df, "empty", True):
                continue
            need = ("Close", "High", "Low", "Volume")
            if not all(col in df.columns for col in need):
                continue
            sub = df[list(need)].dropna()
            closes = [_safe_float(x, float("nan")) for x in sub["Close"].tolist()]
            highs = [_safe_float(x, float("nan")) for x in sub["High"].tolist()]
            lows = [_safe_float(x, float("nan")) for x in sub["Low"].tolist()]
            vols = [_safe_float(x, float("nan")) for x in sub["Volume"].tolist()]
            out_c: List[float] = []
            out_h: List[float] = []
            out_l: List[float] = []
            out_v: List[float] = []
            for c0, h0, lo0, v0 in zip(closes, highs, lows, vols):
                if not (
                    c0 == c0 and h0 == h0 and lo0 == lo0 and v0 == v0 and c0 > 0 and h0 > 0 and lo0 > 0 and v0 >= 0
                ):
                    continue
                out_c.append(float(c0))
                out_h.append(float(h0))
                out_l.append(float(lo0))
                out_v.append(float(v0))
            if len(out_c) < STRATEGY_EVAL_LOOKBACK:
                continue
            return out_c, out_h, out_l, out_v
        except Exception:
            continue
    return None


def _mdd_from_equity(equity: List[float]) -> float:
    """MDD(%), 음수."""
    if len(equity) < 2:
        return 0.0
    peak = equity[0]
    mdd = 0.0
    for x in equity:
        if x > peak:
            peak = x
        if peak > 0:
            dd = (x - peak) / peak * 100.0
            if dd < mdd:
                mdd = dd
    return mdd


def _simulate_strategy(
    strategy_id: str,
    closes: List[float],
    highs: List[float],
    lows: List[float],
    vols: List[float],
    start_idx: int = 20,
) -> Tuple[float, float, float, float]:
    """
    단일 종목·단일 전략 시뮬.

    Returns
    -------
    (누적수익률%, MDD%, 승률 0~1, 거래(신호) 횟수)

    신호 발생 시 매수 AI 학습 라벨과 같은 방식(ai_objective_shared)으로
    진입종가 대비 호라이즌 내 스탑/익절 청산·왕복비용을 반영해 구간 손익률 합산.
    """
    n = min(len(closes), len(highs), len(lows), len(vols))
    if n < start_idx + 2:
        return 0.0, 0.0, 0.0, 0.0
    closes = closes[:n]
    highs = highs[:n]
    lows = lows[:n]
    vols = vols[:n]
    cfg = STRATEGY_OBJECTIVE_CFG

    equity: List[float] = [1.0]

    def signal_trend(t: int) -> bool:
        if t < 20:
            return False
        ma5 = sum(closes[t - 4 : t + 1]) / 5.0
        ma20 = sum(closes[t - 19 : t + 1]) / 20.0
        ma5p = sum(closes[t - 5 : t]) / 5.0
        ma20p = sum(closes[t - 20 : t]) / 20.0
        if not (ma5p <= ma20p and ma5 > ma20):
            return False
        c = closes[t]
        if c < ma20 * 1.0015:
            return False
        if ma20 > 0 and (c / ma20 - 1.0) > 0.15:
            return False
        return True

    def signal_volume(t: int) -> bool:
        if t < 20:
            return False
        ma5 = sum(closes[t - 4 : t + 1]) / 5.0
        ma20 = sum(closes[t - 19 : t + 1]) / 20.0
        if ma5 <= ma20:
            return False
        c = closes[t]
        if c < ma20 * 1.0015:
            return False
        vol3 = sum(vols[t - 2 : t + 1]) / 3.0
        vol20 = sum(vols[t - 19 : t + 1]) / 20.0
        if vol20 <= 0 or vol3 <= vol20 * 1.25:
            return False
        return True

    def signal_ai(t: int) -> bool:
        """평가용 복합 신호(실전 sklearn 진입과 유사한 ‘보수적’ 조건)."""
        return signal_trend(t) and signal_volume(t)

    fn = {
        STRATEGY_TREND: signal_trend,
        STRATEGY_VOLUME: signal_volume,
        STRATEGY_AI: signal_ai,
    }.get(strategy_id, signal_trend)

    eq = 1.0
    wins = 0
    trades = 0
    hmax = max(1, int(cfg.label_horizon_days))
    for t in range(start_idx, n):
        if t + hmax >= n:
            break
        if not fn(t):
            equity.append(eq)
            continue
        entry = float(closes[t])
        if entry <= 0:
            equity.append(eq)
            continue
        atr_pct = aos.compute_atr_pct(highs[: t + 1], lows[: t + 1], closes[: t + 1], int(cfg.atr_period))
        exit_px = aos.resolve_exit_price(closes, highs, lows, t, atr_pct, cfg)
        net_r = aos.net_return_after_costs(entry, exit_px, cfg)
        trades += 1
        eq *= 1.0 + net_r
        if net_r > 0:
            wins += 1
        equity.append(eq)

    total_ret_pct = (eq - 1.0) * 100.0
    mdd_pct = _mdd_from_equity(equity)
    win_rate = (wins / trades) if trades > 0 else 0.0
    return total_ret_pct, mdd_pct, win_rate, float(trades)


def compute_score(return_pct: float, mdd_pct: float) -> float:
    """score = 수익률 - w * |MDD| (MDD는 보통 음수이므로 크기만 패널티)."""
    return float(return_pct) - STRATEGY_MDD_WEIGHT * abs(float(mdd_pct))


@dataclass
class MarketRegimeSnapshot:
    """
    시장 국면 스냅샷.

    - label: bull | bear | range (내부 키)
    - label_ko: 로그/GUI용 한글
    - ret_20d_pct: 최근 약 20거래일 누적 수익률(%)
    - dist_ma20_pct: 종가가 MA20 대비 위/아래 이격(%)
    - ma20_slope_pctp: 전일 대비 MA20 변화율(%p) — 양수면 MA20 우상향
    - vol_20d_pctp: 최근 20일 일간 수익률의 표준편차(%p) — 변동성 프록시
    """

    label: str
    label_ko: str
    ret_20d_pct: float
    dist_ma20_pct: float
    ma20_slope_pctp: float
    vol_20d_pctp: float
    source: str = ""  # "index:^KS11" | "composite:N종목"

    def to_feature_list(self) -> List[float]:
        """strategy_ai_selector 의 시장 6특징과 순서 일치."""
        b = 1.0 if self.label == "bull" else 0.0
        be = 1.0 if self.label == "bear" else 0.0
        r = 1.0 if self.label == "range" else 0.0
        return [
            b,
            be,
            r,
            self.ret_20d_pct,
            self.dist_ma20_pct,
            self.vol_20d_pctp,
        ]


def _std_sample(values: List[float]) -> float:
    if len(values) < 2:
        return 0.0
    m = sum(values) / len(values)
    v = sum((x - m) ** 2 for x in values) / len(values)
    return math.sqrt(v)


def analyze_market_regime(closes: List[float]) -> MarketRegimeSnapshot:
    """
    종가 시계열(오래된→최신)로 상승/하락/횡보를 구분.

    모멘텀 지표(%)에 임계값을 적용:
    - >= MARKET_REGIME_MOMENTUM_BULL → 상승
    - <= MARKET_REGIME_MOMENTUM_BEAR → 하락
    - 그 사이 → 횡보
    """
    n = len(closes)
    if n < 25:
        return MarketRegimeSnapshot(
            label="range",
            label_ko="횡보(데이터부족)",
            ret_20d_pct=0.0,
            dist_ma20_pct=0.0,
            ma20_slope_pctp=0.0,
            vol_20d_pctp=0.0,
            source="",
        )

    c = closes[-1]
    ma20 = sum(closes[-20:]) / 20.0
    ma20_prev = sum(closes[-21:-1]) / 20.0
    slope = ((ma20 - ma20_prev) / ma20_prev * 100.0) if ma20_prev > 0 else 0.0
    base = closes[-21]
    ret20 = ((c / base - 1.0) * 100.0) if base > 0 else 0.0
    dist = ((c / ma20 - 1.0) * 100.0) if ma20 > 0 else 0.0

    daily_rets: List[float] = []
    for i in range(max(1, n - 20), n):
        p0, p1 = closes[i - 1], closes[i]
        if p0 > 0:
            daily_rets.append((p1 / p0 - 1.0) * 100.0)
    vol20 = _std_sample(daily_rets)

    momentum = 0.45 * ret20 + 0.35 * dist + 0.20 * slope
    if momentum >= MARKET_REGIME_MOMENTUM_BULL:
        lab, ko = "bull", "상승"
    elif momentum <= MARKET_REGIME_MOMENTUM_BEAR:
        lab, ko = "bear", "하락"
    else:
        lab, ko = "range", "횡보"

    return MarketRegimeSnapshot(
        label=lab,
        label_ko=ko,
        ret_20d_pct=ret20,
        dist_ma20_pct=dist,
        ma20_slope_pctp=slope,
        vol_20d_pctp=vol20,
        source="",
    )


def _composite_closes_from_loaded(
    loaded: Dict[str, Tuple[List[float], List[float], List[float], List[float]]],
) -> Optional[Tuple[List[float], str]]:
    """로드된 종목들의 공통 구간 종가 평균(등가 지수)."""
    arrays = [list(row[0]) for row in loaded.values()]
    if not arrays:
        return None
    ln = min(len(a) for a in arrays)
    if ln < STRATEGY_EVAL_LOOKBACK:
        return None
    # 뒤에서 ln개 맞춤(시계열 끝 정렬)
    tail = [a[-ln:] for a in arrays]
    comp: List[float] = []
    for t in range(ln):
        comp.append(sum(row[t] for row in tail) / len(tail))
    return comp, f"composite:{len(tail)}종목"


def _load_benchmark_closes() -> Optional[Tuple[List[float], str]]:
    """환경변수 후보 지수 중 첫 성공 종가열."""
    for sym in STRATEGY_MARKET_INDEX_CANDIDATES:
        data = _load_ohlcv(sym)
        if data is None:
            continue
        c, _h, _l, _v = data
        if len(c) >= STRATEGY_EVAL_LOOKBACK:
            return c, f"index:{sym}"
    return None


def _regime_score_bonus(regime_label: str, strategy_id: str) -> float:
    """
    국면별 전략 점수 가산(소폭). RF/규칙 공통으로 쓰는 조정치.

    - 상승: 추세 추종(trend)·거래량 동반(volume)에 유리
    - 하락: 거래량(volume) 가중, 순추세(trend)는 불리
    - 횡보: 변동성·거래량 이벤트(volume)에 상대적 가중
    """
    table: Dict[str, Dict[str, float]] = {
        "bull": {
            STRATEGY_TREND: 0.35,
            STRATEGY_VOLUME: 0.20,
            STRATEGY_AI: 0.08,
        },
        "bear": {
            STRATEGY_TREND: -0.35,
            STRATEGY_VOLUME: 0.45,
            STRATEGY_AI: 0.10,
        },
        "range": {
            STRATEGY_TREND: -0.15,
            STRATEGY_VOLUME: 0.40,
            STRATEGY_AI: 0.05,
        },
    }
    return float(table.get(regime_label, {}).get(strategy_id, 0.0))


def _apply_regime_to_records(
    snapshots: Dict[str, StrategyRecord],
    regime: MarketRegimeSnapshot,
) -> Dict[str, StrategyRecord]:
    """원본 기록은 유지용으로 두고, 선택 전용으로 score만 가산한 복사본."""
    out: Dict[str, StrategyRecord] = {}
    for sid, rec in snapshots.items():
        bonus = _regime_score_bonus(regime.label, sid)
        out[sid] = replace(rec, score=float(rec.score) + bonus)
    return out


@dataclass
class StrategyRecord:
    """전략 한 번 평가 시 집계된 성과 스냅샷."""

    return_pct: float
    mdd_pct: float
    win_rate: float
    trade_count: float
    score: float
    evaluated_at: float = field(default_factory=time.time)


class StrategyManager:
    """
    - 주기마다 3전략 성과 산출
    - AI 선택기로 활성 전략 결정 (학습 샘플 부족 시 변경 안 함)
    """

    def __init__(
        self,
        interval_sec: int = STRATEGY_EVAL_INTERVAL_SEC,
        emit_log: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.interval_sec = max(60, int(interval_sec))
        self._emit_log = emit_log
        self._lock = threading.Lock()
        self._eval_thread_running = False
        self._last_eval_ts = 0.0
        # 실전 기본: 기존 ML 파이프라인(ai) — AI 선택기가 샘플 쌓기 전까지 유지
        self.active_strategy_id: str = STRATEGY_AI
        self.last_records: Dict[str, StrategyRecord] = {}
        self._history: Dict[str, List[StrategyRecord]] = {k: [] for k in STRATEGY_IDS}
        self._selector = StrategyAISelector(STRATEGY_IDS)
        self.last_ai_reason: str = ""
        self.last_market_regime: Optional[MarketRegimeSnapshot] = None
        self._data_dir = STRATEGY_DATA_DIR
        self._eval_history_path = os.path.join(self._data_dir, "strategy_eval_history.jsonl")
        self._training_rows_path = os.path.join(self._data_dir, "strategy_training_rows.jsonl")
        self._validation_report_path = os.path.join(self._data_dir, "strategy_validation_report.json")
        self._load_selector_training_rows()

    def set_emit_log(self, fn: Optional[Callable[[str], None]]) -> None:
        self._emit_log = fn

    def _log(self, msg: str) -> None:
        if self._emit_log:
            try:
                self._emit_log(msg)
            except Exception:
                pass

    def _ensure_data_dir(self) -> None:
        try:
            os.makedirs(self._data_dir, exist_ok=True)
        except OSError:
            pass

    def _append_jsonl(self, path: str, row: Dict[str, Any]) -> None:
        self._ensure_data_dir()
        try:
            with open(path, "a", encoding="utf-8", newline="") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _write_json(self, path: str, payload: Dict[str, Any]) -> None:
        self._ensure_data_dir()
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def _load_selector_training_rows(self) -> None:
        path = self._training_rows_path
        if not os.path.isfile(path):
            return
        rows: List[Tuple[List[float], int]] = []
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        raw = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    x = raw.get("x")
                    y = raw.get("y")
                    if isinstance(x, list) and y is not None:
                        rows.append(([float(v) for v in x], int(y)))
        except (OSError, TypeError, ValueError):
            return
        loaded = self._selector.load_training_rows(rows)
        if loaded:
            self._log(f"[STRAT] 저장된 AI학습행 로드: {loaded}개")

    def _record_to_dict(self, rec: StrategyRecord) -> Dict[str, float]:
        return {
            "return_pct": float(rec.return_pct),
            "mdd_pct": float(rec.mdd_pct),
            "win_rate": float(rec.win_rate),
            "trade_count": float(rec.trade_count),
            "score": float(rec.score),
            "evaluated_at": float(rec.evaluated_at),
        }

    def _market_to_dict(self, regime: MarketRegimeSnapshot) -> Dict[str, Any]:
        return {
            "label": regime.label,
            "label_ko": regime.label_ko,
            "ret_20d_pct": float(regime.ret_20d_pct),
            "dist_ma20_pct": float(regime.dist_ma20_pct),
            "ma20_slope_pctp": float(regime.ma20_slope_pctp),
            "vol_20d_pctp": float(regime.vol_20d_pctp),
            "source": regime.source,
        }

    def _persist_evaluation(
        self,
        codes: List[str],
        loaded_count: int,
        active_before: str,
        chosen: Optional[str],
        snapshots: Dict[str, StrategyRecord],
        records_for_selection: Dict[str, StrategyRecord],
        regime: MarketRegimeSnapshot,
    ) -> None:
        active_after = chosen or active_before
        coverage = loaded_count / float(max(1, len(codes)))
        row = {
            "ts": time.time(),
            "codes": list(codes[:STRATEGY_EVAL_MAX_CODES]),
            "active_before": active_before,
            "active_after": active_after,
            "chosen": chosen,
            "ai_reason": self.last_ai_reason,
            "training_rows": self._selector.training_sample_count,
            "market": self._market_to_dict(regime),
            "data_quality": {
                "requested_codes": float(len(codes[:STRATEGY_EVAL_MAX_CODES])),
                "loaded_codes": float(loaded_count),
                "coverage_ratio": float(coverage),
            },
            "records": {k: self._record_to_dict(v) for k, v in snapshots.items()},
            "records_for_selection": {
                k: {"score": float(v.score)} for k, v in records_for_selection.items()
            },
        }
        self._append_jsonl(self._eval_history_path, row)

        added = self._selector.last_added_training_row
        if added is not None:
            x, y = added
            self._append_jsonl(
                self._training_rows_path,
                {
                    "ts": time.time(),
                    "market_label": regime.label,
                    "x": [float(v) for v in x],
                    "y": int(y),
                },
            )

        scores = [float(r.score) for r in records_for_selection.values()]
        best_score = max(scores) if scores else 0.0
        active_rec = records_for_selection.get(active_after)
        active_score = float(active_rec.score) if active_rec is not None else 0.0
        self._write_json(
            self._validation_report_path,
            {
                "generated_at": time.time(),
                "window_size": 1,
                "active_strategy": active_after,
                "ai_reason": self.last_ai_reason,
                "avg_active_score": active_score,
                "avg_best_score": best_score,
                "avg_score_gap_to_best": best_score - active_score,
                "avg_data_coverage_ratio": float(coverage),
                "training_rows": self._selector.training_sample_count,
                "min_training_samples": self._selector.min_samples,
                "market": self._market_to_dict(regime),
            },
        )

    def schedule_reevaluation(self, stock_codes: List[str], force: bool = False) -> None:
        """시장 루프에서 호출. 주기·중복 실행 방지 후 백그라운드 평가."""
        codes = [str(c).strip() for c in (stock_codes or []) if str(c).strip()]
        if not codes:
            return
        now = time.time()
        with self._lock:
            if self._eval_thread_running:
                return
            if (
                not force
                and self._last_eval_ts > 0
                and (now - self._last_eval_ts) < self.interval_sec
            ):
                return
            self._eval_thread_running = True

        def _run() -> None:
            try:
                self._run_evaluation(codes)
            except Exception as e:
                self._log(f"[STRAT] 평가 오류: {e}")
            finally:
                with self._lock:
                    self._eval_thread_running = False
                self._last_eval_ts = time.time()

        threading.Thread(target=_run, daemon=True).start()

    def _run_evaluation(self, codes: List[str]) -> None:
        self._log(
            f"[STRAT] 멀티전략 평가 시작 (표본 종목 최대 {STRATEGY_EVAL_MAX_CODES}개, "
            f"AI학습행={self._selector.training_sample_count})"
        )

        # 종목당 yfinance 호출 1회만(3전략 동일 데이터 재사용)
        loaded: Dict[str, Tuple[List[float], List[float], List[float], List[float]]] = {}
        for code in codes[:STRATEGY_EVAL_MAX_CODES]:
            data = _load_ohlcv(code)
            if data is not None:
                loaded[code] = data

        if not loaded:
            self._log("[STRAT] 유효 OHLCV 없음 — 성과 스냅샷·전략 변경 생략 (활성 전략 유지)")
            return

        snapshots: Dict[str, StrategyRecord] = {}

        for sid in STRATEGY_IDS:
            rets: List[float] = []
            mdds: List[float] = []
            wrs: List[float] = []
            tcs: List[float] = []
            for _code, row in loaded.items():
                c, h, low, vol = row
                rp, md, wr, tc = _simulate_strategy(sid, c, h, low, vol)
                rets.append(rp)
                mdds.append(md)
                wrs.append(wr)
                tcs.append(tc)
            k = len(rets)
            ret_p = sum(rets) / k
            mdd_p = sum(mdds) / k
            wr = sum(wrs) / k
            tc = sum(tcs) / k
            sc = compute_score(ret_p, mdd_p)
            rec = StrategyRecord(
                return_pct=ret_p,
                mdd_pct=mdd_p,
                win_rate=wr,
                trade_count=tc,
                score=sc,
            )
            snapshots[sid] = rec
            self._history[sid].append(rec)
            if len(self._history[sid]) > 30:
                self._history[sid] = self._history[sid][-30:]
            self._log(
                f"[STRAT] {sid}: R={ret_p:+.2f}% WR={wr*100:.1f}% "
                f"MDD={mdd_p:.2f}% Trades={tc:.1f} Score={sc:.2f}"
            )

        self.last_records = snapshots

        # ---------- 시장 국면: 지수 우선, 실패 시 표본 종목 등가 지수 ----------
        regime: MarketRegimeSnapshot
        bench = _load_benchmark_closes()
        if bench is not None:
            bc, desc = bench
            regime = replace(analyze_market_regime(bc), source=desc)
        else:
            comp = _composite_closes_from_loaded(loaded)
            if comp is not None:
                cc, desc = comp
                regime = replace(analyze_market_regime(cc), source=desc)
            else:
                regime = MarketRegimeSnapshot(
                    label="range",
                    label_ko="횡보(산출불가)",
                    ret_20d_pct=0.0,
                    dist_ma20_pct=0.0,
                    ma20_slope_pctp=0.0,
                    vol_20d_pctp=0.0,
                    source="none",
                )
        self.last_market_regime = regime
        mom = (
            0.45 * regime.ret_20d_pct
            + 0.35 * regime.dist_ma20_pct
            + 0.20 * regime.ma20_slope_pctp
        )
        self._log(
            f"[STRAT] 시장 {regime.label_ko} mom={mom:+.2f} "
            f"(임계 상승≥{MARKET_REGIME_MOMENTUM_BULL} 하락≤{MARKET_REGIME_MOMENTUM_BEAR}) "
            f"R20={regime.ret_20d_pct:+.2f}% MA이격={regime.dist_ma20_pct:+.2f}% "
            f"MA20슬로프={regime.ma20_slope_pctp:+.2f}%p 변동성σ={regime.vol_20d_pctp:.2f}%p "
            f"[{regime.source}]"
        )

        # 규칙/RF 라벨 모두 국면 반영 score 사용 (원본 지표는 last_records에 유지)
        records_for_selection = _apply_regime_to_records(snapshots, regime)
        market_feats = regime.to_feature_list()

        prev = self.active_strategy_id
        chosen = self._selector.process_evaluation(records_for_selection, market_feats)
        if self._selector.last_buffer_reset_reason:
            self._log(
                f"[STRAT] AI학습 버퍼 초기화: {self._selector.last_buffer_reset_reason}"
            )
            self._selector.last_buffer_reset_reason = ""
        self.last_ai_reason = self._selector.last_decision_reason
        self._persist_evaluation(
            codes=codes,
            loaded_count=len(loaded),
            active_before=prev,
            chosen=chosen,
            snapshots=snapshots,
            records_for_selection=records_for_selection,
            regime=regime,
        )

        if chosen is None:
            self._log(
                f"[STRAT] AI 선택 보류({self.last_ai_reason}) — 활성 전략 유지: {prev}"
            )
            return

        self.active_strategy_id = chosen
        if prev != chosen:
            self._log(
                f"[STRAT] 전략 전환: {prev} → {chosen} (방식={self.last_ai_reason})"
            )
        else:
            self._log(f"[STRAT] 활성 유지: {chosen} (방식={self.last_ai_reason})")

    def summary_line(self) -> str:
        """GUI용 한 줄 요약."""
        r = self.last_records.get(self.active_strategy_id)
        ai_tag = self.last_ai_reason or "-"
        mr = self.last_market_regime
        mtxt = mr.label_ko if mr else "-"
        if r is None:
            return f"전략: {self.active_strategy_id} | 시장:{mtxt} | AI:{ai_tag}"
        return (
            f"전략: {self.active_strategy_id} "
            f"R{r.return_pct:+.1f}% WR{r.win_rate*100:.0f}% "
            f"MDD{r.mdd_pct:.1f} S{r.score:.1f} | 시장:{mtxt} | {ai_tag}"
        )
