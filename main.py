import sys
import argparse
import os
import datetime
import threading
import time
import csv
import io
import contextlib
import logging
import warnings
from zoneinfo import ZoneInfo
from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Dict, List, Optional

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
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

try:
    from sklearn.ensemble import RandomForestClassifier

    AI_AVAILABLE = True
except Exception:
    RandomForestClassifier = None  # type: ignore
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

from performance import TradeTracker
from gui import PerformanceDashboard
from strategy_manager import StrategyManager, STRATEGY_AI, STRATEGY_TREND, STRATEGY_VOLUME


# =========================
# 사용자 설정 구간
# =========================

# scan_market() 결과를 사용하므로 기본 고정 종목은 비워 둡니다.
STOCK_CODES: List[str] = []

# 기본 주문 수량. 예산 기반 수량 계산이 실패할 때만 fallback으로 사용합니다.
ORDER_QTY_PER_STOCK = 1

# 리스크 관리 비율 (종목당 비중 제한 + 수익폭 확대)
STOP_LOSS_PCT = -0.015  # -1.5%
TAKE_PROFIT_PCT = 0.045  # +4.5%

# 매수/매도는 시장가로 단순화(실전에서는 지정가/체결전략 고려)
ORDER_TYPE_BUY = 1   # 신규매수
ORDER_TYPE_SELL = 2  # 신규매도
# hogaGb 값(환경/문서 기준에 따라 다를 수 있어, 문제 발생 시 Kiwoom 문서의 매매구분 값을 확인하세요)
# 일반적으로 "03"=시장가로 많이 사용합니다.
HOGA_MARKET = "03"

# TR/실시간 스크린 번호(여러 요청을 섞어 쓰지 않도록 고정 권장)
SCREEN_NO = "0101"
REAL_SCREEN_NO = "9001"

# 전략 강화 파라미터
MA20_ENTRY_GAP_PCT = -0.0060  # MA20 대비 더 아래까지 허용(진입 확대)
MAX_PRICE_TO_MA20_PCT = 0.28  # 과열 상한 완화(추격 허용)
MAX_CONCURRENT_POSITIONS = 8  # 동시 보유 한도(집중 투자)

# +4.5% 도달 후 바로 청산하지 않고, 트레일링 스탑으로 추가 수익 노리기
USE_TRAIL_AFTER_TP = True
TRAIL_LOCK_PCT = 0.015  # 목표 도달 후 최소 이익 잠금

# 목표 익절% 도달 이후 트레일링 시 MA20 하단을 더 느슨하게(0~1 사이 값).
# ma20을 그대로 쓰면 너무 빡빡할 수 있어, 더 오래 들기 위해 factor를 곱해 바닥을 낮춥니다.
TRAIL_MA20_FACTOR = 1.0

# RSI 필터
RSI_PERIOD = 14
RSI_ENTRY_MIN = 32.0  # RSI 하한 완화
RSI_ENTRY_MAX = 82.0  # RSI 상한 완화
RSI_MIN_DELTA = 0.05  # RSI 상승폭 요구 완화

# 거래량 필터
VOL_AVG_PERIOD = 20
VOL_ENTRY_RATIO_MIN = 0.95  # 평균 대비 거래량 요구 완화
VOL_ENTRY_GROWTH_MIN = 0.94  # 전일 대비 거래량 감소 허용 범위 확대

# AI 모델(추가 필터): LightGBM/XGBoost가 있으면 우선 사용하고, 없으면 RandomForest로 fallback
AI_TRAIN_COUNT = 120  # 학습용 최근 일봉 개수
AI_PROBA_ENTRY_MIN = 0.30  # AI 진입 문턱 하향
AI_RET_LABEL_THRESHOLD = 0.002  # 비용 차감 후 기대수익률이 이 값보다 크면 상승 라벨
AI_MIN_TOTAL_SAMPLES = 200
AI_N_ESTIMATORS = 200
AI_RANDOM_STATE = 42
AI_LABEL_HORIZON_DAYS = 3  # 매수 후 N거래일 안의 손절/익절 도달 순서로 라벨 생성
AI_ROUND_TRIP_COST_PCT = 0.0030  # 수수료+세금 근사 왕복 비용
AI_SLIPPAGE_PCT = 0.0015  # 시장가 체결 미끄러짐 보수 반영
AI_MODEL_BACKEND = os.environ.get("AI_MODEL_BACKEND", "auto").strip().lower()

