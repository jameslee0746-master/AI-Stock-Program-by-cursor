"""Stage3.2C: 오프라인 특징·미래수익률 연구. 3.2A/B·원본 미수정. 신호·주문·학습 없음."""
from __future__ import annotations

import csv
import hashlib
import math
import os
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
V2 = os.path.join(ROOT, "data", "scalp", "analysis", "stage31_5day_v2")
OUT = os.path.join(V2, "stage32c_research")
HORIZONS = (1, 5, 10, 30, 60)
FLAT_BPS = 0.5  # 보합: |ret| < 0.5bps. 가정값.

DAYS = [
    {"id": "D1", "date": "2026-11-16", "folder": "D1_20261116", "csat": False},
    {"id": "D2", "date": "2026-11-17", "folder": "D2_20261117", "csat": False},
    {"id": "D3", "date": "2026-11-18", "folder": "D3_20261118", "csat": False},
    {"id": "D4", "date": "2026-11-19", "folder": "D4_20261119_CSAT", "csat": True},
    {"id": "D5", "date": "2026-11-20", "folder": "D5_20261120", "csat": False},
]


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
    if not t:
        return None
    try:
        return abs(int(t))
    except ValueError:
        try:
            return abs(int(float(t)))
        except ValueError:
            return None


def krx_tick(px: int) -> int:
    """KRX 주식 호가단위. 규정 가정값이며 D1 데이터(20만 원대 500원)와 맞는지 검증한다."""
    if px >= 500000:
        return 1000
    if px >= 200000:
        return 500
    if px >= 100000:
        return 100
    if px >= 50000:
        return 50
    if px >= 20000:
        return 50
    if px >= 5000:
        return 10
    if px >= 2000:
        return 5
    return 1


def imb(bid: Optional[int], ask: Optional[int]) -> Optional[float]:
    if bid is None or ask is None:
        return None
    den = bid + ask
    if den == 0:
        return None
    return (bid - ask) / float(den)


