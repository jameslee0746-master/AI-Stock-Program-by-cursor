"""
news_sentiment.py — 종목별 뉴스 감성 점수 계산 모듈

신뢰 소스(본문 우선·가중치↑):
  1. 네이버 증권 JSON (제목+본문) → 가능하면 기사 페이지 본문까지
  2. 매일경제 증권 RSS → 링크 본문 전체(상한 내)
  3. 한국경제 증권 RSS → 링크 본문 전체(상한 내)
  4. 연합뉴스 경제 RSS → 링크 본문 전체(상한 내)
  5. DART 전자공시 (환경변수 DART_API_KEY 설정 시, 제목·가중↑)

보조 소스(요약·가중치↓):
  - 다음 뉴스 검색, 구글 뉴스 RSS

문맥 이해(선택):
  - Gemini 무료 API 키가 있으면 신뢰 기사 본문을 보내 호재/악재 점수 산출
  - 키: 환경변수 GEMINI_API_KEY 또는 data/gemini_api_key.txt
  - 키 없으면 기존 키워드 점수만 사용

점수 범위: -1.0 (매우 부정) ~ +1.0 (매우 긍정)
캐시 TTL: 기본 30분 (NEWS_CACHE_TTL_SEC)
"""
from __future__ import annotations

import os
import re
import time
import json
import hashlib
import threading
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from xml.etree import ElementTree as ET

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 환경변수 설정
# ---------------------------------------------------------------------------
DART_API_KEY: str = os.environ.get("DART_API_KEY", "").strip()
NEWS_CACHE_TTL_SEC: float = float(os.environ.get("NEWS_CACHE_TTL_SEC", "1800"))  # 30분
NEWS_MAX_ARTICLES: int = int(os.environ.get("NEWS_MAX_ARTICLES", "12"))
NEWS_FETCH_TIMEOUT_SEC: float = float(os.environ.get("NEWS_FETCH_TIMEOUT_SEC", "8"))
# 보조 소스(검색/구글) 요약 길이
NEWS_BODY_MAX_CHARS: int = int(os.environ.get("NEWS_BODY_MAX_CHARS", "400"))
# 신뢰 매체 본문 길이 (전문에 가깝게)
NEWS_TRUSTED_BODY_MAX_CHARS: int = int(os.environ.get("NEWS_TRUSTED_BODY_MAX_CHARS", "3500"))
# 종목당 본문 풀페치 상한 (지연·차단 방지)
NEWS_FULL_FETCH_MAX: int = int(os.environ.get("NEWS_FULL_FETCH_MAX", "4"))
NEWS_TRUSTED_WEIGHT: float = float(os.environ.get("NEWS_TRUSTED_WEIGHT", "2.0"))
NEWS_SECONDARY_WEIGHT: float = float(os.environ.get("NEWS_SECONDARY_WEIGHT", "0.55"))
NEWS_DART_WEIGHT: float = float(os.environ.get("NEWS_DART_WEIGHT", "2.5"))
# Gemini (무료 키) — 신뢰 기사 문맥 판정
GEMINI_MODEL: str = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite").strip()
NEWS_GEMINI_ENABLED: bool = os.environ.get("NEWS_GEMINI_ENABLED", "1").strip().lower() in (
    "1",
    "true",
    "yes",
    "y",
)
NEWS_GEMINI_MAX_ARTICLES: int = int(os.environ.get("NEWS_GEMINI_MAX_ARTICLES", "3"))
NEWS_GEMINI_BLEND: float = float(os.environ.get("NEWS_GEMINI_BLEND", "0.90"))  # LLM 비중(추론↑)
NEWS_GEMINI_TIMEOUT_SEC: float = float(os.environ.get("NEWS_GEMINI_TIMEOUT_SEC", "20"))
NEWS_GEMINI_MIN_CHARS: int = int(os.environ.get("NEWS_GEMINI_MIN_CHARS", "120"))
# 일일 전략 패키지 선택에 Gemini 추론 우선 (학습 RF보다 큰 비중)
STRATEGY_GEMINI_ENABLED: bool = os.environ.get("STRATEGY_GEMINI_ENABLED", "1").strip().lower() in (
    "1",
    "true",
    "yes",
    "y",
)
STRATEGY_GEMINI_TIMEOUT_SEC: float = float(os.environ.get("STRATEGY_GEMINI_TIMEOUT_SEC", "25"))
# 테마(반도체·조선·원자력·금융·방산 등) 강세 추론
THEME_STRENGTH_ENABLED: bool = os.environ.get("THEME_STRENGTH_ENABLED", "1").strip().lower() in (
    "1",
    "true",
    "yes",
    "y",
)
THEME_STRENGTH_TTL_SEC: float = float(os.environ.get("THEME_STRENGTH_TTL_SEC", "3600"))  # 1시간
THEME_BUY_SCORE_WEIGHT: float = float(os.environ.get("THEME_BUY_SCORE_WEIGHT", "0.55"))
THEME_STRONG_MIN: float = float(os.environ.get("THEME_STRONG_MIN", "0.35"))
THEME_WEAK_MAX: float = float(os.environ.get("THEME_WEAK_MAX", "-0.35"))
THEME_GEMINI_TIMEOUT_SEC: float = float(os.environ.get("THEME_GEMINI_TIMEOUT_SEC", "30"))

# 테마 id → 한글 라벨
THEME_LABELS: Dict[str, str] = {
    "semiconductor": "반도체",
    "shipbuilding": "조선",
    "nuclear": "원자력",
    "finance": "금융",
    "defense": "방산",
    "bio": "바이오",
    "battery": "2차전지",
    "auto": "자동차",
    "telecom": "통신",
    "retail": "유통",
}
THEME_IDS: Tuple[str, ...] = tuple(THEME_LABELS.keys())

# 종목명/텍스트 → 테마 (키워드 매칭)
THEME_NAME_KEYWORDS: Dict[str, Tuple[str, ...]] = {
    "semiconductor": (
        "반도체", "하이닉스", "삼성전자", "원익", "주성엔지니어링", "이오테크닉스",
        "리노공업", "하나머티리얼즈", "DB하이텍", "솔브레인", "파크시스템스",
        "웨이퍼", "파운드리", "HBM", "DRAM", "낸드", "노광", "식각",
    ),
    "shipbuilding": (
        "조선", "한화오션", "삼성중공업", "HD현대중공업", "현대미포", "현대삼호",
        "힘스", "HD한국조선해양", "선박", "LNG선", "수주",
    ),
    "nuclear": (
        "원자력", "원전", "두산에너빌리티", "한전기술", "한전KPS", "우라늄",
        "SMR", "원자로",
    ),
    "finance": (
        "금융", "은행", "증권", "보험", "KB금융", "신한지주", "하나금융", "우리금융",
        "삼성화재", "삼성생명", "메리츠", "미래에셋", "NH투자", "키움증권",
    ),
    "defense": (
        "방산", "한화에어로스페이스", "LIG넥스원", "한국항공우주", "현대로템",
        "한화시스템", "비츠로셀", "미사일", "방위", "K-방산",
    ),
    "bio": (
        "바이오", "제약", "셀트리온", "삼성바이오로직스", "유한양행", "종근당",
        "한미약품", "신약", "임상",
    ),
    "battery": (
        "배터리", "2차전지", "양극재", "음극재", "LG에너지", "삼성SDI", "에코프로",
        "포스코퓨처엠", "엘앤에프", "천보",
    ),
    "auto": (
        "자동차", "현대차", "기아", "현대모비스", "만도", "한온시스템", "타이어",
        "자율주행",
    ),
    "telecom": ("통신", "SK텔레콤", "KT", "LG유플러스", "5G"),
    "retail": ("유통", "이마트", "신세계", "롯데쇼핑", "쿠팡", "백화점"),
}


