"""
reasoning_report.py — 하루 1회 '추론 요약' 리포트 (log/YYYY-MM-DD_reasoning.txt)

봇이 스스로 내린 판단(시장 국면 · AI 매수확률 · Gemini · 일일 전략 학습 · 오답노트)이
실제로 맞았는지 숫자로 요약한다. 매매 로직에는 관여하지 않는다(읽기·기록 전용).

단독 실행: python reasoning_report.py --date YYYY-MM-DD
"""
from __future__ import annotations

import argparse
import contextlib
import csv
import datetime as _dt
import io
import os
import re
from typing import Dict, List, Optional, Tuple

_ROOT = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(_ROOT, "log")
DATA_DIR = os.path.join(_ROOT, "data")
REGIME_INDEX_TICKER = "069500.KS"
REGIME_VERIFY_DAYS = 10
EVIDENCE_MIN_N = 30
EVIDENCE_MIN_EDGE = 0.003


def _read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception:
        return ""


def _read_csv(path: str) -> List[Dict[str, str]]:
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f))
    except Exception:
        return []


def _fnum(v) -> Optional[float]:
    try:
        return float(v) if v not in (None, "") else None
    except Exception:
        return None


def _hhmm(line: str) -> str:
    m = re.match(r"\[(\d\d:\d\d):\d\d\]", line)
    return m.group(1) if m else ""


# ── 1. 시장 국면 ─────────────────────────────────────────────────────────
def _regime_labels_by_day() -> Dict[str, str]:
    out: Dict[str, str] = {}
    try:
        names = sorted(os.listdir(LOG_DIR))
    except Exception:
        return out
    for n in names:
        m = re.match(r"^(\d{4}-\d{2}-\d{2})_analysis\.txt$", n)
        if not m:
            continue
        lab = re.search(r"시장 레짐:\s*(\w+)", _read(os.path.join(LOG_DIR, n)))
        if lab:
            out[m.group(1)] = lab.group(1).lower()
    return out


def _index_daily_changes() -> Dict[str, float]:
    try:
        import yfinance as yf  # type: ignore
        sink = io.StringIO()
        with contextlib.redirect_stderr(sink), contextlib.redirect_stdout(sink):
            df = yf.Ticker(REGIME_INDEX_TICKER).history(period="3mo", interval="1d", auto_adjust=False)
        if df is None or df.empty:
            return {}
        closes = [(idx.date().isoformat(), float(c)) for idx, c in df["Close"].items()]
    except Exception:
        return {}
    out: Dict[str, float] = {}
    for (_d0, c0), (d1, c1) in zip(closes, closes[1:]):
        out[d1] = c1 / c0 - 1.0
    return out


def _regime_agrees(label: str, chg: float) -> bool:
    if label == "bull":
        return chg > 0
    if label == "bear":
        return chg < 0
    return abs(chg) < 0.01


def _regime_section(day: str, day_log: List[str]) -> Tuple[List[str], str]:
    lines = ["■ 1. 시장 국면 추론"]
    changes = [l for l in day_log if "mom=" in l and "변경" in l]
    labels = _regime_labels_by_day()
    today_label = labels.get(day, "-")
    lines.append(f"  장 마감 판정: {today_label}  | 오늘 판정 변경 {len(changes)}회")
    for l in changes[:5]:
        m = re.search(r"(mom=[^ ]+).*\((변경:[^)]+)\)", l)
        lines.append(f"    {_hhmm(l)} {m.group(2) + ' ' + m.group(1) if m else l[:120]}")
    halts = [l for l in day_log if "[HALT]" in l and ("거래정지 의심" in l or "재개" in l or "체결 없음" in l) and "주문 보류" not in l]
    if halts:
        lines.append(f"  시장 거래정지 감지 {len(halts)}건:")
        lines += [f"    {_hhmm(l)} {l.split('[HALT]', 1)[-1].strip()[:130]}" for l in halts[:6]]
    events = [
        l for l in day_log
        if any(t in l for t in ("[VI]", "[FEED]", "[STOCK-STATE]", "[CALENDAR]")) and "주문 보류" not in l
    ]
    if events:
        lines.append(f"  시장/종목 이벤트 {len(events)}건:")
        lines += [f"    {_hhmm(l)} {l.split('] ', 1)[-1].strip()[:130]}" for l in events[:8]]
    chg = _index_daily_changes()
    rows = [(d, labels[d], chg[d]) for d in sorted(chg) if d in labels and d <= day][-REGIME_VERIFY_DAYS:]
    same_hit = sum(1 for d, lab, c in rows if _regime_agrees(lab, c))
    next_pairs = []
    days_sorted = sorted(chg)
    for d, lab, _c in rows:
        nxt = [x for x in days_sorted if x > d]
        if nxt:
            next_pairs.append((d, lab, chg[nxt[0]]))
    next_hit = sum(1 for d, lab, c in next_pairs if _regime_agrees(lab, c))
    if rows:
        lines.append(f"  검증(KODEX200, 최근 {len(rows)}거래일):")
        lines.append(
            f"    당일 지수 방향과 일치 {same_hit}/{len(rows)}"
            + (f" | 다음날 지수 방향 예측 {next_hit}/{len(next_pairs)}" if next_pairs else "")
        )
        lines.append("    " + "  ".join(f"{d[5:]}:{lab}({c*100:+.1f}%)" for d, lab, c in rows[-6:]))
    else:
        lines.append("  검증: 지수 데이터 없음")
    head = f"국면={today_label}" + (f"(일치 {same_hit}/{len(rows)})" if rows else "")
    return lines, head