# 실전 손실 제어: 매도 사유 기록, 손절 후 재진입 제한, 약세장/연속손실 매수 축소
STOP_LOSS_REENTRY_COOLDOWN_SEC = 4 * 3600
RECENT_LOSS_LOOKBACK_DAYS = 7
RECENT_LOSS_BLOCK_COUNT = 2
RECENT_LOSS_BLOCK_DAYS = 3
MARKET_WEAK_MAX_CONCURRENT_POSITIONS = 3
MARKET_WEAK_AI_ENTRY_ADD = 0.08
DAILY_LOSS_SOFT_LIMIT_PCT = -0.008
DAILY_LOSS_HARD_LIMIT_PCT = -0.015
DAILY_LOSS_AI_ENTRY_ADD = 0.08
DAILY_LOSS_MAX_CONCURRENT_POSITIONS = 2
BUY_SCORE_MIN = 0.0
PARTIAL_TAKE_PROFIT_ENABLED = True
PARTIAL_TAKE_PROFIT_RATIO = 0.5
SCALE_IN_ENABLED = True
SCALE_IN_MIN_PROFIT_PCT = 0.018
SCALE_IN_MAX_POSITION_BUDGET_FACTOR = 0.75
MARKET_WEAK_EXIT_LOSS_PCT = -0.005
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
AI_PROBA_EXIT_MAX = 0.42
AI_EARLY_EXIT_MIN_PROFIT_PCT = 0.02  # +2%부터 AI 조기청산 허용
AI_LOSS_EXIT_MIN_LOSS_PCT = -0.008  # -0.8% 이하부터 AI 손실구간 조기청산 허용
AI_EARLY_EXIT_COOL_PCT = 0.01  # 조기 청산 후 바로 재진입 방지용(간단히 내부 상태로 활용 가능)
AI_EXIT_MIN_HOLD_SEC = 180  # 매수 직후 AI 신호만으로 바로 매도되는 것 방지
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
]

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
MIN_ENTRY_PRICE = 2000  # 매수 하한가(원)
MAX_ENTRY_PRICE = 1_000_000  # 매수 상한가(원)
BUDGET_BASED_ORDER_QTY = True  # 총예산/남은 슬롯 기준으로 주문수량 자동 계산
MAX_POSITION_BUDGET_PCT = 0.12  # 한 종목 최대 투자비중(추정예탁자산 대비)
ACTIVE_BUY_TARGET_LIMIT = 100  # 실매수 대상은 스캔 상위 N개로 압축
ORDER_TIMEOUT_COOLDOWN_SEC = 600  # 매수 timeout 후 같은 종목 재주문 대기
SCAN_CACHE_TTL_SEC = 1800
# 기본 사용 계좌. 환경변수 KIWOOM_ACCOUNT_NO가 있으면 그 값이 우선합니다.
DEFAULT_KIWOOM_ACCOUNT_NO = "8125126211"
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
# 대량 진입대기 표시 시 UI 부하 완화
UI_TABLE_REFRESH_INTERVAL_SEC = 3.0
UI_WAITING_DISPLAY_LIMIT = 300
_SCAN_OHLCV_CACHE: Dict[str, tuple] = {}
_SCAN_OHLCV_CACHE_LOCK = threading.Lock()
_SCAN_BAD_TICKERS = set()
_SCAN_BAD_TICKERS_LOCK = threading.Lock()
_SCAN_BAD_WARNED_TICKERS = set()
_SCAN_BAD_WARNED_TICKERS_LOCK = threading.Lock()
# yfinance 캐시 메모리 보호용 상한
SCAN_OHLCV_CACHE_MAX_ENTRIES = int(os.environ.get("SCAN_OHLCV_CACHE_MAX_ENTRIES", "800"))


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
                with contextlib.redirect_stderr(_sink), contextlib.redirect_stdout(_sink):
                    df = yf.download(
                        ticker,
                        period="6mo",
                        interval="1d",
                        auto_adjust=False,
                        progress=False,
                        threads=False,
                    )
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
        ratio = (vol3 / vol20)
        passed_second = bool(cond_vol and cond_trend and cond_close)
        return code6, ratio, passed_first, passed_second
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


def scan_market(kiwoom: "KiwoomOpenAPI", top_n: int = SCAN_TOP_N) -> List[str]:
    """
    전체 시장 -> 1차 필터 -> 2차 전략 -> 최종 top_n 종목.
    - 거래량: 최근 3일 평균 > 20일 평균 * 1.45
    - 추세: MA5 > MA20
    - 종가 > MA20
    - 최종: 거래량 증가율(vol3/vol20) 상위 top_n
    """
    _scan_console(
        f"[SCAN] ===== 시작 {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} "
        f"top_n={top_n} ====="
    )
    try:
        codes = kiwoom.get_all_kr_codes()
        # 비정상 코드 제외(요청 최소화)
        codes = [c for c in codes if isinstance(c, str) and len(c.strip()) == 6 and c.strip().isdigit()]
    except Exception as e:
        _scan_log(f"[SCAN] code universe load failed: {e}")
        return []
    if not codes:
        _scan_log("[SCAN] no universe codes")
        return []
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
                    code, ratio, passed_first, passed_second = r
                    if passed_first:
                        first_pass_count += 1
                        first_pass_candidates.append((code, ratio))
                    if passed_second:
                        results.append((code, ratio))
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
    _scan_log(f"[SCAN] 최종 {len(selected)}종목: {selected}")
    return selected


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


def _scan_log(msg: str) -> None:
    """스캔 단계 로그 — CMD/터미널에 항상 출력."""
    _scan_console(msg)


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


@lru_cache(maxsize=8)
def _krx_holidays(year: int) -> set:
    """
    KRX 공휴일/휴장일 조회.
    조회 실패 시 빈 집합(주말만 휴장 처리)으로 동작.
    """
    out = set()
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