def _repo_data_dir() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def _load_gemini_api_key() -> str:
    key = (
        os.environ.get("GEMINI_API_KEY", "").strip()
        or os.environ.get("GOOGLE_API_KEY", "").strip()
        or os.environ.get("GOOGLE_AI_API_KEY", "").strip()
    )
    if key:
        return key
    path = os.path.join(_repo_data_dir(), "gemini_api_key.txt")
    try:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    return line
    except Exception as e:
        logger.debug("[NEWS] gemini_api_key.txt 읽기 실패: %s", e)
    return ""


GEMINI_API_KEY: str = _load_gemini_api_key()

# ---------------------------------------------------------------------------
# 한국 금융 감성 사전 (긍정/부정 키워드)
# ---------------------------------------------------------------------------
_POSITIVE_KEYWORDS: List[str] = [
    "호실적", "영업이익", "순이익", "흑자", "매출증가", "성장", "최대실적", "어닝서프라이즈",
    "실적개선", "흑자전환", "수익성", "증익", "최대매출", "사상최대", "역대최대",
    "실적호조", "이익증가", "수익증가", "매출성장", "실적상회",
    "수주", "계약체결", "MOU", "협약", "수주잔고", "대형계약", "수출계약",
    "수주성공", "계약수주", "납품계약", "공급계약",
    "상향", "목표주가상향", "매수의견", "투자의견상향", "강력매수", "신고가",
    "급등", "강세", "반등", "돌파", "상승전환", "저평가", "매력적",
    "배당증가", "자사주매입", "주주환원", "특별배당", "배당확대", "자사주소각",
    "신제품출시", "특허취득", "FDA승인", "임상성공", "신약승인", "제품출시",
    "기술이전", "인허가", "CE인증", "양산개시",
    "우량", "안정", "호조", "선전", "양호", "회복", "개선", "턴어라운드",
    "수혜", "기대감", "모멘텀", "상승동력", "실적기대", "전망밝음",
    "구조조정완료", "체질개선", "경쟁력강화", "시장확대", "점유율확대",
    "파트너십", "전략적제휴", "합작", "인수완료", "M&A성공",
]

_NEGATIVE_KEYWORDS: List[str] = [
    "적자", "영업손실", "순손실", "실적쇼크", "매출감소", "어닝쇼크", "적자전환",
    "수익성악화", "감익", "최저실적", "실적부진", "이익감소", "손실확대",
    "실적하회", "가이던스하회", "미달",
    "소송", "과징금", "제재", "압수수색", "검찰", "횡령", "배임", "부정",
    "사기", "분식", "불공정거래", "내부자거래", "고발", "기소", "피의자",
    "벌금", "징계", "조사착수",
    "하향", "목표주가하향", "매도의견", "투자의견하향", "신저가",
    "급락", "약세", "폭락", "붕괴", "하락전환", "추가하락",
    "리콜", "결함", "생산중단", "공장화재", "사망", "부도", "파산", "워크아웃",
    "상장폐지", "관리종목", "불성실공시", "감사의견거절",
    "파업", "노조", "사죄", "사과", "경영위기", "유동성위기",
    "구조조정", "감원", "희망퇴직", "대규모해고",
    "규제강화", "무역분쟁", "관세", "공급과잉", "수요부진",
    "경기침체", "불황", "위기", "불확실성", "리스크", "우려",
    "원가상승", "비용증가", "마진압박", "환율리스크",
]

_HIGH_WEIGHT_POSITIVE = {
    "급등", "신고가", "어닝서프라이즈", "FDA승인", "신약승인", "최대실적",
    "흑자전환", "사상최대", "역대최대", "턴어라운드",
}
_HIGH_WEIGHT_NEGATIVE = {
    "급락", "신저가", "파산", "부도", "상장폐지", "어닝쇼크",
    "횡령", "배임", "검찰", "경영위기", "감사의견거절",
}


@dataclass
class NewsItem:
    text: str
    weight: float
    source: str
    url: str = ""


def _keyword_score_one(text: str) -> float:
    """단일 텍스트 감성 점수 (-1.0 ~ +1.0)."""
    pos = sum(2.0 if kw in _HIGH_WEIGHT_POSITIVE else 1.0 for kw in _POSITIVE_KEYWORDS if kw in text)
    neg = sum(2.0 if kw in _HIGH_WEIGHT_NEGATIVE else 1.0 for kw in _NEGATIVE_KEYWORDS if kw in text)
    total = pos + neg
    if total == 0:
        return 0.0
    return round((pos - neg) / total, 4)


def _keyword_score_weighted(items: List[NewsItem]) -> float:
    """신뢰 매체 가중 평균. 기사가 없으면 0."""
    if not items:
        return 0.0
    num = 0.0
    den = 0.0
    for it in items:
        s = _keyword_score_one(it.text)
        w = max(0.05, float(it.weight))
        num += s * w
        den += w
    if den <= 0:
        return 0.0
    return round(num / den, 4)


def _strip_tags(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"&[a-zA-Z]+;", " ", text)
    text = re.sub(r"&#\d+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _clip_body(text: str, max_chars: int) -> str:
    t = _strip_tags(text)
    if len(t) > max_chars:
        return t[:max_chars].rstrip()
    return t


def _article_text(title: str, body: str = "", max_chars: Optional[int] = None) -> str:
    title = _strip_tags(title)
    body = _clip_body(body, max_chars if max_chars is not None else NEWS_BODY_MAX_CHARS)
    if title and body:
        return f"{title} {body}"
    return title or body


_NAVER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://finance.naver.com/",
    "Accept-Language": "ko-KR,ko;q=0.9",
}


def _http_get(url: str, headers: Optional[Dict[str, str]] = None) -> bytes:
    h = dict(_NAVER_HEADERS)
    if headers:
        h.update(headers)
    req = Request(url, headers=h)
    with urlopen(req, timeout=NEWS_FETCH_TIMEOUT_SEC) as resp:
        return resp.read()