# ── 2. AI 매수확률 ───────────────────────────────────────────────────────
def _ai_section(day: str, day_log: List[str]) -> Tuple[List[str], str]:
    lines = ["■ 2. AI 매수확률 모델"]
    model = ""
    for l in day_log:
        m = re.search(r"AI:(로드|학습) (\w+ n=\d+ [\d-]+ auc=[\d.]+ ece=[\d.]+)", l)
        if m:
            model = f"{m.group(1)} {m.group(2)}"
    events = [l for l in day_log if "[AI] 학습 완료" in l or "새 모델 교체 거부" in l]
    lines.append(f"  모델: {model or '-'}")
    for l in events[-2:]:
        lines.append(f"    {_hhmm(l)} {l.split('[AI]', 1)[-1].strip()[:140]}")
    sig = [r for r in _read_csv(os.path.join(DATA_DIR, "shadow_signals.csv")) if r.get("date") == day]
    probs = sorted(p for p in (_fnum(r.get("ai_proba")) for r in sig) if p is not None)
    if probs:
        over = sum(1 for p in probs if p >= 0.50)
        lines.append(
            f"  오늘 확률 분포(차단 후보 {len(probs)}건): 최소 {probs[0]:.2f} · 중앙 {probs[len(probs)//2]:.2f} "
            f"· 최대 {probs[-1]:.2f} | 0.50 이상 {over}건"
        )
    else:
        lines.append("  오늘 확률 분포: 기록 없음")
    outs = [
        r for r in _read_csv(os.path.join(DATA_DIR, "shadow_outcomes.csv"))
        if "AI" in r.get("reason_key", "") and _fnum(r.get("ai_proba")) is not None and _fnum(r.get("net_ret")) is not None
    ]
    verdict = "판단 보류"
    if len(outs) >= 20:
        outs.sort(key=lambda r: float(r["ai_proba"]))
        half = len(outs) // 2
        lo = sum(float(r["net_ret"]) for r in outs[:half]) / half
        hi = sum(float(r["net_ret"]) for r in outs[half:]) / (len(outs) - half)
        verdict = "변별력 있음" if hi - lo >= 0.003 else "변별력 없음"
        lines.append(
            f"  변별력 검증(오답노트 확정 {len(outs)}건): 확률 하위절반 평균 {lo*100:+.2f}% vs "
            f"상위절반 {hi*100:+.2f}% → {verdict}"
        )
    else:
        lines.append(f"  변별력 검증: 확정 표본 {len(outs)}건(<20) — 판단 보류")
    head = (
        f"AI {probs[0]:.2f}~{probs[-1]:.2f}·0.50↑{sum(1 for p in probs if p >= 0.5)}건·{verdict}"
        if probs else f"AI {verdict}"
    )
    return lines, head


# ── 3. Gemini ────────────────────────────────────────────────────────────
def _gemini_section(day_log: List[str], strategy_txt: str) -> Tuple[List[str], str]:
    lines = ["■ 3. Gemini 추론"]
    quota = ""
    for l in day_log:
        m = re.search(r"news_calls=(\d+)/(\d+)", l)
        if m:
            quota = f"{m.group(1)}/{m.group(2)}"
    n429 = sum(1 for l in day_log if "[GEMINI-QUOTA]" in l or "HTTP 429" in l)
    theme = [l for l in day_log if "[THEME]" in l and "09:00" <= _hhmm(l) <= "15:30"]
    theme_kw = sum(1 for l in theme if "keyword(" in l)
    lines.append(f"  뉴스 감성 호출: {quota or '-'} | 429·쿼터 경고 {n429}회")
    if theme:
        lines.append(f"  장중 테마 추론: Gemini {len(theme) - theme_kw}회 · 키워드 대체 {theme_kw}회")
    pick = re.search(r"Gemini추론:\s*(.+)", strategy_txt)
    lines.append(f"  일일 전략 추론: {pick.group(1).strip()[:150] if pick else '- (21:30 이전)'}")
    head = f"Gemini 뉴스 {quota or '-'}·429 {n429}"
    return lines, head


