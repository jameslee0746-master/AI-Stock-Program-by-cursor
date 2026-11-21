"""Stage3.2B: 3.2A 분석 복사본만 읽어 오프라인 패턴 분석. 원본·v2 미수정."""
from __future__ import annotations

import csv
import hashlib
import math
import os
from collections import Counter, defaultdict, deque
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
V2 = os.path.join(ROOT, "data", "scalp", "analysis", "stage31_5day_v2")
OUT = os.path.join(V2, "stage32b_analysis")
ALIGN_LAG_SEC = 1.0
DUP_WINDOW = 256

DAYS = [
    {"id": "D1", "date": "2026-11-16", "folder": "D1_20261116", "csat": False},
    {"id": "D2", "date": "2026-11-17", "folder": "D2_20261117", "csat": False},
    {"id": "D3", "date": "2026-11-18", "folder": "D3_20261118", "csat": False},
    {"id": "D4", "date": "2026-11-19", "folder": "D4_20261119_CSAT", "csat": True},
    {"id": "D5", "date": "2026-11-20", "folder": "D5_20261120", "csat": False},
]

D5_ORIG_TICK = os.path.join(
    ROOT, "data", "scalp", "archive", "20261120_072832_s2c-12bc8ee0df21",
    "tick_005930_20261120_observe_part001.csv",
)
D5_ORIG_HOGA = os.path.join(
    ROOT, "data", "scalp", "archive", "20261120_072832_s2c-12bc8ee0df21",
    "orderbook_005930_20261120_observe_part001.csv",
)


def rel(p: str) -> str:
    return os.path.relpath(p, ROOT).replace("\\", "/")


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_iso(s: str) -> Optional[datetime]:
    t = str(s or "").strip()
    if not t:
        return None
    try:
        return datetime.fromisoformat(t)
    except ValueError:
        return None


def abs_int(raw: Any) -> Optional[int]:
    t = str(raw or "").strip().replace(",", "")
    if not t or t in ("-0", "+0"):
        try:
            v = int(float(t)) if t else None
        except ValueError:
            return None
        return 0 if v == 0 else None
    try:
        return abs(int(t))
    except ValueError:
        try:
            return abs(int(float(t)))
        except ValueError:
            return None