def _decode_html(raw: bytes) -> str:
    for enc in ("utf-8", "euc-kr", "cp949"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("utf-8", errors="replace")


_name_cache: Dict[str, Tuple[str, float]] = {}
_rss_cache: Dict[str, Tuple[List[Tuple[str, str, str]], float]] = {}
_article_body_cache: Dict[str, Tuple[str, float]] = {}


def _stock_name(code: str) -> str:
    """네이버 모바일 시세 JSON에서 종목명. 실패 시 빈 문자열."""
    now = time.time()
    cached = _name_cache.get(code)
    if cached and now - cached[1] < NEWS_CACHE_TTL_SEC:
        return cached[0]
    name = ""
    try:
        raw = _http_get(
            f"https://m.stock.naver.com/api/stock/{code}/basic",
            {"Referer": "https://m.stock.naver.com/", "Accept": "application/json"},
        )
        data = json.loads(raw.decode("utf-8", errors="replace"))
        name = str(data.get("stockName") or "").strip()
    except Exception as e:
        logger.debug("[NEWS] 종목명 조회 실패 %s: %s", code, e)
    _name_cache[code] = (name, now)
    return name


def _is_noise_article(text: str) -> bool:
    if len(text) < 8:
        return True
    noise = ("아이클릭아트", "거래대금", "실시간 검색", "광고", "포토뉴스")
    return any(n in text for n in noise)


def _is_trusted_url(url: str) -> bool:
    u = (url or "").lower()
    return any(
        d in u
        for d in (
            "mk.co.kr",
            "hankyung.com",
            "yna.co.kr",
            "n.news.naver.com",
            "news.naver.com",
            "finance.naver.com",
        )
    )


def _extract_article_body(html: str) -> str:
    """언론사 HTML에서 본문 후보 추출."""
    candidates: List[str] = []
    patterns = [
        r'id="article-body"[^>]*>(.*?)</div>',
        r'id="articleBodyContents"[^>]*>(.*?)</div>',
        r'id="artText"[^>]*>(.*?)</div>',
        r'class="[^"]*article-body[^"]*"[^>]*>(.*?)</div>',
        r'class="[^"]*news_cnt_detail[^"]*"[^>]*>(.*?)</div>',
        r'class="[^"]*article__body[^"]*"[^>]*>(.*?)</div>',
        r'itemprop="articleBody"[^>]*>(.*?)</div>',
        r'<article[^>]*>(.*?)</article>',
    ]
    for pat in patterns:
        for m in re.findall(pat, html, flags=re.S | re.I):
            t = _strip_tags(m)
            if len(t) >= 80:
                candidates.append(t)
    if not candidates:
        # fallback: 긴 문단들
        paras = [_strip_tags(p) for p in re.findall(r"<p[^>]*>(.*?)</p>", html, flags=re.S | re.I)]
        paras = [p for p in paras if len(p) >= 40]
        if paras:
            return " ".join(paras)
        return ""
    return max(candidates, key=len)


def _fetch_full_article_body(url: str) -> str:
    """신뢰 매체 기사 URL에서 본문 텍스트."""
    if not url or not url.startswith("http"):
        return ""
    now = time.time()
    cached = _article_body_cache.get(url)
    if cached and now - cached[1] < NEWS_CACHE_TTL_SEC:
        return cached[0]
    body = ""
    try:
        raw = _http_get(url, {"Referer": url})
        html = _decode_html(raw)
        body = _clip_body(_extract_article_body(html), NEWS_TRUSTED_BODY_MAX_CHARS)
    except Exception as e:
        logger.debug("[NEWS] 본문 풀페치 실패 %s: %s", url[:80], e)
    _article_body_cache[url] = (body, now)
    return body


def _enrich_with_full_body(items: List[NewsItem], budget: List[int]) -> List[NewsItem]:
    """budget[0] 한도 내에서 신뢰 URL 본문을 길게 채움."""
    out: List[NewsItem] = []
    for it in items:
        text = it.text
        if (
            budget[0] > 0
            and it.url
            and _is_trusted_url(it.url)
            and len(_strip_tags(text)) < 600
        ):
            full = _fetch_full_article_body(it.url)
            budget[0] -= 1
            if full:
                # 제목 유지 + 본문 교체
                title = text.split(" ", 1)[0] if " " not in text[:40] else _strip_tags(text)[:80]
                # 원래 텍스트 앞부분(제목)을 최대한 유지
                title_guess = _strip_tags(it.text)[:120]
                text = _article_text(title_guess, full, NEWS_TRUSTED_BODY_MAX_CHARS)
                time.sleep(0.15)
        out.append(NewsItem(text=text, weight=it.weight, source=it.source, url=it.url))
    return out


def _dedupe_items(items: List[NewsItem]) -> List[NewsItem]:
    seen = set()
    out: List[NewsItem] = []
    for it in items:
        if _is_noise_article(it.text):
            continue
        key = re.sub(r"\s+", "", it.text)[:48]
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def _load_rss_items(url: str, referer: str) -> List[Tuple[str, str, str]]:
    """RSS → (title, description, link) TTL 캐시."""
    now = time.time()
    cached = _rss_cache.get(url)
    if cached and now - cached[1] < NEWS_CACHE_TTL_SEC:
        return cached[0]
    parsed: List[Tuple[str, str, str]] = []
    try:
        raw = _http_get(url, {"Referer": referer})
        root = ET.fromstring(raw)
        for item in root.findall(".//item"):
            title = item.findtext("title") or ""
            body = item.findtext("description") or ""
            link = (item.findtext("link") or "").strip()
            parsed.append((_strip_tags(title), _clip_body(body, NEWS_BODY_MAX_CHARS), link))
    except Exception as e:
        logger.debug("[NEWS] RSS 실패 %s: %s", url, e)
        if cached:
            return cached[0]
        return []
    _rss_cache[url] = (parsed, now)
    return parsed


def _filter_rss_by_name(
    name: str,
    rss_url: str,
    referer: str,
    source: str,
    max_articles: int,
) -> List[NewsItem]:
    if not name or len(name) < 2:
        return []
    out: List[NewsItem] = []
    for title, body, link in _load_rss_items(rss_url, referer):
        blob = f"{title} {body}"
        if name not in blob:
            continue
        txt = _article_text(title, body, NEWS_TRUSTED_BODY_MAX_CHARS)
        if txt:
            out.append(
                NewsItem(
                    text=txt,
                    weight=NEWS_TRUSTED_WEIGHT,
                    source=source,
                    url=link,
                )
            )
        if len(out) >= max_articles:
            break
    return out


def _fetch_naver_news_json(code: str, max_articles: int = NEWS_MAX_ARTICLES) -> List[NewsItem]:
    """네이버 증권 API — 제목 + 본문 요약 (+기사 링크)."""
    url = f"https://api.stock.naver.com/news/stock/{code}?pageSize={max_articles}&page=1"
    try:
        raw = _http_get(
            url,
            {"Referer": "https://m.stock.naver.com/", "Accept": "application/json"},
        )
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except Exception as e:
        logger.debug("[NEWS] 네이버 JSON 실패 %s: %s", code, e)
        return []
    items: List[dict] = []
    if isinstance(data, list):
        for group in data:
            if isinstance(group, dict):
                items.extend(group.get("items") or [])
            elif isinstance(group, list):
                items.extend(group)
    elif isinstance(data, dict):
        items.extend(data.get("items") or data.get("news") or [])
    out: List[NewsItem] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        title = str(it.get("titleFull") or it.get("title") or "")
        body = str(it.get("body") or it.get("summary") or it.get("subContent") or "")
        office = str(it.get("officeId") or it.get("oid") or "").strip()
        article = str(it.get("articleId") or it.get("aid") or "").strip()
        link = str(it.get("linkUrl") or it.get("originLink") or it.get("url") or "").strip()
        if not link and office and article:
            link = f"https://n.news.naver.com/mnews/article/{office}/{article}"
        txt = _article_text(title, body, NEWS_TRUSTED_BODY_MAX_CHARS)
        if txt:
            out.append(
                NewsItem(
                    text=txt,
                    weight=NEWS_TRUSTED_WEIGHT,
                    source="naver",
                    url=link,
                )
            )
        if len(out) >= max_articles:
            break
    return out


def _fetch_naver_news_titles(code: str, max_articles: int = NEWS_MAX_ARTICLES) -> List[NewsItem]:
    """네이버 금융 HTML 폴백 — 제목만."""
    url = (
        f"https://finance.naver.com/item/news_news.naver"
        f"?code={code}&page=1&sm=title_entity_id.basic&clusterId="
    )
    try:
        raw = _http_get(url)
        html = _decode_html(raw)
    except Exception as e:
        logger.debug("[NEWS] 네이버 HTML 실패 %s: %s", code, e)
        return []
    html = re.sub(r"&[a-zA-Z]+;", " ", html)
    html = re.sub(r"&#\d+;", " ", html)
    titles = re.findall(r'class="tit"[^>]*>\s*([^<]{4,120})\s*<', html)
    if not titles:
        titles = re.findall(r'<td class="title"[^>]*>.*?<a[^>]+>([^<]{4,120})</a>', html, re.S)
    if not titles:
        titles = re.findall(r'<a[^>]+class="[^"]*tit[^"]*"[^>]*>([^<]{4,120})</a>', html)
    cleaned = [re.sub(r"\s+", " ", t).strip() for t in titles if t.strip()]
    return [
        NewsItem(text=t, weight=NEWS_TRUSTED_WEIGHT * 0.7, source="naver_html")
        for t in cleaned[:max_articles]
    ]


def _fetch_daum_news(name: str, max_articles: int = NEWS_MAX_ARTICLES) -> List[NewsItem]:
    """다음 뉴스 검색 — 제목 + 요약 (보조)."""
    if not name:
        return []
    url = f"https://search.daum.net/search?w=news&q={quote(name)}&sort=recency"
    try:
        html = _decode_html(_http_get(url, {"Referer": "https://search.daum.net/"}))
    except Exception as e:
        logger.debug("[NEWS] 다음 뉴스 실패 %s: %s", name, e)
        return []
    titles = [
        _strip_tags(x)
        for x in re.findall(r'class="tit-g clamp-g"[^>]*>(.*?)</', html, flags=re.S)
    ]
    descs = [
        _strip_tags(x)
        for x in re.findall(r'class="conts-desc[^"]*"[^>]*>(.*?)</', html, flags=re.S)
    ]
    out: List[NewsItem] = []
    for i, title in enumerate(titles):
        if len(title) < 4:
            continue
        body = descs[i] if i < len(descs) else ""
        txt = _article_text(title, body)
        if txt:
            out.append(NewsItem(text=txt, weight=NEWS_SECONDARY_WEIGHT, source="daum"))
        if len(out) >= max_articles:
            break
    return out


def _fetch_google_news(name: str, code: str, max_articles: int = NEWS_MAX_ARTICLES) -> List[NewsItem]:
    """구글 뉴스 RSS — 제목 + 요약 (보조)."""
    q = name or code
    if not q:
        return []
    query = quote(f"{q} 주식")
    url = f"https://news.google.com/rss/search?q={query}&hl=ko&gl=KR&ceid=KR:ko"
    try:
        raw = _http_get(url, {"Referer": "https://news.google.com/"})
        root = ET.fromstring(raw)
    except Exception as e:
        logger.debug("[NEWS] 구글 뉴스 실패 %s: %s", q, e)
        return []
    out: List[NewsItem] = []
    for item in root.findall(".//item"):
        title = item.findtext("title") or ""
        body = item.findtext("description") or ""
        link = (item.findtext("link") or "").strip()
        txt = _article_text(title, body)
        if txt:
            out.append(
                NewsItem(
                    text=txt,
                    weight=NEWS_SECONDARY_WEIGHT,
                    source="google",
                    url=link,
                )
            )
        if len(out) >= max_articles:
            break
    return out


def _fetch_mk_news(name: str, max_articles: int = NEWS_MAX_ARTICLES) -> List[NewsItem]:
    return _filter_rss_by_name(
        name,
        "https://www.mk.co.kr/rss/40300001/",
        "https://www.mk.co.kr/",
        "mk",
        max_articles,
    )


def _fetch_hankyung_news(name: str, max_articles: int = NEWS_MAX_ARTICLES) -> List[NewsItem]:
    return _filter_rss_by_name(
        name,
        "https://www.hankyung.com/feed/finance",
        "https://www.hankyung.com/",
        "hankyung",
        max_articles,
    )


def _fetch_yna_news(name: str, max_articles: int = NEWS_MAX_ARTICLES) -> List[NewsItem]:
    return _filter_rss_by_name(
        name,
        "https://www.yna.co.kr/rss/economy.xml",
        "https://www.yna.co.kr/",
        "yna",
        max_articles,
    )


def _fetch_dart_disclosures(code: str, days: int = 7) -> List[NewsItem]:
    """DART 최근 공시 제목 (공식 공시 — 최고 가중)."""
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
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        if data.get("status") != "000":
            return []
        return [
            NewsItem(
                text=str(item.get("report_nm", "") or ""),
                weight=NEWS_DART_WEIGHT,
                source="dart",
            )
            for item in data.get("list", [])
            if item.get("report_nm")
        ]
    except Exception as e:
        logger.debug("[NEWS] DART 요청 실패 %s: %s", code, e)
        return []


_cache: Dict[str, Tuple[float, float]] = {}
_cache_lock = threading.Lock()
_gemini_score_cache: Dict[str, Tuple[float, float]] = {}
_gemini_lock = threading.Lock()
_gemini_status_logged = False
_gemini_live_log_count = 0

# 무료 키 일일 한도(RPD)는 미 태평양 자정(KST 16~17시)에 리셋 — 종목뉴스가 장전·장중 한도를 다 쓰지 않도록 제한
GEMINI_NEWS_BLOB_TTL_SEC: float = float(os.environ.get("GEMINI_NEWS_BLOB_TTL_SEC", "21600"))
GEMINI_NEWS_DAILY_CALLS: int = int(os.environ.get("GEMINI_NEWS_DAILY_CALLS", "150"))
GEMINI_429_COOLDOWN_SEC: float = float(os.environ.get("GEMINI_429_COOLDOWN_SEC", "1800"))
NEWS_GEMINI_MAX_CODES_PER_CYCLE: int = int(os.environ.get("NEWS_GEMINI_MAX_CODES_PER_CYCLE", "30"))
# 리셋(16시) 직후 장후에 예산을 소진해 다음날 장중 429가 나지 않도록 평일 시간대에만 호출
GEMINI_NEWS_WINDOW: str = os.environ.get("GEMINI_NEWS_WINDOW", "07:30-15:30").strip()
GEMINI_THEME_WINDOW: str = os.environ.get("GEMINI_THEME_WINDOW", "07:00-16:45").strip()
_gemini_quota_lock = threading.Lock()
_gemini_quota_day: str = ""
_gemini_news_calls: int = 0
_gemini_cooldown_until: float = 0.0


def _gemini_quota_day_key() -> str:
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")
    except Exception:
        return (datetime.utcnow() - timedelta(hours=7)).strftime("%Y-%m-%d")


def _in_time_window(spec: str) -> bool:
    """'HH:MM-HH:MM' 로컬 평일 시간대 안이면 True. 빈 값/파싱 실패면 항상 True."""
    if not spec:
        return True
    try:
        a, b = [x.strip() for x in spec.split("-", 1)]
        now = datetime.now()
        if now.weekday() >= 5:
            return False
        cur = now.hour * 60 + now.minute
        ah, am = (int(x) for x in a.split(":"))
        bh, bm = (int(x) for x in b.split(":"))
        return ah * 60 + am <= cur <= bh * 60 + bm
    except Exception:
        return True


def _gemini_in_cooldown() -> bool:
    with _gemini_quota_lock:
        return time.time() < _gemini_cooldown_until


def _gemini_note_429() -> None:
    global _gemini_cooldown_until
    with _gemini_quota_lock:
        first = time.time() >= _gemini_cooldown_until
        _gemini_cooldown_until = time.time() + max(60.0, GEMINI_429_COOLDOWN_SEC)
    if first:
        try:
            append_gemini_daily_log(
                "GEMINI-QUOTA",
                f"429 한도초과 — 뉴스·테마 Gemini {int(GEMINI_429_COOLDOWN_SEC // 60)}분 중지(키워드 대체)",
            )
        except Exception:
            pass


def _gemini_take_news_call() -> bool:
    """종목뉴스 Gemini 호출 1회 예약. 쿨다운·일일 예산 초과면 False."""
    global _gemini_quota_day, _gemini_news_calls
    if not _in_time_window(GEMINI_NEWS_WINDOW):
        return False
    day = _gemini_quota_day_key()
    with _gemini_quota_lock:
        if time.time() < _gemini_cooldown_until:
            return False
        if _gemini_quota_day != day:
            _gemini_quota_day = day
            _gemini_news_calls = 0
        if _gemini_news_calls >= max(0, GEMINI_NEWS_DAILY_CALLS):
            return False
        _gemini_news_calls += 1
        return True


def gemini_quota_status() -> str:
    with _gemini_quota_lock:
        cd = max(0, int(_gemini_cooldown_until - time.time()))
        return (
            f"news_calls={_gemini_news_calls}/{GEMINI_NEWS_DAILY_CALLS} "
            f"cooldown={cd}s day={_gemini_quota_day or '-'} "
            f"창={'ON' if _in_time_window(GEMINI_NEWS_WINDOW) else 'OFF'}({GEMINI_NEWS_WINDOW})"
        )


def _gemini_generate_json(
    prompt: str, *, max_tokens: int = 220, temperature: float = 0.2, kind: str = "other"
) -> Optional[dict]:
    """Gemini generateContent → JSON dict. 실패 시 None. kind='strategy' 는 429 쿨다운 무시."""
    global GEMINI_API_KEY, _gemini_status_logged
    if kind != "strategy" and _gemini_in_cooldown():
        return None
    if kind == "theme" and not _in_time_window(GEMINI_THEME_WINDOW):
        return None
    if not GEMINI_API_KEY:
        GEMINI_API_KEY = _load_gemini_api_key()
    if not GEMINI_API_KEY:
        return None
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{quote(GEMINI_MODEL, safe='')}:generateContent"
        f"?key={quote(GEMINI_API_KEY, safe='')}"
    )
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": float(temperature),
            "maxOutputTokens": int(max_tokens),
            "responseMimeType": "application/json",
        },
    }
    try:
        raw = json.dumps(payload).encode("utf-8")
        req = Request(
            url,
            data=raw,
            headers={"Content-Type": "application/json", "User-Agent": "cursorAI-news/1.0"},
            method="POST",
        )
        with urlopen(req, timeout=max(NEWS_GEMINI_TIMEOUT_SEC, STRATEGY_GEMINI_TIMEOUT_SEC)) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        if isinstance(data.get("error"), dict):
            err = data["error"]
            msg = str(err.get("message") or err)[:180]
            logger.warning("[NEWS] Gemini API error: %s", msg)
            try:
                append_gemini_daily_log("GEMINI-ERR", msg)
            except Exception:
                pass
            return None
        parts = (
            (((data.get("candidates") or [{}])[0].get("content") or {}).get("parts"))
            or []
        )
        text_out = " ".join(str(p.get("text") or "") for p in parts if isinstance(p, dict))
        text_out = (text_out or "").strip()
        if not text_out:
            return None
        try:
            obj = json.loads(text_out)
            return obj if isinstance(obj, dict) else None
        except Exception:
            m = re.search(r"\{[\s\S]*\}", text_out)
            if not m:
                return None
            obj = json.loads(m.group(0))
            return obj if isinstance(obj, dict) else None
    except HTTPError as e:
        err_body = ""
        try:
            err_body = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            pass
        logger.warning("[NEWS] Gemini HTTP %s — %s", e.code, err_body[:180])
        if int(getattr(e, "code", 0) or 0) == 429:
            _gemini_note_429()
            return None
        try:
            append_gemini_daily_log("GEMINI-ERR", f"HTTP {e.code} {err_body[:160]}")
        except Exception:
            pass
        return None
    except Exception as e:
        logger.warning("[NEWS] Gemini JSON 호출 실패: %s", e)
        try:
            append_gemini_daily_log("GEMINI-ERR", f"{type(e).__name__}: {e}")
        except Exception:
            pass
        return None


