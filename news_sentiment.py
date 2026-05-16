"""
news_sentiment.py — 종목별 뉴스 감성 점수 계산 모듈

무료 소스:
  1. 네이버 금융 뉴스 (종목코드 기반 크롤링)
  2. DART 전자공시 (환경변수 DART_API_KEY 설정 시 활성화)

점수 범위: -1.0 (매우 부정) ~ +1.0 (매우 긍정)
캐시 TTL: 기본 30분 (NEWS_CACHE_TTL_SEC)
"""
from __future__ import annotations

import os
import re
import time
import threading
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from urllib.request import Request, urlopen
from urllib.error import URLError
from urllib.parse import urlencode

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 환경변수 설정
# ---------------------------------------------------------------------------
DART_API_KEY: str = os.environ.get("DART_API_KEY", "").strip()
NEWS_CACHE_TTL_SEC: float = float(os.environ.get("NEWS_CACHE_TTL_SEC", "1800"))  # 30분
NEWS_MAX_ARTICLES: int = int(os.environ.get("NEWS_MAX_ARTICLES", "20"))
NEWS_FETCH_TIMEOUT_SEC: float = float(os.environ.get("NEWS_FETCH_TIMEOUT_SEC", "8"))

# ---------------------------------------------------------------------------
# 한국 금융 감성 사전 (긍정/부정 키워드)
# ---------------------------------------------------------------------------
_POSITIVE_KEYWORDS: List[str] = [
    # 실적·성장
    "호실적", "영업이익", "순이익", "흑자", "매출증가", "성장", "최대실적", "어닝서프라이즈",
    "실적개선", "흑자전환", "수익성", "증익", "최대매출", "사상최대", "역대최대",
    "실적호조", "이익증가", "수익증가", "매출성장", "실적상회",
    # 수주·계약
    "수주", "계약체결", "MOU", "협약", "수주잔고", "대형계약", "수출계약",
    "수주성공", "계약수주", "납품계약", "공급계약",
    # 주가·투자
    "상향", "목표주가상향", "매수의견", "투자의견상향", "강력매수", "신고가",
    "급등", "강세", "반등", "돌파", "상승전환", "저평가", "매력적",
    # 배당·주주환원
    "배당증가", "자사주매입", "주주환원", "특별배당", "배당확대", "자사주소각",
    # 신제품·특허·승인
    "신제품출시", "특허취득", "FDA승인", "임상성공", "신약승인", "제품출시",
    "기술이전", "인허가", "CE인증", "양산개시",
    # 긍정적 이벤트
    "우량", "안정", "호조", "선전", "양호", "회복", "개선", "턴어라운드",
    "수혜", "기대감", "모멘텀", "상승동력", "실적기대", "전망밝음",
    "구조조정완료", "체질개선", "경쟁력강화", "시장확대", "점유율확대",
    # 협력·파트너십
    "파트너십", "전략적제휴", "합작", "인수완료", "M&A성공",
]

_NEGATIVE_KEYWORDS: List[str] = [
    # 실적 악화
    "적자", "영업손실", "순손실", "실적쇼크", "매출감소", "어닝쇼크", "적자전환",
    "수익성악화", "감익", "최저실적", "실적부진", "이익감소", "손실확대",
    "실적하회", "가이던스하회", "미달",
    # 리스크·법적
    "소송", "과징금", "제재", "압수수색", "검찰", "횡령", "배임", "부정",
    "사기", "분식", "불공정거래", "내부자거래", "고발", "기소", "피의자",
    "벌금", "징계", "조사착수",
    # 주가·투자
    "하향", "목표주가하향", "매도의견", "투자의견하향", "신저가",
    "급락", "약세", "폭락", "붕괴", "하락전환", "추가하락",
    # 부정적 사건
    "리콜", "결함", "생산중단", "공장화재", "사망", "부도", "파산", "워크아웃",
    "상장폐지", "관리종목", "불성실공시", "감사의견거절",
    "파업", "노조", "사죄", "사과", "경영위기", "유동성위기",
    "구조조정", "감원", "희망퇴직", "대규모해고",
    # 거시·업황
    "규제강화", "무역분쟁", "관세", "공급과잉", "수요부진",
    "경기침체", "불황", "위기", "불확실성", "리스크", "우려",
    "원가상승", "비용증가", "마진압박", "환율리스크",
]

# 가중치: 강도가 높은 키워드에 더 높은 점수 부여
_HIGH_WEIGHT_POSITIVE = {
    "급등", "신고가", "어닝서프라이즈", "FDA승인", "신약승인", "최대실적",
    "흑자전환", "사상최대", "역대최대", "턴어라운드",
}
_HIGH_WEIGHT_NEGATIVE = {
    "급락", "신저가", "파산", "부도", "상장폐지", "어닝쇼크",
    "횡령", "배임", "검찰", "경영위기", "감사의견거절",
}


def _keyword_score(texts: List[str]) -> float:
    """텍스트 리스트에서 감성 점수 계산 (-1.0 ~ +1.0)."""
    combined = " ".join(texts)
    pos = sum(2.0 if kw in _HIGH_WEIGHT_POSITIVE else 1.0 for kw in _POSITIVE_KEYWORDS if kw in combined)
    neg = sum(2.0 if kw in _HIGH_WEIGHT_NEGATIVE else 1.0 for kw in _NEGATIVE_KEYWORDS if kw in combined)
    total = pos + neg
    if total == 0:
        return 0.0
    return round((pos - neg) / total, 4)


# ---------------------------------------------------------------------------
# 네이버 금융 뉴스 크롤러
# ---------------------------------------------------------------------------
_NAVER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://finance.naver.com/",
    "Accept-Language": "ko-KR,ko;q=0.9",
}