# ── 4. 일일 전략 학습 ────────────────────────────────────────────────────
def _strategy_section(strategy_txt: str) -> Tuple[List[str], str]:
    lines = ["■ 4. 일일 전략 학습"]
    if not strategy_txt:
        return lines + ["  아직 실행 전 (21:30)"], "전략 -"
    sel = re.search(r"선택된 전략:\s*\[([^\]]+)\]\s*(.+)", strategy_txt)
    lines.append(f"  선택: {('[' + sel.group(1) + '] ' + sel.group(2).strip()) if sel else '-'}")
    block = re.search(r"■ 학습 근거\n(.*?)(?:\n\s*\n|\n■)", strategy_txt, flags=re.S)
    excluded = 0
    if block:
        for l in block.group(1).splitlines():
            if l.strip().startswith("["):
                excluded += 1
            lines.append("  " + l.rstrip())
    changes = re.search(r"■ 파라미터 변경\n(.*?)(?:\n\s*\n|\n■)", strategy_txt, flags=re.S)
    if changes:
        lines.append("  파라미터 변경:")
        lines += ["  " + l.rstrip() for l in changes.group(1).splitlines()]
    name = sel.group(2).strip() if sel else "-"
    head = f"전략={name}" + (f"(학습제외 {excluded})" if excluded else "")
    return lines, head


# ── 5. 오답노트 ──────────────────────────────────────────────────────────
def _shadow_section() -> Tuple[List[str], str]:
    lines = ["■ 5. 오답노트 학습 진행"]
    try:
        import shadow_ledger as sl
        ev = sl.evidence_summary(30)
    except Exception:
        return lines + ["  요약 실패"], "오답노트 -"
    cats = ev.get("categories") or {}
    n_total = int(ev.get("n_total", 0) or 0)
    lines.append(f"  확정 {n_total}건 (최근 30일) — 완화 근거 기준: n≥{EVIDENCE_MIN_N} · 평균≥+{EVIDENCE_MIN_EDGE*100:.1f}%")
    pos = 0
    for cat in ("vol", "ma", "rsi", "ai"):
        e = cats.get(cat) or {}
        n, avg = int(e.get("n", 0) or 0), float(e.get("avg", 0.0) or 0.0)
        if n >= EVIDENCE_MIN_N and avg >= EVIDENCE_MIN_EDGE:
            state = "완화 근거"
            pos += 1
        elif n >= EVIDENCE_MIN_N and avg < 0:
            state = f"차단 유지 (기준까지 {(EVIDENCE_MIN_EDGE - avg)*100:.2f}%p)"
        else:
            state = "판단 보류"
        lines.append(f"    {cat:<4} n={n:>4} 승률 {float(e.get('win', 0))*100:>3.0f}% 평균 {avg*100:+.2f}% → {state}")
    head = f"오답노트 {n_total}건·완화근거 {pos}개"
    return lines, head


def build(day: Optional[str] = None) -> str:
    """리포트 작성 후 경로 반환(실패 시 빈 문자열)."""
    day = day or _dt.date.today().isoformat()
    day_log = _read(os.path.join(LOG_DIR, f"{day}.txt")).splitlines()
    strategy_txt = _read(os.path.join(LOG_DIR, f"{day}_strategy.txt"))
    sections = [
        _regime_section(day, day_log),
        _ai_section(day, day_log),
        _gemini_section(day_log, strategy_txt),
        _strategy_section(strategy_txt),
        _shadow_section(),
    ]
    headline = " | ".join(h for _, h in sections)
    sep = "=" * 64
    lines = [
        sep,
        f"  [REASONING] {day} 추론 요약",
        f"  작성: {_dt.datetime.now().strftime('%H:%M:%S')}",
        sep,
        f"한줄: {headline}",
        "",
    ]
    for body, _ in sections:
        lines += body + [""]
    lines += [sep, ""]
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        path = os.path.join(LOG_DIR, f"{day}_reasoning.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        return path
    except Exception:
        return ""


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    print(build(ap.parse_args().date))