def gemini_pick_daily_strategy(
    candidates: List[Dict],
    context: Dict,
    *,
    top_reason: str = "",
    regime: str = "",
    evidence: Optional[Dict] = None,
    revert_streaks: Optional[Dict[str, int]] = None,
    excluded: Optional[Dict[str, str]] = None,
) -> Optional[Dict]:
    """
    일일 전략 패키지 후보 중 Gemini 추론으로 1개 고름.
    evidence: shadow_ledger.evidence_summary() — 차단 사유별 '샀다면' 확정 성과.
    반환: {"strategy_id": str, "reason": str} 또는 None(키없음/실패).
    후보 밖 id는 무시 — 규칙 엔진 후보만 선택 가능.
    """
    if not STRATEGY_GEMINI_ENABLED or not candidates:
        return None
    if not _load_gemini_api_key() and not GEMINI_API_KEY:
        return None
    allowed = []
    for s in candidates:
        sid = str((s or {}).get("id") or "").strip()
        if not sid:
            continue
        allowed.append(
            {
                "id": sid,
                "name": str((s or {}).get("name") or ""),
                "intent": str((s or {}).get("intent") or ""),
                "priority": int((s or {}).get("priority") or 0),
                "prefer_reasons": list((s or {}).get("prefer_reasons") or []),
            }
        )
    if not allowed:
        return None
    ids = {a["id"] for a in allowed}
    prompt = (
        "당신은 한국 주식 자동매매의 일일 전략 선택기입니다. "
        "차단 횟수보다 '오답노트'(차단된 신호를 샀다면의 실제 3거래일 성과) 근거를 우선하세요.\n"
        "규칙:\n"
        "- 반드시 후보 id 중 하나만 고르세요\n"
        "- 오답노트 categories[사유].avg 가 음수(n≥30)면 그 차단은 손실을 막아 준 것 → 그 사유 완화 금지\n"
        "- avg ≥ +0.003(n≥30)인 사유만 완화(relax) 근거가 됩니다. 그 사이는 판단 보류\n"
        "- 시장 국면이 bear 이면 완화하지 말고 유지/방어를 고르세요\n"
        "- revert_streaks 가 큰(연속 되돌림) 패키지는 효과가 없었던 것이니 피하세요\n"
        "- GIVE-UP/타임아웃이 많으면 방어(tighten) 선호\n"
        "- 근거가 부족하면 가장 변경이 작은 후보를 고르세요\n"
        "- JSON만: {\"strategy_id\":\"...\",\"reason\":\"한줄(근거 수치 포함)\"}\n\n"
        f"시장 국면: {regime or '-'}\n"
        f"오늘 컨텍스트: {json.dumps(context, ensure_ascii=False)}\n"
        f"상세차단 top_reason: {top_reason or '-'}\n"
        f"오답노트: {json.dumps(evidence or {}, ensure_ascii=False)}\n"
        f"revert_streaks: {json.dumps(revert_streaks or {}, ensure_ascii=False)}\n"
        f"규칙엔진이 제외한 패키지: {json.dumps(excluded or {}, ensure_ascii=False)}\n"
        f"후보: {json.dumps(allowed, ensure_ascii=False)}"
    )
    obj = _gemini_generate_json(prompt, max_tokens=180, temperature=0.15, kind="strategy")
    if not obj:
        return None
    sid = str(obj.get("strategy_id") or obj.get("id") or "").strip()
    if sid not in ids:
        return None
    reason = str(obj.get("reason") or "").strip()[:120]
    return {"strategy_id": sid, "reason": reason or "gemini"}


