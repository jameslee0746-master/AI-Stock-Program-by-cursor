# -*- coding: utf-8 -*-
"""
strategy_ai_selector.py — RandomForest 기반 전략 선택기

역할
----
- 각 전략 평가 주기마다 수집된 성과 지표(수익률, 승률, MDD, 거래 횟수, 점수)와
  **시장 국면 특징**(상승/하락/횡보 원-핫 + 연속 지표)을 묶어 RF에 입력합니다.

학습 라벨
---------
- t-1 시점 특징 X_{t-1}에 대해, t 시점에서 **점수(score)가 가장 높은 전략**을 라벨로 저장.

특징 차원
---------
- 전략 3 × 5 = 15차원 + 시장 6차원 = 21차원 (고정).
- pending 벡터 길이가 달라지면(업데이트 등) 학습 버퍼를 초기화합니다.

안정성
------
- 학습 행 < MIN_TRAINING_SAMPLES → None.
- sklearn 없음 → None.
- 단일 클래스 → 점수 최대 전략(규칙).
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

try:
    from sklearn.ensemble import RandomForestClassifier
except Exception:  # pragma: no cover
    RandomForestClassifier = None  # type: ignore

MIN_TRAINING_SAMPLES = int(os.environ.get("STRATEGY_AI_MIN_SAMPLES", "10"))

STRATEGY_RF_N_ESTIMATORS = int(os.environ.get("STRATEGY_RF_N_ESTIMATORS", "120"))
STRATEGY_RF_MAX_DEPTH = int(os.environ.get("STRATEGY_RF_MAX_DEPTH", "6"))
STRATEGY_RF_RANDOM_STATE = int(os.environ.get("STRATEGY_RF_RANDOM_STATE", "42"))

N_MARKET_FEATURES = 6
N_TOTAL_FEATURES = 15 + N_MARKET_FEATURES


def _strategy_index(strategy_ids: Tuple[str, ...], sid: str) -> int:
    try:
        return strategy_ids.index(sid)
    except ValueError:
        return 0


def build_feature_vector(
    strategy_ids: Tuple[str, ...],
    records: Dict[str, Any],
    market_features: Optional[List[float]] = None,
) -> List[float]:
    """
    전략별 5지표 × 3 + 시장 특징 6개.

    시장: [bull, bear, range 원-핫(3), ret20d%, dist_ma20%, vol20d(일간수익률 표준편차 %p)]
    """
    feats: List[float] = []
    for sid in strategy_ids:
        r = records.get(sid)
        if r is None:
            feats.extend([0.0, 0.0, 0.0, 0.0, 0.0])
            continue
        feats.extend(
            [
                float(getattr(r, "return_pct", 0.0)),
                float(getattr(r, "win_rate", 0.0)),
                float(getattr(r, "mdd_pct", 0.0)),
                float(getattr(r, "trade_count", 0.0)),
                float(getattr(r, "score", 0.0)),
            ]
        )
    mf = market_features if market_features is not None else [0.0] * N_MARKET_FEATURES
    if len(mf) != N_MARKET_FEATURES:
        mf = (list(mf) + [0.0] * N_MARKET_FEATURES)[:N_MARKET_FEATURES]
    feats.extend(mf)
    return feats


class StrategyAISelector:
    """RandomForest로 전략 ID(클래스 인덱스)를 예측."""

    def __init__(
        self,
        strategy_ids: Tuple[str, ...],
        min_samples: int = MIN_TRAINING_SAMPLES,
    ) -> None:
        self.strategy_ids = strategy_ids
        self.min_samples = max(1, int(min_samples))
        self._X: List[List[float]] = []
        self._y: List[int] = []
        self._pending_features: Optional[List[float]] = None
        self._model: Optional[object] = None
        self.last_decision_reason: str = "init"
        self.last_buffer_reset_reason: str = ""

    @property
    def training_sample_count(self) -> int:
        return len(self._X)

    def _clear_training_buffer(self, reason: str) -> None:
        self._X.clear()
        self._y.clear()
        self._pending_features = None
        self.last_buffer_reset_reason = reason

    def _argmax_score_sid(self, records: Dict[str, Any]) -> str:
        best = self.strategy_ids[0]
        best_sc = float("-inf")
        for sid in self.strategy_ids:
            r = records.get(sid)
            sc = float(getattr(r, "score", 0.0)) if r is not None else float("-inf")
            if sc > best_sc:
                best_sc = sc
                best = sid
        return best

    def process_evaluation(
        self,
        records: Dict[str, Any],
        market_features: Optional[List[float]] = None,
    ) -> Optional[str]:
        feats = build_feature_vector(self.strategy_ids, records, market_features)

        if self._pending_features is not None and len(self._pending_features) != len(feats):
            self._clear_training_buffer("feature_dim_changed")

        if self._pending_features is not None:
            winner_sid = self._argmax_score_sid(records)
            y = _strategy_index(self.strategy_ids, winner_sid)
            self._X.append(list(self._pending_features))
            self._y.append(y)

        self._pending_features = list(feats)

        n = len(self._X)
        if n < self.min_samples:
            self.last_decision_reason = f"sample_short({n}/{self.min_samples})"
            return None

        if RandomForestClassifier is None:
            self.last_decision_reason = "sklearn_missing"
            return None

        uniq = set(self._y)
        if len(uniq) < 2:
            self.last_decision_reason = "single_class_rule"
            return self._argmax_score_sid(records)

        try:
            clf = RandomForestClassifier(
                n_estimators=STRATEGY_RF_N_ESTIMATORS,
                max_depth=STRATEGY_RF_MAX_DEPTH,
                random_state=STRATEGY_RF_RANDOM_STATE,
                class_weight="balanced_subsample",
            )
            clf.fit(self._X, self._y)
            self._model = clf
            pred = int(clf.predict([feats])[0])
            pred = max(0, min(pred, len(self.strategy_ids) - 1))
            chosen = self.strategy_ids[pred]
            self.last_decision_reason = "random_forest"
            return chosen
        except Exception as e:  # pragma: no cover
            self.last_decision_reason = f"rf_error:{e}"
            return self._argmax_score_sid(records)