def _fetch_naver_news(code: str, max_articles: int = NEWS_MAX_ARTICLES) -> List[str]:
    """네이버 금융 종목 뉴스 제목 목록 반환."""
    url = (
        f"https://finance.naver.com/item/news_news.naver"
        f"?code={code}&page=1&sm=title_entity_id.basic&clusterId="
    )
    try:
        req = Request(url, headers=_NAVER_HEADERS)
        with urlopen(req, timeout=NEWS_FETCH_TIMEOUT_SEC) as resp:
            raw = resp.read()
        # EUC-KR 먼저 시도, 실패 시 UTF-8
        for enc in ("euc-kr", "utf-8", "cp949"):
            try:
                html = raw.decode(enc)
                break
            except Exception:
                continue
        else:
            html = raw.decode("euc-kr", errors="replace")
    except Exception as e:
        logger.debug("[NEWS] 네이버 뉴스 요청 실패 %s: %s", code, e)
        return []

    # HTML 엔티티 간단 제거
    html = re.sub(r"&[a-zA-Z]+;", " ", html)
    html = re.sub(r"&#\d+;", " ", html)

    # <a class="tit"> ... </a> 패턴에서 제목 추출
    titles = re.findall(r'class="tit"[^>]*>\s*([^<]{4,120})\s*<', html)
    # 추가로 <td class="title"> 패턴도 시도
    if not titles:
        titles = re.findall(r'<td class="title"[^>]*>.*?<a[^>]+>([^<]{4,120})</a>', html, re.S)
    # <a> 태그 안 텍스트 일반 패턴
    if not titles:
        titles = re.findall(r'<a[^>]+class="[^"]*tit[^"]*"[^>]*>([^<]{4,120})</a>', html)

    cleaned = [re.sub(r"\s+", " ", t).strip() for t in titles if t.strip()]
    return cleaned[:max_articles]


# ---------------------------------------------------------------------------
# DART 전자공시 크롤러 (API 키 있을 때만)
# ---------------------------------------------------------------------------
def _fetch_dart_disclosures(code: str, days: int = 7) -> List[str]:
    """DART 최근 공시 제목 목록 반환 (DART_API_KEY 필요)."""
    if not DART_API_KEY:
        return []
    today = datetime.now()
    bgn = (today - timedelta(days=days)).strftime("%Y%m%d")
    end = today.strftime("%Y%m%d")
    params = urlencode({
        "crtfc_key": DART_API_KEY,
        "stock_code": code,
        "bgn_de": bgn,
        "end_de": end,
        "page_no": 1,
        "page_count": 20,
    })
    url = f"https://opendart.fss.or.kr/api/list.json?{params}"
    try:
        req = Request(url)
        with urlopen(req, timeout=NEWS_FETCH_TIMEOUT_SEC) as resp:
            import json
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        if data.get("status") != "000":
            return []
        return [item.get("report_nm", "") for item in data.get("list", [])]
    except Exception as e:
        logger.debug("[NEWS] DART 요청 실패 %s: %s", code, e)
        return []


# ---------------------------------------------------------------------------
# 캐시 (종목코드 → (score, fetched_at))
# ---------------------------------------------------------------------------
_cache: Dict[str, Tuple[float, float]] = {}
_cache_lock = threading.Lock()


def get_news_sentiment(code: str, force: bool = False) -> float:
    """
    종목코드에 대한 감성 점수를 반환 (-1.0 ~ +1.0).
    캐시 TTL(기본 30분) 이내이면 캐시 값 재사용.
    force=True 이면 강제 갱신.
    """
    now = time.time()
    with _cache_lock:
        if not force and code in _cache:
            score, fetched_at = _cache[code]
            if now - fetched_at < NEWS_CACHE_TTL_SEC:
                return score

    titles: List[str] = []

    # 1) 네이버 금융 뉴스
    naver_titles = _fetch_naver_news(code)
    titles.extend(naver_titles)

    # 2) DART 공시 (API 키 있을 때)
    dart_titles = _fetch_dart_disclosures(code)
    # 공시는 중요도가 높아 2배 가중
    titles.extend(dart_titles * 2)

    score = _keyword_score(titles) if titles else 0.0

    with _cache_lock:
        _cache[code] = (score, now)

    logger.debug(
        "[NEWS] %s 뉴스=%d건 공시=%d건 → score=%.4f",
        code, len(naver_titles), len(dart_titles), score,
    )
    return score


def prefetch_news_batch(codes: List[str]) -> Dict[str, float]:
    """
    여러 종목 뉴스를 순차적으로 미리 가져옴.
    on_tick 외부 스레드에서 호출 권장 (네트워크 I/O 블로킹).
    """
    results: Dict[str, float] = {}
    for code in codes:
        try:
            results[code] = get_news_sentiment(code)
            time.sleep(0.3)  # 과도한 요청 방지
        except Exception as e:
            logger.debug("[NEWS] prefetch 실패 %s: %s", code, e)
            results[code] = 0.0
    return results


def cache_summary() -> str:
    """현재 캐시 상태 요약 문자열 반환 (로그 출력용)."""
    with _cache_lock:
        if not _cache:
            return "뉴스 캐시 없음"
        now = time.time()
        lines = []
        for code, (score, fetched_at) in sorted(_cache.items()):
            age_min = int((now - fetched_at) / 60)
            bar = "+" if score > 0.1 else ("-" if score < -0.1 else "=")
            lines.append(f"{code}:{score:+.2f}{bar}({age_min}분전)")
        return " | ".join(lines)