def gemini_status_line() -> str:
    """부팅/로그용 Gemini 사용 가능 여부."""
    if not NEWS_GEMINI_ENABLED:
        return "Gemini=off"
    if not GEMINI_API_KEY:
        return "Gemini=키없음(키워드만)"
    strat = "추론전략ON" if STRATEGY_GEMINI_ENABLED else "추론전략off"
    theme = "테마ON" if THEME_STRENGTH_ENABLED else "테마off"
    return (
        f"Gemini=ON({GEMINI_MODEL},{strat},{theme},blend={NEWS_GEMINI_BLEND:.2f})"
    )


def _parse_gemini_score(raw_text: str) -> Optional[float]:
    score, _ = _parse_gemini_news_payload(raw_text)
    return score


def _parse_gemini_news_payload(raw_text: str) -> Tuple[Optional[float], str]:
    """Gemini 뉴스 JSON → (score, summary)."""
    if not raw_text:
        return None, ""
    text = raw_text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.S | re.I)
    if fence:
        text = fence.group(1).strip()
    summary = ""
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            summary = str(obj.get("summary") or obj.get("reason") or "").strip()[:160]
            if "score" in obj:
                return max(-1.0, min(1.0, float(obj["score"]))), summary
    except Exception:
        pass
    m = re.search(r'"score"\s*:\s*(-?\d+(?:\.\d+)?)', text)
    if not summary:
        ms = re.search(r'"summary"\s*:\s*"([^"]*)"', text)
        if ms:
            summary = ms.group(1).strip()[:160]
    if m:
        return max(-1.0, min(1.0, float(m.group(1)))), summary
    m2 = re.search(r"(-?\d+(?:\.\d+)?)", text)
    if m2:
        v = float(m2.group(1))
        if -1.0 <= v <= 1.0:
            return v, summary
    return None, summary


