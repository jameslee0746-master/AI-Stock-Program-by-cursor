"""
shadow_ledger.py — 놓친 매수 신호 기록(오답노트) + 사후 채점

- record(): 정규장 중 '전략조건 미충족'으로 차단된 후보를 하루 (종목, 사유) 1회 기록
- score_and_report(): 기록 후 horizon 거래일이 지난 항목을 일봉으로 채점해
  data/shadow_outcomes.csv 에 저장하고 log/YYYY-MM-DD_shadow.txt 리포트를 쓴다.
매매 로직에는 관여하지 않는다(기록·채점 전용).
"""
from __future__ import annotations

import contextlib
import csv
import datetime as _dt
import io
import os
import re
import threading
from collections import defaultdict
from typing import Callable, Dict, List, Optional, Tuple

try:
    import yfinance as yf  # type: ignore
except Exception:  # pragma: no cover
    yf = None  # type: ignore

_ROOT = os.path.dirname(os.path.abspath(__file__))
LEDGER_PATH = os.path.join(_ROOT, "data", "shadow_signals.csv")
OUTCOME_PATH = os.path.join(_ROOT, "data", "shadow_outcomes.csv")
LOG_DIR = os.path.join(_ROOT, "log")

LEDGER_FIELDS = [
    "date", "time", "code", "price", "strategy", "reason_key", "reason",
    "ai_proba", "rsi14", "ma20_rel", "vol_ratio", "regime",
]
OUTCOME_FIELDS = LEDGER_FIELDS + ["exit", "net_ret", "max_up", "max_down", "scored_on"]

_lock = threading.Lock()
_seen_day = ""
_seen: set = set()


def reason_key(reason: str) -> Tuple[str, str]:
    """'전략조건 미충족(trend: AI 상승확률 부족(트렌드·완화 thr=0.50))' → ('trend', 'AI 상승확률 부족')."""
    inner = reason
    m = re.match(r"^[^(]*\((.*)\)\s*$", reason)
    if m:
        inner = m.group(1)
    strategy = ""
    if ":" in inner:
        strategy, inner = inner.split(":", 1)
    inner = re.sub(r"\s*\([^()]*\d[^()]*\)\s*$", "", inner).strip()
    inner = re.sub(r"(?<![A-Za-z\d])[-+]?\d+(\.\d+)?%?", "#", inner)
    return strategy.strip(), inner


def record(
    code: str,
    price: float,
    reason: str,
    *,
    ai_proba: Optional[float] = None,
    rsi14: Optional[float] = None,
    ma20_rel: Optional[float] = None,
    vol_ratio: Optional[float] = None,
    regime: str = "",
) -> bool:
    global _seen_day
    code = str(code or "").strip()
    if not code or not price or price <= 0:
        return False
    now = _dt.datetime.now()
    day = now.strftime("%Y-%m-%d")
    strategy, key = reason_key(str(reason))
    with _lock:
        if _seen_day != day:
            _seen_day = day
            _seen.clear()
            _seen.update(_load_seen_for_day(day))
        k = (code, strategy, key)
        if k in _seen:
            return False
        _seen.add(k)
        row = {
            "date": day,
            "time": now.strftime("%H:%M:%S"),
            "code": code,
            "price": f"{float(price):.2f}",
            "strategy": strategy,
            "reason_key": key,
            "reason": str(reason)[:160],
            "ai_proba": "" if ai_proba is None else f"{float(ai_proba):.4f}",
            "rsi14": "" if rsi14 is None else f"{float(rsi14):.2f}",
            "ma20_rel": "" if ma20_rel is None else f"{float(ma20_rel):.4f}",
            "vol_ratio": "" if vol_ratio is None else f"{float(vol_ratio):.3f}",
            "regime": str(regime or ""),
        }
        try:
            os.makedirs(os.path.dirname(LEDGER_PATH), exist_ok=True)
            new = not os.path.isfile(LEDGER_PATH) or os.path.getsize(LEDGER_PATH) == 0
            with open(LEDGER_PATH, "a", encoding="utf-8-sig", newline="") as f:
                w = csv.DictWriter(f, fieldnames=LEDGER_FIELDS)
                if new:
                    w.writeheader()
                w.writerow(row)
        except Exception:
            return False
    return True