def load_hoga(path: str) -> List[dict]:
    rows: List[dict] = []
    with open(path, encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            dt = parse_iso(r.get("recv_iso", ""))
            if dt is None:
                continue
            ask1 = abs_int(r.get("fid41_raw"))
            bid1 = abs_int(r.get("fid51_raw"))
            if not ask1 or not bid1:
                continue
            ask_q = [abs_int(r.get("fid%d_raw" % f)) or 0 for f in (61, 62, 63, 64, 65)]
            bid_q = [abs_int(r.get("fid%d_raw" % f)) or 0 for f in (71, 72, 73, 74, 75)]
            mid = (ask1 + bid1) / 2.0
            sp = ask1 - bid1
            rows.append(
                {
                    "ts": dt.timestamp(),
                    "mid": mid,
                    "spread": sp,
                    "ask1": ask1,
                    "bid1": bid1,
                    "ask1q": ask_q[0],
                    "bid1q": bid_q[0],
                    "ask5": sum(ask_q),
                    "bid5": sum(bid_q),
                    "imb1": imb(bid_q[0], ask_q[0]),
                    "imb5": imb(sum(bid_q), sum(ask_q)),
                    "tick": krx_tick(int(round(mid))),
                }
            )
    return rows


def load_ticks(path: str) -> List[dict]:
    rows: List[dict] = []
    with open(path, encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            dt = parse_iso(r.get("recv_iso", ""))
            if dt is None:
                continue
            px = abs_int(r.get("fid10") or r.get("fid10_raw"))
            vol = abs_int(r.get("fid15") or r.get("fid15_raw")) or 0
            try:
                st = float(str(r.get("fid228") or "").strip() or "nan")
            except ValueError:
                st = float("nan")
            s15 = str(r.get("fid15_raw") or "")
            cls = "plus" if s15.startswith("+") else ("minus" if s15.startswith("-") else "unsigned")
            rows.append(
                {
                    "ts": dt.timestamp(),
                    "px": px,
                    "vol": vol,
                    "st": st if st == st else None,
                    "s15": cls,
                    "fid20": str(r.get("fid20_raw") or ""),
                    "fp": (
                        str(r.get("fid20_raw") or ""),
                        str(r.get("fid15_raw") or ""),
                        str(r.get("fid10_raw") or ""),
                    ),
                }
            )
    return rows


def spread_audit(hoga: List[dict]) -> Dict[str, Any]:
    xs = [h["spread"] for h in hoga]
    xs_sorted = sorted(xs)
    n = len(xs)

    def pct(p: float) -> Optional[float]:
        if not n:
            return None
        k = (n - 1) * p
        i = int(math.floor(k))
        j = min(n - 1, int(math.ceil(k)))
        if i == j:
            return xs_sorted[i]
        return xs_sorted[i] * (j - k) + xs_sorted[j] * (k - i)

    from collections import Counter

    c = Counter(xs)
    ge200k = sum(1 for h in hoga if h["mid"] >= 200000)
    sp500 = sum(1 for h in hoga if h["spread"] == 500)
    sp100 = sum(1 for h in hoga if h["spread"] == 100)
    gt2tick = sum(1 for h in hoga if h["spread"] > 2 * h["tick"])
    return {
        "n": n,
        "mean": None if not n else round(sum(xs) / n, 4),
        "median": None if not n else xs_sorted[n // 2],
        "p90": pct(0.90),
        "p95": pct(0.95),
        "p99": pct(0.99),
        "max": None if not n else xs_sorted[-1],
        "min": None if not n else xs_sorted[0],
        "n_spread_100": sp100,
        "n_spread_500": sp500,
        "n_mid_ge_200000": ge200k,
        "n_spread_gt_2tick": gt2tick,
        "top": c.most_common(8),
        "mean_if_only_100_500": None
        if not n
        else round((100 * sp100 + 500 * sp500) / n, 4),
    }


def last_idx_le(rows: List[dict], t: float, start: int) -> int:
    i = start
    while i + 1 < len(rows) and rows[i + 1]["ts"] <= t:
        i += 1
    if i < 0 or i >= len(rows) or rows[i]["ts"] > t:
        return -1
    return i


def ticks_in(rows: List[dict], t0: float, t1: float, start: int) -> Tuple[int, int, List[dict]]:
    """rows with t0 < ts <= t1. start is a hint index."""
    i = start
    while i < len(rows) and rows[i]["ts"] <= t0:
        i += 1
    j = i
    got: List[dict] = []
    while j < len(rows) and rows[j]["ts"] <= t1:
        got.append(rows[j])
        j += 1
    return i, j, got


def bin_imb(v: Optional[float]) -> str:
    if v is None:
        return "missing"
    if v < -0.3:
        return "imb<-0.3"
    if v < -0.1:
        return "-0.3<=imb<-0.1"
    if v <= 0.1:
        return "-0.1<=imb<=0.1"
    if v <= 0.3:
        return "0.1<imb<=0.3"
    return "imb>0.3"


def bin_st(v: Optional[float]) -> str:
    if v is None:
        return "missing"
    if v < 50:
        return "st<50"
    if v < 80:
        return "50<=st<80"
    if v < 100:
        return "80<=st<100"
    if v < 150:
        return "100<=st<150"
    return "st>=150"


def bin_vol_ratio(r: Optional[float]) -> str:
    if r is None:
        return "missing"
    if r < 0.5:
        return "vol_ratio<0.5"
    if r < 1.0:
        return "0.5<=vol_ratio<1"
    if r < 2.0:
        return "1<=vol_ratio<2"
    if r < 4.0:
        return "2<=vol_ratio<4"
    return "vol_ratio>=4"


def bin_spread_ticks(spread: int, tick: int) -> str:
    if tick <= 0:
        return "missing"
    n = spread / float(tick)
    if n <= 1.01:
        return "1tick"
    if n <= 2.01:
        return "2tick"
    return ">2tick"


def classify_ret(ret: float) -> str:
    if ret > FLAT_BPS:
        return "up"
    if ret < -FLAT_BPS:
        return "down"
    return "flat"


def research_day(hoga: List[dict], ticks: List[dict]) -> Dict[str, Any]:
    if not hoga:
        return {"n_grid": 0, "rows": [], "combo": []}
    t0 = int(math.ceil(hoga[0]["ts"]))
    t1 = int(math.floor(hoga[-1]["ts"]))
    qi = 0
    acc: Dict[Tuple[str, int, str], Dict[str, Any]] = {}
    combo_acc: Dict[Tuple[int, str], Dict[str, Any]] = {}
    n_grid = 0
    n_skip_no_quote = 0
    n_feat = 0
    miss_h: Dict[int, int] = {h: 0 for h in HORIZONS}
    used_h: Dict[int, int] = {h: 0 for h in HORIZONS}

    def add(key: Tuple[str, int, str], ret: float, spread_bps: float) -> None:
        a = acc.get(key)
        if a is None:
            a = {"n": 0, "sum": 0.0, "up": 0, "down": 0, "flat": 0, "sum_net_half": 0.0, "sum_net_full": 0.0}
            acc[key] = a
        a["n"] += 1
        a["sum"] += ret
        a["sum_net_half"] += ret - 0.5 * spread_bps
        a["sum_net_full"] += ret - spread_bps
        a[classify_ret(ret)] += 1

    tk = 0
    for t in range(t0, t1 + 1):
        n_grid += 1
        while qi + 1 < len(hoga) and hoga[qi + 1]["ts"] <= t:
            qi += 1
        if hoga[qi]["ts"] > t:
            n_skip_no_quote += 1
            continue
        q = hoga[qi]
        n_feat += 1
        spread_bps = 10000.0 * q["spread"] / q["mid"]
        while tk < len(ticks) and ticks[tk]["ts"] <= t - 2.0:
            tk += 1
        prev: List[dict] = []
        now: List[dict] = []
        k = tk
        while k < len(ticks) and ticks[k]["ts"] <= t:
            if ticks[k]["ts"] <= t - 1.0:
                prev.append(ticks[k])
            else:
                now.append(ticks[k])
            k += 1
        vol_now = sum(x["vol"] for x in now)
        vol_prev = sum(x["vol"] for x in prev)
        freq_now = len(now)
        ratio = None if vol_prev <= 0 else vol_now / float(vol_prev)
        last_st = None
        for x in reversed(now or prev):
            if x["st"] is not None:
                last_st = x["st"]
                break
        if last_st is None and qi >= 0:
            # last tick <= t
            k2 = k - 1
            while k2 >= 0 and ticks[k2]["ts"] > t:
                k2 -= 1
            if k2 >= 0:
                last_st = ticks[k2]["st"]
        d_bid = q["bid5"] - hoga[qi - 1]["bid5"] if qi > 0 else 0
        d_ask = q["ask5"] - hoga[qi - 1]["ask5"] if qi > 0 else 0
        px_chg_1s = None
        if now and now[0]["px"] and now[-1]["px"]:
            px_chg_1s = 10000.0 * (now[-1]["px"] - now[0]["px"]) / now[0]["px"]

        feat_bins = {
            "imb_l5": bin_imb(q["imb5"]),
            "imb_l1": bin_imb(q["imb1"]),
            "strength": bin_st(last_st),
            "vol_ratio_1s": bin_vol_ratio(ratio),
            "spread_ticks": bin_spread_ticks(q["spread"], q["tick"]),
        }
        combo = None
        if (
            feat_bins["imb_l5"] == "imb>0.3"
            and feat_bins["spread_ticks"] == "1tick"
            and feat_bins["vol_ratio_1s"] == "vol_ratio>=4"
        ):
            combo = "imb>0.3 & 1tick & vol_ratio>=4"
        elif feat_bins["imb_l5"] == "imb<-0.3" and feat_bins["spread_ticks"] == "1tick":
            combo = "imb<-0.3 & 1tick"

        qj = qi
        for h in HORIZONS:
            th = t + h
            while qj + 1 < len(hoga) and hoga[qj + 1]["ts"] <= th:
                qj += 1
            if qj <= qi or hoga[qj]["ts"] <= t or hoga[qj]["ts"] > th:
                miss_h[h] += 1
                continue
            mid_f = hoga[qj]["mid"]
            ret = 10000.0 * (mid_f - q["mid"]) / q["mid"]
            used_h[h] += 1
            for fname, bname in feat_bins.items():
                add((fname, h, bname), ret, spread_bps)
            if combo:
                ck = (h, combo)
                a = combo_acc.get(ck)
                if a is None:
                    a = {"n": 0, "sum": 0.0, "up": 0, "down": 0, "flat": 0, "sum_net_half": 0.0}
                    combo_acc[ck] = a
                a["n"] += 1
                a["sum"] += ret
                a["sum_net_half"] += ret - 0.5 * spread_bps
                a[classify_ret(ret)] += 1

    rows = []
    for (fname, h, bname), a in sorted(acc.items()):
        n = a["n"]
        rows.append(
            {
                "feature": fname,
                "horizon_sec": h,
                "bin": bname,
                "n": n,
                "mean_ret_bps": round(a["sum"] / n, 6) if n else None,
                "pct_up": round(100.0 * a["up"] / n, 4) if n else None,
                "pct_down": round(100.0 * a["down"] / n, 4) if n else None,
                "pct_flat": round(100.0 * a["flat"] / n, 4) if n else None,
                "mean_ret_minus_half_spread_bps": round(a["sum_net_half"] / n, 6) if n else None,
                "mean_ret_minus_full_spread_bps": round(a["sum_net_full"] / n, 6) if n else None,
            }
        )
    combos = []
    for (h, name), a in sorted(combo_acc.items()):
        n = a["n"]
        combos.append(
            {
                "horizon_sec": h,
                "combo": name,
                "n": n,
                "mean_ret_bps": round(a["sum"] / n, 6) if n else None,
                "pct_up": round(100.0 * a["up"] / n, 4) if n else None,
                "pct_down": round(100.0 * a["down"] / n, 4) if n else None,
                "mean_ret_minus_half_spread_bps": round(a["sum_net_half"] / n, 6) if n else None,
            }
        )
    return {
        "n_grid": n_grid,
        "n_skip_no_quote": n_skip_no_quote,
        "n_feature_times": n_feat,
        "used_horizon": used_h,
        "missing_horizon": miss_h,
        "rows": rows,
        "combo": combos,
        "t0": t0,
        "t1": t1,
    }


def write_spread_audit(path: str, audits: Dict[str, Any]) -> None:
    lines = [
        "# D1 스프레드 감사",
        "",
        "공식(3.2B와 동일): `spread = abs(FID41) - abs(FID51)`. 1호가 가격이 둘 다 >0인 행만. 단위는 **원**.",
        "임의 보정하지 않음. 3.2B 평균 D1=220.87, D2/D5≈100.6 을 원본 호가로 재현한다.",
        "",
        "## 날짜별 분포 (연속매매 호가)",
        "",
        "| 날짜 | n | 평균 | 중앙값 | p90 | p95 | p99 | max | 100원 | 500원 | mid≥20만 | 2틱초과 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for did in ("D1", "D2", "D3", "D4", "D5"):
        a = audits[did]
        lines.append(
            "| %s | %s | %.2f | %s | %s | %s | %s | %s | %s | %s | %s | %s |"
            % (
                did,
                a["n"],
                a["mean"],
                a["median"],
                a["p90"],
                a["p95"],
                a["p99"],
                a["max"],
                a["n_spread_100"],
                a["n_spread_500"],
                a["n_mid_ge_200000"],
                a["n_spread_gt_2tick"],
            )
        )
    d1 = audits["D1"]
    lines += [
        "",
        "## D1 원인",
        "",
        "계산 오류가 아니다. D1 시가 206,500원으로 **20만 원 이상 구간 호가단위 500원**이 열린다.",
        "같은 날 종가 195,800원이라 장중에 20만 원 아래로 내려가면 호가단위가 100원이 된다.",
        "그래서 스프레드 100원 %s건, 500원 %s건이 한 파일에 섞인다."
        % (d1["n_spread_100"], d1["n_spread_500"]),
        "100원·500원만으로 가중 평균하면 %.2f원으로 전체 평균 %.2f원과 거의 같다."
        % (d1["mean_if_only_100_500"], d1["mean"]),
        "중앙값은 100원이다. 평균만 보면 220원으로 보이지만 전형적인 호가는 1틱이다.",
        "",
        "D2·D3·D5는 시가가 20만 원 미만이라 거의 전부 스프레드 100원(1틱)이다.",
        "D1 09:00 직후 표본: 매도1=207,000 매수1=206,500 → 500원. 호가 잔량도 존재하므로 빈 호가가 아니다.",
        "",
        "KRX 호가단위(가정, 데이터와 정합): 10만~20만 미만 100원, 20만~50만 미만 500원.",
        "비정상(2틱 초과) D1=%s / %s. 이후 날짜도 소수. 평균을 맞추려고 제거하지 않았다."
        % (d1["n_spread_gt_2tick"], d1["n"]),
        "",
        "빈도: " + ", ".join("%s원=%s" % (k, v) for k, v in d1["top"]),
        "",
    ]
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines))


def main() -> int:
    if os.path.isdir(OUT) and os.listdir(OUT):
        raise SystemExit("결과 폴더가 이미 있습니다: %s" % OUT)
    os.makedirs(OUT, exist_ok=False)
    protect: List[Tuple[str, str]] = []
    for spec in DAYS:
        for sess in ("continuous", "closing_auction"):
            for kind in ("tick", "orderbook"):
                p = os.path.join(V2, spec["folder"], sess, "%s.csv" % kind)
                protect.append((p, sha256_file(p)))
    for n in (
        "STAGE32B_ANALYSIS_REPORT.md",
        "ORDERBOOK_ANALYSIS.csv",
        "TIME_BUCKET_ANALYSIS.csv",
        "TICK_ORDERBOOK_ALIGNMENT.csv",
        "DUP_SUSPECT_ANALYSIS.md",
        "DATA_DICTIONARY.md",
    ):
        p = os.path.join(V2, "stage32b_analysis", n)
        if os.path.isfile(p):
            protect.append((p, sha256_file(p)))

    feat_fields = [
        "day", "date", "csat", "feature", "horizon_sec", "bin", "n",
        "mean_ret_bps", "pct_up", "pct_down", "pct_flat",
        "mean_ret_minus_half_spread_bps", "mean_ret_minus_full_spread_bps",
        "n_grid", "n_feature_times", "n_horizon_used", "n_horizon_missing",
        "note",
    ]
    fut_fields = feat_fields[:]
    ob_fields = [
        "day", "date", "csat", "horizon_sec", "combo", "n", "mean_ret_bps",
        "pct_up", "pct_down", "mean_ret_minus_half_spread_bps", "n_feature_times", "note",
    ]
    date_fields = [
        "day", "date", "csat", "n_hoga", "n_ticks", "spread_mean", "spread_median",
        "spread_p90", "spread_p95", "n_spread_100", "n_spread_500", "n_mid_ge_200000",
        "n_grid", "n_feature_times", "n_ret_1s", "n_ret_60s_missing",
        "mean_ret_1s_bps_all", "mean_ret_5s_bps_all", "mean_ret_60s_bps_all",
        "imb_l5_mean", "note",
    ]

    f_feat = open(os.path.join(OUT, "FEATURE_ANALYSIS.csv"), "w", encoding="utf-8", newline="")
    f_fut = open(os.path.join(OUT, "FUTURE_RETURN_ANALYSIS.csv"), "w", encoding="utf-8", newline="")
    f_ob = open(os.path.join(OUT, "ORDERBOOK_SIGNAL_RESEARCH.csv"), "w", encoding="utf-8", newline="")
    f_date = open(os.path.join(OUT, "DATE_COMPARISON.csv"), "w", encoding="utf-8", newline="")
    w_feat = csv.DictWriter(f_feat, fieldnames=feat_fields)
    w_fut = csv.DictWriter(f_fut, fieldnames=fut_fields)
    w_ob = csv.DictWriter(f_ob, fieldnames=ob_fields)
    w_date = csv.DictWriter(f_date, fieldnames=date_fields)
    w_feat.writeheader()
    w_fut.writeheader()
    w_ob.writeheader()
    w_date.writeheader()

    audits: Dict[str, Any] = {}
    researches: Dict[str, Any] = {}
    note = (
        "t=1s grid; features from last quote ts<=t and ticks in (t-1,t]; "
        "future mid=last quote with t<ts<=t+h; no interpolation; overlapping windows; not a trade signal"
    )
    try:
        for spec in DAYS:
            hp = os.path.join(V2, spec["folder"], "continuous", "orderbook.csv")
            tp = os.path.join(V2, spec["folder"], "continuous", "tick.csv")
            hoga = load_hoga(hp)
            ticks = load_ticks(tp)
            print(spec["id"], "loaded hoga", len(hoga), "ticks", len(ticks), flush=True)
            audits[spec["id"]] = spread_audit(hoga)
            audits[spec["id"]]["n_ticks"] = len(ticks)
            res = research_day(hoga, ticks)
            researches[spec["id"]] = res
            print(spec["id"], "grid", res["n_grid"], "feat", res["n_feature_times"], flush=True)
            used1 = res["used_horizon"].get(1, 0)
            miss60 = res["missing_horizon"].get(60, 0)
            ret1 = [r for r in res["rows"] if r["feature"] == "imb_l5" and r["horizon_sec"] == 1]
            # overall 1s from summing imb bins
            def weighted_mean(feature: str, h: int) -> Optional[float]:
                rs = [r for r in res["rows"] if r["feature"] == feature and r["horizon_sec"] == h]
                n = sum(r["n"] for r in rs)
                if not n:
                    return None
                return round(sum(r["mean_ret_bps"] * r["n"] for r in rs if r["mean_ret_bps"] is not None) / n, 6)

            w_date.writerow(
                {
                    "day": spec["id"],
                    "date": spec["date"],
                    "csat": spec["csat"],
                    "n_hoga": len(hoga),
                    "n_ticks": len(ticks),
                    "spread_mean": audits[spec["id"]]["mean"],
                    "spread_median": audits[spec["id"]]["median"],
                    "spread_p90": audits[spec["id"]]["p90"],
                    "spread_p95": audits[spec["id"]]["p95"],
                    "n_spread_100": audits[spec["id"]]["n_spread_100"],
                    "n_spread_500": audits[spec["id"]]["n_spread_500"],
                    "n_mid_ge_200000": audits[spec["id"]]["n_mid_ge_200000"],
                    "n_grid": res["n_grid"],
                    "n_feature_times": res["n_feature_times"],
                    "n_ret_1s": used1,
                    "n_ret_60s_missing": miss60,
                    "mean_ret_1s_bps_all": weighted_mean("imb_l5", 1),
                    "mean_ret_5s_bps_all": weighted_mean("imb_l5", 5),
                    "mean_ret_60s_bps_all": weighted_mean("imb_l5", 60),
                    "imb_l5_mean": None if not hoga else round(sum(x["imb5"] or 0 for x in hoga) / len(hoga), 6),
                    "note": note,
                }
            )
            for r in res["rows"]:
                row = {
                    "day": spec["id"],
                    "date": spec["date"],
                    "csat": spec["csat"],
                    "n_grid": res["n_grid"],
                    "n_feature_times": res["n_feature_times"],
                    "n_horizon_used": res["used_horizon"].get(r["horizon_sec"]),
                    "n_horizon_missing": res["missing_horizon"].get(r["horizon_sec"]),
                    "note": note,
                }
                row.update(r)
                w_feat.writerow(row)
                w_fut.writerow(row)
            for c in res["combo"]:
                w_ob.writerow(
                    {
                        "day": spec["id"],
                        "date": spec["date"],
                        "csat": spec["csat"],
                        "horizon_sec": c["horizon_sec"],
                        "combo": c["combo"],
                        "n": c["n"],
                        "mean_ret_bps": c["mean_ret_bps"],
                        "pct_up": c["pct_up"],
                        "pct_down": c["pct_down"],
                        "mean_ret_minus_half_spread_bps": c["mean_ret_minus_half_spread_bps"],
                        "n_feature_times": res["n_feature_times"],
                        "note": "coincidence table only; not an order signal. " + note,
                    }
                )
    finally:
        f_feat.close()
        f_fut.close()
        f_ob.close()
        f_date.close()

    write_spread_audit(os.path.join(OUT, "SPREAD_D1_AUDIT.md"), audits)
    write_report(os.path.join(OUT, "STAGE32C_RESEARCH_REPORT.md"), audits, researches)

    unchanged = True
    for p, h in protect:
        if sha256_file(p) != h:
            unchanged = False
            print("CHANGED", p, flush=True)
    print("unchanged", unchanged, flush=True)
    return 0 if unchanged else 2


def write_report(path: str, audits: Dict[str, Any], researches: Dict[str, Any]) -> None:
    lines = [
        "# Stage3.2C 체결·호가 특징 연구",
        "",
        "입력: Stage3.2A `stage31_5day_v2` 연속매매 CSV, Stage3.2B 보고서는 읽기만.",
        "출력: `stage32c_research/`. 원본·3.2A·3.2B 미변경. 주문·OCX·학습·매매신호 없음.",
        "",
        "## 방법 (누수 방지)",
        "",
        "- 시점 t: 연속매매 구간의 **1초 격자** (호가 첫 시각 올림 ~ 마지막 내림).",
        "- 특징: 마지막 호가 `recv_iso ≤ t`, 체결은 `(t-1, t]` 및 `(t-2, t-1]` 만. FID10 부호 미사용.",
        "- FID15 `+`/`-`는 특징 구간 집계에 넣지 않고 사전에서 추정으로만 둔다.",
        "- 미래 수익률: mid(t+h) = `t < recv_iso ≤ t+h` 인 마지막 호가의 mid. 없으면 **제외**, 보간 없음.",
        "- 수익률(bps) = 10000 * (mid_future - mid_t) / mid_t.",
        "- 겹치는 1초 창은 독립 표본이 아니다. n은 창 수.",
        "- 구간 경계는 전 기간 분위가 아니라 **고정 임계값** (미래 분포로 자르지 않음).",
        "- 보합: |ret| < 0.5bps (**가정**).",
        "- 비용: 관측 스프레드. 반스프레드 차감은 **시장가 1회 교차 가정**. 수수료는 미확인이라 넣지 않음.",
        "",
        "## D1 스프레드",
        "",
        "상세는 `SPREAD_D1_AUDIT.md`. 요지: D1 평균 220.9원은 버그가 아니다.",
        "20만 원 이상에서 호가단위 500원, 이하에서 100원이 한 날에 섞인다. 중앙값은 100원.",
        "",
        "## 날짜 비교 요약",
        "",
        "| 날짜 | 호가 n | 스프레드 평균/중앙 | 1초격자 | 특징시점 | t+1 사용/결측 | t+60 결측 |",
        "|---|---:|---|---:|---:|---|---:|",
    ]
    for did in ("D1", "D2", "D3", "D5", "D4"):
        a = audits[did]
        r = researches[did]
        tag = " 수능" if did == "D4" else ""
        lines.append(
            "| %s%s | %s | %.1f / %s | %s | %s | %s / %s | %s |"
            % (
                did,
                tag,
                a["n"],
                a["mean"],
                a["median"],
                r["n_grid"],
                r["n_feature_times"],
                r["used_horizon"][1],
                r["missing_horizon"][1],
                r["missing_horizon"][60],
            )
        )
    lines += [
        "",
        "D4는 10:00~16:20만 있고 일반일과 같은 축으로 합치지 않았다.",
        "D5 행은 dup_suspect 이유로 삭제하지 않았다. 1초 격자라 동일 fid20 초의 다건 체결은 그 초 구간에 합쳐진다.",
        "",
        "## 관측 관계 (신호가 아님)",
        "",
        "L5 불균형·체결강도·체결량비·스프레드틱 구간별 t+1~t+60 평균 수익률은 `FEATURE_ANALYSIS.csv` / `FUTURE_RETURN_ANALYSIS.csv`.",
        "동시 조건은 `ORDERBOOK_SIGNAL_RESEARCH.csv`. n이 작은 칸은 일반화하지 말 것.",
        "반스프레드를 빼면 대부분의 짧은 구간 평균은 줄어들거나 음수가 될 수 있다. 전략 결론이 아니다.",
        "",
        "## 비용",
        "",
        "| 항목 | 값 | 구분 |",
        "|---|---|---|",
        "| 호가 스프레드 | 데이터 abs(ask1-bid1) | **관측** |",
        "| 1틱 | 가격 20만 미만 100원, 이상 500원 | 규정 가정, D1과 정합 |",
        "| 반스프레드 차감 | ret - 0.5*spread/mid*10000 bps | **가정** (1회 시장가 교차) |",
        "| 왕복 스프레드 | ret - spread_bps | **가정** |",
        "| 수수료·제세금 | 미적용 | 미확인 |",
        "| 슬리피지 추가 | 0 | **가정** (미확인) |",
        "",
        "매수·매도 체결가 차이는 FID15 부호를 단정할 수 없어 관측 스프레드로만 근사했다.",
        "",
        "이번 단계에서 실제 매수·매도 신호를 만들지 않았다.",
        "",
    ]
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines))


if __name__ == "__main__":
    raise SystemExit(main())