def _repo_log_dir() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "log")


_gemini_daily_lock = threading.Lock()
_gemini_daily_date: str = ""
_gemini_news_logged_codes: set = set()
_GEMINI_NEWS_DAILY_MAX = int(os.environ.get("GEMINI_NEWS_DAILY_MAX", "40"))


def append_gemini_daily_log(section: str, message: str) -> str:
    """
    Gemini 요약/사유를 log/YYYY-MM-DD_gemini_summary.txt 에 append.
    반환: 기록한 파일 경로(실패 시 빈 문자열).
    """
    global _gemini_daily_date, _gemini_news_logged_codes
    msg = (message or "").strip()
    if not msg:
        return ""
    now = datetime.now()
    day = now.strftime("%Y-%m-%d")
    path = os.path.join(_repo_log_dir(), f"{day}_gemini_summary.txt")
    try:
        os.makedirs(_repo_log_dir(), exist_ok=True)
        with _gemini_daily_lock:
            if _gemini_daily_date != day:
                _gemini_daily_date = day
                _gemini_news_logged_codes = set()
            need_header = (not os.path.isfile(path)) or os.path.getsize(path) == 0
            if need_header:
                sep = "=" * 64
                header = (
                    f"{sep}\n"
                    f"  [GEMINI-SUMMARY] {day}\n"
                    f"  종목뉴스·테마·일일전략 Gemini 한줄 요약\n"
                    f"{sep}\n"
                )
                with open(path, "w", encoding="utf-8") as f:
                    f.write(header)
            line = f"[{now.strftime('%H:%M:%S')}][{section}] {msg}\n"
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
        return path
    except Exception as e:
        logger.debug("[NEWS] gemini 일별로그 기록 실패: %s", e)
        return ""


def _gemini_score_for_stock(name: str, code: str, trusted: List[NewsItem]) -> Optional[float]:
    """
    신뢰 기사 본문을 Gemini에 보내 종목 단위 감성 점수 1회 산출.
    실패/키없음이면 None → 키워드 점수 사용.
    """
    global _gemini_status_logged, GEMINI_API_KEY
    if not NEWS_GEMINI_ENABLED:
        return None
    if not GEMINI_API_KEY:
        GEMINI_API_KEY = _load_gemini_api_key()
    if not GEMINI_API_KEY:
        if not _gemini_status_logged:
            logger.info("[NEWS] Gemini 키 없음 — 키워드 감성만 사용 (data/gemini_api_key.txt 또는 GEMINI_API_KEY)")
            _gemini_status_logged = True
        return None

    # 본문이 긴 신뢰 기사 우선
    cands = [
        it
        for it in trusted
        if it.source in ("naver", "mk", "hankyung", "yna", "dart", "naver_html")
        and len(it.text) >= NEWS_GEMINI_MIN_CHARS
    ]
    cands.sort(key=lambda x: len(x.text), reverse=True)
    cands = cands[: max(1, NEWS_GEMINI_MAX_ARTICLES)]
    if not cands:
        return None

    blob_key = hashlib.sha1(
        ("|".join(it.text[:800] for it in cands) + f"|{code}").encode("utf-8", errors="replace")
    ).hexdigest()
    now = time.time()
    with _gemini_lock:
        cached = _gemini_score_cache.get(blob_key)
        if cached and now - cached[1] < max(NEWS_CACHE_TTL_SEC, GEMINI_NEWS_BLOB_TTL_SEC):
            return cached[0]
    if not _gemini_take_news_call():
        return None

    articles_txt = []
    for i, it in enumerate(cands, 1):
        body = it.text[:2200]
        articles_txt.append(f"[{i}|{it.source}] {body}")
    joined = "\n\n".join(articles_txt)
    stock_label = f"{name}({code})" if name else code
    prompt = (
        "당신은 한국 주식 뉴스 분석가입니다. 아래 기사만 근거로 해당 종목의 "
        "단기(수일) 주가 관점 뉴스 감성을 판단하세요.\n"
        f"종목: {stock_label}\n"
        "규칙:\n"
        "- score는 -1.0(매우 악재) ~ +1.0(매우 호재), 중립은 0.0\n"
        "- 단순 시세/거래대금 나열은 거의 0.0\n"
        "- 반어·복합 문장(예: 적자에서 흑자전환)은 전체 문맥으로 판단\n"
        "- JSON만 출력: {\"score\": 0.0, \"summary\": \"한줄요약\"}\n\n"
        f"기사:\n{joined}"
    )

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{quote(GEMINI_MODEL, safe='')}:generateContent"
        f"?key={quote(GEMINI_API_KEY, safe='')}"
    )
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 160,
            "responseMimeType": "application/json",
        },
    }
    try:
        raw = json.dumps(payload).encode("utf-8")
        req = Request(
            url,
            data=raw,
            headers={"Content-Type": "application/json", "User-Agent": "cursorAI-news/1.0"},
            method="POST",
        )
        with urlopen(req, timeout=NEWS_GEMINI_TIMEOUT_SEC) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        parts = (
            (((data.get("candidates") or [{}])[0].get("content") or {}).get("parts"))
            or []
        )
        text_out = " ".join(str(p.get("text") or "") for p in parts if isinstance(p, dict))
        score, summary = _parse_gemini_news_payload(text_out)
        if score is None:
            logger.debug("[NEWS] Gemini 응답 파싱 실패 %s: %s", code, text_out[:200])
            return None
        with _gemini_lock:
            _gemini_score_cache[blob_key] = (score, now)
        if not _gemini_status_logged:
            logger.info("[NEWS] Gemini 문맥판정 ON model=%s", GEMINI_MODEL)
            _gemini_status_logged = True
        # 일별 요약 로그 (종목당 1회, 하루 상한)
        code_s = str(code or "").strip()
        with _gemini_daily_lock:
            under_cap = len(_gemini_news_logged_codes) < max(1, _GEMINI_NEWS_DAILY_MAX)
            already = code_s in _gemini_news_logged_codes
        if under_cap and not already and code_s:
            with _gemini_daily_lock:
                _gemini_news_logged_codes.add(code_s)
            sum_txt = summary or "(요약없음)"
            append_gemini_daily_log(
                "NEWS",
                f"{code_s} {name or '-'} score={score:+.2f} | {sum_txt}",
            )
        return score
    except HTTPError as e:
        err_body = ""
        try:
            err_body = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            pass
        logger.debug("[NEWS] Gemini HTTPError %s: %s %s", code, e, err_body)
        if int(getattr(e, "code", 0) or 0) == 429:
            _gemini_note_429()
        return None
    except Exception as e:
        logger.debug("[NEWS] Gemini 호출 실패 %s: %s", code, e)
        return None