def _load_seen_for_day(day: str) -> set:
    out: set = set()
    for r in _read_csv(LEDGER_PATH):
        if r.get("date") == day:
            out.add((r.get("code", ""), r.get("strategy", ""), r.get("reason_key", "")))
    return out


def _read_csv(path: str) -> List[Dict[str, str]]:
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f))
    except Exception:
        return []


def _fetch_daily(code: str):
    if yf is None:
        return None
    for t in (f"{code}.KS", f"{code}.KQ"):
        try:
            sink = io.StringIO()
            with contextlib.redirect_stderr(sink), contextlib.redirect_stdout(sink):
                df = yf.Ticker(t).history(period="3mo", interval="1d", auto_adjust=False)
            if df is not None and getattr(df.columns, "nlevels", 1) > 1:
                df.columns = df.columns.get_level_values(0)
            if df is not None and not df.empty and {"High", "Low", "Close"} <= set(df.columns):
                return df
        except Exception:
            continue
    return None


def _score_row(r: Dict[str, str], df, horizon: int, stop_pct: float, take_pct: float, cost_pct: float):
    """진입=기록가. D+1..D+horizon 일봉: 같은 날 손절·익절 동시 도달 시 손절(보수적)."""
    try:
        entry = float(r["price"])
        d0 = _dt.date.fromisoformat(r["date"])
    except Exception:
        return None
    days = [(idx.date(), row) for idx, row in df.iterrows() if idx.date() > d0]
    if len(days) < horizon:
        return None
    path = days[:horizon]
    stop_px = entry * (1.0 + stop_pct)
    take_px = entry * (1.0 + take_pct)
    max_up = max(float(row["High"]) for _, row in path) / entry - 1.0
    max_down = min(float(row["Low"]) for _, row in path) / entry - 1.0
    exit_kind, ret = "close", float(path[-1][1]["Close"]) / entry - 1.0
    for _, row in path:
        if float(row["Low"]) <= stop_px:
            exit_kind, ret = "stop", stop_pct
            break
        if float(row["High"]) >= take_px:
            exit_kind, ret = "take", take_pct
            break
    return exit_kind, ret - cost_pct, max_up, max_down


def _bars_after(r: Dict[str, str], df) -> int:
    try:
        d0 = _dt.date.fromisoformat(r["date"])
    except Exception:
        return 0
    return sum(1 for idx in df.index if idx.date() > d0)