def floor_bucket(dt: datetime, sec: int) -> datetime:
    total = dt.hour * 3600 + dt.minute * 60 + dt.second
    fl = total - (total % sec)
    return dt.replace(hour=fl // 3600, minute=(fl % 3600) // 60, second=fl % 60, microsecond=0)


class Welford:
    def __init__(self) -> None:
        self.n = 0
        self.mean = 0.0
        self.m2 = 0.0

    def add(self, x: float) -> None:
        self.n += 1
        d = x - self.mean
        self.mean += d / self.n
        self.m2 += d * (x - self.mean)

    def std(self) -> Optional[float]:
        if self.n < 2:
            return None
        return math.sqrt(self.m2 / (self.n - 1))


def percentile(xs: List[float], p: float) -> Optional[float]:
    if not xs:
        return None
    ys = sorted(xs)
    if len(ys) == 1:
        return ys[0]
    k = (len(ys) - 1) * p
    f = int(math.floor(k))
    c = int(math.ceil(k))
    if f == c:
        return ys[f]
    return ys[f] * (c - k) + ys[c] * (k - f)


def fid15_class(raw: str) -> str:
    s = str(raw or "")
    if s.startswith("+"):
        return "plus"
    if s.startswith("-"):
        return "minus"
    return "unsigned"


def load_ticks(path: str) -> List[dict]:
    rows: List[dict] = []
    with open(path, encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            dt = parse_iso(r.get("recv_iso", ""))
            if dt is None:
                continue
            px = abs_int(r.get("fid10") or r.get("fid10_raw"))
            vol = abs_int(r.get("fid15") or r.get("fid15_raw"))
            try:
                st = float(str(r.get("fid228") or "").strip() or "nan")
            except ValueError:
                st = float("nan")
            rows.append(
                {
                    "dt": dt,
                    "ts": dt.timestamp(),
                    "px": px,
                    "vol": vol or 0,
                    "st": st if st == st else None,
                    "fid10_raw": r.get("fid10_raw", ""),
                    "fid13_raw": r.get("fid13_raw", ""),
                    "fid15_raw": r.get("fid15_raw", ""),
                    "fid20_raw": r.get("fid20_raw", ""),
                    "fid228_raw": r.get("fid228_raw", ""),
                    "seq": r.get("seq", ""),
                    "recv_iso": r.get("recv_iso", ""),
                    "recv_ns": r.get("recv_ns", ""),
                    "s15": fid15_class(r.get("fid15_raw", "")),
                }
            )
    return rows


def load_hoga(path: str) -> List[dict]:
    rows: List[dict] = []
    with open(path, encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            dt = parse_iso(r.get("recv_iso", ""))
            if dt is None:
                continue
            asks_px = [abs_int(r.get("fid%d_raw" % f)) for f in (41, 42, 43, 44, 45)]
            bids_px = [abs_int(r.get("fid%d_raw" % f)) for f in (51, 52, 53, 54, 55)]
            asks_q = [abs_int(r.get("fid%d_raw" % f)) or 0 for f in (61, 62, 63, 64, 65)]
            bids_q = [abs_int(r.get("fid%d_raw" % f)) or 0 for f in (71, 72, 73, 74, 75)]
            ask1p, bid1p = asks_px[0], bids_px[0]
            ask1q, bid1q = asks_q[0], bids_q[0]
            ask5 = sum(asks_q)
            bid5 = sum(bids_q)
            ask_tot = abs_int(r.get("fid121_raw"))
            bid_tot = abs_int(r.get("fid125_raw"))
            empty45 = sum(1 for q in asks_q[3:] + bids_q[3:] if q == 0)
            rows.append(
                {
                    "dt": dt,
                    "ts": dt.timestamp(),
                    "ask1p": ask1p,
                    "bid1p": bid1p,
                    "ask1q": ask1q,
                    "bid1q": bid1q,
                    "ask5": ask5,
                    "bid5": bid5,
                    "ask_tot": ask_tot,
                    "bid_tot": bid_tot,
                    "empty45": empty45,
                    "snap": tuple(
                        r.get("fid%d_raw" % f, "")
                        for f in (41, 42, 43, 44, 45, 51, 52, 53, 54, 55, 61, 62, 63, 64, 65, 71, 72, 73, 74, 75, 121, 125)
                    ),
                    "seq": r.get("seq", ""),
                }
            )
    return rows


def imb(bid: Optional[int], ask: Optional[int]) -> Optional[float]:
    if bid is None or ask is None:
        return None
    den = bid + ask
    if den == 0:
        return None
    return (bid - ask) / den


def tick_buckets(rows: List[dict], sec: int) -> List[dict]:
    groups: Dict[datetime, List[dict]] = defaultdict(list)
    for r in rows:
        groups[floor_bucket(r["dt"], sec)].append(r)
    out = []
    for start in sorted(groups):
        g = groups[start]
        vols = [x["vol"] for x in g]
        pxs = [x["px"] for x in g if x["px"] is not None]
        sts = [x["st"] for x in g if x["st"] is not None]
        wf = Welford()
        for s in sts:
            wf.add(s)
        px_o = pxs[0] if pxs else None
        px_c = pxs[-1] if pxs else None
        ret = None
        rng = None
        if px_o and px_c is not None:
            ret = 10000.0 * (px_c - px_o) / px_o
        if pxs and px_o:
            rng = 10000.0 * (max(pxs) - min(pxs)) / px_o
        out.append(
            {
                "bucket_start": start.isoformat(),
                "n_trades": len(g),
                "vol_sum": sum(vols),
                "vol_mean": round(sum(vols) / len(g), 6) if g else None,
                "vol_max": max(vols) if vols else None,
                "px_open": px_o,
                "px_high": max(pxs) if pxs else None,
                "px_low": min(pxs) if pxs else None,
                "px_close": px_c,
                "ret_bps": None if ret is None else round(ret, 6),
                "range_bps": None if rng is None else round(rng, 6),
                "strength_n": wf.n,
                "strength_mean": None if not wf.n else round(wf.mean, 6),
                "strength_std": None if wf.std() is None else round(wf.std(), 6),
                "n_fid15_plus": sum(1 for x in g if x["s15"] == "plus"),
                "n_fid15_minus": sum(1 for x in g if x["s15"] == "minus"),
                "n_fid15_unsigned": sum(1 for x in g if x["s15"] == "unsigned"),
            }
        )
    return out


def hoga_buckets(rows: List[dict], sec: int) -> List[dict]:
    groups: Dict[datetime, List[dict]] = defaultdict(list)
    for r in rows:
        groups[floor_bucket(r["dt"], sec)].append(r)
    out = []
    for start in sorted(groups):
        g = groups[start]
        n = len(g)

        def mean(xs: List[Optional[float]]) -> Optional[float]:
            vs = [x for x in xs if x is not None]
            return None if not vs else round(sum(vs) / len(vs), 6)

        spreads, mids = [], []
        imb1, imb5, imbt = [], [], []
        zden = 0
        for x in g:
            if x["ask1p"] and x["bid1p"]:
                spreads.append(x["ask1p"] - x["bid1p"])
                mids.append((x["ask1p"] + x["bid1p"]) / 2.0)
            v = imb(x["bid1q"], x["ask1q"])
            if v is None:
                zden += 1
            else:
                imb1.append(v)
            v5 = imb(x["bid5"], x["ask5"])
            if v5 is not None:
                imb5.append(v5)
            vt = imb(x["bid_tot"], x["ask_tot"])
            if vt is not None:
                imbt.append(vt)
        bid5s = [x["bid5"] for x in g]
        ask5s = [x["ask5"] for x in g]
        ratios = []
        for x in g:
            if x["ask5"]:
                ratios.append(x["bid5"] / x["ask5"])
        out.append(
            {
                "bucket_start": start.isoformat(),
                "n_quotes": n,
                "bid1_qty_mean": mean([x["bid1q"] for x in g]),
                "ask1_qty_mean": mean([x["ask1q"] for x in g]),
                "bid5_qty_mean": mean(bid5s),
                "ask5_qty_mean": mean(ask5s),
                "bid_ask_qty_ratio_mean": mean(ratios),
                "spread_mean": mean(spreads),
                "spread_n": len(spreads),
                "mid_mean": mean(mids),
                "imb_l1_mean": mean(imb1),
                "imb_l1_n": len(imb1),
                "imb_l5_mean": mean(imb5),
                "imb_l5_n": len(imb5),
                "imb_tot_mean": mean(imbt),
                "imb_tot_n": len(imbt),
                "n_zero_denom": zden,
                "bid5_start": bid5s[0],
                "bid5_end": bid5s[-1],
                "ask5_start": ask5s[0],
                "ask5_end": ask5s[-1],
                "bid5_delta": bid5s[-1] - bid5s[0],
                "ask5_delta": ask5s[-1] - ask5s[0],
                "empty_lvl4plus_mean": mean([x["empty45"] for x in g]),
            }
        )
    return out


def vol_from_1min(buckets: List[dict]) -> Optional[float]:
    rets = [b["ret_bps"] for b in buckets if b.get("ret_bps") is not None]
    if len(rets) < 2:
        return None
    wf = Welford()
    for x in rets:
        wf.add(x)
    return None if wf.std() is None else round(wf.std(), 6)


def align(ticks: List[dict], hoga: List[dict]) -> Dict[str, Any]:
    if not ticks:
        return {"n_ticks": 0, "n_hoga": len(hoga), "matched": 0, "unmatched": 0}
    j = -1
    dts: List[float] = []
    unmatched = 0
    matched = 0
    used_for_ret = 0
    skipped_lag = 0
    imb_ret: List[Tuple[float, Optional[float], Optional[float], Optional[float]]] = []
    depth_chg: List[Tuple[float, float]] = []
    ti = 0
    for i, t in enumerate(ticks):
        ts = t["ts"]
        while j + 1 < len(hoga) and hoga[j + 1]["ts"] <= ts:
            j += 1
        if j < 0:
            unmatched += 1
            continue
        dt = ts - hoga[j]["ts"]
        if dt < 0:
            unmatched += 1
            continue
        matched += 1
        dts.append(dt)
        if dt > ALIGN_LAG_SEC:
            skipped_lag += 1
            continue
        h = hoga[j]
        iv = imb(h["bid5"], h["ask5"])
        if j > 0:
            prev = hoga[j - 1]
            depth_chg.append((h["bid5"] - prev["bid5"], h["ask5"] - prev["ask5"]))
        px0 = t["px"]
        r1 = r5 = None
        if px0:
            t1 = ts + 1.0
            t5 = ts + 5.0
            k = i + 1
            last1 = last5 = None
            while k < len(ticks) and ticks[k]["ts"] <= t5:
                if ticks[k]["ts"] <= t1 and ticks[k]["px"] is not None:
                    last1 = ticks[k]["px"]
                if ticks[k]["px"] is not None:
                    last5 = ticks[k]["px"]
                k += 1
            if last1 is not None:
                r1 = 10000.0 * (last1 - px0) / px0
            if last5 is not None:
                r5 = 10000.0 * (last5 - px0) / px0
        if iv is not None:
            imb_ret.append((iv, r1, r5, t["px"]))
            used_for_ret += 1
    hist = Counter()
    edges = [0.0, 0.01, 0.05, 0.1, 0.2, 0.5, 1.0, 2.0, 5.0, 10.0, 1e9]
    labels = ["<0.01", "0.01-0.05", "0.05-0.1", "0.1-0.2", "0.2-0.5", "0.5-1", "1-2", "2-5", "5-10", ">10"]
    for d in dts:
        for e, lab in zip(edges[1:], labels):
            if d <= e:
                hist[lab] += 1
                break
    def qstats(xs: List[float]) -> Dict[str, Any]:
        return {
            "n": len(xs),
            "p50": None if not xs else round(percentile(xs, 0.5) or 0, 6),
            "p90": None if not xs else round(percentile(xs, 0.9) or 0, 6),
            "p99": None if not xs else round(percentile(xs, 0.99) or 0, 6),
            "mean": None if not xs else round(sum(xs) / len(xs), 6),
            "frac_le_0_1": None if not xs else round(sum(1 for x in xs if x <= 0.1) / len(xs), 6),
            "frac_le_1": None if not xs else round(sum(1 for x in xs if x <= 1.0) / len(xs), 6),
        }

    quint = []
    if imb_ret:
        xs = sorted(v[0] for v in imb_ret)
        cuts = [percentile(xs, p) or 0 for p in (0.2, 0.4, 0.6, 0.8)]

        def band(v: float) -> int:
            for i, c in enumerate(cuts):
                if v <= c:
                    return i
            return 4

        buckets: Dict[int, List[Tuple[Optional[float], Optional[float]]]] = defaultdict(list)
        for iv, r1, r5, _ in imb_ret:
            buckets[band(iv)].append((r1, r5))
        for b in range(5):
            r1s = [x[0] for x in buckets[b] if x[0] is not None]
            r5s = [x[1] for x in buckets[b] if x[1] is not None]
            quint.append(
                {
                    "q": b + 1,
                    "n": len(buckets[b]),
                    "imb_hi": None if b >= len(cuts) else round(cuts[b], 6),
                    "mean_ret_1s_bps": None if not r1s else round(sum(r1s) / len(r1s), 6),
                    "n_ret_1s": len(r1s),
                    "mean_ret_5s_bps": None if not r5s else round(sum(r5s) / len(r5s), 6),
                    "n_ret_5s": len(r5s),
                }
            )
    bid_ch = [x[0] for x in depth_chg]
    ask_ch = [x[1] for x in depth_chg]
    return {
        "n_ticks": len(ticks),
        "n_hoga": len(hoga),
        "matched": matched,
        "unmatched": unmatched,
        "skipped_lag_gt_1s": skipped_lag,
        "used_imb_ret": used_for_ret,
        "dt": qstats(dts),
        "hist": dict(hist),
        "quintiles": quint,
        "pre_trade_bid5_delta_mean": None if not bid_ch else round(sum(bid_ch) / len(bid_ch), 6),
        "pre_trade_ask5_delta_mean": None if not ask_ch else round(sum(ask_ch) / len(ask_ch), 6),
        "n_pre_trade_depth": len(depth_chg),
        "align_rule": "last hoga with recv_iso <= tick.recv_iso; no future quotes",
        "max_lag_used_for_relation_sec": ALIGN_LAG_SEC,
    }


def investigate_dup(path: str) -> Dict[str, Any]:
    recent: deque = deque(maxlen=DUP_WINDOW)
    n = 0
    n_sus = 0
    same_sec = 0
    same_fp_diff_iso = 0
    exact_iso_fp = 0
    prev = None
    fp_iso: Dict[Tuple[str, str, str, str], str] = {}
    bursts = Counter()
    exact_business = 0
    seen_business: Dict[Tuple[str, str, str, str, str], int] = {}
    consecutive_same_fp = 0
    with open(path, encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            n += 1
            fp = (
                str(r.get("code") or ""),
                "주식체결",
                str(r.get("fid20_raw") or ""),
                str(r.get("fid15_raw") or ""),
                str(r.get("fid10_raw") or ""),
            )
            iso = str(r.get("recv_iso") or "")
            biz = (
                str(r.get("fid10_raw") or ""),
                str(r.get("fid13_raw") or ""),
                str(r.get("fid15_raw") or ""),
                str(r.get("fid20_raw") or ""),
                str(r.get("fid228_raw") or ""),
            )
            seen_business[biz] = seen_business.get(biz, 0) + 1
            if fp in recent:
                n_sus += 1
                if prev and prev["fid20_raw"] == r.get("fid20_raw"):
                    same_sec += 1
                prev_iso = fp_iso.get(fp)
                if prev_iso == iso:
                    exact_iso_fp += 1
                elif prev_iso:
                    same_fp_diff_iso += 1
            if prev and (
                prev["fid20_raw"] == r.get("fid20_raw")
                and prev["fid15_raw"] == r.get("fid15_raw")
                and prev["fid10_raw"] == r.get("fid10_raw")
            ):
                consecutive_same_fp += 1
            recent.append(fp)
            fp_iso[fp] = iso
            bursts[str(r.get("fid20_raw") or "")] += 1
            prev = r
    extra = sum(v - 1 for v in seen_business.values() if v > 1)
    max_burst = max(bursts.values()) if bursts else 0
    n_seconds = len(bursts)
    return {
        "rows": n,
        "fingerprint_window": DUP_WINDOW,
        "fingerprint": "(code, real_type, fid20_raw, fid15_raw, fid10_raw)",
        "dup_suspect_replay": n_sus,
        "suspect_same_fid20_as_prev": same_sec,
        "suspect_same_fp_same_recv_iso": exact_iso_fp,
        "suspect_same_fp_diff_recv_iso": same_fp_diff_iso,
        "consecutive_identical_fp": consecutive_same_fp,
        "business_key_repeat_extra": extra,
        "unique_business_keys": len(seen_business),
        "unique_fid20": n_seconds,
        "max_prints_per_fid20": max_burst,
        "mean_prints_per_fid20": None if not n_seconds else round(n / n_seconds, 3),
        "note": "fid20 is HHMMSS so many prints share one exchange second. Collector still writes the row.",
    }


def hoga_repeat_stats(rows: List[dict]) -> Dict[str, Any]:
    n = len(rows)
    consec = 0
    for i in range(1, n):
        if rows[i]["snap"] == rows[i - 1]["snap"]:
            consec += 1
    return {"rows": n, "consecutive_identical_snapshot": consec, "pct": None if not n else round(100.0 * consec / n, 3)}


def write_dict(path: str, header_tick: List[str], header_hoga: List[str], emp: Dict[str, Any]) -> None:
    lines = [
        "# Stage3.2B 데이터 사전",
        "",
        "입력: `data/scalp/analysis/stage31_5day_v2/` (Stage3.2A 복사본).",
        "원본 아카이브는 D5 dup_suspect 재현에만 읽기 전용으로 사용했다.",
        "",
        "## 체결 tick.csv",
        "",
        "수집 코드 `scalp/collector/tick_collector.py` TICK_FIELDS 와 3.2A 헤더가 같다.",
        "",
        "| 컬럼 | FID/의미 | 형식 | 부호 | 결측 |",
        "|---|---|---|---|---|",
        "| recv_ns | 로컬 수신 단조 시각 | int | 없음 | 없음 |",
        "| recv_iso | 로컬 수신 시각 | ISO 마이크로초 | 없음 | 없음 |",
        "| code | 종목 | 005930 | 없음 | 없음 |",
        "| real_type | 실시간 타입 | 주식체결 | 없음 | 없음 |",
        "| fid10_raw / fid10 | FID10 현재가 | 원 | `+` 상승, `-` 하락(전일 대비 부호). **매수/매도 아님** | fid10_ok |",
        "| fid13_raw / fid13 | FID13 누적거래량 | 주 | 보통 무부호 | fid13_ok |",
        "| fid15_raw / fid15 | FID15 체결량 | 주 | `+`/`-`/무부호. 키움 관례상 매수·매도 추정 가능하나 **단정하지 않음** | fid15_ok |",
        "| fid20_raw | FID20 체결시간 | HHMMSS (초 단위) | 없음 | fid20_ok |",
        "| fid20_sod | FID20 초환산 | 0~86399 | 없음 | |",
        "| fid228_raw / fid228 | FID228 체결강도 | 실수 | 없음 | fid228_ok |",
        "| clock_offset_sec | 로컬초−FID20초 | 초 | 지연이 아님 | |",
        "| seq | tick+호가 전역 일련번호 | int | 한 파일 안 연속 가정 금지 | 없음 |",
        "",
        "실제 헤더: `%s`" % ",".join(header_tick),
        "",
        "3.2A 연속매매 fid15 부호 분포(단정 아님):",
        "",
        "| 날짜 | + | - | 무부호 |",
        "|---|---:|---:|---:|",
    ]
    for did, d in emp.items():
        lines.append("| %s | %s | %s | %s |" % (did, d["plus"], d["minus"], d["unsigned"]))
    lines += [
        "",
        "무부호 1건은 각 날 시초 대량 체결(동시호가 체결 묶음)이다. 매수·매도로 분류하지 않았다.",
        "",
        "## 호가 orderbook.csv",
        "",
        "수집 코드 `HOGA_FIDS` = 21,41-45,51-55,61-65,71-75,121,125. 3.2A 헤더와 일치.",
        "",
        "| 컬럼 | 의미 | 비고 |",
        "|---|---|---|",
        "| fid21_raw | 호가시간 HHMMSS | 초 단위 |",
        "| fid41-45_raw | 매도호가 1~5 | 현재가와 같이 `-` 하락 부호. 가격은 abs 사용 |",
        "| fid51-55_raw | 매수호가 1~5 | 동일 |",
        "| fid61-65_raw | 매도잔량 1~5 | `-0`/0 은 해당 단계 없음 |",
        "| fid71-75_raw | 매수잔량 1~5 | 동일 |",
        "| fid121_raw | 매도 총잔량 | 5단계보다 넓을 수 있음 |",
        "| fid125_raw | 매수 총잔량 | 동일 |",
        "| seq | 전역 일련번호 | tick과 섞임 |",
        "",
        "실제 헤더: `%s`" % ",".join(header_hoga),
        "",
        "분석 규칙: 가격·수량은 절댓값. 단계 수량 0은 깊이 합에 0으로 포함. 스프레드/중간가는 1호가 가격 둘 다 >0 일 때만.",
        "불균형 = (bid - ask) / (bid + ask). 분모 0은 제외하고 `n_zero_denom`에 센다.",
        "L1=1호가 수량, L5=1~5호가 수량 합, TOT=FID121/125.",
        "KRX/NXT 구분은 컬럼에 없어 붙이지 않았다.",
        "",
    ]
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines))


def main() -> int:
    if os.path.isdir(OUT) and os.listdir(OUT):
        raise SystemExit("결과 폴더가 이미 있습니다. 덮어쓰지 않습니다: %s" % OUT)
    os.makedirs(OUT, exist_ok=False)
    v2_hashes = {}
    for spec in DAYS:
        for kind in ("tick", "orderbook"):
            for sess in ("continuous", "closing_auction"):
                p = os.path.join(V2, spec["folder"], sess, "%s.csv" % kind)
                v2_hashes[p] = sha256_file(p)
    orig_hashes = {}
    for p in (D5_ORIG_TICK, D5_ORIG_HOGA):
        orig_hashes[p] = sha256_file(p)

    time_fields = [
        "day", "date", "csat", "session", "bucket_sec", "bucket_start", "n_trades",
        "vol_sum", "vol_mean", "vol_max", "px_open", "px_high", "px_low", "px_close",
        "ret_bps", "range_bps", "strength_n", "strength_mean", "strength_std",
        "n_fid15_plus", "n_fid15_minus", "n_fid15_unsigned",
    ]
    ob_fields = [
        "day", "date", "csat", "session", "bucket_sec", "bucket_start", "n_quotes",
        "bid1_qty_mean", "ask1_qty_mean", "bid5_qty_mean", "ask5_qty_mean",
        "bid_ask_qty_ratio_mean", "spread_mean", "spread_n", "mid_mean",
        "imb_l1_mean", "imb_l1_n", "imb_l5_mean", "imb_l5_n", "imb_tot_mean", "imb_tot_n",
        "n_zero_denom", "bid5_start", "bid5_end", "ask5_start", "ask5_end",
        "bid5_delta", "ask5_delta", "empty_lvl4plus_mean",
    ]
    al_fields = [
        "row_type", "day", "date", "csat", "session", "n_ticks", "n_hoga", "matched",
        "unmatched", "skipped_lag_gt_1s", "dt_p50", "dt_p90", "dt_p99", "dt_mean",
        "frac_le_0_1s", "frac_le_1s", "hist_bin", "hist_n", "imb_q", "imb_q_n",
        "mean_ret_1s_bps", "n_ret_1s", "mean_ret_5s_bps", "n_ret_5s",
        "pre_bid5_delta_mean", "pre_ask5_delta_mean", "note",
    ]

    f_time = open(os.path.join(OUT, "TIME_BUCKET_ANALYSIS.csv"), "w", encoding="utf-8", newline="")
    f_ob = open(os.path.join(OUT, "ORDERBOOK_ANALYSIS.csv"), "w", encoding="utf-8", newline="")
    f_al = open(os.path.join(OUT, "TICK_ORDERBOOK_ALIGNMENT.csv"), "w", encoding="utf-8", newline="")
    w_time = csv.DictWriter(f_time, fieldnames=time_fields)
    w_ob = csv.DictWriter(f_ob, fieldnames=ob_fields)
    w_al = csv.DictWriter(f_al, fieldnames=al_fields)
    w_time.writeheader()
    w_ob.writeheader()
    w_al.writeheader()

    summaries: Dict[str, Any] = {}
    emp_signs: Dict[str, Any] = {}
    header_tick = header_hoga = None

    try:
        for spec in DAYS:
            day_sum: Dict[str, Any] = {"csat": spec["csat"], "date": spec["date"]}
            for sess in ("continuous", "closing_auction"):
                tp = os.path.join(V2, spec["folder"], sess, "tick.csv")
                hp = os.path.join(V2, spec["folder"], sess, "orderbook.csv")
                if header_tick is None:
                    with open(tp, encoding="utf-8", newline="") as fh:
                        header_tick = next(csv.reader(fh))
                    with open(hp, encoding="utf-8", newline="") as fh:
                        header_hoga = next(csv.reader(fh))
                ticks = load_ticks(tp)
                hoga = load_hoga(hp)
                print(spec["id"], sess, "ticks", len(ticks), "hoga", len(hoga), flush=True)
                if sess == "continuous":
                    emp_signs[spec["id"]] = {
                        "plus": sum(1 for x in ticks if x["s15"] == "plus"),
                        "minus": sum(1 for x in ticks if x["s15"] == "minus"),
                        "unsigned": sum(1 for x in ticks if x["s15"] == "unsigned"),
                    }
                for sec in (60, 300):
                    for b in tick_buckets(ticks, sec):
                        row = {"day": spec["id"], "date": spec["date"], "csat": spec["csat"], "session": sess, "bucket_sec": sec}
                        row.update(b)
                        w_time.writerow(row)
                    for b in hoga_buckets(hoga, sec):
                        row = {"day": spec["id"], "date": spec["date"], "csat": spec["csat"], "session": sess, "bucket_sec": sec}
                        row.update(b)
                        w_ob.writerow(row)
                al = align(ticks, hoga)
                day_sum[sess] = {
                    "n_ticks": len(ticks),
                    "n_hoga": len(hoga),
                    "vol_sum": sum(x["vol"] for x in ticks),
                    "px_open": ticks[0]["px"] if ticks else None,
                    "px_close": ticks[-1]["px"] if ticks else None,
                    "strength_mean": (
                        round(sum(x["st"] for x in ticks if x["st"] is not None) / max(1, sum(1 for x in ticks if x["st"] is not None)), 6)
                        if ticks
                        else None
                    ),
                    "vol_1min_std_bps": vol_from_1min(tick_buckets(ticks, 60)) if ticks else None,
                    "spread_mean": None,
                    "imb_l5_mean": None,
                    "align": al,
                    "hoga_repeat": hoga_repeat_stats(hoga),
                }
                if hoga:
                    spreads = [h["ask1p"] - h["bid1p"] for h in hoga if h["ask1p"] and h["bid1p"]]
                    imbs = [imb(h["bid5"], h["ask5"]) for h in hoga]
                    imbs = [x for x in imbs if x is not None]
                    day_sum[sess]["spread_mean"] = None if not spreads else round(sum(spreads) / len(spreads), 6)
                    day_sum[sess]["imb_l5_mean"] = None if not imbs else round(sum(imbs) / len(imbs), 6)
                w_al.writerow(
                    {
                        "row_type": "match_summary",
                        "day": spec["id"],
                        "date": spec["date"],
                        "csat": spec["csat"],
                        "session": sess,
                        "n_ticks": al.get("n_ticks"),
                        "n_hoga": al.get("n_hoga"),
                        "matched": al.get("matched"),
                        "unmatched": al.get("unmatched"),
                        "skipped_lag_gt_1s": al.get("skipped_lag_gt_1s"),
                        "dt_p50": (al.get("dt") or {}).get("p50"),
                        "dt_p90": (al.get("dt") or {}).get("p90"),
                        "dt_p99": (al.get("dt") or {}).get("p99"),
                        "dt_mean": (al.get("dt") or {}).get("mean"),
                        "frac_le_0_1s": (al.get("dt") or {}).get("frac_le_0_1"),
                        "frac_le_1s": (al.get("dt") or {}).get("frac_le_1"),
                        "pre_bid5_delta_mean": al.get("pre_trade_bid5_delta_mean"),
                        "pre_ask5_delta_mean": al.get("pre_trade_ask5_delta_mean"),
                        "note": al.get("align_rule"),
                    }
                )
                for lab, cnt in (al.get("hist") or {}).items():
                    w_al.writerow(
                        {
                            "row_type": "dt_hist",
                            "day": spec["id"],
                            "date": spec["date"],
                            "csat": spec["csat"],
                            "session": sess,
                            "hist_bin": lab,
                            "hist_n": cnt,
                        }
                    )
                for q in al.get("quintiles") or []:
                    w_al.writerow(
                        {
                            "row_type": "imb_l5_vs_fwd_return",
                            "day": spec["id"],
                            "date": spec["date"],
                            "csat": spec["csat"],
                            "session": sess,
                            "imb_q": q["q"],
                            "imb_q_n": q["n"],
                            "mean_ret_1s_bps": q["mean_ret_1s_bps"],
                            "n_ret_1s": q["n_ret_1s"],
                            "mean_ret_5s_bps": q["mean_ret_5s_bps"],
                            "n_ret_5s": q["n_ret_5s"],
                            "note": "observational only; not a trading signal. lag<=1s, last quote before tick",
                        }
                    )
            summaries[spec["id"]] = day_sum
    finally:
        f_time.close()
        f_ob.close()
        f_al.close()

    d5_cont = os.path.join(V2, "D5_20261120", "continuous", "tick.csv")
    dup_v2 = investigate_dup(d5_cont)
    dup_orig = investigate_dup(D5_ORIG_TICK)
    dup_hoga_v2 = summaries["D5"]["continuous"]["hoga_repeat"]

    write_dict(os.path.join(OUT, "DATA_DICTIONARY.md"), header_tick or [], header_hoga or [], emp_signs)
    write_dup(os.path.join(OUT, "DUP_SUSPECT_ANALYSIS.md"), dup_orig, dup_v2, dup_hoga_v2)
    write_report(os.path.join(OUT, "STAGE32B_ANALYSIS_REPORT.md"), summaries, emp_signs, dup_orig, dup_v2)

    unchanged = True
    for p, h in v2_hashes.items():
        if sha256_file(p) != h:
            unchanged = False
            print("V2_CHANGED", p, flush=True)
    for p, h in orig_hashes.items():
        if sha256_file(p) != h:
            unchanged = False
            print("ORIG_CHANGED", p, flush=True)
    print("unchanged", unchanged, flush=True)
    return 0 if unchanged else 2


def write_dup(path: str, orig: Dict[str, Any], v2: Dict[str, Any], hoga: Dict[str, Any]) -> None:
    lines = [
        "# D5 dup_suspect 조사",
        "",
        "stats 값 `dup_suspect=175,682` 는 확정 중복 건수가 아니다. 행을 삭제·수정하지 않았다.",
        "",
        "## 수집 코드 판정",
        "",
        "`scalp/collector/tick_collector.py` 는 체결 콜백마다 지문 `(code, real_type, fid20_raw, fid15_raw, fid10_raw)` 를 만든다.",
        "최근 256개(`dup_window=256`) 안에 같은 지문이 있으면 `dup_suspect` 만 1 증가하고 **그래도 CSV에 기록한다**.",
        "recv_iso, seq, fid13, fid228 은 지문에 없다. 호가 콜백은 dup_suspect를 올리지 않는다.",
        "fid20 은 HHMMSS **초 단위**라서, 같은 초에 같은 가격·같은 수량이 여러 번 찍히면 정상적으로도 카운트가 올라간다.",
        "",
        "## 재현 (읽기 전용)",
        "",
        "| 항목 | 원본 세션 tick 전체 | 3.2A 연속매매 tick |",
        "|---|---:|---:|",
        "| 행 수 | %s | %s |" % (orig["rows"], v2["rows"]),
        "| 지문 재현 dup_suspect | %s | %s |" % (orig["dup_suspect_replay"], v2["dup_suspect_replay"]),
        "| 직전 행과 같은 fid20 인 의심 | %s | %s |" % (orig["suspect_same_fid20_as_prev"], v2["suspect_same_fid20_as_prev"]),
        "| 같은 지문+같은 recv_iso | %s | %s |" % (orig["suspect_same_fp_same_recv_iso"], v2["suspect_same_fp_same_recv_iso"]),
        "| 같은 지문+다른 recv_iso | %s | %s |" % (orig["suspect_same_fp_diff_recv_iso"], v2["suspect_same_fp_diff_recv_iso"]),
        "| 연속 행 동일 지문 | %s | %s |" % (orig["consecutive_identical_fp"], v2["consecutive_identical_fp"]),
        "| 비즈니스키(10/13/15/20/228) 반복 초과분 | %s | %s |" % (orig["business_key_repeat_extra"], v2["business_key_repeat_extra"]),
        "| 고유 fid20 초 | %s | %s |" % (orig["unique_fid20"], v2["unique_fid20"]),
        "| 초당 최대 체결 행 | %s | %s |" % (orig["max_prints_per_fid20"], v2["max_prints_per_fid20"]),
        "| 초당 평균 체결 행 | %s | %s |" % (orig["mean_prints_per_fid20"], v2["mean_prints_per_fid20"]),
        "",
        (
            "원본 재현 %s건은 stats 175,682와 같다. 호가는 지문에 없고 체결만 센다."
            % orig["dup_suspect_replay"]
            if orig["dup_suspect_replay"] == 175682
            else "원본 재현 %s건, stats는 175,682. 차이는 창 상태·호가 혼입이 아니라 재현 범위 확인이 필요하다."
            % orig["dup_suspect_replay"]
        ),
        "",
        "## 분류",
        "",
        "1. **정상 반복 가능**: 같은 fid20 초 안의 연속 체결. 거래소 시각이 초 단위라 가격·수량이 같으면 지문이 같다.",
        "2. **내용 동일·수신시각 다름**: 같은 지문이 다른 recv_iso 로 다시 보임. 콜백 재진입 또는 같은 체결 내용의 연속 출력 가능. 삭제하지 않음.",
        "3. **완전 동일 행(recv_iso까지 같음)**: 아래 표의 same recv_iso 건. 있어도 seq는 다름.",
        "4. **호가 스냅샷 반복**: 연속매매 호가에서 직전과 호가·잔량이 같은 행 %s / %s (%.3f%%). 잔량이 안 바뀌면 정상일 수 있다."
        % (hoga.get("consecutive_identical_snapshot"), hoga.get("rows"), hoga.get("pct") or 0),
        "",
        "seq가 다른 것은 전역 카운터라 당연하다. seq 차이만으로 중복이 아니다.",
        "이번 단계에서 어떤 행도 제거하지 않았다.",
        "",
    ]
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines))