def get_news_sentiment(code: str, force: bool = False, use_gemini: bool = True) -> float:
    """
    종목코드에 대한 감성 점수를 반환 (-1.0 ~ +1.0).
    신뢰 매체 본문 + (키 있으면) Gemini 문맥 판정. 캐시 TTL 이내면 재사용.
    """
    now = time.time()
    with _cache_lock:
        if not force and code in _cache:
            score, fetched_at = _cache[code]
            if now - fetched_at < NEWS_CACHE_TTL_SEC:
                return score

    name = _stock_name(code)

    naver = _fetch_naver_news_json(code)
    if not naver:
        naver = _fetch_naver_news_titles(code)
    mk = _fetch_mk_news(name)
    hk = _fetch_hankyung_news(name)
    yna = _fetch_yna_news(name)
    dart = _fetch_dart_disclosures(code)

    trusted = naver + mk + hk + yna + dart
    budget = [max(0, int(NEWS_FULL_FETCH_MAX))]
    trusted = _enrich_with_full_body(trusted, budget)

    secondary = _fetch_daum_news(name) + _fetch_google_news(name, code)
    items = _dedupe_items(trusted + secondary)
    kw_score = _keyword_score_weighted(items)

    llm_score = _gemini_score_for_stock(name, code, trusted) if use_gemini else None
    if llm_score is None:
        score = kw_score
        mode = "kw"
    else:
        blend = max(0.0, min(1.0, NEWS_GEMINI_BLEND))
        score = round(blend * float(llm_score) + (1.0 - blend) * float(kw_score), 4)
        mode = f"gemini+kw({blend:.2f})"

    with _cache_lock:
        _cache[code] = (score, now)

    by_src: Dict[str, int] = {}
    for it in items:
        by_src[it.source] = by_src.get(it.source, 0) + 1
    global _gemini_live_log_count
    if mode.startswith("gemini") and _gemini_live_log_count < 2:
        _gemini_live_log_count += 1
        logger.info(
            "[NEWS] Gemini혼합 %s kw=%+.2f llm=%+.2f → %.2f",
            code,
            kw_score,
            float(llm_score),
            score,
        )
    logger.debug(
        "[NEWS] %s(%s) items=%d mode=%s kw=%+.3f llm=%s %s → score=%.4f",
        code,
        name or "-",
        len(items),
        mode,
        kw_score,
        f"{llm_score:+.3f}" if llm_score is not None else "-",
        " ".join(f"{k}={v}" for k, v in sorted(by_src.items())),
        score,
    )
    return score


def prefetch_news_batch(codes: List[str]) -> Dict[str, float]:
    """
    여러 종목 뉴스를 순차적으로 미리 가져옴.
    on_tick 외부 스레드에서 호출 권장 (네트워크 I/O 블로킹).
    앞쪽 NEWS_GEMINI_MAX_CODES_PER_CYCLE 종목만 Gemini 판정(호출자가 보유·우선 종목을 앞에 둘 것).
    """
    results: Dict[str, float] = {}
    # Gemini 호출 시 무료 한도·차단 완화
    gap = 0.8 if (NEWS_GEMINI_ENABLED and _load_gemini_api_key()) else 0.35
    cap = max(0, NEWS_GEMINI_MAX_CODES_PER_CYCLE)
    for i, code in enumerate(codes):
        try:
            results[code] = get_news_sentiment(code, use_gemini=i < cap)
            time.sleep(gap)
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


# ---------------------------------------------------------------------------
# 테마 강세 추론 (반도체·조선·원자력·금융·방산 등)
# ---------------------------------------------------------------------------
_theme_scores: Dict[str, float] = {t: 0.0 for t in THEME_IDS}
_theme_reason: str = ""
_theme_fetched_at: float = 0.0
_theme_lock = threading.Lock()


def _fetch_rss_top_headlines(
    rss_url: str, referer: str, source: str, max_articles: int = 12
) -> List[NewsItem]:
    """이름 필터 없이 RSS 상위 헤드라인."""
    out: List[NewsItem] = []
    try:
        for title, body, link in _load_rss_items(rss_url, referer):
            txt = _article_text(title, body, 400)
            if not txt or _is_noise_article(txt):
                continue
            out.append(
                NewsItem(text=txt, weight=NEWS_TRUSTED_WEIGHT, source=source, url=link)
            )
            if len(out) >= max_articles:
                break
    except Exception as e:
        logger.debug("[THEME] RSS 실패 %s: %s", source, e)
    return out


def _collect_theme_headlines() -> List[str]:
    items: List[NewsItem] = []
    items += _fetch_rss_top_headlines(
        "https://www.mk.co.kr/rss/40300001/", "https://www.mk.co.kr/", "mk", 10
    )
    items += _fetch_rss_top_headlines(
        "https://www.hankyung.com/feed/finance", "https://www.hankyung.com/", "hankyung", 10
    )
    items += _fetch_rss_top_headlines(
        "https://www.yna.co.kr/rss/economy.xml", "https://www.yna.co.kr/", "yna", 10
    )
    # 중복 제목 제거
    seen = set()
    lines: List[str] = []
    for it in items:
        key = it.text[:80]
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"[{it.source}] {it.text[:220]}")
        if len(lines) >= 28:
            break
    return lines


