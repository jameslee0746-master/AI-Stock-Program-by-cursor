import sys
import argparse
import os
import datetime
import threading
import time
import traceback
import csv
from collections import defaultdict, deque
import io
import contextlib
import logging
import json
import math
import pickle
import warnings
from zoneinfo import ZoneInfo
from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from PyQt5.QAxContainer import QAxWidget
from PyQt5.QtCore import QEventLoop, QTimer, QTime, Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QApplication,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

try:
    from sklearn.base import clone
    from sklearn.calibration import CalibratedClassifierCV
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score

    AI_AVAILABLE = True
except Exception:
    clone = None  # type: ignore
    CalibratedClassifierCV = None  # type: ignore
    RandomForestClassifier = None  # type: ignore
    brier_score_loss = None  # type: ignore
    log_loss = None  # type: ignore
    roc_auc_score = None  # type: ignore
    AI_AVAILABLE = False

try:
    from lightgbm import LGBMClassifier  # type: ignore
except Exception:
    LGBMClassifier = None  # type: ignore

try:
    from xgboost import XGBClassifier  # type: ignore
except Exception:
    XGBClassifier = None  # type: ignore

try:
    import yfinance as yf  # type: ignore
except Exception:
    yf = None  # type: ignore

try:
    from news_sentiment import (
        get_news_sentiment,
        prefetch_news_batch,
        cache_summary as _news_cache_summary,
        gemini_status_line as _news_gemini_status_line,
        gemini_pick_daily_strategy as _gemini_pick_daily_strategy,
        refresh_theme_strength as _refresh_theme_strength,
        get_theme_boost_for_code as _get_theme_boost_for_code,
        theme_strength_summary as _theme_strength_summary,
        is_theme_strong_for_code as _is_theme_strong_for_code,
        THEME_STRENGTH_ENABLED as _THEME_STRENGTH_ENABLED,
        append_gemini_daily_log as _append_gemini_daily_log,
        gemini_quota_status as _gemini_quota_status,
    )
    NEWS_SENTIMENT_AVAILABLE = True
except Exception:
    NEWS_SENTIMENT_AVAILABLE = False
    def get_news_sentiment(code: str, force: bool = False) -> float:  # type: ignore[misc]
        return 0.0
    def prefetch_news_batch(codes) -> dict:  # type: ignore[misc]
        return {}
    def _news_cache_summary() -> str:  # type: ignore[misc]
        return "뉴스 모듈 미설치"
    def _news_gemini_status_line() -> str:  # type: ignore[misc]
        return "Gemini=off"
    def _gemini_pick_daily_strategy(*_a, **_k):  # type: ignore[misc]
        return None
    def _refresh_theme_strength(force: bool = False):  # type: ignore[misc]
        return {}
    def _get_theme_boost_for_code(code: str, name: str = "") -> float:  # type: ignore[misc]
        return 0.0
    def _theme_strength_summary() -> str:  # type: ignore[misc]
        return "테마off"
    def _is_theme_strong_for_code(code: str, name: str = "") -> bool:  # type: ignore[misc]
        return False
    _THEME_STRENGTH_ENABLED = False
    def _append_gemini_daily_log(section: str, message: str) -> str:  # type: ignore[misc]
        return ""
    def _gemini_quota_status() -> str:  # type: ignore[misc]
        return "-"

if yf is not None:
    # yfinance의 "Failed download / possibly delisted" 원문 스팸 출력 억제
    try:
        warnings.filterwarnings("ignore", module=r"yfinance")
    except Exception:
        pass
    for _logger_name in ("yfinance", "yfinance.base", "yfinance.multi"):
        try:
            logging.getLogger(_logger_name).setLevel(logging.CRITICAL)
        except Exception:
            pass

try:
    import shadow_ledger as _shadow
except Exception:
    _shadow = None  # type: ignore
try:
    import reasoning_report as _reasoning
except Exception:
    _reasoning = None  # type: ignore

from performance import TradeTracker, fifo_open_positions_from_records
from gui import PerformanceDashboard
from strategy_manager import (
    StrategyManager,
    STRATEGY_AI,
    STRATEGY_QUALITY_DIP,
    STRATEGY_TREND,
    STRATEGY_VOLUME,
    analyze_market_regime,
    regime_momentum,
)
from dataclasses import replace
import ai_objective_shared as aos


# =========================
# 사용자 설정 구간
# =========================

# scan_market() 결과를 사용하므로 기본 고정 종목은 비워 둡니다.
STOCK_CODES: List[str] = []

# 기본 주문 수량. 예산 기반 수량 계산이 실패할 때만 fallback으로 사용합니다.
ORDER_QTY_PER_STOCK = 1

# 리스크 관리 비율 (종목당 비중 제한 + 수익폭 확대)
STOP_LOSS_PCT = -0.030  # -3.0%  (was -1.5% — 손절 완화)
TAKE_PROFIT_PCT = 0.060  # +6.0%  (was +4.5% — 익절 목표 상향)

# 매수/매도는 시장가로 단순화(실전에서는 지정가/체결전략 고려)
ORDER_TYPE_BUY = 1   # 신규매수
ORDER_TYPE_SELL = 2  # 신규매도
# hogaGb 값(환경/문서 기준에 따라 다를 수 있어, 문제 발생 시 Kiwoom 문서의 매매구분 값을 확인하세요)
# 일반적으로 "03"=시장가로 많이 사용합니다.
HOGA_MARKET = "03"

# TR/실시간 스크린 번호(여러 요청을 섞어 쓰지 않도록 고정 권장)
SCREEN_NO = "0101"
REAL_SCREEN_NO = "9001"
VI_REAL_SCREEN_NO = "9051"

# 전략 강화 파라미터
# (완화 1단계) BUY-BLOCK "MA20 대비 너무 낮음" 완화: MA20 아래 허용 폭 소폭 확대
MA20_ENTRY_GAP_PCT = -0.0120  # was -0.0060
MAX_PRICE_TO_MA20_PCT = 0.28  # 과열 상한 완화(추격 허용)
MAX_CONCURRENT_POSITIONS = 5  # 동시 보유 한도(손실일 낙폭 축소)

# +4.5% 도달 후 바로 청산하지 않고, 트레일링 스탑으로 추가 수익 노리기
USE_TRAIL_AFTER_TP = True
TRAIL_LOCK_PCT = 0.015  # 목표 도달 후 최소 이익 잠금

# 목표 익절% 도달 이후 트레일링 시 MA20 하단을 더 느슨하게(0~1 사이 값).
# ma20을 그대로 쓰면 너무 빡빡할 수 있어, 더 오래 들기 위해 factor를 곱해 바닥을 낮춥니다.
TRAIL_MA20_FACTOR = 1.0

# RSI 필터
RSI_PERIOD = 14
# (완화 1단계) "RSI 범위 이탈" 여지 / "RSI 상승폭 부족" 완화
RSI_ENTRY_MIN = 30.0  # was 32.0
RSI_ENTRY_MAX = 82.0  # RSI 상한 완화
RSI_MIN_DELTA = 0.03  # was 0.05; "RSI 상승 아님"은 그대로(전일 대비 strictly 상승)

# 거래량 필터
VOL_AVG_PERIOD = 20
# (완화 1단계) "거래량 평균비 부족" / "전일대비 거래량 부족" 완화
VOL_ENTRY_RATIO_MIN = 0.88  # was 0.95
VOL_ENTRY_GROWTH_MIN = 0.88  # was 0.94

# AI 모델(추가 필터): LightGBM/XGBoost가 있으면 우선 사용하고, 없으면 RandomForest로 fallback
AI_TRAIN_COUNT = int(os.environ.get("AI_TRAIN_COUNT", "200"))  # 학습용 최근 일봉 개수(was 120 -> 종목당 샘플 확대)
AI_PROBA_ENTRY_MIN = 0.50  # 2단계: 나쁜 매수 축소(was 0.42; json 0.30 덮어쓰기 주의)
AI_RET_LABEL_THRESHOLD = 0.002  # 비용 차감 후 기대수익률이 이 값보다 크면 상승 라벨
# 실측 결과 학습 유니버스 기준 총 샘플이 보통 97~194개 사이에서 형성되어 200 문턱을
# 거의 매일 못 넘고 "학습 스킵"으로 빠졌다(2026-06~07 로그에서 194/200이 반복 확인됨).
# 150으로 낮춰 194·177 등 흔한 샘플 수에서는 실제로 학습이 진행되도록 한다.
AI_MIN_TOTAL_SAMPLES = 150
# 당일 스캔·보유 종목만으로는 학습 샘플이 부족할 때, 시총 상위 유동성 종목을 학습 전용으로 보강
AI_TRAIN_MIN_CODES = int(os.environ.get("AI_TRAIN_MIN_CODES", "60"))
# 재학습 결과가 이보다 낮거나 기존 모델 auc 미만이면 교체하지 않음(무신호 모델은 확률이 pos_rate 로 붕괴해 AI 경로 전면 차단)
AI_REPLACE_MIN_AUC = float(os.environ.get("AI_REPLACE_MIN_AUC", "0.52"))
# trend 경로 AI 소프트 문턱 상한(하한 0.50 은 코드에서 강제)
TREND_AI_SOFT_THR_CAP = float(os.environ.get("TREND_AI_SOFT_THR_CAP", "0.50"))
STRATEGY_FATIGUE_REVERTS = int(os.environ.get("STRATEGY_FATIGUE_REVERTS", "3"))
STRATEGY_FATIGUE_WINDOW_DAYS = int(os.environ.get("STRATEGY_FATIGUE_WINDOW_DAYS", "14"))
SHADOW_EVIDENCE_MIN_N = int(os.environ.get("SHADOW_EVIDENCE_MIN_N", "30"))
SHADOW_EVIDENCE_LOOKBACK_DAYS = int(os.environ.get("SHADOW_EVIDENCE_LOOKBACK_DAYS", "30"))
SHADOW_EVIDENCE_MIN_EDGE = float(os.environ.get("SHADOW_EVIDENCE_MIN_EDGE", "0.003"))
TICK_STALL_SEC = int(os.environ.get("TICK_STALL_SEC", "180"))
HALT_DETECT_ENABLED = os.environ.get("HALT_DETECT_ENABLED", "1").strip() not in ("0", "false", "False")
HALT_TICK_WINDOW_SEC = int(os.environ.get("HALT_TICK_WINDOW_SEC", "60"))
HALT_ACTIVE_RATIO = float(os.environ.get("HALT_ACTIVE_RATIO", "0.10"))
HALT_RATE_COLLAPSE = float(os.environ.get("HALT_RATE_COLLAPSE", "0.05"))
HALT_RESUME_RATIO = float(os.environ.get("HALT_RESUME_RATIO", "0.40"))
HALT_CONFIRM_SEC = int(os.environ.get("HALT_CONFIRM_SEC", "60"))
HALT_RESUME_COOLDOWN_MIN = int(os.environ.get("HALT_RESUME_COOLDOWN_MIN", "30"))
HALT_MAX_MIN = int(os.environ.get("HALT_MAX_MIN", "90"))
VI_GUARD_ENABLED = os.environ.get("VI_GUARD_ENABLED", "1").strip() not in ("0", "false", "False")
VI_HOLD_MAX_SEC = int(os.environ.get("VI_HOLD_MAX_SEC", "180"))
VI_BUY_COOLDOWN_SEC = int(os.environ.get("VI_BUY_COOLDOWN_SEC", "300"))
STOCK_STATE_GUARD_ENABLED = os.environ.get("STOCK_STATE_GUARD_ENABLED", "1").strip() not in ("0", "false", "False")
STOCK_STATE_BLOCK_KEYWORDS = (
    "투자경고", "투자위험", "관리종목", "거래정지", "정리매매", "투자주의환기", "단기과열",
)
FEED_RECOVER_ENABLED = os.environ.get("FEED_RECOVER_ENABLED", "1").strip() not in ("0", "false", "False")
FEED_RECOVER_SILENT_SEC = int(os.environ.get("FEED_RECOVER_SILENT_SEC", "180"))
FEED_RECOVER_RETRY_GAP_SEC = int(os.environ.get("FEED_RECOVER_RETRY_GAP_SEC", "600"))
FEED_RECOVER_MAX_PER_DAY = int(os.environ.get("FEED_RECOVER_MAX_PER_DAY", "3"))
AI_TRAIN_SUPPLEMENT_MC_TOP_N = int(os.environ.get("AI_TRAIN_SUPPLEMENT_MC_TOP_N", "60"))
AI_N_ESTIMATORS = 200
AI_RANDOM_STATE = 42
AI_LABEL_HORIZON_DAYS = 3  # 매수 후 N거래일 안의 손절/익절 도달 순서로 라벨 생성
AI_ROUND_TRIP_COST_PCT = 0.0030  # 수수료+세금 근사 왕복 비용
AI_SLIPPAGE_PCT = 0.0015  # 시장가 체결 미끄러짐 보수 반영
AI_MODEL_BACKEND = os.environ.get("AI_MODEL_BACKEND", "auto").strip().lower()
AI_MODEL_FILENAME = os.environ.get("AI_MODEL_FILENAME", "ai_buy_model.pkl")
AI_MODEL_MAX_AGE_DAYS = int(os.environ.get("AI_MODEL_MAX_AGE_DAYS", "14"))
# pykrx/스캔 실패 시에도 학습이 돌아가도록 유동성 대형주·ETF 고정 보강
AI_TRAIN_FALLBACK_CODES = [
    c.strip()
    for c in os.environ.get(
        "AI_TRAIN_FALLBACK_CODES",
        "005930,000660,005380,000270,035420,035720,051910,006400,"
        "068270,105560,055550,012330,028260,066570,003550,096770,"
        "034730,017670,030200,069500,229200",
    ).split(",")
    if c.strip()
]
AI_VAL_FRAC = float(os.environ.get("AI_VAL_FRAC", "0.2"))
AI_VAL_MIN_ROWS_PER_CODE = int(os.environ.get("AI_VAL_MIN_ROWS_PER_CODE", "12"))
AI_CALIBRATION_MIN_VAL = int(os.environ.get("AI_CALIBRATION_MIN_VAL", "30"))
AI_CALIBRATION_METHOD = os.environ.get("AI_CALIBRATION_METHOD", "sigmoid").strip().lower()
AI_TRAIN_MERGE_BACK_MIN = int(os.environ.get("AI_TRAIN_MERGE_BACK_MIN", "80"))
AI_METRICS_JSON_BASENAME = os.environ.get("AI_METRICS_JSON_BASENAME", "ai_calibration_metrics.json")
# 트렌드/거량(STRATEGY_TREND·VOLUME)일 때도 진입 분류기 확률을 완화 문턱으로 적용(모델 없으면 스킵)
AI_SOFT_FILTER_TECH_STRATEGIES = os.environ.get("AI_SOFT_FILTER_TECH_STRATEGIES", "1").strip().lower() in (
    "1",
    "true",
    "yes",
    "y",
)
# _dynamic_ai_entry_min() 대비 추가 완화(음수=더 낮은 proba 허용) — 2단계에서 완화폭 축소
AI_TECH_STRATEGY_ENTRY_DELTA = float(os.environ.get("AI_TECH_STRATEGY_ENTRY_DELTA", "-0.02"))
# 완화 문턱의 절대 하한(시장 악화로 dynamic이 높아져도 이 값 미만이면 불가)
AI_TECH_STRATEGY_ENTRY_FLOOR = float(os.environ.get("AI_TECH_STRATEGY_ENTRY_FLOOR", "0.46"))

# ── 2단계: AI 통과해도 bear / 장초 / 애프터·프리 신규매수 하드 차단 ──
BLOCK_BUY_IN_BEAR = os.environ.get("BLOCK_BUY_IN_BEAR", "1").strip().lower() in (
    "1", "true", "yes", "y",
)
# 약세 하드블록은 지수(ETF) 국면이 확정된 경우에만 적용.
# 관심종목 합성(composite) 국면은 편향이 커서 하드블록하지 않고 AI가산/비중축소만 사용.
BLOCK_BUY_IN_BEAR_REQUIRE_INDEX = os.environ.get(
    "BLOCK_BUY_IN_BEAR_REQUIRE_INDEX", "1"
).strip().lower() in ("1", "true", "yes", "y")
# 키움 일봉으로 국면 재산출할 지수 대리 종목(우선순위). yfinance 실패 시 사용.
_KIWOOM_REGIME_INDEX_RAW = os.environ.get(
    "KIWOOM_REGIME_INDEX_CODES",
    "069500,229200",  # KODEX 200, KODEX 코스닥150
)
KIWOOM_REGIME_INDEX_CODES = [
    c.strip() for c in _KIWOOM_REGIME_INDEX_RAW.split(",") if c.strip()
]
# 장중 국면 자동 재평가 주기(초). 아침 1회 고정으로 반등일을 통째로 막는 문제 방지.
REGIME_REFRESH_INTERVAL_SEC = float(os.environ.get("REGIME_REFRESH_INTERVAL_SEC", "1800"))
BLOCK_BUY_IN_AFTER_HOURS = os.environ.get("BLOCK_BUY_IN_AFTER_HOURS", "1").strip().lower() in (
    "1", "true", "yes", "y",
)
BLOCK_BUY_IN_PREMARKET = os.environ.get("BLOCK_BUY_IN_PREMARKET", "1").strip().lower() in (
    "1", "true", "yes", "y",
)

# ── 3단계: 학습 품질(AUC) 나쁘면 매수 규모 자동 축소 ──
AI_QUALITY_SHRINK_ENABLED = os.environ.get("AI_QUALITY_SHRINK_ENABLED", "1").strip().lower() in (
    "1", "true", "yes", "y",
)
AI_QUALITY_AUC_SOFT = float(os.environ.get("AI_QUALITY_AUC_SOFT", "0.55"))   # 미만 → size×0.75
AI_QUALITY_AUC_HARD = float(os.environ.get("AI_QUALITY_AUC_HARD", "0.53"))   # 미만 → size×0.50
AI_QUALITY_AUC_CRIT = float(os.environ.get("AI_QUALITY_AUC_CRIT", "0.50"))   # 미만 → size×0.25
AI_QUALITY_THR_ADD_SOFT = float(os.environ.get("AI_QUALITY_THR_ADD_SOFT", "0.03"))
AI_QUALITY_THR_ADD_HARD = float(os.environ.get("AI_QUALITY_THR_ADD_HARD", "0.06"))
AI_QUALITY_THR_ADD_CRIT = float(os.environ.get("AI_QUALITY_THR_ADD_CRIT", "0.08"))

# ── 4단계: 뉴스·시장 요약으로 당일 편향만 조절(전략 재작성 아님) ──
DAY_BIAS_ENABLED = os.environ.get("DAY_BIAS_ENABLED", "1").strip().lower() in (
    "1", "true", "yes", "y",
)
DAY_BIAS_NEWS_NEG = float(os.environ.get("DAY_BIAS_NEWS_NEG", "-0.15"))
DAY_BIAS_NEWS_POS = float(os.environ.get("DAY_BIAS_NEWS_POS", "0.18"))  # Gemini 점수 반영 쉽게
DAY_BIAS_NEG_AI_ADD = float(os.environ.get("DAY_BIAS_NEG_AI_ADD", "0.05"))
DAY_BIAS_NEG_SIZE_MULT = float(os.environ.get("DAY_BIAS_NEG_SIZE_MULT", "0.70"))
DAY_BIAS_POS_AI_DELTA = float(os.environ.get("DAY_BIAS_POS_AI_DELTA", "-0.02"))
NEWS_CODE_BLOCK_THRESHOLD = float(os.environ.get("NEWS_CODE_BLOCK_THRESHOLD", "-0.55"))
# 추론(Gemini 일일전략) 우선 — 규칙 후보 안에서 Gemini 선택을 RF/우선순위보다 앞세움
STRATEGY_REASONING_FIRST = os.environ.get("STRATEGY_REASONING_FIRST", "1").strip().lower() in (
    "1",
    "true",
    "yes",
    "y",
)
# 강세 테마 종목 매수 수량 소폭 가산 (우량/일반 공통, 상한 1.25)
THEME_STRONG_SIZE_MULT = float(os.environ.get("THEME_STRONG_SIZE_MULT", "1.15"))
THEME_PREFETCH_INTERVAL_SEC = float(os.environ.get("THEME_PREFETCH_INTERVAL_SEC", "3600"))

# 실전 손실 제어: 매도 사유 기록, 손절 후 재진입 제한, 약세장/연속손실 매수 축소
STOP_LOSS_REENTRY_COOLDOWN_SEC = 30 * 60  # 30분 (was 4시간 — 당일 반등 재매수 허용)
# 손절 후 재진입(reentry) 시 타이트한 스톱 / 포지션 크기 절반 적용
REENTRY_STOP_LOSS_PCT = -0.015          # 재진입 손절: -1.5% (기본 -3.0%보다 타이트)
REENTRY_POSITION_SIZE_FACTOR = 0.5     # 재진입 포지션 크기 배율 (기본의 50%)
RECENT_LOSS_LOOKBACK_DAYS = 7
RECENT_LOSS_BLOCK_COUNT = 5  # was 2: 7일내 2회 손절 시 차단 → 5회로 완화
RECENT_LOSS_BLOCK_DAYS = 3
MARKET_WEAK_MAX_CONCURRENT_POSITIONS = 3
MARKET_WEAK_AI_ENTRY_ADD = 0.10  # 2단계: bear 시 AI 문턱 추가 가산(was 0.08)
DAILY_LOSS_SOFT_LIMIT_PCT = -0.015  # was -0.008: 소프트 한도 완화
DAILY_LOSS_HARD_LIMIT_PCT = -0.030  # was -0.015: 하드 한도 완화 (1회 손절 후 하루 매수 차단 방지)
DAILY_LOSS_AI_ENTRY_ADD = 0.08
DAILY_LOSS_MAX_CONCURRENT_POSITIONS = 2
DAILY_AI_LOSS_EXIT_BLOCK_COUNT = 2
DAILY_WIN_RATE_BLOCK_MIN_SELLS = 10
DAILY_WIN_RATE_BLOCK_PCT = 0.20

# 잔고(TR opw00018)와 거래 CSV(FIFO 재구성) 불일치 감지
RECONCILE_AVG_REL_TOL = 0.015  # 평단 상대 허용 오차 (1.5%)
RECONCILE_AVG_ABS_MIN_KRW = 40.0  # 절대 허용(원); 틱 차이 무시
RECONCILE_ISSUE_COOLDOWN_SEC = 300.0  # 동일 불일치 요약 재로그 최소 간격

BUY_SCORE_MIN = 0.40  # 2단계: 랭킹 컷 상향(was 0.30)
PARTIAL_TAKE_PROFIT_ENABLED = True
PARTIAL_TAKE_PROFIT_RATIO = 0.5
SCALE_IN_ENABLED = True
SCALE_IN_MIN_PROFIT_PCT = 0.018
SCALE_IN_MAX_POSITION_BUDGET_FACTOR = 0.75
MARKET_WEAK_EXIT_LOSS_PCT = -0.005
# 장 시작 직후(갭·급변동) 신규 매수 금지 분 — 018880 갭 손절 사례 대응
MARKET_OPEN_BUY_GUARD_MINUTES = int(os.environ.get("MARKET_OPEN_BUY_GUARD_MINUTES", "10"))
# bull(상승장) 전용 진입 소폭 완화. 보수 모드로 포지션/손절 한도를 극단적으로 좁혀둔 상태에서
# 확인된 상승장에서도 거래가 완전히 막히는 것을 막기 위해, 동시보유 한도·최근손절 차단 횟수도
# bull 국면에서 소폭(안전 상한 내) 완화한다. bear/range 국면에서는 손대지 않고 보수 기준 그대로 유지.
BULL_ENTRY_RELAX_ENABLED = os.environ.get("BULL_ENTRY_RELAX_ENABLED", "1").strip().lower() in (
    "1", "true", "yes", "y",
)
BULL_AI_ENTRY_DELTA = float(os.environ.get("BULL_AI_ENTRY_DELTA", "-0.04"))
BULL_VOL_RATIO_MULT = float(os.environ.get("BULL_VOL_RATIO_MULT", "0.90"))
BULL_VOL_GROWTH_MULT = float(os.environ.get("BULL_VOL_GROWTH_MULT", "0.92"))
# 2026-07-07: 불장 완화가 장초 매수금지 구간을 30분→25분으로 깎아서, 그 풀리는
# 순간(09:25:00)에 진입한 004830/037070이 5~6분 만에 손절(-3.1%/-3.3%)됐다.
# 보수모드에서 사용자가 늘려둔 안전장치를 불장 판단만으로 뒷문에서 다시 줄이지
# 않도록 0으로 되돌린다(더 이상 단축하지 않음).
BULL_GUARD_MINUTES_DELTA = int(os.environ.get("BULL_GUARD_MINUTES_DELTA", "0"))
BULL_SCORE_DELTA = float(os.environ.get("BULL_SCORE_DELTA", "-0.03"))
BULL_TREND_VOL_MULT = float(os.environ.get("BULL_TREND_VOL_MULT", "0.85"))
# bull 국면 한정 동시보유 한도 완화: +bonus, 단 (현재 한도, hard_cap) 중 큰 값을 넘지 않음
# → 보수 모드(한도=1)일 때만 사실상 늘어나고, 원래 기본값(5)보다 더 커지지는 않음(안전장치)
BULL_MAX_POSITIONS_BONUS = int(os.environ.get("BULL_MAX_POSITIONS_BONUS", "1"))
BULL_MAX_POSITIONS_HARD_CAP = int(os.environ.get("BULL_MAX_POSITIONS_HARD_CAP", "3"))
# bull 국면 한정 "최근 손절 반복" 차단 임계값 완화(+bonus)
BULL_RECENT_LOSS_BONUS = int(os.environ.get("BULL_RECENT_LOSS_BONUS", "1"))
# 매수 차단 사유 요약 로그 주기(초)
BUY_DIAG_INTERVAL_SEC = float(os.environ.get("BUY_DIAG_INTERVAL_SEC", "300"))
# 모의투자 전용 유지매매(아래 상수) — MOCK_LIVE_PARITY=1 이면 전부 무시하고 실전 규칙
MOCK_KEEP_VOL_CAP = float(os.environ.get("MOCK_KEEP_VOL_CAP", "0.67"))
MOCK_KEEP_AI_SOFT_CAP = float(os.environ.get("MOCK_KEEP_AI_SOFT_CAP", "0.46"))
MOCK_BEAR_QUALITY_DIP = os.environ.get("MOCK_BEAR_QUALITY_DIP", "1").strip().lower() in (
    "1",
    "true",
    "yes",
    "y",
)
MOCK_BEAR_SIZE_MULT = float(os.environ.get("MOCK_BEAR_SIZE_MULT", "0.50"))
# 모의도 실전 규칙(거래량 AND·AI 품질가산·약세 하드블록·VOL 하한). 유지매매 완화 없음
MOCK_LIVE_PARITY = os.environ.get("MOCK_LIVE_PARITY", "1").strip().lower() in (
    "1",
    "true",
    "yes",
    "y",
)
# 실전(및 모의 패리티)은 strategy_params VOL을 이 하한 아래로 쓰지 않음
LIVE_VOL_ENTRY_FLOOR = float(os.environ.get("LIVE_VOL_ENTRY_FLOOR", "0.68"))
# 당일 매수 0건 + 하락/횡보 국면 + 장중 2시간 경과 시 진입 문턱 소폭 완화
NO_TRADE_RELAX_AFTER_HOUR = int(os.environ.get("NO_TRADE_RELAX_AFTER_HOUR", "11"))
NO_TRADE_RELAX_AI_DELTA = float(os.environ.get("NO_TRADE_RELAX_AI_DELTA", "-0.02"))  # 2단계: 완화폭 축소(was -0.04)
NO_TRADE_RELAX_SCORE_DELTA = float(os.environ.get("NO_TRADE_RELAX_SCORE_DELTA", "-0.05"))
# 하락/횡보장에서 엔진 진입 규칙을 volume(완화 필터)으로 고정
REGIME_OVERRIDE_STRATEGY = os.environ.get("REGIME_OVERRIDE_STRATEGY", "1").strip().lower() in (
    "1",
    "true",
    "yes",
    "y",
)
# volume 전략: MA20 대비 허용 하락폭 (ai 기본 -1.2%보다 완화)
VOLUME_MA20_ENTRY_GAP_PCT = float(os.environ.get("VOLUME_MA20_ENTRY_GAP_PCT", "-0.04"))
# 하락/횡보·국면 미평가 시 MA5<=MA20 이더라도 MA20 근처 반등 시 진입 허용
VOLUME_WEAK_MA_ENTRY_ENABLED = os.environ.get("VOLUME_WEAK_MA_ENTRY_ENABLED", "1").strip().lower() in (
    "1",
    "true",
    "yes",
    "y",
)
VOLUME_WEAK_MA20_ENTRY_GAP_PCT = float(os.environ.get("VOLUME_WEAK_MA20_ENTRY_GAP_PCT", "-0.06"))
# 무거래 완화 시작 시각 (기본 10:30)
NO_TRADE_RELAX_AFTER_TIME = os.environ.get("NO_TRADE_RELAX_AFTER_TIME", "10:30").strip()
AUTO_BACKTEST_GATE_ENABLED = True
AUTO_BACKTEST_CACHE_SEC = 30 * 60
AUTO_BACKTEST_MAX_CODES = 80
AUTO_BT_CAUTION_AVG_RET = -0.004
AUTO_BT_BLOCK_AVG_RET = -0.012
AUTO_BT_CAUTION_WIN_RATE = 0.42
AUTO_BT_BLOCK_WIN_RATE = 0.35
AUTO_BT_CAUTION_AI_ENTRY_ADD = 0.06
AUTO_BT_BLOCK_BUY = True
AUTO_BT_SCORE_WEIGHT = 6.0

# AI 기반 매도(조기 청산/트레일링 보조) 파라미터
# proba_up < AI_PROBA_EXIT_MAX 일 때 조기청산 → 값을 낮추면 더 ‘베어리시’해야 매도(보유 연장)
AI_PROBA_EXIT_MAX = 0.38            # was 0.42 — AI 매도 문턱 강화(더 확실한 하락 신호만 반응)
AI_EARLY_EXIT_MIN_PROFIT_PCT = 0.035  # was 0.02 — +3.5%부터 AI 조기익절 허용 (was +2%)
AI_LOSS_EXIT_MIN_LOSS_PCT = -0.020  # was -0.008 — -2.0% 이하부터 AI 손실청산 허용 (was -0.8%)
AI_EARLY_EXIT_COOL_PCT = 0.01
AI_EXIT_MIN_HOLD_SEC = 1800  # was 180 — 매수 후 최소 30분 보유 후에만 AI 매도 허용 (was 3분)
BACKTEST_LOOKBACK_BARS = 60
AI_FEATURE_NAMES = [
    "ma_gap_now",
    "ma_gap_prev",
    "rsi14",
    "vol_ratio",
    "ret_1d",
    "vol_chg",
    "volatility_20d",
    "macd_line_norm",
    "macd_signal_norm",
    "atr_pct",
    "trade_win_rate",
    "trade_avg_pnl_pct",
    "trade_count_norm",
    "trade_stop_loss_rate",   # 2026-07-20: 실전 손절 비중
    "trade_weak_exit_rate",   # 2026-07-20: 약세청산 비중
    "news_sentiment",   # 뉴스 감성 점수 (-1.0 ~ +1.0)
]
# 실전 CSV 라운드트립을 일봉 라벨보다 N배 강조(소수 표본 보정)
AI_LIVE_TRADE_SAMPLE_WEIGHT = float(os.environ.get("AI_LIVE_TRADE_SAMPLE_WEIGHT", "8"))
AI_USE_LIVE_TRADE_LABELS = os.environ.get("AI_USE_LIVE_TRADE_LABELS", "1").strip().lower() in (
    "1",
    "true",
    "yes",
    "y",
)
# 학습 로직/라벨 주입 방식 변경 시 올려서 디스크 모델 강제 재학습
AI_MODEL_TRAIN_TAG = os.environ.get("AI_MODEL_TRAIN_TAG", "live_labels_v2")

# ATR 기반 종목별 리스크. 변동성 큰 종목은 손절/익절 폭을 자동으로 넓힙니다.
USE_ATR_RISK = True
ATR_PERIOD = 14
ATR_STOP_MULT = 1.2
ATR_TAKE_MULT = 2.4
ATR_STOP_MIN_PCT = 0.012
ATR_STOP_MAX_PCT = 0.035
ATR_TAKE_MIN_PCT = 0.035
ATR_TAKE_MAX_PCT = 0.080

# MA 계산을 위해 TR에서 가져올 봉 개수(너무 크게 잡을수록 TR이 느려집니다)
MA_TR_COUNT = 25

# MA TR은 한 번에 하나의 종목만 요청(라운드로빈)해서 GUI 프리징/지연을 줄입니다.
MA_REFRESH_INTERVAL_SEC = 0.3
MA_REFRESH_BATCH_PER_TICK = 3
SCAN_TOP_N = 0  # 0 이하이면 스캔 통과 종목 전체 사용(개수 제한 없음)
SCAN_WORKERS = 16
SCAN_CHUNK_SIZE = 48
SCAN_MIN_DAILY_VOLUME = 120_000.0  # 1차 유동성 기준 완화
SCAN_MIN_DAILY_VOLUME_RELAXED = 40_000.0  # 테스트스캔 ON 1차 추가 완화
SCAN_RELAXED_MAX_UNIVERSE = 800  # 테스트스캔 ON 시 유니버스 샘플 상한
SCAN_RELAXED_TIMEOUT_SEC = 120  # 테스트스캔 ON 시 스캔 최대 시간(초)
# 우량 대형 근사: KOSPI+KOSDAQ 통합 시총 상위 N종만 스캔 유니버스(0=전체). 환경변수 SCAN_QUALITY_MC_TOP_N 로 덮어씀.
# 조회 우선순위: pykrx(KRX) → 네이버 시총 API → 당일 디스크 캐시 → 고정 폴백. 전부 실패 시에만 전체 유니버스.
SCAN_QUALITY_MC_TOP_N = int(os.environ.get("SCAN_QUALITY_MC_TOP_N", "200"))
_MC_TOP_ALLOWLIST_CACHE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "data",
    "mc_top_allowlist_cache.json",
)
# KRX/네이버 모두 실패할 때 쓰는 대형·유동성 근사 목록(시총 상위 근사, 순서≈대형 우선)
_SCAN_QUALITY_MC_FALLBACK_DEFAULT = (
    "005930,000660,373220,207940,005380,000270,068270,005490,105560,055550,"
    "035420,012330,028260,006400,051910,066570,003670,032830,086790,316140,"
    "034730,015760,017670,030200,096770,003550,033780,009150,018260,010130,"
    "011200,024110,138040,259960,035720,036570,251270,352820,277810,247540,"
    "196170,068760,196080,000810,161390,010950,011170,047050,042700,009540,"
    "064350,267250,004020,010140,329180,042660,079550,012450,000720,047810,"
    "034020,326030,128940,000100,006260,018880,011780,090430,051900,241560,"
    "021240,139480,004170,023530,028050,071050,078930,005940,016360,001450,"
    "005830,000880,138930,175330,029780,088350,006800,005387,005385,005935,"
    "003490,011070,009830,010060,086280,180640,204320,000150,003410,004370,"
    "007070,271560,097950,005180,001040,002790,004990,030000,069960,035250,"
    "282330,111770,192820,214450,145720,039490,003000,005300,280360,001680,"
    "000120,009420,014680,018250,008770,004000,008930,002380,001740,001120,"
    "003230,005070,006280,009240,012750,016380,017800,020150,024110,025540,"
    "026960,028670,032640,033240,034220,036460,039130,042670,047040,051600,"
    "052690,055490,057050,058470,060980,064960,069500,069260,071320,073240,"
    "079160,086790,090350,093370,095570,100250,103140,105840,112610,114090,"
    "120110,122870,128820,136480,137310,138040,145210,161890,178920,183300,"
    "192080,200670,214320,229200,234080,241590,251270,259960,263750,267260,"
    "272210,278470,293490,298040,307950,316140,323410,329180,348210,357780,"
    "373220,383220,402340,403550,443060,450080,456040,462520,489790,000660"
)
SCAN_QUALITY_MC_FALLBACK_CODES = [
    c.strip()
    for c in os.environ.get(
        "SCAN_QUALITY_MC_FALLBACK_CODES", _SCAN_QUALITY_MC_FALLBACK_DEFAULT
    ).split(",")
    if c.strip() and len(c.strip()) == 6 and c.strip().isdigit()
]
# 1(기본): 스캔 호출마다 시총상위 적용 ↔ 전 종목 유니버스를 번갈아 사용. 0 이면 항상 시총 필터만(또는 TOP_N=0이면 의미 없음).
SCAN_QUALITY_ROTATE_WITH_FULL = os.environ.get("SCAN_QUALITY_ROTATE_WITH_FULL", "1").strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
)
# 우량 스캔 차례(SCAn 시총상위 intent)에서 시총허용 종목에 적용하는 눌림 진입(전략선택기 비포함)
QUALITY_DIP_ENABLED = os.environ.get("QUALITY_DIP_ENABLED", "1").strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
)
# 시총 허용목록에 있으면 전체유니버스 스캔 차례에도 눌림 규칙 유지(적극 매수)
QUALITY_DIP_ALWAYS_ON_ALLOWLIST = os.environ.get(
    "QUALITY_DIP_ALWAYS_ON_ALLOWLIST", "1"
).strip().lower() not in ("0", "false", "no", "off")
# 우량 눌림은 약세(bear) 하드블록 예외(parity여도) — 비중만 축소
QUALITY_DIP_ALLOW_IN_BEAR = os.environ.get("QUALITY_DIP_ALLOW_IN_BEAR", "1").strip().lower() in (
    "1",
    "true",
    "yes",
    "y",
)
QUALITY_BEAR_SIZE_MULT = float(os.environ.get("QUALITY_BEAR_SIZE_MULT", "0.55"))
# 우량 눌림 진입 완화(적극매수)
QDIP_MAX_PRICE_ABOVE_MA20_PCT = float(os.environ.get("QDIP_MAX_PRICE_ABOVE_MA20_PCT", "0.020"))
QDIP_MIN_PRICE_VS_MA20_PCT = float(os.environ.get("QDIP_MIN_PRICE_VS_MA20_PCT", "-0.18"))
QDIP_RSI_MAX_ENTRY = float(os.environ.get("QDIP_RSI_MAX_ENTRY", "52"))
QDIP_RSI_MIN_ENTRY = float(os.environ.get("QDIP_RSI_MIN_ENTRY", "18"))
QDIP_RSI_DROP_MAX_TOLERANCE = float(os.environ.get("QDIP_RSI_DROP_MAX_TOLERANCE", "3.0"))
QDIP_VOL_RATIO_MIN = float(os.environ.get("QDIP_VOL_RATIO_MIN", "0.65"))
QDIP_VOL_DAY_GROWTH_MIN = float(os.environ.get("QDIP_VOL_DAY_GROWTH_MIN", "0.80"))
# 우량 스캔 차례에 돌파형 필터와 별개로 눌림 구간 종목을 매수 후보에 추가(0=끔)
SCAN_QUALITY_DIP_MAX = int(os.environ.get("SCAN_QUALITY_DIP_MAX", "20"))
# 눌림 후보 정렬 기준: MA20 대비 이 이격(-5%)에 가까운 순
SCAN_QUALITY_DIP_TARGET_REL = float(os.environ.get("SCAN_QUALITY_DIP_TARGET_REL", "-0.05"))
QDIP_REQUIRE_MA5_GT_MA20 = os.environ.get("QDIP_REQUIRE_MA5_GT_MA20", "0").strip().lower() in (
    "1",
    "true",
    "yes",
    "y",
)
QUALITY_AI_THR_RELAX = float(os.environ.get("QUALITY_AI_THR_RELAX", "0.06"))  # soft thr 완화(우량눌림)
# 우량주 물타기(평단 아래 분할 추가매수) — 수익 SCALE_IN 과 별개
QUALITY_AVG_DOWN_ENABLED = os.environ.get("QUALITY_AVG_DOWN_ENABLED", "1").strip().lower() in (
    "1",
    "true",
    "yes",
    "y",
)
QUALITY_AVG_DOWN_MAX = int(os.environ.get("QUALITY_AVG_DOWN_MAX", "3"))
QUALITY_AVG_DOWN_STEP_PCT = float(os.environ.get("QUALITY_AVG_DOWN_STEP_PCT", "0.02"))  # -2%마다
QUALITY_AVG_DOWN_COOLDOWN_SEC = float(os.environ.get("QUALITY_AVG_DOWN_COOLDOWN_SEC", "900"))
QUALITY_AVG_DOWN_SIZE_FACTOR = float(os.environ.get("QUALITY_AVG_DOWN_SIZE_FACTOR", "0.55"))
QUALITY_AVG_DOWN_NEWS_MIN = float(os.environ.get("QUALITY_AVG_DOWN_NEWS_MIN", "-0.40"))
MIN_ENTRY_PRICE = 2000  # 매수 하한가(원)
MAX_ENTRY_PRICE = 1_000_000  # 매수 상한가(원)
BUDGET_BASED_ORDER_QTY = True  # 총예산/남은 슬롯 기준으로 주문수량 자동 계산
MAX_POSITION_BUDGET_PCT = 0.20  # 한 종목 최대 투자비중(추정예탁자산 대비)  was 0.12
ACTIVE_BUY_TARGET_LIMIT = 100  # 실매수 대상은 스캔 상위 N개로 압축
ORDER_TIMEOUT_COOLDOWN_SEC = 600  # 매수 timeout 후 같은 종목 재주문 대기
# 2026-07-14: 300s로 줄였더니(빠른 정리) 044960 사고가 났다.
# 1차 매수 14:22 timeout → 300s 가드 만료(14:28)로 pending 추적 삭제 → 쿨다운 600s
# 만료(14:33) 직후 2차 매수 415주가 또 나감. 그사이 1차 주문이 늦게 체결되며
# 최종 830주(의도 415의 2배) + CSV 누락(398주). 가드는 쿨다운 이상이어야
# 재주문 허용 시점에도 1차 추적이 남아 중복매수를 막을 수 있다. 기본 1800s 복원.
ORDER_TIMEOUT_LATE_FILL_GUARD_SEC = max(
    int(os.environ.get("ORDER_TIMEOUT_LATE_FILL_GUARD_SEC", "1800")),
    int(ORDER_TIMEOUT_COOLDOWN_SEC),
)
# 2026-07-09: 매도쪽은 buy와 달리 late_fill_guard가 없어서, elapsed>25s에 바로
# pending_orders에서 제거해버렸다. TR 응답이 25초보다 늦게 오면(TR 과호출 시 흔함)
# 실제 체결을 영영 못 잡아서 CSV에 매도 기록이 누락되는 사고(004985)가 발생했다.
# 또한 같은 종목에 매도주문이 이미 진행중인데 timeout으로 오판하고 재주문을 내면
# pending_orders[code]가 덮어써져 첫 주문 추적이 끊긴다. 아래 가드로 재현 방지.
SELL_ORDER_TIMEOUT_LATE_FILL_GUARD_SEC = int(
    os.environ.get("SELL_ORDER_TIMEOUT_LATE_FILL_GUARD_SEC", "180")
)
MAX_SINGLE_BUY_ORDER_KRW = float(os.environ.get("MAX_SINGLE_BUY_ORDER_KRW", "2000000"))  # was 300000
# 진입 변동성 / 실행 리스크(전역 매수 간격·시간당 한도·ATR 게이트)
# USE_ENTRY_ATR_GATE=1 이고 ENTRY_ATR_MAX_PCT>0 이면 ma[code].atr_pct 초과 시 신규 매수만 차단(스케일인은 buy_market만 타고 _should_buy_common은 안 탐)
USE_ENTRY_ATR_GATE = os.environ.get("USE_ENTRY_ATR_GATE", "0").strip().lower() in ("1", "true", "yes", "y")
ENTRY_ATR_MAX_PCT = float(os.environ.get("ENTRY_ATR_MAX_PCT", "0"))
ATR_INVERSE_ORDER_CAP = os.environ.get("ATR_INVERSE_ORDER_CAP", "0").strip().lower() in ("1", "true", "yes", "y")
ENTRY_ATR_REF_PCT = float(os.environ.get("ENTRY_ATR_REF_PCT", "0.02"))
ATR_ORDER_CAP_MIN_FACTOR = float(os.environ.get("ATR_ORDER_CAP_MIN_FACTOR", "0.25"))
MIN_SEC_BETWEEN_ANY_BUYS = float(os.environ.get("MIN_SEC_BETWEEN_ANY_BUYS", "0"))
MAX_NEW_BUYS_PER_HOUR = int(os.environ.get("MAX_NEW_BUYS_PER_HOUR", "0"))
SCAN_CACHE_TTL_SEC = 1800
# 기본 사용 계좌(상시모의 국내주식). 목록에 있으면 환경변수보다 이 값을 우선합니다.
DEFAULT_KIWOOM_ACCOUNT_NO = "81375140"
DEFAULT_OVERSEAS_ACCOUNT_NO = "6110-4621"
US_STOCK_REGULAR_ONLY = True
OVERSEAS_AUTO_UPGRADE = int(os.environ.get("OVERSEAS_AUTO_UPGRADE", "0"))
OVERSEAS_LOGIN_TIMEOUT_MS = int(os.environ.get("OVERSEAS_LOGIN_TIMEOUT_MS", "180000"))
OVERSEAS_ACCOUNT_READY_TIMEOUT_MS = int(os.environ.get("OVERSEAS_ACCOUNT_READY_TIMEOUT_MS", "60000"))
# 테스트용: 장외 시간에도 조건검색 강제 실행
FORCE_SCAN_ANYTIME = os.environ.get("FORCE_SCAN_ANYTIME", "").strip().lower() in ("1", "true", "yes", "y")
SCAN_RELAXED_MODE = False  # 테스트스캔 ON 시 2차 필터 완화
# 누적 진입대기 보존 설정
# - MAX_CUMULATED_TARGETS <= 0 이면 상한 없이 누적
# - TARGET_TTL_DAYS <= 0 이면 TTL 만료 없이 누적
MAX_CUMULATED_TARGETS = 0
TARGET_TTL_DAYS = 0
# 메모리 보호용 하드 상한(항상 적용). 너무 커지면 최신 관측 우선으로 자동 축소.
HARD_MAX_CUMULATED_TARGETS = int(os.environ.get("HARD_MAX_CUMULATED_TARGETS", "1000"))
KEEPALIVE_INTERVAL_MIN = 30
TOTAL_LIMIT_KRW = float(os.environ.get("TOTAL_LIMIT_KRW", "10000000"))
# 대량 진입대기 표시 시 UI 부하 완화 (화면 구성 동일, 갱신만 가볍게)
UI_TABLE_REFRESH_INTERVAL_SEC = 4.0
UI_WAITING_DISPLAY_LIMIT = 300
_SCAN_OHLCV_CACHE: Dict[str, tuple] = {}
_SCAN_OHLCV_CACHE_LOCK = threading.Lock()
_SCAN_BAD_TICKERS = set()
_SCAN_BAD_TICKERS_LOCK = threading.Lock()
_SCAN_BAD_WARNED_TICKERS = set()
_SCAN_BAD_WARNED_TICKERS_LOCK = threading.Lock()
# yfinance 캐시 메모리 보호용 상한
SCAN_OHLCV_CACHE_MAX_ENTRIES = int(os.environ.get("SCAN_OHLCV_CACHE_MAX_ENTRIES", "800"))

_MC_TOP_FILTER_CACHE_DAY: str = ""
_MC_TOP_FILTER_CACHE_N: int = 0
_MC_TOP_FILTER_CACHE_SET: Optional[Set[str]] = None
_MC_TOP_FILTER_CACHE_LOCK = threading.Lock()

_SCAN_MC_ROTATION_COUNT = 0
_SCAN_MC_ROTATION_LOCK = threading.Lock()


def _scan_should_apply_mc_cap() -> bool:
    """시총 상위 필터를 이번 스캔에 적용할지. 로테이션 시 짝번=우량(시총제한), 홀번=전체."""
    mc_n = max(0, int(SCAN_QUALITY_MC_TOP_N))
    if mc_n <= 0:
        return False
    if not SCAN_QUALITY_ROTATE_WITH_FULL:
        return True
    global _SCAN_MC_ROTATION_COUNT
    with _SCAN_MC_ROTATION_LOCK:
        apply_cap = (_SCAN_MC_ROTATION_COUNT % 2 == 0)
        _SCAN_MC_ROTATION_COUNT += 1
    return apply_cap


def _mc_normalize_code6(raw: object) -> str:
    dig = "".join(ch for ch in str(raw or "").strip() if ch.isdigit())
    if len(dig) < 6:
        return ""
    c6 = dig[-6:]
    return c6 if len(c6) == 6 and c6.isdigit() else ""


def _mc_top_store_memory(today_s: str, top_n: int, top_set: Set[str]) -> Set[str]:
    global _MC_TOP_FILTER_CACHE_DAY, _MC_TOP_FILTER_CACHE_N, _MC_TOP_FILTER_CACHE_SET
    with _MC_TOP_FILTER_CACHE_LOCK:
        _MC_TOP_FILTER_CACHE_DAY = today_s
        _MC_TOP_FILTER_CACHE_N = top_n
        _MC_TOP_FILTER_CACHE_SET = set(top_set)
    return set(top_set)


def _mc_top_save_disk(today_s: str, ranked_codes: List[str], source: str) -> None:
    path = _MC_TOP_ALLOWLIST_CACHE_PATH
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        payload = {
            "day": today_s,
            "source": source,
            "codes": list(ranked_codes),
            "saved_at": datetime.datetime.now().isoformat(timespec="seconds"),
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=0)
    except Exception:
        pass


def _mc_top_load_disk(top_n: int) -> Tuple[Optional[List[str]], str]:
    path = _MC_TOP_ALLOWLIST_CACHE_PATH
    if not os.path.isfile(path):
        return None, ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        codes_raw = payload.get("codes") or []
        codes: List[str] = []
        seen: Set[str] = set()
        for raw in codes_raw:
            c6 = _mc_normalize_code6(raw)
            if not c6 or c6 in seen:
                continue
            seen.add(c6)
            codes.append(c6)
        if not codes:
            return None, ""
        src = str(payload.get("source") or "disk")
        day = str(payload.get("day") or "")
        return codes[: max(top_n, 1)], f"{src}:{day or '?'}"
    except Exception:
        return None, ""


def _mc_top_from_pykrx(top_n: int) -> Optional[List[str]]:
    try:
        from pykrx import stock as krx_stock  # type: ignore
    except ImportError:
        return None

    from datetime import date as date_cls, timedelta

    caps: Dict[str, float] = {}
    for back in range(14):
        cand = date_cls.today() - timedelta(days=back)
        if cand.weekday() >= 5:
            continue
        try:
            if cand in _krx_holidays(cand.year):
                continue
        except Exception:
            pass
        ds = cand.strftime("%Y%m%d")
        got_any = False
        for mkt in ("KOSPI", "KOSDAQ"):
            try:
                df = krx_stock.get_market_cap_by_ticker(ds, market=mkt)
            except Exception:
                df = None
            if df is None or getattr(df, "empty", True):
                continue
            cols = list(getattr(df, "columns", []) or [])
            cap_col = ""
            for cand_col in ("시가총액", "MKTCAP", "market_cap", "MarketCap"):
                if cand_col in cols:
                    cap_col = cand_col
                    break
            if not cap_col:
                continue
            got_any = True
            for idx in df.index:
                c6 = _mc_normalize_code6(idx)
                if not c6:
                    continue
                try:
                    cap = float(df.loc[idx, cap_col])
                except Exception:
                    try:
                        cap = float(df.loc[str(idx), cap_col])
                    except Exception:
                        continue
                if cap > 0:
                    caps[c6] = max(caps.get(c6, 0.0), cap)
        if got_any and caps:
            break
    if not caps:
        return None
    ranked = sorted(caps.items(), key=lambda kv: kv[1], reverse=True)
    return [c for c, _ in ranked[: max(top_n, 1)]]


def _mc_top_from_naver(top_n: int) -> Optional[List[str]]:
    """네이버 모바일 시총순위 API (KRX 403 우회). KOSPI+KOSDAQ 합산 후 상위 top_n."""
    try:
        from urllib.request import Request, urlopen
        from urllib.error import HTTPError, URLError
    except ImportError:
        return None

    page_size = 100
    pages = min(5, max(2, (int(top_n) + page_size - 1) // page_size + 1))
    caps: Dict[str, float] = {}
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; KiwoomBot/1.0)",
        "Referer": "https://m.stock.naver.com/",
        "Accept": "application/json",
    }
    for mkt in ("KOSPI", "KOSDAQ"):
        for page in range(1, pages + 1):
            url = (
                f"https://m.stock.naver.com/api/stocks/marketValue/{mkt}"
                f"?page={page}&pageSize={page_size}"
            )
            try:
                req = Request(url, headers=headers)
                with urlopen(req, timeout=12) as resp:
                    raw = resp.read().decode("utf-8", errors="replace")
                payload = json.loads(raw)
            except (HTTPError, URLError, TimeoutError, ValueError, OSError):
                continue
            except Exception:
                continue
            stocks = payload.get("stocks") if isinstance(payload, dict) else None
            if not isinstance(stocks, list) or not stocks:
                break
            for row in stocks:
                if not isinstance(row, dict):
                    continue
                c6 = _mc_normalize_code6(row.get("itemCode") or row.get("reutersCode"))
                if not c6:
                    continue
                cap_raw = row.get("marketValueRaw")
                try:
                    cap = float(str(cap_raw).replace(",", "").strip())
                except Exception:
                    continue
                if cap > 0:
                    caps[c6] = max(caps.get(c6, 0.0), cap)
            if len(stocks) < page_size:
                break
    if not caps:
        return None
    ranked = sorted(caps.items(), key=lambda kv: kv[1], reverse=True)
    return [c for c, _ in ranked[: max(top_n, 1)]]


def _mc_top_from_fallback(top_n: int) -> List[str]:
    out: List[str] = []
    seen: Set[str] = set()
    for raw in SCAN_QUALITY_MC_FALLBACK_CODES:
        c6 = _mc_normalize_code6(raw)
        if not c6 or c6 in seen:
            continue
        seen.add(c6)
        out.append(c6)
        if len(out) >= top_n:
            break
    if len(out) < top_n:
        for raw in AI_TRAIN_FALLBACK_CODES:
            c6 = _mc_normalize_code6(raw)
            if not c6 or c6 in seen:
                continue
            seen.add(c6)
            out.append(c6)
            if len(out) >= top_n:
                break
    return out


def _mc_top_allowlist_for_scan(top_n: int) -> Optional[Set[str]]:
    """
    KOSPI+KOSDAQ 합산 시가총액 상위 top_n 종목코드 집합(6자리).
    우선순위: 메모리캐시 → pykrx → 네이버 → 디스크캐시 → 고정 폴백.
    고정 폴백까지 실패할 때만 None(호출측 전체 유니버스).
    """
    if top_n <= 0:
        return None
    today_s = datetime.date.today().isoformat()
    with _MC_TOP_FILTER_CACHE_LOCK:
        if (
            _MC_TOP_FILTER_CACHE_SET is not None
            and _MC_TOP_FILTER_CACHE_N == top_n
            and _MC_TOP_FILTER_CACHE_DAY == today_s
        ):
            return set(_MC_TOP_FILTER_CACHE_SET)

    ranked: Optional[List[str]] = None
    source = ""

    ranked = _mc_top_from_pykrx(top_n)
    if ranked:
        source = "pykrx"
    else:
        ranked = _mc_top_from_naver(top_n)
        if ranked:
            source = "naver"

    if ranked:
        _mc_top_save_disk(today_s, ranked, source)
        top_set = set(ranked[:top_n])
        _scan_log(
            f"[SCAN] 시총 유니버스: {source} KOSPI+KOSDAQ 합산 상위 {top_n}종 중 {len(top_set)}종 로드"
        )
        return _mc_top_store_memory(today_s, top_n, top_set)

    disk_codes, disk_tag = _mc_top_load_disk(top_n)
    if disk_codes:
        top_set = set(disk_codes[:top_n])
        _scan_log(
            f"[SCAN] 시총 유니버스: 디스크캐시({disk_tag}) {len(top_set)}종 "
            f"(실시간 시총 실패 대체)"
        )
        return _mc_top_store_memory(today_s, top_n, top_set)

    fb = _mc_top_from_fallback(top_n)
    if fb:
        top_set = set(fb)
        _scan_log(
            f"[SCAN] 시총 유니버스: 고정폴백 {len(top_set)}종 "
            f"(KRX/네이버 미수신 - 우량허용 유지)"
        )
        return _mc_top_store_memory(today_s, top_n, top_set)

    _scan_log("[SCAN] 시총상위 필터: 시총 목록 확보 실패 — 전체 유니버스로 스캔합니다.")
    return None


def _kiwoom_login_error_text(err_code: object) -> str:
    try:
        code = int(err_code)
    except Exception:
        return "알 수 없는 로그인 오류"
    return {
        0: "정상",
        -1: "로그인 이벤트 타임아웃 또는 사용자 취소",
        -100: "사용자정보교환 실패",
        -101: "서버 접속 실패",
        -102: "버전처리 실패",
    }.get(code, "키움 OpenAPI 로그인 실패")


def _to_yf_candidates(code: str) -> List[str]:
    s = str(code).strip().upper()
    if not s:
        return []
    if s.endswith(".KS") or s.endswith(".KQ"):
        base = s.split(".")[0]
        alt = f"{base}.KQ" if s.endswith(".KS") else f"{base}.KS"
        return [s, alt]
    if len(s) == 6 and s.isdigit():
        return [f"{s}.KS", f"{s}.KQ"]
    return [s]


def _safe_yf_ohlcv_filtered(code: str, min_len: int = 30) -> Optional[tuple]:
    """
    예외처리 + 코드변환 + 필터링 통합:
    - code -> .KS/.KQ 후보 변환
    - yfinance 다운로드 예외처리
    - df.empty / 컬럼 / NaN / 길이 필터 처리
    """
    if yf is None:
        return None
    for ticker in _to_yf_candidates(code):
        with _SCAN_BAD_TICKERS_LOCK:
            if ticker in _SCAN_BAD_TICKERS:
                continue
        try:
            now = datetime.datetime.now()
            with _SCAN_OHLCV_CACHE_LOCK:
                c = _SCAN_OHLCV_CACHE.get(ticker)
            if (
                c is not None
                and isinstance(c[0], datetime.datetime)
                and (now - c[0]).total_seconds() < float(SCAN_CACHE_TTL_SEC)
            ):
                df = c[1]
            else:
                # yfinance가 delisted 경고를 stderr로 직접 출력하는 것을 차단
                _sink = io.StringIO()
                # yf.download는 전역 상태를 공유해 병렬 스캔 시 결과가 섞이고 다단 컬럼을 반환 → Ticker.history 사용
                with contextlib.redirect_stderr(_sink), contextlib.redirect_stdout(_sink):
                    df = yf.Ticker(ticker).history(
                        period="6mo",
                        interval="1d",
                        auto_adjust=False,
                    )
                if df is not None and getattr(df.columns, "nlevels", 1) > 1:
                    df.columns = df.columns.get_level_values(0)
                # yfinance 원문 경고는 숨기고, 종목당 1회만 간단히 요약 로그 출력
                ymsg = _sink.getvalue()
                if ymsg and ("failed download" in ymsg.lower() or "possibly delisted" in ymsg.lower()):
                    need_log = False
                    with _SCAN_BAD_WARNED_TICKERS_LOCK:
                        if ticker not in _SCAN_BAD_WARNED_TICKERS:
                            _SCAN_BAD_WARNED_TICKERS.add(ticker)
                            need_log = True
                    if need_log:
                        _scan_log(f"[SCAN] yfinance 데이터 없음(1회): {ticker}")
                with _SCAN_OHLCV_CACHE_LOCK:
                    _SCAN_OHLCV_CACHE[ticker] = (now, df)
                    # 캐시가 과도하게 커지면 오래된 항목부터 정리
                    max_cache = max(100, int(SCAN_OHLCV_CACHE_MAX_ENTRIES))
                    if len(_SCAN_OHLCV_CACHE) > max_cache:
                        keys_sorted = sorted(
                            _SCAN_OHLCV_CACHE.keys(),
                            key=lambda k: _SCAN_OHLCV_CACHE.get(k, (datetime.datetime.min, None))[0],
                        )
                        drop_n = len(_SCAN_OHLCV_CACHE) - max_cache
                        for k in keys_sorted[:drop_n]:
                            _SCAN_OHLCV_CACHE.pop(k, None)

            if df is None or getattr(df, "empty", True):
                with _SCAN_BAD_TICKERS_LOCK:
                    _SCAN_BAD_TICKERS.add(ticker)
                continue
            if "Close" not in df.columns or "Volume" not in df.columns:
                with _SCAN_BAD_TICKERS_LOCK:
                    _SCAN_BAD_TICKERS.add(ticker)
                continue

            sub = df[["Close", "Volume"]].dropna()
            # NaN/비정상값 자동 제거
            closes_raw = [float(x) for x in sub["Close"].tolist()]
            volumes_raw = [float(x) for x in sub["Volume"].tolist()]
            closes: List[float] = []
            volumes: List[float] = []
            for c0, v0 in zip(closes_raw, volumes_raw):
                if not (c0 == c0 and v0 == v0):  # NaN
                    continue
                if c0 <= 0 or v0 < 0:
                    continue
                closes.append(c0)
                volumes.append(v0)
            n = min(len(closes), len(volumes))
            if n < min_len:
                with _SCAN_BAD_TICKERS_LOCK:
                    _SCAN_BAD_TICKERS.add(ticker)
                continue
            return ticker, closes[-n:], volumes[-n:]
        except Exception as e:
            _log(f"[SCAN] {ticker} download/parse error: {e}")
            with _SCAN_BAD_TICKERS_LOCK:
                _SCAN_BAD_TICKERS.add(ticker)
            continue
    return None


def _safe_yf_ohlcv_lists(code: str, min_len: int = 30) -> Optional[Dict[str, List[float]]]:
    data = _safe_yf_ohlcv_filtered(code, min_len=min_len)
    if data is None:
        return None
    ticker, _closes, _volumes = data
    try:
        with _SCAN_OHLCV_CACHE_LOCK:
            c = _SCAN_OHLCV_CACHE.get(ticker)
        df = c[1] if c is not None else None
        if df is None or getattr(df, "empty", True):
            return None
        need = ["Open", "High", "Low", "Close", "Volume"]
        if any(col not in df.columns for col in need):
            return None
        sub = df[need].dropna()
        out = {
            "opens": [float(x) for x in sub["Open"].tolist()],
            "highs": [float(x) for x in sub["High"].tolist()],
            "lows": [float(x) for x in sub["Low"].tolist()],
            "closes": [float(x) for x in sub["Close"].tolist()],
            "volumes": [float(x) for x in sub["Volume"].tolist()],
        }
        n = min(len(v) for v in out.values())
        if n < min_len:
            return None
        return {k: v[-n:] for k, v in out.items()}
    except Exception:
        return None


def _scan_one_symbol_with_yf(code: str) -> Optional[tuple]:
    try:
        data = _safe_yf_ohlcv_filtered(code, min_len=30)
        if data is None:
            return None
        _ticker, closes, volumes = data

        # 거래정지/상폐 추정 필터:
        # 최근 10일 거래량이 전부 0 이거나 가격이 비정상적으로 고정이면 제외
        tail_v = volumes[-10:] if len(volumes) >= 10 else volumes
        tail_c = closes[-10:] if len(closes) >= 10 else closes
        if tail_v and all(v <= 0 for v in tail_v):
            return None
        if tail_c and (max(tail_c) - min(tail_c) == 0):
            return None

        vol3 = sum(volumes[-3:]) / 3.0
        vol20 = sum(volumes[-20:]) / 20.0 if sum(volumes[-20:]) > 0 else 0.0
        if vol20 <= 0:
            return None
        # 1차 필터: 유동성/기본 품질
        min_vol = float(SCAN_MIN_DAILY_VOLUME_RELAXED if SCAN_RELAXED_MODE else SCAN_MIN_DAILY_VOLUME)
        if volumes[-1] < min_vol:
            return None
        if closes[-1] < float(MIN_ENTRY_PRICE) or closes[-1] > float(MAX_ENTRY_PRICE):
            return None
        ma5 = sum(closes[-5:]) / 5.0
        ma20 = sum(closes[-20:]) / 20.0
        close = closes[-1]

        passed_first = True
        # 2차 전략 필터
        if SCAN_RELAXED_MODE:
            cond_vol = vol3 > vol20 * 0.98
            cond_trend = ma5 >= ma20 * 0.99
            cond_close = close >= ma20 * 0.97
        else:
            cond_vol = vol3 > vol20 * 1.05
            cond_trend = ma5 >= ma20 * 0.998
            cond_close = close >= ma20 * 0.995
        code6 = str(code).replace(".KS", "").replace(".KQ", "")
        vol_ratio = vol3 / vol20

        # 거래대금(Trading Value) 기반 복합 스코어
        # TV = 종가 × 거래량 → 실제 수급 강도를 더 정확히 반영
        n20 = min(20, len(closes), len(volumes))
        n3 = min(3, len(closes), len(volumes))
        tv3 = sum(closes[-n3 + i] * volumes[-n3 + i] for i in range(n3)) / max(1, n3)
        tv20 = sum(closes[-n20 + i] * volumes[-n20 + i] for i in range(n20)) / max(1, n20)
        tv_ratio = (tv3 / tv20) if tv20 > 0 else vol_ratio
        # 복합 스코어: 거래량 비율 40% + 거래대금 비율 60%
        score = vol_ratio * 0.4 + tv_ratio * 0.6

        passed_second = bool(cond_vol and cond_trend and cond_close)
        # 우량 눌림 후보: 돌파형 2차 필터와 별개로 MA20 근처 조정·RSI 눌림 구간
        dip_score: Optional[float] = None
        if ma20 > 0:
            rel = close / ma20 - 1.0
            rsi14 = _compute_rsi_sma(closes, RSI_PERIOD)
            if (
                rsi14 is not None
                and float(QDIP_MIN_PRICE_VS_MA20_PCT) <= rel <= float(QDIP_MAX_PRICE_ABOVE_MA20_PCT)
                and float(QDIP_RSI_MIN_ENTRY) <= rsi14 <= float(QDIP_RSI_MAX_ENTRY)
                and vol_ratio >= float(QDIP_VOL_RATIO_MIN)
            ):
                dip_score = abs(rel - float(SCAN_QUALITY_DIP_TARGET_REL))
        return code6, score, passed_first, passed_second, dip_score
    except Exception as e:
        _log(f"[SCAN] rule eval error {code}: {e}")
        return None
    return None


def _scan_chunk(codes: List[str]) -> List[tuple]:
    out: List[tuple] = []
    for c in codes:
        try:
            r = _scan_one_symbol_with_yf(c)
            if r is not None:
                out.append(r)
        except Exception as e:
            _log(f"[SCAN] chunk item error {c}: {e}")
            continue
    return out


def _auto_scan_parallel_params(total_codes: int) -> tuple:
    """
    실행 환경에 맞춘 스캔 병렬 파라미터 자동 튜닝.
    - workers: CPU 코어/유니버스 크기 기반
    - chunk_size: workers 대비 균등 분할 + 최소/최대 제한
    """
    cpu = max(1, int(os.cpu_count() or 1))
    # I/O(yfinance) 성격이라 CPU 코어보다 넉넉히 사용
    workers = min(32, max(6, cpu * 3))
    # 유니버스가 작으면 과한 스레드/청크를 피함
    workers = min(workers, max(4, total_codes // 20))
    # 기본 청크를 유니버스/워커에 맞춰 조정
    chunk = max(16, min(96, (total_codes + workers - 1) // workers))
    return workers, chunk


def scan_market(kiwoom: "KiwoomOpenAPI", top_n: int = SCAN_TOP_N) -> Tuple[List[str], bool]:
    """
    전체 시장 -> 1차 필터 -> 2차 전략 -> 최종 top_n 종목.
    - 거래량: 최근 3일 평균 > 20일 평균 * 1.45
    - 추세: MA5 > MA20
    - 종가 > MA20
    - 최종: 거래량 증가율(vol3/vol20) 상위 top_n
    - SCAN_QUALITY_MC_TOP_N>0 이고 SCAN_QUALITY_ROTATE_WITH_FULL 이면 스캔 호출마다
      시총 상위 우량 유니버스 ↔ 전 종목 유니버스를 번갈아 적용합니다.
    Returns
    -------
    (선정 종목코드 목록, quality_scan_intent)
        quality_scan_intent True 이면 이번 스캔은 시총상위 슬롯(다음 매수는 시총허용종목에 눌림 규칙 바인딩).
    """
    _scan_console(
        f"[SCAN] ===== 시작 {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} "
        f"top_n={top_n} ====="
    )
    mc_n = max(0, int(SCAN_QUALITY_MC_TOP_N))
    apply_cap = _scan_should_apply_mc_cap() if mc_n > 0 else False
    quality_scan_intent = bool(mc_n > 0 and apply_cap)
    with _SCAN_CONSOLE_LOCK:
        _SCAN_LAST_SUMMARY.clear()

    try:
        codes = kiwoom.get_all_kr_codes()
        # 비정상 코드 제외(요청 최소화)
        codes = [c for c in codes if isinstance(c, str) and len(c.strip()) == 6 and c.strip().isdigit()]
    except Exception as e:
        _scan_log(f"[SCAN] code universe load failed: {e}")
        return [], quality_scan_intent
    if not codes:
        _scan_log("[SCAN] no universe codes")
        return [], quality_scan_intent

    if mc_n > 0 and apply_cap:
        allow = _mc_top_allowlist_for_scan(mc_n)
        if allow is not None:
            before_ct = len(codes)
            codes = [str(c).strip() for c in codes if str(c).strip() in allow]
            _scan_log(f"[SCAN] 우량 로테이션: 시총 상위 적용 유니버스 {before_ct} -> {len(codes)}종목 (기준≤{mc_n})")
        else:
            _scan_log("[SCAN] 시총 상위 목록 미사용(소스 실패) - 원래 유니버스로 진행")
    elif mc_n > 0 and not apply_cap:
        _scan_log("[SCAN] 우량 로테이션: 이번 스캔=전체 유니버스 (시총 상위 필터 생략, 비우량 포함). 다음 스캔=시총상위")

    if SCAN_RELAXED_MODE and len(codes) > int(SCAN_RELAXED_MAX_UNIVERSE):
        # 테스트스캔은 즉시성 우선: 전체 대신 샘플링으로 빠르게 후보를 확보
        step = max(1, len(codes) // int(SCAN_RELAXED_MAX_UNIVERSE))
        sampled = codes[::step][: int(SCAN_RELAXED_MAX_UNIVERSE)]
        _scan_log(
            f"[SCAN] 테스트스캔 샘플링 적용: {len(codes)} -> {len(sampled)}종목"
        )
        codes = sampled
    _scan_log(f"[SCAN] 전체 시장 종목수: {len(codes)}")
    with _SCAN_BAD_TICKERS_LOCK:
        bad_before = len(_SCAN_BAD_TICKERS)

    results: List[tuple] = []
    first_pass_candidates: List[tuple] = []
    dip_candidates: List[tuple] = []
    first_pass_count = 0
    auto_workers, auto_chunk = _auto_scan_parallel_params(len(codes))
    # 사용자가 상수를 높게 주면 상수 우선, 자동값은 하한/권장값 역할
    workers = max(auto_workers, max(4, int(SCAN_WORKERS)))
    chunk_size = max(auto_chunk, max(16, int(SCAN_CHUNK_SIZE)))
    _scan_log(f"[SCAN] parallel workers={workers}, chunk={chunk_size}")
    chunks: List[List[str]] = [codes[i : i + chunk_size] for i in range(0, len(codes), chunk_size)]
    total_chunks = len(chunks)
    _scan_log(f"[SCAN] 병렬 청크 수: {total_chunks}")
    completed_chunks = 0
    ex = ThreadPoolExecutor(max_workers=workers)
    started_at = datetime.datetime.now()
    timeout_sec = int(SCAN_RELAXED_TIMEOUT_SEC) if SCAN_RELAXED_MODE else 0
    try:
        futs = [ex.submit(_scan_chunk, ch) for ch in chunks]
        for f in as_completed(futs):
            if timeout_sec > 0:
                elapsed = (datetime.datetime.now() - started_at).total_seconds()
                if elapsed >= float(timeout_sec):
                    _scan_log(
                        f"[SCAN] 테스트스캔 시간제한 도달({int(elapsed)}s) - 중간 결과로 마감"
                    )
                    break
            try:
                batch = f.result()
                for r in batch:
                    code, ratio, passed_first, passed_second, dip_score = r
                    if passed_first:
                        first_pass_count += 1
                        first_pass_candidates.append((code, ratio))
                    if passed_second:
                        results.append((code, ratio))
                    if dip_score is not None:
                        dip_candidates.append((code, dip_score))
            except Exception as e:
                _scan_log(f"[SCAN] worker error: {e}")
                continue
            completed_chunks += 1
            if completed_chunks == 1 or completed_chunks == total_chunks or completed_chunks % 10 == 0:
                _scan_console(
                    f"[SCAN] 진행: 청크 {completed_chunks}/{total_chunks} | "
                    f"누적 2차통과 {len(results)}건"
                )
    finally:
        ex.shutdown(wait=False, cancel_futures=True)

    results.sort(key=lambda x: x[1], reverse=True)
    _scan_log(f"[SCAN] 1차 필터 통과: {first_pass_count}")
    _scan_log(f"[SCAN] 2차 전략 통과: {len(results)}")
    first_rejected = max(0, len(codes) - first_pass_count)
    second_rejected = max(0, first_pass_count - len(results))
    _scan_log(
        f"[SCAN] 필터 탈락 요약: 1차탈락 {first_rejected}, 2차탈락 {second_rejected}"
    )
    with _SCAN_BAD_TICKERS_LOCK:
        bad_after = len(_SCAN_BAD_TICKERS)
    _scan_log(f"[SCAN] 제외된 bad ticker: +{max(0, bad_after - bad_before)} (누적 {bad_after})")
    top_n_int = int(top_n)
    if top_n_int <= 0:
        selected = [c for c, _ratio in results]
    else:
        selected = [c for c, _ratio in results[: max(1, top_n_int)]]
    if SCAN_RELAXED_MODE and not selected and first_pass_candidates:
        first_pass_candidates.sort(key=lambda x: x[1], reverse=True)
        fallback_n = 120 if top_n_int <= 0 else max(1, top_n_int)
        selected = [c for c, _ratio in first_pass_candidates[:fallback_n]]
        _scan_log(
            f"[SCAN] 테스트스캔 보정: 2차 통과 0건 -> 1차 상위 {len(selected)}종목 대체"
        )
    elif SCAN_RELAXED_MODE and top_n_int <= 0 and len(selected) < 80 and first_pass_candidates:
        # 테스트스캔 ON에서는 진입대기 관찰용으로 최소 개수를 확보
        first_pass_candidates.sort(key=lambda x: x[1], reverse=True)
        keep = list(dict.fromkeys([c for c, _ in results]))
        for c, _ratio in first_pass_candidates:
            if c in keep:
                continue
            keep.append(c)
            if len(keep) >= 120:
                break
        if len(keep) > len(selected):
            selected = keep
            _scan_log(
                f"[SCAN] 테스트스캔 보강: 2차 통과 {len(results)}건 + 1차 보강 -> 최종 {len(selected)}종목"
            )
    if SCAN_RELAXED_MODE and not selected:
        # 최후 안전장치: 테스트스캔 ON인데 후보가 0이면 샘플 코드로 강제 채움
        fallback_n = 120 if top_n_int <= 0 else max(1, top_n_int)
        selected = list(dict.fromkeys([str(c).strip() for c in codes if str(c).strip()]))[:fallback_n]
        _scan_log(
            f"[SCAN] 테스트스캔 안전장치: 필터 결과 0건 -> 샘플 {len(selected)}종목 강제 대체"
        )
    if (not selected) and first_pass_candidates:
        # 일반 모드에서도 0건 종료를 피하기 위한 최소 안전장치
        first_pass_candidates.sort(key=lambda x: x[1], reverse=True)
        fallback_n = 60 if top_n_int <= 0 else max(1, top_n_int)
        selected = [c for c, _ratio in first_pass_candidates[:fallback_n]]
        _scan_log(
            f"[SCAN] 일반모드 보정: 2차 통과 0건 -> 1차 상위 {len(selected)}종목 대체"
        )
    dip_max = max(0, int(SCAN_QUALITY_DIP_MAX))
    if quality_scan_intent and dip_max > 0 and dip_candidates:
        dip_candidates.sort(key=lambda x: x[1])
        picked_set = set(selected)
        added = [c for c, _s in dip_candidates if c not in picked_set][:dip_max]
        if added:
            selected = list(selected) + added
            _scan_log(
                f"[SCAN] 우량 눌림 후보 추가 {len(added)}종목 "
                f"(조건충족 {len(dip_candidates)}종 중 상위{dip_max}): {added}"
            )
    elif quality_scan_intent:
        _scan_log("[SCAN] 우량 눌림 후보 0종목 (MA20·RSI·거래량 구간 충족 없음)")
    _scan_log(f"[SCAN] 최종 {len(selected)}종목: {selected}")
    return selected, quality_scan_intent


def run_backtest_for_codes(codes: List[str], lookback: int = BACKTEST_LOOKBACK_BARS) -> Dict[str, object]:
    """
    조건검색 결과 종목을 대상으로 실전 규칙에 가까운 손절/익절/비용 반영 백테스트 수행.
    """
    if not codes:
        return {"ok": False, "reason": "empty", "results": []}

    rows: List[Dict[str, object]] = []
    for c in codes:
        d = _safe_yf_ohlcv_lists(c, min_len=max(30, int(lookback)))
        if d is None:
            continue
        highs = list(d.get("highs", []))
        lows = list(d.get("lows", []))
        closes = list(d.get("closes", []))
        if len(closes) < max(25, int(lookback)):
            continue
        start = max(20, len(closes) - int(lookback))
        trades: List[float] = []
        wins = 0
        losses = 0
        for i in range(start, len(closes) - 1):
            entry = float(closes[i])
            if entry <= 0:
                continue
            atr_pct = _compute_atr_pct(
                [float(x) for x in highs[: i + 1]],
                [float(x) for x in lows[: i + 1]],
                [float(x) for x in closes[: i + 1]],
                ATR_PERIOD,
            )
            stop_abs = (
                min(float(ATR_STOP_MAX_PCT), max(float(ATR_STOP_MIN_PCT), atr_pct * float(ATR_STOP_MULT)))
                if atr_pct > 0
                else abs(float(STOP_LOSS_PCT))
            )
            take_abs = (
                min(float(ATR_TAKE_MAX_PCT), max(float(ATR_TAKE_MIN_PCT), atr_pct * float(ATR_TAKE_MULT)))
                if atr_pct > 0
                else abs(float(TAKE_PROFIT_PCT))
            )
            stop_price = entry * (1.0 - stop_abs)
            take_price = entry * (1.0 + take_abs)
            exit_price = float(closes[min(len(closes) - 1, i + int(AI_LABEL_HORIZON_DAYS))])
            reason = "horizon"
            for j in range(i + 1, min(len(closes), i + int(AI_LABEL_HORIZON_DAYS) + 1)):
                if float(lows[j]) <= stop_price:
                    exit_price = stop_price
                    reason = "stop_loss"
                    break
                if float(highs[j]) >= take_price:
                    exit_price = take_price
                    reason = "take_profit"
                    break
            net_ret = (exit_price / entry - 1.0) - float(AI_ROUND_TRIP_COST_PCT) - float(AI_SLIPPAGE_PCT)
            trades.append(net_ret)
            if net_ret > 0:
                wins += 1
            if reason == "stop_loss":
                losses += 1
        if not trades:
            continue
        ret = sum(trades) / float(len(trades))
        rows.append({"code": c, "ret": ret, "trades": len(trades), "wins": wins, "stop_losses": losses})

    if not rows:
        return {"ok": False, "reason": "no_data", "results": []}

    avg_ret = sum(float(r["ret"]) for r in rows) / float(len(rows))
    total_trades = sum(int(r.get("trades", 0) or 0) for r in rows)
    total_wins = sum(int(r.get("wins", 0) or 0) for r in rows)
    win_rate = (total_wins / float(total_trades)) if total_trades > 0 else 0.0
    rows.sort(key=lambda x: float(x["ret"]), reverse=True)
    return {"ok": True, "results": rows, "avg_ret": avg_ret, "win_rate": win_rate}

# 디버그 로그 토글(운용 중엔 False 추천)
# 필요할 때 KIWOOM_DEBUG=1 로 실행하면 _log() 출력이 켜집니다.
DEBUG = os.environ.get("KIWOOM_DEBUG", "").strip().lower() in ("1", "true", "yes", "y")


def _log(msg: str) -> None:
    if DEBUG:
        print(msg, flush=True)


_SCAN_CONSOLE_LOCK = threading.Lock()


def _scan_console(msg: str) -> None:
    """종목 검색 진행 상황 — CMD/터미널 표준출력에 표시(항상)."""
    try:
        with _SCAN_CONSOLE_LOCK:
            print(msg, flush=True)
    except Exception:
        pass


_SCAN_DAYLOG_KEYS = ("1차 필터 통과", "2차 전략 통과", "필터 탈락 요약", "우량 눌림 후보", "우량 로테이션", "보정:", "bad ticker")
_SCAN_LAST_SUMMARY: List[str] = []


def _scan_log(msg: str) -> None:
    """스캔 단계 로그 — CMD/터미널에 항상 출력. 요약 줄은 _on_scan_finished 에서 일일 로그로 재출력."""
    _scan_console(msg)
    if any(k in msg for k in _SCAN_DAYLOG_KEYS):
        with _SCAN_CONSOLE_LOCK:
            _SCAN_LAST_SUMMARY.append(msg[:400])


def _compute_rsi_sma(prices: List[int], period: int) -> Optional[float]:
    """
    단순 이동평균 기반 RSI 계산(간단 필터용).
    Wilder 방식(지수/평활)과 완전히 동일하진 않지만, 추세/모멘텀 필터로는 충분히 동작합니다.
    """
    if len(prices) < period + 1:
        return None

    deltas = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    gains = 0.0
    losses = 0.0
    for d in deltas[-period:]:
        if d > 0:
            gains += d
        else:
            losses += -d

    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _compute_rsi_series_wilder(prices: List[int], period: int) -> List[Optional[float]]:
    """
    Wilder 방식 RSI 시계열(정확도/성능 균형).
    반환 길이는 prices와 같고, 초반 period 이전 인덱스는 None입니다.
    """
    n = len(prices)
    if n < period + 1:
        return [None] * n

    deltas = [prices[i] - prices[i - 1] for i in range(1, n)]
    gains = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]

    rsi: List[Optional[float]] = [None] * n

    # 첫 RSI(인덱스 period)
    avg_gain = sum(gains[:period]) / float(period)
    avg_loss = sum(losses[:period]) / float(period)

    def _rsi_from(av_g: float, av_l: float) -> float:
        if av_l == 0:
            return 100.0
        rs = av_g / av_l
        return 100.0 - (100.0 / (1.0 + rs))

    rsi[period] = _rsi_from(avg_gain, avg_loss)

    for i in range(period + 1, n):
        # delta는 deltas[i-1] (prices[i-1] -> prices[i])
        gain = gains[i - 1]
        loss = losses[i - 1]
        avg_gain = (avg_gain * (period - 1) + gain) / float(period)
        avg_loss = (avg_loss * (period - 1) + loss) / float(period)
        rsi[i] = _rsi_from(avg_gain, avg_loss)

    return rsi


def _ema_series(values: List[float], span: int) -> List[float]:
    if not values or span <= 1:
        return [float(v) for v in values]
    alpha = 2.0 / (float(span) + 1.0)
    out: List[float] = []
    ema = float(values[0])
    out.append(ema)
    for v in values[1:]:
        x = float(v)
        ema = alpha * x + (1.0 - alpha) * ema
        out.append(ema)
    return out


def _compute_atr_pct(
    highs: List[float],
    lows: List[float],
    closes: List[float],
    period: int = ATR_PERIOD,
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
    atr = sum(trs[-int(period):]) / float(min(len(trs), int(period)))
    last_close = float(closes[-1])
    return (atr / last_close) if last_close > 0 else 0.0


_KRX_STATIC_HOLIDAYS = {
    2026: (
        "2026-01-01", "2026-02-16", "2026-02-17", "2026-02-18", "2026-03-02",
        "2026-05-01", "2026-05-05", "2026-05-25", "2026-06-03", "2026-07-17",
        "2026-08-17", "2026-09-24", "2026-09-25", "2026-10-05", "2026-10-09",
        "2026-12-25", "2026-12-31",
    ),
    2027: (
        "2027-01-01", "2027-02-08", "2027-02-09", "2027-03-01", "2027-05-05",
        "2027-05-13", "2027-08-16", "2027-09-14", "2027-09-15", "2027-09-16",
        "2027-10-04", "2027-10-11", "2027-12-27", "2027-12-31",
    ),
}

# 개장 지연(분), 마감 지연(분). 수능일: 10:00~16:30.
_KRX_SPECIAL_SESSIONS = {
    datetime.date(2026, 11, 19): (60, 60),
}


@lru_cache(maxsize=8)
def _krx_holidays(year: int) -> set:
    """
    KRX 공휴일/휴장일 = 정적 목록 ∪ pykrx 조회.
    pykrx 실패 시 정적 목록만 사용(목록 없는 연도는 주말만 휴장).
    """
    out = set()
    for s in _KRX_STATIC_HOLIDAYS.get(int(year), ()):
        out.add(datetime.date.fromisoformat(s))
    try:
        from pykrx.website.krx.market import core  # type: ignore

        df = core.MKD01023().fetch(str(year))
        col = "calnd_dd" if "calnd_dd" in df.columns else df.columns[0]
        for x in df[col].tolist():
            s = str(x).replace("/", "-").strip()[:10]
            try:
                out.add(datetime.date.fromisoformat(s))
            except Exception:
                continue
    except Exception:
        pass
    return out


def _is_krx_trading_day(d: datetime.date) -> bool:
    if d.weekday() >= 5:
        return False
    return d not in _krx_holidays(d.year)


def _krx_session_shift(d: datetime.date) -> Tuple[int, int]:
    """(개장 지연분, 마감 지연분). 수능일 +60/+60, 연초 첫 거래일 개장 10:00(+60/0)."""
    if d in _KRX_SPECIAL_SESSIONS:
        return _KRX_SPECIAL_SESSIONS[d]
    first = datetime.date(d.year, 1, 1)
    while first < d and not _is_krx_trading_day(first):
        first += datetime.timedelta(days=1)
    if first == d and _is_krx_trading_day(d):
        return (60, 0)
    return (0, 0)


def _krx_session_bounds(d: datetime.date) -> Dict[str, int]:
    """장 구간 경계(자정 기준 초). 특수 개장일이면 지연 반영."""
    o, c = _krx_session_shift(d)
    h = 3600
    return {
        "pre_start": 8 * h + o * 60,
        "pre_end": 8 * h + 50 * 60 + o * 60,
        "open": 9 * h + o * 60,
        "regular_end": 15 * h + 20 * 60 + c * 60,
        "close": 15 * h + 30 * 60 + c * 60,
        "after_end": 20 * h,
        "open_shift_min": o,
        "close_shift_min": c,
    }


def _qtime_sec(t: "QTime") -> int:
    return t.hour() * 3600 + t.minute() * 60 + t.second()


def _us_market_session_name(now_kst: Optional[datetime.datetime] = None) -> str:
    """
    미국주식 정규장만 운용.
    미국 동부시간 기준 평일 09:30~16:00을 정규장으로 본다.
    """
    now = now_kst or datetime.datetime.now()
    try:
        if now.tzinfo is None:
            now = now.replace(tzinfo=ZoneInfo("Asia/Seoul"))
        et = now.astimezone(ZoneInfo("America/New_York"))
    except Exception:
        return "시간확인불가"
    if et.weekday() >= 5:
        return "휴장"
    t = et.time()
    if datetime.time(9, 30) <= t < datetime.time(16, 0):
        return "정규장"
    if datetime.time(4, 0) <= t < datetime.time(9, 30):
        return "프리마켓"
    if datetime.time(16, 0) <= t < datetime.time(20, 0):
        return "애프터마켓"
    return "장외"


def _is_us_regular_market_open(now_kst: Optional[datetime.datetime] = None) -> bool:
    return _us_market_session_name(now_kst) == "정규장"


def _ai_ece_binary(y_true: List[int], proba_pos: List[float], n_bins: int = 10) -> float:
    """예측 신뢰도 vs 실제 양비율(간단 ECE)."""
    n = min(len(y_true), len(proba_pos))
    if n <= 0 or n_bins <= 0:
        return 0.0
    ece_w = 0.0
    for bi in range(n_bins):
        lo = bi / float(n_bins)
        hi = (bi + 1) / float(n_bins)
        confs_acc: List[float] = []
        labels_acc: List[int] = []
        for yt, pv in zip(y_true[:n], proba_pos[:n]):
            p = float(pv)
            last = bi == n_bins - 1
            if last:
                ok = p >= lo - 1e-15
            else:
                ok = lo <= p < hi - 1e-15
            if ok:
                confs_acc.append(p)
                labels_acc.append(int(yt))
        if not labels_acc:
            continue
        w = len(labels_acc) / float(n)
        avg_conf = sum(confs_acc) / float(len(confs_acc))
        avg_acc = sum(labels_acc) / float(len(labels_acc))
        ece_w += abs(avg_conf - avg_acc) * w
    return float(ece_w)


def _ai_binary_metrics_safe(
    y_true: List[int], proba_pos: List[float]
) -> Dict[str, float]:
    out: Dict[str, float] = {}
    if not y_true or not proba_pos or brier_score_loss is None:
        return out
    try:
        m = min(len(y_true), len(proba_pos))
        yt = [int(y_true[i]) for i in range(m)]
        pp = [max(1e-6, min(1.0 - 1e-6, float(proba_pos[i]))) for i in range(m)]
        out["brier"] = float(brier_score_loss(yt, pp))
        uniq = set(yt)
        if len(uniq) >= 2 and roc_auc_score is not None:
            out["auc"] = float(roc_auc_score(yt, pp))
        if log_loss is not None and len(uniq) >= 2:
            out["logloss"] = float(log_loss(yt, pp, labels=[0, 1]))
        out["ece"] = float(_ai_ece_binary(yt, pp))
    except Exception:
        pass
    return out


def _ai_label_distribution_report(y: List[int], net_rets: List[float]) -> Dict[str, Any]:
    if not y:
        return {}
    pos = sum(1 for v in y if int(v) == 1)
    nn = sorted(float(x) for x in net_rets) if net_rets else []
    pct = lambda q: nn[int(round((len(nn) - 1) * q))] if nn else None
    return {
        "n_samples": len(y),
        "pos_rate": round(pos / max(1, len(y)), 6),
        "neg_rate": round(1.0 - pos / max(1, len(y)), 6),
        "net_ret_p10": pct(0.1),
        "net_ret_p50": pct(0.5),
        "net_ret_p90": pct(0.9),
    }


def _ai_write_calibration_json(path: str, report: Dict[str, Any]) -> None:
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
    except Exception as e:
        _log(f"[AI] calibration metrics JSON 저장 실패: {path} ({e})")


def _normalize_kiwoom_account_for_match(acc: str) -> str:
    """Chejan 9201과 engine.account 비교용(숫자만)."""
    return "".join(ch for ch in str(acc or "") if ch.isdigit())


def _parse_chejan_code9001(raw: str) -> str:
    """Chejan 종목코드 FID 9001 → 6자리 숫자."""
    s = str(raw or "").strip().lstrip("A")
    digits = "".join(ch for ch in s if ch.isdigit())
    if len(digits) >= 6:
        c6 = digits[-6:]
        return c6 if c6.isdigit() else ""
    return ""


def _safe_opw_money_int(cell: object) -> Optional[int]:
    """opw00004 등 TR 금액 필드 안전 파싱(부호·콤마)."""
    raw = str(cell or "").strip().replace(",", "").replace(" ", "")
    if not raw:
        return None
    neg = raw.startswith("-") or raw.startswith("\u2212")
    body = raw[1:] if neg else raw
    digits = "".join(ch for ch in body if ch.isdigit())
    if not digits:
        return None
    try:
        v = int(digits)
    except ValueError:
        return None
    return -v if neg else v


SCREEN_NO_ACCOUNT_EVAL = "0404"


# ─── strategy_params.json 로더 ────────────────────────────────────────────────
# 앱 기동 시 strategy_params.json 을 읽어, 자동 조정된 파라미터를 모듈 전역변수로 덮어씀.
# 파일이 없거나 파싱 실패 시 하드코딩 기본값을 그대로 사용한다.
def _apply_strategy_params_from_file() -> None:
    """strategy_params.json 의 value 필드를 모듈 globals 에 적용한다."""
    _json_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "strategy_params.json")
    if not os.path.exists(_json_path):
        return
    try:
        import json as _json
        with open(_json_path, "r", encoding="utf-8") as _f:
            _data = _json.load(_f)
        _params = _data.get("params", {})
        _g = globals()
        _applied: list = []
        for _key, _meta in _params.items():
            if _key in _g and "value" in _meta:
                _old = _g[_key]
                _new = _meta["value"]
                if type(_old) is int and not isinstance(_new, bool):
                    _new = int(_new)
                _g[_key] = _new
                if _old != _new:
                    _applied.append(f"{_key}: {_old} → {_new}")
        if _applied:
            print(f"[PARAM-LOAD] strategy_params.json 로드 — {len(_applied)}개 파라미터 적용:")
            for _msg in _applied:
                print(f"  {_msg}")
    except Exception as _e:
        print(f"[PARAM-LOAD] strategy_params.json 로드 실패: {_e}")


_apply_strategy_params_from_file()
# ─────────────────────────────────────────────────────────────────────────────


class KiwoomOpenAPI(QAxWidget):
    """
    KHOpenAPICtrl.1 을 PyQt5 QAxWidget으로 감싼 래퍼.
    - CommConnect 로그인
    - TR 요청/응답(QEventLoop 대기)
    - 실시간 주식체결 데이터 수신
    - 체결·주문통보(OnReceiveChejanData) → 엔진 콜백 on_chejan_data
    """

    def __init__(self):
        super().__init__()
        self.setControl("KHOPENAPI.KHOpenAPICtrl.1")
        if self.isNull():
            # 컨트롤이 로딩되지 않으면 OnEventConnect 등이 절대 안 들어옵니다.
            _log("[Kiwoom] KHOPENAPI control not loaded (QAxWidget isNull).")

        # 이벤트 루프/응답 저장소
        self._login_loop: Optional[QEventLoop] = None
        self._tr_loops: Dict[str, QEventLoop] = {}
        self._tr_responses: Dict[str, object] = {}
        self._last_error: Optional[str] = None
        # opw00018 싱글데이터(추정예탁자산 등) — request_holdings 직후 엔진이 읽어감
        self.last_opw00018_summary: Dict[str, int] = {}
        # GetServerGubun == "1" → 모의투자. 로그인 후 설정.
        self.is_mock_server = False

        # 이벤트 연결
        self.OnEventConnect.connect(self._on_event_connect)
        self.OnReceiveTrData.connect(self._on_receive_tr_data)
        self.OnReceiveRealData.connect(self._on_receive_real_data)
        self.OnReceiveMsg.connect(self._on_receive_msg)
        try:
            self.OnReceiveChejanData.connect(self._on_receive_chejan_data)
        except Exception:
            pass

        # 콜백(엔진에서 주입)
        self.on_real_price = None  # type: ignore
        self.on_vi_event = None  # Callable[[str, bool, str], None] | None
        self.on_chejan_data = None  # Callable[[Dict[str, str]], None] | None
        self._real_codes: List[str] = []

    def _chejan_get(self, fid: str) -> str:
        try:
            v = self.dynamicCall("GetChejanData(QString)", str(fid))
            return str(v or "").strip()
        except Exception:
            try:
                v = self.dynamicCall("GetChejanData(int)", int(str(fid)))
                return str(v or "").strip()
            except Exception:
                return ""

    def _on_receive_chejan_data(self, *args):
        fn = getattr(self, "on_chejan_data", None)
        if not callable(fn):
            return
        try:
            gubun = str(args[0] if len(args) > 0 else "").strip()
        except Exception:
            gubun = ""
        if gubun != "0":
            return
        try:
            fields = {
                "gubun": gubun,
                "9201": self._chejan_get("9201"),
                "9203": self._chejan_get("9203"),
                "9001": self._chejan_get("9001"),
                "913": self._chejan_get("913"),
                "907": self._chejan_get("907"),
                "905": self._chejan_get("905"),
                "910": self._chejan_get("910"),
                "911": self._chejan_get("911"),
                "909": self._chejan_get("909"),
                "908": self._chejan_get("908"),
                "902": self._chejan_get("902"),
            }
            fn(fields)
        except Exception:
            pass

    def _on_event_connect(self, *args):
        # Kiwoom 이벤트 시그니처가 환경에 따라 타입/파라미터 형태가 달라질 수 있어
        # 인자를 관대하게 받습니다.
        err_code = args[0] if args else -1
        _log(f"[Kiwoom] OnEventConnect err_code={err_code}")
        if self._login_loop is not None:
            self._tr_responses["login_err"] = err_code
            self._login_loop.exit()

    def _on_receive_msg(self, screen_no, rqname, trcode, msg):
        # 필요 시 로깅 강화 가능
        self._last_error = f"OnReceiveMsg: {msg}"

    def _list_login_accounts(self) -> List[str]:
        """GetLoginInfo에서 계좌목록을 모은다(ACCNO/ACCLIST 등)."""
        merged: List[str] = []
        for key in ("ACCNO", "ACCLIST"):
            try:
                raw = self.dynamicCall("GetLoginInfo(QString)", key)
            except Exception:
                raw = ""
            for part in str(raw or "").replace(",", ";").split(";"):
                clean = part.strip()
                if clean:
                    merged.append(clean)
        # 숫자만 기준으로 중복 제거(하이픈/공백 차이 흡수)
        seen: set = set()
        out: List[str] = []
        for a in merged:
            digits = "".join(ch for ch in a if ch.isdigit())
            key = digits or a
            if key in seen:
                continue
            seen.add(key)
            out.append(a)
        return out

    @staticmethod
    def _account_digits(acc: str) -> str:
        return "".join(ch for ch in str(acc or "") if ch.isdigit())

    def _pick_account_from_list(self, accounts: List[str], wanted: str) -> Optional[str]:
        """정확 일치 → 숫자만 일치 → 앞자리(부분번호) 일치 순으로 선택."""
        wanted = str(wanted or "").strip()
        if not wanted:
            return None
        for a in accounts:
            if a == wanted:
                return a
        want_d = self._account_digits(wanted)
        if not want_d:
            return None
        for a in accounts:
            if self._account_digits(a) == want_d:
                return a
        # 사용자가 8자리만 입력한 경우 등: 목록 계좌가 wanted 로 시작하면 매칭
        # (예: wanted=81314462, list=8131446211)
        prefix_hits = [
            a for a in accounts
            if self._account_digits(a).startswith(want_d) and len(self._account_digits(a)) >= len(want_d)
        ]
        if len(prefix_hits) == 1:
            return prefix_hits[0]
        if len(prefix_hits) > 1:
            # 동명이면 국내(8…) 우선
            domestic_hits = [a for a in prefix_hits if self._account_digits(a).startswith("8")]
            if len(domestic_hits) == 1:
                return domestic_hits[0]
        return None

    def _get_login_account(self) -> str:
        # 로그인 직후 ACCNO가 비거나 해외계좌만 먼저 보이는 경우가 있어 짧게 재시도
        accounts: List[str] = []
        preferred_default = str(DEFAULT_KIWOOM_ACCOUNT_NO).strip()
        for attempt in range(1, 9):
            accounts = self._list_login_accounts()
            if accounts and (
                self._pick_account_from_list(accounts, preferred_default)
                or any(self._account_digits(a).startswith("8") for a in accounts)
            ):
                break
            print(
                f"[Kiwoom] account list retry {attempt}/8: {accounts or '(empty)'}",
                flush=True,
            )
            _log(f"[Kiwoom] account list retry {attempt}/8: {accounts}")
            loop = QEventLoop()
            QTimer.singleShot(500, loop.quit)
            loop.exec_()

        if not accounts:
            raise RuntimeError("No accounts returned from Kiwoom (GetLoginInfo ACCNO/ACCLIST).")

        try:
            snap = {
                k: str(self.dynamicCall("GetLoginInfo(QString)", k) or "")
                for k in ("USER_ID", "GetServerGubun", "ACCOUNT_CNT")
            }
            print(f"[Kiwoom] login info: {snap}", flush=True)
            _log(f"[Kiwoom] login info: {snap}")
        except Exception:
            snap = {}

        print(f"[Kiwoom] Login accounts: {accounts}", flush=True)
        _log(f"[Kiwoom] Login accounts: {accounts}")

        server_gubun = str(snap.get("GetServerGubun", "") or "").strip()
        # 키움: GetServerGubun == "1" → 모의투자, 그 외 실서버
        is_mock = server_gubun == "1"
        self.is_mock_server = is_mock
        if is_mock:
            print(
                "[Kiwoom] WARN 모의투자 서버 접속(GetServerGubun=1). "
                f"실계좌 {DEFAULT_KIWOOM_ACCOUNT_NO} 자동매매는 실전 접속이 필요합니다.",
                flush=True,
            )

        env_account_no = os.environ.get("KIWOOM_ACCOUNT_NO", "").strip()
        # 국내 자동매매: DEFAULT_KIWOOM_ACCOUNT_NO를 환경변수보다 우선
        preferred: List[str] = []
        if preferred_default:
            preferred.append(preferred_default)
        if env_account_no and env_account_no not in preferred:
            preferred.append(env_account_no)

        for want in preferred:
            picked = self._pick_account_from_list(accounts, want)
            if not picked:
                continue
            # 기본 국내계좌가 목록에 없을 때만 환경변수 계좌 허용(단, 국내 8xxx만)
            if want == env_account_no and preferred_default:
                if self._account_digits(want) != self._account_digits(preferred_default):
                    if not self._account_digits(picked).startswith("8"):
                        continue
                    print(
                        f"[Kiwoom] WARN 기본계좌 {preferred_default} 없음 → "
                        f"환경변수 계좌 {picked} 사용",
                        flush=True,
                    )
            print(f"[Kiwoom] Using account: {picked}", flush=True)
            _log(f"[Kiwoom] Using account: {picked} (wanted={want})")
            return picked

        env_account_index = os.environ.get("KIWOOM_ACCOUNT_INDEX", "").strip()
        if env_account_index:
            try:
                idx = int(env_account_index)
            except ValueError:
                raise RuntimeError(f"KIWOOM_ACCOUNT_INDEX must be int. Got: {env_account_index}")
            if 0 <= idx < len(accounts):
                selected = accounts[idx]
                print(f"[Kiwoom] Using account: {selected}", flush=True)
                _log(f"[Kiwoom] Using account by KIWOOM_ACCOUNT_INDEX={idx} -> {selected}")
                return selected
            raise RuntimeError(
                f"KIWOOM_ACCOUNT_INDEX out of range: {idx} (available={accounts})"
            )

        # 국내증권(보통 8로 시작)만 후보 — 해외 6110 계좌로 조용히 진행 금지
        domestic = [
            a for a in accounts
            if self._account_digits(a).startswith("8")
        ]
        if len(domestic) == 1:
            selected = domestic[0]
            print(
                f"[Kiwoom] WARN 기본계좌 {preferred_default} 없음 → "
                f"유일한 국내계좌 {selected} 사용",
                flush=True,
            )
            _log(f"[Kiwoom] Using sole domestic account: {selected}")
            return selected
        if len(domestic) > 1:
            raise RuntimeError(
                f"국내계좌가 여러 개입니다. KIWOOM_ACCOUNT_NO로 지정하세요. "
                f"wanted={preferred_default}, available={accounts}"
            )

        raise RuntimeError(
            f"국내계좌 {preferred_default} 를 키움 로그인 목록에서 찾지 못했습니다. "
            f"USER_ID={snap.get('USER_ID', '?')} "
            f"server={'모의투자' if is_mock else '실전'}(gubun={server_gubun or '-'}) "
            f"available={accounts}. "
            + (
                "지금 모의투자로 로그인되어 있습니다. 키움 로그인 창에서 '실서버/실전'을 선택한 뒤 "
                f"계좌 {preferred_default} 가 보이는 ID로 다시 로그인하세요."
                if is_mock
                else "실전이면 해당 계좌가 있는 키움 ID로 로그인했는지 확인하세요."
            )
        )

    def login(self, timeout_ms: int = 30000) -> str:
        if self.isNull():
            raise RuntimeError(
                "KHOPENAPI control not loaded. "
                "Please verify Kiwoom OpenAPI (32-bit) is installed/registered correctly "
                "and matches your Python 32-bit interpreter."
            )

        self._login_loop = QEventLoop()
        self._tr_responses.pop("login_err", None)
        self._last_error = None
        _log("[Kiwoom] CommConnect() called")
        self.dynamicCall("CommConnect()")
        try:
            state = self.dynamicCall("GetConnectState()")
            _log(f"[Kiwoom] GetConnectState() after CommConnect: {state}")
        except Exception:
            pass

        # 로그인 대기
        timer = QTimer()
        timer.setSingleShot(True)
        def _on_timeout():
            # 이벤트가 안 들어오면 여기로 옴
            self._tr_responses["login_err"] = -1
            self._last_error = f"CommConnect timeout after {timeout_ms}ms"
            _log(f"[Kiwoom] Login timeout: {self._last_error}")
            if self._login_loop is not None:
                self._login_loop.quit()

        timer.timeout.connect(_on_timeout)
        timer.start(timeout_ms)
        _log("[Kiwoom] Waiting for login event...")

        # COM 이벤트가 Qt 이벤트루프만으로 전달되지 않는 환경 대응
        # (Kiwoom OpenAPI는 COM 객체이므로 pythoncom 메시지 펌핑이 필요할 수 있음)
        try:
            import pythoncom  # type: ignore

            def _pump_com():
                try:
                    pythoncom.PumpWaitingMessages()
                except Exception:
                    pass

            pump_timer = QTimer()
            pump_timer.setInterval(50)
            pump_timer.timeout.connect(_pump_com)
            pump_timer.start()
        except Exception:
            pump_timer = None

        self._login_loop.exec_()
        timer.stop()
        if pump_timer is not None:
            pump_timer.stop()

        err = self._tr_responses.get("login_err", -1)
        if err != 0:
            reason = _kiwoom_login_error_text(err)
            raise RuntimeError(f"Login failed: err_code={err} ({reason}) last_error={self._last_error}")

        return self._get_login_account()

    def request_daily_closes(self, code: str, end_date_yyyymmdd: str, count: int = 60) -> List[int]:
        """
        opt10081(주식일봉차트조회)로 일봉 '종가/현재가'를 가져와 최근 count개 정수 리스트 반환.
        반환 순서는 '오래된->최신'으로 정렬합니다.
        """
        rqname = f"REQ_DAILY_{code}"
        self._tr_loops[rqname] = QEventLoop()
        self._tr_responses.pop(rqname, None)

        # Inputs: 종목코드, 기준일자, 수정주가구분
        self.dynamicCall("SetInputValue(QString, QString)", "종목코드", code)
        self.dynamicCall("SetInputValue(QString, QString)", "기준일자", end_date_yyyymmdd)
        self.dynamicCall("SetInputValue(QString, QString)", "수정주가구분", "1")

        # CommRqData(rqname, trcode, prevnext, screenNo)
        self.dynamicCall(
            "CommRqData(QString, QString, int, QString)",
            rqname,
            "opt10081",
            0,
            SCREEN_NO,
        )

        # 응답 대기(무한 대기 방지)
        loop = self._tr_loops[rqname]
        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(loop.quit)
        timer.start(30000)
        loop.exec_()
        timer.stop()
        self._tr_loops.pop(rqname, None)

        closes = self._tr_responses.get(rqname)
        if not closes:
            return []

        # 최신 count개만 사용
        closes = closes[-count:]
        return closes

    def get_all_kr_codes(self) -> List[str]:
        """
        코스피(0) + 코스닥(10) 전체 종목코드(6자리) 반환.
        """
        merged: List[str] = []
        for market in ("0", "10"):
            raw = self.dynamicCall("GetCodeListByMarket(QString)", market)
            for t in str(raw or "").split(";"):
                s = t.strip()
                if len(s) == 6 and s.isdigit():
                    merged.append(s)
        return list(dict.fromkeys(merged))

    def request_daily_closes_and_volumes(
        self,
        code: str,
        end_date_yyyymmdd: str,
        count: int = 60,
    ) -> tuple[List[int], List[int]]:
        """
        opt10081(주식일봉차트조회)로 최근 count개 일봉의 '현재가'와 '거래량'을 함께 가져옵니다.
        반환 순서는 '오래된->최신'입니다.
        """
        rqname = f"REQ_DAILY_VOL_{code}"
        self._tr_loops[rqname] = QEventLoop()
        self._tr_responses.pop(rqname, None)

        self.dynamicCall("SetInputValue(QString, QString)", "종목코드", code)
        self.dynamicCall("SetInputValue(QString, QString)", "기준일자", end_date_yyyymmdd)
        self.dynamicCall("SetInputValue(QString, QString)", "수정주가구분", "1")

        self.dynamicCall(
            "CommRqData(QString, QString, int, QString)",
            rqname,
            "opt10081",
            0,
            SCREEN_NO,
        )

        loop = self._tr_loops[rqname]
        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(loop.quit)
        timer.start(30000)
        loop.exec_()
        timer.stop()
        self._tr_loops.pop(rqname, None)

        result = self._tr_responses.get(rqname)
        if not isinstance(result, dict):
            return [], []

        closes = result.get("closes", [])
        volumes = result.get("volumes", [])
        return closes[-count:], volumes[-count:]

    def request_daily_ohlcv(
        self,
        code: str,
        end_date_yyyymmdd: str,
        count: int = 60,
    ) -> Dict[str, List[int]]:
        """
        opt10081로 최근 count개 일봉 OHLCV를 가져옵니다.
        반환 순서는 '오래된->최신'입니다.
        """
        rqname = f"REQ_DAILY_OHLCV_{code}"
        self._tr_loops[rqname] = QEventLoop()
        self._tr_responses.pop(rqname, None)

        self.dynamicCall("SetInputValue(QString, QString)", "종목코드", code)
        self.dynamicCall("SetInputValue(QString, QString)", "기준일자", end_date_yyyymmdd)
        self.dynamicCall("SetInputValue(QString, QString)", "수정주가구분", "1")

        self.dynamicCall(
            "CommRqData(QString, QString, int, QString)",
            rqname,
            "opt10081",
            0,
            SCREEN_NO,
        )

        loop = self._tr_loops[rqname]
        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(loop.quit)
        timer.start(30000)
        loop.exec_()
        timer.stop()
        self._tr_loops.pop(rqname, None)

        result = self._tr_responses.get(rqname)
        if not isinstance(result, dict):
            return {
                "opens": [],
                "highs": [],
                "lows": [],
                "closes": [],
                "volumes": [],
                "dates": [],
            }
        return {
            "opens": list(result.get("opens", []))[-count:],
            "highs": list(result.get("highs", []))[-count:],
            "lows": list(result.get("lows", []))[-count:],
            "closes": list(result.get("closes", []))[-count:],
            "volumes": list(result.get("volumes", []))[-count:],
            "dates": list(result.get("dates", []))[-count:],
        }

    def request_holdings(
        self, account: str, password: str = "", timeout_ms: int = 30000
    ) -> Optional[Dict[str, Dict[str, int]]]:
        """
        opw00018(계좌평가잔고내역)으로 보유수량/매입가를 가져옵니다.
        주의: 비밀번호 TR이 필요한 환경에서는 password 설정이 필요합니다.
        반환: {code: {'qty': int, 'avg_price': int}}. TR 미수신 시 None(타임아웃 등).
        """
        rqname = "REQ_HOLDINGS"
        self._tr_loops[rqname] = QEventLoop()
        self._tr_responses.pop(rqname, None)

        self.dynamicCall("SetInputValue(QString, QString)", "계좌번호", account)
        self.dynamicCall("SetInputValue(QString, QString)", "비밀번호", password)
        self.dynamicCall("SetInputValue(QString, QString)", "비밀번호입력매체구분", "00")
        self.dynamicCall("SetInputValue(QString, QString)", "조회구분", "2")

        self.dynamicCall(
            "CommRqData(QString, QString, int, QString)",
            rqname,
            "opw00018",
            0,
            SCREEN_NO,
        )

        loop = self._tr_loops[rqname]
        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(loop.quit)
        timer.start(timeout_ms)
        loop.exec_()
        timer.stop()

        self._tr_loops.pop(rqname, None)

        data = self._tr_responses.get(rqname)
        # 키가 없으면 TR 미수신(타임아웃 등) — 빈 잔고 {}와 구분해야 positions를 덮어쓰지 않음
        if data is None:
            return None
        if not isinstance(data, dict):
            return None
        try:
            print(
                f"[HOLDINGS-TR] account={account} rows={len(data)} pw={'set' if str(password).strip() else 'empty'}",
                flush=True,
            )
        except Exception:
            pass
        return data

    def request_account_eval_summary(
        self, account: str, password: str = "", timeout_ms: int = 15000
    ) -> Optional[Dict[str, int]]:
        """
        opw00004 계좌평가현황 — 싱글데이터 손익 항목(증광 HTS 「누적투자손익」 등와 정합 가능).
        """
        rqname = "REQ_OPW00004"
        self._tr_loops[rqname] = QEventLoop()
        self._tr_responses.pop(rqname, None)

        self.dynamicCall("SetInputValue(QString, QString)", "계좌번호", account)
        self.dynamicCall("SetInputValue(QString, QString)", "비밀번호", password)
        self.dynamicCall("SetInputValue(QString, QString)", "상장폐지조회구분", "0")
        self.dynamicCall("SetInputValue(QString, QString)", "비밀번호입력매체구분", "00")
        self.dynamicCall(
            "CommRqData(QString, QString, int, QString)",
            rqname,
            "opw00004",
            0,
            SCREEN_NO_ACCOUNT_EVAL,
        )
        loop = self._tr_loops[rqname]
        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(loop.quit)
        timer.start(timeout_ms)
        loop.exec_()
        timer.stop()
        self._tr_loops.pop(rqname, None)
        data = self._tr_responses.get(rqname)
        if not isinstance(data, dict):
            return None
        return dict(data)

    def request_current_price(self, code: str, timeout_ms: int = 3000) -> int:
        """opt10001 현재가 TR로 단일 종목 현재가 조회."""
        clean = str(code).replace(".KS", "").replace(".KQ", "").strip()
        if len(clean) != 6 or not clean.isdigit():
            return 0
        rqname = f"REQ_PRICE_{clean}"
        self._tr_loops[rqname] = QEventLoop()
        self._tr_responses.pop(rqname, None)

        self.dynamicCall("SetInputValue(QString, QString)", "종목코드", clean)
        self.dynamicCall(
            "CommRqData(QString, QString, int, QString)",
            rqname,
            "opt10001",
            0,
            SCREEN_NO,
        )

        loop = self._tr_loops[rqname]
        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(loop.quit)
        timer.start(timeout_ms)
        loop.exec_()
        timer.stop()
        self._tr_loops.pop(rqname, None)

        try:
            return abs(int(self._tr_responses.get(rqname, 0) or 0))
        except Exception:
            return 0

    def send_order_market(self, account: str, code: str, qty: int, order_type: int) -> None:
        """
        SendOrder로 시장가 주문 전송.
        - 주문명: "주문"
        - 화면번호: "0101"
        - 매매구분: BUY_SELL_MARKET (2=시장가)
        - 가격: 0 (시장가)
        """
        if qty <= 0:
            return

        # SendOrder(rqname, screenNo, accNo, orderType, code, qty, price, hogaGb, orgOrderNo)
        self.dynamicCall(
            "SendOrder(QString, QString, QString, int, QString, int, int, QString, QString)",
            ["주문", SCREEN_NO, account, order_type, code, qty, 0, HOGA_MARKET, ""],
        )

    def get_master_code_name(self, code: str) -> str:
        clean = str(code).replace(".KS", "").replace(".KQ", "").strip()
        if len(clean) != 6 or not clean.isdigit():
            return clean or str(code)
        try:
            name = self.dynamicCall("GetMasterCodeName(QString)", clean)
            s = str(name or "").strip()
            return s if s else clean
        except Exception:
            return clean

    def set_real_reg(self, codes: List[str], fid_list: str = "10") -> None:
        """
        SetRealReg로 '주식체결' 실시간 등록.
        fid_list는 콤마/세미콜론 구분이 아니라 문자열 그대로 전달되며,
        보통 "10;11;12" 같은 형태를 사용합니다(환경에 따라 요구 형식이 다를 수 있어 조정 필요).
        """
        if not codes:
            return
        # 키움 실시간 등록은 화면번호/종목수 제한이 있어 100개 단위로 분할 등록.
        # 첫 호출만 "0"(신규), 이후는 "1"(추가)로 전달.
        cleaned = [str(c).strip().replace(".KS", "").replace(".KQ", "") for c in codes]
        cleaned = [c for c in cleaned if len(c) == 6 and c.isdigit()]
        if not cleaned:
            return
        uniq_codes = list(dict.fromkeys(cleaned))
        chunk_size = 100
        base = int(REAL_SCREEN_NO)
        for i in range(0, len(uniq_codes), chunk_size):
            chunk = uniq_codes[i : i + chunk_size]
            screen_no = str(base + (i // chunk_size))
            real_type = "0" if i == 0 else "1"
            self.dynamicCall(
                "SetRealReg(QString, QString, QString, QString)",
                screen_no,
                ";".join(chunk),
                fid_list,
                real_type,
            )
        self._real_codes = uniq_codes
        if VI_GUARD_ENABLED:
            # FID 9068(VI발동구분)을 등록하면 해당 종목의 'VI발동/해제' 실시간이 수신된다.
            vi_base = int(VI_REAL_SCREEN_NO)
            for i in range(0, len(uniq_codes), chunk_size):
                chunk = uniq_codes[i : i + chunk_size]
                try:
                    self.dynamicCall(
                        "SetRealReg(QString, QString, QString, QString)",
                        str(vi_base + (i // chunk_size)),
                        ";".join(chunk),
                        "9068",
                        "1",
                    )
                except Exception:
                    pass

    def reregister_real(self, fid_list: str = "10") -> int:
        """실시간 등록 전체 해제 후 재등록. 재등록한 종목 수 반환."""
        codes = list(self._real_codes)
        if not codes:
            return 0
        try:
            self.dynamicCall("SetRealRemove(QString, QString)", "ALL", "ALL")
        except Exception:
            pass
        self.set_real_reg(codes, fid_list=fid_list)
        return len(codes)

    def get_master_stock_flags(self, code: str) -> str:
        """GetMasterConstruction(감리구분) + GetMasterStockState(종목상태) 원문을 '|'로 합쳐 반환."""
        clean = str(code).replace(".KS", "").replace(".KQ", "").strip()
        if len(clean) != 6 or not clean.isdigit():
            return ""
        parts = []
        for fn in ("GetMasterConstruction(QString)", "GetMasterStockState(QString)"):
            try:
                s = str(self.dynamicCall(fn, clean) or "").strip()
                if s:
                    parts.append(s)
            except Exception:
                continue
        return "|".join(parts)

    def _on_receive_tr_data(
        self,
        screen_no: str,
        rqname: str,
        trcode: str,
        record_name: str,
        prev_next: str,
        data_length: int,
        error_code: str,
        message: str,
        splm_msg: str,
    ):
        try:
            if rqname.startswith("REQ_PRICE_") and trcode == "opt10001":
                s_price = self.dynamicCall(
                    "GetCommData(QString, QString, int, QString)",
                    trcode,
                    rqname,
                    0,
                    "현재가",
                )
                self._tr_responses[rqname] = abs(
                    int(str(s_price or "").strip().replace(",", "") or "0")
                )

            elif rqname.startswith("REQ_DAILY_OHLCV_") and trcode == "opt10081":
                cnt = int(self.dynamicCall("GetRepeatCnt(QString, QString)", trcode, rqname))
                opens_desc: List[int] = []
                highs_desc: List[int] = []
                lows_desc: List[int] = []
                closes_desc: List[int] = []
                vols_desc: List[int] = []
                dates_desc: List[str] = []

                for i in range(cnt):
                    row: Dict[str, int] = {}
                    for field, key in (
                        ("시가", "open"),
                        ("고가", "high"),
                        ("저가", "low"),
                        ("현재가", "close"),
                        ("거래량", "volume"),
                    ):
                        raw = self.dynamicCall(
                            "GetCommData(QString, QString, int, QString)",
                            trcode,
                            rqname,
                            i,
                            field,
                        )
                        row[key] = abs(int(str(raw).strip().replace(",", "") or "0"))
                    opens_desc.append(row["open"])
                    highs_desc.append(row["high"])
                    lows_desc.append(row["low"])
                    closes_desc.append(row["close"])
                    vols_desc.append(row["volume"])
                    try:
                        d_raw = self.dynamicCall(
                            "GetCommData(QString, QString, int, QString)",
                            trcode,
                            rqname,
                            i,
                            "일자",
                        )
                        d_s = "".join(ch for ch in str(d_raw or "") if ch.isdigit())[:8]
                    except Exception:
                        d_s = ""
                    dates_desc.append(d_s)

                self._tr_responses[rqname] = {
                    "opens": list(reversed(opens_desc)),
                    "highs": list(reversed(highs_desc)),
                    "lows": list(reversed(lows_desc)),
                    "closes": list(reversed(closes_desc)),
                    "volumes": list(reversed(vols_desc)),
                    "dates": list(reversed(dates_desc)),
                }

            elif rqname.startswith("REQ_DAILY_VOL_") and trcode == "opt10081":
                cnt = int(self.dynamicCall("GetRepeatCnt(QString, QString)", trcode, rqname))
                closes_desc: List[int] = []
                vols_desc: List[int] = []

                for i in range(cnt):
                    s_close = self.dynamicCall(
                        "GetCommData(QString, QString, int, QString)",
                        trcode,
                        rqname,
                        i,
                        "현재가",
                    )
                    close = int(str(s_close).strip().replace(",", ""))
                    closes_desc.append(close)

                    s_vol = self.dynamicCall(
                        "GetCommData(QString, QString, int, QString)",
                        trcode,
                        rqname,
                        i,
                        "거래량",
                    )
                    vol = int(str(s_vol).strip().replace(",", ""))
                    vols_desc.append(vol)

                closes = list(reversed(closes_desc))
                volumes = list(reversed(vols_desc))
                self._tr_responses[rqname] = {"closes": closes, "volumes": volumes}

            elif rqname.startswith("REQ_DAILY_") and trcode == "opt10081":
                # 반복 레코드 수
                cnt = int(self.dynamicCall("GetRepeatCnt(QString, QString)", trcode, rqname))
                closes_desc: List[int] = []

                # opt10081에서 "현재가"는 일봉의 종가(혹은 기준가)로 쓰이는 경우가 많습니다.
                for i in range(cnt):
                    s_close = self.dynamicCall(
                        "GetCommData(QString, QString, int, QString)",
                        trcode,
                        rqname,
                        i,
                        "현재가",
                    )
                    close = int(str(s_close).strip().replace(",", ""))
                    closes_desc.append(close)

                # Kiwoom 응답은 대개 최신->과거 순서이므로 오래된->최신으로 뒤집습니다.
                closes = list(reversed(closes_desc))
                self._tr_responses[rqname] = closes

            elif rqname == "REQ_HOLDINGS" and trcode == "opw00018":
                cnt = int(self.dynamicCall("GetRepeatCnt(QString, QString)", trcode, rqname))
                holdings: Dict[str, Dict[str, int]] = {}

                for i in range(cnt):
                    code = self.dynamicCall(
                        "GetCommData(QString, QString, int, QString)",
                        trcode,
                        rqname,
                        i,
                        "종목번호",
                    )
                    code = str(code).strip()
                    code = code.lstrip("A")  # 종목번호가 "A005930" 형태로 올 수 있음

                    qty_s = self.dynamicCall(
                        "GetCommData(QString, QString, int, QString)",
                        trcode,
                        rqname,
                        i,
                        "보유수량",
                    )
                    avg_s = self.dynamicCall(
                        "GetCommData(QString, QString, int, QString)",
                        trcode,
                        rqname,
                        i,
                        "매입가",
                    )
                    buy_amt_s = self.dynamicCall(
                        "GetCommData(QString, QString, int, QString)",
                        trcode,
                        rqname,
                        i,
                        "매입금액",
                    )
                    cur_s = self.dynamicCall(
                        "GetCommData(QString, QString, int, QString)",
                        trcode,
                        rqname,
                        i,
                        "현재가",
                    )

                    qty = int(str(qty_s).strip().replace(",", "") or "0")
                    avg_price = int(str(avg_s).strip().replace(",", "") or "0")
                    buy_amt = int(str(buy_amt_s).strip().replace(",", "") or "0")
                    cur_price = abs(int(str(cur_s).strip().replace(",", "") or "0"))
                    if avg_price <= 0 and qty > 0 and buy_amt > 0:
                        avg_price = int(buy_amt / qty)

                    # 일부 계좌/환경에서 매입가가 0으로 오는 경우가 있어 qty 기준으로 우선 반영
                    if qty > 0:
                        holdings[code] = {
                            "qty": qty,
                            "avg_price": max(1, int(avg_price)),
                            "current_price": max(0, int(cur_price)),
                        }

                # 싱글데이터: HTS 「추정예탁자산」·총평가손익 등과 정합
                summary: Dict[str, int] = {}
                for fld in (
                    "추정예탁자산",
                    "총평가금액",
                    "총매입금액",
                    "총평가손익금액",
                    "실현손익",
                    "손익금액",
                ):
                    try:
                        s = self.dynamicCall(
                            "GetCommData(QString, QString, int, QString)",
                            trcode,
                            rqname,
                            0,
                            fld,
                        )
                    except Exception:
                        continue
                    vi = _safe_opw_money_int(s)
                    if vi is not None:
                        summary[fld] = int(vi)
                self.last_opw00018_summary = summary
                self._tr_responses[rqname] = holdings

            elif rqname == "REQ_OPW00004" and str(trcode).lower() == "opw00004":
                out: Dict[str, int] = {}
                for fld in (
                    "당일투자손익",
                    "당월투자손익",
                    "누적투자손익",
                    "손익금액",
                    "추정예탁자산",
                    "예탁자산평가액",
                    "유가잔고평가액",
                    "총매입금액",
                    "예수금",
                ):
                    try:
                        s = self.dynamicCall(
                            "GetCommData(QString, QString, int, QString)",
                            trcode,
                            rqname,
                            0,
                            fld,
                        )
                    except Exception:
                        continue
                    vi = _safe_opw_money_int(s)
                    if vi is not None:
                        out[fld] = int(vi)
                self._tr_responses[rqname] = out

        finally:
            loop = self._tr_loops.get(rqname)
            if loop is not None:
                loop.exit()

    def _on_receive_real_data(self, code: str, real_type: str, real_data: str):
        # 실시간 '주식체결'에서 현재가 FID 10
        rt = real_type.strip()
        if rt.startswith("VI"):
            if self.on_vi_event is not None:
                try:
                    def _g(fid: int) -> str:
                        return str(self.dynamicCall("GetCommRealData(QString, int)", code, fid) or "").strip()

                    kind = _g(9068)
                    released_at = _g(1224).replace(":", "")
                    released = kind == "2" or (released_at.isdigit() and int(released_at) > 0)
                    detail = f"구분={kind or '-'} 적용={_g(1225) or '-'} 발동가={_g(1221) or '-'} 해제시각={released_at or '-'}"
                    self.on_vi_event(str(code).strip(), released, detail)
                except Exception:
                    pass
            return
        if rt != "주식체결":
            return

        if self.on_real_price is not None:
            try:
                price_raw = self.dynamicCall("GetCommRealData(QString, int)", code, 10)
                # 키움 FID 10 현재가는 등락 방향 부호가 붙을 수 있으므로 절대값 사용
                price = abs(int(str(price_raw).strip().replace(",", "")))
                self.on_real_price(code, price)
            except Exception:
                # 실시간 데이터 포맷이 환경별로 다를 수 있어 방어
                pass


class KiwoomOverseasOpenAPI(QAxWidget):
    """
    KFOpenAPI 해외/글로벌 계열 컨트롤 래퍼 뼈대.

    국내 KHOpenAPI와 TR/주문/종목 체계가 달라서 실제 조회·주문은 별도 구현합니다.
    현재 단계에서는 GUI 분리와 설치/등록 상태 확인용으로만 사용합니다.
    """

    def __init__(self):
        super().__init__()
        self._dll_dir_handle = None
        try:
            openapi_g = r"C:\OpenApiG"
            if os.path.isdir(openapi_g):
                if hasattr(os, "add_dll_directory"):
                    self._dll_dir_handle = os.add_dll_directory(openapi_g)
                try:
                    import ctypes

                    ctypes.windll.kernel32.SetDllDirectoryW(openapi_g)
                except Exception:
                    pass
        except Exception:
            pass
        self.setControl("KFOPENAPI.KFOpenAPICtrl.1")
        self.available = not self.isNull()
        self.account_no = DEFAULT_OVERSEAS_ACCOUNT_NO
        self.last_error: str = "" if self.available else "KFOPENAPI control not loaded"
        self._openapi_dir = r"C:\OpenApiG"
        self._login_loop: Optional[QEventLoop] = None
        self._responses: Dict[str, object] = {}
        try:
            self.OnEventConnect.connect(self._on_event_connect)
            self.OnReceiveMsg.connect(self._on_receive_msg)
        except Exception as e:
            self.last_error = f"KFOPENAPI event connect failed: {e}"

    def _on_event_connect(self, *args):
        err_code = args[0] if args else -1
        self._responses["login_err"] = err_code
        self._responses["event_err"] = err_code
        if self._login_loop is not None:
            self._login_loop.exit()

    def _on_receive_msg(self, screen_no, rqname, trcode, msg):
        self.last_error = f"OnReceiveMsg: {msg}"

    def accounts(self) -> List[str]:
        merged: List[str] = []
        # KFOPENAPI 버전/상품에 따라 계좌목록 키가 다르게 동작할 수 있어 후보를 모두 확인.
        for key in ("ACCNO", "ACCLIST", "ACCOUNT", "ACCOUNTNO"):
            try:
                raw = self.dynamicCall("GetLoginInfo(QString)", key)
            except Exception:
                raw = ""
            for a in str(raw or "").replace(",", ";").split(";"):
                clean = a.strip()
                if clean:
                    merged.append(clean)
        return list(dict.fromkeys(merged))

    def login_info_snapshot(self) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for key in ("ACCOUNT_CNT", "ACCNO", "ACCLIST", "ACCOUNT", "ACCOUNTNO", "USER_ID", "USER_NAME", "GetServerGubun"):
            try:
                out[key] = str(self.dynamicCall("GetLoginInfo(QString)", key) or "")
            except Exception as e:
                out[key] = f"ERROR:{e}"
        return out

    def _wait_accounts_ready(self, timeout_ms: int = OVERSEAS_ACCOUNT_READY_TIMEOUT_MS) -> List[str]:
        accounts = self.accounts()
        if accounts:
            return accounts
        loop = QEventLoop()
        deadline = datetime.datetime.now() + datetime.timedelta(milliseconds=int(timeout_ms))

        poll_timer = QTimer()

        def _poll():
            accounts_now = self.accounts()
            if accounts_now:
                self._responses["accounts_ready"] = accounts_now
                loop.exit()
                return
            try:
                self._responses["ready_state"] = int(self.dynamicCall("GetConnectState()") or 0)
            except Exception:
                self._responses["ready_state"] = 0
            if datetime.datetime.now() >= deadline:
                loop.quit()

        poll_timer.setInterval(500)
        poll_timer.timeout.connect(_poll)
        poll_timer.start()
        _poll()
        loop.exec_()
        poll_timer.stop()
        return list(self._responses.get("accounts_ready", []) or self.accounts())

    def login(self, timeout_ms: int = OVERSEAS_LOGIN_TIMEOUT_MS) -> List[str]:
        if self.isNull():
            raise RuntimeError("KFOPENAPI control not loaded")
        self._login_loop = QEventLoop()
        self._responses.pop("login_err", None)
        self.last_error = ""
        # KFOPENAPI 문서: CommConnect(LONG nAutoUpgrade), 0=수동, 1=자동 버전처리.
        # OCX를 우리 앱 안에서 쓰는 구조라 기본은 수동(0)으로 둔다.
        auto_upgrade = 1 if int(OVERSEAS_AUTO_UPGRADE) else 0
        old_cwd = os.getcwd()
        changed_cwd = False
        try:
            if os.path.isdir(self._openapi_dir):
                os.chdir(self._openapi_dir)
                changed_cwd = True
            ret = self.dynamicCall("CommConnect(long)", auto_upgrade)
        except Exception:
            ret = self.dynamicCall("CommConnect(int)", auto_upgrade)
        finally:
            if changed_cwd:
                try:
                    os.chdir(old_cwd)
                except Exception:
                    pass
        self._responses["connect_ret"] = ret

        timer = QTimer()
        timer.setSingleShot(True)

        def _on_timeout():
            # 일부 KFOPENAPI 환경은 OnEventConnect가 늦거나 누락될 수 있어 연결상태/계좌를 최종 확인.
            try:
                state = int(self.dynamicCall("GetConnectState()") or 0)
            except Exception:
                state = 0
            accounts = self.accounts()
            if accounts:
                self._responses["login_err"] = 0
                self._responses["accounts_on_timeout"] = accounts
                self.last_error = ""
            else:
                self._responses["login_err"] = -1
                self.last_error = (
                    f"CommConnect timeout after {timeout_ms}ms "
                    f"(ret={self._responses.get('connect_ret')}, state={state})"
                )
            if self._login_loop is not None:
                self._login_loop.quit()

        timer.timeout.connect(_on_timeout)
        timer.start(timeout_ms)

        try:
            import pythoncom  # type: ignore

            def _pump_com():
                try:
                    pythoncom.PumpWaitingMessages()
                except Exception:
                    pass

            pump_timer = QTimer()
            pump_timer.setInterval(50)
            pump_timer.timeout.connect(_pump_com)
            pump_timer.start()
        except Exception:
            pump_timer = None

        poll_timer = QTimer()

        def _poll_connected():
            try:
                state = int(self.dynamicCall("GetConnectState()") or 0)
            except Exception:
                state = 0
            accounts = self.accounts()
            self._responses["last_state"] = state
            if accounts:
                self._responses["login_err"] = 0
                self._responses["accounts_on_poll"] = accounts
                if self._login_loop is not None:
                    self._login_loop.exit()

        poll_timer.setInterval(500)
        poll_timer.timeout.connect(_poll_connected)
        poll_timer.start()
        self._login_loop.exec_()
        timer.stop()
        poll_timer.stop()
        if pump_timer is not None:
            pump_timer.stop()

        err = self._responses.get("login_err", -1)
        if err != 0:
            raise RuntimeError(
                f"KFOPENAPI login failed: err_code={err} "
                f"state={self._responses.get('last_state', '-')} "
                f"ret={self._responses.get('connect_ret', '-')} "
                f"last_error={self.last_error}"
            )
        accounts = self._wait_accounts_ready()
        if not accounts:
            try:
                state = int(self.dynamicCall("GetConnectState()") or 0)
            except Exception:
                state = 0
            raise RuntimeError(
                f"KFOPENAPI login returned empty accounts "
                f"(state={state}, ret={self._responses.get('connect_ret', '-')}, "
                f"event={self._responses.get('event_err', '-')}, "
                f"info={self.login_info_snapshot()})"
            )
        return accounts

    def market_session_name(self) -> str:
        return _us_market_session_name()

    def is_regular_market_open(self) -> bool:
        return _is_us_regular_market_open()


class TradingEngine:
    def __init__(self, kiwoom: KiwoomOpenAPI, account: str, stock_codes: List[str]):
        self.kiwoom = kiwoom
        self.account = account
        self.stock_codes = stock_codes
        self._is_mock_server = bool(getattr(kiwoom, "is_mock_server", False))
        self.connected_at = datetime.datetime.now()
        self.last_keepalive_at: Optional[datetime.datetime] = None
        self._disconnected_at: Optional[datetime.datetime] = None  # 끊김 최초 감지 시각 (UI용)
        self.on_log = None  # type: ignore

        # 실시간 가격
        self.current_price: Dict[str, int] = {}
        self.current_price_source: Dict[str, str] = {}

        # 포트폴리오(내부 관리): code -> {qty, avg_price}
        self.positions: Dict[str, Dict[str, int]] = {}

        # MA 캐시: code -> {ma5, ma20}
        self.ma: Dict[str, Dict[str, float]] = {}

        # 목표 익절(트레일링 진입) 도달 여부: code -> bool
        self.tp_reached: Dict[str, bool] = {c: False for c in stock_codes}

        # AI 모델
        self.ai_model = None
        self.ai_trained_date: Optional[datetime.date] = None
        self.ai_model_name: str = ""
        self.ai_model_samples: int = 0
        self.ai_model_loaded_from_disk: bool = False
        self._ai_needs_retrain: bool = False
        self._ai_train_attempted_date: Optional[datetime.date] = None
        self._ai_train_scheduled: bool = False
        self._ai_training_active: bool = False
        self._regime_update_scheduled: bool = False
        self._regime_update_active: bool = False
        self._strat_ohlcv_warm_scheduled: bool = False
        self._strat_ohlcv_warm_active: bool = False
        self._holdings_sync_scheduled: bool = False
        self._holdings_sync_active: bool = False
        self._holdings_sync_kind: str = "periodic"
        # 3단계: 학습 품질 메트릭(GUI·매수규모 연동)
        self.ai_val_auc: Optional[float] = None
        self.ai_val_brier: Optional[float] = None
        self.ai_val_ece: Optional[float] = None
        self.ai_live_injected: int = 0
        self.ai_live_wins: int = 0
        self._day_bias_last_log_at: Optional[datetime.datetime] = None

        # 멀티 전략 비교·선택(strategy_manager.py)
        self.strategy_manager: Optional[StrategyManager] = None

        # 스캔 로테이션 슬롯(시총 상위)·시총 허용 집합 — 우량 종목만 눌림 진입 규칙 적용용
        self._scan_quality_intent_active: bool = False
        self._quality_mcap_allowlist: Set[str] = set()

        # 중복 주문 방지
        self.pending_buy: Dict[str, bool] = {c: False for c in stock_codes}
        self.pending_sell: Dict[str, bool] = {c: False for c in stock_codes}
        self.last_buy_date: Dict[str, datetime.date] = {}
        self.entry_time: Dict[str, datetime.datetime] = {}
        self.buy_cooldown_until: Dict[str, datetime.datetime] = {}
        self.sell_cooldown_until: Dict[str, datetime.datetime] = {}  # 매도 timeout 후 재시도 억제
        self._sell_retry_counts: Dict[str, int] = {}  # 종목별 매도 재시도 횟수 (주문 교체 후에도 누적)
        self.pending_orders: Dict[str, Dict[str, object]] = {}
        self.realized_pnl_krw: float = 0.0
        # opw00004 계좌평가(키움 HTS 집계·수수료 반영 가능) — 주기 호출 후 갱신
        self.opw_daily_invest_pnl_krw: Optional[int] = None
        self.opw_month_invest_pnl_krw: Optional[int] = None
        self.opw_accum_invest_profit_loss_krw: Optional[int] = None
        self.opw_holdings_eval_pnl_krw: Optional[int] = None
        # 2026-07-15: HTS 「추정예탁자산」과 GUI를 맞추기 위해 opw00018/04 싱글데이터 저장
        self.opw_estimated_deposit_krw: Optional[int] = None
        self.opw_eval_fetched_at: Optional[datetime.datetime] = None
        self.opw_deposit_fetched_at: Optional[datetime.datetime] = None
        self._scale_in_done: Dict[str, bool] = {}
        self._quality_entry_codes: Set[str] = set()  # 우량눌림으로 진입한 종목(물타기 대상)
        self._quality_avg_down_count: Dict[str, int] = {}
        self._quality_avg_down_at: Dict[str, datetime.datetime] = {}
        self._last_theme_prefetch_at: Optional[datetime.datetime] = None
        self._theme_prefetch_thread: Optional[threading.Thread] = None
        self._daily_report_done_date: Optional[datetime.date] = None
        self._log_file_date: Optional[datetime.date] = None
        self._log_file_handle: Optional[object] = None  # 일별 로그 파일 핸들
        self._log_analysis_done_date: Optional[datetime.date] = None  # 자동 분석 실행 날짜
        self._log_auto_tune_done_date: Optional[datetime.date] = None  # 자동 파라미터 튜닝 실행 날짜
        self._daily_strategy_done_date: Optional[datetime.date] = None  # 일일 전략 패키지 적용 날짜
        # 손절 후 재진입 추적: 손절 체결가 기록 → 해당가 회복 시에만 재진입 허용
        self._stop_loss_prices: Dict[str, float] = {}   # code → 손절 체결가
        self._reentry_codes: set = set()                # 현재 재진입 모드인 종목
        self._auto_bt_running = False
        self._auto_bt_last_at: Optional[datetime.datetime] = None
        self._auto_bt_state: Dict[str, object] = {
            "mode": "neutral",
            "avg_ret": 0.0,
            "win_rate": 0.0,
            "reason": "not_run",
        }
        self._auto_bt_score_by_code: Dict[str, float] = {}
        self._auto_bt_lock = threading.Lock()
        # 체결 기록 → performance.TradeTracker 연동용 콜백
        self.on_trade_fill: Optional[Callable[[Dict[str, object]], None]] = None
        self.trade_tracker: Optional[TradeTracker] = None
        self._trade_feature_cache: Dict[str, tuple] = {}
        base = os.path.dirname(os.path.abspath(__file__))
        self.order_event_csv_path = os.path.join(base, "data", "order_event_log.csv")
        self.ai_model_path = os.path.join(base, "data", AI_MODEL_FILENAME)

        # 루프 주기
        self.timer = QTimer()
        self.timer.setInterval(2000)
        self.timer.timeout.connect(self.on_tick)

        # 실시간 콜백 연결
        self.kiwoom.on_real_price = self._on_real_price
        self._code_last_tick: Dict[str, float] = {}
        self._halt_day: Optional[datetime.date] = None
        self._halt_active = False
        self._halt_since: Optional[datetime.datetime] = None
        self._halt_low_since: Optional[datetime.datetime] = None
        self._halt_resumed_at: Optional[datetime.datetime] = None
        self._halt_healthy_seen = False
        self._halt_long_warned = False
        self._halt_order_logged: Dict[tuple, float] = {}
        self._halt_rate_samples: deque = deque()
        self._halt_rate_baseline = 0.0
        self.kiwoom.on_vi_event = self._on_vi_event
        self._vi_active: Dict[str, float] = {}
        self._vi_released_at: Dict[str, float] = {}
        self._vi_order_logged: Dict[tuple, float] = {}
        self._stock_state_day: Optional[datetime.date] = None
        self._stock_state_cache: Dict[str, str] = {}
        self._last_any_tick = 0.0
        self._feed_day: Optional[datetime.date] = None
        self._feed_watch_since = 0.0
        self._feed_retries = 0
        self._feed_last_retry = -1e9
        self._feed_gave_up = False
        self._real_price_tick_total = 0
        self._real_price_tick_window = 0
        self._real_price_window_started_at = datetime.datetime.now()

        self._last_ma_refresh_date: Optional[datetime.date] = None
        self._need_portfolio_sync = True
        self._last_periodic_holdings_sync_at: Optional[datetime.datetime] = None
        self._last_reconcile_sync_at: Optional[datetime.datetime] = None
        self._last_positions_sync_warn_at: Optional[datetime.datetime] = None
        # 2026-07-09: opw00018이 일시적으로 0건을 반환했을 때 즉시 보유를 지워버리면
        # (023910이 17:17:04에 0건→0주로 지워진 뒤 1초 후 다시 49주로 조회된 사례),
        # 그 사이 매수금지("이미 보유중") 체크가 풀려 중복 매수 주문이 나가는 사고가
        # 있었다. 연속으로 몇 번 0건이 나와야 실제로 청산된 것으로 신뢰하도록 스트릭을 둔다.
        self._holdings_empty_streak: int = 0
        self._last_news_prefetch_at: Optional[datetime.datetime] = None
        self._news_prefetch_thread: Optional[threading.Thread] = None
        self.last_holdings_tr_rows: int = 0
        self._last_holdings_brief: str = ""
        self._last_buy_block_reason_at: Dict[str, datetime.datetime] = {}
        self._buy_block_stats: Dict[str, int] = {}
        self._buy_block_stats_date: Optional[datetime.date] = None
        self._last_buy_diag_at: Optional[datetime.datetime] = None
        self._ai_model_missing_warned: bool = False
        self._strat_force_eval_done: bool = False
        self._last_regime_fallback_at: Optional[datetime.datetime] = None
        # StrategyManager 평가용 키움 OHLCV 캐시 (메인 스레드에서만 TR 호출)
        self._strat_ohlcv_cache: Dict[str, Tuple[List[float], List[float], List[float], List[float]]] = {}
        self._strat_ohlcv_cache_at: Dict[str, datetime.datetime] = {}
        self._strat_ohlcv_cache_lock = threading.Lock()
        self._last_strat_ohlcv_warm_at: Optional[datetime.datetime] = None
        self._last_holdings_price_sync_at: Optional[datetime.datetime] = None
        self._last_holdings_price_brief: str = ""
        self._last_reconcile_issue_sig: str = ""
        self._last_reconcile_issue_at: Optional[datetime.datetime] = None
        self._chejan_dedup_keys: set = set()
        self._chejan_dedup_order: deque = deque(maxlen=600)

        # MA 라운드로빈 상태
        self._ma_needed_codes = set(stock_codes)
        self._ma_refresh_idx = 0
        self._next_ma_refresh_at = datetime.datetime.min
        self._last_global_buy_sent_at: Optional[datetime.datetime] = None
        self._global_buy_timestamps: deque = deque(maxlen=4000)

    def set_scan_quality_context(
        self, quality_intent: bool, allowlist: Optional[Set[str]] = None
    ) -> None:
        """마지막 배경 스캔 결과: 우량 슬롯이면 시총 허용 집합으로 눌림 규칙을 바인딩.
        전체 유니버스 차례여도 기존 허용목록은 유지(적극 우량매수)."""
        self._scan_quality_intent_active = bool(quality_intent)
        if allowlist:
            self._quality_mcap_allowlist = set(allowlist)

    def _use_quality_dip_for_code(self, code: str) -> bool:
        if not QUALITY_DIP_ENABLED:
            return False
        c = str(code or "").strip()
        if not c:
            return False
        if c in getattr(self, "_quality_entry_codes", set()):
            return True
        if c not in self._quality_mcap_allowlist:
            return False
        if self._scan_quality_intent_active:
            return True
        return bool(QUALITY_DIP_ALWAYS_ON_ALLOWLIST)

    def _quality_dip_bear_exception(self, code: str) -> bool:
        """우량 눌림 종목은 bear 하드블록 예외(비중 축소)."""
        return bool(QUALITY_DIP_ALLOW_IN_BEAR) and self._use_quality_dip_for_code(code)

    def _market_regime_label(self) -> str:
        sm = self.strategy_manager
        regime = getattr(sm, "last_market_regime", None) if sm is not None else None
        return str(getattr(regime, "label", "") or "").lower()

    def _market_regime_source(self) -> str:
        sm = self.strategy_manager
        regime = getattr(sm, "last_market_regime", None) if sm is not None else None
        return str(getattr(regime, "source", "") or "")

    def _regime_source_is_index(self) -> bool:
        """지수/ETF 기반 국면이면 True. 관심종목 합성은 False."""
        src = self._market_regime_source().lower()
        if not src:
            return False
        if "composite" in src:
            return False
        return ("index:" in src) or ("kiwoom:index" in src) or src.startswith("index")

    def _effective_buy_strategy_id(self, code: str) -> str:
        if self._use_quality_dip_for_code(code):
            return STRATEGY_QUALITY_DIP
        if self._mock_bear_quality_dip_active():
            return STRATEGY_QUALITY_DIP
        if bool(REGIME_OVERRIDE_STRATEGY):
            label = self._market_regime_label()
            # 상승장: trend 전략(MA5>MA20 추세 추종, RSI 조건 완화)
            if label == "bull":
                return STRATEGY_TREND
            # 하락/횡보/미평가: volume 완화 규칙
            if label in ("bear", "range", ""):
                return STRATEGY_VOLUME
        sm = self.strategy_manager
        return sm.active_strategy_id if sm is not None else STRATEGY_AI

    def _is_weak_or_unknown_regime(self) -> bool:
        return self._market_regime_label() in ("bear", "range", "")

    def _allow_volume_weak_ma_entry(self, v: Dict[str, float], cur: float) -> bool:
        """MA5<=MA20 이지만 MA20 근처에서 RSI 반등 시 volume 진입 허용."""
        if not bool(VOLUME_WEAK_MA_ENTRY_ENABLED) or not self._is_weak_or_unknown_regime():
            return False
        ma20 = float(v.get("ma20", 0.0) or 0.0)
        if ma20 <= 0:
            return False
        rel = cur / ma20 - 1.0
        if rel < float(VOLUME_WEAK_MA20_ENTRY_GAP_PCT) or rel > float(QDIP_MAX_PRICE_ABOVE_MA20_PCT):
            return False
        rsi = float(v.get("rsi14", 0.0) or 0.0)
        rsi_prev = float(v.get("rsi14_prev", rsi) or rsi)
        return rsi > rsi_prev and rsi >= 28.0

    def _fetch_kiwoom_ohlcv_tuple(
        self, code: str
    ) -> Optional[Tuple[List[float], List[float], List[float], List[float]]]:
        """키움 일봉 → (closes, highs, lows, volumes). 메인 스레드에서만 호출."""
        code = str(code or "").strip()
        if not code:
            return None
        today = datetime.date.today().strftime("%Y%m%d")
        try:
            ohlcv = self.kiwoom.request_daily_ohlcv(
                code, end_date_yyyymmdd=today, count=80
            )
        except Exception:
            return None
        closes = [float(x) for x in (ohlcv.get("closes") or [])]
        highs = [float(x) for x in (ohlcv.get("highs") or [])]
        lows = [float(x) for x in (ohlcv.get("lows") or [])]
        vols = [float(x) for x in (ohlcv.get("volumes") or [])]
        n = min(len(closes), len(highs), len(lows), len(vols))
        if n < 25:
            return None
        return (closes[-n:], highs[-n:], lows[-n:], vols[-n:])

    def _get_cached_strat_ohlcv(
        self, code: str
    ) -> Optional[Tuple[List[float], List[float], List[float], List[float]]]:
        """StrategyManager 백그라운드 평가용 — 캐시만 읽고 TR은 하지 않음."""
        code = str(code or "").strip()
        with self._strat_ohlcv_cache_lock:
            row = self._strat_ohlcv_cache.get(code)
            ts = self._strat_ohlcv_cache_at.get(code)
        if row is None or ts is None:
            return None
        if (datetime.datetime.now() - ts).total_seconds() > 6 * 3600:
            return None
        return row

    def _kiwoom_tr_busy(self) -> bool:
        """키움 TR(일봉·잔고)이 메인스레드에서 도는 중이면 True (on_tick 하트비트만)."""
        return bool(
            getattr(self, "_ai_training_active", False)
            or getattr(self, "_regime_update_active", False)
            or getattr(self, "_strat_ohlcv_warm_active", False)
            or getattr(self, "_holdings_sync_active", False)
        )

    def _warm_strategy_ohlcv_cache(self, force: bool = False) -> None:
        """평가 전 일봉 캐시 — on_tick에서는 예약만, 실제 TR은 QTimer에서."""
        now = datetime.datetime.now()
        if (
            not force
            and self._last_strat_ohlcv_warm_at is not None
            and (now - self._last_strat_ohlcv_warm_at).total_seconds() < 1800.0
        ):
            return
        if getattr(self, "_strat_ohlcv_warm_scheduled", False) or getattr(
            self, "_strat_ohlcv_warm_active", False
        ):
            return
        if self._kiwoom_tr_busy():
            return
        self._strat_ohlcv_warm_scheduled = True
        self._strat_ohlcv_warm_force = bool(force)
        QTimer.singleShot(80, self._run_strat_ohlcv_warm_deferred)

    def _run_strat_ohlcv_warm_deferred(self) -> None:
        self._strat_ohlcv_warm_scheduled = False
        if getattr(self, "_strat_ohlcv_warm_active", False) or getattr(
            self, "_ai_training_active", False
        ):
            return
        force = bool(getattr(self, "_strat_ohlcv_warm_force", False))
        now = datetime.datetime.now()
        if (
            not force
            and self._last_strat_ohlcv_warm_at is not None
            and (now - self._last_strat_ohlcv_warm_at).total_seconds() < 1800.0
        ):
            return
        self._strat_ohlcv_warm_active = True
        try:
            codes: List[str] = []
            for c in list(KIWOOM_REGIME_INDEX_CODES) + list(self.stock_codes or [])[:8]:
                c = str(c).strip()
                if c and c not in codes:
                    codes.append(c)
            warmed = 0
            for code in codes:
                row = self._fetch_kiwoom_ohlcv_tuple(code)
                if row is None:
                    continue
                with self._strat_ohlcv_cache_lock:
                    self._strat_ohlcv_cache[code] = row
                    self._strat_ohlcv_cache_at[code] = now
                warmed += 1
            self._last_strat_ohlcv_warm_at = now
            if warmed:
                self._emit_log(f"[STRAT] 키움 OHLCV 캐시 {warmed}/{len(codes)}종목")
        finally:
            self._strat_ohlcv_warm_active = False

    def _maybe_update_market_regime_from_kiwoom(self) -> None:
        """키움 일봉 국면 갱신 — on_tick에서는 예약만 (TR로 매매루프 STALL 방지).

        우선순위: 지수 ETF(KODEX200 등) → 관심종목 합성.
        장중 REGIME_REFRESH_INTERVAL_SEC 마다 재평가. yfinance 성공 시 StrategyManager가 덮어쓸 수 있다.
        """
        sm = self.strategy_manager
        if sm is None:
            return
        now = datetime.datetime.now()
        interval = max(300.0, float(REGIME_REFRESH_INTERVAL_SEC))
        prev = getattr(sm, "last_market_regime", None)
        if (
            prev is not None
            and self._last_regime_fallback_at is not None
            and (now - self._last_regime_fallback_at).total_seconds() < interval
        ):
            return
        # 첫 산출은 짧게 재시도 가능하도록 최소 간격 완화
        if (
            prev is None
            and self._last_regime_fallback_at is not None
            and (now - self._last_regime_fallback_at).total_seconds() < 120.0
        ):
            return
        if getattr(self, "_regime_update_scheduled", False) or getattr(
            self, "_regime_update_active", False
        ):
            return
        if self._kiwoom_tr_busy():
            return
        self._last_regime_fallback_at = now
        self._regime_update_scheduled = True
        QTimer.singleShot(120, self._run_regime_update_deferred)

    def _run_regime_update_deferred(self) -> None:
        """QTimer: 캐시 우선·필요 시 키움 TR로 국면 산출 (on_tick 밖)."""
        self._regime_update_scheduled = False
        if getattr(self, "_regime_update_active", False) or getattr(
            self, "_ai_training_active", False
        ):
            return
        sm = self.strategy_manager
        if sm is None:
            return
        self._regime_update_active = True
        try:
            today = datetime.date.today().strftime("%Y%m%d")

            def _closes_for(code: str) -> List[float]:
                cached = self._get_cached_strat_ohlcv(code)
                if cached and len(cached[0]) >= 25:
                    return list(cached[0])
                try:
                    ohlcv = self.kiwoom.request_daily_ohlcv(
                        code, end_date_yyyymmdd=today, count=60
                    )
                    closes = [float(x) for x in (ohlcv.get("closes", []) or [])]
                    return closes if len(closes) >= 25 else []
                except Exception:
                    return []

            regime = None
            for idx_code in KIWOOM_REGIME_INDEX_CODES:
                closes = _closes_for(idx_code)
                if not closes:
                    continue
                regime = replace(
                    analyze_market_regime(closes),
                    source=f"kiwoom:index:{idx_code}",
                )
                break

            if regime is None:
                closes_list: List[List[float]] = []
                for code in list(self.stock_codes or [])[:8]:
                    closes = _closes_for(code)
                    if closes:
                        closes_list.append(closes)
                    if len(closes_list) >= 4:
                        break
                if len(closes_list) >= 2:
                    ln = min(len(c) for c in closes_list)
                    comp = [
                        sum(c[-ln:][i] for c in closes_list) / len(closes_list)
                        for i in range(ln)
                    ]
                    regime = replace(
                        analyze_market_regime(comp),
                        source=f"kiwoom:composite:{len(closes_list)}",
                    )

            if regime is None:
                return

            raw_label = str(regime.label)
            regime, prev_label, adopted = sm.set_market_regime(regime)
            mom = regime_momentum(regime)
            changed = prev_label != str(regime.label)
            held = raw_label != str(regime.label)
            self._emit_log(
                f"[STRAT] 시장 국면(키움) {regime.label_ko} mom={mom:+.2f} "
                f"R20={regime.ret_20d_pct:+.1f}% MA이격={regime.dist_ma20_pct:+.1f}% "
                f"src={regime.source}"
                + (
                    f" (변경:{prev_label or '-'}→{regime.label})"
                    if changed and prev_label
                    else ""
                )
                + (
                    f" (원판정 {raw_label}→히스테리시스 유지)"
                    if held and adopted
                    else ""
                )
            )
        finally:
            self._regime_update_active = False

    def scan_quality_ui_tag(self) -> str:
        if not QUALITY_DIP_ENABLED or not self._scan_quality_intent_active:
            return ""
        n = len(self._quality_mcap_allowlist)
        if n <= 0:
            return " | 스캔:우량슬롯(시총허용집합 없음→눌림미적용)"
        return f" | 스캔:우량·눌림({n}종 시총허용)"

    def _prune_global_buy_window(self, now: datetime.datetime) -> None:
        cutoff = now - datetime.timedelta(seconds=3600)
        dq = self._global_buy_timestamps
        while dq and dq[0] < cutoff:
            dq.popleft()

    def _effective_max_single_buy_krw(self, code: Optional[str]) -> float:
        base = max(0.0, float(MAX_SINGLE_BUY_ORDER_KRW))
        if base <= 0 or not bool(ATR_INVERSE_ORDER_CAP) or not code:
            return base
        v = self.ma.get(code) or {}
        atr_pct = float(v.get("atr_pct", 0.0) or 0.0)
        if atr_pct <= 0:
            return base
        ref = max(1e-9, float(ENTRY_ATR_REF_PCT))
        factor = ref / max(ref, atr_pct)
        lo = max(0.0, min(1.0, float(ATR_ORDER_CAP_MIN_FACTOR)))
        factor = max(lo, min(1.0, factor))
        return base * factor

    def _on_real_price(self, code: str, price: int):
        self.current_price[code] = abs(int(price))
        self.current_price_source[code] = "REAL"
        self._real_price_tick_total += 1
        self._real_price_tick_window += 1
        mono = time.monotonic()
        self._code_last_tick[code] = mono
        self._last_any_tick = mono

    def _on_vi_event(self, code: str, released: bool, detail: str) -> None:
        mono = time.monotonic()
        try:
            name = self.kiwoom.get_master_code_name(code)
        except Exception:
            name = code
        if released:
            if self._vi_active.pop(code, None) is not None:
                self._vi_released_at[code] = mono
                self._emit_log(
                    f"[VI] {name}({code}) 해제 ({detail}) — {VI_BUY_COOLDOWN_SEC // 60}분간 신규매수 금지"
                )
            return
        if code not in self._vi_active:
            self._emit_log(f"[VI] {name}({code}) 발동 ({detail}) — 해제까지 주문 보류")
        self._vi_active[code] = mono

    def _vi_is_active(self, code: str) -> bool:
        started = self._vi_active.get(code)
        if started is None:
            return False
        if time.monotonic() - started >= float(VI_HOLD_MAX_SEC):
            self._vi_active.pop(code, None)
            self._vi_released_at[code] = time.monotonic()
            return False
        return True

    def _vi_buy_block_reason(self, code: str) -> str:
        if not VI_GUARD_ENABLED:
            return ""
        if self._vi_is_active(code):
            return "VI 발동 중(단일가) — 신규매수 금지"
        rel = self._vi_released_at.get(code)
        if rel is not None:
            left = float(VI_BUY_COOLDOWN_SEC) - (time.monotonic() - rel)
            if left > 0:
                return f"VI 해제 직후 변동성 — 신규매수 금지(잔여 {int((left + 59) // 60)}분)"
        return ""

    def _vi_order_guard(self, code: str, side: str) -> bool:
        """종목 VI 발동 중이면 주문 전송을 막고 True 반환(단일가 체결로 불리한 가격 방지)."""
        if not VI_GUARD_ENABLED or not self._vi_is_active(code):
            return False
        now = time.monotonic()
        key = (code, side)
        if now - self._vi_order_logged.get(key, -1e9) >= 60.0:
            self._vi_order_logged[key] = now
            self._emit_log(f"[VI] {code} {side} 주문 보류 — VI 발동 중 (해제 후 재평가)")
        return True

    def _stock_state_flags(self, code: str) -> str:
        today = datetime.date.today()
        if self._stock_state_day != today:
            self._stock_state_day = today
            self._stock_state_cache = {}
        if code not in self._stock_state_cache:
            getter = getattr(self.kiwoom, "get_master_stock_flags", None)
            self._stock_state_cache[code] = getter(code) if callable(getter) else ""
        return self._stock_state_cache[code]

    def _stock_state_block_reason(self, code: str) -> str:
        if not STOCK_STATE_GUARD_ENABLED:
            return ""
        flags = self._stock_state_flags(code)
        hits = [k for k in STOCK_STATE_BLOCK_KEYWORDS if k in flags]
        return f"위험 종목상태({','.join(hits)}) — 신규매수 금지" if hits else ""

    def _log_stock_state_summary(self) -> None:
        if not STOCK_STATE_GUARD_ENABLED:
            return
        flagged = []
        for c in self.stock_codes or []:
            r = self._stock_state_block_reason(c)
            if r:
                flagged.append(f"{c}:{r.split('(')[1].split(')')[0]}")
        if flagged:
            self._emit_log(f"[STOCK-STATE] 매수 제외 {len(flagged)}종목: {', '.join(flagged[:20])}")
        else:
            self._emit_log(f"[STOCK-STATE] 대상 {len(self.stock_codes or [])}종목 중 위험 상태 종목 없음")

    def _log_market_calendar_today(self) -> None:
        today = datetime.date.today()
        if not _is_krx_trading_day(today):
            self._emit_log(f"[CALENDAR] {today} 휴장일 — 매매 없음")
            return
        b = _krx_session_bounds(today)
        if b["open_shift_min"] or b["close_shift_min"]:
            def _hm(sec: int) -> str:
                return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}"

            self._emit_log(
                f"[CALENDAR] {today} 특수 개장일 — 정규장 {_hm(b['open'])}~{_hm(b['close'])} "
                f"(개장 +{b['open_shift_min']}분, 마감 +{b['close_shift_min']}분)"
            )

    def _maybe_recover_realtime_feed(self) -> None:
        """정규장 중 실시간 체결이 전 종목 FEED_RECOVER_SILENT_SEC 이상 끊기면 SetRealReg 재등록(일 최대 N회)."""
        if not FEED_RECOVER_ENABLED or self._halt_active:
            return
        if self.market_session_name() != "정규장" or not self.stock_codes:
            return
        mono = time.monotonic()
        today = datetime.date.today()
        if self._feed_day != today:
            self._feed_day = today
            self._feed_watch_since = mono
            self._feed_retries = 0
            self._feed_last_retry = -1e9
            self._feed_gave_up = False
        silent = mono - max(self._last_any_tick, self._feed_watch_since, self._feed_last_retry)
        if silent < float(FEED_RECOVER_SILENT_SEC):
            return
        if self._feed_retries >= int(FEED_RECOVER_MAX_PER_DAY):
            if not self._feed_gave_up:
                self._feed_gave_up = True
                self._emit_log(
                    f"[FEED] 실시간 재등록 {self._feed_retries}회 후에도 수신 0/s — "
                    f"API 연결 이상 가능, 앱 재시작 권장"
                )
            return
        if mono - self._feed_last_retry < float(FEED_RECOVER_RETRY_GAP_SEC):
            return
        self._feed_retries += 1
        self._feed_last_retry = mono
        try:
            n = self.kiwoom.reregister_real(fid_list="10")
            self._emit_log(
                f"[FEED] 정규장 실시간 수신 {int(silent)}초 0건 — 실시간 재등록 {n}종목 "
                f"({self._feed_retries}/{int(FEED_RECOVER_MAX_PER_DAY)})"
            )
        except Exception as e:
            self._emit_log(f"[FEED] 실시간 재등록 실패: {e}")

    def _update_market_halt_state(self) -> None:
        """정규장 중 대상 종목 체결이 일제히 끊기면 시장 거래정지(서킷브레이커 등) 의심 상태로 전환."""
        if not bool(HALT_DETECT_ENABLED):
            return
        now_dt = datetime.datetime.now()
        today = now_dt.date()
        if self._halt_day != today:
            self._halt_day = today
            self._halt_active = False
            self._halt_since = None
            self._halt_low_since = None
            self._halt_resumed_at = None
            self._halt_healthy_seen = False
            self._halt_long_warned = False
        if self.market_session_name() != "정규장":
            if self._halt_active:
                self._halt_active = False
                self._emit_log("[HALT] 정규장 종료 — 거래정지 의심 상태 해제")
            self._halt_low_since = None
            return
        if _qtime_sec(QTime.currentTime()) < _krx_session_bounds(today)["open"] + 12 * 60:
            return
        codes = [c for c in (self.stock_codes or []) if c]
        if not codes:
            return
        mono = time.monotonic()
        win = float(HALT_TICK_WINDOW_SEC)
        active = sum(1 for c in codes if mono - self._code_last_tick.get(c, -1e9) <= win)
        ratio = active / len(codes)
        samples = self._halt_rate_samples
        samples.append((mono, int(self._real_price_tick_total)))
        while len(samples) > 2 and mono - samples[1][0] >= win:
            samples.popleft()
        span = mono - samples[0][0]
        rate = (samples[-1][1] - samples[0][1]) / span if span >= win * 0.8 else None
        if ratio >= 0.5 and not self._halt_active:
            self._halt_healthy_seen = True
            if rate is not None:
                self._halt_rate_baseline = (
                    rate if self._halt_rate_baseline <= 0 else 0.98 * self._halt_rate_baseline + 0.02 * rate
                )
        base = self._halt_rate_baseline
        rate_collapsed = (
            rate is not None and base >= 20.0 and rate < float(HALT_RATE_COLLAPSE) * base
        )
        if not self._halt_active:
            if not self._halt_healthy_seen or (ratio >= float(HALT_ACTIVE_RATIO) and not rate_collapsed):
                self._halt_low_since = None
                return
            if self._halt_low_since is None:
                self._halt_low_since = now_dt
                return
            if (now_dt - self._halt_low_since).total_seconds() < float(HALT_CONFIRM_SEC):
                return
            self._halt_active = True
            self._halt_since = self._halt_low_since
            self._halt_long_warned = False
            self._emit_log(
                f"[HALT] 시장 거래정지 의심(서킷브레이커 등) {self._halt_since:%H:%M:%S}부터 — "
                f"최근{int(win)}초 체결 종목 {active}/{len(codes)} · 수신 {rate or 0:.1f}/s (평소 {base:.0f}/s) "
                f"— 신규매수·주문 중지"
            )
            return
        if ratio >= float(HALT_RESUME_RATIO) and (base < 20.0 or (rate or 0) >= 0.3 * base):
            dur = (now_dt - (self._halt_since or now_dt)).total_seconds() / 60.0
            self._halt_active = False
            self._halt_resumed_at = now_dt
            self._halt_low_since = None
            self._emit_log(
                f"[HALT] 거래 재개 감지 {now_dt:%H:%M:%S} (정지 {dur:.0f}분, 체결 종목 {active}/{len(codes)}) — "
                f"{int(HALT_RESUME_COOLDOWN_MIN)}분간 신규매수 금지"
            )
            return
        if (
            not self._halt_long_warned
            and self._halt_since is not None
            and (now_dt - self._halt_since).total_seconds() >= float(HALT_MAX_MIN) * 60.0
        ):
            self._halt_long_warned = True
            self._emit_log(
                f"[HALT] {int(HALT_MAX_MIN)}분 넘게 체결 없음 — 당일 거래종료(서킷 3단계) 또는 시세 장애 가능. "
                f"장 재개가 확인되지 않으면 앱 재시작 권장"
            )

    def _market_halt_buy_block_reason(self) -> str:
        if self._halt_active:
            return "시장 거래정지 의심(서킷브레이커) — 신규매수 금지"
        if self._halt_resumed_at is not None:
            left = float(HALT_RESUME_COOLDOWN_MIN) * 60.0 - (
                datetime.datetime.now() - self._halt_resumed_at
            ).total_seconds()
            if left > 0:
                return f"거래 재개 후 {int(HALT_RESUME_COOLDOWN_MIN)}분 신규매수 금지(잔여 {int(left // 60) + 1}분)"
        return ""

    def _halt_order_guard(self, code: str, side: str) -> bool:
        """거래정지 의심 중이면 주문 전송을 막고 True 반환."""
        if not self._halt_active:
            return False
        now = time.monotonic()
        key = (code, side)
        if now - self._halt_order_logged.get(key, -1e9) >= 300.0:
            self._halt_order_logged[key] = now
            self._emit_log(f"[HALT] {code} {side} 주문 보류 — 시장 거래정지 의심 중 (재개 후 재평가)")
        return True

    def consume_real_price_stats(self) -> Dict[str, float]:
        """
        최근 구간 실시간 현재가 수신 통계.
        호출 시 window 카운터를 리셋한다.
        """
        now = datetime.datetime.now()
        elapsed = (now - self._real_price_window_started_at).total_seconds()
        elapsed = max(0.001, float(elapsed))
        cnt = int(self._real_price_tick_window)
        per_sec = float(cnt) / elapsed
        self._real_price_tick_window = 0
        self._real_price_window_started_at = now
        return {
            "count": float(cnt),
            "elapsed_sec": float(elapsed),
            "per_sec": float(per_sec),
            "total": float(self._real_price_tick_total),
        }

    def _emit_log(self, msg: str) -> None:
        # GUI on_log가 연결되면 파일 기록도 GUI 경로에 위임(REAL/SCAN 누락 방지).
        # 미연결(부트 초반)일 때만 엔진이 직접 파일에 쓴다.
        if self.on_log is not None:
            try:
                self.on_log(msg)
                return
            except Exception:
                pass
        self._write_log_file(msg)

    def _emit_feature_verify_log(self) -> None:
        """신규 기능(뉴스추론·우량눌림·물타기) 반영 여부를 부팅 로그+전용 파일에 남긴다."""
        now = datetime.datetime.now()
        today = now.strftime("%Y-%m-%d")
        lines = [
            "=" * 64,
            f"  [FEATURE-VERIFY] {today} {now.strftime('%H:%M:%S')} 코드 반영 확인",
            "=" * 64,
            f"  NEWS_SENTIMENT_AVAILABLE={NEWS_SENTIMENT_AVAILABLE}",
            f"  NEWS={_news_gemini_status_line() if NEWS_SENTIMENT_AVAILABLE else 'off'}",
            f"  STRATEGY_REASONING_FIRST={bool(STRATEGY_REASONING_FIRST)}",
            f"  QUALITY_DIP_ENABLED={bool(QUALITY_DIP_ENABLED)}",
            f"  QUALITY_DIP_ALWAYS_ON_ALLOWLIST={bool(QUALITY_DIP_ALWAYS_ON_ALLOWLIST)}",
            f"  QUALITY_DIP_ALLOW_IN_BEAR={bool(QUALITY_DIP_ALLOW_IN_BEAR)} "
            f"(size×{float(QUALITY_BEAR_SIZE_MULT):.2f})",
            f"  QDIP RSI={float(QDIP_RSI_MIN_ENTRY):.0f}~{float(QDIP_RSI_MAX_ENTRY):.0f} "
            f"VOL≥{float(QDIP_VOL_RATIO_MIN):.2f} "
            f"MA20={float(QDIP_MIN_PRICE_VS_MA20_PCT):+.0%}~{float(QDIP_MAX_PRICE_ABOVE_MA20_PCT):+.1%}",
            f"  QUALITY_AI_THR_RELAX={float(QUALITY_AI_THR_RELAX):.2f}",
            f"  TREND_AI_SOFT_THR_CAP={float(TREND_AI_SOFT_THR_CAP):.2f} (하한 0.50)",
            f"  SHADOW_LEDGER={'ON' if _shadow is not None else 'OFF'} (오답노트 기록·20:15 채점)",
            f"  DAILY_STRATEGY_LEARNING=ON (국면·오답노트 n≥{SHADOW_EVIDENCE_MIN_N}·"
            f"연속되돌림≥{STRATEGY_FATIGUE_REVERTS}/{STRATEGY_FATIGUE_WINDOW_DAYS}일 반영)",
            f"  REASONING_REPORT={'ON' if _reasoning is not None else 'OFF'} (일일전략 후 log/*_reasoning.txt)",
            f"  TICK_STALL_WATCHDOG=ON (on_tick {TICK_STALL_SEC}s 정지 시 log/*_stall.txt 스택 기록)",
            f"  HALT_DETECT={'ON' if HALT_DETECT_ENABLED else 'OFF'} (체결종목<{HALT_ACTIVE_RATIO:.0%} "
            f"{HALT_CONFIRM_SEC}s→주문중지, 재개 후 {HALT_RESUME_COOLDOWN_MIN}분 신규매수 금지)",
            f"  KRX_CALENDAR=ON (휴장 {sum(len(v) for v in _KRX_STATIC_HOLIDAYS.values())}일 정적목록 "
            f"{min(_KRX_STATIC_HOLIDAYS)}~{max(_KRX_STATIC_HOLIDAYS)}+pykrx, 수능·연초 개장지연)",
            f"  STOCK_STATE_GUARD={'ON' if STOCK_STATE_GUARD_ENABLED else 'OFF'} "
            f"({'/'.join(STOCK_STATE_BLOCK_KEYWORDS)} 매수 제외)",
            f"  VI_GUARD={'ON' if VI_GUARD_ENABLED else 'OFF'} (발동 중 주문보류 최대 {VI_HOLD_MAX_SEC}s, "
            f"해제 후 {VI_BUY_COOLDOWN_SEC // 60}분 신규매수 금지)",
            f"  FEED_RECOVER={'ON' if FEED_RECOVER_ENABLED else 'OFF'} (정규장 {FEED_RECOVER_SILENT_SEC}s 무수신→"
            f"재등록, 일 {FEED_RECOVER_MAX_PER_DAY}회)",
            "  NEWS_NONBLOCK=ON (매수판정은 캐시만·miss는 백그라운드 조회, on_tick HTTP 차단 방지)",
            f"  UI_TABLE_LIGHT=ON (진입대기 등 테이블 {UI_TABLE_REFRESH_INTERVAL_SEC:.0f}s·"
            f"변경셀만 갱신·표시상한 {UI_WAITING_DISPLAY_LIMIT})",
            "  AI_TRAIN_DEFERRED=ON (재학습은 on_tick 밖 QTimer·정규장 제외, 학습 중 하트비트만)",
            "  REGIME_UPDATE_DEFERRED=ON (국면·전략OHLCV 캐시도 on_tick 밖 QTimer·TR중 하트비트)",
            "  HOLDINGS_SYNC_DEFERRED=ON (잔고/reconcile TR도 on_tick 밖 QTimer·TR중 하트비트)",
            f"  QUALITY_AVG_DOWN_ENABLED={bool(QUALITY_AVG_DOWN_ENABLED)} "
            f"max={int(QUALITY_AVG_DOWN_MAX)} "
            f"step={float(QUALITY_AVG_DOWN_STEP_PCT):.0%} "
            f"cooldown={float(QUALITY_AVG_DOWN_COOLDOWN_SEC):.0f}s "
            f"size×{float(QUALITY_AVG_DOWN_SIZE_FACTOR):.2f}",
            f"  MOCK_LIVE_PARITY={bool(MOCK_LIVE_PARITY)}",
            f"  THEME_STRENGTH="
            f"{'ON' if (NEWS_SENTIMENT_AVAILABLE and bool(_THEME_STRENGTH_ENABLED)) else 'off'} "
            f"size×{float(THEME_STRONG_SIZE_MULT):.2f}",
            "  확인태그: [FEATURE](5분) [THEME] [NEWS] [AVG-DOWN] [SCAN]우량 "
            "[DAILY-STRATEGY]Gemini [FEATURE-VERIFY]부팅 "
            "[GEMINI] log/*_gemini_summary.txt",
            "=" * 64,
        ]
        for ln in lines:
            self._emit_log(ln)
        # 전용 파일 (재시작마다 덮어씀 — 오늘 반영 여부 확인용)
        try:
            root = os.path.dirname(os.path.abspath(__file__))
            log_dir = os.path.join(root, "log")
            os.makedirs(log_dir, exist_ok=True)
            path = os.path.join(log_dir, f"{today}_feature_verify.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
            self._emit_log(f"[FEATURE-VERIFY] 기록 완료 → log/{today}_feature_verify.txt")
        except Exception as e:
            self._emit_log(f"[FEATURE-VERIFY] 파일 기록 실패: {e}")

    def _market_phase_tag(self) -> str:
        """현재 시각에 해당하는 장 구간 태그를 반환한다."""
        try:
            b = _krx_session_bounds(datetime.date.today())
            s = _qtime_sec(QTime.currentTime())
            if b["pre_start"] <= s < b["open"]:
                return "PRE"   # 프리마켓 08:00~08:59 (특수일 지연)
            if b["open"] <= s < b["close"]:
                return "MKT"   # 정규장 09:00~15:30 (특수일 지연)
            if b["close"] <= s < b["after_end"]:
                return "AFT"   # 애프터마켓 15:30~20:00
            return "OUT"       # 장외 20:00 이후 / 08:00 이전
        except Exception:
            return "OUT"

    def _write_log_file(self, msg: str) -> None:
        """로그를 log/YYYY-MM-DD.txt 파일에 장 구간 태그와 함께 저장한다."""
        try:
            today = datetime.date.today()
            if self._log_file_date != today:
                # 날짜가 바뀌면 이전 파일 닫고 새 파일 열기
                if self._log_file_handle is not None:
                    try:
                        self._log_file_handle.close()  # type: ignore[union-attr]
                    except Exception:
                        pass
                log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "log")
                os.makedirs(log_dir, exist_ok=True)
                log_path = os.path.join(log_dir, today.strftime("%Y-%m-%d") + ".txt")
                self._log_file_handle = open(log_path, "a", encoding="utf-8", buffering=1)
                self._log_file_date = today
            if self._log_file_handle is not None:
                ts = datetime.datetime.now().strftime("%H:%M:%S")
                phase = self._market_phase_tag()
                self._log_file_handle.write(f"[{ts}][{phase}] {msg}\n")  # type: ignore[union-attr]
        except Exception:
            pass

    def _log_buy_block(self, code: str, reason: str, interval_sec: float = 120.0) -> None:
        """매수 차단 사유 로그(종목+사유 단위로 주기 제한)."""
        now = datetime.datetime.now()
        key = f"{code}|{reason}"
        last = self._last_buy_block_reason_at.get(key)
        if last is not None and (now - last).total_seconds() < float(interval_sec):
            return
        today = datetime.date.today()
        if self._buy_block_stats_date != today:
            self._buy_block_stats.clear()
            self._buy_block_stats_date = today
        cat = str(reason).split("(")[0].strip()[:48] or "기타"
        self._buy_block_stats[cat] = int(self._buy_block_stats.get(cat, 0)) + 1
        self._last_buy_block_reason_at[key] = now
        self._emit_log(f"[BUY-BLOCK] {code} {reason}")
        if cat == "전략조건 미충족":
            self._shadow_record(code, str(reason))

    def _shadow_record(self, code: str, reason: str) -> None:
        if _shadow is None or self.market_session_name() != "정규장":
            return
        try:
            cur = float(self.current_price.get(code, 0) or 0)
            if cur <= 0:
                return
            v = self.ma.get(code) or {}
            ma20 = float(v.get("ma20", 0.0) or 0.0)
            proba: Optional[float] = None
            if "AI" in reason and self.ai_model is not None:
                x = self._ai_features_from_cached(code, cur)
                if x is not None:
                    proba = float(self.ai_model.predict_proba([x])[0][1])
            _shadow.record(
                code,
                cur,
                reason,
                ai_proba=proba,
                rsi14=float(v["rsi14"]) if v.get("rsi14") is not None else None,
                ma20_rel=(cur / ma20 - 1.0) if ma20 > 0 else None,
                vol_ratio=float(v["vol_ratio"]) if v.get("vol_ratio") is not None else None,
                regime=self._market_regime_label(),
            )
        except Exception:
            pass

    def _maybe_run_shadow_scoring(self) -> None:
        """장후 20:15 이후 하루 1회 놓친 신호 채점(백그라운드)."""
        if _shadow is None or QTime.currentTime() < QTime(20, 15, 0):
            return
        today = datetime.date.today()
        if getattr(self, "_shadow_scored_date", None) == today:
            return
        self._shadow_scored_date = today
        stop = float(STOP_LOSS_PCT)
        take = float(TAKE_PROFIT_PCT)
        cost = float(AI_ROUND_TRIP_COST_PCT) + float(AI_SLIPPAGE_PCT)

        def _run() -> None:
            try:
                _shadow.score_and_report(
                    stop_pct=stop, take_pct=take, cost_pct=cost, log_fn=self._emit_log
                )
            except Exception as e:
                self._emit_log(f"[SHADOW] 채점 오류: {e}")

        threading.Thread(target=_run, daemon=True, name="shadow-score").start()

    def _maybe_write_reasoning_report(self) -> None:
        """일일 전략 적용 후 하루 1회 추론 요약 리포트(백그라운드)."""
        if _reasoning is None:
            return
        today = datetime.date.today()
        if getattr(self, "_daily_strategy_done_date", None) != today:
            return
        if getattr(self, "_reasoning_written_date", None) == today:
            return
        self._reasoning_written_date = today

        def _run() -> None:
            try:
                path = _reasoning.build(today.isoformat())
                head = ""
                if path:
                    with open(path, "r", encoding="utf-8") as f:
                        head = next((l for l in f if l.startswith("한줄:")), "").strip()
                self._emit_log(f"[REASONING] {head or '작성 실패'} → log/{today}_reasoning.txt")
            except Exception as e:
                self._emit_log(f"[REASONING] 작성 오류: {e}")

        threading.Thread(target=_run, daemon=True, name="reasoning-report").start()

    def _invested_budget_krw(self) -> float:
        return sum(
            float((p or {}).get("avg_price", 0) or 0) * float((p or {}).get("qty", 0) or 0)
            for p in (self.positions or {}).values()
            if int((p or {}).get("qty", 0) or 0) > 0
        )

    def _unrealized_pnl_krw(self) -> float:
        total = 0.0
        for code, p in (self.positions or {}).items():
            qty = int((p or {}).get("qty", 0) or 0)
            avg = float((p or {}).get("avg_price", 0) or 0)
            cur = float(self.current_price.get(code, 0) or 0)
            if qty > 0 and avg > 0 and cur > 0:
                total += (cur - avg) * float(qty)
        return total

    def _dynamic_total_budget_krw(self) -> float:
        # 키움 추정예탁자산이 있으면 매수 예산도 HTS와 동일 기준 사용
        kw_asset = getattr(self, "opw_estimated_deposit_krw", None)
        if kw_asset is not None and int(kw_asset) > 0:
            return float(kw_asset)
        base = max(0.0, float(TOTAL_LIMIT_KRW))
        estimated_asset = base + float(self.realized_pnl_krw or 0.0) + self._unrealized_pnl_krw()
        # 수익/손실 모두 반영한 추정예탁자산을 매수 예산 기준으로 사용.
        return max(0.0, estimated_asset)

    def _pending_buy_budget_krw(self) -> float:
        total = 0.0
        for code, meta in (self.pending_orders or {}).items():
            if str((meta or {}).get("side", "")).lower() != "buy":
                continue
            qty = int((meta or {}).get("qty", 0) or 0)
            px = float(self.current_price.get(code, 0) or 0)
            total += max(0, qty) * max(0.0, px)
        return total

    def _budget_based_order_qty(self, price: float, code: Optional[str] = None) -> int:
        if price <= 0:
            return 0
        if not bool(BUDGET_BASED_ORDER_QTY):
            return max(1, int(ORDER_QTY_PER_STOCK))
        total_limit = max(0.0, self._dynamic_total_budget_krw())
        if total_limit <= 0:
            return max(1, int(ORDER_QTY_PER_STOCK))
        used = self._invested_budget_krw() + self._pending_buy_budget_krw()
        remaining_budget = max(0.0, total_limit - used)
        if remaining_budget < price:
            return 0
        active_positions = sum(
            1 for _c, p in (self.positions or {}).items() if int((p or {}).get("qty", 0) or 0) > 0
        )
        pending_buys = sum(
            1
            for _c, m in (self.pending_orders or {}).items()
            if str((m or {}).get("side", "")).lower() == "buy"
        )
        remaining_slots = max(1, self._effective_max_positions() - active_positions - pending_buys)
        per_slot_budget = remaining_budget / float(remaining_slots)
        max_position_budget = total_limit * max(0.01, float(MAX_POSITION_BUDGET_PCT))
        target_budget = max(price, min(per_slot_budget, max_position_budget))
        max_single_order = max(0.0, self._effective_max_single_buy_krw(code))
        if max_single_order > 0:
            target_budget = min(target_budget, max_single_order)
        qty = int(target_budget // price)
        # 재진입 포지션은 리스크 절반 (손절가 미회복 상태에서 재진입이므로 신중하게)
        if code is not None and code in getattr(self, "_reentry_codes", set()):
            qty = max(1, int(qty * float(REENTRY_POSITION_SIZE_FACTOR)))
        # 우량 눌림 + bear 예외 시 비중 축소
        if (
            code is not None
            and self._quality_dip_bear_exception(code)
            and self._is_market_weak()
        ):
            qty = max(1, int(qty * max(0.10, min(1.0, float(QUALITY_BEAR_SIZE_MULT)))))
        # 강세 테마 종목 수량 소폭 가산
        if (
            code is not None
            and NEWS_SENTIMENT_AVAILABLE
            and bool(_THEME_STRENGTH_ENABLED)
            and _is_theme_strong_for_code(code)
        ):
            qty = max(1, int(qty * max(1.0, min(1.25, float(THEME_STRONG_SIZE_MULT)))))
        # 3·4단계: 학습품질·당일편향으로 수량 축소
        size_mult = float(self._ai_quality_size_factor())
        try:
            size_mult *= float(self._day_bias_adjustments().get("size_mult", 1.0) or 1.0)
        except Exception:
            pass
        if self._mock_bear_quality_dip_active():
            size_mult *= max(0.10, min(1.0, float(MOCK_BEAR_SIZE_MULT)))
        size_mult = max(0.10, min(1.0, size_mult))
        if size_mult < 0.999:
            qty = max(1, int(qty * size_mult)) if qty > 0 else 0
        return max(1, qty) if qty > 0 else 0

    def _append_order_event(
        self,
        event: str,
        code: str,
        side: str = "",
        qty: int = 0,
        price: float = 0.0,
        note: str = "",
    ) -> None:
        """주문 전송/미체결/체결 상태를 CSV로 저장."""
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        row = {
            "timestamp": ts,
            "event": str(event),
            "side": str(side).upper(),
            "code": str(code),
            "qty": int(qty or 0),
            "price": float(price or 0.0),
            "note": str(note or ""),
        }
        path = self.order_event_csv_path
        fieldnames = ["timestamp", "event", "side", "code", "qty", "price", "note"]
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            need_header = not os.path.exists(path)
            with open(path, "a", encoding="utf-8-sig", newline="") as f:
                w = csv.DictWriter(f, fieldnames=fieldnames)
                if need_header:
                    w.writeheader()
                w.writerow(row)
        except Exception:
            pass

    def _notify_trade_fill(self, payload: Dict[str, object]) -> None:
        """매수/매도 체결 확정 시 performance 모듈 등으로 전달."""
        fn = self.on_trade_fill
        if not callable(fn):
            return
        try:
            fn(payload)
        except Exception:
            pass

    def _is_market_open(self) -> bool:
        return self.market_session_name() in ("프리마켓", "정규장", "애프터마켓")

    def market_session_name(self) -> str:
        today = datetime.date.today()
        if not _is_krx_trading_day(today):
            return "휴장"
        b = _krx_session_bounds(today)
        s = _qtime_sec(QTime.currentTime())
        if b["pre_start"] <= s <= b["pre_end"]:
            return "프리마켓"
        if b["open"] <= s < b["regular_end"]:
            return "정규장"
        if b["close"] <= s <= b["after_end"]:
            return "애프터마켓"
        return "장외"

    def refresh_ma_for_code(self, code: str) -> bool:
        today = datetime.date.today()
        end_date = today.strftime("%Y%m%d")
        ohlcv = self.kiwoom.request_daily_ohlcv(
            code,
            end_date_yyyymmdd=end_date,
            count=MA_TR_COUNT,
        )
        closes = list(ohlcv.get("closes", []))
        volumes = list(ohlcv.get("volumes", []))
        highs = list(ohlcv.get("highs", []))
        lows = list(ohlcv.get("lows", []))
        # golden cross(전일 대비) 계산을 위해 최소 21개 필요
        if len(closes) < 21 or len(volumes) < 21:
            return False
        # 최신 5/20
        ma5 = sum(closes[-5:]) / 5.0
        ma20 = sum(closes[-20:]) / 20.0
        # 전일 기준(최신 1일을 제외하고 계산)
        ma5_prev = sum(closes[-6:-1]) / 5.0
        ma20_prev = sum(closes[-21:-1]) / 20.0

        # RSI(최신/전일)
        rsi14 = _compute_rsi_sma(closes, RSI_PERIOD)
        rsi14_prev = _compute_rsi_sma(closes[:-1], RSI_PERIOD)
        if rsi14 is None or rsi14_prev is None:
            return False

        # 거래량(최신/평균)
        vol_today = volumes[-1]
        vol_prev = volumes[-2]
        vol_avg = sum(volumes[-VOL_AVG_PERIOD:]) / float(VOL_AVG_PERIOD)
        vol_ratio = (vol_today / vol_avg) if vol_avg > 0 else 0.0
        vol_chg = (vol_today / vol_prev - 1.0) if vol_prev > 0 else 0.0

        # 1일 수익률(최신 기준, 최근 2개 종가로 계산)
        close_today = closes[-1]
        close_prev = closes[-2]
        ret_1d = (close_today / close_prev - 1.0) if close_prev > 0 else 0.0

        # 변동성(최근 20일 일수익률 표준편차)
        rets: List[float] = []
        for i in range(max(1, len(closes) - 20), len(closes)):
            p0 = float(closes[i - 1])
            p1 = float(closes[i])
            if p0 > 0:
                rets.append((p1 / p0) - 1.0)
        if len(rets) >= 2:
            mu = sum(rets) / float(len(rets))
            var = sum((r - mu) * (r - mu) for r in rets) / float(len(rets) - 1)
            volatility = var ** 0.5
        else:
            volatility = 0.0

        # MACD(12,26,9)
        closes_f = [float(x) for x in closes]
        ema12 = _ema_series(closes_f, 12)
        ema26 = _ema_series(closes_f, 26)
        macd_line_series = [a - b for a, b in zip(ema12, ema26)]
        macd_signal_series = _ema_series(macd_line_series, 9)
        macd_line = float(macd_line_series[-1]) if macd_line_series else 0.0
        macd_signal = float(macd_signal_series[-1]) if macd_signal_series else 0.0
        atr_pct = _compute_atr_pct(
            [float(x) for x in highs],
            [float(x) for x in lows],
            [float(x) for x in closes],
            ATR_PERIOD,
        )

        self.ma[code] = {
            "ma5": ma5,
            "ma20": ma20,
            "ma5_prev": ma5_prev,
            "ma20_prev": ma20_prev,
            "rsi14": rsi14,
            "rsi14_prev": rsi14_prev,
            "vol_today": vol_today,
            "vol_prev": vol_prev,
            "vol_avg": vol_avg,
            "vol_ratio": vol_ratio,
            "vol_chg": vol_chg,
            "close_today": close_today,
            "close_prev": close_prev,
            "ret_1d": ret_1d,
            "volatility": volatility,
            "macd_line": macd_line,
            "macd_signal": macd_signal,
            "atr_pct": atr_pct,
        }
        return True

    def _trade_features_for_code(self, code: str) -> List[float]:
        cached = self._trade_feature_cache.get(code)
        today = datetime.date.today()
        if cached and cached[0] == today:
            return list(cached[1])
        records = []
        tracker = getattr(self, "trade_tracker", None)
        if tracker is not None:
            try:
                records = [r for r in list(getattr(tracker, "records", []) or []) if str(r.code) == str(code)]
            except Exception:
                records = []
        sells = [r for r in records if str(getattr(r, "side", "")).upper() == "SELL"]
        if not sells:
            feats = [0.5, 0.0, 0.0, 0.0, 0.0]
        else:
            pnl_pcts = [float(getattr(r, "pnl_pct", 0.0) or 0.0) for r in sells]
            wins = [p for p in pnl_pcts if p > 0]
            win_rate = len(wins) / float(max(1, len(pnl_pcts)))
            avg_pnl = sum(pnl_pcts) / float(max(1, len(pnl_pcts)))
            count_norm = min(1.0, len(pnl_pcts) / 20.0)
            reasons = [str(getattr(r, "sell_reason", "") or "").strip().lower() for r in sells]
            stop_n = sum(1 for r in reasons if r == "stop_loss")
            weak_n = sum(1 for r in reasons if r == "market_weak_exit")
            stop_rate = stop_n / float(max(1, len(reasons)))
            weak_rate = weak_n / float(max(1, len(reasons)))
            feats = [win_rate, avg_pnl / 100.0, count_norm, stop_rate, weak_rate]
        self._trade_feature_cache[code] = (today, feats)
        return list(feats)

    def _live_round_trips_from_tracker(self) -> Dict[str, List[Dict[str, object]]]:
        """
        trade_history CSV를 FIFO로 묶어 완료된 라운드트립(매수→매도)을 종목별로 반환.
        라벨은 실제 실현 손익 기준(pnl_pct>0 → 1).
        """
        out: Dict[str, List[Dict[str, object]]] = {}
        tracker = getattr(self, "trade_tracker", None)
        if tracker is None:
            return out
        try:
            records = sorted(list(getattr(tracker, "records", []) or []), key=lambda r: r.ts)
        except Exception:
            return out
        lots: Dict[str, List[List[object]]] = {}
        for r in records:
            code = str(getattr(r, "code", "") or "").strip()
            if not code:
                continue
            side = str(getattr(r, "side", "")).upper()
            qty = int(getattr(r, "qty", 0) or 0)
            px = float(getattr(r, "price", 0) or 0)
            if qty <= 0 or px <= 0:
                continue
            if side == "BUY":
                lots.setdefault(code, []).append([qty, px, r.ts])
                continue
            if side != "SELL":
                continue
            remain = qty
            buy_ts = None
            buy_cost = 0.0
            buy_qty = 0
            q = lots.setdefault(code, [])
            while remain > 0 and q:
                lot_qty, lot_px, lot_ts = q[0]
                take = min(int(lot_qty), remain)
                buy_cost += float(lot_px) * take
                buy_qty += take
                if buy_ts is None:
                    buy_ts = lot_ts
                lot_qty -= take
                remain -= take
                if lot_qty <= 0:
                    q.pop(0)
                else:
                    q[0][0] = lot_qty
            if buy_qty <= 0:
                continue
            avg_buy = buy_cost / float(buy_qty)
            pnl_pct = float(getattr(r, "pnl_pct", None) or 0.0)
            if not math.isfinite(pnl_pct) or abs(pnl_pct) < 1e-12:
                pnl_pct = (px / avg_buy - 1.0) * 100.0 if avg_buy > 0 else 0.0
            out.setdefault(code, []).append(
                {
                    "buy_ts": buy_ts,
                    "sell_ts": r.ts,
                    "pnl_pct": float(pnl_pct),
                    "sell_reason": str(getattr(r, "sell_reason", "") or ""),
                    "qty": int(buy_qty),
                }
            )
        return out

    def _sell_records_for_code(self, code: str) -> List[object]:
        tracker = getattr(self, "trade_tracker", None)
        if tracker is None:
            return []
        try:
            records = list(getattr(tracker, "records", []) or [])
        except Exception:
            return []
        return [
            r
            for r in records
            if str(getattr(r, "code", "")).strip() == str(code)
            and str(getattr(r, "side", "")).upper() == "SELL"
        ]

    def _recent_loss_count_for_code(self, code: str) -> int:
        cutoff = datetime.datetime.now() - datetime.timedelta(days=int(RECENT_LOSS_LOOKBACK_DAYS))
        count = 0
        for r in self._sell_records_for_code(code):
            ts = getattr(r, "ts", None)
            pnl_pct = float(getattr(r, "pnl_pct", 0.0) or 0.0)
            if isinstance(ts, datetime.datetime) and ts >= cutoff and pnl_pct < 0:
                count += 1
        return count

    def _is_market_weak(self) -> bool:
        return self._market_regime_label() == "bear"

    def _is_bull_regime(self) -> bool:
        return self._market_regime_label() == "bull"

    def _refresh_mock_server_flag(self) -> bool:
        """키움 GetServerGubun==1 이면 모의투자."""
        kiwoom = getattr(self, "kiwoom", None)
        cached = getattr(kiwoom, "is_mock_server", None) if kiwoom is not None else None
        if cached is True:
            self._is_mock_server = True
            return True
        try:
            gubun = str(
                kiwoom.dynamicCall("GetLoginInfo(QString)", "GetServerGubun") or ""
            ).strip()
            is_mock = gubun == "1"
            self._is_mock_server = is_mock
            if kiwoom is not None:
                kiwoom.is_mock_server = is_mock
            return is_mock
        except Exception:
            return bool(getattr(self, "_is_mock_server", False))

    def _is_mock_trading(self) -> bool:
        return bool(getattr(self, "_is_mock_server", False))

    def _risk_on_regime(self) -> bool:
        """상승·횡보 — 약세장 하드블록이 꺼진 구간."""
        return self._market_regime_label() in ("bull", "range")

    def _mock_keep_trading_active(self) -> bool:
        """모의 전용 유지매매. MOCK_LIVE_PARITY면 실전과 같이 항상 꺼짐."""
        if bool(MOCK_LIVE_PARITY):
            return False
        return self._is_mock_trading() and self._risk_on_regime()

    def _mock_bear_quality_dip_active(self) -> bool:
        """모의 약세 눌림 예외. MOCK_LIVE_PARITY면 실전처럼 약세 하드블록."""
        if bool(MOCK_LIVE_PARITY):
            return False
        return (
            bool(MOCK_BEAR_QUALITY_DIP)
            and self._is_mock_trading()
            and self._is_market_weak()
        )

    def _mock_ai_soft_cap_active(self) -> bool:
        return self._mock_keep_trading_active() or self._mock_bear_quality_dip_active()

    def _bull_entry_relax_active(self) -> bool:
        # 실전: 확인된 상승장만. 모의 유지매매의 횡보 완화는 _mock_keep_trading_active.
        return bool(BULL_ENTRY_RELAX_ENABLED) and self._is_bull_regime()

    def _effective_market_open_guard_minutes(self) -> int:
        base = max(0, int(MARKET_OPEN_BUY_GUARD_MINUTES))
        if self._bull_entry_relax_active():
            return max(10, base + int(BULL_GUARD_MINUTES_DELTA))
        return base

    def _vol_floor_exempt(self, code: Optional[str]) -> bool:
        """시총 우량허용 종목은 LIVE_VOL_ENTRY_FLOOR 대신 json VOL 그대로 사용."""
        c = str(code or "").strip()
        return bool(c) and c in (getattr(self, "_quality_mcap_allowlist", set()) or set())

    def _effective_vol_entry_ratio_min(self, code: Optional[str] = None) -> float:
        base = float(VOL_ENTRY_RATIO_MIN)
        if (not self._is_mock_trading() or bool(MOCK_LIVE_PARITY)) and not self._vol_floor_exempt(code):
            base = max(base, float(LIVE_VOL_ENTRY_FLOOR))
        if self._bull_entry_relax_active():
            base = max(0.55, base * float(BULL_VOL_RATIO_MULT))
        if self._mock_keep_trading_active():
            return min(base, float(MOCK_KEEP_VOL_CAP))
        return base

    def _effective_vol_entry_growth_min(self, code: Optional[str] = None) -> float:
        base = float(VOL_ENTRY_GROWTH_MIN)
        if (not self._is_mock_trading() or bool(MOCK_LIVE_PARITY)) and not self._vol_floor_exempt(code):
            base = max(base, float(LIVE_VOL_ENTRY_FLOOR))
        if self._bull_entry_relax_active():
            base = max(0.55, base * float(BULL_VOL_GROWTH_MULT))
        if self._mock_keep_trading_active():
            return min(base, float(MOCK_KEEP_VOL_CAP))
        return base

    def _in_market_open_buy_guard(self) -> bool:
        """09:00~09:XX 신규 매수 금지(갭·시초 변동성). guard_min=15 → 9:15부터 허용."""
        guard_min = self._effective_market_open_guard_minutes()
        if guard_min <= 0 or self.market_session_name() != "정규장":
            return False
        open_s = _krx_session_bounds(datetime.date.today())["open"]
        s = _qtime_sec(QTime.currentTime())
        return open_s <= s < open_s + guard_min * 60

    def _today_had_buy(self) -> bool:
        today = datetime.date.today()
        if any(d == today for d in (self.last_buy_date or {}).values()):
            return True
        tracker = getattr(self, "trade_tracker", None)
        if tracker is None:
            return False
        try:
            for r in list(getattr(tracker, "records", []) or []):
                if str(getattr(r, "side", "")).upper() != "BUY":
                    continue
                ts = getattr(r, "ts", None)
                if isinstance(ts, datetime.datetime) and ts.date() == today:
                    return True
        except Exception:
            pass
        return False

    def _no_trade_relax_active(self) -> bool:
        """당일 무매수·장중 N시 이후 진입 문턱 완화(약세장 하드블록은 별도)."""
        if not self._is_market_open() or self._today_had_buy():
            return False
        if self._is_market_weak():
            return False
        # 상승장은 추격 완화 금지. 모의 패리티도 실전과 동일.
        if self._is_bull_regime() and (
            (not self._is_mock_trading()) or bool(MOCK_LIVE_PARITY)
        ):
            return False
        now = datetime.datetime.now()
        try:
            hh, mm = NO_TRADE_RELAX_AFTER_TIME.split(":", 1)
            relax_after = datetime.time(int(hh), int(mm))
        except Exception:
            relax_after = datetime.time(int(NO_TRADE_RELAX_AFTER_HOUR), 0)
        if now.time() < relax_after:
            return False
        return True

    def _effective_buy_score_min(self) -> float:
        base = float(BUY_SCORE_MIN)
        if self._no_trade_relax_active():
            return max(0.10, base + float(NO_TRADE_RELAX_SCORE_DELTA))
        if self._bull_entry_relax_active():
            return max(0.10, base + float(BULL_SCORE_DELTA))
        return base

    def _today_realized_pnl_pct(self) -> float:
        tracker = getattr(self, "trade_tracker", None)
        if tracker is None:
            return 0.0
        today = datetime.date.today()
        total = 0.0
        try:
            records = list(getattr(tracker, "records", []) or [])
        except Exception:
            return 0.0
        for r in records:
            if str(getattr(r, "side", "")).upper() != "SELL":
                continue
            ts = getattr(r, "ts", None)
            if isinstance(ts, datetime.datetime) and ts.date() == today:
                total += float(getattr(r, "realized_pnl_krw", 0.0) or 0.0)
        base = max(1.0, float(TOTAL_LIMIT_KRW))
        return total / base

    def _today_sell_reason_count(self, reason: str) -> int:
        tracker = getattr(self, "trade_tracker", None)
        if tracker is None:
            return 0
        today = datetime.date.today()
        target = str(reason or "").strip()
        count = 0
        try:
            records = list(getattr(tracker, "records", []) or [])
        except Exception:
            return 0
        for r in records:
            if str(getattr(r, "side", "")).upper() != "SELL":
                continue
            ts = getattr(r, "ts", None)
            sell_reason = str(getattr(r, "sell_reason", "") or "").strip()
            if isinstance(ts, datetime.datetime) and ts.date() == today and sell_reason == target:
                count += 1
        return count

    def _today_sell_stats(self) -> tuple[int, int]:
        """당일 매도 완료 종목 수와 수익 종목 수를 반환.
        같은 종목의 반복 체결(부분 체결 or stop_loss 재시도)은 종목별로 합산해 1건으로 집계한다.
        """
        tracker = getattr(self, "trade_tracker", None)
        if tracker is None:
            return 0, 0
        today = datetime.date.today()
        try:
            records = list(getattr(tracker, "records", []) or [])
        except Exception:
            return 0, 0
        code_pnl: Dict[str, float] = {}  # 종목코드 → 당일 누적 실현손익
        for r in records:
            if str(getattr(r, "side", "")).upper() != "SELL":
                continue
            ts = getattr(r, "ts", None)
            if not (isinstance(ts, datetime.datetime) and ts.date() == today):
                continue
            code = str(getattr(r, "code", "") or "")
            pnl = float(getattr(r, "realized_pnl_krw", 0.0) or 0.0)
            code_pnl[code] = code_pnl.get(code, 0.0) + pnl
        sells = len(code_pnl)
        wins = sum(1 for v in code_pnl.values() if v > 0)
        return sells, wins

    def _effective_max_positions(self) -> int:
        limit = int(MAX_CONCURRENT_POSITIONS)
        if self._bull_entry_relax_active():
            # 보수 한도(예: 1)에서만 실질적으로 늘어나고, hard_cap이 현재 한도보다 작으면
            # 현재 한도를 그대로 사용해 원래 설정보다 더 느슨해지지 않도록 한다.
            limit = min(limit + int(BULL_MAX_POSITIONS_BONUS), max(limit, int(BULL_MAX_POSITIONS_HARD_CAP)))
        if self._is_market_weak():
            limit = min(limit, int(MARKET_WEAK_MAX_CONCURRENT_POSITIONS))
        if self._today_realized_pnl_pct() <= float(DAILY_LOSS_SOFT_LIMIT_PCT):
            limit = min(limit, int(DAILY_LOSS_MAX_CONCURRENT_POSITIONS))
        return max(1, limit)

    def _effective_recent_loss_block_count(self) -> int:
        """RECENT_LOSS_BLOCK_COUNT의 실효값. bull 국면에서는 소폭 완화, 그 외는 보수 기준 그대로."""
        limit = int(RECENT_LOSS_BLOCK_COUNT)
        if self._bull_entry_relax_active():
            limit += int(BULL_RECENT_LOSS_BONUS)
        return max(1, limit)

    def _load_ai_metrics_from_disk(self) -> None:
        """ai_calibration_metrics.json 에서 AUC/Brier/ECE·실전라벨 통계를 읽는다."""
        try:
            base = os.path.dirname(str(getattr(self, "ai_model_path", "") or "")) or "."
            path = os.path.join(base, str(AI_METRICS_JSON_BASENAME))
            if not os.path.isfile(path):
                return
            with open(path, "r", encoding="utf-8") as f:
                report = json.load(f)
            if not isinstance(report, dict):
                return
            cal = report.get("val_metrics_calibrated") or report.get("val_metrics_raw") or {}
            raw = report.get("val_metrics_raw") or {}
            src = cal if isinstance(cal, dict) and cal.get("auc") is not None else raw
            if isinstance(src, dict):
                try:
                    self.ai_val_auc = float(src.get("auc")) if src.get("auc") is not None else None
                except Exception:
                    self.ai_val_auc = None
                try:
                    self.ai_val_brier = float(src.get("brier")) if src.get("brier") is not None else None
                except Exception:
                    self.ai_val_brier = None
                try:
                    self.ai_val_ece = float(src.get("ece")) if src.get("ece") is not None else None
                except Exception:
                    self.ai_val_ece = None
            live = report.get("live_trade_labels") or {}
            if isinstance(live, dict):
                self.ai_live_injected = int(live.get("injected", 0) or 0)
                self.ai_live_wins = int(live.get("wins", 0) or 0)
        except Exception:
            pass

    def _ai_quality_size_factor(self) -> float:
        """3단계: AUC가 낮을수록 주문 수량을 줄인다."""
        if not bool(AI_QUALITY_SHRINK_ENABLED):
            return 1.0
        auc = getattr(self, "ai_val_auc", None)
        if auc is None:
            return 1.0
        try:
            a = float(auc)
        except Exception:
            return 1.0
        if a < float(AI_QUALITY_AUC_CRIT):
            return 0.25
        if a < float(AI_QUALITY_AUC_HARD):
            return 0.50
        if a < float(AI_QUALITY_AUC_SOFT):
            return 0.75
        return 1.0

    def _ai_quality_thr_add(self) -> float:
        if not bool(AI_QUALITY_SHRINK_ENABLED):
            return 0.0
        auc = getattr(self, "ai_val_auc", None)
        if auc is None:
            return 0.0
        try:
            a = float(auc)
        except Exception:
            return 0.0
        if a < float(AI_QUALITY_AUC_CRIT):
            return float(AI_QUALITY_THR_ADD_CRIT)
        if a < float(AI_QUALITY_AUC_HARD):
            return float(AI_QUALITY_THR_ADD_HARD)
        if a < float(AI_QUALITY_AUC_SOFT):
            return float(AI_QUALITY_THR_ADD_SOFT)
        return 0.0

    def _watchlist_news_avg(self) -> Optional[float]:
        if not NEWS_SENTIMENT_AVAILABLE:
            return None
        codes = list(dict.fromkeys(list(self.stock_codes or []) + list((self.positions or {}).keys())))
        if not codes:
            return None
        vals: List[float] = []
        for c in codes[:40]:
            try:
                vals.append(float(get_news_sentiment(c)))
            except Exception:
                continue
        if not vals:
            return None
        return float(sum(vals) / float(len(vals)))

    def _day_bias_adjustments(self) -> Dict[str, float]:
        """4단계: 뉴스 평균·시장 국면으로 당일 AI문턱/수량 편향만 조절."""
        out = {"ai_add": 0.0, "size_mult": 1.0, "news_avg": 0.0}
        if not bool(DAY_BIAS_ENABLED):
            return out
        now = datetime.datetime.now()
        cached = getattr(self, "_day_bias_cache", None)
        cache_at = getattr(self, "_day_bias_cache_at", None)
        if (
            isinstance(cached, dict)
            and isinstance(cache_at, datetime.datetime)
            and (now - cache_at).total_seconds() < 60.0
        ):
            return dict(cached)
        news_avg = self._watchlist_news_avg()
        if news_avg is None:
            self._day_bias_cache = dict(out)
            self._day_bias_cache_at = now
            return out
        out["news_avg"] = float(news_avg)
        regime = self._market_regime_label()
        if news_avg <= float(DAY_BIAS_NEWS_NEG):
            out["ai_add"] = float(DAY_BIAS_NEG_AI_ADD)
            out["size_mult"] = float(DAY_BIAS_NEG_SIZE_MULT)
        elif news_avg >= float(DAY_BIAS_NEWS_POS) and regime == "bull":
            out["ai_add"] = float(DAY_BIAS_POS_AI_DELTA)
        last = getattr(self, "_day_bias_last_log_at", None)
        if last is None or (now - last).total_seconds() >= 600.0:
            self._day_bias_last_log_at = now
            self._emit_log(
                f"[DAY-BIAS] news_avg={news_avg:+.3f} regime={regime or '-'} "
                f"ai_add={out['ai_add']:+.3f} size×{out['size_mult']:.2f}"
            )
        self._day_bias_cache = dict(out)
        self._day_bias_cache_at = now
        return out

    def _dynamic_ai_entry_min(self) -> float:
        threshold = float(AI_PROBA_ENTRY_MIN)
        if self._is_market_weak():
            threshold += float(MARKET_WEAK_AI_ENTRY_ADD)
        if self._today_realized_pnl_pct() <= float(DAILY_LOSS_SOFT_LIMIT_PCT):
            threshold += float(DAILY_LOSS_AI_ENTRY_ADD)
        with self._auto_bt_lock:
            bt_mode = str(self._auto_bt_state.get("mode", "neutral"))
        if bt_mode == "caution":
            threshold += float(AUTO_BT_CAUTION_AI_ENTRY_ADD)
        if self._no_trade_relax_active():
            threshold += float(NO_TRADE_RELAX_AI_DELTA)
        if self._bull_entry_relax_active():
            threshold += float(BULL_AI_ENTRY_DELTA)
        # 3단계: 학습 품질 나쁨 → 문턱 가산
        threshold += float(self._ai_quality_thr_add())
        # 4단계: 당일 뉴스·국면 편향
        try:
            threshold += float(self._day_bias_adjustments().get("ai_add", 0.0) or 0.0)
        except Exception:
            pass
        # 상승·횡보 유지매매는 모의투자만. 실전은 auc 품질가산(+0.06) 포함한 기존 문턱.
        if self._mock_keep_trading_active():
            return float(AI_PROBA_ENTRY_MIN)
        return min(0.80, max(0.0, threshold))

    def _auto_bt_mode(self) -> str:
        with self._auto_bt_lock:
            return str(self._auto_bt_state.get("mode", "neutral"))

    def _auto_bt_code_score(self, code: str) -> float:
        with self._auto_bt_lock:
            return float(self._auto_bt_score_by_code.get(code, 0.0) or 0.0)

    def _buy_score(self, code: str) -> float:
        v = self.ma.get(code) or {}
        cur = float(self.current_price.get(code, 0) or 0)
        ma20 = float(v.get("ma20", 0.0) or 0.0)
        if cur <= 0 or ma20 <= 0:
            return float("-inf")
        rsi = float(v.get("rsi14", 50.0) or 50.0)
        rsi_prev = float(v.get("rsi14_prev", rsi) or rsi)
        vol_ratio = float(v.get("vol_ratio", 0.0) or 0.0)
        atr_pct = float(v.get("atr_pct", 0.0) or 0.0)
        trade_feats = self._trade_features_for_code(code)
        trade_win = float(trade_feats[0]) if trade_feats else 0.5
        trade_avg = float(trade_feats[1]) if len(trade_feats) > 1 else 0.0
        trade_count = float(trade_feats[2]) if len(trade_feats) > 2 else 0.0
        trade_stop = float(trade_feats[3]) if len(trade_feats) > 3 else 0.0
        trade_weak = float(trade_feats[4]) if len(trade_feats) > 4 else 0.0

        dip = self._use_quality_dip_for_code(code)
        if dip:
            rel = cur / ma20 - 1.0
            ma_score = max(-0.5, min(0.55, (-rel) * 3.0))
            rsi_score = max(-0.45, min(0.55, (float(QDIP_RSI_MAX_ENTRY) + 8.0 - rsi) / 55.0)) + max(
                -0.15, min(0.25, (rsi - rsi_prev) / 22.0)
            )
            vol_score = max(0.0, min(1.1, vol_ratio / 2.5))
            thr = float(self._dynamic_ai_entry_min_soft()) - max(0.0, float(QUALITY_AI_THR_RELAX))
            thr = max(0.50, thr)
            ai_score = 0.0
            proba_up = self._ai_proba_up(code, cur)
            if proba_up is not None:
                ai_score = (float(proba_up) - thr) * 2.0
            news_score = 0.0
            if NEWS_SENTIMENT_AVAILABLE:
                try:
                    ns = float(get_news_sentiment(code))
                    news_score = max(-0.35, min(0.55, ns * 0.85))
                except Exception:
                    news_score = 0.0
            theme_boost = 0.0
            if NEWS_SENTIMENT_AVAILABLE and bool(_THEME_STRENGTH_ENABLED):
                try:
                    theme_boost = float(_get_theme_boost_for_code(code))
                except Exception:
                    theme_boost = 0.0
            atr_penalty = max(0.0, min(0.85, atr_pct * 11.0))
            loss_penalty = min(1.0, self._recent_loss_count_for_code(code) * 0.4)
            return (
                ma_score
                + rsi_score
                + vol_score
                + ai_score
                + news_score
                + theme_boost
                + (trade_win - 0.5)
                + trade_avg
                + (trade_count * 0.1)
                - (trade_stop * 0.35)
                - (trade_weak * 0.25)
                + (self._auto_bt_code_score(code) * float(AUTO_BT_SCORE_WEIGHT))
                - atr_penalty
                - loss_penalty
            )

        ma_score = max(-0.5, min(0.5, (cur / ma20 - 1.0))) * 2.0
        rsi_score = max(-0.5, min(0.5, (rsi - 50.0) / 50.0)) + max(-0.2, min(0.2, (rsi - rsi_prev) / 20.0))
        vol_score = max(0.0, min(1.0, vol_ratio / 2.0))
        atr_penalty = max(0.0, min(0.8, atr_pct * 10.0))
        ai_score = 0.0
        proba_up = self._ai_proba_up(code, cur)
        if proba_up is not None:
            ai_score = (float(proba_up) - self._dynamic_ai_entry_min()) * 2.0
        loss_penalty = min(1.0, self._recent_loss_count_for_code(code) * 0.4)
        theme_boost = 0.0
        if NEWS_SENTIMENT_AVAILABLE and bool(_THEME_STRENGTH_ENABLED):
            try:
                theme_boost = float(_get_theme_boost_for_code(code))
            except Exception:
                theme_boost = 0.0
        return (
            ma_score
            + rsi_score
            + vol_score
            + ai_score
            + theme_boost
            + (trade_win - 0.5)
            + trade_avg
            + (trade_count * 0.1)
            - (trade_stop * 0.35)
            - (trade_weak * 0.25)
            + (self._auto_bt_code_score(code) * float(AUTO_BT_SCORE_WEIGHT))
            - atr_penalty
            - loss_penalty
        )

    def _ranked_buy_candidates(self) -> List[tuple]:
        ranked: List[tuple] = []
        for code in self.stock_codes:
            try:
                if not self._should_buy(code):
                    continue
                score = self._buy_score(code)
                if score >= self._effective_buy_score_min():
                    ranked.append((score, code))
            except Exception:
                continue
        ranked.sort(reverse=True)
        return ranked

    def _maybe_start_auto_backtest(self, force: bool = False) -> None:
        if not bool(AUTO_BACKTEST_GATE_ENABLED):
            return
        if not self.stock_codes:
            return
        now = datetime.datetime.now()
        with self._auto_bt_lock:
            if self._auto_bt_running:
                return
            if (
                not force
                and isinstance(self._auto_bt_last_at, datetime.datetime)
                and (now - self._auto_bt_last_at).total_seconds() < float(AUTO_BACKTEST_CACHE_SEC)
            ):
                return
            self._auto_bt_running = True

        held = {
            c
            for c, p in (self.positions or {}).items()
            if int((p or {}).get("qty", 0) or 0) > 0
        }
        codes = [c for c in list(dict.fromkeys(self.stock_codes)) if c not in held]
        codes = codes[: max(1, int(AUTO_BACKTEST_MAX_CODES))]

        def _worker() -> None:
            mode = "neutral"
            avg_ret = 0.0
            win_rate = 0.0
            reason = "ok"
            score_by_code: Dict[str, float] = {}
            try:
                result = run_backtest_for_codes(codes, lookback=BACKTEST_LOOKBACK_BARS)
                if not bool(result.get("ok", False)):
                    reason = str(result.get("reason", "failed"))
                else:
                    avg_ret = float(result.get("avg_ret", 0.0) or 0.0)
                    win_rate = float(result.get("win_rate", 0.0) or 0.0)
                    for row in list(result.get("results", []) or []):
                        code = str((row or {}).get("code", "")).strip()
                        if code:
                            score_by_code[code] = float((row or {}).get("ret", 0.0) or 0.0)
                    if avg_ret <= float(AUTO_BT_BLOCK_AVG_RET) or win_rate <= float(AUTO_BT_BLOCK_WIN_RATE):
                        mode = "block"
                    elif avg_ret <= float(AUTO_BT_CAUTION_AVG_RET) or win_rate <= float(AUTO_BT_CAUTION_WIN_RATE):
                        mode = "caution"
            except Exception as e:
                reason = f"error:{e}"
            finally:
                with self._auto_bt_lock:
                    self._auto_bt_state = {
                        "mode": mode,
                        "avg_ret": avg_ret,
                        "win_rate": win_rate,
                        "reason": reason,
                    }
                    self._auto_bt_score_by_code = score_by_code
                    self._auto_bt_last_at = datetime.datetime.now()
                    self._auto_bt_running = False
                self._emit_log(
                    f"[BT-GATE] mode={mode} avg={avg_ret*100:+.2f}% "
                    f"win={win_rate*100:.1f}% reason={reason}"
                )

        threading.Thread(target=_worker, daemon=True).start()

    def _risk_pcts_for_code(self, code: str) -> tuple[float, float]:
        # 재진입 포지션은 기본 손절보다 타이트하게 (-1.5%)
        is_reentry = code in getattr(self, "_reentry_codes", set())

        if not bool(USE_ATR_RISK):
            sl = float(REENTRY_STOP_LOSS_PCT) if is_reentry else float(STOP_LOSS_PCT)
            return sl, float(TAKE_PROFIT_PCT)
        atr_pct = float((self.ma.get(code) or {}).get("atr_pct", 0.0) or 0.0)
        if atr_pct <= 0:
            sl = float(REENTRY_STOP_LOSS_PCT) if is_reentry else float(STOP_LOSS_PCT)
            return sl, float(TAKE_PROFIT_PCT)
        stop_abs = min(float(ATR_STOP_MAX_PCT), max(float(ATR_STOP_MIN_PCT), atr_pct * float(ATR_STOP_MULT)))
        take_abs = min(float(ATR_TAKE_MAX_PCT), max(float(ATR_TAKE_MIN_PCT), atr_pct * float(ATR_TAKE_MULT)))
        # 재진입은 ATR 손절도 REENTRY_STOP_LOSS_PCT 이하로 제한
        if is_reentry:
            stop_abs = min(stop_abs, abs(float(REENTRY_STOP_LOSS_PCT)))
        return -stop_abs, take_abs

    def _ai_features_from_cached(self, code: str, cur_price: float) -> Optional[List[float]]:
        """
        캐시된 MA/RSI/거래량/1일수익률을 사용해 RF 입력 피처를 생성합니다.
        """
        v = self.ma.get(code)
        if not v:
            return None
        ma5 = float(v.get("ma5", 0.0))
        ma20 = float(v.get("ma20", 0.0))
        ma5_prev = float(v.get("ma5_prev", ma5))
        ma20_prev = float(v.get("ma20_prev", ma20))
        rsi14 = float(v.get("rsi14", 0.0))
        vol_ratio = float(v.get("vol_ratio", 0.0))
        vol_chg = float(v.get("vol_chg", 0.0))
        ret_1d = float(v.get("ret_1d", 0.0))
        volatility = float(v.get("volatility", 0.0))
        macd_line = float(v.get("macd_line", 0.0))
        macd_signal = float(v.get("macd_signal", 0.0))
        atr_pct = float(v.get("atr_pct", 0.0))

        if ma20 <= 0:
            return None

        # 피처 스케일을 단순화(트리 모델이라 큰 스케일 이슈는 덜하지만, 안정성 위해 정규화)
        f1 = (ma5 / ma20) - 1.0
        f2 = (ma5_prev / ma20_prev) - 1.0
        f3 = rsi14 / 100.0
        f4 = vol_ratio
        f5 = ret_1d
        f6 = vol_chg
        f7 = volatility
        f8 = macd_line / ma20
        f9 = macd_signal / ma20
        f10 = atr_pct
        f11, f12, f13, f14, f15 = self._trade_features_for_code(code)

        # 뉴스 감성 점수 (캐시 우선, 네트워크 블로킹 없음)
        f16 = float(get_news_sentiment(code)) if NEWS_SENTIMENT_AVAILABLE else 0.0

        # cur_price 기반 피처가 필요하다면 여기서 추가 가능(현재는 ret_1d/ma 비율로 대체)
        _ = cur_price
        return [f1, f2, f3, f4, f5, f6, f7, f8, f9, f10, f11, f12, f13, f14, f15, f16]

    def _ai_proba_up(self, code: str, cur_price: float) -> Optional[float]:
        if self.ai_model is None:
            return None
        x = self._ai_features_from_cached(code, cur_price)
        if x is None:
            return None
        try:
            return float(self.ai_model.predict_proba([x])[0][1])
        except Exception:
            return None

    def _build_ai_classifier(self):
        backend = str(AI_MODEL_BACKEND or "auto").lower()
        if backend in ("auto", "lightgbm", "lgbm") and LGBMClassifier is not None:
            try:
                return (
                    LGBMClassifier(
                        n_estimators=AI_N_ESTIMATORS,
                        random_state=AI_RANDOM_STATE,
                        class_weight="balanced",
                        verbosity=-1,
                    ),
                    "LightGBM",
                )
            except Exception:
                pass
        if backend in ("auto", "xgboost", "xgb") and XGBClassifier is not None:
            try:
                return (
                    XGBClassifier(
                        n_estimators=AI_N_ESTIMATORS,
                        random_state=AI_RANDOM_STATE,
                        eval_metric="logloss",
                        n_jobs=-1,
                    ),
                    "XGBoost",
                )
            except Exception:
                pass
        if RandomForestClassifier is not None:
            return (
                RandomForestClassifier(
                    n_estimators=AI_N_ESTIMATORS,
                    random_state=AI_RANDOM_STATE,
                    n_jobs=-1,
                    class_weight="balanced",
                ),
                "RandomForest",
            )
        return None, ""

    def _load_ai_model_from_disk(self, allow_stale: bool = False) -> bool:
        path = str(getattr(self, "ai_model_path", "") or "")
        if not path or not os.path.isfile(path):
            return False
        try:
            with open(path, "rb") as f:
                payload = pickle.load(f)
            if not isinstance(payload, dict):
                return False
            feature_names = list(payload.get("feature_names", []) or [])
            if feature_names != list(AI_FEATURE_NAMES):
                self._emit_log("[AI] 저장 모델 무시: feature schema mismatch")
                return False
            train_tag = str(payload.get("train_tag", "") or "")
            if train_tag != str(AI_MODEL_TRAIN_TAG):
                self._emit_log(
                    f"[AI] 저장 모델 무시: train_tag mismatch "
                    f"({train_tag or '-'} != {AI_MODEL_TRAIN_TAG})"
                )
                return False
            model = payload.get("model")
            if model is None or not hasattr(model, "predict_proba"):
                self._emit_log("[AI] 저장 모델 무시: predict_proba 없음")
                return False
            trained_raw = str(payload.get("trained_date", "") or "")
            trained_date: Optional[datetime.date] = None
            if trained_raw:
                try:
                    trained_date = datetime.date.fromisoformat(trained_raw)
                except ValueError:
                    trained_date = None
            stale = False
            if trained_date is not None:
                age_days = (datetime.date.today() - trained_date).days
                if age_days > int(AI_MODEL_MAX_AGE_DAYS):
                    if not allow_stale:
                        self._emit_log(
                            f"[AI] 저장 모델 오래됨({age_days}일>{int(AI_MODEL_MAX_AGE_DAYS)}일) "
                            "— 임시로드 시도/재학습 예약"
                        )
                        return False
                    stale = True
                    self._emit_log(
                        f"[AI] 저장 모델 임시 사용(오래됨 {age_days}일) — 재학습 전까지 유지"
                    )
            self.ai_model = model
            self.ai_trained_date = trained_date
            self.ai_model_name = str(payload.get("model_name", "unknown") or "unknown")
            self.ai_model_samples = int(payload.get("samples", 0) or 0)
            self.ai_model_loaded_from_disk = True
            self._ai_needs_retrain = bool(stale)
            self._load_ai_metrics_from_disk()
            self._emit_log(
                f"[AI] 저장 모델 로드: {self.ai_model_name} "
                f"samples={self.ai_model_samples} date={trained_raw or '-'} | {self.ai_status_line()}"
            )
            return True
        except Exception as e:
            self._emit_log(f"[AI] 저장 모델 로드 실패: {e}")
            return False

    def _save_ai_model_to_disk(
        self,
        model: object,
        model_name: str,
        samples: int,
        trained_date: datetime.date,
        extra_payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        path = str(getattr(self, "ai_model_path", "") or "")
        if not path:
            return
        payload: Dict[str, Any] = {
            "version": 2,
            "train_tag": str(AI_MODEL_TRAIN_TAG),
            "model": model,
            "model_name": str(model_name or "unknown"),
            "samples": int(samples),
            "trained_date": trained_date.isoformat(),
            "feature_names": list(AI_FEATURE_NAMES),
            "label_horizon_days": int(AI_LABEL_HORIZON_DAYS),
            "round_trip_cost_pct": float(AI_ROUND_TRIP_COST_PCT),
            "slippage_pct": float(AI_SLIPPAGE_PCT),
        }
        if isinstance(extra_payload, dict):
            payload.update(extra_payload)
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "wb") as f:
                pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)
            self._emit_log(f"[AI] 모델 저장 완료: {os.path.basename(path)}")
        except Exception as e:
            self._emit_log(f"[AI] 모델 저장 실패: {e}")

    def train_random_forest(self) -> None:
        model, model_name = self._build_ai_classifier()
        if model is None:
            return

        today = datetime.date.today()
        prev_auc = getattr(self, "ai_val_auc", None) if self.ai_model is not None else None
        prev_metrics_txt: Optional[str] = None
        try:
            _mp = os.path.join(
                os.path.dirname(str(self.ai_model_path)) or ".", str(AI_METRICS_JSON_BASENAME)
            )
            if os.path.isfile(_mp):
                with open(_mp, "r", encoding="utf-8") as _f:
                    prev_metrics_txt = _f.read()
        except Exception:
            prev_metrics_txt = None
        # 실패 시에도 날짜를 찍으면 당일 재학습이 영구 스킵되므로 성공 시에만 기록
        try:
            end_date = today.strftime("%Y%m%d")
            aicfg = aos.AiObjectiveConfig(
                label_horizon_days=int(AI_LABEL_HORIZON_DAYS),
                atr_period=int(ATR_PERIOD),
                use_atr_risk=bool(USE_ATR_RISK),
                stop_loss_pct=float(STOP_LOSS_PCT),
                take_profit_pct=float(TAKE_PROFIT_PCT),
                atr_stop_mult=float(ATR_STOP_MULT),
                atr_take_mult=float(ATR_TAKE_MULT),
                atr_stop_min_pct=float(ATR_STOP_MIN_PCT),
                atr_stop_max_pct=float(ATR_STOP_MAX_PCT),
                atr_take_min_pct=float(ATR_TAKE_MIN_PCT),
                atr_take_max_pct=float(ATR_TAKE_MAX_PCT),
                round_trip_cost_pct=float(AI_ROUND_TRIP_COST_PCT),
                slippage_pct=float(AI_SLIPPAGE_PCT),
            )

            X_train: List[List[float]] = []
            y_train: List[int] = []
            w_train: List[float] = []
            X_val: List[List[float]] = []
            y_val: List[int] = []
            w_val: List[float] = []
            label_audit_y: List[int] = []
            label_audit_net: List[float] = []
            live_trips_by_code: Dict[str, List[Dict[str, object]]] = {}
            try:
                live_trips_by_code = self._live_round_trips_from_tracker()
            except Exception:
                live_trips_by_code = {}
            live_sample_n = 0
            live_win_n = 0

            use_calib = bool(
                AI_AVAILABLE and clone is not None and CalibratedClassifierCV is not None
            )
            val_frac = float(AI_VAL_FRAC)
            val_frac = min(0.45, max(0.05, val_frac))
            min_pc = max(3, int(AI_VAL_MIN_ROWS_PER_CODE))

            # 각 종목별로 학습 데이터를 만들고 합칩니다.
            # (학습 시 TR을 여러 번 호출하므로, 가능한 한 종목/샘플 수를 제한)
            train_count = AI_TRAIN_COUNT
            training_codes = list(dict.fromkeys(self.stock_codes or []))
            # 스캔 전이거나 장외면 보유·실전거래 종목도 학습 대상에 포함
            for c in list((self.positions or {}).keys()):
                c6 = str(c).strip()
                if c6 and c6 not in training_codes:
                    training_codes.append(c6)
            for code_key in list(live_trips_by_code.keys()):
                c6 = str(code_key).strip()
                if c6 and c6 not in training_codes:
                    training_codes.append(c6)
            if len(training_codes) < int(AI_TRAIN_MIN_CODES):
                try:
                    supplement = _mc_top_allowlist_for_scan(int(AI_TRAIN_SUPPLEMENT_MC_TOP_N))
                except Exception:
                    supplement = None
                if supplement:
                    before_ct = len(training_codes)
                    for c in supplement:
                        c6 = str(c).strip()
                        if c6 and c6 not in training_codes:
                            training_codes.append(c6)
                        if len(training_codes) >= int(AI_TRAIN_MIN_CODES):
                            break
                    if len(training_codes) > before_ct:
                        self._emit_log(
                            f"[AI] 학습 종목 보강: {before_ct} -> {len(training_codes)} "
                            f"(시총상위 학습전용 추가, 실매매 대상엔 미포함)"
                        )
            if len(training_codes) < int(AI_TRAIN_MIN_CODES):
                before_ct = len(training_codes)
                for c in AI_TRAIN_FALLBACK_CODES:
                    c6 = str(c).strip()
                    if c6 and c6 not in training_codes:
                        training_codes.append(c6)
                if len(training_codes) > before_ct:
                    self._emit_log(
                        f"[AI] 학습 종목 보강: {before_ct} -> {len(training_codes)} "
                        f"(고정 유동성 종목 폴백)"
                    )
            if not training_codes:
                self._emit_log("[AI] 학습 스킵: 학습 종목 0개")
                if self.ai_model is None:
                    self._load_ai_model_from_disk(allow_stale=True)
                return
            self._emit_log(f"[AI] 학습 유니버스 {len(training_codes)}종목")
            for code in training_codes:
                ohlcv = self.kiwoom.request_daily_ohlcv(
                    code, end_date_yyyymmdd=end_date, count=train_count
                )
                closes = list(ohlcv.get("closes", []))
                highs = list(ohlcv.get("highs", []))
                lows = list(ohlcv.get("lows", []))
                volumes = list(ohlcv.get("volumes", []))
                if len(closes) < 50 or len(volumes) < 50:
                    continue
                if len(highs) != len(closes) or len(lows) != len(closes):
                    highs = list(closes)
                    lows = list(closes)

                # 시계열 전처리(학습 성능 개선)
                rsi_series = _compute_rsi_series_wilder(closes, RSI_PERIOD)
                closes_f = [float(x) for x in closes]
                highs_f = [float(x) for x in highs]
                lows_f = [float(x) for x in lows]
                ema12_series = _ema_series(closes_f, 12)
                ema26_series = _ema_series(closes_f, 26)
                macd_line_series = [a - b for a, b in zip(ema12_series, ema26_series)]
                macd_signal_series = _ema_series(macd_line_series, 9)
                trade_f1, trade_f2, trade_f3, trade_f4, trade_f5 = self._trade_features_for_code(code)

                X_code: List[List[float]] = []
                y_code: List[int] = []
                net_code: List[float] = []
                w_code: List[float] = []

                # prefix sum으로 MA 합을 빠르게 계산
                # prefix[i] = sum(closes[:i]) ; sum(l..r)=prefix[r]-prefix[l]
                prefix = [0.0]
                s = 0.0
                for p in closes:
                    s += float(p)
                    prefix.append(s)

                def _sum(l: int, r_excl: int) -> float:
                    return prefix[r_excl] - prefix[l]

                def _feats_at(t: int) -> Optional[List[float]]:
                    ma5 = _sum(t - 4, t + 1) / 5.0
                    ma20 = _sum(t - 19, t + 1) / 20.0
                    ma5_prev = _sum(t - 5, t) / 5.0
                    ma20_prev = _sum(t - 20, t - 1) / 20.0
                    if ma20 <= 0 or ma20_prev <= 0:
                        return None
                    rsi14 = rsi_series[t]
                    if rsi14 is None:
                        return None
                    vol_today = volumes[t]
                    vol_prev = volumes[t - 1]
                    vol_avg = sum(volumes[t - (VOL_AVG_PERIOD - 1) : t + 1]) / float(VOL_AVG_PERIOD)
                    if vol_avg <= 0:
                        return None
                    vol_ratio = vol_today / vol_avg
                    vol_chg = (vol_today / vol_prev - 1.0) if vol_prev > 0 else 0.0
                    close_today = closes[t]
                    close_prev = closes[t - 1]
                    if close_prev <= 0:
                        return None
                    ret_1d = close_today / close_prev - 1.0
                    rs: List[float] = []
                    st = max(1, t - 19)
                    for j in range(st, t + 1):
                        p0 = closes[j - 1]
                        p1 = closes[j]
                        if p0 > 0:
                            rs.append(p1 / p0 - 1.0)
                    if len(rs) >= 2:
                        mu = sum(rs) / float(len(rs))
                        var = sum((r - mu) * (r - mu) for r in rs) / float(len(rs) - 1)
                        volatility = var ** 0.5
                    else:
                        volatility = 0.0
                    macd_line = float(macd_line_series[t]) if t < len(macd_line_series) else 0.0
                    macd_signal = float(macd_signal_series[t]) if t < len(macd_signal_series) else 0.0
                    atr_pct = aos.compute_atr_pct(
                        highs_f[: t + 1],
                        lows_f[: t + 1],
                        closes_f[: t + 1],
                        int(ATR_PERIOD),
                    )
                    f1 = (ma5 / ma20) - 1.0
                    f2 = (ma5_prev / ma20_prev) - 1.0
                    f3 = float(rsi14) / 100.0
                    f4 = float(vol_ratio)
                    f5 = float(ret_1d)
                    f6 = float(vol_chg)
                    f7 = float(volatility)
                    f8 = float(macd_line / ma20) if ma20 > 0 else 0.0
                    f9 = float(macd_signal / ma20) if ma20 > 0 else 0.0
                    f10 = float(atr_pct)
                    # 일봉 학습에서는 뉴스 0(과거 재현 불가). 실전 피처는 종목 집계값 사용.
                    return [
                        f1, f2, f3, f4, f5, f6, f7, f8, f9, f10,
                        trade_f1, trade_f2, trade_f3, trade_f4, trade_f5, 0.0,
                    ]

                # 인덱스 t는 "오늘", 라벨은 t+1
                # 필요 조건: ma20(19..t), vol_avg(19..t), rsi14(14..t) 확보
                start_t = max(20, int(ATR_PERIOD) + 1)
                horizon = max(1, int(AI_LABEL_HORIZON_DAYS))
                for t in range(start_t, len(closes) - horizon):
                    feats = _feats_at(t)
                    if feats is None:
                        continue
                    X_code.append(feats)
                    entry = float(closes[t])
                    exit_price = aos.resolve_exit_price(
                        closes_f, highs_f, lows_f, t, float(feats[9]), aicfg
                    )
                    net_ret = aos.net_return_after_costs(entry, exit_price, aicfg)
                    y = 1 if net_ret > float(AI_RET_LABEL_THRESHOLD) else 0
                    y_code.append(y)
                    net_code.append(float(net_ret))
                    w_code.append(1.0)

                # 실전 CSV 라운드트립은 분할 후 train에만 주입(시계열 끝으로 쏠림 방지)
                live_X: List[List[float]] = []
                live_y: List[int] = []
                live_net: List[float] = []
                live_w: List[float] = []
                if bool(AI_USE_LIVE_TRADE_LABELS):
                    dates = [str(d).strip() for d in list(ohlcv.get("dates", []) or [])]
                    date_to_idx: Dict[str, int] = {}
                    for i, d in enumerate(dates):
                        if len(d) >= 8:
                            date_to_idx[d[:8]] = i
                    if not date_to_idx and list(live_trips_by_code.get(code, []) or []):
                        # 날짜 누락 시 매수일 매칭 불가 — 로그는 종목당 1회만
                        if not getattr(self, "_ai_live_date_warn_codes", None):
                            self._ai_live_date_warn_codes = set()
                        if code not in self._ai_live_date_warn_codes:
                            self._ai_live_date_warn_codes.add(code)
                            self._emit_log(
                                f"[AI] 실전라벨 스킵 {code}: OHLCV dates 비어있음 "
                                f"(closes={len(closes)})"
                            )
                    live_weight = max(1.0, float(AI_LIVE_TRADE_SAMPLE_WEIGHT))
                    for trip in list(live_trips_by_code.get(code, []) or []):
                        buy_ts = trip.get("buy_ts")
                        if not isinstance(buy_ts, datetime.datetime):
                            continue
                        key = buy_ts.strftime("%Y%m%d")
                        t_idx = date_to_idx.get(key)
                        if t_idx is None:
                            for back in range(1, 6):
                                alt = (buy_ts - datetime.timedelta(days=back)).strftime("%Y%m%d")
                                if alt in date_to_idx:
                                    t_idx = date_to_idx[alt]
                                    break
                        if t_idx is None or t_idx < start_t or t_idx >= len(closes):
                            continue
                        feats = _feats_at(int(t_idx))
                        if feats is None:
                            continue
                        pnl_pct = float(trip.get("pnl_pct", 0.0) or 0.0)
                        y_live = 1 if pnl_pct > 0.0 else 0
                        live_X.append(feats)
                        live_y.append(y_live)
                        live_net.append(float(pnl_pct) / 100.0)
                        live_w.append(float(live_weight))
                        live_sample_n += 1
                        if y_live == 1:
                            live_win_n += 1

                nloc = len(X_code)
                if nloc <= 0 and not live_X:
                    continue
                label_audit_y.extend(y_code)
                label_audit_net.extend(net_code)
                label_audit_y.extend(live_y)
                label_audit_net.extend(live_net)
                if nloc <= 0:
                    X_train.extend(live_X)
                    y_train.extend(live_y)
                    w_train.extend(live_w)
                    continue
                if not use_calib or nloc < min_pc:
                    X_train.extend(X_code)
                    y_train.extend(y_code)
                    w_train.extend(w_code)
                    X_train.extend(live_X)
                    y_train.extend(live_y)
                    w_train.extend(live_w)
                    continue
                n_val_pc = max(1, int(round(nloc * val_frac)))
                n_tr_pc = nloc - n_val_pc
                if n_tr_pc < 1:
                    n_tr_pc = nloc - 1
                    n_val_pc = nloc - n_tr_pc
                X_train.extend(X_code[:n_tr_pc])
                y_train.extend(y_code[:n_tr_pc])
                w_train.extend(w_code[:n_tr_pc])
                X_train.extend(live_X)
                y_train.extend(live_y)
                w_train.extend(live_w)
                X_val.extend(X_code[n_tr_pc:])
                y_val.extend(y_code[n_tr_pc:])
                w_val.extend(w_code[n_tr_pc:])

            n_total = len(X_train) + len(X_val)
            if n_total < AI_MIN_TOTAL_SAMPLES:
                # 표본이 부족하면 모델 학습 스킵 (날짜 찍지 않음 → 재시도 가능)
                self._emit_log(f"[AI] 학습 스킵: samples={n_total}/{int(AI_MIN_TOTAL_SAMPLES)}")
                if self.ai_model is None:
                    self._load_ai_model_from_disk(allow_stale=True)
                return

            if bool(AI_USE_LIVE_TRADE_LABELS):
                trip_codes = len(live_trips_by_code)
                trip_n = sum(len(v) for v in live_trips_by_code.values())
                self._emit_log(
                    f"[AI] 실전라벨: trips={trip_n} codes={trip_codes} "
                    f"injected={live_sample_n} win={live_win_n} "
                    f"weight={float(AI_LIVE_TRADE_SAMPLE_WEIGHT):.1f}"
                )

            label_report = _ai_label_distribution_report(label_audit_y, label_audit_net)
            calib_applied = False
            metrics_report: Dict[str, Any] = {
                "trained_date": today.isoformat(),
                "model_name": str(model_name or "unknown"),
                "label_audit": label_report,
                "live_trade_labels": {
                    "enabled": bool(AI_USE_LIVE_TRADE_LABELS),
                    "injected": int(live_sample_n),
                    "wins": int(live_win_n),
                    "sample_weight": float(AI_LIVE_TRADE_SAMPLE_WEIGHT),
                },
                "split": {
                    "n_train": len(X_train),
                    "n_val": len(X_val),
                    "val_frac_target": val_frac,
                    "per_code_split_min_rows": min_pc,
                },
            }

            train_merge_min = max(30, int(AI_TRAIN_MERGE_BACK_MIN))
            cal_min = max(15, int(AI_CALIBRATION_MIN_VAL))
            biclass_val = len(set(y_val)) >= 2 if y_val else False

            if (
                use_calib
                and len(X_val) >= cal_min
                and biclass_val
                and len(X_train) >= train_merge_min
            ):
                base_m = clone(model)
                try:
                    base_m.fit(X_train, y_train, sample_weight=w_train)
                except TypeError:
                    base_m.fit(X_train, y_train)
                p_raw_v: List[float] = []
                try:
                    p_raw_v = [float(p[1]) for p in base_m.predict_proba(X_val)]
                except Exception:
                    p_raw_v = []
                metrics_report["val_metrics_raw"] = _ai_binary_metrics_safe(y_val, p_raw_v)

                cal_method = (
                    str(AI_CALIBRATION_METHOD).lower()
                    if str(AI_CALIBRATION_METHOD).lower() in ("sigmoid", "isotonic")
                    else "sigmoid"
                )
                # sklearn>=1.2는 estimator=, <1.2는 base_estimator= 파라미터명을 사용한다.
                # 설치된 버전(1.1.2)에서 estimator=만 쓰면 매번
                # "__init__() got an unexpected keyword argument 'estimator'"로 학습이 중단되어
                # AI 모델이 몇 주간 전혀 갱신되지 못했던 원인이었다 — 두 버전 모두 지원하도록 방어.
                try:
                    calibrator = CalibratedClassifierCV(estimator=base_m, method=cal_method, cv="prefit")
                except TypeError:
                    calibrator = CalibratedClassifierCV(base_estimator=base_m, method=cal_method, cv="prefit")
                try:
                    calibrator.fit(X_val, y_val, sample_weight=w_val)
                except TypeError:
                    calibrator.fit(X_val, y_val)
                p_cal_v: List[float] = []
                try:
                    p_cal_v = [float(p[1]) for p in calibrator.predict_proba(X_val)]
                except Exception:
                    p_cal_v = []
                metrics_report["val_metrics_calibrated"] = _ai_binary_metrics_safe(y_val, p_cal_v)
                metrics_report["calibration"] = {"method": cal_method, "cv": "prefit"}

                raw = metrics_report.get("val_metrics_raw") or {}
                cal = metrics_report.get("val_metrics_calibrated") or {}

                def _fmtm(md: Dict[str, float], k: str) -> str:
                    v = md.get(k, float("nan"))
                    try:
                        x = float(v)
                        return "nan" if not math.isfinite(x) else f"{x:.4f}"
                    except Exception:
                        return "nan"

                self._emit_log(
                    f"[AI] 시계열 val(n={len(y_val)}) raw: auc={_fmtm(raw, 'auc')} "
                    f"brier={_fmtm(raw, 'brier')} ece={_fmtm(raw, 'ece')} | "
                    f"cal({cal_method}): auc={_fmtm(cal, 'auc')} brier={_fmtm(cal, 'brier')} ece={_fmtm(cal, 'ece')}"
                )
                self._emit_log(
                    f"[AI] 라벨 요약 pos_rate={float(label_report.get('pos_rate', 0))*100:.1f}% "
                    f"n={label_report.get('n_samples','-')} "
                    f"net_ret_med={label_report.get('net_ret_p50','-')}"
                )

                final_model = calibrator
                calib_applied = True
                imps_src = base_m

                metrics_path = os.path.join(
                    os.path.dirname(str(self.ai_model_path)) or ".",
                    str(AI_METRICS_JSON_BASENAME),
                )
                _ai_write_calibration_json(metrics_path, metrics_report)
            else:
                if use_calib and len(X_val) > 0:
                    self._emit_log(
                        f"[AI] 검증분할·교정 생략→전량 학습 "
                        f"(n_tr={len(X_train)}, n_val={len(X_val)}, biclass_val={biclass_val}, "
                        f"need_tr>={train_merge_min} need_val>={cal_min})"
                    )
                Xm = X_train + X_val
                ym = y_train + y_val
                wm = w_train + w_val
                fitted = clone(model)
                try:
                    fitted.fit(Xm, ym, sample_weight=wm)
                except TypeError:
                    fitted.fit(Xm, ym)
                final_model = fitted
                imps_src = fitted
                metrics_report["fit_mode"] = "full_pool_no_calibration"
                self._emit_log(
                    f"[AI] 라벨 요약 pos_rate={(sum(ym)/max(1,len(ym))*100):.1f}% n={len(ym)}"
                )

                metrics_path = os.path.join(
                    os.path.dirname(str(self.ai_model_path)) or ".",
                    str(AI_METRICS_JSON_BASENAME),
                )
                _ai_write_calibration_json(metrics_path, metrics_report)

            _vm = metrics_report.get("val_metrics_calibrated") or metrics_report.get("val_metrics_raw") or {}
            try:
                new_auc: Optional[float] = float(_vm.get("auc")) if _vm.get("auc") is not None else None
                if new_auc is not None and not math.isfinite(new_auc):
                    new_auc = None
            except Exception:
                new_auc = None
            if self.ai_model is not None and (
                new_auc is None
                or new_auc < float(AI_REPLACE_MIN_AUC)
                or (prev_auc is not None and new_auc < float(prev_auc))
            ):
                if prev_metrics_txt is not None:
                    try:
                        with open(metrics_path, "w", encoding="utf-8") as _f:
                            _f.write(prev_metrics_txt)
                    except Exception:
                        pass
                self._emit_log(
                    f"[AI] 새 모델 교체 거부: val auc={new_auc if new_auc is None else round(new_auc, 4)} "
                    f"(기준≥{float(AI_REPLACE_MIN_AUC):.2f}, 기존={prev_auc if prev_auc is None else round(float(prev_auc), 4)}) "
                    f"— 기존 모델 유지 | {self.ai_status_line()}"
                )
                return

            self.ai_model = final_model
            self.ai_trained_date = today
            self.ai_model_name = str(model_name or "unknown")
            self.ai_model_samples = int(n_total)
            self.ai_model_loaded_from_disk = False
            self._ai_needs_retrain = False
            self._save_ai_model_to_disk(
                final_model,
                model_name,
                int(n_total),
                today,
                extra_payload={
                    "calibration_applied": calib_applied,
                    "sklearn_calibration_method": str(AI_CALIBRATION_METHOD).lower()
                    if calib_applied
                    else "",
                    "live_injected": int(live_sample_n),
                    "live_wins": int(live_win_n),
                },
            )
            self._load_ai_metrics_from_disk()
            self._emit_log(
                f"[AI] 학습 완료: {model_name} samples={int(n_total)} "
                f"label=비용반영 스탑/익절 {int(AI_LABEL_HORIZON_DAYS)}일 "
                f"calibrated={'yes' if calib_applied else 'no'} | {self.ai_status_line()}"
            )
            try:
                imps = list(getattr(imps_src, "feature_importances_", []))
                if imps:
                    pairs = list(zip(AI_FEATURE_NAMES, imps))
                    pairs.sort(key=lambda x: float(x[1]), reverse=True)
                    top = ", ".join([f"{k}:{v:.3f}" for k, v in pairs[:5]])
                    self._emit_log(f"[AI] feature importance top5 -> {top}")
            except Exception:
                pass
        except Exception as e:
            # 학습 실패 시 전략은 기술적 필터만 사용 (당일 재시도 가능하도록 날짜 미기록)
            self._emit_log(f"[AI] 학습 중 예외로 중단(메트릭 JSON·모델 저장 생략 가능): {e}")
            if self.ai_model is None:
                self._load_ai_model_from_disk(allow_stale=True)
            return

    def ai_status_line(self) -> str:
        if self.ai_model is None:
            if getattr(self, "_ai_train_attempted_date", None) == datetime.date.today():
                return "AI:학습실패/샘플부족"
            return "AI:미학습"
        src = "로드" if bool(self.ai_model_loaded_from_disk) else "학습"
        date_txt = self.ai_trained_date.isoformat() if self.ai_trained_date else "-"
        parts = [
            f"AI:{src}",
            str(self.ai_model_name or "-"),
            f"n={int(self.ai_model_samples)}",
            date_txt,
        ]
        auc = getattr(self, "ai_val_auc", None)
        if auc is not None:
            try:
                parts.append(f"auc={float(auc):.3f}")
            except Exception:
                pass
        ece = getattr(self, "ai_val_ece", None)
        if ece is not None:
            try:
                parts.append(f"ece={float(ece):.3f}")
            except Exception:
                pass
        inj = int(getattr(self, "ai_live_injected", 0) or 0)
        wins = int(getattr(self, "ai_live_wins", 0) or 0)
        if inj > 0:
            parts.append(f"live={wins}/{inj}")
        qf = float(self._ai_quality_size_factor())
        if qf < 0.999:
            parts.append(f"size×{qf:.2f}")
        return " ".join(parts)

    def _maybe_reset_daily_state(self, today: datetime.date) -> None:
        if self._last_ma_refresh_date == today:
            return

        self._last_ma_refresh_date = today
        self._need_portfolio_sync = True

        # 오늘 MA는 아직 다 갱신 전
        self._ma_needed_codes = set(self.stock_codes)
        self._ma_refresh_idx = 0
        self._next_ma_refresh_at = datetime.datetime.now()

    def _maybe_refresh_ma_step(self) -> None:
        if not self._ma_needed_codes:
            return
        if not self.stock_codes:
            self._ma_needed_codes.clear()
            return

        now = datetime.datetime.now()
        if now < self._next_ma_refresh_at:
            return

        self._next_ma_refresh_at = now + datetime.timedelta(seconds=MA_REFRESH_INTERVAL_SEC)

        # 다음 종목 여러 개를 상위 순서대로 처리(대기 300개+일 때 초기 판단 지연 완화)
        batch_n = max(1, int(MA_REFRESH_BATCH_PER_TICK))
        tried = 0
        total = max(1, len(self.stock_codes))
        while tried < batch_n and self._ma_needed_codes:
            code = self.stock_codes[self._ma_refresh_idx % total]
            self._ma_refresh_idx += 1
            tried += 1
            if code not in self._ma_needed_codes:
                continue
            try:
                ok = self.refresh_ma_for_code(code)
                if ok:
                    self._ma_needed_codes.discard(code)
            except Exception:
                # 실패해도 다음 종목부터 계속 진행
                pass

    def _positions_from_ledger(self) -> Optional[Dict[str, Dict[str, int]]]:
        """거래 CSV(FIFO)로 미청산 잔고를 재구성한다. 실패 시 None."""
        tracker = getattr(self, "trade_tracker", None)
        if tracker is None:
            return None
        try:
            ledger_map, _oversell = fifo_open_positions_from_records(tracker.all_records())
        except Exception:
            return None
        out: Dict[str, Dict[str, int]] = {}
        for code, lg in (ledger_map or {}).items():
            q = int(round(float((lg or {}).get("qty", 0.0) or 0.0)))
            if q <= 0:
                continue
            avg = int(round(float((lg or {}).get("avg_price", 0.0) or 0.0)))
            out[str(code).strip()] = {"qty": q, "avg_price": max(1, avg)}
        return out

    def _positions_brief(self, positions: Dict[str, Dict[str, int]]) -> str:
        hold_items = sorted(
            (
                (c, int((p or {}).get("qty", 0) or 0))
                for c, p in (positions or {}).items()
                if int((p or {}).get("qty", 0) or 0) > 0
            ),
            key=lambda x: x[0],
        )
        return ", ".join(f"{c}:{q}" for c, q in hold_items)

    def _commit_positions_snapshot(self, positions: Dict[str, Dict[str, int]], source: str) -> None:
        """잔고 스냅샷을 positions에 반영하고 후처리(로그·종목목록·RECONCILE)를 수행한다."""
        brief = self._positions_brief(positions)
        if brief != self._last_holdings_brief:
            self._emit_log(f"[HOLDINGS] {source} 보유수량 요약 {brief or '-'}")
            self._last_holdings_brief = brief
        self.positions = dict(positions or {})
        self.last_holdings_tr_rows = len(self.positions)
        if self.positions:
            merged_codes = list(
                dict.fromkeys(list(self.stock_codes or []) + list(self.positions.keys()))
            )
            self.stock_codes = merged_codes
            for c in merged_codes:
                self.pending_buy.setdefault(c, False)
                self.pending_sell.setdefault(c, False)
                self.tp_reached.setdefault(c, False)
        self._reconcile_holdings_with_trade_log()

    def _adjust_position_on_chejan_fill(self, code: str, side: str, qty: int, price: int) -> None:
        """체결 통보 직후 내부 positions를 즉시 갱신한다(TR 지연 시 GUI 유령잔고 방지)."""
        if qty <= 0 or price <= 0:
            return
        pos = dict(self.positions.get(code) or {"qty": 0, "avg_price": 0})
        old_q = int(pos.get("qty", 0) or 0)
        old_avg = int(pos.get("avg_price", 0) or 0)
        side_u = str(side).upper()
        if side_u == "BUY":
            new_q = old_q + int(qty)
            if old_q > 0 and old_avg > 0:
                new_avg = int(round((old_avg * old_q + int(price) * int(qty)) / float(new_q)))
            else:
                new_avg = int(price)
            self.positions[code] = {"qty": new_q, "avg_price": max(1, new_avg)}
            hold_q = new_q
        elif side_u == "SELL":
            new_q = max(0, old_q - int(qty))
            if new_q <= 0:
                self.positions.pop(code, None)
                self._scale_in_done[code] = False
                self._quality_avg_down_count.pop(code, None)
                self._quality_avg_down_at.pop(code, None)
                self._quality_entry_codes.discard(code)
            else:
                self.positions[code] = {"qty": new_q, "avg_price": old_avg}
            hold_q = new_q
        else:
            return
        self._emit_log(
            f"[POSITION] chejan {side_u} {code} fill={qty}@{price} -> hold={hold_q}"
        )

    def sync_portfolio(self, password: str = "") -> None:
        # opw00018이 실패할 경우를 대비해 예외 처리합니다.
        try:
            data = self.kiwoom.request_holdings(self.account, password=password)
            if data is None:
                ledger_pos = self._positions_from_ledger()
                if ledger_pos is not None:
                    if ledger_pos != (self.positions or {}):
                        self._commit_positions_snapshot(ledger_pos, "CSV원장(TR미응답)")
                    return
                now = datetime.datetime.now()
                should_warn = (
                    self._last_positions_sync_warn_at is None
                    or (now - self._last_positions_sync_warn_at).total_seconds() >= 300
                )
                if should_warn:
                    self._emit_log(
                        "[HOLDINGS] 잔고조회 TR 미응답(타임아웃 등). CSV 원장도 없어 기존 보유 유지."
                    )
                    self._last_positions_sync_warn_at = now
                return
            has_password = bool(str(password).strip())
            ledger_pos = self._positions_from_ledger()
            if isinstance(data, dict):
                normalized: Dict[str, Dict[str, int]] = {}
                for k, v in data.items():
                    key_raw = str(k or "").strip().lstrip("A")
                    # 계좌 TR 응답 코드 형식 차이를 흡수(예: A005930, 005930 ...)
                    key_digits = "".join(ch for ch in key_raw if ch.isdigit())
                    code6 = key_digits[-6:] if len(key_digits) >= 6 else key_raw
                    if len(code6) == 6 and code6.isdigit():
                        current_from_holdings = int((v or {}).get("current_price", 0) or 0)
                        normalized[code6] = {
                            "qty": int((v or {}).get("qty", 0) or 0),
                            "avg_price": int((v or {}).get("avg_price", 0) or 0),
                        }
                        if current_from_holdings > 0:
                            old_px = int(self.current_price.get(code6, 0) or 0)
                            self.current_price[code6] = current_from_holdings
                            self.current_price_source[code6] = "HOLDINGS_TR"
                            if old_px != current_from_holdings:
                                self._emit_log(
                                    f"[PRICE] 잔고TR 현재가 반영 {code6}: {old_px}->{current_from_holdings}"
                                )
                existing_positions = sum(
                    1 for p in (self.positions or {}).values()
                    if int((p or {}).get("qty", 0) or 0) > 0
                )

                if normalized:
                    self._holdings_empty_streak = 0
                    self._commit_positions_snapshot(normalized, f"TR(opw00018 {len(normalized)}행)")
                elif existing_positions == 0:
                    self._holdings_empty_streak = 0
                    self._commit_positions_snapshot({}, "TR(opw00018 0행)")
                else:
                    # TR 0건인데 앱에는 보유가 남아 있음 — 키움·CSV 교차 확인
                    trust_broker_empty = has_password
                    if not trust_broker_empty and ledger_pos is not None and len(ledger_pos) == 0:
                        trust_broker_empty = True
                    # 2026-07-09: 0건 응답이 일시적 TR 글리치일 수 있어(023910 사고), 비번이
                    # 설정된 경우에도 곧바로 신뢰하지 않고 연속 확인(HOLDINGS_EMPTY_CONFIRM_N회,
                    # 주기 60초)이 될 때까지는 기존 보유를 유지한다. CSV 원장이 이미 0건으로
                    # 확인되는 경우는 예외적으로 즉시 신뢰한다(더 강한 근거).
                    ledger_confirms_empty = ledger_pos is not None and len(ledger_pos) == 0
                    HOLDINGS_EMPTY_CONFIRM_N = 2
                    if trust_broker_empty and not ledger_confirms_empty:
                        self._holdings_empty_streak += 1
                        if self._holdings_empty_streak < HOLDINGS_EMPTY_CONFIRM_N:
                            self._emit_log(
                                f"[HOLDINGS] TR 0건(연속 {self._holdings_empty_streak}회) — 기존 보유 "
                                f"{existing_positions}종목 임시 유지(글리치 대비, {HOLDINGS_EMPTY_CONFIRM_N}회 연속 시 제거)"
                            )
                            return
                    if trust_broker_empty:
                        self._holdings_empty_streak = 0
                        self._emit_log(
                            f"[HOLDINGS] TR 0건 — 키움 보유 0주로 동기화 "
                            f"(기존 {existing_positions}종목 제거)"
                        )
                        self._commit_positions_snapshot({}, "TR 0건→0주")
                    elif ledger_pos is not None:
                        self._commit_positions_snapshot(ledger_pos, "CSV원장(TR 0건)")
                    else:
                        now = datetime.datetime.now()
                        should_warn = (
                            self._last_positions_sync_warn_at is None
                            or (now - self._last_positions_sync_warn_at).total_seconds() >= 300
                        )
                        if should_warn:
                            self._emit_log(
                                f"[HOLDINGS] TR 0건이나 기존 보유 {existing_positions}종목 유지 "
                                "(계좌비번 확인 또는 TR 일시 실패)"
                            )
                            self._last_positions_sync_warn_at = now
                        return
            else:
                data = {}
            # 비밀번호 미설정/조회 실패로 빈 응답이 오는 경우를 진단 로그로 남김(과다 방지: 5분 1회)
            if not data:
                now = datetime.datetime.now()
                should_warn = (
                    self._last_positions_sync_warn_at is None
                    or (now - self._last_positions_sync_warn_at).total_seconds() >= 300
                )
                if should_warn:
                    if has_password:
                        # TR이 정상적으로 0행을 준 경우(실제 무보유)와 비번 오류를 구분
                        ledger_n = len(ledger_pos or {})
                        self._emit_log(
                            f"[HOLDINGS] 키움 잔고 0종목(opw00018 정상·비번설정됨). "
                            f"CSV원장 오픈로트≈{ledger_n}종 — "
                            f"{'실보유 없음으로 해석' if ledger_n == 0 else 'CSV 유령보유와 불일치(RECONCILE 참고)'}"
                        )
                    else:
                        self._emit_log(
                            "[HOLDINGS] 잔고조회 결과 0건 (opw00018). 계좌비번=미설정(--pw 또는 KIWOOM_ACCOUNT_PASSWORD 필요). "
                            "보유종목이 있다면 앱 재시작 시 --pw 계좌비밀번호 인자를 추가하세요"
                        )
                    self._last_positions_sync_warn_at = now
        except Exception as e:
            # 로그인/잔고 동기화는 환경에 따라 필요 설정이 다를 수 있음.
            # 최소 동작을 위해 내부 포트폴리오는 유지합니다.
            now = datetime.datetime.now()
            should_warn = (
                self._last_positions_sync_warn_at is None
                or (now - self._last_positions_sync_warn_at).total_seconds() >= 300
            )
            if should_warn:
                self._emit_log(f"[HOLDINGS] 잔고조회 오류: {e}")
                self._last_positions_sync_warn_at = now
        finally:
            self._ingest_opw00018_account_summary()
            self._maybe_refresh_opw_eval_snapshot(password=str(password or ""))

    def _ingest_opw00018_account_summary(self) -> None:
        """opw00018 싱글데이터(추정예탁자산 등)를 엔진 상태에 반영."""
        kw = getattr(self, "kiwoom", None)
        summary = getattr(kw, "last_opw00018_summary", None) if kw is not None else None
        if not isinstance(summary, dict) or not summary:
            return
        now = datetime.datetime.now()
        if "추정예탁자산" in summary and int(summary["추정예탁자산"]) > 0:
            self.opw_estimated_deposit_krw = int(summary["추정예탁자산"])
            self.opw_deposit_fetched_at = now
        # 총평가손익금액이 있으면 보유평가손익 보조 지표로 저장
        for key in ("총평가손익금액", "손익금액"):
            if key in summary:
                self.opw_holdings_eval_pnl_krw = int(summary[key])
                break

    def _maybe_refresh_opw_eval_snapshot(self, password: str = "") -> None:
        """
        opw00004 계좌평가현황(TR)으로 당일/누적 투자손익 등을 채운다.
        잔고 TR 실패 여부와 무관히 주기 호출 가능(약 120s 간격).
        """
        now = datetime.datetime.now()
        interval = 120.0
        la = getattr(self, "opw_eval_fetched_at", None)
        if la is not None and (now - la).total_seconds() < interval:
            return
        kw = getattr(self, "kiwoom", None)
        fn = getattr(kw, "request_account_eval_summary", None) if kw is not None else None
        if not callable(fn):
            return
        try:
            res = fn(self.account, password=str(password or ""))
        except Exception:
            return
        if not isinstance(res, dict) or not res:
            return
        self.opw_eval_fetched_at = now
        if "당일투자손익" in res:
            self.opw_daily_invest_pnl_krw = int(res["당일투자손익"])
        if "당월투자손익" in res:
            self.opw_month_invest_pnl_krw = int(res["당월투자손익"])
        if "누적투자손익" in res:
            self.opw_accum_invest_profit_loss_krw = int(res["누적투자손익"])
        # 싱글필드 "손익금액"은 종목별 합 평가손익에 가까운 계열(증광 표기와 명칭이 다를 수 있음)
        if "손익금액" in res:
            self.opw_holdings_eval_pnl_krw = int(res["손익금액"])
        # opw00004에도 추정예탁/예탁자산평가액이 오는 환경이 있음 → GUI·예산 동기화
        for key in ("추정예탁자산", "예탁자산평가액"):
            if key in res and int(res[key]) > 0:
                self.opw_estimated_deposit_krw = int(res[key])
                self.opw_deposit_fetched_at = now
                break

    def _reconcile_holdings_with_trade_log(self) -> None:
        """opw00018 잔고(self.positions)와 TradeTracker CSV(FIFO 재구성) 불일치를 검사한다."""
        tracker = getattr(self, "trade_tracker", None)
        if tracker is None:
            return
        try:
            recs = tracker.all_records()
            ledger_map, oversell = fifo_open_positions_from_records(recs)
        except Exception:
            return

        broker: Dict[str, Dict[str, int]] = {}
        for c, p in (self.positions or {}).items():
            q = int((p or {}).get("qty", 0) or 0)
            if q <= 0:
                continue
            broker[str(c)] = {
                "qty": q,
                "avg_price": int((p or {}).get("avg_price", 0) or 0),
            }

        issues: List[str] = []
        for c in sorted(oversell.keys()):
            ov = int(oversell.get(c, 0) or 0)
            if ov > 0:
                issues.append(f"{c}:CSV매도과다 qty={ov}")

        all_codes = sorted(set(broker.keys()) | set(ledger_map.keys()))
        tol_rel = float(RECONCILE_AVG_REL_TOL)
        tol_abs = float(RECONCILE_AVG_ABS_MIN_KRW)

        for code in all_codes:
            b = broker.get(code)
            lg = ledger_map.get(code)
            bq = int(b["qty"]) if b else 0
            lq_float = float((lg or {}).get("qty", 0.0)) if lg else 0.0
            lq = int(round(lq_float))
            if bq != lq:
                issues.append(f"{code}:qty broker={bq} ledger={lq}")
                continue
            if bq > 0 and b is not None and lg is not None:
                bp = int(b.get("avg_price", 0) or 0)
                lp = float(lg.get("avg_price", 0.0) or 0.0)
                if bp > 0 and lp > 0:
                    diff = abs(float(bp) - lp)
                    if diff >= tol_abs and (diff / max(float(bp), 1.0)) > tol_rel:
                        issues.append(
                            f"{code}:avg broker={bp} ledger={lp:.1f}",
                        )

        if not issues:
            prev_sig = self._last_reconcile_issue_sig
            self._last_reconcile_issue_sig = ""
            self._last_reconcile_issue_at = None
            if prev_sig:
                self._emit_log("[RECONCILE] 불일치 해소 — 현재 잔고·거래CSV 일치")
            return

        sig = "|".join(issues)
        now = datetime.datetime.now()
        last_at = self._last_reconcile_issue_at
        if (
            sig == self._last_reconcile_issue_sig
            and last_at is not None
            and (now - last_at).total_seconds() < float(RECONCILE_ISSUE_COOLDOWN_SEC)
        ):
            return
        self._last_reconcile_issue_sig = sig
        self._last_reconcile_issue_at = now
        preview = "; ".join(issues[:12])
        extra = "" if len(issues) <= 12 else f" 외 {len(issues)-12}건"
        msg = f"[RECONCILE] 잔고≠CSV ({preview}{extra})"
        self._emit_log(msg)
        self._append_order_event(
            event="RECONCILE_MISMATCH",
            code="",
            side="",
            qty=0,
            price=0.0,
            note=msg[:1600],
        )

    def _sync_holding_current_prices(self, interval_sec: float = 10.0) -> None:
        """보유 종목 현재가는 yfinance/전일종가 대신 키움 현재가 TR로 주기 보정."""
        now = datetime.datetime.now()
        if (
            self._last_holdings_price_sync_at is not None
            and (now - self._last_holdings_price_sync_at).total_seconds() < float(interval_sec)
        ):
            return
        held = [
            c
            for c, p in (self.positions or {}).items()
            if int((p or {}).get("qty", 0) or 0) > 0
        ]
        if not held:
            return
        self._last_holdings_price_sync_at = now
        changed: List[str] = []
        for code in held:
            try:
                px = int(self.kiwoom.request_current_price(code, timeout_ms=2500) or 0)
            except Exception:
                px = 0
            if px <= 0:
                continue
            old = int(self.current_price.get(code, 0) or 0)
            self.current_price[code] = px
            self.current_price_source[code] = "KIWOOM_TR"
            if old != px:
                changed.append(f"{code}:{old}->{px}")
        brief = ", ".join(changed[:5])
        if brief and brief != self._last_holdings_price_brief:
            extra = "" if len(changed) <= 5 else f" 외 {len(changed)-5}종목"
            self._emit_log(f"[PRICE] 보유 현재가 키움TR 보정 {brief}{extra}")
            self._last_holdings_price_brief = brief

    def _maybe_emit_buy_diagnostics(self) -> None:
        """장중 주기적으로 매수 후보·차단 사유·국면 요약."""
        if not self._is_market_open():
            return
        now = datetime.datetime.now()
        if (
            self._last_buy_diag_at is not None
            and (now - self._last_buy_diag_at).total_seconds() < float(BUY_DIAG_INTERVAL_SEC)
        ):
            return
        self._last_buy_diag_at = now
        n_targets = len(list(self.stock_codes or []))
        n_ma = sum(1 for c in (self.stock_codes or []) if bool(self.ma.get(c)))
        candidates = self._ranked_buy_candidates()
        regime = self._market_regime_label() or "미평가"
        sm = self.strategy_manager
        active = getattr(sm, "active_strategy_id", "-") if sm is not None else "-"
        sample = (self.stock_codes[0] if self.stock_codes else "")
        entry_rule = self._effective_buy_strategy_id(sample) if sample else active
        relax = "ON" if self._no_trade_relax_active() else "off"
        bull_relax = "ON" if self._bull_entry_relax_active() else "off"
        guard = "ON" if self._in_market_open_buy_guard() else "off"
        ai_tag = "학습됨" if self.ai_model is not None else "미학습(기술만)"
        top_blocks = sorted(
            self._buy_block_stats.items(), key=lambda x: x[1], reverse=True
        )[:6]
        block_txt = ", ".join(f"{k}:{v}" for k, v in top_blocks) if top_blocks else "없음"
        self._emit_log(
            f"[BUY-DIAG] 대상 {n_targets}종 MA준비 {n_ma} | 후보 {len(candidates)} | "
            f"시장={regime} 활성전략={active} 진입규칙={entry_rule} AI={ai_tag} | "
            f"AI문턱={self._dynamic_ai_entry_min():.2f} score≥{self._effective_buy_score_min():.2f} "
            f"동시보유한도={self._effective_max_positions()} 최근손절한도={self._effective_recent_loss_block_count()} | "
            f"완화={relax} bull완화={bull_relax} "
            f"모의유지={'ON' if self._mock_keep_trading_active() else 'off'} "
            f"장초금지={guard} | 차단상위: {block_txt}"
        )
        if candidates:
            preview = ", ".join(f"{c}({s:.2f})" for s, c in candidates[:3])
            self._emit_log(f"[BUY-DIAG] 상위후보: {preview}")
        # 기능 동작 요약(5분마다 BUY-DIAG와 함께 1줄)
        try:
            q_n = len(self._quality_mcap_allowlist or set())
            avg_n = sum(1 for _c, n in (self._quality_avg_down_count or {}).items() if int(n or 0) > 0)
            theme_txt = "-"
            if NEWS_SENTIMENT_AVAILABLE and bool(_THEME_STRENGTH_ENABLED):
                theme_txt = _theme_strength_summary()
                if len(theme_txt) > 90:
                    theme_txt = theme_txt[:87] + "..."
            self._emit_log(
                f"[FEATURE] 우량허용={q_n} 물타기중={avg_n} "
                f"테마={theme_txt}"
            )
        except Exception:
            pass

    def _should_buy_common(self, code: str) -> bool:
        # 2단계: 세션/국면 하드 차단 (AI 확률 통과와 무관)
        session = self.market_session_name()
        if bool(BLOCK_BUY_IN_PREMARKET) and session == "프리마켓":
            self._log_buy_block(code, "프리마켓 신규매수 금지", interval_sec=300.0)
            return False
        if bool(BLOCK_BUY_IN_AFTER_HOURS) and session == "애프터마켓":
            self._log_buy_block(code, "애프터마켓 신규매수 금지", interval_sec=300.0)
            return False
        halt_reason = self._market_halt_buy_block_reason()
        if halt_reason:
            self._log_buy_block(code, halt_reason, interval_sec=300.0)
            return False
        state_reason = self._stock_state_block_reason(code)
        if state_reason:
            self._log_buy_block(code, state_reason, interval_sec=1800.0)
            return False
        vi_reason = self._vi_buy_block_reason(code)
        if vi_reason:
            self._log_buy_block(code, vi_reason, interval_sec=120.0)
            return False
        if bool(BLOCK_BUY_IN_BEAR) and self._is_market_weak():
            # 지수 확정 bear만 하드블록. composite 약세는 편향 가능해 통과시키고
            # 기존 MARKET_WEAK_AI_ENTRY_ADD / 비중축소 경로로만 보수화한다.
            # 모의만 예외: 전면금지 대신 눌림(quality_dip) 필터로 넘긴다.
            # 우량 눌림: parity여도 하드블록 예외(적극 매수).
            if self._mock_bear_quality_dip_active() or self._quality_dip_bear_exception(code):
                pass
            elif (not bool(BLOCK_BUY_IN_BEAR_REQUIRE_INDEX)) or self._regime_source_is_index():
                self._log_buy_block(
                    code,
                    f"약세장(bear) 신규매수 금지 src={self._market_regime_source() or '-'}",
                    interval_sec=300.0,
                )
                return False
            # composite bear: 하드블록 생략 (자동 완화)
            pass
        if self._in_market_open_buy_guard():
            self._log_buy_block(
                code,
                f"장초 변동성 구간 매수금지(09:00~09:{self._effective_market_open_guard_minutes():02d})",
                interval_sec=300.0,
            )
            return False
        # 4단계: 종목 뉴스 매우 부정 시 차단
        if bool(DAY_BIAS_ENABLED) and NEWS_SENTIMENT_AVAILABLE:
            try:
                ns = float(get_news_sentiment(code))
                if ns <= float(NEWS_CODE_BLOCK_THRESHOLD):
                    self._log_buy_block(
                        code,
                        f"뉴스감성 매우부정({ns:+.2f}≤{float(NEWS_CODE_BLOCK_THRESHOLD):+.2f})",
                        interval_sec=300.0,
                    )
                    return False
            except Exception:
                pass
        if code in self.positions and self.positions[code].get("qty", 0) > 0:
            self._log_buy_block(code, "이미 보유중")
            return False
        if self.pending_buy.get(code, False):
            self._log_buy_block(code, "매수 미체결 진행중")
            return False
        pending_order = (self.pending_orders or {}).get(code, {}) or {}
        if str(pending_order.get("side", "")).lower() == "buy":
            self._log_buy_block(code, "매수 주문 확인 대기")
            return False
        cooldown_until = self.buy_cooldown_until.get(code)
        if isinstance(cooldown_until, datetime.datetime) and datetime.datetime.now() < cooldown_until:
            remain = int((cooldown_until - datetime.datetime.now()).total_seconds())
            self._log_buy_block(code, f"매수 timeout 쿨다운({remain}s)")
            return False
        # 손절 후 재진입: 현재가가 손절 체결가를 회복해야 재매수 허용
        if code in self._reentry_codes:
            stop_px = self._stop_loss_prices.get(code, 0.0)
            cur_px = float(self.current_price.get(code) or 0.0)
            if stop_px > 0 and cur_px > 0 and cur_px <= stop_px:
                self._log_buy_block(code, f"손절가 미회복(현재{int(cur_px)} ≤ 손절{int(stop_px)})")
                return False
        today_loss_pct = self._today_realized_pnl_pct()
        if today_loss_pct <= float(DAILY_LOSS_HARD_LIMIT_PCT):
            self._log_buy_block(code, f"당일 손실한도 초과({today_loss_pct*100:.2f}%)")
            return False
        ai_loss_exits = self._today_sell_reason_count("ai_loss_exit")
        if ai_loss_exits >= int(DAILY_AI_LOSS_EXIT_BLOCK_COUNT):
            self._log_buy_block(
                code,
                f"당일 AI 손실청산 반복({ai_loss_exits}/{int(DAILY_AI_LOSS_EXIT_BLOCK_COUNT)})",
            )
            return False
        today_sells, today_wins = self._today_sell_stats()
        if today_sells >= int(DAILY_WIN_RATE_BLOCK_MIN_SELLS):
            today_win_rate = today_wins / float(max(1, today_sells))
            if today_win_rate < float(DAILY_WIN_RATE_BLOCK_PCT):
                self._log_buy_block(
                    code,
                    f"당일 승률 저조({today_wins}/{today_sells}, {today_win_rate*100:.1f}%)",
                )
                return False
        bt_mode = self._auto_bt_mode()
        if bool(AUTO_BT_BLOCK_BUY) and bt_mode == "block":
            self._log_buy_block(code, "자동백테스트 게이트 차단")
            return False
        recent_losses = self._recent_loss_count_for_code(code)
        effective_recent_loss_limit = self._effective_recent_loss_block_count()
        if recent_losses >= effective_recent_loss_limit:
            self._log_buy_block(
                code,
                f"최근 손절 반복({recent_losses}/{effective_recent_loss_limit}회, {int(RECENT_LOSS_LOOKBACK_DAYS)}일)",
            )
            return False
        cur = self.current_price.get(code)
        if cur is None:
            self._log_buy_block(code, "현재가 미수신")
            return False
        if float(cur) < float(MIN_ENTRY_PRICE):
            self._log_buy_block(code, f"매수가격 하한 미달({int(cur)}<{int(MIN_ENTRY_PRICE)})")
            return False
        if float(cur) > float(MAX_ENTRY_PRICE):
            self._log_buy_block(code, f"매수가격 상한 초과({int(cur)}>{int(MAX_ENTRY_PRICE)})")
            return False
        # 당일 재매수 허용 — 손절 후 반등 시 재진입 가능 (쿨다운은 STOP_LOSS_REENTRY_COOLDOWN_SEC로 제어)
        if not self.ma.get(code):
            self._log_buy_block(code, "MA 데이터 없음")
            return False
        if bool(USE_ENTRY_ATR_GATE) and float(ENTRY_ATR_MAX_PCT) > 0:
            atr_pct = float((self.ma.get(code) or {}).get("atr_pct", 0.0) or 0.0)
            if atr_pct > float(ENTRY_ATR_MAX_PCT):
                self._log_buy_block(
                    code,
                    f"진입 ATR 상한 초과({atr_pct * 100.0:.2f}%>{float(ENTRY_ATR_MAX_PCT) * 100.0:.2f}%)",
                )
                return False
        active_positions = sum(1 for _c, p in self.positions.items() if p.get("qty", 0) > 0)
        effective_limit = self._effective_max_positions()
        if active_positions >= effective_limit:
            self._log_buy_block(
                code,
                f"동시보유 한도 도달({active_positions}/{int(effective_limit)})",
            )
            return False
        return True

    def _ai_buy_filter(self, code: str, cur: float) -> bool:
        if self.ai_model is None:
            if not self._ai_model_missing_warned:
                self._ai_model_missing_warned = True
                self._emit_log("[AI] 모델 미학습 — 확률 필터 스킵, 기술적 조건만 적용")
            return self._ai_buy_filter_soft(code, cur)
        x = self._ai_features_from_cached(code, cur)
        if x is None:
            return False
        try:
            proba_up = float(self.ai_model.predict_proba([x])[0][1])
            return proba_up >= self._dynamic_ai_entry_min()
        except Exception:
            return False

    def _dynamic_ai_entry_min_soft(self) -> float:
        base = float(self._dynamic_ai_entry_min()) + float(AI_TECH_STRATEGY_ENTRY_DELTA)
        floor = float(AI_TECH_STRATEGY_ENTRY_FLOOR)
        if floor > 0.0:
            thr = max(floor, base)
        else:
            thr = max(0.0, min(0.95, base))
        # json 0.50 하드문턱은 유지. 모의 trend/volume/약세눌림 소프트만 상한.
        if self._mock_ai_soft_cap_active():
            return min(thr, float(MOCK_KEEP_AI_SOFT_CAP))
        return thr

    def _trend_ai_entry_thr(self) -> float:
        return max(0.50, min(float(self._dynamic_ai_entry_min_soft()), float(TREND_AI_SOFT_THR_CAP)))

    def _ai_buy_filter_soft(self, code: str, cur: float, thr_override: Optional[float] = None) -> bool:
        """
        트렌드/거량 전용: 진입 분류기를 AI 전략보다 낮은 문턱으로만 적용.
        모델이 없거나 특성 미구성 시 기술적 조건만으로 진행(True).
        """
        if not bool(AI_SOFT_FILTER_TECH_STRATEGIES):
            return True
        if self.ai_model is None:
            return True
        x = self._ai_features_from_cached(code, cur)
        if x is None:
            return True
        try:
            proba_up = float(self.ai_model.predict_proba([x])[0][1])
            thr = float(thr_override) if thr_override is not None else float(self._dynamic_ai_entry_min_soft())
            return proba_up >= thr
        except Exception:
            return True

    def _should_buy_volume_weak_regime(
        self,
        code: str,
        v: Dict[str, float],
        cur: float,
        fail: Callable[[str], bool],
    ) -> bool:
        """
        하락/횡보·국면 미평가 시 volume 완화 규칙.
        RSI 상승 필수·MA5>MA20 필수를 완화해 후보 종목을 확보한다.
        """
        ma20 = float(v.get("ma20", 0.0) or 0.0)
        if ma20 <= 0:
            return fail("MA20 없음")
        gap_pct = float(VOLUME_WEAK_MA20_ENTRY_GAP_PCT)
        if cur < ma20 * (1.0 + gap_pct):
            return fail("MA20 대비 너무 낮음")
        price_to_ma20 = cur / ma20 - 1.0
        if price_to_ma20 > max(float(MAX_PRICE_TO_MA20_PCT), 0.32):
            return fail("MA20 대비 과열")
        rsi14 = float(v.get("rsi14", 0.0) or 0.0)
        if rsi14 < 26.0 or rsi14 > 76.0:
            return fail("RSI 범위 이탈")
        vol_ratio = float(v.get("vol_ratio", 0.0) or 0.0)
        vol_today = int(v.get("vol_today", 0) or 0)
        vol_prev = int(v.get("vol_prev", 0) or 0)
        # 실전/약세: 기존 0.70 AND 0.88. 모의 유지매매만 실효 VOL·OR.
        if self._mock_keep_trading_active():
            ratio_ok = vol_ratio >= self._effective_vol_entry_ratio_min()
            growth_ok = (vol_prev <= 0) or (
                vol_today >= int(vol_prev * self._effective_vol_entry_growth_min())
            )
            if not (ratio_ok or growth_ok):
                return fail("거래량 평균비·전일대비 모두 부족")
        else:
            ratio_min, growth_min = 0.70, 0.88
            if self._vol_floor_exempt(code):
                ratio_min = min(ratio_min, self._effective_vol_entry_ratio_min(code))
                growth_min = min(growth_min, self._effective_vol_entry_growth_min(code))
            if vol_ratio < ratio_min:
                return fail("거래량 평균비 부족")
            if vol_prev > 0 and vol_today < int(vol_prev * growth_min):
                return fail("전일대비 거래량 부족")
        if not self._ai_buy_filter_soft(code, cur):
            return fail(f"AI 확률 부족(완화 thr={self._dynamic_ai_entry_min_soft():.2f})")
        return True

    def _should_buy_for_strategy(
        self, code: str, v: Dict[str, float], cur: float, strategy_id: str
    ) -> bool:
        ma5 = v["ma5"]
        ma20 = v["ma20"]
        ma5_prev = v.get("ma5_prev", ma5)
        ma20_prev = v.get("ma20_prev", ma20)
        rsi14 = float(v.get("rsi14", 0.0))
        rsi14_prev = float(v.get("rsi14_prev", rsi14))
        vol_ratio = float(v.get("vol_ratio", 0.0))
        vol_today = int(v.get("vol_today", 0))
        vol_prev = int(v.get("vol_prev", 0))

        def fail(reason: str) -> bool:
            self._log_buy_block(
                code,
                f"전략조건 미충족({strategy_id}: {reason})",
                interval_sec=90.0,
            )
            return False

        # 시총상위 스캔 슬롯 + 허용종목: 추세형 MA20 게이트 대신 눌림 블록만 적용
        if strategy_id == STRATEGY_QUALITY_DIP:
            if ma20 <= 0:
                return fail("(눌림) MA20 없음")
            rel = cur / ma20 - 1.0
            if rel > float(QDIP_MAX_PRICE_ABOVE_MA20_PCT):
                return fail("(눌림) MA20 대비 과열·추격")
            if rel < float(QDIP_MIN_PRICE_VS_MA20_PCT):
                return fail("(눌림) MA20 대비 과도 하락")
            if rsi14 > float(QDIP_RSI_MAX_ENTRY) or rsi14 < float(QDIP_RSI_MIN_ENTRY):
                return fail("(눌림) RSI 구간 불가")
            if rsi14 < rsi14_prev - float(QDIP_RSI_DROP_MAX_TOLERANCE):
                return fail("(눌림) RSI 급하락")
            if self._mock_bear_quality_dip_active():
                ratio_ok = vol_ratio >= min(
                    float(QDIP_VOL_RATIO_MIN), self._effective_vol_entry_ratio_min()
                )
                growth_ok = (vol_prev <= 0) or (
                    vol_today >= int(vol_prev * min(
                        float(QDIP_VOL_DAY_GROWTH_MIN),
                        self._effective_vol_entry_growth_min(),
                    ))
                )
                if not (ratio_ok or growth_ok):
                    return fail("(눌림) 거래량 평균비·전일대비 모두 부족")
            else:
                if vol_ratio < float(QDIP_VOL_RATIO_MIN):
                    return fail("(눌림) 거래량 평균비 부족")
                if vol_prev > 0 and vol_today < int(vol_prev * float(QDIP_VOL_DAY_GROWTH_MIN)):
                    return fail("(눌림) 전일대비 거래량 부족")
            if bool(QDIP_REQUIRE_MA5_GT_MA20) and not (ma5 > ma20):
                return fail("(눌림) MA5<=MA20")
            # 우량 눌림: AI soft 문턱을 QUALITY_AI_THR_RELAX 만큼 완화(학습보다 진입 우선)
            if bool(AI_SOFT_FILTER_TECH_STRATEGIES) and self.ai_model is not None:
                proba = self._ai_proba_up(code, cur)
                soft_thr = max(
                    0.50,
                    float(self._dynamic_ai_entry_min_soft()) - max(0.0, float(QUALITY_AI_THR_RELAX)),
                )
                if proba is not None and float(proba) < soft_thr:
                    return fail(f"(눌림) AI 확률 부족(thr={soft_thr:.2f})")
            return True

        if strategy_id == STRATEGY_VOLUME and self._is_weak_or_unknown_regime():
            return self._should_buy_volume_weak_regime(code, v, cur, fail)

        # trend는 뒤에서 자체 MA20 게이트 처리
        if strategy_id not in (STRATEGY_TREND,):
            gap_pct = float(MA20_ENTRY_GAP_PCT)
            if strategy_id == STRATEGY_VOLUME:
                gap_pct = float(VOLUME_MA20_ENTRY_GAP_PCT)
            if cur < ma20 * (1.0 + gap_pct):
                return fail("MA20 대비 너무 낮음")
            price_to_ma20 = cur / ma20 - 1.0 if ma20 > 0 else 0.0
            if price_to_ma20 > MAX_PRICE_TO_MA20_PCT:
                return fail("MA20 대비 과열")

        if strategy_id == STRATEGY_VOLUME:
            if not (ma5 > ma20) and not self._allow_volume_weak_ma_entry(v, cur):
                return fail("MA5<=MA20")
            if rsi14 < (RSI_ENTRY_MIN - 3.0) or rsi14 > (RSI_ENTRY_MAX + 2.0):
                return fail("RSI 범위 이탈")
            if rsi14 <= rsi14_prev:
                return fail("RSI 상승 아님")
            if (rsi14 - rsi14_prev) < (RSI_MIN_DELTA * 0.5):
                return fail("RSI 상승폭 부족")
            vol_ratio_min = self._effective_vol_entry_ratio_min(code) * 1.08
            g_min = max(self._effective_vol_entry_growth_min(code), 0.96)
            ratio_ok = vol_ratio >= vol_ratio_min
            growth_ok = (vol_prev <= 0) or (vol_today >= int(vol_prev * g_min))
            if self._mock_keep_trading_active():
                if not (ratio_ok or growth_ok):
                    return fail("거래량 평균비·전일대비 모두 부족")
            else:
                if not ratio_ok:
                    return fail("거래량 평균비 부족")
                if not growth_ok:
                    return fail("전일대비 거래량 부족")
            if not self._ai_buy_filter_soft(code, cur):
                return fail(f"AI 상승확률 부족(거량·완화 thr={self._dynamic_ai_entry_min_soft():.2f})")
            return True

        if strategy_id == STRATEGY_TREND:
            # 상승장 조정 국면: MA20 -10% 이상이면 허용 (대부분 종목이 MA20 아래)
            if ma20 > 0 and cur < ma20 * (1.0 - 0.15):
                return fail("MA20 대비 과도 하락(trend:-15%)")
            # MA5<=MA20 조건 제거: 상승장 조정 시 MA5가 MA20 아래인 것은 정상
            if rsi14 < (RSI_ENTRY_MIN - 8.0) or rsi14 > (RSI_ENTRY_MAX + 8.0):
                return fail("RSI 범위 이탈")
            # RSI 40 이상이면 하락 중이어도 허용 (조정 후 반등 포착)
            if rsi14 < rsi14_prev and rsi14 < 40.0:
                return fail("RSI 하락·약세(40 미만)")
            trend_vol_min = self._effective_vol_entry_ratio_min(code) * (
                float(BULL_TREND_VOL_MULT) if self._bull_entry_relax_active() else 0.65
            )
            ratio_ok = vol_ratio >= trend_vol_min
            if self._mock_keep_trading_active():
                growth_ok = (vol_prev <= 0) or (
                    vol_today >= int(vol_prev * self._effective_vol_entry_growth_min(code))
                )
                if not (ratio_ok or growth_ok):
                    return fail("거래량 평균비·전일대비 모두 부족")
            elif not ratio_ok:
                return fail("거래량 평균비 부족")
            trend_thr = self._trend_ai_entry_thr()
            if not self._ai_buy_filter_soft(code, cur, thr_override=trend_thr):
                return fail(f"AI 상승확률 부족(트렌드·완화 thr={trend_thr:.2f})")
            return True

        # STRATEGY_AI: 상승추세·RSI·거래량·RF 확률
        if not (ma5 > ma20):
            return fail("MA5<=MA20")
        if rsi14 < RSI_ENTRY_MIN or rsi14 > RSI_ENTRY_MAX:
            return fail("RSI 범위 이탈")
        if rsi14 <= rsi14_prev:
            return fail("RSI 상승 아님")
        if (rsi14 - rsi14_prev) < RSI_MIN_DELTA:
            return fail("RSI 상승폭 부족")
        ratio_ok = vol_ratio >= self._effective_vol_entry_ratio_min(code)
        growth_ok = (vol_prev <= 0) or (
            vol_today >= int(vol_prev * self._effective_vol_entry_growth_min(code))
        )
        if self._mock_keep_trading_active():
            if not (ratio_ok or growth_ok):
                return fail("거래량 평균비·전일대비 모두 부족")
        else:
            if not ratio_ok:
                return fail("거래량 평균비 부족")
            if not growth_ok:
                return fail("전일대비 거래량 부족")
        if not self._ai_buy_filter_soft(code, cur):
            return fail(f"AI 상승확률 부족(thr={self._dynamic_ai_entry_min_soft():.2f})")
        return True

    def _should_buy(self, code: str) -> bool:
        if not self._should_buy_common(code):
            return False
        v = self.ma[code]
        cur = float(self.current_price[code])
        sid = self._effective_buy_strategy_id(code)
        ok = self._should_buy_for_strategy(code, v, cur, sid)
        return ok

    def _sell_reason(self, code: str) -> str:
        pos = self.positions.get(code)
        if not pos or pos.get("qty", 0) <= 0:
            return ""
        if self.pending_sell.get(code, False):
            return ""
        cooldown_until = self.sell_cooldown_until.get(code)
        if isinstance(cooldown_until, datetime.datetime) and datetime.datetime.now() < cooldown_until:
            return ""

        cur = self.current_price.get(code)
        if cur is None:
            return ""

        avg = pos["avg_price"]
        stop_pct, take_pct = self._risk_pcts_for_code(code)
        stop_price = avg * (1.0 + stop_pct)
        take_price = avg * (1.0 + take_pct)
        entry_at = self.entry_time.get(code)
        hold_sec = (
            (datetime.datetime.now() - entry_at).total_seconds()
            if isinstance(entry_at, datetime.datetime)
            else float("inf")
        )
        ai_exit_allowed = hold_sec >= float(AI_EXIT_MIN_HOLD_SEC)

        # 기본 손절(기술적 규칙 우선)
        if cur <= stop_price:
            return "stop_loss"
        if self._is_market_weak() and ai_exit_allowed and cur <= avg * (1.0 + float(MARKET_WEAK_EXIT_LOSS_PCT)):
            return "market_weak_exit"

        # AI 기반 매도(손실방지/조기 익절)
        proba_up = self._ai_proba_up(code, float(cur))

        # 손실 구간: 의미 있는 손실 이후에만 AI 하락 우세를 조기 정리 신호로 사용
        if ai_exit_allowed and proba_up is not None and cur <= avg * (1.0 + AI_LOSS_EXIT_MIN_LOSS_PCT):
            if proba_up < AI_PROBA_EXIT_MAX:
                return "ai_loss_exit"

        # 조기 익절(너무 일찍 손익반전 방지)
        if (
            ai_exit_allowed
            and proba_up is not None
            and cur >= avg * (1.0 + AI_EARLY_EXIT_MIN_PROFIT_PCT)
        ):
            if proba_up < AI_PROBA_EXIT_MAX:
                return "ai_profit_exit"

        # 목표 익절 도달 이후 트레일링 스탑으로 수익을 더 끌어올리기
        if USE_TRAIL_AFTER_TP:
            if not self.tp_reached.get(code, False):
                if cur >= take_price:
                    self.tp_reached[code] = True
                    # 2주 이상이면 일부 익절 후 잔여분만 트레일링, 1주는 목표가에서 즉시 실현.
                    if bool(PARTIAL_TAKE_PROFIT_ENABLED) and int(pos.get("qty", 0) or 0) > 1:
                        return "partial_take_profit"
                    return "take_profit"
                return ""

            # tp_reached 이후: 최소 이익(TRAIL_LOCK_PCT) 또는 MA20 중 더 보수적인 가격으로 방어
            v = self.ma.get(code, {})
            ma20 = v.get("ma20", avg * (1.0 + TRAIL_LOCK_PCT))
            # factor 적용(현재는 1.0 기본값)
            trailing_stop = max(avg * (1.0 + TRAIL_LOCK_PCT), ma20 * TRAIL_MA20_FACTOR)
            # 트레일링 스탑 방어 + AI가 하락우세면 더 일찍 청산
            if ai_exit_allowed and proba_up is not None and proba_up < AI_PROBA_EXIT_MAX:
                return "ai_trailing_exit"
            return "trailing_stop" if cur <= trailing_stop else ""

        # 트레일링 미사용이면 목표 익절% 즉시 청산
        return "take_profit" if cur >= take_price else ""

    def _should_sell(self, code: str) -> bool:
        return bool(self._sell_reason(code))

    def _should_scale_in(self, code: str) -> bool:
        if not bool(SCALE_IN_ENABLED) or self._scale_in_done.get(code, False):
            return False
        if self.pending_buy.get(code, False) or self.pending_sell.get(code, False):
            return False
        pending_order = (self.pending_orders or {}).get(code, {}) or {}
        if str(pending_order.get("side", "")).lower() in ("buy", "sell"):
            return False
        pos = self.positions.get(code) or {}
        qty = int(pos.get("qty", 0) or 0)
        avg = float(pos.get("avg_price", 0) or 0)
        cur = float(self.current_price.get(code, 0) or 0)
        if qty <= 0 or avg <= 0 or cur <= 0:
            return False
        if self._today_realized_pnl_pct() <= float(DAILY_LOSS_SOFT_LIMIT_PCT):
            return False
        if cur < avg * (1.0 + float(SCALE_IN_MIN_PROFIT_PCT)):
            return False
        v = self.ma.get(code) or {}
        if not (float(v.get("ma5", 0.0) or 0.0) > float(v.get("ma20", 0.0) or 0.0)):
            return False
        max_budget = self._dynamic_total_budget_krw() * float(MAX_POSITION_BUDGET_PCT) * float(SCALE_IN_MAX_POSITION_BUDGET_FACTOR)
        current_budget = avg * float(qty)
        return current_budget < max_budget

    def _scale_in_order_qty(self, code: str) -> int:
        pos = self.positions.get(code) or {}
        qty = int(pos.get("qty", 0) or 0)
        avg = float(pos.get("avg_price", 0) or 0)
        cur = float(self.current_price.get(code, 0) or 0)
        if qty <= 0 or avg <= 0 or cur <= 0:
            return 0
        max_budget = self._dynamic_total_budget_krw() * float(MAX_POSITION_BUDGET_PCT)
        current_budget = avg * float(qty)
        add_budget = max(0.0, max_budget - current_budget)
        return max(0, int(add_budget // cur))

    def _is_quality_avg_down_eligible(self, code: str) -> bool:
        c = str(code or "").strip()
        if not c:
            return False
        if c in getattr(self, "_quality_entry_codes", set()):
            return True
        if c in self._quality_mcap_allowlist:
            return True
        return self._use_quality_dip_for_code(c)

    def _should_quality_avg_down(self, code: str) -> bool:
        """우량주 평단 아래 물타기(최대 QUALITY_AVG_DOWN_MAX회)."""
        if not bool(QUALITY_AVG_DOWN_ENABLED):
            return False
        if not self._is_quality_avg_down_eligible(code):
            return False
        if self.pending_buy.get(code, False) or self.pending_sell.get(code, False):
            return False
        pending_order = (self.pending_orders or {}).get(code, {}) or {}
        if str(pending_order.get("side", "")).lower() in ("buy", "sell"):
            return False
        if self.market_session_name() != "정규장":
            return False
        if self._today_realized_pnl_pct() <= float(DAILY_LOSS_HARD_LIMIT_PCT):
            return False
        pos = self.positions.get(code) or {}
        qty = int(pos.get("qty", 0) or 0)
        avg = float(pos.get("avg_price", 0) or 0)
        cur = float(self.current_price.get(code, 0) or 0)
        if qty <= 0 or avg <= 0 or cur <= 0:
            return False
        count = int(self._quality_avg_down_count.get(code, 0) or 0)
        if count >= max(1, int(QUALITY_AVG_DOWN_MAX)):
            return False
        last_at = self._quality_avg_down_at.get(code)
        if isinstance(last_at, datetime.datetime):
            if (datetime.datetime.now() - last_at).total_seconds() < float(QUALITY_AVG_DOWN_COOLDOWN_SEC):
                return False
        loss_pct = cur / avg - 1.0
        need = -float(QUALITY_AVG_DOWN_STEP_PCT) * float(count + 1)
        if loss_pct > need:
            return False
        if bool(DAY_BIAS_ENABLED) and NEWS_SENTIMENT_AVAILABLE:
            try:
                ns = float(get_news_sentiment(code))
                if ns <= float(QUALITY_AVG_DOWN_NEWS_MIN):
                    return False
                if ns <= float(NEWS_CODE_BLOCK_THRESHOLD):
                    return False
            except Exception:
                pass
        max_budget = self._dynamic_total_budget_krw() * float(MAX_POSITION_BUDGET_PCT)
        current_budget = avg * float(qty)
        return current_budget < max_budget * 0.98

    def _quality_avg_down_qty(self, code: str) -> int:
        pos = self.positions.get(code) or {}
        qty = int(pos.get("qty", 0) or 0)
        avg = float(pos.get("avg_price", 0) or 0)
        cur = float(self.current_price.get(code, 0) or 0)
        if qty <= 0 or avg <= 0 or cur <= 0:
            return 0
        max_budget = self._dynamic_total_budget_krw() * float(MAX_POSITION_BUDGET_PCT)
        current_budget = avg * float(qty)
        room = max(0.0, max_budget - current_budget)
        add_budget = room * max(0.10, min(1.0, float(QUALITY_AVG_DOWN_SIZE_FACTOR)))
        max_single = max(0.0, self._effective_max_single_buy_krw(code))
        if max_single > 0:
            add_budget = min(add_budget, max_single)
        return max(0, int(add_budget // cur))

    def buy_market(
        self,
        code: str,
        qty_override: Optional[int] = None,
        *,
        avg_down: bool = False,
    ) -> None:
        if self._halt_order_guard(code, "BUY") or self._vi_order_guard(code, "BUY"):
            return
        # 2026-07-14: 매도쪽 sell_market 가드와 동일. 이미 추적 중인 매수주문이 있는데
        # timeout 쿨다운만 보고 재호출하면 pending_orders[code]가 덮어써져 1차 주문의
        # 늦은 체결 추적이 끊기고, 브로커에는 주문이 2건 쌓인다(044960 830주 사고).
        existing = self.pending_orders.get(code)
        if existing and str(existing.get("side", "")).lower() == "buy":
            now_g = datetime.datetime.now()
            timeout_at = existing.get("timeout_at")
            sent_at = existing.get("sent_at")
            if isinstance(timeout_at, datetime.datetime):
                guard_age = (now_g - timeout_at).total_seconds()
            elif isinstance(sent_at, datetime.datetime):
                guard_age = (now_g - sent_at).total_seconds()
            else:
                guard_age = 0.0
            if guard_age < float(ORDER_TIMEOUT_LATE_FILL_GUARD_SEC):
                self._emit_log(
                    f"[BUY-SKIP] {code} 이전 매수주문 추적 중(경과 {int(guard_age)}s/"
                    f"{int(ORDER_TIMEOUT_LATE_FILL_GUARD_SEC)}s) — 중복 주문 방지"
                )
                return
        self.pending_buy[code] = True
        qty = max(1, int(ORDER_QTY_PER_STOCK))

        # 시장가 매수는 체결가가 다를 수 있어, 내부 포지션은 "현재가"로 근사 관리합니다.
        cur = self.current_price.get(code)
        if cur is None:
            self.pending_buy[code] = False
            self._append_order_event(
                event="ORDER_SKIP_NO_PRICE",
                code=code,
                side="BUY",
                qty=qty,
                note="current_price missing",
            )
            return
        now = datetime.datetime.now()
        min_gap = float(MIN_SEC_BETWEEN_ANY_BUYS)
        if min_gap > 0.0 and self._last_global_buy_sent_at is not None:
            elapsed = (now - self._last_global_buy_sent_at).total_seconds()
            if elapsed < min_gap:
                self.pending_buy[code] = False
                self._log_buy_block(
                    code,
                    f"전역 매수 간격({elapsed:.1f}s<{min_gap:.0f}s)",
                    interval_sec=30.0,
                )
                return
        self._prune_global_buy_window(now)
        hour_cap = int(MAX_NEW_BUYS_PER_HOUR)
        if hour_cap > 0 and len(self._global_buy_timestamps) >= hour_cap:
            self.pending_buy[code] = False
            self._log_buy_block(
                code,
                f"시간당 매수 주문 한도({len(self._global_buy_timestamps)}/{hour_cap})",
                interval_sec=60.0,
            )
            return
        qty = int(qty_override) if qty_override is not None else self._budget_based_order_qty(float(cur), code)
        if qty <= 0:
            self.pending_buy[code] = False
            remaining = max(
                0.0,
                self._dynamic_total_budget_krw()
                - self._invested_budget_krw()
                - self._pending_buy_budget_krw(),
            )
            self._append_order_event(
                event="ORDER_SKIP_BUDGET",
                code=code,
                side="BUY",
                qty=0,
                price=float(cur),
                note=f"remaining_budget={int(remaining)}",
            )
            self._emit_log(f"[BUY-BLOCK] {code} 예산 부족 remaining={int(remaining):,} price={int(cur):,}")
            return
        order_value = float(cur) * float(qty)
        max_single_order = max(0.0, self._effective_max_single_buy_krw(code))
        if max_single_order > 0 and order_value > max_single_order:
            capped_qty = int(max_single_order // float(cur))
            if capped_qty <= 0:
                self.pending_buy[code] = False
                self._append_order_event(
                    event="ORDER_SKIP_MAX_BUY",
                    code=code,
                    side="BUY",
                    qty=0,
                    price=float(cur),
                    note=f"max_single_order={int(max_single_order)}",
                )
                self._emit_log(
                    f"[BUY-BLOCK] {code} 1회 매수상한 초과 max={int(max_single_order):,} price={int(cur):,}"
                )
                return
            self._append_order_event(
                event="ORDER_QTY_CAPPED",
                code=code,
                side="BUY",
                qty=int(capped_qty),
                price=float(cur),
                note=f"from={int(qty)} max_single_order={int(max_single_order)}",
            )
            qty = int(capped_qty)

        prev_qty_before = int((self.positions.get(code) or {}).get("qty", 0))
        sent_at = datetime.datetime.now()
        self.kiwoom.send_order_market(self.account, code, qty, ORDER_TYPE_BUY)
        self._last_global_buy_sent_at = sent_at
        self._global_buy_timestamps.append(sent_at)
        self._prune_global_buy_window(sent_at)
        self.pending_orders[code] = {
            "side": "buy",
            "qty": qty,
            "prev_qty": prev_qty_before,
            "sent_at": sent_at,
            "fills_notified_qty": 0,
            "order_px_ref": float(cur),
            "quality_entry": bool(
                self._use_quality_dip_for_code(code)
                or self._is_quality_avg_down_eligible(code)
            ),
            "avg_down": bool(avg_down),
        }
        if avg_down:
            self._quality_avg_down_at[code] = sent_at
        self._append_order_event(
            event="ORDER_SENT",
            code=code,
            side="BUY",
            qty=qty,
            price=float(cur),
        )
        self._emit_log(f"[BUY-ORDER] {code} qty={qty} ref={cur} budget_based={bool(BUDGET_BASED_ORDER_QTY)}")
        # 주문 1건당 태그 한 줄(과다 로그 방지)
        tags = []
        if bool((self.pending_orders.get(code) or {}).get("avg_down")):
            tags.append("물타기")
        elif bool((self.pending_orders.get(code) or {}).get("quality_entry")):
            tags.append("우량눌림")
        if NEWS_SENTIMENT_AVAILABLE and bool(_THEME_STRENGTH_ENABLED):
            try:
                tb = float(_get_theme_boost_for_code(code))
                if abs(tb) >= 0.15:
                    tags.append(f"테마{tb:+.2f}")
            except Exception:
                pass
        if tags:
            self._emit_log(f"[FEATURE] 매수태그 {code} " + "/".join(tags))

    def sell_market(self, code: str, reason: str = "") -> None:
        if self._halt_order_guard(code, "SELL") or self._vi_order_guard(code, "SELL"):
            return
        # 2026-07-09: 이미 추적 중인 매도주문이 있는데 또 sell_market이 호출되면
        # pending_orders[code]가 덮어써져서 첫 주문의 체결 추적이 끊긴다(004985 사고).
        # 아직 살아있는(guard 기간 이내) 매도주문 추적이 있으면 중복 주문을 거부한다.
        existing = self.pending_orders.get(code)
        if existing and str(existing.get("side", "")) == "sell":
            sent_at = existing.get("sent_at")
            age = (
                (datetime.datetime.now() - sent_at).total_seconds()
                if isinstance(sent_at, datetime.datetime)
                else 0.0
            )
            if age < float(SELL_ORDER_TIMEOUT_LATE_FILL_GUARD_SEC):
                self._emit_log(
                    f"[SELL-SKIP] {code} 이전 매도주문 추적 중(경과 {int(age)}s) — 중복 주문 방지"
                )
                return
        self.pending_sell[code] = True
        pos = self.positions.get(code) or {}
        qty = int(pos.get("qty", 0))
        if qty <= 0:
            self.pending_sell[code] = False
            self._append_order_event(
                event="ORDER_SKIP_NO_QTY",
                code=code,
                side="SELL",
                qty=qty,
                note="position qty <= 0",
            )
            return

        if reason == "partial_take_profit" and bool(PARTIAL_TAKE_PROFIT_ENABLED) and qty > 1:
            qty = max(1, int(qty * float(PARTIAL_TAKE_PROFIT_RATIO)))
        prev_avg = int(pos.get("avg_price", 0) or 0)
        self.kiwoom.send_order_market(self.account, code, qty, ORDER_TYPE_SELL)
        self.pending_orders[code] = {
            "side": "sell",
            "qty": qty,
            "prev_qty": int(pos.get("qty", 0)),
            "prev_avg_price": prev_avg,
            "reason": str(reason or ""),
            "sent_at": datetime.datetime.now(),
            "fills_notified_qty": 0,
            "order_px_ref": float(self.current_price.get(code, 0) or prev_avg or 0),
        }
        self._append_order_event(
            event="ORDER_SENT",
            code=code,
            side="SELL",
            qty=qty,
            price=float(self.current_price.get(code, 0) or prev_avg or 0),
            note=f"reason={reason}" if reason else "",
        )
        self._emit_log(f"[SELL-ORDER] {code} qty={qty} reason={reason or '-'}")

    def _chejan_dedup_seen(self, key: str) -> bool:
        """동일 체결 이벤트 중복 처리 방지."""
        if key in self._chejan_dedup_keys:
            return True
        self._chejan_dedup_keys.add(key)
        self._chejan_dedup_order.append(key)
        while len(self._chejan_dedup_order) > 550:
            old = self._chejan_dedup_order.popleft()
            self._chejan_dedup_keys.discard(old)
        return False

    def _on_kiwoom_chejan(self, fields: Dict[str, str]) -> None:
        """주문체결 통보(FID 참고: 키움 개발가이드 8.19 주문체결)."""
        if str(fields.get("gubun", "")).strip() != "0":
            return
        ca = _normalize_kiwoom_account_for_match(str(fields.get("9201", "")))
        ea = _normalize_kiwoom_account_for_match(str(self.account))
        if not ca or ca != ea:
            return
        code = _parse_chejan_code9001(fields.get("9001", ""))
        if len(code) != 6 or not code.isdigit():
            return
        status = str(fields.get("913", "") or "").strip()
        if "체결" not in status:
            return
        ord_no = str(fields.get("9203", "") or "").strip()

        def _parse_qty_fid(fid: str) -> int:
            try:
                raw_q = str(fields.get(fid, "") or "0").strip().replace(",", "")
                return abs(int(float(raw_q or 0)))
            except (ValueError, TypeError):
                return 0

        q915 = _parse_qty_fid("915")
        q911 = _parse_qty_fid("911")
        meta_prev = self.pending_orders.get(code)
        fill_qty = 0
        if q915 > 0:
            fill_qty = q915
        elif q911 > 0:
            prev911 = int((meta_prev or {}).get("last_chejan_911_cum", 0) or 0)
            if prev911 > 0 and q911 >= prev911:
                fill_qty = q911 - prev911
            else:
                fill_qty = q911
        if fill_qty <= 0:
            return
        try:
            fill_price = abs(
                int(float(str(fields.get("910", "") or "0").strip().replace(",", "") or 0))
            )
        except (ValueError, TypeError):
            fill_price = 0
        if fill_price <= 0:
            return

        side907 = str(fields.get("907", "") or "").strip()
        if side907 == "1":
            side = "SELL"
        elif side907 == "2":
            side = "BUY"
        else:
            g905 = str(fields.get("905", "") or "").strip()
            if "매도" in g905:
                side = "SELL"
            elif "매수" in g905:
                side = "BUY"
            else:
                return

        exec_id = (
            str(fields.get("909", "") or "").strip()
            or str(fields.get("908", "") or "").strip()
        )
        dedup_key = f"{ord_no}|{exec_id}|{side}|{fill_qty}|{fill_price}"
        if self._chejan_dedup_seen(dedup_key):
            return
        now_ts = datetime.datetime.now()
        self._apply_kiwoom_chejan_fill(
            code=code,
            side=side,
            fill_qty=fill_qty,
            fill_price=fill_price,
            order_no=ord_no or "-",
            now_ts=now_ts,
            chejan_911_snapshot=q911 if q911 > 0 else None,
        )

    def _apply_kiwoom_chejan_fill(
        self,
        code: str,
        side: str,
        fill_qty: int,
        fill_price: int,
        order_no: str,
        now_ts: datetime.datetime,
        chejan_911_snapshot: Optional[int] = None,
    ) -> None:
        meta = self.pending_orders.get(code)
        if meta is None:
            return
        pend = str(meta.get("side", "")).strip().lower()
        if side == "BUY" and pend != "buy":
            return
        if side == "SELL" and pend != "sell":
            return

        ord_qty = int(meta.get("qty", 0) or 0)
        already = int(meta.get("fills_notified_qty", 0) or 0)
        room = max(0, ord_qty - already)
        use_qty = min(int(fill_qty), room) if ord_qty > 0 else int(fill_qty)
        if use_qty <= 0:
            return

        if chejan_911_snapshot is not None and int(chejan_911_snapshot) > 0:
            meta["last_chejan_911_cum"] = int(chejan_911_snapshot)

        px = float(int(fill_price))
        note_suffix = f"chejan ord={order_no}"

        if side == "BUY":
            self.pending_buy[code] = False
            if already == 0:
                self.last_buy_date[code] = datetime.date.today()
                self.entry_time[code] = now_ts
                self.tp_reached[code] = False
            if bool(meta.get("quality_entry")) or bool(meta.get("avg_down")):
                self._quality_entry_codes.add(code)
            if bool(meta.get("avg_down")) and already == 0:
                self._quality_avg_down_count[code] = int(
                    self._quality_avg_down_count.get(code, 0) or 0
                ) + 1
                self._quality_avg_down_at[code] = now_ts
            avg_price_int = int(round(px))
            if avg_price_int > 0:
                self._append_order_event(
                    event="ORDER_FILLED",
                    code=code,
                    side="BUY",
                    qty=int(use_qty),
                    price=float(avg_price_int),
                    note=note_suffix,
                )
                self._notify_trade_fill(
                    {
                        "side": "BUY",
                        "code": code,
                        "price": float(avg_price_int),
                        "qty": int(use_qty),
                        "ts": now_ts,
                    }
                )
            meta["fills_notified_qty"] = already + use_qty
            if int(meta["fills_notified_qty"]) >= ord_qty:
                self.pending_orders.pop(code, None)
            self._adjust_position_on_chejan_fill(code, "BUY", int(use_qty), int(round(px)))
            self._emit_log(
                f"[CHEJAN-FILL] BUY {code} qty={use_qty} px={avg_price_int} cum={meta['fills_notified_qty']}/{ord_qty}"
            )
            self._need_portfolio_sync = True  # 체결 직후 다음 틱(≤2초)에 잔고 즉시 갱신

        elif side == "SELL":
            prev_avg = int(meta.get("prev_avg_price", 0) or 0)
            if prev_avg <= 0:
                return
            sell_reason = str(meta.get("reason", "") or "")
            self.pending_sell[code] = False
            sell_price_f = px
            realized = (sell_price_f - float(prev_avg)) * float(use_qty)
            self.realized_pnl_krw += float(realized)
            pnl_pct = (sell_price_f - float(prev_avg)) / float(prev_avg) * 100.0
            if sell_reason in ("stop_loss", "ai_loss_exit"):
                self.buy_cooldown_until[code] = now_ts + datetime.timedelta(
                    seconds=float(STOP_LOSS_REENTRY_COOLDOWN_SEC)
                )
                # 재진입 모드 진입: 손절 체결가 저장, 해당가 회복 후에만 재매수 허용
                self._stop_loss_prices[code] = float(sell_price_f)
                self._reentry_codes.add(code)
            elif sell_reason in ("take_profit", "trailing_stop", "ai_profit_exit",
                                 "ai_trailing_exit", "partial_take_profit"):
                # 수익 청산 시 재진입 모드 해제
                self._reentry_codes.discard(code)
                self._stop_loss_prices.pop(code, None)
            self._append_order_event(
                event="ORDER_FILLED",
                code=code,
                side="SELL",
                qty=int(use_qty),
                price=float(sell_price_f),
                note=(
                    (f"{note_suffix} reason={sell_reason}") if sell_reason else note_suffix
                ),
            )
            self._notify_trade_fill(
                {
                    "side": "SELL",
                    "code": code,
                    "price": sell_price_f,
                    "qty": int(use_qty),
                    "pnl_pct": float(pnl_pct),
                    "realized_pnl_krw": float(realized),
                    "sell_reason": sell_reason,
                    "ts": now_ts,
                }
            )
            meta["fills_notified_qty"] = already + use_qty
            if int(meta["fills_notified_qty"]) >= ord_qty:
                self.pending_orders.pop(code, None)
            self._adjust_position_on_chejan_fill(code, "SELL", int(use_qty), int(round(px)))
            self._emit_log(
                f"[CHEJAN-FILL] SELL {code} qty={use_qty} px={int(round(px))} "
                f"cum={meta['fills_notified_qty']}/{ord_qty}"
            )
            self._need_portfolio_sync = True  # 체결 직후 다음 틱(≤2초)에 잔고 즉시 갱신

    def _reconcile_orders_and_portfolio(self) -> None:
        """
        체결확인 -> 포트폴리오 업데이트.
        잔고 TR은 on_tick 밖 QTimer로 돌리고, 완료 후 pending 체결을 추정한다.
        """
        if not self.pending_orders:
            return
        RECONCILE_SYNC_INTERVAL_SEC = 10.0
        now_r = datetime.datetime.now()
        if (
            self._last_reconcile_sync_at is not None
            and (now_r - self._last_reconcile_sync_at).total_seconds() < RECONCILE_SYNC_INTERVAL_SEC
        ):
            return
        if not self._schedule_holdings_sync(kind="reconcile", delay_ms=50):
            return
        self._last_reconcile_sync_at = now_r

    def _reconcile_pending_orders_after_sync(self) -> None:
        """잔고 TR 완료 후 pending 주문 체결 추정 (deferred 콜백)."""
        now = datetime.datetime.now()
        to_clear: List[str] = []
        for code, meta in list(self.pending_orders.items()):
            side = str(meta.get("side", ""))
            sent_at = meta.get("sent_at")
            elapsed = (now - sent_at).total_seconds() if isinstance(sent_at, datetime.datetime) else 0.0
            pos = self.positions.get(code, {})
            cur_qty = int(pos.get("qty", 0))
            ord_qty = int(meta.get("qty", 0) or 0)
            if side == "buy":
                prev_qty_before = int(meta.get("prev_qty", 0) or 0)
                filled_qty = max(0, cur_qty - prev_qty_before)
                if filled_qty > 0:
                    already = int(meta.get("fills_notified_qty", 0) or 0)
                    to_notify = max(0, filled_qty - already)
                    self.pending_buy[code] = False
                    if to_notify > 0 and already == 0:
                        self.last_buy_date[code] = datetime.date.today()
                        self.entry_time[code] = datetime.datetime.now()
                        self.tp_reached[code] = False
                        if bool(meta.get("quality_entry")) or bool(meta.get("avg_down")):
                            self._quality_entry_codes.add(code)
                        if bool(meta.get("avg_down")):
                            self._quality_avg_down_count[code] = int(
                                self._quality_avg_down_count.get(code, 0) or 0
                            ) + 1
                            self._quality_avg_down_at[code] = datetime.datetime.now()
                    avg_price = int(pos.get("avg_price", 0) or 0)
                    if avg_price > 0 and to_notify > 0:
                        self._append_order_event(
                            event="ORDER_FILLED",
                            code=code,
                            side="BUY",
                            qty=int(to_notify),
                            price=float(avg_price),
                            note="tr_holdings_avg",
                        )
                        self._notify_trade_fill(
                            {
                                "side": "BUY",
                                "code": code,
                                "price": float(avg_price),
                                "qty": int(to_notify),
                                "ts": datetime.datetime.now(),
                            }
                        )
                    meta["fills_notified_qty"] = filled_qty
                    if filled_qty >= ord_qty:
                        to_clear.append(code)
                    self._emit_log(
                        f"[FILLED-BUY] {code} broker={filled_qty} tr_slice={to_notify} total_pos={cur_qty}"
                    )
                elif elapsed > 25:
                    if not bool(meta.get("timeout_logged", False)):
                        meta["timeout_logged"] = True
                        meta["timeout_at"] = now
                        self.buy_cooldown_until[code] = now + datetime.timedelta(
                            seconds=float(ORDER_TIMEOUT_COOLDOWN_SEC)
                        )
                        self._append_order_event(
                            event="ORDER_TIMEOUT",
                            code=code,
                            side="BUY",
                            qty=int(ord_qty),
                            note=(
                                f"pending {int(elapsed)}s; cooldown {int(ORDER_TIMEOUT_COOLDOWN_SEC)}s; "
                                f"late_fill_guard {int(ORDER_TIMEOUT_LATE_FILL_GUARD_SEC)}s"
                            ),
                        )
                        self._emit_log(
                            f"[WARN] buy pending timeout {code} - 늦은 체결 확인 대기 "
                            f"{int(ORDER_TIMEOUT_LATE_FILL_GUARD_SEC)}s"
                        )
                    timeout_at = meta.get("timeout_at")
                    guard_elapsed = (
                        (now - timeout_at).total_seconds() if isinstance(timeout_at, datetime.datetime) else 0.0
                    )
                    if guard_elapsed >= float(ORDER_TIMEOUT_LATE_FILL_GUARD_SEC):
                        self.pending_buy[code] = False
                        to_clear.append(code)
                        self._append_order_event(
                            event="ORDER_LATE_FILL_GUARD_EXPIRED",
                            code=code,
                            side="BUY",
                            qty=int(ord_qty),
                            note=f"guard_elapsed={int(guard_elapsed)}s",
                        )
            elif side == "sell":
                prev_qty = int(meta.get("prev_qty", ord_qty) or ord_qty)
                if cur_qty < prev_qty:
                    self.pending_sell[code] = False
                    self.sell_cooldown_until.pop(code, None)  # 체결 시 쿨다운 해제
                    self._sell_retry_counts.pop(code, None)   # 체결 시 누적 retry 초기화
                    sold_qty = prev_qty - cur_qty
                    already = int(meta.get("fills_notified_qty", 0) or 0)
                    to_notify = max(0, sold_qty - already)
                    prev_avg = int(meta.get("prev_avg_price", 0) or 0)
                    sell_reason = str(meta.get("reason", "") or "")
                    ref = float(meta.get("order_px_ref", 0) or 0)
                    curp = float(self.current_price.get(code) or 0)
                    if ref > 0 and curp > 0:
                        sell_price = (ref + curp) / 2.0
                    else:
                        sell_price = float(curp or prev_avg)
                    if sold_qty >= ord_qty and int(sold_qty) != int(ord_qty):
                        mismatch_note = (
                            f"ordered={int(ord_qty)} filled_est={int(sold_qty)} "
                            f"prev={int(prev_qty)} cur={int(cur_qty)}"
                        )
                        self._append_order_event(
                            event="ORDER_FILL_QTY_MISMATCH",
                            code=code,
                            side="SELL",
                            qty=int(sold_qty),
                            price=float(sell_price),
                            note=mismatch_note,
                        )
                        self._emit_log(f"[WARN] sell fill qty mismatch {code} {mismatch_note}")
                    if to_notify > 0 and prev_avg > 0:
                        self._append_order_event(
                            event="ORDER_FILLED",
                            code=code,
                            side="SELL",
                            qty=int(to_notify),
                            price=float(sell_price),
                            note=(
                                (f"tr_mid_px reason={sell_reason}")
                                if sell_reason
                                else "tr_mid_px"
                            ),
                        )
                        realized = (sell_price - float(prev_avg)) * float(to_notify)
                        self.realized_pnl_krw += float(realized)
                        pnl_pct = (sell_price - float(prev_avg)) / float(prev_avg) * 100.0
                        if sell_reason in ("stop_loss", "ai_loss_exit"):
                            self.buy_cooldown_until[code] = now + datetime.timedelta(
                                seconds=float(STOP_LOSS_REENTRY_COOLDOWN_SEC)
                            )
                        self._notify_trade_fill(
                            {
                                "side": "SELL",
                                "code": code,
                                "price": sell_price,
                                "qty": int(to_notify),
                                "pnl_pct": float(pnl_pct),
                                "realized_pnl_krw": float(realized),
                                "sell_reason": sell_reason,
                                "ts": datetime.datetime.now(),
                            }
                        )
                    meta["fills_notified_qty"] = sold_qty
                    if cur_qty <= 0:
                        self.tp_reached[code] = False
                        self._scale_in_done[code] = False
                        self._quality_avg_down_count.pop(code, None)
                        self._quality_avg_down_at.pop(code, None)
                        self._quality_entry_codes.discard(code)
                    if sold_qty >= ord_qty:
                        to_clear.append(code)
                    self._emit_log(
                        f"[FILLED-SELL] {code} sold={sold_qty} tr_slice={to_notify} reason={sell_reason or '-'}"
                    )
                elif elapsed > 25:
                    # 2026-07-09: 예전엔 여기서 바로 pending_orders를 지워버려서, TR 응답이
                    # 25초보다 늦게 오는 경우(TR 과호출 시 흔함) 실제 체결을 영영 못 잡고
                    # CSV 기록이 누락되는 사고(004985)가 있었다. buy쪽 late_fill_guard와
                    # 동일하게, WARN/재시도 허용은 여기서 하되 pending_orders 추적 자체는
                    # guard 기간 동안 유지해서 늦은 체결도 위 cur_qty<prev_qty 분기가 잡게 한다.
                    if not bool(meta.get("timeout_logged", False)):
                        meta["timeout_logged"] = True
                        meta["timeout_at"] = now
                        self.pending_sell[code] = False
                        # 종목별 누적 retry 횟수 (주문 교체 후에도 유지)
                        self._sell_retry_counts[code] = self._sell_retry_counts.get(code, 0) + 1
                        retry_count = self._sell_retry_counts[code]
                        SELL_RETRY_GIVE_UP = 15  # 15회 이상이면 매도 포기 (하한가 등)
                        if retry_count >= SELL_RETRY_GIVE_UP:
                            # 장시간 미체결 → 수동 확인 필요, 더 이상 자동 재시도 안 함
                            give_up_until = datetime.datetime.now() + datetime.timedelta(hours=2)
                            self.sell_cooldown_until[code] = give_up_until
                            self._append_order_event(
                                event="ORDER_TIMEOUT",
                                code=code,
                                side="SELL",
                                qty=int(ord_qty),
                                note=f"pending {int(elapsed)}s retry={retry_count} GIVE_UP 2h",
                            )
                            self._emit_log(
                                f"[WARN] sell GIVE-UP {code} retry={retry_count}회 초과 — 2시간 후 재시도 (하한가/수동확인 권장)"
                            )
                        else:
                            cooldown_sec = min(120 * retry_count, 600)  # 1회:2분, 5회+:10분 상한
                            self.sell_cooldown_until[code] = datetime.datetime.now() + datetime.timedelta(seconds=cooldown_sec)
                            self._append_order_event(
                                event="ORDER_TIMEOUT",
                                code=code,
                                side="SELL",
                                qty=int(ord_qty),
                                note=f"pending {int(elapsed)}s retry={retry_count} cooldown={cooldown_sec}s; late_fill_guard {int(SELL_ORDER_TIMEOUT_LATE_FILL_GUARD_SEC)}s",
                            )
                            self._emit_log(
                                f"[WARN] sell pending timeout {code} (재시도 {retry_count}회, {cooldown_sec}s 후 재시도, "
                                f"늦은 체결 확인 대기 {int(SELL_ORDER_TIMEOUT_LATE_FILL_GUARD_SEC)}s)"
                            )
                    timeout_at = meta.get("timeout_at")
                    guard_elapsed = (
                        (now - timeout_at).total_seconds() if isinstance(timeout_at, datetime.datetime) else 0.0
                    )
                    if guard_elapsed >= float(SELL_ORDER_TIMEOUT_LATE_FILL_GUARD_SEC):
                        to_clear.append(code)
                        self._append_order_event(
                            event="ORDER_LATE_FILL_GUARD_EXPIRED",
                            code=code,
                            side="SELL",
                            qty=int(ord_qty),
                            note=f"guard_elapsed={int(guard_elapsed)}s",
                        )
        for code in to_clear:
            self.pending_orders.pop(code, None)

    def _emit_daily_report_if_needed(self) -> None:
        now = datetime.datetime.now()
        if self._daily_report_done_date == now.date():
            return
        # 정규장 종료 이후에만 1회 출력
        if _qtime_sec(QTime.currentTime()) < _krx_session_bounds(now.date())["close"]:
            return
        tracker = getattr(self, "trade_tracker", None)
        if tracker is None:
            return
        try:
            records = list(getattr(tracker, "records", []) or [])
        except Exception:
            return
        today_records = [
            r
            for r in records
            if isinstance(getattr(r, "ts", None), datetime.datetime)
            and getattr(r, "ts").date() == now.date()
        ]
        sells = [r for r in today_records if str(getattr(r, "side", "")).upper() == "SELL"]
        buys = [r for r in today_records if str(getattr(r, "side", "")).upper() == "BUY"]
        realized = sum(float(getattr(r, "realized_pnl_krw", 0.0) or 0.0) for r in sells)
        wins = sum(1 for r in sells if float(getattr(r, "realized_pnl_krw", 0.0) or 0.0) > 0)
        reasons: Dict[str, int] = {}
        for r in sells:
            reason = str(getattr(r, "sell_reason", "") or "unknown")
            reasons[reason] = reasons.get(reason, 0) + 1
        reason_txt = ", ".join(f"{k}:{v}" for k, v in sorted(reasons.items())) or "-"
        win_rate = (wins / float(len(sells)) * 100.0) if sells else 0.0
        self._emit_log(
            f"[DAILY] 매수 {len(buys)}건 / 매도 {len(sells)}건 / 실현손익 {int(realized):+,}원 "
            f"/ 승률 {win_rate:.1f}% / 매도사유 {reason_txt}"
        )
        try:
            self._load_ai_metrics_from_disk()
            auc = getattr(self, "ai_val_auc", None)
            ece = getattr(self, "ai_val_ece", None)
            inj = int(getattr(self, "ai_live_injected", 0) or 0)
            wins = int(getattr(self, "ai_live_wins", 0) or 0)
            qf = float(self._ai_quality_size_factor())
            auc_txt = f"{float(auc):.3f}" if auc is not None else "-"
            ece_txt = f"{float(ece):.3f}" if ece is not None else "-"
            self._emit_log(
                f"[DAILY-AI] auc={auc_txt} ece={ece_txt} live={wins}/{inj} "
                f"thr={self._dynamic_ai_entry_min():.2f} size×{qf:.2f} | {self.ai_status_line()}"
            )
        except Exception:
            pass
        self._daily_report_done_date = now.date()
        # 장 마감 리포트 출력 후 로그 자동 분석 예약 (15:40 이후 별도 실행)

    def _auto_analyze_daily_log(self) -> None:
        """애프터마켓 종료(20:00) 후 하루 1회: 프리마켓~애프터마켓 전 구간 로그를 파싱해
        자동 분석 리포트와 코드 수정 제안을 생성한다."""
        today = datetime.date.today()
        if self._log_analysis_done_date == today:
            return
        # 애프터마켓(15:30~20:00) 완전 종료 이후에만 실행
        if QTime.currentTime() < QTime(20, 0, 0):
            return

        log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "log")
        log_path = os.path.join(log_dir, today.strftime("%Y-%m-%d") + ".txt")
        if not os.path.exists(log_path):
            return

        import re
        import collections

        try:
            with open(log_path, "r", encoding="utf-8") as f:
                raw_lines = f.readlines()
        except Exception:
            return

        # 재시작 등으로 하루에 이 분석이 두 번 이상 실행되면, 이전에 append된
        # 리포트 텍스트 자체가 오늘 로그 파일에 남아 있어 재파싱 시 WARN 등이
        # 중복 집계될 수 있다. 리포트 블록을 감싼 센티널 사이 구간은 제외한다.
        REPORT_START = "<!--AUTO-ANALYSIS-REPORT-START-->"
        REPORT_END = "<!--AUTO-ANALYSIS-REPORT-END-->"
        filtered_lines = []
        _in_prev_report = False
        for raw in raw_lines:
            if REPORT_START in raw:
                _in_prev_report = True
                continue
            if REPORT_END in raw:
                _in_prev_report = False
                continue
            if _in_prev_report:
                continue
            filtered_lines.append(raw)
        raw_lines = filtered_lines

        # ─── 구간별 파싱 ──────────────────────────────────────
        # 로그 형식: [HH:MM:SS][PRE/MKT/AFT/OUT] ...
        PHASES = ("PRE", "MKT", "AFT", "OUT")

        def _phase(line: str) -> str:
            m = re.search(r"\]\[([A-Z]+)\]", line)
            return m.group(1) if m else "OUT"

        buy_orders: collections.Counter = collections.Counter()    # phase → count
        sell_orders: collections.Counter = collections.Counter()
        filled_sells: collections.Counter = collections.Counter()
        # [FILLED-SELL]은 TR 재조정 경로에서만 드물게 찍혀 거의 항상 0으로 남는다.
        # 실제 체결 여부는 chejan 이벤트([POSITION] chejan BUY/SELL)로 판단해야 한다.
        chejan_fills: collections.Counter = collections.Counter()  # phase → 체결 이벤트 수(부분체결 포함)
        sell_timeouts: collections.Counter = collections.Counter()  # stock_code → count
        sell_giveups: list = []
        buy_blocks: collections.Counter = collections.Counter()    # reason → count
        # 정규장 구간만 실시간 수신율 집계 (MKT)
        real_rates_mkt: list = []
        real_rates_aft: list = []
        holdings_zero: int = 0
        warn_lines: list = []
        diag_lines: list = []
        bt_gate_lines: list = []
        filled_sell_reasons: collections.Counter = collections.Counter()

        for raw in raw_lines:
            s = raw.strip()
            ph = _phase(s)
            if "[BUY-ORDER]" in s:
                buy_orders[ph] += 1
            elif "[SELL-ORDER]" in s:
                sell_orders[ph] += 1
                m = re.search(r"reason=(\S+)", s)
                if m:
                    filled_sell_reasons[m.group(1)] += 1
            elif "[FILLED-SELL]" in s:
                filled_sells[ph] += 1
            elif "[POSITION] chejan " in s:
                chejan_fills[ph] += 1
            elif "sell pending timeout" in s:
                m = re.search(r"timeout (\w+)", s)
                if m:
                    sell_timeouts[m.group(1)] += 1
            elif "sell GIVE-UP" in s:
                sell_giveups.append(s)
            elif "[BUY-BLOCK]" in s:
                m = re.search(r"\[BUY-BLOCK\] \S+ (.+)$", s)
                if m:
                    reason = m.group(1).split("(")[0].strip()
                    buy_blocks[reason] += 1
            elif "[REAL]" in s:
                m = re.search(r"수신율 ([\d.]+)/s", s)
                if m:
                    rate = float(m.group(1))
                    if ph == "MKT":
                        real_rates_mkt.append(rate)
                    elif ph == "AFT":
                        real_rates_aft.append(rate)
            elif "TR 0건이나" in s or "잔고조회 결과 0건" in s:
                holdings_zero += 1
            elif "[WARN]" in s and "수신율 0.0" not in s:
                warn_lines.append(s)
            elif "[BUY-DIAG]" in s:
                diag_lines.append(s)
            elif "[BT-GATE]" in s:
                bt_gate_lines.append(s)

        # ─── 집계 ─────────────────────────────────────────────
        n_buy_total = sum(buy_orders.values())
        n_sell_total = sum(sell_orders.values())
        # 체결 건수는 chejan 이벤트 기준(실제 브로커 체결통보)으로 집계한다.
        # [FILLED-SELL] 태그만 세면 거의 매일 0건으로 나와 "체결 0건" 오탐 알림이 발생했었다.
        n_filled_total = sum(chejan_fills.values()) or sum(filled_sells.values())
        n_timeout_total = sum(sell_timeouts.values())
        n_giveup = len(sell_giveups)

        avg_real_mkt = (sum(real_rates_mkt) / len(real_rates_mkt)) if real_rates_mkt else 0.0
        zero_real_mkt = sum(1 for r in real_rates_mkt if r == 0.0)
        avg_real_aft = (sum(real_rates_aft) / len(real_rates_aft)) if real_rates_aft else 0.0

        top_blocks = buy_blocks.most_common(7)
        top_timeout_stocks = sell_timeouts.most_common(5)

        last_market = "-"
        last_strategy = "-"
        if diag_lines:
            m = re.search(r"시장=(\S+)\s+활성전략=(\S+)", diag_lines[-1])
            if m:
                last_market, last_strategy = m.group(1), m.group(2)

        last_bt_mode = "-"
        if bt_gate_lines:
            m = re.search(r"mode=(\S+)", bt_gate_lines[-1])
            if m:
                last_bt_mode = m.group(1)

        # ─── 코드 수정 제안 생성 ──────────────────────────────
        suggestions: list = []

        # 매수 차단 분석
        block_reasons = {r: c for r, c in top_blocks}
        if block_reasons.get("당일 승률 저조", 0) > 50:
            suggestions.append(
                "【파라미터】DAILY_WIN_RATE_BLOCK_MIN_SELLS 값을 늘리거나 "
                "DAILY_WIN_RATE_BLOCK_PCT를 낮추면 승률 차단 완화 가능"
            )
        if block_reasons.get("현재가 미수신", 0) > 500:
            suggestions.append(
                "【실시간】현재가 미수신 종목이 많음 — SetRealReg 등록 목록 확인, "
                "종목 수 축소(MAX_WATCH_COUNT) 또는 재등록 로직 점검 필요"
            )
        if block_reasons.get("전략조건 미충족", 0) > 200 and n_buy_total == 0:
            suggestions.append(
                "【전략】진입 조건이 전일보다 엄격 — MA20_ENTRY_GAP_PCT, "
                "VOL_ENTRY_RATIO_MIN, RSI_ENTRY_MIN/MAX 완화 검토"
            )
        if block_reasons.get("당일 손실한도 초과", 0) > 0:
            suggestions.append(
                "【리스크】당일 손실한도(DAILY_LOSS_SOFT/HARD_LIMIT_PCT) 초과 — "
                "손절 기준(STOP_LOSS_PCT) 또는 포지션 크기 조정 검토"
            )

        # 매도 미체결 분석
        if n_giveup > 0:
            giveup_codes = ", ".join(c for c in sell_timeouts.keys()
                                     if sell_timeouts[c] >= 10)
            suggestions.append(
                f"【매도】{n_giveup}개 종목 매도 GIVE-UP ({giveup_codes or '확인필요'}) — "
                "하한가 종목 제거 또는 손절 임계값 재검토 필요"
            )
        if n_timeout_total > 10 and n_giveup == 0:
            suggestions.append(
                f"【매도】매도 미체결 {n_timeout_total}회 — "
                "SELL_ORDER_TIMEOUT_LATE_FILL_GUARD_SEC·재시도 쿨다운 점검 또는 "
                "지정가→시장가 전환 검토"
            )

        # 실시간 수신 분석
        if real_rates_mkt and zero_real_mkt > 5:
            suggestions.append(
                f"【실시간】정규장 중 수신율 0/s 구간 {zero_real_mkt}회 — "
                "SetRealReg 재호출 로직 또는 접속 안정성 점검 필요"
            )
        elif not real_rates_mkt:
            suggestions.append(
                "【실시간】정규장 [REAL] 로그 없음 — GUI 로그만 있고 파일 미기록일 수 있음"
                "(로그 파일 기록 경로 확인)"
            )

        # 잔고조회 분석
        if holdings_zero > 10:
            suggestions.append(
                f"【잔고】HOLDINGS 0종목 이벤트 {holdings_zero}회 — "
                "HTS 실보유가 0이면 정상. CSV 유령보유면 RECONCILE만 무시/정리. "
                "HTS에 보유가 보이면 비번·opw00018 재확인"
            )

        if not suggestions:
            suggestions.append("특별한 코드 수정 없이 정상 동작 범위로 보임")

        # ─── 리포트 작성 ──────────────────────────────────────
        sep = "=" * 64
        lines_out = [
            sep,
            f"  [AI-ANALYSIS] {today.strftime('%Y-%m-%d')} 전 구간 자동 분석 리포트",
            f"  분석 시점: 애프터마켓 종료 후 ({datetime.datetime.now().strftime('%H:%M')})",
            sep,
            "",
            "■ 구간별 주문 요약",
            f"  {'구간':<6} {'매수':>6} {'매도주문':>8} {'체결':>6}",
            f"  {'-'*30}",
        ]
        for ph in ("PRE", "MKT", "AFT"):
            ph_label = {"PRE": "프리마켓", "MKT": "정규장  ", "AFT": "애프터  "}[ph]
            ph_filled = chejan_fills[ph] or filled_sells[ph]
            lines_out.append(
                f"  {ph_label}  {buy_orders[ph]:>5}건  {sell_orders[ph]:>7}건  {ph_filled:>5}건"
            )
        lines_out += [
            f"  {'합계':<8}  {n_buy_total:>5}건  {n_sell_total:>7}건  {n_filled_total:>5}건",
            "",
            "■ 매도 미체결",
            f"  timeout 총 {n_timeout_total}회  |  GIVE-UP {n_giveup}건",
        ]
        if top_timeout_stocks:
            lines_out.append("  반복 미체결 종목:")
            for rank, (code, cnt) in enumerate(top_timeout_stocks, 1):
                lines_out.append(f"    {rank}. {code}: {cnt}회")
        if filled_sell_reasons:
            lines_out.append("  체결 매도 사유: " +
                             ", ".join(f"{k}:{v}" for k, v in filled_sell_reasons.most_common()))

        lines_out += [
            "",
            "■ 실시간 시세 수신 (정규장 기준)",
        ]
        if real_rates_mkt:
            lines_out.append(
                f"  정규장 평균: {avg_real_mkt:.1f}/s   0/s 구간: {zero_real_mkt}회"
            )
        else:
            lines_out.append(
                "  정규장: [REAL] 로그 샘플 없음(수신율 측정 불가 — GUI전용 로그였을 수 있음)"
            )
        if real_rates_aft:
            lines_out.append(
                f"  애프터마켓 평균: {avg_real_aft:.1f}/s  (15:30 이후 시세 미수신은 정상)"
            )
        else:
            lines_out.append("  애프터마켓: [REAL] 샘플 없음")

        lines_out += [
            "",
            "■ 매수 차단 TOP-7 사유",
        ]
        for rank, (reason, cnt) in enumerate(top_blocks, 1):
            lines_out.append(f"  {rank}. {reason}: {cnt}회")
        if not top_blocks:
            lines_out.append("  (차단 없음)")

        lines_out += [
            "",
            "■ 잔고조회 0건 이벤트",
            f"  {holdings_zero}회",
            "",
            "■ 장 마감 시점 상태",
            f"  시장 레짐: {last_market}  /  전략: {last_strategy}  /  BT-GATE: {last_bt_mode}",
            "",
            "■ 경고(WARN) 상위 5건",
        ]
        for w in warn_lines[:5]:
            lines_out.append(f"  {w[-120:]}")
        if not warn_lines:
            lines_out.append("  (없음)")

        lines_out += [
            "",
            sep,
            "  ★ 코드 수정 제안 (내일 대응 전 확인 권장)",
            sep,
        ]
        for i, sug in enumerate(suggestions, 1):
            lines_out.append(f"  [{i}] {sug}")

        lines_out += ["", sep, ""]

        report_text = "\n".join(lines_out)

        # 로그 파일에 추가 기록 (센티널로 감싸서, 같은 날 재실행 시 이 블록을
        # 다시 파싱해 WARN 등이 중복 집계되지 않도록 한다)
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(
                    "\n<!--AUTO-ANALYSIS-REPORT-START-->\n"
                    + report_text
                    + "\n<!--AUTO-ANALYSIS-REPORT-END-->\n"
                )
        except Exception:
            pass

        # 별도 analysis 파일 저장
        try:
            analysis_path = os.path.join(log_dir, today.strftime("%Y-%m-%d") + "_analysis.txt")
            with open(analysis_path, "w", encoding="utf-8") as f:
                f.write(report_text)
        except Exception:
            pass

        # 앱 로그창 핵심 요약
        summary = (
            f"[AI-ANALYSIS] {today} 전구간 분석완료 — "
            f"매수:{n_buy_total} 매도:{n_sell_total} 체결:{n_filled_total} "
            f"timeout:{n_timeout_total} giveup:{n_giveup} "
            f"차단1위:{top_blocks[0][0] if top_blocks else '-'} "
            f"정규장수신율:{avg_real_mkt:.1f}/s  →  log/{today}_analysis.txt"
        )
        self._emit_log(summary)
        if NEWS_SENTIMENT_AVAILABLE:
            try:
                gemini_path = _append_gemini_daily_log(
                    "DAY-END",
                    f"장후요약 테마={_theme_strength_summary()} | "
                    f"분석: 매수{n_buy_total} 체결{n_filled_total} "
                    f"차단1위={top_blocks[0][0] if top_blocks else '-'}",
                )
                if gemini_path:
                    self._emit_log(
                        f"[NEWS] Gemini일별요약 → log/{today.strftime('%Y-%m-%d')}_gemini_summary.txt"
                    )
            except Exception:
                pass
        self._log_analysis_done_date = today

    # ──────────────────────────────────────────────────────────────────────────
    def _auto_tune_strategy_params(self) -> None:
        """분석 파일을 읽어 strategy_params.json 을 자동 조정하고 git commit 한다.
        _auto_analyze_daily_log() 완료(21:00 이후) 후 하루 1회 실행.
        - '안전' 파라미터(승률 차단, 거래량 필터 등)만 소폭 조정 (±1 step 이내).
        - STOP_LOSS_PCT·DAILY_LOSS_HARD_LIMIT_PCT 등 리스크 핵심 파라미터는 건드리지 않는다.
        - log/YYYY-MM-DD_autofix.txt 에 변경 내역과 이유를 상세 기록.
        - strategy_params.json 변경 후 git add + commit 으로 이력 보존.
        """
        today = datetime.date.today()
        if self._log_auto_tune_done_date == today:
            return
        if self._log_analysis_done_date != today:
            return
        if QTime.currentTime() < QTime(21, 0, 0):
            return

        import json as _json
        import subprocess
        import re as _re
        import collections as _collections

        log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "log")
        analysis_path = os.path.join(log_dir, today.strftime("%Y-%m-%d") + "_analysis.txt")
        json_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "strategy_params.json")

        if not os.path.exists(analysis_path):
            return

        # ── 분석 파일 파싱 ──────────────────────────────────────────────────
        try:
            with open(analysis_path, "r", encoding="utf-8") as _f:
                analysis_text = _f.read()
        except Exception:
            return

        # 매수 차단 사유 TOP 추출
        block_counts: _collections.Counter = _collections.Counter()
        for m in _re.finditer(r"(\d+)\.\s+(.+?):\s+(\d+)회", analysis_text):
            block_counts[m.group(2).strip()] = int(m.group(3))

        # 매도 GIVE-UP 건수
        giveup_count = len(_re.findall(r"GIVE-UP \d+건", analysis_text))

        # 당일 매수 건수
        buy_match = _re.search(r"합계\s+(\d+)건\s+\d+건\s+\d+건", analysis_text)
        n_buy_total = int(buy_match.group(1)) if buy_match else 0

        # ── strategy_params.json 로드 ────────────────────────────────────
        try:
            with open(json_path, "r", encoding="utf-8") as _f:
                sp = _json.load(_f)
        except Exception as e:
            self._emit_log(f"[AUTO-TUNE] strategy_params.json 로드 실패: {e}")
            return

        # 보수 모드(conservative)에서는 완화 방향 자동 조정 금지
        _conservative = str(sp.get("_mode", "")).lower() == "conservative"
        if _conservative:
            self._emit_log("[AUTO-TUNE] 보수 모드 — 완화 방향 자동 조정 건너뜀 (강화 방향만 허용)")


        params = sp.get("params", {})
        changes: list = []  # (param_name, old_val, new_val, reason)

        def _adjust(name: str, direction: int, reason: str) -> None:
            """direction: +1 = 올림(완화 방향), -1 = 내림(강화 방향)
            auto_tune_floor / auto_tune_ceil 필드가 있으면 자동 튜닝의 누적 조정 한계를 제한한다.
            같은 방향으로 최근 5일 내 3회 이상 조정된 경우 추가 조정을 건너뛴다.
            보수 모드(_mode=conservative)에서는 완화 방향(+1) 조정을 금지한다.
            """
            if _conservative and direction > 0:
                return  # 보수 모드: 완화 방향 조정 금지
            if name not in params:
                return
            meta = params[name]
            old_v = meta["value"]
            step = meta.get("step", 1)
            new_v = old_v + direction * step
            # auto_tune_floor/ceil: 자동 튜닝이 건드릴 수 있는 안전 범위
            if direction < 0 and "auto_tune_floor" in meta:
                new_v = max(float(meta["auto_tune_floor"]), new_v)
            if direction > 0 and "auto_tune_ceil" in meta:
                new_v = min(float(meta["auto_tune_ceil"]), new_v)
            new_v = max(meta["min"], min(meta["max"], new_v))
            if new_v == old_v:
                return
            # 최근 5일 내 같은 방향 조정 횟수 확인 (과도한 편향 방지)
            recent = [
                c for c in (sp.get("history") or [])[-5:]
                for c in c.get("changes", [])
                if c.get("param") == name
            ]
            same_dir = sum(
                1 for c in recent
                if (direction < 0 and c.get("to", c.get("from", 0)) < c.get("from", 0))
                or (direction > 0 and c.get("to", c.get("from", 0)) > c.get("from", 0))
            )
            if same_dir >= 3:
                return  # 같은 방향으로 이미 3회 조정 → 건너뜀
            meta["value"] = round(new_v, 6) if isinstance(new_v, float) else new_v
            changes.append((name, old_v, new_v, reason))

        # ── 조정 규칙 ────────────────────────────────────────────────────
        win_rate_block_cnt = block_counts.get("당일 승률 저조", 0)
        strategy_block_cnt = block_counts.get("전략조건 미충족", 0)
        no_price_block_cnt = block_counts.get("현재가 미수신", 0)

        # 규칙 1: 승률 차단이 50회 초과 → 차단 기준 완화 (MIN_SELLS +2)
        if win_rate_block_cnt > 50:
            _adjust("DAILY_WIN_RATE_BLOCK_MIN_SELLS", +1,
                    f"당일 승률 저조 차단 {win_rate_block_cnt}회 → MIN_SELLS 완화")

        # 규칙 2: 승률 차단이 0회이고 매수도 충분히 됐음 → 차단 기준 소폭 강화 (MIN_SELLS -2)
        elif win_rate_block_cnt == 0 and n_buy_total >= 3:
            _adjust("DAILY_WIN_RATE_BLOCK_MIN_SELLS", -1,
                    f"승률 차단 없음 + 매수 {n_buy_total}건 → MIN_SELLS 소폭 강화")

        # 규칙 3: 전략 조건 미충족이 200회 초과 & 매수 0건 → 거래량 필터 완화
        if strategy_block_cnt > 200 and n_buy_total == 0:
            _adjust("VOL_ENTRY_RATIO_MIN", -1,
                    f"전략조건 미충족 {strategy_block_cnt}회+매수0건 → 거래량비율 완화")
            _adjust("VOL_ENTRY_GROWTH_MIN", -1,
                    f"전략조건 미충족 {strategy_block_cnt}회+매수0건 → 거래량증가 완화")
        # 규칙 4: 거래량 필터 이후 매수가 충분히 발생 → 필터 소폭 복원
        elif strategy_block_cnt < 100 and n_buy_total >= 3:
            _adjust("VOL_ENTRY_RATIO_MIN", +1,
                    f"전략조건 차단 {strategy_block_cnt}회+매수{n_buy_total}건 → 거래량비율 소폭 강화")

        # 규칙 5: 현재가 미수신이 500회 초과 → AI 문턱은 내리지 않음(2단계: AI≥0.50 유지)
        if no_price_block_cnt > 500:
            self._emit_log(
                f"[AUTO-TUNE] 현재가 미수신 {no_price_block_cnt}회 — "
                "AI 문턱 완화는 건너뜀(AI_PROBA floor 0.50)"
            )

        # AI 하한 강제(이미 낮아져 있으면 복구)
        if "AI_PROBA_ENTRY_MIN" in params:
            _ai = params["AI_PROBA_ENTRY_MIN"]
            _floor = max(0.50, float(_ai.get("auto_tune_floor", 0.50) or 0.50))
            _ai["auto_tune_floor"] = _floor
            _cur = float(_ai.get("value", _floor) or _floor)
            if _cur < _floor:
                changes.append(("AI_PROBA_ENTRY_MIN", _cur, _floor, f"AI 문턱 하한 복구({_floor})"))
                _ai["value"] = _floor

        # 규칙 6: GIVE-UP 발생 시 MARKET_OPEN_BUY_GUARD 강화 (아침 급변 종목 진입 억제)
        if giveup_count > 0:
            _adjust("MARKET_OPEN_BUY_GUARD_MINUTES", +1,
                    f"매도 GIVE-UP {giveup_count}건 → 장초 매수 금지 강화")

        # ── 변경사항 없으면 종료 ─────────────────────────────────────────
        if not changes:
            self._emit_log(f"[AUTO-TUNE] {today} 파라미터 변경 없음 (현행 설정 유지)")
            self._log_auto_tune_done_date = today
            return

        # ── 히스토리 업데이트 및 저장 ─────────────────────────────────
        history_entry = {
            "date": today.strftime("%Y-%m-%d"),
            "changes": [
                {"param": n, "from": o, "to": nv, "reason": r}
                for n, o, nv, r in changes
            ],
        }
        history: list = sp.get("history", [])
        history.append(history_entry)
        if len(history) > 90:
            history = history[-90:]
        sp["history"] = history
        sp["last_tuned_date"] = today.strftime("%Y-%m-%d")

        try:
            with open(json_path, "w", encoding="utf-8") as _f:
                _json.dump(sp, _f, ensure_ascii=False, indent=2)
        except Exception as e:
            self._emit_log(f"[AUTO-TUNE] strategy_params.json 저장 실패: {e}")
            return

        # ── autofix 로그 파일 작성 ───────────────────────────────────────
        fix_lines = [
            "=" * 64,
            f"  [AUTO-TUNE] {today.strftime('%Y-%m-%d')} 자동 파라미터 조정",
            f"  실행 시각: {datetime.datetime.now().strftime('%H:%M:%S')}",
            "=" * 64,
            "",
            "■ 적용된 변경 사항",
        ]
        for nm, ov, nv, rs in changes:
            note = params.get(nm, {}).get("note", "")
            fix_lines.append(f"  {nm}")
            fix_lines.append(f"    {ov}  →  {nv}    ({rs})")
            if note:
                fix_lines.append(f"    ※ {note}")
        fix_lines += [
            "",
            "■ 분석 근거",
            f"  승률 차단 횟수: {win_rate_block_cnt}",
            f"  전략조건 미충족: {strategy_block_cnt}",
            f"  현재가 미수신: {no_price_block_cnt}",
            f"  당일 매수 건수: {n_buy_total}",
            f"  매도 GIVE-UP: {giveup_count}",
            "",
            "■ 주의사항",
            "  STOP_LOSS_PCT, DAILY_LOSS_HARD_LIMIT_PCT 등 리스크 핵심 파라미터는",
            "  자동 조정에서 제외됩니다. 수동으로 확인하세요.",
            "",
            "=" * 64,
        ]
        fix_text = "\n".join(fix_lines)
        try:
            fix_path = os.path.join(log_dir, today.strftime("%Y-%m-%d") + "_autofix.txt")
            with open(fix_path, "w", encoding="utf-8") as _f:
                _f.write(fix_text)
        except Exception:
            pass

        # ── git commit ──────────────────────────────────────────────────
        commit_msg = (
            f"[auto-tune] {today} 파라미터 자동 조정 "
            + ", ".join(f"{n}:{o}→{nv}" for n, o, nv, _ in changes)
        )
        try:
            repo_dir = os.path.dirname(os.path.abspath(__file__))
            subprocess.run(
                ["git", "add", "strategy_params.json"],
                cwd=repo_dir, capture_output=True, timeout=15
            )
            subprocess.run(
                ["git", "commit", "-m", commit_msg],
                cwd=repo_dir, capture_output=True, timeout=15
            )
            self._emit_log(f"[AUTO-TUNE] git commit 완료: {commit_msg[:80]}")
        except Exception as e:
            self._emit_log(f"[AUTO-TUNE] git commit 실패: {e}")

        # ── 앱 로그 요약 ────────────────────────────────────────────────
        change_summary = ", ".join(f"{n}:{o}→{nv}" for n, o, nv, _ in changes)
        self._emit_log(
            f"[AUTO-TUNE] {today} 파라미터 {len(changes)}개 조정 완료 — {change_summary}"
            f"  →  log/{today}_autofix.txt"
        )
        self._log_auto_tune_done_date = today

    # ──────────────────────────────────────────────────────────────────────────
    def _evaluate_yesterday_strategy(
        self,
        schedule: dict,
        today_analysis_text: str,
    ) -> bool:
        """어제 적용한 전략 패키지가 효과적이었는지 평가한다.
        평가 기준:
          - 'steady_state' 전략은 항상 유지
          - revert_on_loss=True:
              MOCK_LIVE_PARITY=0 모의만: 당일 손실한도 초과 시에만 롤백 (매수 0건은 유지)
              실전 및 모의 패리티: 매수 0건 또는 손실한도 초과 시 롤백
        Returns True if the strategy should be kept, False if it should be reverted.
        """
        import re as _re
        current = schedule.get("current")
        if not current:
            return True

        if current.get("strategy_id") == "steady_state":
            return True

        # 오늘 장중에 적용한 전략은 아직 평가 불가(적용 후 장일 1일 필요)
        applied_date_str = str(current.get("applied_date") or "")
        today_str = datetime.date.today().strftime("%Y-%m-%d")
        if applied_date_str and applied_date_str >= today_str:
            self._emit_log(
                f"[DAILY-STRATEGY] '{current.get('strategy_id')}' "
                f"적용일={applied_date_str} — 당일/미래 적용분 평가 유예"
            )
            return True

        # 오늘 분석에서 매수 건수 파악
        buy_match = _re.search(r"합계\s+(\d+)건\s+\d+건\s+\d+건", today_analysis_text)
        n_buy_today = int(buy_match.group(1)) if buy_match else 0

        daily_loss = "당일 손실한도 초과" in today_analysis_text
        applied_strategy = current.get("strategy_id", "")

        # 적용 다음날: 실전은 0매수/손실 시 롤백. 모의만 0매수 유지(OpenAPI 유지매매).
        if current.get("revert_on_loss", False):
            if daily_loss:
                self._emit_log(
                    f"[DAILY-STRATEGY] '{applied_strategy}' 손실한도 초과 → 롤백 예정"
                )
                return False
            if n_buy_today == 0:
                mock_keep_zero = self._is_mock_trading() and not bool(MOCK_LIVE_PARITY)
                if mock_keep_zero:
                    self._emit_log(
                        f"[DAILY-STRATEGY] '{applied_strategy}' 매수0건 — 모의투자라 "
                        f"롤백하지 않고 유지"
                    )
                else:
                    self._emit_log(
                        f"[DAILY-STRATEGY] '{applied_strategy}' 매수0건 — "
                        f"{'모의=실전규칙' if self._is_mock_trading() else '실전'}이라 롤백 예정"
                    )
                    return False

        self._emit_log(
            f"[DAILY-STRATEGY] '{applied_strategy}' 유지 (매수{n_buy_today}건)"
        )
        return True

    def _select_and_apply_daily_strategy(self) -> None:
        """하루 1개 전략 패키지를 자동 선택·적용한다.
        - _auto_tune_strategy_params() 완료(21:30) 후 하루 1회 실행
        - strategy_library.json 에서 오늘 로그 패턴에 맞는 전략을 선택
        - strategy_params.json 에 반영
        - strategy_schedule.json 에 이력 기록
        - log/YYYY-MM-DD_strategy.txt 로 상세 기록
        - git commit
        """
        today = datetime.date.today()
        if getattr(self, "_daily_strategy_done_date", None) == today:
            return
        if self._log_auto_tune_done_date != today:
            return
        if QTime.currentTime() < QTime(21, 30, 0):
            return

        import json as _json
        import re as _re
        import subprocess

        root = os.path.dirname(os.path.abspath(__file__))
        log_dir = os.path.join(root, "log")
        lib_path = os.path.join(root, "strategy_library.json")
        sched_path = os.path.join(root, "strategy_schedule.json")
        sp_path = os.path.join(root, "strategy_params.json")
        analysis_path = os.path.join(log_dir, today.strftime("%Y-%m-%d") + "_analysis.txt")

        for p in (lib_path, analysis_path, sp_path):
            if not os.path.exists(p):
                self._emit_log(f"[DAILY-STRATEGY] 파일 없음: {p}")
                return

        try:
            with open(lib_path, "r", encoding="utf-8") as f:
                library = _json.load(f)
            with open(sched_path, "r", encoding="utf-8") as f:
                schedule = _json.load(f)
            with open(sp_path, "r", encoding="utf-8") as f:
                sp = _json.load(f)
            with open(analysis_path, "r", encoding="utf-8") as f:
                analysis_text = f.read()
        except Exception as e:
            self._emit_log(f"[DAILY-STRATEGY] 파일 로드 실패: {e}")
            return

        # 오늘 이미 전략을 적용했으면 재시작해도 롤백/재선택하지 않음
        _cur = schedule.get("current") or {}
        _cur_applied = str(_cur.get("applied_date") or "")
        if _cur_applied == today.strftime("%Y-%m-%d") and _cur.get("strategy_id"):
            self._emit_log(
                f"[DAILY-STRATEGY] 오늘 이미 적용됨: [{_cur.get('strategy_id')}] "
                f"{_cur.get('strategy_name', '')} — 유지 (재선택 생략)"
            )
            self._daily_strategy_done_date = today
            return

        # ── 1. 어제 전략 평가 / 롤백 ───────────────────────────────────────
        keep_current = self._evaluate_yesterday_strategy(schedule, analysis_text)
        if not keep_current:
            # 어제 적용한 변경사항 롤백
            prev = schedule.get("current", {})
            prev_params = prev.get("prev_values", {})
            sp_params = sp.get("params", {})
            for k, v in prev_params.items():
                if k in sp_params:
                    sp_params[k]["value"] = v
            self._emit_log(
                f"[DAILY-STRATEGY] 롤백 완료: {list(prev_params.keys())}"
            )
            # 히스토리에 실패로 기록
            h_entry = dict(prev)
            h_entry["result"] = "reverted"
            schedule.setdefault("history", []).append(h_entry)
            schedule["current"] = None

        # 완화 패키지 유지 시 재선택 금지 — 같은 VOL delta가 매일 겹쳐 쌓이는 루프 방지.
        # steady_state 는 revert_on_loss=False 이므로 여기 안 걸리고 오늘 패턴으로 새 패키지를 고른다.
        _keep_pack = schedule.get("current") or {}
        if (
            keep_current
            and self._is_mock_trading()
            and not bool(MOCK_LIVE_PARITY)
            and bool(_keep_pack.get("revert_on_loss"))
            and str(_keep_pack.get("strategy_id") or "") not in ("", "steady_state")
        ):
            sid = str(_keep_pack.get("strategy_id") or "")
            sname = str(_keep_pack.get("strategy_name") or "")
            self._emit_log(
                f"[DAILY-STRATEGY] 완화패키지 유지: [{sid}] {sname} — "
                f"모의투자 재선택 생략(실전은 재선택, 손실한도 초과 시에만 롤백)"
            )
            sep = "=" * 64
            keep_lines = [
                sep,
                f"  [DAILY-STRATEGY] {today.strftime('%Y-%m-%d')} 일일 전략 업데이트",
                f"  실행 시각: {datetime.datetime.now().strftime('%H:%M:%S')}",
                sep,
                "",
                f"■ 유지: [{sid}] {sname}",
                "  재선택하지 않음 — 모의투자 전용. 실전 세션에서는 이 경로를 타지 않음",
                "  롤백 조건(모의): 당일 손실한도 초과 시에만 (매수 0건은 유지)",
                "",
                sep,
                "",
            ]
            strategy_log_path = os.path.join(
                log_dir, today.strftime("%Y-%m-%d") + "_strategy.txt"
            )
            try:
                with open(strategy_log_path, "w", encoding="utf-8") as f:
                    f.write("\n".join(keep_lines))
            except Exception:
                pass
            self._daily_strategy_done_date = today
            return

        # ── 2. 오늘 패턴 분석 ────────────────────────────────────────────
        block_counts: dict = {}
        for m in _re.finditer(r"\d+\.\s+(.+?):\s+(\d+)회", analysis_text):
            block_counts[m.group(1).strip()] = int(m.group(2))

        win_rate_block = block_counts.get("당일 승률 저조", 0)
        strategy_block = block_counts.get("전략조건 미충족", 0)
        no_price_block = block_counts.get("현재가 미수신", 0)
        daily_loss_block = block_counts.get("당일 손실한도 초과", 0)

        giveup_match = _re.search(r"GIVE-UP (\d+)건", analysis_text)
        giveup_count = int(giveup_match.group(1)) if giveup_match else 0

        timeout_match = _re.search(r"timeout 총 (\d+)회", analysis_text)
        timeout_count = int(timeout_match.group(1)) if timeout_match else 0

        buy_match = _re.search(r"합계\s+(\d+)건\s+\d+건\s+\d+건", analysis_text)
        n_buy = int(buy_match.group(1)) if buy_match else 0

        # 상세 차단 사유(로그) — "전략조건 미충족"만으로는 VOL/MA/RSI 구분이 안 됨
        reason_scores = {"vol": 0, "ma": 0, "rsi": 0, "ai": 0}
        try:
            day_log = os.path.join(log_dir, today.strftime("%Y-%m-%d") + ".txt")
            if os.path.isfile(day_log):
                with open(day_log, "r", encoding="utf-8", errors="replace") as _lf:
                    for _line in _lf:
                        if "전략조건 미충족" not in _line and "[BUY-BLOCK]" not in _line:
                            continue
                        if "거래량 평균비 부족" in _line or "전일대비 거래량 부족" in _line:
                            reason_scores["vol"] += 1
                        if "MA20 대비" in _line:
                            reason_scores["ma"] += 1
                        if "RSI" in _line:
                            reason_scores["rsi"] += 1
                        if "AI" in _line and ("확률" in _line or "상승확률" in _line):
                            reason_scores["ai"] += 1
        except Exception:
            pass
        top_reason = ""
        if any(reason_scores.values()):
            top_reason = max(reason_scores, key=lambda k: reason_scores[k])
            self._emit_log(
                f"[DAILY-STRATEGY] 상세차단 vol={reason_scores['vol']} "
                f"ma={reason_scores['ma']} rsi={reason_scores['rsi']} "
                f"ai={reason_scores['ai']} → top={top_reason or '-'}"
            )

        # 최근 적용된 전략 ID 목록 (중복 방지)
        # reverted 된 전략은 다시 후보에 넣는다(원인 재발 시 재적용 필요).
        # kept 만 중복 제외. reverted/superseded 는 원인 재발 시 재적용 가능.
        recent_ids = {
            h.get("strategy_id")
            for h in (schedule.get("history") or [])[-7:]
            if h.get("result") == "kept"
        }

        # ── 3. 전략 선택 ──────────────────────────────────────────────────
        ctx = {
            "win_rate_block": win_rate_block,
            "strategy_block": strategy_block,
            "no_price_block": no_price_block,
            "daily_loss_block": daily_loss_block,
            "giveup_count": giveup_count,
            "timeout_count": timeout_count,
            "n_buy": n_buy,
        }

        def _matches(trigger: str) -> bool:
            if trigger == "default":
                return True
            try:
                return bool(eval(  # noqa: S307
                    trigger,
                    {"__builtins__": {}},
                    ctx,
                ))
            except Exception:
                return False

        _conservative_mode = str(sp.get("_mode", "")).lower() == "conservative"
        # 2단계: 일일전략/자동완화가 AI 문턱을 0.50 미만으로 내리지 못함
        _ai_floor = 0.50
        try:
            _ai_meta = (sp.get("params") or {}).get("AI_PROBA_ENTRY_MIN") or {}
            _ai_floor = float(_ai_meta.get("auto_tune_floor", _ai_floor) or _ai_floor)
        except Exception:
            _ai_floor = 0.50
        _ai_floor = max(0.50, _ai_floor)

        def _strategy_allowed(strat: dict) -> bool:
            intent = str(strat.get("intent", "") or "").lower()
            if not _conservative_mode:
                return True
            if intent in ("tighten", "neutral", ""):
                return True
            if intent == "relax":
                return bool(strat.get("allow_in_conservative", False))
            return True

        def _blocked_by_recent(strat: dict) -> bool:
            sid = strat.get("id")
            if sid not in recent_ids:
                return False
            # 오늘 top 차단 사유와 맞는 전략은 recent kept 여도 재허용
            prefer = set(strat.get("prefer_reasons") or [])
            if top_reason and top_reason in prefer:
                return False
            return True

        regime_label = self._market_regime_label()
        evidence: dict = {}
        if _shadow is not None:
            try:
                evidence = _shadow.evidence_summary(SHADOW_EVIDENCE_LOOKBACK_DAYS)
            except Exception:
                evidence = {}
        ev_cats = evidence.get("categories") or {}

        revert_streaks: dict = {}
        _closed: set = set()
        _fatigue_cutoff = (today - datetime.timedelta(days=STRATEGY_FATIGUE_WINDOW_DAYS)).strftime("%Y-%m-%d")
        for h in reversed(schedule.get("history") or []):
            sid = str(h.get("strategy_id") or "")
            if not sid or sid in _closed:
                continue
            if str(h.get("applied_date") or "") < _fatigue_cutoff:
                continue
            if h.get("result") == "reverted":
                revert_streaks[sid] = revert_streaks.get(sid, 0) + 1
            else:
                _closed.add(sid)

        def _ev_sign(cat: str) -> int:
            """오답노트 확정 근거: +1 완화 근거, -1 차단이 손실 회피, 0 표본 부족·중립."""
            ev = ev_cats.get(cat) or {}
            if int(ev.get("n", 0) or 0) < SHADOW_EVIDENCE_MIN_N:
                return 0
            avg = float(ev.get("avg", 0.0) or 0.0)
            if avg >= SHADOW_EVIDENCE_MIN_EDGE:
                return 1
            return -1 if avg < 0 else 0

        def _learned_block(strat: dict) -> str:
            sid = str(strat.get("id") or "")
            intent = str(strat.get("intent", "") or "").lower()
            if intent == "relax" and regime_label == "bear":
                return "약세장 — 완화 금지"
            streak = int(revert_streaks.get(sid, 0))
            if streak >= STRATEGY_FATIGUE_REVERTS:
                return f"{streak}회 연속 되돌림 — 효과 없음"
            prefer = [str(c) for c in (strat.get("prefer_reasons") or [])]
            neg = [c for c in prefer if _ev_sign(c) < 0]
            if intent == "relax" and neg:
                ev_txt = ", ".join(
                    f"{c} n={int(ev_cats[c]['n'])} 평균{float(ev_cats[c]['avg'])*100:+.2f}%"
                    for c in neg
                )
                return f"오답노트: 차단이 손실 회피 ({ev_txt})"
            return ""

        learned_excluded: dict = {}
        candidates = []
        for s in library.get("strategies", []):
            if (
                s.get("id") == "steady_state"
                or _blocked_by_recent(s)
                or not _matches(s.get("trigger", "default"))
                or not _strategy_allowed(s)
            ):
                continue
            why = _learned_block(s)
            if why:
                learned_excluded[str(s.get("id"))] = why
                continue
            candidates.append(s)
        for sid, why in learned_excluded.items():
            self._emit_log(f"[DAILY-STRATEGY] 학습 제외 [{sid}] {why}")

        def _cand_sort_key(s: dict) -> tuple:
            # 오답노트 + 근거 > 상세 사유 일치 > priority
            prefer = set(s.get("prefer_reasons") or [])
            ev_boost = 200 if any(_ev_sign(c) > 0 for c in prefer) else 0
            reason_boost = 100 if (top_reason and top_reason in prefer) else 0
            return (ev_boost + reason_boost, int(s.get("priority", 0) or 0))

        candidates.sort(key=_cand_sort_key, reverse=True)

        if _conservative_mode:
            self._emit_log(
                "[DAILY-STRATEGY] 보수 모드 — 허용된 완화(allow_in_conservative)만 포함"
            )

        # 추론 우선: Gemini가 후보 중 고르면 규칙 우선순위보다 앞세움
        gemini_pick = None
        if bool(STRATEGY_REASONING_FIRST) and candidates and NEWS_SENTIMENT_AVAILABLE:
            self._emit_log(
                f"[DAILY-STRATEGY] Gemini추론 시도 (후보 {len(candidates)}개, "
                f"top_reason={top_reason or '-'})"
            )
            try:
                gemini_pick = _gemini_pick_daily_strategy(
                    candidates, ctx, top_reason=top_reason,
                    regime=regime_label, evidence=evidence,
                    revert_streaks=revert_streaks, excluded=learned_excluded,
                )
            except Exception as e:
                self._emit_log(f"[DAILY-STRATEGY] Gemini 추론 실패: {e}")
                gemini_pick = None
            if not gemini_pick:
                self._emit_log("[DAILY-STRATEGY] Gemini추론 결과 없음 — 규칙 우선순위로 선택")
                if NEWS_SENTIMENT_AVAILABLE:
                    _append_gemini_daily_log(
                        "STRATEGY",
                        "결과없음 — 규칙 우선순위로 선택 "
                        f"(후보={len(candidates)} top={top_reason or '-'})",
                    )
        elif bool(STRATEGY_REASONING_FIRST):
            self._emit_log(
                "[DAILY-STRATEGY] Gemini추론 건너뜀 "
                f"(후보={len(candidates)} news={NEWS_SENTIMENT_AVAILABLE})"
            )
        if gemini_pick and gemini_pick.get("strategy_id"):
            gid = str(gemini_pick["strategy_id"])
            for i, s in enumerate(candidates):
                if str(s.get("id")) == gid:
                    chosen = candidates.pop(i)
                    candidates.insert(0, chosen)
                    self._emit_log(
                        f"[DAILY-STRATEGY] Gemini추론 우선: [{gid}] "
                        f"{gemini_pick.get('reason', '')}"
                    )
                    if NEWS_SENTIMENT_AVAILABLE:
                        _append_gemini_daily_log(
                            "STRATEGY",
                            f"선택 [{gid}] | {gemini_pick.get('reason', '')}",
                        )
                    break

        if candidates:
            chosen = candidates[0]
        else:
            chosen = next(
                (s for s in library["strategies"] if s["id"] == "steady_state"),
                {"id": "steady_state", "name": "안정 기조 유지", "params": {},
                 "revert_on_loss": False}
            )

        # ── 4. 파라미터 적용 ──────────────────────────────────────────────
        sp_params = sp.get("params", {})
        applied_changes: dict = {}
        prev_values: dict = {}

        for pname, meta in (chosen.get("params") or {}).items():
            if pname not in sp_params:
                continue
            p = sp_params[pname]
            old_v = p["value"]
            delta = float(meta.get("delta", 0))
            # AI 문턱은 완화(내림) 금지 — 강화(올림)만 허용
            if pname == "AI_PROBA_ENTRY_MIN" and delta < 0:
                self._emit_log(
                    f"[DAILY-STRATEGY] AI_PROBA 완화 delta={delta} 무시 (floor={_ai_floor})"
                )
                continue
            new_v = old_v + delta
            new_v = max(p.get("min", new_v), min(p.get("max", new_v), new_v))
            if pname == "AI_PROBA_ENTRY_MIN":
                new_v = max(float(_ai_floor), float(new_v))
            new_v = round(new_v, 6) if isinstance(new_v, float) else int(new_v)
            if new_v == old_v:
                continue
            prev_values[pname] = old_v
            sp_params[pname]["value"] = new_v
            applied_changes[pname] = (old_v, new_v)

        # 현재 AI가 floor 미만이면 즉시 복구
        if "AI_PROBA_ENTRY_MIN" in sp_params:
            cur_ai = float(sp_params["AI_PROBA_ENTRY_MIN"].get("value", _ai_floor) or _ai_floor)
            if cur_ai < float(_ai_floor):
                prev_values.setdefault("AI_PROBA_ENTRY_MIN", cur_ai)
                sp_params["AI_PROBA_ENTRY_MIN"]["value"] = float(_ai_floor)
                applied_changes["AI_PROBA_ENTRY_MIN"] = (cur_ai, float(_ai_floor))
                self._emit_log(
                    f"[DAILY-STRATEGY] AI_PROBA {cur_ai} → {_ai_floor} 복구(하한)"
                )

        # ── 5. 스케줄 업데이트 및 저장 ───────────────────────────────────
        new_current = {
            "strategy_id": chosen["id"],
            "strategy_name": chosen.get("name", ""),
            "applied_date": today.strftime("%Y-%m-%d"),
            "revert_on_loss": chosen.get("revert_on_loss", False),
            "prev_values": prev_values,
            "changes": {k: {"from": v[0], "to": v[1]} for k, v in applied_changes.items()},
            "context": ctx,
        }
        if keep_current and schedule.get("current"):
            prev_cur = dict(schedule["current"])
            prev_cur["result"] = "kept"
            schedule.setdefault("history", []).append(prev_cur)

        schedule["current"] = new_current
        history = schedule.get("history", [])
        if len(history) > 90:
            history = history[-90:]
        schedule["history"] = history

        try:
            with open(sp_path, "w", encoding="utf-8") as f:
                _json.dump(sp, f, ensure_ascii=False, indent=2)
            with open(sched_path, "w", encoding="utf-8") as f:
                _json.dump(schedule, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self._emit_log(f"[DAILY-STRATEGY] 저장 실패: {e}")
            return

        # ── 6. 상세 로그 파일 작성 ───────────────────────────────────────
        sep = "=" * 64
        log_lines = [
            sep,
            f"  [DAILY-STRATEGY] {today.strftime('%Y-%m-%d')} 일일 전략 업데이트",
            f"  실행 시각: {datetime.datetime.now().strftime('%H:%M:%S')}",
            sep,
            "",
            f"■ 선택된 전략: [{chosen['id']}] {chosen.get('name', '')}",
            f"  설명: {chosen.get('description', '')}",
            f"  트리거: {chosen.get('trigger', '')}",
            f"  Gemini추론: "
            + (
                f"우선선택 [{gemini_pick.get('strategy_id')}] {gemini_pick.get('reason', '')}"
                if gemini_pick
                else (
                    "건너뜀 (후보 없음 — 학습 제외로 안정 유지)" if not candidates
                    else ("시도했으나 미선택/실패" if bool(STRATEGY_REASONING_FIRST) else "비활성")
                )
            ),
            "",
            "■ 오늘 분석 수치",
            f"  승률 차단: {win_rate_block}회  /  전략조건 미충족: {strategy_block}회",
            f"  현재가 미수신: {no_price_block}회  /  손실한도 초과: {daily_loss_block}회",
            f"  GIVE-UP: {giveup_count}건  /  매도 timeout: {timeout_count}회",
            f"  당일 매수: {n_buy}건",
            f"  상세차단: vol={reason_scores['vol']} ma={reason_scores['ma']} "
            f"rsi={reason_scores['rsi']} ai={reason_scores['ai']} top={top_reason or '-'}",
            "",
            "■ 학습 근거",
            f"  시장 국면: {regime_label or '-'}",
            f"  오답노트 확정 {int(evidence.get('n_total', 0) or 0)}건 "
            f"(최근 {SHADOW_EVIDENCE_LOOKBACK_DAYS}일, 판단 기준 n≥{SHADOW_EVIDENCE_MIN_N})",
        ]
        _ev_label = {
            1: "완화 근거",
            -1: "차단 유지(손실 회피)",
            0: f"판단 보류(n<{SHADOW_EVIDENCE_MIN_N} 또는 평균<+{SHADOW_EVIDENCE_MIN_EDGE*100:.1f}%)",
        }
        for cat in ("vol", "ma", "rsi", "ai"):
            ev = ev_cats.get(cat)
            if not ev:
                continue
            log_lines.append(
                f"    {cat:<4} n={int(ev.get('n', 0)):>4}  승률 {float(ev.get('win', 0))*100:>3.0f}%  "
                f"평균 {float(ev.get('avg', 0))*100:+.2f}%  → {_ev_label[_ev_sign(cat)]}"
            )
        if revert_streaks:
            log_lines.append(
                "  연속 되돌림: "
                + ", ".join(f"{k}×{v}" for k, v in sorted(revert_streaks.items(), key=lambda kv: -kv[1]))
            )
        if learned_excluded:
            log_lines.append("  학습 제외:")
            log_lines += [f"    [{k}] {v}" for k, v in learned_excluded.items()]
        log_lines += [
            "",
            "■ 파라미터 변경",
        ]
        if applied_changes:
            for pname, (ov, nv) in applied_changes.items():
                note = sp_params.get(pname, {}).get("note", "")
                log_lines.append(f"  {pname}: {ov} → {nv}  ({note})")
        else:
            log_lines.append("  변경 없음 (이미 경계값이거나 steady_state)")

        log_lines += [
            "",
            "■ 내일 평가 기준",
            f"  revert_on_loss={chosen.get('revert_on_loss', False)} — "
            + (
                "모의: 손실한도 초과 시에만 롤백 / 실전: 매수 0건 또는 손실 시 롤백"
                if chosen.get("revert_on_loss")
                else "성과 무관 유지"
            ),
            "",
            "■ 최근 7일 전략 이력",
        ]
        for h in (schedule.get("history") or [])[-7:]:
            log_lines.append(
                f"  {h.get('applied_date','-')} [{h.get('strategy_id','-')}] "
                f"{h.get('strategy_name','')} → {h.get('result','pending')}"
            )
        log_lines += ["", sep, ""]

        strategy_log_path = os.path.join(log_dir, today.strftime("%Y-%m-%d") + "_strategy.txt")
        try:
            with open(strategy_log_path, "w", encoding="utf-8") as f:
                f.write("\n".join(log_lines))
        except Exception:
            pass

        # ── 7. git commit ──────────────────────────────────────────────────
        change_msg = (
            ", ".join(f"{k}:{v[0]}→{v[1]}" for k, v in applied_changes.items())
            if applied_changes else "파라미터 변경 없음"
        )
        commit_msg = (
            f"[daily-strategy] {today} [{chosen['id']}] {chosen.get('name','')} — {change_msg}"
        )
        try:
            subprocess.run(
                ["git", "add", "strategy_params.json", "strategy_schedule.json"],
                cwd=root, capture_output=True, timeout=15
            )
            subprocess.run(
                ["git", "commit", "-m", commit_msg],
                cwd=root, capture_output=True, timeout=15
            )
            self._emit_log(f"[DAILY-STRATEGY] git commit: {commit_msg[:90]}")
        except Exception as e:
            self._emit_log(f"[DAILY-STRATEGY] git commit 실패: {e}")

        self._emit_log(
            f"[DAILY-STRATEGY] {today} 전략 적용 완료: [{chosen['id']}] {chosen.get('name','')} "
            f"→ log/{today}_strategy.txt"
        )
        self._daily_strategy_done_date = today

    def _maybe_prefetch_news(self) -> None:
        """30분마다 보유·관심 종목 뉴스를 백그라운드 스레드로 미리 가져온다."""
        if not NEWS_SENTIMENT_AVAILABLE:
            return
        NEWS_PREFETCH_INTERVAL_SEC = 1800.0  # 30분
        now = datetime.datetime.now()
        if self._last_news_prefetch_at is not None:
            if (now - self._last_news_prefetch_at).total_seconds() < NEWS_PREFETCH_INTERVAL_SEC:
                return
        # 이전 스레드가 아직 돌고 있으면 건너뜀
        if self._news_prefetch_thread is not None and self._news_prefetch_thread.is_alive():
            return

        codes = list(dict.fromkeys(
            list((self.positions or {}).keys()) + list(self.stock_codes or [])
        ))
        if not codes:
            return

        self._last_news_prefetch_at = now

        def _run() -> None:
            try:
                prefetch_news_batch(codes)
                summary = _news_cache_summary()
                if len(summary) > 120:
                    summary = summary[:117] + "..."
                self._emit_log(
                    f"[NEWS] 감성갱신 n={len(codes)} | Gemini {_gemini_quota_status()} | {summary}"
                )
                day = datetime.date.today().strftime("%Y-%m-%d")
                gemini_path = os.path.join(
                    os.path.dirname(os.path.abspath(__file__)),
                    "log",
                    f"{day}_gemini_summary.txt",
                )
                if os.path.isfile(gemini_path):
                    self._emit_log(
                        f"[NEWS] Gemini요약 파일: log/{day}_gemini_summary.txt"
                    )
            except Exception as e:
                self._emit_log(f"[NEWS] prefetch 오류: {e}")

        self._news_prefetch_thread = threading.Thread(target=_run, daemon=True)
        self._news_prefetch_thread.start()

    def _maybe_prefetch_themes(self) -> None:
        """테마 강세(반도체·조선·원자력·금융·방산 등) Gemini 추론 주기 갱신."""
        if not NEWS_SENTIMENT_AVAILABLE or not bool(_THEME_STRENGTH_ENABLED):
            return
        now = datetime.datetime.now()
        if self._last_theme_prefetch_at is not None:
            if (now - self._last_theme_prefetch_at).total_seconds() < float(
                THEME_PREFETCH_INTERVAL_SEC
            ):
                return
        if self._theme_prefetch_thread is not None and self._theme_prefetch_thread.is_alive():
            return
        self._last_theme_prefetch_at = now

        def _run() -> None:
            try:
                _refresh_theme_strength(force=True)
                self._emit_log(f"[THEME] {_theme_strength_summary()}")
            except Exception as e:
                self._emit_log(f"[THEME] 갱신 오류: {e}")

        self._theme_prefetch_thread = threading.Thread(target=_run, daemon=True)
        self._theme_prefetch_thread.start()

    def _maybe_periodic_holdings_sync(self) -> None:
        """60초마다 또는 체결 직후 플래그 시 잔고 TR 예약 (on_tick에서는 TR 직접 호출 안 함)."""
        HOLDINGS_SYNC_INTERVAL_SEC = 60.0
        now = datetime.datetime.now()
        due = (
            self._last_periodic_holdings_sync_at is None
            or (now - self._last_periodic_holdings_sync_at).total_seconds()
            >= HOLDINGS_SYNC_INTERVAL_SEC
        )
        if not (self._need_portfolio_sync or due):
            return
        self._schedule_holdings_sync(kind="periodic", delay_ms=50)

    def _schedule_holdings_sync(self, *, kind: str, delay_ms: int = 50) -> bool:
        """잔고 TR을 QTimer로 예약. 성공 시 True."""
        if getattr(self, "_holdings_sync_scheduled", False) or getattr(
            self, "_holdings_sync_active", False
        ):
            return False
        if self._kiwoom_tr_busy():
            return False
        self._holdings_sync_kind = str(kind or "periodic")
        self._holdings_sync_scheduled = True
        QTimer.singleShot(int(delay_ms), self._run_holdings_sync_deferred)
        return True

    def _run_holdings_sync_deferred(self) -> None:
        """QTimer: opw00018 잔고 TR — on_tick 밖에서 수행해 STALL 방지."""
        self._holdings_sync_scheduled = False
        if getattr(self, "_holdings_sync_active", False):
            return
        if (
            getattr(self, "_ai_training_active", False)
            or getattr(self, "_regime_update_active", False)
            or getattr(self, "_strat_ohlcv_warm_active", False)
        ):
            self._holdings_sync_scheduled = True
            QTimer.singleShot(400, self._run_holdings_sync_deferred)
            return
        kind = str(getattr(self, "_holdings_sync_kind", "periodic") or "periodic")
        self._holdings_sync_active = True
        try:
            sync_ok = True
            try:
                self.sync_portfolio(
                    password=self._account_password if hasattr(self, "_account_password") else ""
                )
            except Exception:
                sync_ok = False
            now = datetime.datetime.now()
            if kind == "periodic":
                self._need_portfolio_sync = False
                self._last_periodic_holdings_sync_at = now
            elif kind == "reconcile" and sync_ok:
                self._reconcile_pending_orders_after_sync()
        finally:
            self._holdings_sync_active = False

    def _maybe_train_ai_model(self) -> None:
        """
        모델이 없거나 오래되면 하루 1회 재학습을 예약한다.
        실제 학습(키움 일봉 TR)은 on_tick 밖에서 QTimer로 돌려 매매 루프 STALL을 막는다.
        정규장 중에는 예약하지 않는다.
        """
        if not AI_AVAILABLE:
            return
        needs_retrain = bool(getattr(self, "_ai_needs_retrain", False))
        if self.ai_model is not None and not needs_retrain:
            return
        today = datetime.date.today()
        if getattr(self, "_ai_train_attempted_date", None) == today:
            return
        if getattr(self, "_ai_train_scheduled", False) or getattr(self, "_ai_training_active", False):
            return
        if self.market_session_name() == "정규장":
            return
        self._ai_train_scheduled = True
        phase = self._market_phase_tag()
        self._emit_log(
            f"[AI] 재학습 예약(매매루프 비차단) phase={phase}, "
            f"실전라벨={'ON' if AI_USE_LIVE_TRADE_LABELS else 'OFF'}"
            f"{', stale교체' if needs_retrain else ''}"
        )
        QTimer.singleShot(300, self._run_ai_train_deferred)

    def _run_ai_train_deferred(self) -> None:
        """QTimer 콜백: 키움 TR이 포함된 학습을 on_tick 밖에서 수행."""
        self._ai_train_scheduled = False
        if getattr(self, "_ai_training_active", False):
            return
        if not AI_AVAILABLE:
            return
        today = datetime.date.today()
        if getattr(self, "_ai_train_attempted_date", None) == today:
            return
        needs_retrain = bool(getattr(self, "_ai_needs_retrain", False))
        if self.ai_model is not None and not needs_retrain:
            return
        if self.market_session_name() == "정규장":
            return
        self._ai_train_attempted_date = today
        self._ai_training_active = True
        phase = self._market_phase_tag()
        try:
            self._emit_log(
                f"[AI] 재학습 시작 (deferred/{phase}, 실전라벨="
                f"{'ON' if AI_USE_LIVE_TRADE_LABELS else 'OFF'}"
                f"{', stale교체' if needs_retrain else ''})"
            )
            self.train_random_forest()
        finally:
            self._ai_training_active = False

    def on_tick(self):
        self._last_tick_at = time.monotonic()
        # 키움 일봉 TR(재학습·국면·OHLCV캐시)이 메인스레드에서 돌 때: 하트비트만
        if self._kiwoom_tr_busy():
            return
        today = datetime.date.today()
        self._maybe_reset_daily_state(today)

        # 연결 끊김 시각 추적 (UI 표시용 — 재연결 시도 없음, 앱 재시작으로 처리)
        try:
            st = int(self.kiwoom.dynamicCall("GetConnectState()") or 0)
        except Exception:
            st = 0
        if st == 0:
            if self._disconnected_at is None:
                self._disconnected_at = datetime.datetime.now()
        else:
            self._disconnected_at = None

        if not self._is_market_open():
            self._sync_holding_current_prices(interval_sec=30.0)
            self._maybe_periodic_holdings_sync()
            self._emit_daily_report_if_needed()
            self._auto_analyze_daily_log()
            self._auto_tune_strategy_params()
            self._select_and_apply_daily_strategy()
            self._maybe_run_shadow_scoring()
            self._maybe_write_reasoning_report()
            # 장외에도 AI 재학습 허용 (피처 스키마 변경 후 익일 오전까지 빈 모델로 남는 문제 방지)
            self._maybe_train_ai_model()
            return

        self._update_market_halt_state()
        self._maybe_recover_realtime_feed()
        if self._halt_active:
            self._reconcile_orders_and_portfolio()
            return

        self._maybe_start_auto_backtest()

        # 체결확인 -> 포트폴리오 업데이트
        self._reconcile_orders_and_portfolio()
        self._maybe_refresh_ma_step()
        self._sync_holding_current_prices()

        self._maybe_update_market_regime_from_kiwoom()
        sm = self.strategy_manager
        if sm is not None and self.stock_codes:
            # 장 시작 후 1회는 강제 평가 (국면이 키움으로 먼저 잡혀도 학습행 쌓기)
            force_eval = not self._strat_force_eval_done
            self._warm_strategy_ohlcv_cache(force=force_eval)
            if force_eval:
                self._strat_force_eval_done = True
            sm.schedule_reevaluation(self.stock_codes, force=force_eval)

        self._maybe_periodic_holdings_sync()
        self._maybe_prefetch_news()
        self._maybe_prefetch_themes()
        self._maybe_emit_buy_diagnostics()

        # AI 모델은 하루 1회만(시장 상황에 따라 바꾸고 싶으면 여기 튜닝)
        self._maybe_train_ai_model()

        # 실시간 루프: 가격 기반 손절/익절 우선, 그 다음 MA 기반 신규매수
        for code in self.stock_codes:
            try:
                reason = self._sell_reason(code)
                if reason:
                    self.sell_market(code, reason=reason)
            except Exception:
                continue

        for code in self.stock_codes:
            try:
                if self._should_quality_avg_down(code):
                    add_qty = self._quality_avg_down_qty(code)
                    if add_qty <= 0:
                        continue
                    n = int(self._quality_avg_down_count.get(code, 0) or 0) + 1
                    self.buy_market(code, qty_override=add_qty, avg_down=True)
                    self._emit_log(
                        f"[AVG-DOWN] {code} qty={add_qty} "
                        f"회차={n}/{int(QUALITY_AVG_DOWN_MAX)}"
                    )
                    break
            except Exception:
                continue

        for code in self.stock_codes:
            try:
                if self._should_scale_in(code):
                    add_qty = self._scale_in_order_qty(code)
                    if add_qty <= 0:
                        continue
                    self.buy_market(code, qty_override=add_qty)
                    self._scale_in_done[code] = True
                    self._emit_log(f"[SCALE-IN] {code} qty={add_qty}")
                    break
            except Exception:
                continue

        for _score, code in self._ranked_buy_candidates()[:1]:
            try:
                self.buy_market(code)
                self._emit_log(f"[BUY-SCORE] {code} score={_score:.3f}")
                break
            except Exception:
                continue

    def start(self, account_password: str = ""):
        self._account_password = account_password
        self.kiwoom.on_chejan_data = self._on_kiwoom_chejan
        if AI_AVAILABLE and self.ai_model is None:
            loaded = self._load_ai_model_from_disk(allow_stale=False)
            if not loaded:
                # 유효기간 초과여도 일단 로드해 장중 공백 방지 → 재학습은 백그라운드로
                loaded = self._load_ai_model_from_disk(allow_stale=True)
            force_flag = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "ai_force_retrain.flag")
            if loaded and os.path.exists(force_flag):
                try:
                    os.remove(force_flag)
                except OSError:
                    pass
                self._ai_needs_retrain = True
                self._emit_log("[AI] 강제 재학습 플래그 감지 — 기존 모델 유지하며 장외 deferred 재학습")
            if not loaded:
                self._emit_log("[AI] 디스크 모델 없음/불일치 — 장외 deferred 재학습 예정")
            elif getattr(self, "_ai_needs_retrain", False):
                self._emit_log("[AI] 오래된 모델 임시 사용 중 — 장외 deferred 재학습 예약됨")
        # 시작 시 잔고만 동기화( MA는 라운드로빈으로 점진 갱신 )
        self.sync_portfolio(password=account_password)
        today = datetime.date.today()
        self._refresh_mock_server_flag()
        if self._is_mock_trading():
            if bool(MOCK_LIVE_PARITY):
                self._emit_log(
                    "[MODE] 모의투자 — 실전과 동일 규칙 (거래량 AND·AI 품질가산·"
                    f"VOL 하한 {LIVE_VOL_ENTRY_FLOOR:.2f}·약세 하드블록). "
                    "유지매매 완화 없음."
                )
            else:
                self._emit_log(
                    "[MODE] 모의투자 — 가끔매매 완화(AI 하드 0.50·소프트 "
                    f"{MOCK_KEEP_AI_SOFT_CAP:.2f}·거래량 OR(trend·volume)·VOL 상한 "
                    f"{MOCK_KEEP_VOL_CAP:.2f}·약세는 눌림만 비중×{MOCK_BEAR_SIZE_MULT:.2f}) "
                    "적용. 실전 로그인 시에는 꺼집니다."
                )
        else:
            self._emit_log(
                "[MODE] 실전 — 가끔매매 완화 미적용. 거래량 AND·AI 품질가산·"
                f"VOL 하한 {LIVE_VOL_ENTRY_FLOOR:.2f} 유지."
            )
        if NEWS_SENTIMENT_AVAILABLE:
            self._emit_log(f"[NEWS] {_news_gemini_status_line()}")
        # 장외·스캔 전이라도 시총 우량허용 집합을 미리 바인딩 (눌림/물타기·FEATURE 로그용)
        try:
            mc_n = max(0, int(SCAN_QUALITY_MC_TOP_N))
            if mc_n > 0:
                al = _mc_top_allowlist_for_scan(mc_n)
                if al:
                    self.set_scan_quality_context(True, al)
                    self._emit_log(
                        f"[BOOT] 시총 우량허용 {len(al)}종 로드 "
                        f"(quality_dip={'ON' if QUALITY_DIP_ENABLED else 'OFF'} "
                        f"물타기×{int(QUALITY_AVG_DOWN_MAX) if QUALITY_AVG_DOWN_ENABLED else 0})"
                    )
                else:
                    self._emit_log("[BOOT] 시총 우량허용 미확보 — 스캔 시 재시도")
        except Exception as e:
            self._emit_log(f"[BOOT] 시총 우량허용 로드 실패: {e}")
        self._emit_feature_verify_log()
        # 부팅 직후 테마 강세 1회 비동기 갱신
        if NEWS_SENTIMENT_AVAILABLE and bool(_THEME_STRENGTH_ENABLED):
            self._last_theme_prefetch_at = None  # 즉시 허용
            self._maybe_prefetch_themes()
        self._last_ma_refresh_date = today
        self._need_portfolio_sync = False  # start에서 이미 sync 했으므로

        # 오늘 MA 갱신은 점진적으로 수행
        self._ma_needed_codes = set(self.stock_codes)
        self._ma_refresh_idx = 0
        self._next_ma_refresh_at = datetime.datetime.now()

        # 실시간 등록: "주식체결" fid 10(현재가)
        self.kiwoom.set_real_reg(self.stock_codes, fid_list="10")
        self._log_stock_state_summary()
        self._log_market_calendar_today()

        self._last_tick_at = time.monotonic()
        self.timer.start()
        self._start_tick_stall_watchdog()

    def _start_tick_stall_watchdog(self) -> None:
        """on_tick 이 TICK_STALL_SEC 이상 다시 시작되지 않으면 전 스레드 스택을 log/*_stall.txt 로 남긴다."""
        if getattr(self, "_stall_watchdog_started", False):
            return
        self._stall_watchdog_started = True

        def _run() -> None:
            dumped_for = None
            while True:
                time.sleep(30)
                last = getattr(self, "_last_tick_at", None)
                if last is None:
                    continue
                gap = time.monotonic() - last
                if gap < TICK_STALL_SEC:
                    dumped_for = None
                    continue
                if dumped_for == last:
                    continue
                dumped_for = last
                try:
                    names = {t.ident: t.name for t in threading.enumerate()}
                    now = datetime.datetime.now()
                    lines = [f"[{now:%H:%M:%S}] on_tick 정지 {gap:.0f}s — 전 스레드 스택"]
                    for ident, frame in sys._current_frames().items():
                        lines.append(f"\n--- thread {names.get(ident, '?')} ({ident}) ---")
                        lines += [l.rstrip() for l in traceback.format_stack(frame)]
                    path = os.path.join(
                        os.path.dirname(os.path.abspath(__file__)), "log", f"{now:%Y-%m-%d}_stall.txt"
                    )
                    with open(path, "a", encoding="utf-8") as f:
                        f.write("\n".join(lines) + "\n\n")
                    self._emit_log(
                        f"[STALL] on_tick {gap:.0f}s 정지 — 스레드 스택 기록 → log/{now:%Y-%m-%d}_stall.txt "
                        f"(매매 루프 멈춤: 앱 재시작 필요)"
                    )
                except Exception as e:
                    self._emit_log(f"[STALL] 스택 기록 실패: {e}")

        threading.Thread(target=_run, daemon=True, name="tick-stall-watchdog").start()

    def update_targets(self, codes: List[str]) -> None:
        """스캔 결과 종목으로 매매 대상을 런타임에 갱신.

        2026-07-07: 여기서 스캔 결과로 stock_codes를 통째로 덮어써서, 스캔 후보에
        없는 보유종목(예: 023910)이 실시간 시세 등록(SetRealReg)과 손절/익절 감시
        루프(_sell_reason)에서 통째로 빠져버리는 버그가 있었다. 023910이 하루 종일
        시세 1틱만 받고 이후 전혀 갱신되지 않아, +6%대 수익에도 익절이 전혀 체크되지
        않았던 원인. 보유중인 종목은 스캔 결과에 없어도 항상 유지되도록 병합한다.
        """
        scanned = list(dict.fromkeys([str(c).strip() for c in codes if str(c).strip()]))
        held = [str(c).strip() for c in self.positions.keys() if str(c).strip()]
        ordered = list(dict.fromkeys(scanned + held))
        self.stock_codes = ordered
        self.pending_buy = {c: False for c in ordered}
        self.pending_sell = {c: False for c in ordered}
        for c in ordered:
            if c not in self.tp_reached:
                self.tp_reached[c] = False
        self._ma_needed_codes = set(ordered)
        self._ma_refresh_idx = 0
        self._next_ma_refresh_at = datetime.datetime.now()

        # 현재가 선반영은 GUI 쪽 백그라운드 프리패치에서 처리(대량 종목 시 UI 정지 방지)

        self.kiwoom.set_real_reg(self.stock_codes, fid_list="10")
        self._log_stock_state_summary()
        self._maybe_start_auto_backtest(force=True)
        if self.on_log is not None:
            try:
                self.on_log(f"[TARGET] 대상 갱신 {len(self.stock_codes)}종목: {self.stock_codes}")
            except Exception:
                pass

    def maybe_keepalive(self) -> bool:
        """
        30분 주기로 경량 keep-alive를 수행하고, 성공 여부를 반환.
        """
        if not self._is_market_open():
            return False
        now = datetime.datetime.now()
        if self.last_keepalive_at is not None:
            gap = (now - self.last_keepalive_at).total_seconds()
            if gap < KEEPALIVE_INTERVAL_MIN * 60:
                return False
        try:
            _ = self.kiwoom.dynamicCall("GetConnectState()")
            self.last_keepalive_at = now
            return True
        except Exception:
            return False

def attach_trade_logging(engine: TradingEngine, tracker: TradeTracker) -> None:
    """체결 알림 → TradeTracker.append (CSV 자동 저장)."""

    def _on_fill(payload: Dict[str, object]) -> None:
        ts = payload.get("ts")
        if not isinstance(ts, datetime.datetime):
            ts = None
        tracker.append(
            code=str(payload.get("code", "")),
            side=str(payload.get("side", "BUY")),
            price=float(payload.get("price", 0)),
            qty=int(payload.get("qty", 0)),
            ts=ts,
            pnl_pct=payload.get("pnl_pct"),
            realized_pnl_krw=payload.get("realized_pnl_krw"),
            sell_reason=str(payload.get("sell_reason", "") or ""),
        )

    engine.on_trade_fill = _on_fill
    engine.trade_tracker = tracker


class TradingWindow(QMainWindow):
    scan_finished = pyqtSignal(list, bool)
    scan_message = pyqtSignal(str)
    log_message = pyqtSignal(str)
    _prefetch_ui_tick = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.engine: Optional[TradingEngine] = None
        self.kiwoom: Optional[KiwoomOpenAPI] = None
        self.overseas_api: Optional[KiwoomOverseasOpenAPI] = None
        self.trade_tracker: Optional[TradeTracker] = None
        self.force_scan_anytime: bool = FORCE_SCAN_ANYTIME
        self.request_scan_now: Optional[Callable[[], None]] = None
        self.request_scan_now_force: Optional[Callable[[], None]] = None
        self._dashboard: Optional[PerformanceDashboard] = None
        self.account_no: str = "-"
        self.code_names: Dict[str, str] = {}
        self.setWindowTitle("AI 스톡 프로그램")
        self.resize(1100, 760)
        self.setStyleSheet(
            """
            QMainWindow { background: #f6f8fb; }
            QLabel { color: #222; font-size: 12px; }
            QPushButton {
                background: #2f6fed; color: white; border: none; border-radius: 8px;
                padding: 8px 14px; font-weight: 600;
            }
            QPushButton:hover { background: #2459be; }
            QTableWidget {
                background: white; border: 1px solid #d7dce5; border-radius: 8px;
                gridline-color: #edf1f7; font-size: 12px;
            }
            QHeaderView::section {
                background: #eef3ff; color: #1f2a44; padding: 6px; border: none;
                border-bottom: 1px solid #d7dce5; font-weight: 700;
            }
            QPlainTextEdit {
                background: #0f172a; color: #d1fae5; border-radius: 8px;
                border: 1px solid #22314d; font-family: Consolas, 'Courier New', monospace;
                font-size: 12px;
            }
            """
        )

        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        row1 = QHBoxLayout()
        self.lbl_status = QLabel("상태: 준비")
        self.lbl_market = QLabel("장시간: -")
        self.lbl_auto_start = QLabel("자동시작: ON")
        self.lbl_server = QLabel("서버상태: -")
        self.lbl_account = QLabel("계좌번호: -")
        self.lbl_limit = QLabel(f"총한도액: {int(TOTAL_LIMIT_KRW):,}원")
        self.lbl_est_asset = QLabel(f"추정예탁(키움): {int(TOTAL_LIMIT_KRW):,}원")
        self.lbl_opw_pnl = QLabel("계좌손익(키움): —")
        self.lbl_total_ret = QLabel("계좌수익률(키움): —")
        self.lbl_total_pnl = QLabel("앱매매 손익(실현+평가): 0원")
        self.lbl_app_ret = QLabel("앱매매 실현수익률: 0.00%")
        for w in (
            self.lbl_status,
            self.lbl_market,
            self.lbl_auto_start,
            self.lbl_server,
            self.lbl_account,
            self.lbl_limit,
        ):
            w.setStyleSheet("background:#ffffff; border:1px solid #dce3ef; border-radius:6px; padding:4px 8px;")
        row1.addWidget(self.lbl_status)
        row1.addWidget(self.lbl_market)
        row1.addWidget(self.lbl_auto_start)
        row1.addWidget(self.lbl_server)
        row1.addWidget(self.lbl_account)
        row1.addWidget(self.lbl_limit)
        row1.addStretch(1)
        self.lbl_credit = QLabel("Programed by 이창용")
        self.lbl_credit.setStyleSheet("font-size:11px; color:#94a3b8; font-weight:300;")
        row1.addWidget(self.lbl_credit)

        row2 = QHBoxLayout()
        self.lbl_conn_time = QLabel("서버연결시간: 00:00:00")
        self.lbl_keepalive = QLabel("KeepAlive(30분): -")
        self.lbl_positions = QLabel("보유종목: 0")
        self.lbl_pending_buy = QLabel("매수미체결: 0")
        self.lbl_pending_sell = QLabel("매도미체결: 0")
        self.lbl_waiting = QLabel("진입대기: 0")
        _chip = "background:#ffffff; border:1px solid #dce3ef; border-radius:6px; padding:4px 8px;"
        for w in (
            self.lbl_conn_time,
            self.lbl_keepalive,
            self.lbl_positions,
            self.lbl_waiting,
        ):
            w.setStyleSheet(_chip)
        self.btn_backtest = QPushButton("백테스트")
        self.btn_stop = QPushButton("정지")
        self.btn_force_scan = QPushButton("")
        self.btn_strategy_report = QPushButton("전략리포트")
        self.btn_backtest.setFixedHeight(28)
        self.btn_backtest.setFixedWidth(112)
        self.btn_backtest.setStyleSheet("font-size:11px; padding:4px 6px; font-weight:600;")
        self.btn_stop.setFixedHeight(28)
        self.btn_stop.setFixedWidth(112)
        self.btn_stop.setStyleSheet("font-size:11px; padding:4px 6px; font-weight:600;")
        self.btn_force_scan.setFixedHeight(28)
        self.btn_force_scan.setFixedWidth(112)
        self.btn_force_scan.setStyleSheet("font-size:11px; padding:4px 6px; font-weight:600;")
        self.btn_strategy_report.setFixedHeight(28)
        self.btn_strategy_report.setFixedWidth(112)
        self.btn_strategy_report.setStyleSheet("font-size:11px; padding:4px 6px; font-weight:600;")
        self.btn_backtest.clicked.connect(self._run_backtest)
        self.btn_stop.clicked.connect(self._toggle_engine)
        self.btn_force_scan.clicked.connect(self._toggle_force_scan)
        self.btn_strategy_report.clicked.connect(self._show_strategy_report)
        self._sync_force_scan_button_text()
        row2.addWidget(self.lbl_conn_time)
        row2.addWidget(self.lbl_keepalive)
        row2_pos_wait = QVBoxLayout()
        row2_pos_wait.setSpacing(2)
        row2_pos_wait.setContentsMargins(0, 0, 0, 0)
        row2_pos_wait.addWidget(self.lbl_positions, 0, Qt.AlignLeft)
        row2_pos_wait.addWidget(self.lbl_waiting, 0, Qt.AlignLeft)
        row2.addLayout(row2_pos_wait)
        row2.addStretch(1)
        self.btn_dashboard = QPushButton("수익 대시보드")
        self.btn_dashboard.setFixedHeight(28)
        self.btn_dashboard.setFixedWidth(112)
        self.btn_dashboard.setStyleSheet("font-size:11px; padding:4px 6px; font-weight:600;")
        self.btn_dashboard.clicked.connect(self._open_dashboard)
        row2.addWidget(self.btn_force_scan)
        row2.addWidget(self.btn_strategy_report)
        row2.addWidget(self.btn_backtest)
        row2.addWidget(self.btn_dashboard)
        row2.addWidget(self.btn_stop)

        # 전략/시장/AI/스캔 — 가로 과확장 방지용 2줄
        self.lbl_strategy = QLabel("전략: -")
        self.lbl_regime = QLabel("시장: -")
        self.lbl_ai_status = QLabel("AI: -")
        self.lbl_scan_quality = QLabel("스캔: -")
        for w in (
            self.lbl_strategy,
            self.lbl_regime,
            self.lbl_ai_status,
            self.lbl_scan_quality,
        ):
            w.setStyleSheet(_chip)
            w.setWordWrap(True)
            w.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
            w.setMinimumWidth(0)
        row_strat_a = QHBoxLayout()
        row_strat_a.setSpacing(6)
        row_strat_a.addWidget(self.lbl_strategy, 1)
        row_strat_a.addWidget(self.lbl_regime, 1)
        row_strat_b = QHBoxLayout()
        row_strat_b.setSpacing(6)
        row_strat_b.addWidget(self.lbl_ai_status, 2)
        row_strat_b.addWidget(self.lbl_scan_quality, 1)
        row_strat = QVBoxLayout()
        row_strat.setSpacing(4)
        row_strat.setContentsMargins(0, 0, 0, 0)
        row_strat.addLayout(row_strat_a)
        row_strat.addLayout(row_strat_b)

        row_mid = QHBoxLayout()
        for w in (
            self.lbl_est_asset,
            self.lbl_opw_pnl,
            self.lbl_total_ret,
            self.lbl_total_pnl,
            self.lbl_app_ret,
        ):
            w.setStyleSheet("background:#ffffff; border:1px solid #dce3ef; border-radius:6px; padding:4px 8px;")
            row_mid.addWidget(w)
        row_mid.addStretch(1)

        layout.addLayout(row1)
        layout.addLayout(row_mid)
        layout.addLayout(row2)
        layout.addLayout(row_strat)

        self.tabs = QTabWidget()
        self.domestic_tab = QWidget()
        self.overseas_tab = QWidget()
        self.domestic_layout = QVBoxLayout(self.domestic_tab)
        self.domestic_layout.setContentsMargins(0, 8, 0, 0)
        self.domestic_layout.setSpacing(8)
        self.overseas_layout = QVBoxLayout(self.overseas_tab)
        self.overseas_layout.setContentsMargins(0, 8, 0, 0)
        self.overseas_layout.setSpacing(8)
        self.tabs.addTab(self.domestic_tab, "국내주식")
        self.tabs.addTab(self.overseas_tab, "해외주식")

        self.lbl_codes = QLabel("종목·업체명: -")
        self.lbl_scan = QLabel("스캔요약: 전체 - | 1차 - | 2차 - | 최종 -")
        self.lbl_scan_progress = QLabel("스캔상태: 대기")
        self.lbl_hint = QLabel("로그인 후 자동매매 엔진이 시작됩니다.")
        self.lbl_codes.setStyleSheet("font-size:13px; font-weight:600; color:#1f2a44;")
        self.lbl_scan.setStyleSheet("font-size:12px; color:#3a4a6b;")
        self.lbl_scan_progress.setStyleSheet("font-size:12px; color:#6b7280;")
        self.lbl_pending_buy.setStyleSheet("background:#ffffff; border:1px solid #dce3ef; border-radius:6px; padding:3px 8px;")
        self.lbl_pending_sell.setStyleSheet("background:#ffffff; border:1px solid #dce3ef; border-radius:6px; padding:3px 8px;")
        self.lbl_hint.setStyleSheet("font-size:12px; color:#4b5563;")
        self.lbl_codes.setVisible(False)
        self.lbl_hint.setVisible(False)
        row_scan = QHBoxLayout()
        row_scan.setSpacing(8)
        row_scan.addWidget(self.lbl_scan)
        row_scan.addWidget(self.lbl_scan_progress)
        row_scan.addStretch(1)
        row_scan.addStretch(5)
        self.domestic_layout.addLayout(row_scan)
        self.domestic_layout.addWidget(self.lbl_hint)

        row_lists = QHBoxLayout()
        row_lists.setSpacing(0)
        row_lists.setContentsMargins(0, 0, 0, 0)
        self.tbl_holdings = QTableWidget(0, 5)
        self.tbl_holdings.setHorizontalHeaderLabels(
            ["보유종목", "매입가", "현재가", "수량", "수익"]
        )
        self.tbl_holdings.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.tbl_holdings.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.tbl_holdings.horizontalHeader().setDefaultAlignment(Qt.AlignCenter)
        self.tbl_holdings.verticalHeader().setDefaultSectionSize(24)
        self.tbl_holdings.setMinimumHeight(360)
        self.tbl_waiting = QTableWidget(0, 4)
        self.tbl_waiting.setHorizontalHeaderLabels(["진입종목", "현재가", "상태", "비고"])
        self.tbl_waiting.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.tbl_waiting.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.tbl_waiting.horizontalHeader().setDefaultAlignment(Qt.AlignCenter)
        _wh0 = self.tbl_waiting.horizontalHeaderItem(0)
        if _wh0 is not None:
            _wh0.setTextAlignment(int(Qt.AlignLeft | Qt.AlignVCenter))
        self.tbl_waiting.verticalHeader().setDefaultSectionSize(24)
        self.tbl_waiting.setMinimumHeight(360)
        self.tbl_orders = QTableWidget(0, 7)
        self.tbl_orders.setHorizontalHeaderLabels(
            ["종목", "매수가", "체결가", "주문상태", "수익", "체결/잔량", "비고"]
        )
        self.tbl_orders.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.tbl_orders.horizontalHeader().setSectionResizeMode(6, QHeaderView.Stretch)
        self.tbl_orders.horizontalHeader().setDefaultAlignment(Qt.AlignCenter)
        self.tbl_orders.verticalHeader().setDefaultSectionSize(24)
        self.tbl_orders.setMinimumHeight(360)
        table_style = (
            "font-size:11px;"
            "QHeaderView::section {"
            "padding:0px;"
            "margin:0px;"
            "}"
        )
        for t in (self.tbl_holdings, self.tbl_waiting, self.tbl_orders):
            t.setStyleSheet(table_style)
            t.setTextElideMode(Qt.ElideNone)
        row_lists.addWidget(self.tbl_holdings, 2)
        row_lists.addWidget(self.tbl_waiting, 2)
        row_lists.addWidget(self.tbl_orders, 2)
        self.domestic_layout.addLayout(row_lists)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(180)
        self.domestic_layout.addWidget(self.log)

        self._build_overseas_tab()
        layout.addWidget(self.tabs)

        # 진입대기 변화 기록 파일(재실행 후에도 확인 가능)
        self._waiting_log_path = os.path.join(os.getcwd(), "waiting_status_log.csv")
        self._last_waiting_snapshot: Optional[tuple] = None
        self._last_waiting_log_at: Optional[datetime.datetime] = None
        self._last_low_price_drop_count: int = -1
        # 누적 타깃 관리(최근 관측 시각 기반 TTL/상한 정리)
        self._target_last_seen: Dict[str, datetime.date] = {}
        # yfinance 현재가 백그라운드 프리패치(스캔 중 재갱신에도 중단 없이 누적 처리)
        self._price_prefetch_running = False
        self._price_prefetch_queue: List[str] = []
        # 실시간/캐시 모두 비는 종목용 키움 기준가 캐시
        self._master_last_price_cache: Dict[str, int] = {}
        self._last_real_stats_log_at: Optional[datetime.datetime] = None
        self._last_holdings_verify_log_at: Optional[datetime.datetime] = None
        self._last_zero_target_warn_at: Optional[datetime.datetime] = None
        self._last_zero_real_warn_at: Optional[datetime.datetime] = None
        self._last_table_shape: Optional[tuple] = None
        self._last_waiting_fp: Optional[tuple] = None
        self._last_holding_fp: Optional[tuple] = None
        self._last_order_fp: Optional[tuple] = None
        self._last_column_resize_at: Optional[datetime.datetime] = None
        self._last_target_table_refresh_at: Optional[datetime.datetime] = None
        self._last_after_hours_log_at: Dict[str, datetime.datetime] = {}
        self._after_hours_log_counts: Dict[str, int] = {}
        self._prefetch_ui_tick.connect(self.refresh_ui)

        self._ui_timer = QTimer(self)
        self._ui_timer.setInterval(1000)
        self._ui_timer.timeout.connect(self.refresh_ui)
        self._ui_timer.start()
        self._overseas_ui_timer = QTimer(self)
        self._overseas_ui_timer.setInterval(30_000)
        self._overseas_ui_timer.timeout.connect(self._refresh_overseas_status)
        self._overseas_ui_timer.start()
        self.scan_finished.connect(self._on_scan_finished)
        self.scan_message.connect(self._on_scan_message)
        self.log_message.connect(self._append_log_ui)

    def _build_overseas_tab(self) -> None:
        row_top = QHBoxLayout()
        self.lbl_overseas_status = QLabel("해외 API: 확인 중")
        self.lbl_overseas_account = QLabel(f"해외계좌: {DEFAULT_OVERSEAS_ACCOUNT_NO}")
        self.lbl_overseas_market = QLabel("해외장: -")
        self.lbl_overseas_strategy = QLabel("해외전략: 준비")
        for w in (
            self.lbl_overseas_status,
            self.lbl_overseas_account,
            self.lbl_overseas_market,
            self.lbl_overseas_strategy,
        ):
            w.setStyleSheet("background:#ffffff; border:1px solid #dce3ef; border-radius:6px; padding:4px 8px;")
            row_top.addWidget(w)
        row_top.addStretch(1)
        self.btn_overseas_login = QPushButton("해외 로그인 확인")
        self.btn_overseas_login.setFixedHeight(28)
        self.btn_overseas_login.setFixedWidth(128)
        self.btn_overseas_login.setStyleSheet("font-size:11px; padding:4px 6px; font-weight:600;")
        self.btn_overseas_login.clicked.connect(self._check_overseas_login)
        row_top.addWidget(self.btn_overseas_login)
        self.overseas_layout.addLayout(row_top)

        self.lbl_overseas_hint = QLabel(
            "해외주식 탭은 국내 계좌와 분리된 영역입니다. 현재는 KFOPENAPI 연결 상태와 별도 보유/주문 표시 영역을 준비합니다."
        )
        self.lbl_overseas_hint.setStyleSheet("font-size:12px; color:#4b5563;")
        self.overseas_layout.addWidget(self.lbl_overseas_hint)

        row_tables = QHBoxLayout()
        row_tables.setSpacing(0)
        self.tbl_overseas_holdings = QTableWidget(0, 6)
        self.tbl_overseas_holdings.setHorizontalHeaderLabels(
            ["보유종목", "통화", "매입가", "현재가", "수량", "수익"]
        )
        self.tbl_overseas_waiting = QTableWidget(0, 5)
        self.tbl_overseas_waiting.setHorizontalHeaderLabels(
            ["진입종목", "시장", "현재가", "상태", "비고"]
        )
        self.tbl_overseas_orders = QTableWidget(0, 7)
        self.tbl_overseas_orders.setHorizontalHeaderLabels(
            ["종목", "매수가", "체결가", "주문상태", "수익", "체결/잔량", "비고"]
        )
        for table in (
            self.tbl_overseas_holdings,
            self.tbl_overseas_waiting,
            self.tbl_overseas_orders,
        ):
            table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
            table.horizontalHeader().setSectionResizeMode(table.columnCount() - 1, QHeaderView.Stretch)
            table.horizontalHeader().setDefaultAlignment(Qt.AlignCenter)
            table.verticalHeader().setDefaultSectionSize(24)
            table.setMinimumHeight(360)
            table.setStyleSheet("font-size:11px;")
            table.setTextElideMode(Qt.ElideNone)
        row_tables.addWidget(self.tbl_overseas_holdings, 2)
        row_tables.addWidget(self.tbl_overseas_waiting, 2)
        row_tables.addWidget(self.tbl_overseas_orders, 2)
        self.overseas_layout.addLayout(row_tables)

        self.overseas_log = QPlainTextEdit()
        self.overseas_log.setReadOnly(True)
        self.overseas_log.setMinimumHeight(180)
        self.overseas_layout.addWidget(self.overseas_log)

        try:
            self.overseas_api = KiwoomOverseasOpenAPI()
            if self.overseas_api.available:
                self.lbl_overseas_status.setText("해외 API: KFOPENAPI 로드됨")
                self._append_overseas_log("[OVERSEAS] KFOPENAPI 컨트롤 로드 완료")
            else:
                self.lbl_overseas_status.setText("해외 API: 로드 실패")
                self._append_overseas_log(f"[OVERSEAS] {self.overseas_api.last_error}")
        except Exception as e:
            self.overseas_api = None
            self.lbl_overseas_status.setText("해외 API: 초기화 오류")
            self._append_overseas_log(f"[OVERSEAS] KFOPENAPI 초기화 오류: {e}")
        self._refresh_overseas_status()

    def _refresh_overseas_status(self) -> None:
        session = _us_market_session_name()
        open_txt = "운용가능" if _is_us_regular_market_open() else "정규장 아님"
        self.lbl_overseas_market.setText(f"미국장: {session} ({open_txt})")
        if bool(US_STOCK_REGULAR_ONLY):
            self.lbl_overseas_strategy.setText("해외전략: 정규장만 운용")

    def _append_overseas_log(self, msg: str) -> None:
        try:
            ts = datetime.datetime.now().strftime("%H:%M:%S")
            self.overseas_log.appendPlainText(f"[{ts}] {msg}")
        except Exception:
            pass

    def _check_overseas_login(self) -> None:
        if self.overseas_api is None:
            try:
                self.overseas_api = KiwoomOverseasOpenAPI()
            except Exception as e:
                self._append_overseas_log(f"[OVERSEAS] KFOPENAPI 초기화 실패: {e}")
                return
        if not bool(getattr(self.overseas_api, "available", False)):
            self._append_overseas_log("[OVERSEAS] KFOPENAPI 컨트롤이 로드되지 않았습니다.")
            self.lbl_overseas_status.setText("해외 API: 로드 실패")
            return

        self.btn_overseas_login.setEnabled(False)
        self.lbl_overseas_status.setText("해외 API: 로그인 확인 중")
        self._append_overseas_log("[OVERSEAS] 로그인/계좌 확인 시작")
        try:
            accounts = self.overseas_api.login(timeout_ms=int(OVERSEAS_LOGIN_TIMEOUT_MS))
            target = str(DEFAULT_OVERSEAS_ACCOUNT_NO).strip()
            found = any(a.replace("-", "") == target.replace("-", "") or a == target for a in accounts)
            self.lbl_overseas_status.setText("해외 API: 로그인 확인 완료")
            self.lbl_overseas_account.setText(f"해외계좌: {target} ({'확인됨' if found else '목록 없음'})")
            self._append_overseas_log(f"[OVERSEAS] 계좌목록: {accounts if accounts else '-'}")
            try:
                self._append_overseas_log(f"[OVERSEAS] LoginInfo: {self.overseas_api.login_info_snapshot()}")
            except Exception:
                pass
            if found:
                self._append_overseas_log(f"[OVERSEAS] 대상 해외계좌 확인됨: {target}")
            else:
                self._append_overseas_log(f"[OVERSEAS] 대상 해외계좌가 목록에 없습니다: {target}")
        except Exception as e:
            self.lbl_overseas_status.setText("해외 API: 로그인 실패")
            self._append_overseas_log(f"[OVERSEAS] 로그인 확인 실패: {e}")
        finally:
            self.btn_overseas_login.setEnabled(True)

    def set_engine(
        self,
        engine: TradingEngine,
        account_no: str,
        kiwoom: KiwoomOpenAPI,
        trade_tracker: TradeTracker,
    ) -> None:
        self.engine = engine
        self.kiwoom = kiwoom
        self.trade_tracker = trade_tracker
        self.engine.on_log = self._append_log
        sm = getattr(self.engine, "strategy_manager", None)
        if sm is not None:
            sm.set_emit_log(self._append_log)
        # start()는 on_log 연결 전에 실행되므로, GUI에 시총/기능확인 로그를 여기서 한 번 더 노출
        try:
            al_n = len(getattr(self.engine, "_quality_mcap_allowlist", set()) or set())
            if al_n > 0:
                self._append_log(
                    f"[BOOT] 시총 우량허용 {al_n}종 바인딩됨 "
                    f"(quality_dip={'ON' if QUALITY_DIP_ENABLED else 'OFF'} "
                    f"물타기×{int(QUALITY_AVG_DOWN_MAX) if QUALITY_AVG_DOWN_ENABLED else 0})"
                )
            else:
                mc_n = max(0, int(SCAN_QUALITY_MC_TOP_N))
                if mc_n > 0:
                    al = _mc_top_allowlist_for_scan(mc_n)
                    if al:
                        self.engine.set_scan_quality_context(True, al)
                        self._append_log(
                            f"[BOOT] 시총 우량허용 {len(al)}종 로드 "
                            f"(quality_dip={'ON' if QUALITY_DIP_ENABLED else 'OFF'} "
                            f"물타기×{int(QUALITY_AVG_DOWN_MAX) if QUALITY_AVG_DOWN_ENABLED else 0})"
                        )
            if NEWS_SENTIMENT_AVAILABLE:
                self._append_log(f"[NEWS] {_news_gemini_status_line()}")
            self._append_log(
                f"[BOOT] 실효 VOL={self.engine._effective_vol_entry_ratio_min():.2f} "
                f"우량허용={self.engine._effective_vol_entry_ratio_min(next(iter(self.engine._quality_mcap_allowlist), None)):.2f} "
                f"(json VOL={float(VOL_ENTRY_RATIO_MIN):.2f}, "
                f"floor={float(LIVE_VOL_ENTRY_FLOOR):.2f}, "
                f"parity={'ON' if MOCK_LIVE_PARITY else 'OFF'})"
            )
            self.engine._emit_feature_verify_log()
        except Exception as e:
            self._append_log(f"[BOOT] 시총/기능확인 로그 재출력 실패: {e}")
        self.account_no = account_no
        self.lbl_account.setText(f"계좌번호: {account_no}")
        self.code_names = {}
        pairs: List[str] = []
        for c in engine.stock_codes:
            try:
                name = kiwoom.get_master_code_name(c)
            except Exception:
                name = c
            self.code_names[c] = name
            pairs.append(f"{c} {name}")
        self.lbl_codes.setText("종목·업체명: " + (", ".join(pairs) if pairs else "-"))
        self.lbl_scan.setText(f"스캔요약: 전체 - | 1차/2차 통과는 로그 참고 | 최종 {len(engine.stock_codes)}")
        self.lbl_hint.setText("자동매매 엔진 실행 중입니다.")
        self._seed_prices_from_cache(list(engine.stock_codes))
        self._append_log("[INFO] 엔진 시작 및 GUI 연결 완료")
        self.refresh_ui()
        self._schedule_price_prefetch(list(self.engine.stock_codes))

    def set_targets(self, codes: List[str], kiwoom: KiwoomOpenAPI) -> None:
        if self.engine is None:
            return
        filtered_codes: List[str] = []
        dropped_low = 0
        for c in list(codes or []):
            code = str(c).strip()
            if not code:
                continue
            held_qty = int(((self.engine.positions or {}).get(code, {}) or {}).get("qty", 0) or 0)
            if held_qty > 0:
                filtered_codes.append(code)
                continue
            px = int(self.engine.current_price.get(code, 0) or 0)
            if px <= 0:
                mv = self.engine.ma.get(code, {})
                px = int(float(mv.get("close_today", 0) or 0))
            if px <= 0:
                px = self._get_cached_close_price(code)
            if px <= 0:
                px = self._get_kiwoom_master_last_price(code)
            if px > 0 and (
                float(px) < float(MIN_ENTRY_PRICE) or float(px) > float(MAX_ENTRY_PRICE)
            ):
                dropped_low += 1
                continue
            filtered_codes.append(code)

        if dropped_low > 0:
            self._append_log(
                f"[SCAN] 가격범위 제외({int(MIN_ENTRY_PRICE):,}~{int(MAX_ENTRY_PRICE):,}원 밖): {dropped_low}종목"
            )

        self.engine.update_targets(filtered_codes)
        self.code_names = {}
        pairs: List[str] = []
        for c in self.engine.stock_codes:
            try:
                name = kiwoom.get_master_code_name(c)
            except Exception:
                name = c
            self.code_names[c] = name
            pairs.append(f"{c} {name}")
        self.lbl_codes.setText("종목·업체명: " + (", ".join(pairs) if pairs else "-"))
        self.lbl_scan.setText(
            f"스캔요약: 전체 - | 1차/2차 통과는 로그 참고 | 최종 {len(self.engine.stock_codes)}"
        )
        seeded = self._seed_prices_from_cache(list(self.engine.stock_codes))
        if seeded > 0:
            self._append_log(f"[PRICE] 캐시 종가 즉시 반영 {seeded}종목")
        if self.engine.stock_codes:
            self.lbl_hint.setText(f"조건검색 선정 {len(self.engine.stock_codes)}종목: {self.engine.stock_codes}")
            self._append_log(f"[SCAN] 최종 선정 {len(self.engine.stock_codes)}종목: {self.engine.stock_codes}")
        else:
            self.lbl_hint.setText("조건검색 결과 없음 (현재 매수 대상 비어 있음)")
            self._append_log("[SCAN] 조건검색 결과 없음")
        self.refresh_ui()
        self._schedule_price_prefetch(list(self.engine.stock_codes))

    def _schedule_price_prefetch(self, codes: Optional[List[str]] = None) -> None:
        """실시간가가 비어 있을 때 yfinance 종가를 백그라운드로 채움(UI 스레드 블로킹 없음)."""
        if self.engine is None:
            return
        engine_ref = self.engine
        base = list(codes if codes is not None else (engine_ref.stock_codes or []))
        need = [str(c).strip() for c in base if str(c).strip()]
        if not need:
            return

        queued = set(self._price_prefetch_queue)
        for c in need:
            if int(engine_ref.current_price.get(c, 0) or 0) > 0:
                continue
            if c in queued:
                continue
            self._price_prefetch_queue.append(c)
            queued.add(c)
        if not self._price_prefetch_queue:
            return
        if self._price_prefetch_running:
            return
        self._price_prefetch_running = True

        def worker() -> None:
            filled = 0
            try:
                while self._price_prefetch_queue:
                    c = self._price_prefetch_queue.pop(0)
                    try:
                        if int(engine_ref.current_price.get(c, 0) or 0) > 0:
                            continue
                        d = _safe_yf_ohlcv_filtered(c, min_len=2)
                        if d is None:
                            continue
                        _tk, closes, _vols = d
                        if not closes:
                            continue
                        px = int(float(closes[-1]))
                        if px > 0:
                            engine_ref.current_price[c] = px
                            engine_ref.current_price_source[c] = "YF_PREFETCH"
                            filled += 1
                            if filled % 12 == 0:
                                self._prefetch_ui_tick.emit()
                    except Exception:
                        pass
                    time.sleep(0.04)
                self._prefetch_ui_tick.emit()
            finally:
                self._price_prefetch_running = False
                if self._price_prefetch_queue:
                    self._schedule_price_prefetch([])

        threading.Thread(target=worker, daemon=True, name="yf-price-prefetch").start()

    def _get_cached_close_price(self, code: str) -> int:
        """스캔/프리패치에서 이미 받은 yfinance 캐시 종가를 즉시 조회(추가 네트워크 호출 없음)."""
        try:
            for tk in _to_yf_candidates(code):
                with _SCAN_OHLCV_CACHE_LOCK:
                    c = _SCAN_OHLCV_CACHE.get(tk)
                if c is None or len(c) < 2:
                    continue
                df = c[1]
                if df is None or getattr(df, "empty", True):
                    continue
                if "Close" not in df.columns:
                    continue
                closes = [float(x) for x in df["Close"].dropna().tolist()]
                if not closes:
                    continue
                px = int(closes[-1])
                if px > 0:
                    return px
        except Exception:
            pass
        return 0

    def _seed_prices_from_cache(self, codes: Optional[List[str]] = None) -> int:
        """대상 종목의 현재가가 비어 있으면 yfinance 캐시 종가를 즉시 주입."""
        if self.engine is None:
            return 0
        base = list(codes if codes is not None else (self.engine.stock_codes or []))
        filled = 0
        for code in base:
            try:
                if int(self.engine.current_price.get(code, 0) or 0) > 0:
                    continue
                px = self._get_cached_close_price(code)
                if px > 0:
                    self.engine.current_price[code] = px
                    self.engine.current_price_source[code] = "YF_CACHE"
                    filled += 1
            except Exception:
                continue
        return filled

    def _get_kiwoom_master_last_price(self, code: str) -> int:
        """키움 기준가 조회(GetMasterLastPrice)로 현재가 대체."""
        if self.kiwoom is None:
            return 0
        clean = str(code).replace(".KS", "").replace(".KQ", "").strip()
        if len(clean) != 6 or not clean.isdigit():
            return 0
        cached = int(self._master_last_price_cache.get(clean, 0) or 0)
        if cached > 0:
            return cached
        try:
            raw = self.kiwoom.dynamicCall("GetMasterLastPrice(QString)", clean)
            px = abs(int(str(raw or "").strip().replace(",", "")))
            if px > 0:
                self._master_last_price_cache[clean] = px
                return px
        except Exception:
            return 0
        return 0

    def _on_scan_finished(self, picked: List[str], quality_scan_intent: bool = False) -> None:
        if self.kiwoom is None:
            return
        with _SCAN_CONSOLE_LOCK:
            summary = list(_SCAN_LAST_SUMMARY)
            _SCAN_LAST_SUMMARY.clear()
        for line in summary:
            self._append_log(line)
        if self.engine is not None:
            mc_n = max(0, int(SCAN_QUALITY_MC_TOP_N))
            if quality_scan_intent and mc_n > 0:
                al = _mc_top_allowlist_for_scan(mc_n)
                self.engine.set_scan_quality_context(True, al)
                if al:
                    self._append_log(
                        f"[SCAN] 우량슬롯·눌림 ON 허용{len(al)}종 "
                        f"(bear예외={'Y' if QUALITY_DIP_ALLOW_IN_BEAR else 'N'} "
                        f"물타기×{int(QUALITY_AVG_DOWN_MAX) if QUALITY_AVG_DOWN_ENABLED else 0})"
                    )
                else:
                    self._append_log(
                        "[SCAN] 우량 슬롯인데 시총 허용 목록 미확보 — 눌림 허용집합 비움"
                    )
            else:
                # 허용목록은 유지 — 전체 스캔 차례에도 우량 눌림/물타기 계속
                self.engine.set_scan_quality_context(False, None)
                n_keep = len(getattr(self.engine, "_quality_mcap_allowlist", set()) or set())
                if n_keep > 0:
                    self._append_log(f"[SCAN] 전체유니버스 — 우량허용 {n_keep}종 유지")
        today = datetime.date.today()
        existing = list(self.engine.stock_codes) if self.engine is not None else []
        picked_clean = [str(c).strip() for c in list(picked or []) if str(c).strip()]
        self.scan_message.emit(f"[SCAN] 최종 선정 {len(picked_clean)}종목: {picked_clean[:10]}")

        for c in existing:
            self._target_last_seen.setdefault(c, today)
        for c in picked_clean:
            self._target_last_seen[c] = today

        # 기존 순서 유지 + 신규 스캔 종목 누적(중복 제거)
        merged = list(dict.fromkeys(existing + picked_clean))

        held_set = {
            c
            for c, p in ((self.engine.positions if self.engine is not None else {}) or {}).items()
            if int((p or {}).get("qty", 0) or 0) > 0
        }
        non_held_merged = [c for c in merged if c not in held_set]

        # 스캔 0건/기존비어있음이면 마지막 유효 대기목록으로 복구(진입대기 급감 방지)
        if (not picked_clean) and (not non_held_merged):
            fallback_limit = (
                max(1, int(MAX_CUMULATED_TARGETS))
                if int(MAX_CUMULATED_TARGETS) > 0
                else 100000
            )
            fallback = self._load_last_nonzero_waiting_codes(limit=fallback_limit)
            if fallback:
                add_fallback = [c for c in fallback if c not in held_set]
                merged = list(dict.fromkeys(existing + add_fallback))
                self._append_log(
                    f"[SCAN] 결과 0건 - 최근 유효 대기목록에서 미보유 {len(add_fallback)}종목 복구 적용"
                )
                non_held_merged = [c for c in merged if c not in held_set]
        if (not picked_clean) and (not non_held_merged):
            try:
                universe = [str(c).strip() for c in self.kiwoom.get_all_kr_codes() if str(c).strip()]
            except Exception:
                universe = []
            sampled = [c for c in universe if c not in held_set][:120]
            if sampled:
                merged = list(dict.fromkeys(existing + sampled))
                self._append_log(
                    f"[SCAN] 결과 0건 - 유니버스 샘플 미보유 {len(sampled)}종목 임시 대체"
                )

        # TTL 정리: 최근 TARGET_TTL_DAYS 안에 재등장한 종목만 유지
        ttl_days = int(TARGET_TTL_DAYS)
        ttl_filtered: List[str] = []
        pruned_ttl = 0
        if ttl_days > 0:
            cutoff = today - datetime.timedelta(days=ttl_days)
            for c in merged:
                seen = self._target_last_seen.get(c, today)
                if seen >= cutoff:
                    ttl_filtered.append(c)
                else:
                    pruned_ttl += 1
                    self._target_last_seen.pop(c, None)
        else:
            ttl_filtered = list(merged)

        # 상한 정리: 최근 관측일이 최신인 순으로 유지(동일일자는 기존 순서 유지)
        cap = int(MAX_CUMULATED_TARGETS)
        if cap > 0 and len(ttl_filtered) > cap:
            sorted_recent = sorted(
                ttl_filtered,
                key=lambda c: (self._target_last_seen.get(c, datetime.date.min),),
                reverse=True,
            )
            kept_set = set(sorted_recent[:cap])
            capped = [c for c in ttl_filtered if c in kept_set]
            pruned_cap = len(ttl_filtered) - len(capped)
            for c in ttl_filtered:
                if c not in kept_set:
                    self._target_last_seen.pop(c, None)
            merged = capped
        else:
            merged = ttl_filtered
            pruned_cap = 0

        # 하드 상한은 MAX_CUMULATED_TARGETS 설정과 무관하게 항상 적용(메모리 보호)
        hard_cap = max(200, int(HARD_MAX_CUMULATED_TARGETS))
        pruned_hard = 0
        if len(merged) > hard_cap:
            sorted_recent = sorted(
                merged,
                key=lambda c: (self._target_last_seen.get(c, datetime.date.min),),
                reverse=True,
            )
            kept_set = set(sorted_recent[:hard_cap])
            hard_capped = [c for c in merged if c in kept_set]
            pruned_hard = len(merged) - len(hard_capped)
            for c in merged:
                if c not in kept_set:
                    self._target_last_seen.pop(c, None)
            merged = hard_capped

        active_limit = max(1, int(ACTIVE_BUY_TARGET_LIMIT))
        held_ordered = [c for c in merged if c in held_set]
        buy_candidates = [c for c in merged if c not in held_set]
        pruned_active = max(0, len(buy_candidates) - active_limit)
        if pruned_active > 0:
            merged = list(dict.fromkeys(held_ordered + buy_candidates[:active_limit]))
            self.scan_message.emit(
                f"[SCAN] 실매수 후보 상위 {active_limit}종목으로 압축 "
                f"(제외 {pruned_active}종목, 보유 {len(held_ordered)}종목 유지)"
            )

        added = max(0, len(merged) - len(existing))
        if added > 0:
            self.scan_message.emit(f"[SCAN] 누적 추가 {added}종목 (총 {len(merged)}종목)")
        else:
            self.scan_message.emit(f"[SCAN] 누적 추가 없음 (총 {len(merged)}종목)")
        if pruned_ttl > 0 or pruned_cap > 0 or pruned_hard > 0:
            self.scan_message.emit(
                f"[SCAN] 자동 정리 TTL-{pruned_ttl}, CAP-{pruned_cap}, HARD-{pruned_hard} "
                f"(유지 {len(merged)} / hard {hard_cap})"
            )
        self.set_targets(merged, self.kiwoom)

    def _open_dashboard(self) -> None:
        """수익률 분석 대시보드(별도 창)."""
        if self.engine is None or self.trade_tracker is None:
            self._append_log("[대시보드] 엔진 또는 거래기록 모듈이 준비되지 않았습니다.")
            return
        if self._dashboard is None:
            self._dashboard = PerformanceDashboard(
                engine=self.engine,
                tracker=self.trade_tracker,
                parent=self,
            )
        else:
            self._dashboard.set_engine(self.engine)
            self._dashboard.set_tracker(self.trade_tracker)
        self._dashboard.show()
        self._dashboard.raise_()
        self._dashboard.activateWindow()

    def _toggle_engine(self) -> None:
        if self.engine is None:
            return
        if self.engine.timer.isActive():
            self.engine.timer.stop()
            self.lbl_hint.setText("자동매매를 일시 정지했습니다.")
        else:
            self.engine.timer.start()
            self.lbl_hint.setText("자동매매를 재개했습니다.")
        self.refresh_ui()

    def _sync_force_scan_button_text(self) -> None:
        mode = "ON" if self.force_scan_anytime else "OFF"
        self.btn_force_scan.setText(f"테스트스캔 {mode}")

    def _toggle_force_scan(self) -> None:
        global SCAN_RELAXED_MODE
        self.force_scan_anytime = not self.force_scan_anytime
        SCAN_RELAXED_MODE = bool(self.force_scan_anytime)
        self._sync_force_scan_button_text()
        state = "활성화" if self.force_scan_anytime else "비활성화"
        self._append_log(
            f"[SCAN] 테스트스캔 모드 {state} (필터완화 {'ON' if SCAN_RELAXED_MODE else 'OFF'})"
        )
        # ON 전환 시 즉시 1회 스캔 시도 (다음 타이머 tick 대기 없이)
        if self.force_scan_anytime and callable(self.request_scan_now_force):
            try:
                self.request_scan_now_force()
            except Exception:
                pass

    def _show_strategy_report(self) -> None:
        """전략 성과 스냅샷을 하단 로그창에 출력."""
        if self.engine is None:
            self._append_log("[STRAT] 엔진이 준비되지 않았습니다.")
            return
        sm = getattr(self.engine, "strategy_manager", None)
        if sm is None:
            self._append_log("[STRAT] StrategyManager가 연결되지 않았습니다.")
            return
        self._append_log("[STRAT] ===== 전략 리포트 =====")
        self._append_log(f"[STRAT] 활성 전략: {getattr(sm, 'active_strategy_id', '-')}")
        if callable(getattr(sm, "summary_line", None)):
            try:
                self._append_log(f"[STRAT] 요약: {sm.summary_line()}")
            except Exception:
                pass
        records = getattr(sm, "last_records", {}) or {}
        if not records:
            self._append_log("[STRAT] 아직 평가 데이터가 없습니다.")
            return
        for sid, rec in records.items():
            try:
                self._append_log(
                    f"[STRAT] {sid}: R={float(getattr(rec, 'return_pct', 0.0)):+.2f}% "
                    f"WR={float(getattr(rec, 'win_rate', 0.0))*100:.1f}% "
                    f"MDD={float(getattr(rec, 'mdd_pct', 0.0)):.2f}% "
                    f"Trades={float(getattr(rec, 'trade_count', 0.0)):.1f} "
                    f"Score={float(getattr(rec, 'score', 0.0)):.2f}"
                )
            except Exception:
                self._append_log(f"[STRAT] {sid}: 출력 실패")

    def _on_scan_message(self, text: str) -> None:
        msg = str(text or "")
        self._append_log(msg)
        if "[SCAN] 진행:" in msg:
            self.lbl_scan_progress.setText(msg.replace("[SCAN] ", "").strip())
            return
        if "[SCAN] 강제시작:" in msg or "[SCAN] 시작:" in msg:
            self.lbl_scan_progress.setText("스캔상태: 실행 중")
            return
        if "[SCAN] 완료:" in msg:
            self.lbl_scan_progress.setText(msg.replace("[SCAN] ", "").strip())
            return
        if "[SCAN] 실패:" in msg:
            self.lbl_scan_progress.setText(msg.replace("[SCAN] ", "").strip())
            return

    def refresh_ui(self) -> None:
        if self.engine is None:
            return
        try:
            running = self.engine.timer.isActive()
            self.lbl_status.setText("상태: 실행 중" if running else "상태: 정지")
            self.btn_stop.setText("정지" if running else "재개")
            self.lbl_market.setText(f"장시간: {self.engine.market_session_name()}")
            active_hold = sum(
                1 for _c, p in (self.engine.positions or {}).items() if int(p.get("qty", 0) or 0) > 0
            )
            pending_buy = sum(1 for _c, v in (self.engine.pending_buy or {}).items() if bool(v))
            pending_sell = sum(1 for _c, v in (self.engine.pending_sell or {}).items() if bool(v))
            self.lbl_positions.setText(f"보유종목: {active_hold}")
            self.lbl_pending_buy.setText(f"매수미체결: {pending_buy}")
            self.lbl_pending_sell.setText(f"매도미체결: {pending_sell}")
            total_pnl = 0
            for code, p in (self.engine.positions or {}).items():
                qty = int((p or {}).get("qty", 0) or 0)
                avg = int((p or {}).get("avg_price", 0) or 0)
                cur_px = int(self.engine.current_price.get(code, 0) or 0)
                if qty > 0 and avg > 0 and cur_px > 0:
                    total_pnl += (cur_px - avg) * qty
            realized_pnl = 0.0
            if self.trade_tracker is not None:
                try:
                    realized_pnl = float(self.trade_tracker.total_realized_pnl_krw())
                except Exception:
                    realized_pnl = 0.0
            cumulative_pnl = float(total_pnl) + float(realized_pnl)
            # --- 키움 계좌 기준 (HTS 동기화) ---
            kw_asset = getattr(self.engine, "opw_estimated_deposit_krw", None)
            kw_accum = getattr(self.engine, "opw_accum_invest_profit_loss_krw", None)
            kw_eval_pnl = getattr(self.engine, "opw_holdings_eval_pnl_krw", None)

            if kw_asset is not None and int(kw_asset) > 0:
                est_asset = float(kw_asset)
                asset_txt = f"추정예탁(키움): {int(est_asset):,}원"
            else:
                est_asset = float(TOTAL_LIMIT_KRW) + cumulative_pnl
                asset_txt = f"추정예탁(앱추정): {int(est_asset):,}원"

            # HTS 「손익금액/누적수익률」과 맞추는 기준:
            # 이 계좌는 opw 누적투자손익·손익금액이 0 또는 소액(평가손익)으로만
            # 오는 경우가 있어(스크린샷 +28,778), TR 손익 필드보다
            # 추정예탁 − 총한도액이 HTS와 일치한다(7,497,684−10,000,000=−2,502,316).
            pnl_note = ""
            kw_pnl: Optional[int] = None
            if kw_asset is not None and int(kw_asset) > 0 and float(TOTAL_LIMIT_KRW) > 0:
                kw_pnl = int(round(float(kw_asset) - float(TOTAL_LIMIT_KRW)))
                pnl_note = ""
            else:
                pnl_candidates: List[int] = []
                for v in (kw_accum, kw_eval_pnl):
                    if v is not None and int(v) != 0:
                        pnl_candidates.append(int(v))
                if pnl_candidates:
                    kw_pnl = max(pnl_candidates, key=lambda x: abs(x))
                    pnl_note = "(TR)"

            at_iv = getattr(self.engine, "opw_eval_fetched_at", None)
            at_dep = getattr(self.engine, "opw_deposit_fetched_at", None)
            age_s = ""
            stamp = at_dep or at_iv
            if isinstance(stamp, datetime.datetime):
                sec = int((datetime.datetime.now() - stamp).total_seconds())
                age_s = f" ·{max(0, sec // 60)}분전"

            day_iv = getattr(self.engine, "opw_daily_invest_pnl_krw", None)
            if kw_pnl is not None:
                basis = float(TOTAL_LIMIT_KRW) if float(TOTAL_LIMIT_KRW) > 0 else max(float(est_asset), 1.0)
                kw_ret_pct = (float(kw_pnl) / basis) * 100.0
                pnl_bits = [f"계좌손익(키움): {int(kw_pnl):+,}원{pnl_note}"]
                if day_iv is not None:
                    pnl_bits.append(f"당일 {int(day_iv):+,}")
                self.lbl_opw_pnl.setText(" ".join(pnl_bits) + age_s)
                self.lbl_total_ret.setText(f"계좌수익률(키움): {kw_ret_pct:+.2f}%")
            else:
                self.lbl_opw_pnl.setText(f"계좌손익(키움): —{age_s}")
                self.lbl_total_ret.setText("계좌수익률(키움): —")

            self.lbl_est_asset.setText(asset_txt)

            # --- 앱 매매 기준 (trade_history CSV: 이 봇이 기록한 매수·매도) ---
            app_realized_ret = 0.0
            if self.trade_tracker is not None:
                try:
                    app_realized_ret = float(self.trade_tracker.overall_return_pct())
                except Exception:
                    app_realized_ret = 0.0
            self.lbl_total_pnl.setText(
                f"앱매매 손익(실현 {int(realized_pnl):+,} / 평가 {int(total_pnl):+,} / 합계 {int(cumulative_pnl):+,})원"
            )
            self.lbl_app_ret.setText(f"앱매매 실현수익률: {app_realized_ret:+.2f}%")
            if self.kiwoom is not None:
                try:
                    st = int(self.kiwoom.dynamicCall("GetConnectState()") or 0)
                    if st == 1:
                        self.lbl_server.setText("서버상태: 연결됨")
                        self.lbl_server.setStyleSheet("color: green; font-weight: bold;")
                        elapsed = int((datetime.datetime.now() - self.engine.connected_at).total_seconds())
                        hh = elapsed // 3600
                        mm = (elapsed % 3600) // 60
                        ss = elapsed % 60
                        self.lbl_conn_time.setText(f"서버연결시간: {hh:02d}:{mm:02d}:{ss:02d}")
                        self.lbl_conn_time.setStyleSheet("")
                    else:
                        self.lbl_server.setText("서버상태: 끊김 ⚠")
                        self.lbl_server.setStyleSheet("color: red; font-weight: bold;")
                        # 끊긴 경과 시간 표시
                        disc_at = getattr(self.engine, "_disconnected_at", None)
                        if disc_at is not None:
                            disc_sec = int((datetime.datetime.now() - disc_at).total_seconds())
                            dm = disc_sec // 60
                            ds = disc_sec % 60
                            self.lbl_conn_time.setText(f"끊김 경과: {dm:02d}분{ds:02d}초 — 앱 재시작 필요")
                        else:
                            self.lbl_conn_time.setText("서버연결시간: 끊김 — 앱 재시작 필요")
                        self.lbl_conn_time.setStyleSheet("color: red;")
                except Exception:
                    self.lbl_server.setText("서버상태: 확인실패")
                    self.lbl_server.setStyleSheet("color: orange;")
            if self.engine.maybe_keepalive():
                t = (
                    self.engine.last_keepalive_at.strftime("%H:%M:%S")
                    if self.engine.last_keepalive_at
                    else "-"
                )
                self.lbl_keepalive.setText(f"KeepAlive(30분): {t}")
                self._append_log(f"[KEEPALIVE] {t}")
            self._refresh_target_table()
            target_count = len(list(self.engine.stock_codes or []))
            if target_count <= 0:
                self.lbl_hint.setVisible(True)
                market_open = self.engine._is_market_open()
                session = self.engine.market_session_name()
                if market_open:
                    self.lbl_hint.setText("경고: 대상종목 0개 (검색/필터 결과 없음). 테스트스캔 또는 강제 재검색 확인")
                    now = datetime.datetime.now()
                    should_warn = (
                        self._last_zero_target_warn_at is None
                        or (now - self._last_zero_target_warn_at).total_seconds() >= 60.0
                    )
                    if should_warn:
                        self._append_log("[WARN] 대상종목 0개: [SCAN] 시작/완료/최종 로그를 확인하세요.")
                        self._last_zero_target_warn_at = now
                else:
                    self.lbl_hint.setText(f"장시간 아님({session}) - 종목 검색 대기 중")
                    self._last_zero_target_warn_at = None
            else:
                # 평상시에는 힌트 라벨을 숨겨 기존 UI 밀도를 유지
                self.lbl_hint.setVisible(False)
            qtag = ""
            if self.engine is not None and callable(getattr(self.engine, "scan_quality_ui_tag", None)):
                qtag = str(self.engine.scan_quality_ui_tag() or "").lstrip(" |")
            if not qtag:
                qtag = "스캔: -"
            ai_txt = (
                self.engine.ai_status_line()
                if callable(getattr(self.engine, "ai_status_line", None))
                else "AI:-"
            )
            sm = getattr(self.engine, "strategy_manager", None)
            if sm is not None and callable(getattr(sm, "summary_parts", None)):
                parts = sm.summary_parts()
                self.lbl_strategy.setText(parts.get("strategy") or "전략: -")
                self.lbl_regime.setText(parts.get("market") or "시장: -")
                sel = parts.get("selector") or "-"
                self.lbl_ai_status.setText(f"{ai_txt} | 선택:{sel}")
            elif sm is not None:
                # 구버전 strategy_manager 호환
                self.lbl_strategy.setText(sm.summary_line())
                self.lbl_regime.setText("시장: -")
                self.lbl_ai_status.setText(ai_txt)
            else:
                self.lbl_strategy.setText("전략: -")
                self.lbl_regime.setText("시장: -")
                self.lbl_ai_status.setText(ai_txt)
            self.lbl_scan_quality.setText(qtag)
            now = datetime.datetime.now()
            if (
                self._last_real_stats_log_at is None
                or (now - self._last_real_stats_log_at).total_seconds() >= 60.0
            ):
                st = self.engine.consume_real_price_stats()
                cps = float(st.get("per_sec", 0.0))
                cpm = cps * 60.0
                self._append_log(
                    f"[REAL] 수신율 {cps:.2f}/s ({cpm:.1f}/min) "
                    f"최근{int(st.get('elapsed_sec', 0.0))}초 {int(st.get('count', 0.0))}건 "
                    f"누적 {int(st.get('total', 0.0))}건"
                )
                if target_count > 0 and cps <= 0.0 and self.engine._is_market_open():
                    should_warn = (
                        self._last_zero_real_warn_at is None
                        or (now - self._last_zero_real_warn_at).total_seconds() >= 60.0
                    )
                    if should_warn:
                        self._append_log(
                            f"[WARN] 대상 {target_count}종목인데 실시간 수신율 0.0/s (SetRealReg/장상태/종목코드 확인)"
                        )
                        self._last_zero_real_warn_at = now
                self._last_real_stats_log_at = now
        except Exception:
            pass

    def _append_log(self, text: str) -> None:
        msg = str(text)
        try:
            self.log_message.emit(msg)
        except Exception:
            pass
        # GUI·스캔·REAL 로그도 일일 파일에 남겨 자동분석이 시세/스캔을 볼 수 있게 함
        try:
            eng = getattr(self, "engine", None)
            if eng is not None and callable(getattr(eng, "_write_log_file", None)):
                eng._write_log_file(msg)
        except Exception:
            pass

    def _append_log_ui(self, text: str) -> None:
        try:
            msg = str(text)
            if self._should_suppress_after_hours_log(msg):
                return
            ts = datetime.datetime.now().strftime("%H:%M:%S")
            self.log.appendPlainText(f"[{ts}] {msg}")
        except Exception:
            pass

    def _should_suppress_after_hours_log(self, msg: str) -> bool:
        """정규장 외 반복 상태 로그를 줄이고, 주문/오류/체결 등 중요 로그는 유지."""
        try:
            if self.engine is None:
                return False
            session = self.engine.market_session_name()
            if session == "정규장":
                return False
            important_tokens = (
                "[BUY-ORDER]",
                "[SELL-ORDER]",
                "[FILLED-",
                "ORDER_",
                "[ERROR]",
                "[WARN] 테이블",
                "[SCAN] 시작",
                "[SCAN] 강제시작",
                "[SCAN] 완료",
                "[SCAN] 실패",
                "[SCAN] 최종 선정",
                "[BOOT]",
            )
            if any(tok in msg for tok in important_tokens):
                return False
            limited_rules = (
                ("real", "[REAL]"),
                ("scan_wait", "[SCAN] 대기:"),
                ("target_zero", "[WARN] 대상종목 0개"),
                ("market_wait", "장시간 아님"),
                ("keepalive", "[KEEPALIVE]"),
                ("buy_block", "[BUY-BLOCK]"),
                ("price", "[PRICE]"),
                ("holdings", "[HOLDINGS]"),
                ("target", "[TARGET]"),
            )
            key = ""
            for k, token in limited_rules:
                if token in msg:
                    key = k
                    break
            if not key:
                return False
            if session in ("휴장", "프리마켓") and key in ("real", "scan_wait", "target_zero"):
                return True
            if key in ("real", "scan_wait", "target_zero"):
                count = int(self._after_hours_log_counts.get(key, 0) or 0)
                if count >= 3:
                    return True
                self._after_hours_log_counts[key] = count + 1
                return False
            now = datetime.datetime.now()
            last = self._last_after_hours_log_at.get(key)
            if last is not None and (now - last).total_seconds() < 300.0:
                return True
            self._last_after_hours_log_at[key] = now
            return False
        except Exception:
            return False

    def _write_waiting_status_log(self, waiting_codes: List[str], all_codes: List[str]) -> None:
        """진입대기 상태를 CSV로 기록."""
        now = datetime.datetime.now()
        waiting_tuple = tuple(sorted(waiting_codes))
        all_tuple = tuple(sorted(all_codes))
        snapshot = (len(waiting_tuple), waiting_tuple, len(all_tuple))
        # 상태 변화가 있을 때만 기록(로그 과다 방지)
        if snapshot == self._last_waiting_snapshot:
            return
        self._last_waiting_snapshot = snapshot
        self._last_waiting_log_at = now
        try:
            need_header = not os.path.exists(self._waiting_log_path)
            with open(self._waiting_log_path, "a", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                if need_header:
                    writer.writerow(
                        [
                            "timestamp",
                            "total_targets",
                            "waiting_count",
                            "waiting_codes",
                        ]
                    )
                writer.writerow(
                    [
                        now.strftime("%Y-%m-%d %H:%M:%S"),
                        len(all_tuple),
                        len(waiting_tuple),
                        "|".join(waiting_tuple),
                    ]
                )
        except Exception:
            pass

    def _load_last_nonzero_waiting_codes(self, limit: int = 33) -> List[str]:
        """waiting_status_log.csv에서 (최근 중) 최대 waiting_count 행 우선으로 코드 복구."""
        try:
            if not os.path.exists(self._waiting_log_path):
                return []
            best_codes: List[str] = []
            best_count = -1
            best_ts = ""
            with open(self._waiting_log_path, "r", encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    try:
                        cur_count = int(str(row.get("waiting_count", "0") or "0"))
                        if cur_count <= 0:
                            continue
                    except Exception:
                        continue
                    s = str(row.get("waiting_codes", "") or "")
                    codes = [c.strip() for c in s.split("|") if c.strip()]
                    if not codes:
                        continue
                    ts = str(row.get("timestamp", "") or "")
                    # waiting_count가 큰 행 우선, 같으면 더 최근 timestamp 우선
                    if (cur_count > best_count) or (cur_count == best_count and ts >= best_ts):
                        best_count = cur_count
                        best_ts = ts
                        best_codes = codes
            if not best_codes:
                return []
            # 중복 제거 후 상위 limit
            return list(dict.fromkeys(best_codes))[: max(1, int(limit))]
        except Exception:
            return []

    def _run_backtest(self) -> None:
        if self.engine is None:
            self._append_log("[BT] 엔진이 준비되지 않았습니다.")
            return
        codes = list(self.engine.stock_codes or [])
        if not codes:
            self._append_log("[BT] 조건검색 결과 종목이 없습니다.")
            return
        self._append_log(f"[BT] 조건검색 결과 {len(codes)}종목 백테스트 시작...")
        r = run_backtest_for_codes(codes, lookback=BACKTEST_LOOKBACK_BARS)
        if not bool(r.get("ok", False)):
            self._append_log(f"[BT] 실패/스킵: {r.get('reason', 'unknown')}")
            return
        avg_ret = float(r.get("avg_ret", 0.0))
        win_rate = float(r.get("win_rate", 0.0))
        self._append_log(
            f"[BT] lookback={BACKTEST_LOOKBACK_BARS}일 avg_return={avg_ret*100:+.2f}% win_rate={win_rate*100:.1f}%"
        )
        for row in list(r.get("results", []))[:3]:
            code = str(row.get("code", ""))
            rt = float(row.get("ret", 0.0))
            self._append_log(f"[BT] top {code}: {rt*100:+.2f}%")

    def _apply_table_rows(
        self,
        table: "QTableWidget",
        rows: List[List[str]],
        *,
        left_cols: Optional[Set[int]] = None,
    ) -> bool:
        """셀 텍스트가 바뀐 칸만 갱신. left_cols는 왼쪽 정렬 열 번호. 변경이 있으면 True."""
        changed = False
        left = left_cols or set()
        table.setUpdatesEnabled(False)
        try:
            if table.rowCount() != len(rows):
                table.setRowCount(len(rows))
                changed = True
            for i, vals in enumerate(rows):
                for c, v in enumerate(vals):
                    s = str(v)
                    align = int(Qt.AlignLeft | Qt.AlignVCenter) if c in left else int(Qt.AlignCenter)
                    item = table.item(i, c)
                    if item is None:
                        item = QTableWidgetItem(s)
                        item.setTextAlignment(align)
                        table.setItem(i, c, item)
                        changed = True
                    else:
                        if item.text() != s:
                            item.setText(s)
                            changed = True
                        if int(item.textAlignment()) != align:
                            item.setTextAlignment(align)
                            changed = True
        finally:
            table.setUpdatesEnabled(True)
        return changed

    def _refresh_target_table(self) -> None:
        if self.engine is None:
            return
        try:
            now_table = datetime.datetime.now()
            if (
                self._last_target_table_refresh_at is not None
                and (now_table - self._last_target_table_refresh_at).total_seconds()
                < float(UI_TABLE_REFRESH_INTERVAL_SEC)
            ):
                return
            self._last_target_table_refresh_at = now_table
            codes = list(self.engine.stock_codes or [])
            # 중앙 리스트에는 스캔 대상 + 현재 보유 종목을 함께 표시합니다.
            held_codes = [
                c
                for c, p in (self.engine.positions or {}).items()
                if int((p or {}).get("qty", 0) or 0) > 0
            ]
            if held_codes:
                codes = list(dict.fromkeys(codes + held_codes))
                self.engine.stock_codes = list(codes)
            # 안전장치: 리스트 표시 단계에서도 저가주(<MIN_ENTRY_PRICE) 제거
            kept_codes: List[str] = []
            dropped_low = 0
            for code in codes:
                held_qty = int(
                    ((self.engine.positions or {}).get(code, {}) or {}).get("qty", 0) or 0
                )
                # 보유 중인 종목은 저가 필터로 목록/실시간 상태에서 빼지 않음(표시·동기화 일관성)
                if held_qty > 0:
                    kept_codes.append(code)
                    continue
                px = int(self.engine.current_price.get(code, 0) or 0)
                if px <= 0:
                    mv = self.engine.ma.get(code, {})
                    px = int(float(mv.get("close_today", 0) or 0))
                if px <= 0:
                    px = self._get_cached_close_price(code)
                if px > 0 and (
                    float(px) < float(MIN_ENTRY_PRICE) or float(px) > float(MAX_ENTRY_PRICE)
                ):
                    dropped_low += 1
                    continue
                kept_codes.append(code)

            if dropped_low > 0:
                self.engine.stock_codes = kept_codes
                self.engine.pending_buy = {c: bool(self.engine.pending_buy.get(c, False)) for c in kept_codes}
                self.engine.pending_sell = {c: bool(self.engine.pending_sell.get(c, False)) for c in kept_codes}
                self.engine.tp_reached = {c: bool(self.engine.tp_reached.get(c, False)) for c in kept_codes}
                self.code_names = {c: self.code_names.get(c, c) for c in kept_codes}
                codes = kept_codes
                if dropped_low != self._last_low_price_drop_count:
                    self._append_log(
                        f"[SCAN] 가격범위 제외({int(MIN_ENTRY_PRICE):,}~{int(MAX_ENTRY_PRICE):,}원 밖): 표시목록에서 {dropped_low}종목 제거"
                    )
                    self._last_low_price_drop_count = dropped_low
            else:
                self._last_low_price_drop_count = 0

            holding_rows: List[List[str]] = []
            waiting_rows: List[List[str]] = []
            order_rows: List[List[str]] = []
            order_keys = set()
            waiting_codes: List[str] = []
            for code in codes:
                name = self.code_names.get(code, code)
                pos = (self.engine.positions or {}).get(code, {}) or {}
                qty = int(pos.get("qty", 0) or 0)
                avg_price = int(pos.get("avg_price", 0) or 0)
                cur = int(self.engine.current_price.get(code, 0) or 0)
                # 실시간가가 비면 MA 캐시의 최근 종가를 대체 표시
                if cur <= 0:
                    mv = self.engine.ma.get(code, {})
                    cur = int(float(mv.get("close_today", 0) or 0))
                # MA도 비면 스캔 단계에서 확보한 yfinance 캐시 종가를 즉시 표시
                if cur <= 0:
                    cur = self._get_cached_close_price(code)
                    if cur > 0:
                        self.engine.current_price[code] = cur
                        self.engine.current_price_source[code] = "YF_CACHE"
                # 마지막 fallback은 보유/미체결 중심으로만 수행(대량 대기목록 UI 지연 방지)
                if cur <= 0 and (
                    qty > 0
                    or self.engine.pending_buy.get(code, False)
                    or self.engine.pending_sell.get(code, False)
                ):
                    cur = self._get_kiwoom_master_last_price(code)
                    if cur > 0:
                        self.engine.current_price[code] = cur
                        self.engine.current_price_source[code] = "KIWOOM_MASTER"
                buy_str = f"{avg_price:,}" if avg_price > 0 else "-"
                pnl_str = "-"
                pnl_pct_str = "-"
                if qty > 0 and avg_price > 0 and cur > 0:
                    pnl = (int(cur) - int(avg_price)) * int(qty)
                    pnl_pct = (float(cur) / float(avg_price) - 1.0) * 100.0
                    pnl_str = f"{pnl:+,}"
                    pnl_pct_str = f"{pnl_pct:+.2f}%"
                if qty > 0:
                    status = f"보유중({qty}주)"
                elif self.engine.pending_buy.get(code, False):
                    status = "매수 미체결"
                elif self.engine.pending_sell.get(code, False):
                    status = "매도 미체결"
                else:
                    status = "진입 대기"
                    waiting_codes.append(code)
                cur_str = f"{cur:,}" if cur > 0 else "-"
                if qty > 0:
                    symbol = str(name)
                    profit_display = pnl_str if pnl_pct_str == "-" else f"{pnl_str} / {pnl_pct_str}"
                    holding_rows.append([symbol, buy_str, cur_str, f"{qty:,}", profit_display])
                elif status == "진입 대기":
                    symbol = str(name)
                    if len(waiting_rows) < int(UI_WAITING_DISPLAY_LIMIT):
                        waiting_rows.append([symbol, cur_str, status, "-"])
                else:
                    if not str(name).strip() or str(name).strip() == str(code):
                        try:
                            symbol = str(self.kiwoom.get_master_code_name(code)) if self.kiwoom is not None else str(code)
                        except Exception:
                            symbol = str(code)
                    else:
                        symbol = str(name)
                    # 미체결 상태는 현재 포지션 평균단가를 우선 "매수가"로 표시
                    buy_px = int(((self.engine.positions or {}).get(code, {}) or {}).get("avg_price", 0) or 0)
                    buy_px_str = f"{buy_px:,}" if buy_px > 0 else "-"
                    pending_meta = (self.engine.pending_orders or {}).get(code, {}) or {}
                    req_qty = int(pending_meta.get("qty", 0) or 0)
                    filled_so_far = int(pending_meta.get("fills_notified_qty", 0) or 0)
                    remain = max(0, req_qty - filled_so_far)
                    if req_qty > 0:
                        remain_str = f"{filled_so_far:,} / {remain:,}"
                    else:
                        remain_str = "-"
                    order_rows.append([symbol, buy_px_str, "-", status, "-", remain_str, "-"])
                    order_keys.add((code, status))

            # 최근 체결된 매수/매도도 우측 "매수/매도종목"에 함께 표시
            # 동일 종목·당일 여러 번 부분체결되면 레코드가 여러 줄인데, 기존엔 (code,상태)로
            # 최근 한 줄만 남아 HTS 합계(예: 43주)와 어긋남 → 일자별로 수량·가중평단 합산
            try:
                now_dt = datetime.datetime.now()
                cutoff = now_dt - datetime.timedelta(days=7)
                records = self.trade_tracker.all_records() if self.trade_tracker is not None else []
                window_recs: List[Any] = []
                for r in records:
                    if r.ts < cutoff:
                        continue
                    side_u = str(r.side).upper()
                    if side_u not in ("BUY", "SELL"):
                        continue
                    code6 = str(r.code).strip()
                    if len(code6) != 6 or not code6.isdigit():
                        continue
                    window_recs.append(r)

                grp_map: Dict[tuple, List[Any]] = defaultdict(list)
                for r in window_recs:
                    key_g = (str(r.code).strip(), str(r.side).upper(), r.ts.date())
                    grp_map[key_g].append(r)

                sorted_groups = sorted(
                    grp_map.items(),
                    key=lambda kv: max(x.ts for x in kv[1]),
                    reverse=True,
                )

                for (code, side_upper, _grp_day), grp in sorted_groups:
                    grp_sorted = sorted(grp, key=lambda x: x.ts)
                    status = "매수 체결" if side_upper == "BUY" else "매도 체결"
                    agg_key = (code, status, _grp_day)
                    if agg_key in order_keys:
                        continue

                    total_qty = sum(max(0, int(getattr(x, "qty", 0) or 0)) for x in grp_sorted)
                    if total_qty <= 0:
                        continue
                    w_den = sum(int(x.qty) for x in grp_sorted if int(getattr(x, "qty", 0) or 0) > 0)
                    w_num = sum(
                        float(x.price) * int(x.qty)
                        for x in grp_sorted
                        if int(getattr(x, "qty", 0) or 0) > 0
                    )
                    exec_px_ag = int(round(w_num / float(w_den))) if w_den > 0 else int(
                        float(grp_sorted[-1].price) or 0.0
                    )
                    earliest = grp_sorted[0].ts
                    latest = grp_sorted[-1].ts

                    name = self.code_names.get(code, code)
                    if not str(name).strip() or str(name).strip() == str(code):
                        try:
                            symbol = (
                                str(self.kiwoom.get_master_code_name(code))
                                if self.kiwoom is not None
                                else str(code)
                            )
                        except Exception:
                            symbol = str(code)
                    else:
                        symbol = str(name)

                    exec_px_str = f"{exec_px_ag:,}" if exec_px_ag > 0 else "-"

                    if side_upper == "BUY":
                        buy_px_str = f"{exec_px_ag:,}" if exec_px_ag > 0 else "-"
                        profit_str = "-"
                    else:
                        total_pnl = sum(
                            float(getattr(x, "realized_pnl_krw", 0) or 0) for x in grp_sorted
                        )
                        apx = (
                            exec_px_ag - int(round(total_pnl / float(total_qty)))
                            if total_qty > 0
                            else 0
                        )
                        buy_px_ag = max(1, apx)
                        buy_px_str = f"{buy_px_ag:,}"
                        pct_est = (
                            ((float(exec_px_ag) / float(buy_px_ag)) - 1.0) * 100.0
                            if buy_px_ag > 0 and exec_px_ag > 0
                            else None
                        )
                        if pct_est is not None and math.isfinite(pct_est):
                            profit_str = f"{int(round(total_pnl)):+,} / {float(pct_est):+.2f}%"
                        else:
                            profit_str = f"{int(round(total_pnl)):+,}"

                    remain_str = f"{total_qty:,} / 0"
                    split_n = len(grp_sorted)
                    if split_n <= 1:
                        note = latest.strftime("%m-%d %H:%M:%S")
                    else:
                        note = (
                            f"{earliest.strftime('%m-%d %H:%M')}~{latest.strftime('%H:%M')} "
                            f"({split_n}회분)"
                        )

                    order_rows.append(
                        [symbol, buy_px_str, exec_px_str, status, profit_str, remain_str, note]
                    )
                    order_keys.add(agg_key)
            except Exception:
                pass

            hidden_waiting = max(0, len(waiting_codes) - len(waiting_rows))
            waiting_display = list(waiting_rows)
            if hidden_waiting > 0:
                waiting_display.append([f"... 외 {hidden_waiting:,}종목", "-", "진입 대기", "표시 제한"])

            holding_fp = tuple(tuple(r) for r in holding_rows)
            waiting_fp = tuple(tuple(r) for r in waiting_display)
            order_fp = tuple(tuple(r) for r in order_rows)
            holdings_changed = holding_fp != self._last_holding_fp
            waiting_changed = waiting_fp != self._last_waiting_fp
            orders_changed = order_fp != self._last_order_fp
            if holdings_changed:
                self._apply_table_rows(self.tbl_holdings, holding_rows)
                self._last_holding_fp = holding_fp
            if waiting_changed:
                self._apply_table_rows(self.tbl_waiting, waiting_display, left_cols={0})
                self._last_waiting_fp = waiting_fp
            if orders_changed:
                self._apply_table_rows(self.tbl_orders, order_rows)
                self._last_order_fp = order_fp

            # 리사이즈 비용이 커서(특히 테스트스캔 ON 대량 행) 변화 시에만 간헐 수행
            now_resize = datetime.datetime.now()
            shape = (len(holding_rows), len(waiting_display), len(order_rows))
            need_resize = shape != self._last_table_shape
            due_resize = (
                self._last_column_resize_at is None
                or (now_resize - self._last_column_resize_at).total_seconds() >= 8.0
            )
            if need_resize and due_resize:
                self.tbl_holdings.resizeColumnsToContents()
                self.tbl_waiting.resizeColumnsToContents()
                self.tbl_orders.resizeColumnsToContents()
                self._last_column_resize_at = now_resize
            self._last_table_shape = shape
            preview = ", ".join(waiting_codes[:5])
            if len(waiting_codes) > 5:
                preview += " ..."
            self.lbl_waiting.setText(f"진입대기: {len(waiting_codes)}")
            self._write_waiting_status_log(waiting_codes, codes)
        except Exception as e:
            if not getattr(self, "_logged_refresh_target_err", False):
                self._logged_refresh_target_err = True
                try:
                    self._append_log(f"[WARN] 테이블 갱신 실패(이후 1회만 로그): {e!r}")
                except Exception:
                    pass
            try:
                self.tbl_holdings.setRowCount(0)
                self.tbl_waiting.setRowCount(0)
                self.tbl_orders.setRowCount(0)
                self.lbl_waiting.setText("진입대기: 0")
            except Exception:
                pass


def main():
    # CMD에서 한글 출력 시 깨짐 완화
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    # pythonw 등 콘솔 없이 실행될 때 별도 CMD 창 — MYSTOCK_CONSOLE=1
    if os.environ.get("MYSTOCK_CONSOLE", "").strip().lower() in ("1", "true", "yes", "y"):
        try:
            import ctypes

            if sys.platform == "win32" and int(ctypes.windll.kernel32.GetConsoleWindow() or 0) == 0:
                ctypes.windll.kernel32.AllocConsole()
                sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace")
        except Exception:
            pass

    # COM 초기화(환경에 따라 필수일 수 있음)
    try:
        import pythoncom  # type: ignore

        pythoncom.CoInitialize()
    except Exception:
        pass

    # 거래 내역 CSV는 로그인 후 선택 계좌별 파일로 로드합니다.
    trade_tracker = TradeTracker()

    app = QApplication(sys.argv)
    window = TradingWindow()
    window.show()

    parser = argparse.ArgumentParser(description="Kiwoom OpenAPI MA5/MA20 auto trading")
    parser.add_argument(
        "--pw",
        dest="account_password",
        default=os.environ.get("KIWOOM_ACCOUNT_PASSWORD", ""),
        help="계좌 비밀번호(opw00018 TR 필요 시). 기본값은 환경변수 KIWOOM_ACCOUNT_PASSWORD",
    )
    args, _unknown = parser.parse_known_args()

    # 실행 전 비밀번호는 TR 호출(opw00018) 시에만 필요할 수 있습니다.
    account_password = args.account_password.strip()
    pw_configured = bool(account_password)

    kiwoom = KiwoomOpenAPI()

    # Qt 이벤트 루프가 이미 돌고 있을 때 CommConnect/실시간 이벤트가 잘 들어오도록
    # 로그인/트레이딩 시작을 "다음 틱"에 스케줄링합니다.
    def _start_after_event_loop():
        try:
            window.scan_message.emit(
                f"[BOOT] 계좌비번 전달 상태: {'설정됨' if pw_configured else '미설정'} "
                "(미설정이면 잔고조회가 0건으로 보일 수 있음)"
            )
            account = kiwoom.login()
            window.scan_message.emit(f"[BOOT] 선택 계좌: {account}")
            env_acc = os.environ.get("KIWOOM_ACCOUNT_NO", "").strip()
            if not env_acc:
                window.scan_message.emit(
                    f"[BOOT] KIWOOM_ACCOUNT_NO 미설정 - 기본 국내계좌 {DEFAULT_KIWOOM_ACCOUNT_NO} 사용"
                )
            elif "".join(ch for ch in env_acc if ch.isdigit()) != "".join(
                ch for ch in str(DEFAULT_KIWOOM_ACCOUNT_NO) if ch.isdigit()
            ):
                window.scan_message.emit(
                    f"[BOOT] WARN 환경변수 KIWOOM_ACCOUNT_NO={env_acc} "
                    f"(기본 국내계좌={DEFAULT_KIWOOM_ACCOUNT_NO}) — 목록에 기본계좌 있으면 기본 우선"
                )
            account_key = "".join(ch for ch in str(account) if ch.isdigit()) or str(account).strip()
            trade_tracker.csv_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "data",
                f"trade_history_{account_key}.csv",
            )
            trade_tracker.load()
            window.scan_message.emit(f"[BOOT] 거래기록 파일: {os.path.basename(trade_tracker.csv_path)}")
            # UI 멈춤 방지: 엔진은 즉시 시작하고 전체시장 스캔은 백그라운드 스레드에서 수행
            engine = TradingEngine(kiwoom, account, [])
            try:
                engine.realized_pnl_krw = float(trade_tracker.total_realized_pnl_krw())
            except Exception:
                engine.realized_pnl_krw = 0.0
            # strategy_manager: 3전략 성과(수익·승률·MDD·거래수)·점수 산출 +
            # strategy_ai_selector.RandomForest 전략 선택 (학습 샘플 10개 미만이면 전략 변경 안 함)
            engine.strategy_manager = StrategyManager(
                ohlcv_loader=engine._get_cached_strat_ohlcv,
            )
            attach_trade_logging(engine, trade_tracker)
            engine.start(account_password=account_password)
            window.set_engine(engine, account, kiwoom, trade_tracker)
            scan_state = {
                "running": False,
                "last_scan_at": None,  # type: Optional[datetime.datetime]
            }

            def _scan_worker() -> None:
                started = datetime.datetime.now()
                _scan_console(f"[SCAN] 백그라운드 검색 스레드 시작 (top_n={SCAN_TOP_N})")
                qs = False
                try:
                    picked, qs = scan_market(kiwoom, top_n=SCAN_TOP_N)
                except Exception as e:
                    _log(f"[SCAN] background failed: {e}")
                    _scan_console(f"[SCAN] 백그라운드 실패: {e}")
                    window.scan_message.emit(f"[SCAN] 실패: {e}")
                    picked = []
                    qs = False
                elapsed = int((datetime.datetime.now() - started).total_seconds())
                _scan_console(f"[SCAN] 백그라운드 검색 종료: {len(picked)}종목, {elapsed}s")
                window.scan_message.emit(f"[SCAN] 완료: {len(picked)}종목 선정, {elapsed}s 소요")
                window.scan_finished.emit(picked, bool(qs))
                scan_state["last_scan_at"] = datetime.datetime.now()
                scan_state["running"] = False

            def _maybe_start_scan(force: bool = False) -> None:
                if bool(scan_state.get("running", False)):
                    return
                session = engine.market_session_name()
                force_anytime = bool(getattr(window, "force_scan_anytime", False))
                allow_outside_market = bool(force or force_anytime)
                if not engine._is_market_open() and not allow_outside_market:
                    # 요청사항: 종목 검색은 장시간에만 수행
                    window.lbl_hint.setText(f"장시간 아님({session}) - 종목 검색 대기 중")
                    window.scan_message.emit(
                        f"[SCAN] 대기: 장시간 아님({session}) - 테스트스캔 ON 또는 강제 재검색 필요"
                    )
                    return
                if not engine._is_market_open() and allow_outside_market:
                    window.scan_message.emit(
                        f"[SCAN] 장외 강제 실행: 장외({session})에서도 검색 실행"
                    )
                now = datetime.datetime.now()
                last_scan_at = scan_state.get("last_scan_at")
                if (not force) and isinstance(last_scan_at, datetime.datetime):
                    gap = (now - last_scan_at).total_seconds()
                    if gap < 30 * 60:
                        return
                scan_state["running"] = True
                window.lbl_hint.setText("전체시장 조건검색 진행 중입니다...")
                global SCAN_RELAXED_MODE
                SCAN_RELAXED_MODE = bool(force_anytime or force)
                if force:
                    window.scan_message.emit(
                        f"[SCAN] 강제시작: 전체시장 조건검색 top_n={SCAN_TOP_N} "
                        f"(필터완화 {'ON' if SCAN_RELAXED_MODE else 'OFF'})"
                    )
                else:
                    window.scan_message.emit(f"[SCAN] 시작: 전체시장 조건검색 top_n={SCAN_TOP_N}")
                threading.Thread(target=_scan_worker, daemon=True).start()

            # 시작 시 1회 확인 + 장시간 동안 30분 간격 스캔을 위한 주기 체크
            _scan_retry_timer = QTimer(window)
            _scan_retry_timer.setInterval(60_000)  # 1분마다 상태 확인(실행은 30분 간격)
            _scan_retry_timer.timeout.connect(_maybe_start_scan)
            _scan_retry_timer.start()
            window.request_scan_now = _maybe_start_scan
            window.request_scan_now_force = lambda: _maybe_start_scan(force=True)
            SCAN_RELAXED_MODE = bool(window.force_scan_anytime)
            # 시작 시에는 장시간이면 자동 스캔, 장외에서는 테스트스캔 ON일 때만 강제 스캔
            _maybe_start_scan(force=bool(window.force_scan_anytime))
        except Exception as e:
            # 로그인/계좌 문제는 창을 유지해 원인 확인 가능하게 한다 (즉시 quit 금지)
            msg = str(e)
            print(f"[ERROR] Kiwoom login/account failed: {msg}", flush=True)
            try:
                window.scan_message.emit(f"[ERROR] 로그인/계좌 실패: {msg}")
            except Exception:
                pass
            try:
                window.lbl_hint.setText(f"로그인/계좌 실패 — 창 유지. 콘솔 메시지 확인")
            except Exception:
                pass
            try:
                from PyQt5.QtWidgets import QMessageBox

                QMessageBox.critical(
                    window,
                    "키움 로그인/계좌 오류",
                    f"{msg}\n\n"
                    f"필요한 국내계좌: {DEFAULT_KIWOOM_ACCOUNT_NO}\n"
                    "앱은 종료하지 않습니다. 키움 로그인 ID·실전/모의·다른 OpenAPI 중복실행을 "
                    "확인한 뒤 앱을 다시 실행하세요.",
                )
            except Exception:
                pass
            # app.quit() 하지 않음 — 사용자가 메시지를 읽을 수 있게 유지

    QTimer.singleShot(0, _start_after_event_loop)
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()