class KiwoomOpenAPI(QAxWidget):
    """
    KHOpenAPICtrl.1 을 PyQt5 QAxWidget으로 감싼 래퍼.
    - CommConnect 로그인
    - TR 요청/응답(QEventLoop 대기)
    - 실시간 주식체결 데이터 수신
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

        # 이벤트 연결
        self.OnEventConnect.connect(self._on_event_connect)
        self.OnReceiveTrData.connect(self._on_receive_tr_data)
        self.OnReceiveRealData.connect(self._on_receive_real_data)
        self.OnReceiveMsg.connect(self._on_receive_msg)

        # 콜백(엔진에서 주입)
        self.on_real_price = None  # type: ignore

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

    def _get_login_account(self) -> str:
        acc = self.dynamicCall("GetLoginInfo(QString)", "ACCNO")

        # "12345;67890" 형태일 수 있어 여러 계좌가 반환될 수 있습니다.
        accounts = [a.strip() for a in str(acc).split(";") if a.strip()]
        if not accounts:
            raise RuntimeError("No accounts returned from Kiwoom (GetLoginInfo ACCNO).")

        # 모의/실투를 위해 원하는 계좌를 선택할 수 있게 환경변수를 지원합니다.
        # - KIWOOM_ACCOUNT_NO: 계좌번호 문자열과 정확히 매칭되는 항목 선택
        # - KIWOOM_ACCOUNT_INDEX: 0-based 인덱스로 선택
        env_account_no = os.environ.get("KIWOOM_ACCOUNT_NO", "").strip()
        if env_account_no:
            for a in accounts:
                if a == env_account_no:
                    _log(f"[Kiwoom] Using account by KIWOOM_ACCOUNT_NO={env_account_no}")
                    print(f"[Kiwoom] Using account: {env_account_no}", flush=True)
                    return a
            # 예전 계좌번호가 환경변수에 남아 있어도 기본 설정 계좌로 조용히 fallback.

        default_account_no = str(DEFAULT_KIWOOM_ACCOUNT_NO).strip()
        if default_account_no:
            for a in accounts:
                if a == default_account_no:
                    _log(f"[Kiwoom] Using default configured account: {default_account_no}")
                    print(f"[Kiwoom] Using account: {default_account_no}", flush=True)
                    return a

        env_account_index = os.environ.get("KIWOOM_ACCOUNT_INDEX", "").strip()
        if env_account_index:
            try:
                idx = int(env_account_index)
            except ValueError:
                raise RuntimeError(f"KIWOOM_ACCOUNT_INDEX must be int. Got: {env_account_index}")
            if 0 <= idx < len(accounts):
                selected = accounts[idx]
                _log(f"[Kiwoom] Using account by KIWOOM_ACCOUNT_INDEX={idx} -> {selected}")
                print(f"[Kiwoom] Using account: {selected}", flush=True)
                return selected
            raise RuntimeError(f"KIWOOM_ACCOUNT_INDEX out of range: {idx} (available_count={len(accounts)})")

        # 기본: 첫 계좌 사용(기존 동작)
        selected = accounts[0]
        _log(f"[Kiwoom] Using default account: {selected} (available={accounts})")
        print(f"[Kiwoom] Using account: {selected}", flush=True)
        return selected

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
            return {"opens": [], "highs": [], "lows": [], "closes": [], "volumes": []}
        return {
            "opens": list(result.get("opens", []))[-count:],
            "highs": list(result.get("highs", []))[-count:],
            "lows": list(result.get("lows", []))[-count:],
            "closes": list(result.get("closes", []))[-count:],
            "volumes": list(result.get("volumes", []))[-count:],
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

                self._tr_responses[rqname] = {
                    "opens": list(reversed(opens_desc)),
                    "highs": list(reversed(highs_desc)),
                    "lows": list(reversed(lows_desc)),
                    "closes": list(reversed(closes_desc)),
                    "volumes": list(reversed(vols_desc)),
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

                self._tr_responses[rqname] = holdings

        finally:
            loop = self._tr_loops.get(rqname)
            if loop is not None:
                loop.exit()

    def _on_receive_real_data(self, code: str, real_type: str, real_data: str):
        # 실시간 '주식체결'에서 현재가 FID 10
        if real_type.strip() != "주식체결":
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
        self.connected_at = datetime.datetime.now()
        self.last_keepalive_at: Optional[datetime.datetime] = None
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

        # 멀티 전략 비교·선택(strategy_manager.py)
        self.strategy_manager: Optional[StrategyManager] = None

        # 중복 주문 방지
        self.pending_buy: Dict[str, bool] = {c: False for c in stock_codes}
        self.pending_sell: Dict[str, bool] = {c: False for c in stock_codes}
        self.last_buy_date: Dict[str, datetime.date] = {}
        self.entry_time: Dict[str, datetime.datetime] = {}
        self.buy_cooldown_until: Dict[str, datetime.datetime] = {}
        self.pending_orders: Dict[str, Dict[str, object]] = {}
        self.realized_pnl_krw: float = 0.0
        self._scale_in_done: Dict[str, bool] = {}
        self._daily_report_done_date: Optional[datetime.date] = None
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

        # 루프 주기
        self.timer = QTimer()
        self.timer.setInterval(2000)
        self.timer.timeout.connect(self.on_tick)

        # 실시간 콜백 연결
        self.kiwoom.on_real_price = self._on_real_price
        self._real_price_tick_total = 0
        self._real_price_tick_window = 0
        self._real_price_window_started_at = datetime.datetime.now()

        self._last_ma_refresh_date: Optional[datetime.date] = None
        self._need_portfolio_sync = True
        self._last_positions_sync_warn_at: Optional[datetime.datetime] = None
        self.last_holdings_tr_rows: int = 0
        self._last_holdings_brief: str = ""
        self._last_buy_block_reason_at: Dict[str, datetime.datetime] = {}
        self._last_holdings_price_sync_at: Optional[datetime.datetime] = None
        self._last_holdings_price_brief: str = ""

        # MA 라운드로빈 상태
        self._ma_needed_codes = set(stock_codes)
        self._ma_refresh_idx = 0
        self._next_ma_refresh_at = datetime.datetime.min

    def _on_real_price(self, code: str, price: int):
        self.current_price[code] = abs(int(price))
        self.current_price_source[code] = "REAL"
        self._real_price_tick_total += 1
        self._real_price_tick_window += 1

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
        if self.on_log is not None:
            try:
                self.on_log(msg)
            except Exception:
                pass

    def _log_buy_block(self, code: str, reason: str, interval_sec: float = 120.0) -> None:
        """매수 차단 사유 로그(종목+사유 단위로 주기 제한)."""
        now = datetime.datetime.now()
        key = f"{code}|{reason}"
        last = self._last_buy_block_reason_at.get(key)
        if last is not None and (now - last).total_seconds() < float(interval_sec):
            return
        self._last_buy_block_reason_at[key] = now
        self._emit_log(f"[BUY-BLOCK] {code} {reason}")

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

    def _budget_based_order_qty(self, price: float) -> int:
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
        remaining_slots = max(1, int(MAX_CONCURRENT_POSITIONS) - active_positions - pending_buys)
        per_slot_budget = remaining_budget / float(remaining_slots)
        max_position_budget = total_limit * max(0.01, float(MAX_POSITION_BUDGET_PCT))
        target_budget = max(price, min(per_slot_budget, max_position_budget))
        qty = int(target_budget // price)
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
        now = QTime.currentTime()
        if QTime(8, 0, 0) <= now <= QTime(8, 50, 0):
            return "프리마켓"
        if QTime(9, 0, 0) <= now < QTime(15, 20, 0):
            return "정규장"
        if QTime(15, 30, 0) <= now <= QTime(20, 0, 0):
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
            feats = [0.5, 0.0, 0.0]
        else:
            pnl_pcts = [float(getattr(r, "pnl_pct", 0.0) or 0.0) for r in sells]
            wins = [p for p in pnl_pcts if p > 0]
            win_rate = len(wins) / float(max(1, len(pnl_pcts)))
            avg_pnl = sum(pnl_pcts) / float(max(1, len(pnl_pcts)))
            count_norm = min(1.0, len(pnl_pcts) / 20.0)
            feats = [win_rate, avg_pnl / 100.0, count_norm]
        self._trade_feature_cache[code] = (today, feats)
        return list(feats)

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
        sm = self.strategy_manager
        regime = getattr(sm, "last_market_regime", None) if sm is not None else None
        return str(getattr(regime, "label", "") or "").lower() == "bear"

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

    def _effective_max_positions(self) -> int:
        limit = int(MAX_CONCURRENT_POSITIONS)
        if self._is_market_weak():
            limit = min(limit, int(MARKET_WEAK_MAX_CONCURRENT_POSITIONS))
        if self._today_realized_pnl_pct() <= float(DAILY_LOSS_SOFT_LIMIT_PCT):
            limit = min(limit, int(DAILY_LOSS_MAX_CONCURRENT_POSITIONS))
        return max(1, limit)

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
        trade_win, trade_avg, trade_count = self._trade_features_for_code(code)
        ma_score = max(-0.5, min(0.5, (cur / ma20 - 1.0))) * 2.0
        rsi_score = max(-0.5, min(0.5, (rsi - 50.0) / 50.0)) + max(-0.2, min(0.2, (rsi - rsi_prev) / 20.0))
        vol_score = max(0.0, min(1.0, vol_ratio / 2.0))
        atr_penalty = max(0.0, min(0.8, atr_pct * 10.0))
        ai_score = 0.0
        proba_up = self._ai_proba_up(code, cur)
        if proba_up is not None:
            ai_score = (float(proba_up) - self._dynamic_ai_entry_min()) * 2.0
        loss_penalty = min(1.0, self._recent_loss_count_for_code(code) * 0.4)
        return (
            ma_score
            + rsi_score
            + vol_score
            + ai_score
            + (trade_win - 0.5)
            + trade_avg
            + (trade_count * 0.1)
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
                if score >= float(BUY_SCORE_MIN):
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
        if not bool(USE_ATR_RISK):
            return float(STOP_LOSS_PCT), float(TAKE_PROFIT_PCT)
        atr_pct = float((self.ma.get(code) or {}).get("atr_pct", 0.0) or 0.0)
        if atr_pct <= 0:
            return float(STOP_LOSS_PCT), float(TAKE_PROFIT_PCT)
        stop_abs = min(float(ATR_STOP_MAX_PCT), max(float(ATR_STOP_MIN_PCT), atr_pct * float(ATR_STOP_MULT)))
        take_abs = min(float(ATR_TAKE_MAX_PCT), max(float(ATR_TAKE_MIN_PCT), atr_pct * float(ATR_TAKE_MULT)))
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
        f11, f12, f13 = self._trade_features_for_code(code)

        # cur_price 기반 피처가 필요하다면 여기서 추가 가능(현재는 ret_1d/ma 비율로 대체)
        _ = cur_price
        return [f1, f2, f3, f4, f5, f6, f7, f8, f9, f10, f11, f12, f13]

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

    def train_random_forest(self) -> None:
        model, model_name = self._build_ai_classifier()
        if model is None:
            return

        try:
            today = datetime.date.today()
            end_date = today.strftime("%Y%m%d")

            X_all: List[List[float]] = []
            y_all: List[int] = []

            # 각 종목별로 학습 데이터를 만들고 합칩니다.
            # (학습 시 TR을 여러 번 호출하므로, 가능한 한 종목/샘플 수를 제한)
            train_count = AI_TRAIN_COUNT
            for code in self.stock_codes:
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
                trade_f1, trade_f2, trade_f3 = self._trade_features_for_code(code)

                # prefix sum으로 MA 합을 빠르게 계산
                # prefix[i] = sum(closes[:i]) ; sum(l..r)=prefix[r]-prefix[l]
                prefix = [0.0]
                s = 0.0
                for p in closes:
                    s += float(p)
                    prefix.append(s)

                def _sum(l: int, r_excl: int) -> float:
                    return prefix[r_excl] - prefix[l]

                # 인덱스 t는 "오늘", 라벨은 t+1
                # 필요 조건: ma20(19..t), vol_avg(19..t), rsi14(14..t) 확보
                start_t = max(20, int(ATR_PERIOD) + 1)
                horizon = max(1, int(AI_LABEL_HORIZON_DAYS))
                for t in range(start_t, len(closes) - horizon):
                    ma5 = _sum(t - 4, t + 1) / 5.0
                    ma20 = _sum(t - 19, t + 1) / 20.0
                    ma5_prev = _sum(t - 5, t) / 5.0
                    ma20_prev = _sum(t - 20, t - 1) / 20.0
                    if ma20 <= 0 or ma20_prev <= 0:
                        continue

                    rsi14 = rsi_series[t]
                    if rsi14 is None:
                        continue

                    vol_today = volumes[t]
                    vol_prev = volumes[t - 1]
                    vol_avg = sum(volumes[t - (VOL_AVG_PERIOD - 1) : t + 1]) / float(VOL_AVG_PERIOD)
                    if vol_avg <= 0:
                        continue
                    vol_ratio = vol_today / vol_avg
                    vol_chg = (vol_today / vol_prev - 1.0) if vol_prev > 0 else 0.0

                    close_today = closes[t]
                    close_prev = closes[t - 1]
                    if close_prev <= 0:
                        continue
                    ret_1d = close_today / close_prev - 1.0

                    # 변동성(최근 20일 일수익률 표준편차)
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
                    atr_pct = _compute_atr_pct(
                        highs_f[: t + 1],
                        lows_f[: t + 1],
                        closes_f[: t + 1],
                        ATR_PERIOD,
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

                    X_all.append([f1, f2, f3, f4, f5, f6, f7, f8, f9, f10, trade_f1, trade_f2, trade_f3])
                    entry = float(closes[t])
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
                    exit_price = float(closes[min(len(closes) - 1, t + horizon)])
                    for j in range(t + 1, min(len(closes), t + horizon + 1)):
                        if float(lows[j]) <= stop_price:
                            exit_price = stop_price
                            break
                        if float(highs[j]) >= take_price:
                            exit_price = take_price
                            break
                    net_ret = (exit_price / entry - 1.0) - float(AI_ROUND_TRIP_COST_PCT) - float(AI_SLIPPAGE_PCT)
                    y = 1 if net_ret > float(AI_RET_LABEL_THRESHOLD) else 0
                    y_all.append(y)

            if len(X_all) < AI_MIN_TOTAL_SAMPLES:
                # 표본이 부족하면 모델 학습 스킵
                return

            model.fit(X_all, y_all)
            self.ai_model = model
            self.ai_trained_date = today
            self._emit_log(
                f"[AI] 학습 완료: {model_name} samples={len(X_all)} "
                f"label=익절/손절 {int(AI_LABEL_HORIZON_DAYS)}일, 비용반영"
            )
            try:
                imps = list(getattr(model, "feature_importances_", []))
                if imps:
                    pairs = list(zip(AI_FEATURE_NAMES, imps))
                    pairs.sort(key=lambda x: float(x[1]), reverse=True)
                    top = ", ".join([f"{k}:{v:.3f}" for k, v in pairs[:5]])
                    self._emit_log(f"[AI] feature importance top5 -> {top}")
            except Exception:
                pass
        except Exception:
            # 학습 실패 시 전략은 기술적 필터만 사용
            return

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

    def sync_portfolio(self, password: str = "") -> None:
        # opw00018이 실패할 경우를 대비해 예외 처리합니다.
        try:
            data = self.kiwoom.request_holdings(self.account, password=password)
            if data is None:
                now = datetime.datetime.now()
                should_warn = (
                    self._last_positions_sync_warn_at is None
                    or (now - self._last_positions_sync_warn_at).total_seconds() >= 300
                )
                if should_warn:
                    self._emit_log(
                        "[HOLDINGS] 잔고조회 TR 미응답(타임아웃 등). 기존 보유(positions)는 유지합니다."
                    )
                    self._last_positions_sync_warn_at = now
                return
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
                self.last_holdings_tr_rows = len(normalized)
                self.positions = normalized
                hold_items = sorted(
                    (
                        (c, int((p or {}).get("qty", 0) or 0))
                        for c, p in normalized.items()
                        if int((p or {}).get("qty", 0) or 0) > 0
                    ),
                    key=lambda x: x[0],
                )
                brief = ", ".join(f"{c}:{q}" for c, q in hold_items)
                if brief != self._last_holdings_brief:
                    msg = brief if brief else "-"
                    self._emit_log(f"[HOLDINGS] 보유수량 요약 {msg} (TR행 {self.last_holdings_tr_rows})")
                    self._last_holdings_brief = brief
                # 보유 종목은 스캔 대상 여부와 무관하게 항상 중앙 리스트에 포함
                if normalized:
                    merged_codes = list(dict.fromkeys(list(self.stock_codes or []) + list(normalized.keys())))
                    self.stock_codes = merged_codes
                    for c in merged_codes:
                        self.pending_buy.setdefault(c, False)
                        self.pending_sell.setdefault(c, False)
                        self.tp_reached.setdefault(c, False)
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
                    pw_state = "설정됨" if str(password).strip() else "미설정"
                    self._emit_log(
                        f"[HOLDINGS] 잔고조회 결과 0건 (opw00018). 계좌비번={pw_state}. "
                        "키움 HTS 보유와 다르면 KIWOOM_ACCOUNT_PASSWORD 또는 --pw 확인 필요"
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

    def _should_buy_common(self, code: str) -> bool:
        if code in self.positions and self.positions[code].get("qty", 0) > 0:
            self._log_buy_block(code, "이미 보유중")
            return False
        if self.pending_buy.get(code, False):
            self._log_buy_block(code, "매수 미체결 진행중")
            return False
        cooldown_until = self.buy_cooldown_until.get(code)
        if isinstance(cooldown_until, datetime.datetime) and datetime.datetime.now() < cooldown_until:
            remain = int((cooldown_until - datetime.datetime.now()).total_seconds())
            self._log_buy_block(code, f"매수 timeout 쿨다운({remain}s)")
            return False
        today_loss_pct = self._today_realized_pnl_pct()
        if today_loss_pct <= float(DAILY_LOSS_HARD_LIMIT_PCT):
            self._log_buy_block(code, f"당일 손실한도 초과({today_loss_pct*100:.2f}%)")
            return False
        bt_mode = self._auto_bt_mode()
        if bool(AUTO_BT_BLOCK_BUY) and bt_mode == "block":
            self._log_buy_block(code, "자동백테스트 게이트 차단")
            return False
        recent_losses = self._recent_loss_count_for_code(code)
        if recent_losses >= int(RECENT_LOSS_BLOCK_COUNT):
            self._log_buy_block(
                code,
                f"최근 손절 반복({recent_losses}회/{int(RECENT_LOSS_LOOKBACK_DAYS)}일)",
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
        today = datetime.date.today()
        if self.last_buy_date.get(code) == today:
            self._log_buy_block(code, "당일 재매수 제한")
            return False
        if not self.ma.get(code):
            self._log_buy_block(code, "MA 데이터 없음")
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
            return True
        x = self._ai_features_from_cached(code, cur)
        if x is None:
            return False
        try:
            proba_up = float(self.ai_model.predict_proba([x])[0][1])
            return proba_up >= self._dynamic_ai_entry_min()
        except Exception:
            return False

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

        if cur < ma20 * (1.0 + MA20_ENTRY_GAP_PCT):
            return fail("MA20 대비 너무 낮음")
        price_to_ma20 = cur / ma20 - 1.0 if ma20 > 0 else 0.0
        if price_to_ma20 > MAX_PRICE_TO_MA20_PCT:
            return fail("MA20 대비 과열")

        if strategy_id == STRATEGY_VOLUME:
            if not (ma5 > ma20):
                return fail("MA5<=MA20")
            if rsi14 < (RSI_ENTRY_MIN - 3.0) or rsi14 > (RSI_ENTRY_MAX + 2.0):
                return fail("RSI 범위 이탈")
            if rsi14 <= rsi14_prev:
                return fail("RSI 상승 아님")
            if (rsi14 - rsi14_prev) < (RSI_MIN_DELTA * 0.5):
                return fail("RSI 상승폭 부족")
            if vol_ratio < (VOL_ENTRY_RATIO_MIN * 1.08):
                return fail("거래량 평균비 부족")
            # 공격형: volume 전략도 전일 대비 거래량 감소 일부를 허용
            g_min = max(VOL_ENTRY_GROWTH_MIN, 0.96)
            if vol_prev > 0 and vol_today < int(vol_prev * g_min):
                return fail("전일대비 거래량 부족")
            return True

        if strategy_id == STRATEGY_TREND:
            if not (ma5 > ma20):
                return fail("MA5<=MA20")
            if rsi14 < RSI_ENTRY_MIN or rsi14 > RSI_ENTRY_MAX:
                return fail("RSI 범위 이탈")
            if rsi14 <= rsi14_prev:
                return fail("RSI 상승 아님")
            if (rsi14 - rsi14_prev) < RSI_MIN_DELTA:
                return fail("RSI 상승폭 부족")
            if vol_ratio < (VOL_ENTRY_RATIO_MIN * 0.85):
                return fail("거래량 평균비 부족")
            if vol_prev > 0 and vol_today < int(vol_prev * (VOL_ENTRY_GROWTH_MIN * 0.95)):
                return fail("전일대비 거래량 부족")
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
        if vol_ratio < VOL_ENTRY_RATIO_MIN:
            return fail("거래량 평균비 부족")
        if vol_prev > 0 and vol_today < int(vol_prev * VOL_ENTRY_GROWTH_MIN):
            return fail("전일대비 거래량 부족")
        if not self._ai_buy_filter(code, cur):
            return fail("AI 상승확률 부족")
        return True

    def _should_buy(self, code: str) -> bool:
        if not self._should_buy_common(code):
            return False
        v = self.ma[code]
        cur = float(self.current_price[code])
        sm = self.strategy_manager
        sid = sm.active_strategy_id if sm is not None else STRATEGY_AI
        ok = self._should_buy_for_strategy(code, v, cur, sid)
        return ok

    def _sell_reason(self, code: str) -> str:
        pos = self.positions.get(code)
        if not pos or pos.get("qty", 0) <= 0:
            return ""
        if self.pending_sell.get(code, False):
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
                    # 아직 팔지 않고, 이후 하락 시 트레일링 스탑으로 정리
                    if bool(PARTIAL_TAKE_PROFIT_ENABLED) and int(pos.get("qty", 0) or 0) > 1:
                        return "partial_take_profit"
                    return ""
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

    def buy_market(self, code: str, qty_override: Optional[int] = None) -> None:
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
        qty = int(qty_override) if qty_override is not None else self._budget_based_order_qty(float(cur))
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

        prev_qty_before = int((self.positions.get(code) or {}).get("qty", 0))
        self.kiwoom.send_order_market(self.account, code, qty, ORDER_TYPE_BUY)
        self.pending_orders[code] = {
            "side": "buy",
            "qty": qty,
            "prev_qty": prev_qty_before,
            "sent_at": datetime.datetime.now(),
        }
        self._append_order_event(
            event="ORDER_SENT",
            code=code,
            side="BUY",
            qty=qty,
            price=float(cur),
        )
        self._emit_log(f"[BUY-ORDER] {code} qty={qty} ref={cur} budget_based={bool(BUDGET_BASED_ORDER_QTY)}")

    def sell_market(self, code: str, reason: str = "") -> None:
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

    def _reconcile_orders_and_portfolio(self) -> None:
        """
        체결확인 -> 포트폴리오 업데이트.
        Chejan 이벤트가 없더라도 잔고 동기화로 체결 여부를 추정한다.
        """
        if not self.pending_orders:
            return
        try:
            self.sync_portfolio(password=self._account_password if hasattr(self, "_account_password") else "")
        except Exception:
            return

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
                if cur_qty >= max(1, ord_qty):
                    self.pending_buy[code] = False
                    self.last_buy_date[code] = datetime.date.today()
                    self.entry_time[code] = datetime.datetime.now()
                    self.tp_reached[code] = False
                    prev_qty_before = int(meta.get("prev_qty", 0) or 0)
                    filled_qty = max(0, cur_qty - prev_qty_before)
                    avg_price = int(pos.get("avg_price", 0) or 0)
                    if filled_qty > 0 and avg_price > 0:
                        self._append_order_event(
                            event="ORDER_FILLED",
                            code=code,
                            side="BUY",
                            qty=int(filled_qty),
                            price=float(avg_price),
                        )
                        self._notify_trade_fill(
                            {
                                "side": "BUY",
                                "code": code,
                                "price": float(avg_price),
                                "qty": int(filled_qty),
                                "ts": datetime.datetime.now(),
                            }
                        )
                    to_clear.append(code)
                    self._emit_log(f"[FILLED-BUY] {code} qty={cur_qty}")
                elif elapsed > 25:
                    self.pending_buy[code] = False
                    self.buy_cooldown_until[code] = now + datetime.timedelta(
                        seconds=float(ORDER_TIMEOUT_COOLDOWN_SEC)
                    )
                    to_clear.append(code)
                    self._append_order_event(
                        event="ORDER_TIMEOUT",
                        code=code,
                        side="BUY",
                        qty=int(ord_qty),
                        note=f"pending {int(elapsed)}s; cooldown {int(ORDER_TIMEOUT_COOLDOWN_SEC)}s",
                    )
                    self._emit_log(
                        f"[WARN] buy pending timeout {code} - {int(ORDER_TIMEOUT_COOLDOWN_SEC)}s 재주문 보류"
                    )
            elif side == "sell":
                prev_qty = int(meta.get("prev_qty", ord_qty) or ord_qty)
                if cur_qty < prev_qty:
                    self.pending_sell[code] = False
                    sold_qty = prev_qty - cur_qty
                    prev_avg = int(meta.get("prev_avg_price", 0) or 0)
                    sell_reason = str(meta.get("reason", "") or "")
                    sell_price = float(self.current_price.get(code) or prev_avg)
                    if sold_qty > 0 and prev_avg > 0:
                        self._append_order_event(
                            event="ORDER_FILLED",
                            code=code,
                            side="SELL",
                            qty=int(sold_qty),
                            price=float(sell_price),
                            note=f"reason={sell_reason}" if sell_reason else "",
                        )
                        realized = (sell_price - float(prev_avg)) * float(sold_qty)
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
                                "qty": int(sold_qty),
                                "pnl_pct": float(pnl_pct),
                                "realized_pnl_krw": float(realized),
                                "sell_reason": sell_reason,
                                "ts": datetime.datetime.now(),
                            }
                        )
                    if cur_qty <= 0:
                        self.tp_reached[code] = False
                        self._scale_in_done[code] = False
                    to_clear.append(code)
                    self._emit_log(f"[FILLED-SELL] {code} qty={sold_qty} reason={sell_reason or '-'}")
                elif elapsed > 25:
                    self.pending_sell[code] = False
                    to_clear.append(code)
                    self._append_order_event(
                        event="ORDER_TIMEOUT",
                        code=code,
                        side="SELL",
                        qty=int(ord_qty),
                        note=f"pending {int(elapsed)}s",
                    )
                    self._emit_log(f"[WARN] sell pending timeout {code}")
        for code in to_clear:
            self.pending_orders.pop(code, None)

    def _emit_daily_report_if_needed(self) -> None:
        now = datetime.datetime.now()
        if self._daily_report_done_date == now.date():
            return
        # 정규장 종료 이후에만 1회 출력
        if QTime.currentTime() < QTime(15, 30, 0):
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
        self._daily_report_done_date = now.date()

    def on_tick(self):
        today = datetime.date.today()
        self._maybe_reset_daily_state(today)

        if not self._is_market_open():
            self._sync_holding_current_prices(interval_sec=30.0)
            self._emit_daily_report_if_needed()
            return

        self._maybe_start_auto_backtest()

        # 체결확인 -> 포트폴리오 업데이트
        self._reconcile_orders_and_portfolio()
        self._maybe_refresh_ma_step()
        self._sync_holding_current_prices()

        sm = self.strategy_manager
        if sm is not None and self.stock_codes:
            sm.schedule_reevaluation(self.stock_codes)

        if self._need_portfolio_sync:
            self.sync_portfolio(password=self._account_password if hasattr(self, "_account_password") else "")
            self._need_portfolio_sync = False

        # AI 모델은 하루 1회만(시장 상황에 따라 바꾸고 싶으면 여기 튜닝)
        if AI_AVAILABLE and self.ai_model is None and self.ai_trained_date != today:
            # 학습은 TR 호출이 많아 약간 시간이 걸릴 수 있으므로, 하루 1회만 수행합니다.
            self.train_random_forest()

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
        # 시작 시 잔고만 동기화( MA는 라운드로빈으로 점진 갱신 )
        self.sync_portfolio(password=account_password)
        today = datetime.date.today()
        self._last_ma_refresh_date = today
        self._need_portfolio_sync = False  # start에서 이미 sync 했으므로

        # 오늘 MA 갱신은 점진적으로 수행
        self._ma_needed_codes = set(self.stock_codes)
        self._ma_refresh_idx = 0
        self._next_ma_refresh_at = datetime.datetime.now()

        # 실시간 등록: "주식체결" fid 10(현재가)
        self.kiwoom.set_real_reg(self.stock_codes, fid_list="10")

        self.timer.start()

    def update_targets(self, codes: List[str]) -> None:
        """스캔 결과 종목으로 매매 대상을 런타임에 갱신."""
        ordered = list(dict.fromkeys([str(c).strip() for c in codes if str(c).strip()]))
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
    scan_finished = pyqtSignal(list)
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
        self.resize(1280, 760)
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
        self.lbl_total_pnl = QLabel("누적손익금액: 0원")
        self.lbl_total_ret = QLabel("누적수익률: 0.00%")
        self.lbl_est_asset = QLabel(f"추정예탁자산: {int(TOTAL_LIMIT_KRW):,}원")
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
        self.lbl_strategy = QLabel("전략: -")
        for w in (
            self.lbl_conn_time,
            self.lbl_keepalive,
            self.lbl_positions,
            self.lbl_waiting,
            self.lbl_strategy,
        ):
            w.setStyleSheet("background:#ffffff; border:1px solid #dce3ef; border-radius:6px; padding:4px 8px;")
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
        row2.addWidget(self.lbl_strategy)
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

        row_mid = QHBoxLayout()
        for w in (self.lbl_total_pnl, self.lbl_total_ret, self.lbl_est_asset):
            w.setStyleSheet("background:#ffffff; border:1px solid #dce3ef; border-radius:6px; padding:4px 8px;")
            row_mid.addWidget(w)
        row_mid.addStretch(1)

        layout.addLayout(row1)
        layout.addLayout(row_mid)
        layout.addLayout(row2)

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
        self.tbl_waiting.verticalHeader().setDefaultSectionSize(24)
        self.tbl_waiting.setMinimumHeight(360)
        self.tbl_orders = QTableWidget(0, 7)
        self.tbl_orders.setHorizontalHeaderLabels(
            ["종목", "매수가", "체결가", "주문상태", "수익", "수량/잔량", "비고"]
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
            ["종목", "매수가", "체결가", "주문상태", "수익", "수량/잔량", "비고"]
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

    def _on_scan_finished(self, picked: List[str]) -> None:
        if self.kiwoom is None:
            return
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
            base_capital = float(TOTAL_LIMIT_KRW) if float(TOTAL_LIMIT_KRW) > 0 else 1.0
            cum_ret_pct = (cumulative_pnl / base_capital) * 100.0
            est_asset = float(TOTAL_LIMIT_KRW) + cumulative_pnl
            self.lbl_total_pnl.setText(f"누적손익금액: {int(cumulative_pnl):+,}원")
            self.lbl_total_ret.setText(f"누적수익률: {cum_ret_pct:+.2f}%")
            self.lbl_est_asset.setText(f"추정예탁자산: {int(est_asset):,}원")
            if self.kiwoom is not None:
                try:
                    st = int(self.kiwoom.dynamicCall("GetConnectState()") or 0)
                    self.lbl_server.setText("서버상태: 연결됨" if st == 1 else "서버상태: 끊김")
                except Exception:
                    self.lbl_server.setText("서버상태: 확인실패")
            elapsed = int((datetime.datetime.now() - self.engine.connected_at).total_seconds())
            hh = elapsed // 3600
            mm = (elapsed % 3600) // 60
            ss = elapsed % 60
            self.lbl_conn_time.setText(f"서버연결시간: {hh:02d}:{mm:02d}:{ss:02d}")
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
            sm = getattr(self.engine, "strategy_manager", None)
            if sm is not None:
                self.lbl_strategy.setText(sm.summary_line())
            else:
                self.lbl_strategy.setText("전략: -")
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
        try:
            self.log_message.emit(str(text))
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
                    remain_str = f"{req_qty:,} / {req_qty:,}" if req_qty > 0 else "-"
                    order_rows.append([symbol, buy_px_str, "-", status, "-", remain_str, "-"])
                    order_keys.add((code, status))

            # 최근 체결된 매수/매도도 우측 "매수/매도종목"에 함께 표시
            try:
                now_dt = datetime.datetime.now()
                cutoff = now_dt - datetime.timedelta(days=7)
                records = self.trade_tracker.all_records() if self.trade_tracker is not None else []
                for r in reversed(records):
                    if r.ts < cutoff:
                        continue
                    side = str(r.side).upper()
                    if side not in ("BUY", "SELL"):
                        continue
                    code = str(r.code).strip()
                    if len(code) != 6 or not code.isdigit():
                        continue
                    status = "매수 체결" if side == "BUY" else "매도 체결"
                    key = (code, status)
                    if key in order_keys:
                        continue
                    name = self.code_names.get(code, code)
                    if not str(name).strip() or str(name).strip() == str(code):
                        try:
                            symbol = str(self.kiwoom.get_master_code_name(code)) if self.kiwoom is not None else str(code)
                        except Exception:
                            symbol = str(code)
                    else:
                        symbol = str(name)
                    exec_px = int(float(r.price) or 0.0)
                    exec_px_str = f"{exec_px:,}" if exec_px > 0 else "-"
                    buy_px = 0
                    if side == "BUY":
                        # 매수 체결은 체결가 자체를 매수가로 사용
                        buy_px = exec_px
                    else:
                        # 매도 체결은 수익률(우선) 또는 실현손익으로 원매수가 역산
                        pnl_pct = getattr(r, "pnl_pct", None)
                        pnl_krw = float(getattr(r, "realized_pnl_krw", 0.0) or 0.0)
                        qty_v = max(1, int(getattr(r, "qty", 0) or 0))
                        if pnl_pct is not None:
                            try:
                                denom = 1.0 + (float(pnl_pct) / 100.0)
                                if abs(denom) > 1e-9:
                                    buy_px = int(round(float(exec_px) / denom))
                            except Exception:
                                buy_px = 0
                        if buy_px <= 0:
                            try:
                                buy_px = int(round(float(exec_px) - (pnl_krw / float(qty_v))))
                            except Exception:
                                buy_px = 0
                    buy_px_str = f"{buy_px:,}" if buy_px > 0 else "-"
                    profit_str = "-"
                    if side == "SELL":
                        pnl_krw = float(getattr(r, "realized_pnl_krw", 0.0) or 0.0)
                        pnl_pct = getattr(r, "pnl_pct", None)
                        if pnl_pct is not None:
                            profit_str = f"{int(pnl_krw):+,} / {float(pnl_pct):+.2f}%"
                        else:
                            profit_str = f"{int(pnl_krw):+,}"
                    qty_v = int(getattr(r, "qty", 0) or 0)
                    remain_str = f"{qty_v:,} / 0" if qty_v > 0 else "-"
                    note = r.ts.strftime("%m-%d %H:%M:%S")
                    order_rows.append([symbol, buy_px_str, exec_px_str, status, profit_str, remain_str, note])
                    order_keys.add(key)
            except Exception:
                pass

            self.tbl_holdings.setRowCount(len(holding_rows))
            for i, vals in enumerate(holding_rows):
                for c, v in enumerate(vals):
                    item = QTableWidgetItem(str(v))
                    item.setTextAlignment(Qt.AlignCenter)
                    self.tbl_holdings.setItem(i, c, item)
            self.tbl_waiting.setRowCount(len(waiting_rows))
            for i, vals in enumerate(waiting_rows):
                for c, v in enumerate(vals):
                    self.tbl_waiting.setItem(i, c, QTableWidgetItem(str(v)))
            hidden_waiting = max(0, len(waiting_codes) - len(waiting_rows))
            if hidden_waiting > 0:
                i = self.tbl_waiting.rowCount()
                self.tbl_waiting.insertRow(i)
                summary = f"... 외 {hidden_waiting:,}종목"
                for c, v in enumerate([summary, "-", "진입 대기", "표시 제한"]):
                    self.tbl_waiting.setItem(i, c, QTableWidgetItem(str(v)))
            self.tbl_orders.setRowCount(len(order_rows))
            for i, vals in enumerate(order_rows):
                for c, v in enumerate(vals):
                    self.tbl_orders.setItem(i, c, QTableWidgetItem(str(v)))
            # 리사이즈 비용이 커서(특히 테스트스캔 ON 대량 행) 변화 시에만 간헐 수행
            now_resize = datetime.datetime.now()
            shape = (len(holding_rows), len(waiting_rows), len(order_rows))
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
            if not os.environ.get("KIWOOM_ACCOUNT_NO", "").strip():
                window.scan_message.emit(
                    f"[BOOT] KIWOOM_ACCOUNT_NO 미설정 - 기본 설정 계좌 {DEFAULT_KIWOOM_ACCOUNT_NO} 사용을 시도합니다."
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
            engine.strategy_manager = StrategyManager()
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
                try:
                    picked = scan_market(kiwoom, top_n=SCAN_TOP_N)
                except Exception as e:
                    _log(f"[SCAN] background failed: {e}")
                    _scan_console(f"[SCAN] 백그라운드 실패: {e}")
                    window.scan_message.emit(f"[SCAN] 실패: {e}")
                    picked = []
                elapsed = int((datetime.datetime.now() - started).total_seconds())
                _scan_console(f"[SCAN] 백그라운드 검색 종료: {len(picked)}종목, {elapsed}s")
                window.scan_message.emit(f"[SCAN] 완료: {len(picked)}종목 선정, {elapsed}s 소요")
                window.scan_finished.emit(picked)
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
            # Kiwoom 로그인 실패/환경 문제를 명확히 노출하고 종료
            print(f"[ERROR] Kiwoom login failed: {e}", flush=True)
            window.lbl_hint.setText(f"로그인 실패: {e}")
            app.quit()

    QTimer.singleShot(0, _start_after_event_loop)
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()