def score_and_report(
    *,
    stop_pct: float,
    take_pct: float,
    cost_pct: float,
    horizon: int = 3,
    max_codes: int = 200,
    log_fn: Optional[Callable[[str], None]] = None,
) -> str:
    """채점 후 리포트 경로 반환(실패 시 빈 문자열)."""
    ledger = _read_csv(LEDGER_PATH)
    done = {
        (r.get("date"), r.get("code"), r.get("strategy"), r.get("reason_key"))
        for r in _read_csv(OUTCOME_PATH)
    }
    today = _dt.date.today()
    pending = [
        r for r in ledger
        if (r.get("date"), r.get("code"), r.get("strategy"), r.get("reason_key")) not in done
        and r.get("date", "") < today.isoformat()
    ]
    codes = list(dict.fromkeys(r["code"] for r in pending))[: max(1, int(max_codes))]
    cache: Dict[str, object] = {}
    for c in codes:
        cache[c] = _fetch_daily(c)
    new_rows: List[Dict[str, str]] = []
    prelim: List[Dict[str, str]] = []
    for r in pending:
        df = cache.get(r["code"])
        if df is None:
            continue
        res = _score_row(r, df, horizon, stop_pct, take_pct, cost_pct)
        if res is None:
            held = _bars_after(r, df)
            if 1 <= held < horizon:
                part = _score_row(r, df, held, stop_pct, take_pct, cost_pct)
                if part is not None:
                    p = dict(r)
                    p.update({
                        "exit": part[0],
                        "net_ret": f"{part[1]:.4f}",
                        "max_up": f"{part[2]:.4f}",
                        "max_down": f"{part[3]:.4f}",
                    })
                    prelim.append(p)
            continue
        exit_kind, net, up, dn = res
        out = dict(r)
        out.update({
            "exit": exit_kind,
            "net_ret": f"{net:.4f}",
            "max_up": f"{up:.4f}",
            "max_down": f"{dn:.4f}",
            "scored_on": today.isoformat(),
        })
        new_rows.append(out)
    if new_rows:
        os.makedirs(os.path.dirname(OUTCOME_PATH), exist_ok=True)
        new = not os.path.isfile(OUTCOME_PATH) or os.path.getsize(OUTCOME_PATH) == 0
        with open(OUTCOME_PATH, "a", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=OUTCOME_FIELDS, extrasaction="ignore")
            if new:
                w.writeheader()
            w.writerows(new_rows)
    waiting = max(0, len(ledger) - len(done) - len(new_rows))
    path = _write_report(
        today, len(ledger), waiting, len(new_rows), horizon, stop_pct, take_pct, cost_pct, prelim
    )
    if log_fn is not None:
        try:
            log_fn(
                f"[SHADOW] 채점 +{len(new_rows)}건 · 예비 {len(prelim)}건 "
                f"(기록 {len(ledger)} · 대기 {waiting}) "
                f"→ {os.path.relpath(path, _ROOT) if path else '-'}"
            )
        except Exception:
            pass
    return path


_MA20_BINS = (
    (-9.0, -0.03, "<-3%"), (-0.03, 0.0, "-3~0%"), (0.0, 0.02, "0~2%"),
    (0.02, 0.04, "2~4%"), (0.04, 0.07, "4~7%"), (0.07, 9.0, "≥7%"),
)


def _f(r: Dict[str, str], k: str) -> Optional[float]:
    try:
        v = r.get(k)
        return float(v) if v not in (None, "") else None
    except Exception:
        return None


def _stats(v: List[Dict[str, str]]) -> Tuple[int, float, float, float, float]:
    """(n, 승률, 평균 순수익, 손절 비율, 평균 최대상승)."""
    nets = [_f(r, "net_ret") or 0.0 for r in v]
    n = len(nets)
    if not n:
        return 0, 0.0, 0.0, 0.0, 0.0
    stops = sum(1 for r in v if r.get("exit") == "stop")
    ups = [_f(r, "max_up") or 0.0 for r in v]
    return n, sum(1 for x in nets if x > 0) / n, sum(nets) / n, stops / n, sum(ups) / n


def _row_txt(label: str, width: int, v: List[Dict[str, str]]) -> str:
    n, wr, avg, sr, up = _stats(v)
    return f"  {label[:width - 1]:<{width}}{n:>5}{wr*100:>7.0f}%{avg*100:>8.2f}%{sr*100:>7.0f}%{up*100:>8.2f}%"