def write_report(path: str, summaries: Dict[str, Any], signs: Dict[str, Any], dup_orig: Dict[str, Any], dup_v2: Dict[str, Any]) -> None:
    def fint(n: Any) -> str:
        return "-" if n is None else format(int(n), ",")

    def fflt(n: Any, d: int = 3) -> str:
        return "-" if n is None else format(float(n), ".%df" % d)

    lines = [
        "# Stage3.2B 삼성전자 5일 체결·호가 패턴 분석",
        "",
        "입력: `data/scalp/analysis/stage31_5day_v2/` 만 분석에 사용.",
        "D5 원본 tick은 dup_suspect 재현에만 읽기 전용.",
        "3.2A 파일·아카이브 원본 미수정. 주문·OCX·전략·학습 없음. 매매 신호 없음.",
        "",
        "## 공식",
        "",
        "- 가격 = abs(FID10). 부호는 등락이지 매수/매도가 아니다.",
        "- 체결량 = abs(FID15). `+`/`-`/무부호는 추정 표시만.",
        "- 1분 수익률(bps) = 10000 * (분봉종가 - 분봉시가) / 분봉시가",
        "- 변동성 = 해당 세션 1분 수익률의 표본 표준편차(bps)",
        "- 스프레드 = abs(매도1) - abs(매수1), 둘 다 >0",
        "- mid = (abs(매도1)+abs(매수1))/2",
        "- OBI = (bid_depth - ask_depth) / (bid_depth + ask_depth), 분모 0 제외",
        "- L1 depth = 1호가 수량, L5 = 1~5호가 수량 합, TOT = FID125/121",
        "- 체결-호가 연결: **마지막 호가 recv_iso ≤ 체결 recv_iso**. 미래 호가 사용 금지.",
        "- 관계 분석 허용 지연: 0 ≤ Δt ≤ **1.0초**. 그보다 긴 연결은 집계만 하고 관계 분석에서 제외.",
        "- 1초/5초 전방 수익률: 현재 체결 이후, 시각 ≤ t+h 인 마지막 체결 가격. 표본 없으면 제외.",
        "",
        "## 일반 거래일 연속매매 비교 (D1·D2·D3·D5)",
        "",
        "| 항목 | D1 | D2 | D3 | D5 |",
        "|---|---:|---:|---:|---:|",
    ]
    keys = ("D1", "D2", "D3", "D5")
    def col(fn):
        return " | ".join(fn(k) for k in keys)

    lines.append("| 체결 건 | %s |" % col(lambda k: fint(summaries[k]["continuous"]["n_ticks"])))
    lines.append("| 호가 건 | %s |" % col(lambda k: fint(summaries[k]["continuous"]["n_hoga"])))
    lines.append("| 체결량 합 | %s |" % col(lambda k: fint(summaries[k]["continuous"]["vol_sum"])))
    lines.append("| 시가→종가 | %s |" % col(lambda k: "%s→%s" % (fint(summaries[k]["continuous"]["px_open"]), fint(summaries[k]["continuous"]["px_close"]))))
    lines.append("| 체결강도 평균 | %s |" % col(lambda k: fflt(summaries[k]["continuous"]["strength_mean"])))
    lines.append("| 1분 수익률 표준편차(bps) | %s |" % col(lambda k: fflt(summaries[k]["continuous"]["vol_1min_std_bps"])))
    lines.append("| 평균 스프레드(원) | %s |" % col(lambda k: fflt(summaries[k]["continuous"]["spread_mean"], 2)))
    lines.append("| L5 잔량 불균형 평균 | %s |" % col(lambda k: fflt(summaries[k]["continuous"]["imb_l5_mean"], 4)))
    lines.append("| 호가 매칭 건 | %s |" % col(lambda k: fint(summaries[k]["continuous"]["align"]["matched"])))
    lines.append("| 호가 미매칭 | %s |" % col(lambda k: fint(summaries[k]["continuous"]["align"]["unmatched"])))
    lines.append("| Δt p50(초) | %s |" % col(lambda k: fflt(summaries[k]["continuous"]["align"]["dt"]["p50"], 4)))
    lines.append("| Δt≤0.1초 비율 | %s |" % col(lambda k: fflt((summaries[k]["continuous"]["align"]["dt"]["frac_le_0_1"] or 0) * 100, 1) + "%"))
    lines.append("| 체결 직전 bid5 변화 평균 | %s |" % col(lambda k: fflt(summaries[k]["continuous"]["align"]["pre_trade_bid5_delta_mean"], 2)))
    lines.append("| 체결 직전 ask5 변화 평균 | %s |" % col(lambda k: fflt(summaries[k]["continuous"]["align"]["pre_trade_ask5_delta_mean"], 2)))
    lines += [
        "",
        "시간대별 1분/5분 수치는 `TIME_BUCKET_ANALYSIS.csv`, 호가는 `ORDERBOOK_ANALYSIS.csv`.",
        "",
        "## D4 수능일 (별도)",
        "",
    ]
    d4 = summaries["D4"]["continuous"]
    lines += [
        "| 항목 | D4 10:00~16:20 |",
        "|---|---:|",
        "| 체결 건 | %s |" % fint(d4["n_ticks"]),
        "| 호가 건 | %s |" % fint(d4["n_hoga"]),
        "| 체결량 합 | %s |" % fint(d4["vol_sum"]),
        "| 시가→종가 | %s→%s |" % (fint(d4["px_open"]), fint(d4["px_close"])),
        "| 체결강도 평균 | %s |" % fflt(d4["strength_mean"]),
        "| 1분 수익률 표준편차(bps) | %s |" % fflt(d4["vol_1min_std_bps"]),
        "| 평균 스프레드 | %s |" % fflt(d4["spread_mean"], 2),
        "| L5 불균형 | %s |" % fflt(d4["imb_l5_mean"], 4),
        "| Δt p50 | %s |" % fflt(d4["align"]["dt"]["p50"], 4),
        "",
        "D4를 D1·D2·D3·D5와 같은 09:00축으로 합치지 않았다.",
        "",
        "## 종가 단일가",
        "",
        "| 날짜 | tick | hoga | 비고 |",
        "|---|---:|---:|---|",
    ]
    for k, spec in (("D1", ""), ("D2", ""), ("D3", ""), ("D4", " 16:20~16:30"), ("D5", " 15:29:54 1건")):
        s = summaries[k]["closing_auction"]
        lines.append("| %s | %s | %s | %s |" % (k, fint(s["n_ticks"]), fint(s["n_hoga"]), spec or "체결 0"))
    lines += [
        "",
        "종가 단일가 체결은 D5 1건뿐이라 분봉·전방수익률 비교 표본이 없다. 호가만 별도 CSV 세션=`closing_auction`.",
        "",
        "## 체결-호가 연결",
        "",
        "미래 호가를 과거 체결에 붙이지 않았다. 세션 첫 체결 전에 호가가 없으면 unmatched.",
        "Δt 분포는 `TICK_ORDERBOOK_ALIGNMENT.csv` 의 `dt_hist`. 관계 분석은 Δt≤1초만.",
        "L5 불균형 오분위 vs 이후 1초·5초 수익률은 관찰값이며 신호가 아니다.",
        "",
        "## D5 dup_suspect",
        "",
        "원본 재현 %s / stats 175,682. 상세는 `DUP_SUSPECT_ANALYSIS.md`." % fint(dup_orig["dup_suspect_replay"]),
        "3.2A 연속매매 재현 %s / 행 %s. 삭제하지 않음." % (fint(dup_v2["dup_suspect_replay"]), fint(dup_v2["rows"])),
        "",
        "## 산출 파일",
        "",
        "- DATA_DICTIONARY.md",
        "- TIME_BUCKET_ANALYSIS.csv",
        "- ORDERBOOK_ANALYSIS.csv",
        "- TICK_ORDERBOOK_ALIGNMENT.csv",
        "- DUP_SUSPECT_ANALYSIS.md",
        "- STAGE32B_ANALYSIS_REPORT.md",
        "",
        "Stage3.2B 이후 전략·학습은 이 작업에서 하지 않았다.",
        "",
    ]
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines))


if __name__ == "__main__":
    raise SystemExit(main())