def _keyword_theme_scores(headlines: List[str]) -> Dict[str, float]:
    """헤드라인 키워드 히트로 테마 점수 근사 (Gemini 실패 시)."""
    blob = "\n".join(headlines)
    out: Dict[str, float] = {}
    for tid, kws in THEME_NAME_KEYWORDS.items():
        hits = sum(1 for kw in kws if kw and kw in blob)
        # 긍정/부정 키워드와 함께 근처 문장 감성
        pos = sum(1 for kw in _POSITIVE_KEYWORDS if kw in blob and any(t in blob for t in kws[:6]))
        neg = sum(1 for kw in _NEGATIVE_KEYWORDS if kw in blob and any(t in blob for t in kws[:6]))
        raw = 0.0
        if hits > 0:
            raw = min(0.85, 0.12 * hits)
            if pos + neg > 0:
                raw = raw * (0.55 + 0.45 * ((pos - neg) / max(1, pos + neg)))
            # 단순 언급만 많으면 중립에 가깝게
            if pos + neg == 0:
                raw *= 0.35
        out[tid] = round(max(-1.0, min(1.0, raw)), 3)
    return out


def themes_for_stock(code: str, name: str = "") -> List[str]:
    """종목코드/명으로 소속 테마 목록."""
    nm = (name or "").strip() or _stock_name(code)
    blob = f"{nm} {code}"
    found: List[str] = []
    for tid, kws in THEME_NAME_KEYWORDS.items():
        if any(kw and kw in blob for kw in kws):
            found.append(tid)
    return found


def refresh_theme_strength(force: bool = False) -> Dict[str, float]:
    """
    시장 헤드라인 + Gemini로 테마 강세 점수 갱신.
    반환: theme_id → -1.0~+1.0
    """
    global _theme_scores, _theme_reason, _theme_fetched_at
    if not THEME_STRENGTH_ENABLED:
        return dict(_theme_scores)
    now = time.time()
    with _theme_lock:
        if (
            not force
            and _theme_fetched_at > 0
            and now - _theme_fetched_at < THEME_STRENGTH_TTL_SEC
        ):
            return dict(_theme_scores)

    headlines = _collect_theme_headlines()
    kw_scores = _keyword_theme_scores(headlines)
    reason = "keyword"
    scores = dict(kw_scores)
    gemini_ok = False

    if not headlines:
        reason = "keyword(헤드라인없음)"
    elif _load_gemini_api_key() or GEMINI_API_KEY:
        labels = {tid: THEME_LABELS.get(tid, tid) for tid in THEME_IDS}
        prompt = (
            "당신은 한국 주식 시장 테마 분석가입니다. 아래 최근 경제·증권 헤드라인만 근거로 "
            "테마별 단기(수일) 강세/약세를 추론하세요.\n"
            "규칙:\n"
            "- 각 테마 score는 -1.0(매우약세) ~ +1.0(매우강세), 근거 부족은 0.0\n"
            "- 단순 언급만 있고 호재/악재가 불명확하면 0에 가깝게\n"
            "- JSON만: {\"themes\":{\"semiconductor\":0.0,...},\"top\":[\"id1\",\"id2\"],\"reason\":\"한줄\"}\n"
            f"- 테마 id와 한글: {json.dumps(labels, ensure_ascii=False)}\n\n"
            "헤드라인:\n" + "\n".join(headlines[:28])
        )
        obj = _gemini_generate_json(prompt, max_tokens=400, temperature=0.15, kind="theme")
        if obj and isinstance(obj.get("themes"), dict):
            merged: Dict[str, float] = {}
            for tid in THEME_IDS:
                try:
                    llm_v = float(obj["themes"].get(tid, kw_scores.get(tid, 0.0)))
                except (TypeError, ValueError):
                    llm_v = float(kw_scores.get(tid, 0.0))
                kw_v = float(kw_scores.get(tid, 0.0))
                # 추론 80% + 키워드 20%
                merged[tid] = round(max(-1.0, min(1.0, 0.80 * llm_v + 0.20 * kw_v)), 3)
            scores = merged
            reason = str(obj.get("reason") or "gemini")[:120]
            top = obj.get("top") or []
            if isinstance(top, list) and top:
                reason = f"{reason} | top={','.join(str(x) for x in top[:3])}"
            gemini_ok = True
        else:
            reason = "keyword(Gemini테마실패)"
    else:
        reason = "keyword(키없음)"

    with _theme_lock:
        _theme_scores = {t: float(scores.get(t, 0.0)) for t in THEME_IDS}
        _theme_reason = reason
        _theme_fetched_at = time.time()
        out = dict(_theme_scores)

    ranked = sorted(out.items(), key=lambda x: x[1], reverse=True)
    top_s = ", ".join(
        f"{THEME_LABELS.get(k, k)}:{v:+.2f}" for k, v in ranked[:5]
    )
    mode = "gemini" if gemini_ok else "keyword"
    logger.info("[THEME] 갱신 mode=%s | %s | %s", mode, top_s, reason)
    append_gemini_daily_log(
        "THEME",
        f"mode={mode} | {top_s} | {reason}",
    )
    return out


def get_theme_strength_map() -> Dict[str, float]:
    with _theme_lock:
        if _theme_fetched_at <= 0:
            return {t: 0.0 for t in THEME_IDS}
        return dict(_theme_scores)


def get_theme_boost_for_code(code: str, name: str = "") -> float:
    """
    매수 스코어 가산값. 강세 테마 소속이면 +, 약세면 -.
    테마 미매칭이면 0.
    """
    if not THEME_STRENGTH_ENABLED:
        return 0.0
    themes = themes_for_stock(code, name)
    if not themes:
        return 0.0
    scores = get_theme_strength_map()
    best = max(float(scores.get(t, 0.0)) for t in themes)
    worst = min(float(scores.get(t, 0.0)) for t in themes)
    # 강세 우선 가산, 뚜렷한 약세면 감점
    if best >= float(THEME_STRONG_MIN):
        return round(min(0.50, best * float(THEME_BUY_SCORE_WEIGHT)), 4)
    if worst <= float(THEME_WEAK_MAX):
        return round(max(-0.30, worst * 0.45), 4)
    return round(best * 0.20, 4)


def theme_strength_summary() -> str:
    with _theme_lock:
        if _theme_fetched_at <= 0:
            return "테마미갱신"
        age = int((time.time() - _theme_fetched_at) / 60)
        ranked = sorted(_theme_scores.items(), key=lambda x: x[1], reverse=True)
        parts = [f"{THEME_LABELS.get(k, k)}:{v:+.2f}" for k, v in ranked[:6]]
        return f"{' | '.join(parts)} ({age}분전, {_theme_reason[:40]})"


def is_theme_strong_for_code(code: str, name: str = "") -> bool:
    themes = themes_for_stock(code, name)
    if not themes:
        return False
    scores = get_theme_strength_map()
    return max(float(scores.get(t, 0.0)) for t in themes) >= float(THEME_STRONG_MIN)