def _section(rows: List[Dict[str, str]], title: str) -> List[str]:
    rows = [r for r in rows if _f(r, "net_ret") is not None]
    if not rows:
        return [f"■ {title}: 없음"]
    head = f"{'n':>5}{'승률':>7}{'평균':>8}{'손절':>6}{'최대↑':>7}"
    groups: Dict[Tuple[str, str], List[Dict[str, str]]] = defaultdict(list)
    ai_bins: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    ma_bins: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    for r in rows:
        groups[(r.get("strategy", ""), r.get("reason_key", ""))].append(r)
        pv = _f(r, "ai_proba")
        if pv is not None and "AI" in r.get("reason_key", ""):
            lo = int(pv * 50) / 50
            ai_bins[f"{lo:.2f}~{lo + 0.02:.2f}"].append(r)
        m = _f(r, "ma20_rel")
        if m is not None:
            for lo, hi, name in _MA20_BINS:
                if lo <= m < hi:
                    ma_bins[name].append(r)
                    break
    out = [
        f"■ {title} — 차단 사유별 '샀다면' 성과 (기대값 순)",
        f"  {'전략':<12}{'사유':<28}{head}",
        _row_txt("전체", 40, rows),
    ]
    for (strat, key), v in sorted(groups.items(), key=lambda kv: -_stats(kv[1])[2]):
        out.append(_row_txt(f"{strat[:11]:<12}{key[:27]}", 40, v))
    if ai_bins:
        out += ["", f"  AI 확률 구간별 (AI 문턱 차단분)", f"  {'구간':<14}{head}"]
        out += [_row_txt(b, 14, ai_bins[b]) for b in sorted(ai_bins)]
    if ma_bins:
        out += ["", f"  MA20 이격 구간별 (전체 차단분)", f"  {'구간':<14}{head}"]
        out += [_row_txt(name, 14, ma_bins[name]) for _, _, name in _MA20_BINS if ma_bins.get(name)]
    return out


EVIDENCE_CATEGORIES = {"vol": ("거래량",), "ma": ("MA20",), "rsi": ("RSI",), "ai": ("AI",)}


def evidence_summary(lookback_days: int = 30) -> Dict[str, object]:
    """확정 채점 결과를 일일 전략 선택용 차단 카테고리(vol/ma/rsi/ai)별로 요약."""
    cutoff = (_dt.date.today() - _dt.timedelta(days=int(lookback_days))).isoformat()
    rows = [
        r for r in _read_csv(OUTCOME_PATH)
        if r.get("date", "") >= cutoff and _f(r, "net_ret") is not None
    ]
    cats: Dict[str, Dict[str, float]] = {}
    for cat, keys in EVIDENCE_CATEGORIES.items():
        v = [r for r in rows if any(k in r.get("reason_key", "") for k in keys)]
        n, wr, avg, sr, _up = _stats(v)
        cats[cat] = {"n": n, "win": round(wr, 3), "avg": round(avg, 4), "stop": round(sr, 3)}
    return {"n_total": len(rows), "lookback_days": int(lookback_days), "categories": cats}


def _write_report(
    today: _dt.date, n_ledger: int, n_waiting: int, n_new: int,
    horizon: int, stop_pct: float, take_pct: float, cost_pct: float,
    prelim: Optional[List[Dict[str, str]]] = None,
) -> str:
    sep = "=" * 64
    lines = [
        sep,
        f"  [SHADOW] {today.isoformat()} 놓친 신호 채점 리포트 (오답노트)",
        f"  기준: 기록가 진입 → {horizon}거래일, 손절 {stop_pct*100:+.1f}% / 익절 {take_pct*100:+.1f}% "
        f"/ 비용 {cost_pct*100:.2f}% 차감 (같은 날 손절·익절 동시 도달 시 손절로 간주)",
        sep,
        f"  누적 기록 {n_ledger}건 | 이번 채점 {n_new}건 | 예비 {len(prelim or [])}건 | 채점 대기 {n_waiting}건",
        "",
    ]
    lines += _section(_read_csv(OUTCOME_PATH), f"확정 ({horizon}거래일)")
    lines += [""]
    lines += _section(prelim or [], f"예비 (1~{horizon - 1}거래일 경과분, 오늘 종가까지)")
    lines += [
        "",
        "  해석: 평균이 꾸준히 +이고 n이 충분(≥30)한 사유는 '좋은 신호를 막는' 조건 후보.",
        "        평균이 −인 사유는 그 차단이 손실을 막아 준 것. 표본이 적을 때는 판단 보류.",
        sep,
        "",
    ]
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        path = os.path.join(LOG_DIR, f"{today.isoformat()}_shadow.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        return path
    except Exception:
        return ""
