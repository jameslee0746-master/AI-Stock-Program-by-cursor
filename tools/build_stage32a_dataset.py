"""Stage3.2A: 원본 미수정, 분석 폴더에 정규장 필터 복사본만 만든다."""
from __future__ import annotations

import csv
import hashlib
import json
import os
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_ROOT = os.path.join(ROOT, "data", "scalp", "analysis", "stage31_5day_v2")
REPORT_DIR = os.path.join(OUT_ROOT, "reports")
CODE = "005930"
GAP_SEC = 60.0

DAYS = [
    {
        "id": "D1",
        "date": "2026-11-16",
        "folder": "D1_20261116",
        "session_id": "s2c-2ce377909284",
        "csat": False,
        "open_hm": 900,
        "auction_hm": 1520,
        "close_hm": 1530,
        "tick": os.path.join(ROOT, "data", "scalp", "archive", "20261116_073049_s2c-2ce377909284", "tick_005930_20261116_observe_part001.csv"),
        "orderbook": os.path.join(ROOT, "data", "scalp", "archive", "20261116_073049_s2c-2ce377909284", "orderbook_005930_20261116_observe_part001.csv"),
        "stats": os.path.join(ROOT, "data", "scalp", "archive", "20261116_073049_s2c-2ce377909284", "collector_stats.json"),
        "jsonl": os.path.join(ROOT, "data", "scalp", "archive", "20261116_073049_s2c-2ce377909284", "stage2c_login_only_20261116_073049_2ce377909284.jsonl"),
        "expect_sha": {
            "tick": "2e50066c6265e54b8825e5cf890e0a5f3b3d05b84e23f855a9a4f046622dfb4c",
            "orderbook": "5899ba61da0213a00938620f7b9b1ef36d076c67634d466a92da7c636924e5c6",
        },
    },
    {
        "id": "D2",
        "date": "2026-11-17",
        "folder": "D2_20261117",
        "session_id": "s2c-d0f21c502633",
        "csat": False,
        "open_hm": 900,
        "auction_hm": 1520,
        "close_hm": 1530,
        "tick": os.path.join(ROOT, "data", "scalp", "archive", "20261117_072934_s2c-d0f21c502633", "tick_005930_20261117_observe_part001.csv"),
        "orderbook": os.path.join(ROOT, "data", "scalp", "archive", "20261117_072934_s2c-d0f21c502633", "orderbook_005930_20261117_observe_part001.csv"),
        "stats": os.path.join(ROOT, "data", "scalp", "archive", "20261117_072934_s2c-d0f21c502633", "collector_stats.json"),
        "jsonl": os.path.join(ROOT, "data", "scalp", "archive", "20261117_072934_s2c-d0f21c502633", "stage2c_login_only_20261117_072934_d0f21c502633.jsonl"),
        "expect_sha": {
            "tick": "b024e13da6592c15a077995d8b5b26158ba448a7741d629712ce958da663b88d",
            "orderbook": "a6d8fddf27ade83e9bb2d3271cd7ae429bdb83f5c0a1773e20b0127288150430",
        },
    },
    {
        "id": "D3",
        "date": "2026-11-18",
        "folder": "D3_20261118",
        "session_id": "s2c-0f83412d71ea",
        "csat": False,
        "open_hm": 900,
        "auction_hm": 1520,
        "close_hm": 1530,
        "tick": os.path.join(ROOT, "data", "scalp", "tick_005930_20261118_observe_part001.csv"),
        "orderbook": os.path.join(ROOT, "data", "scalp", "orderbook_005930_20261118_observe_part001.csv"),
        "stats": None,
        "jsonl": os.path.join(ROOT, "logs", "stage2c_login_only_20261118_073219_0f83412d71ea.jsonl"),
        "dup_suspect_from_verify": 61623,
        "expect_sha": {
            "tick": "30489c50b945cd566de352ad3f7ff7727ebf52cd017bdf8705bbe109cabbbc59",
            "orderbook": "6f6ab689620f55db4bdafbb4ed7ae891464ac67bd898ed7478b230578377c769",
        },
    },
    {
        "id": "D4",
        "date": "2026-11-19",
        "folder": "D4_20261119_CSAT",
        "session_id": "s2c-e05ee8168f8c",
        "csat": True,
        "open_hm": 1000,
        "auction_hm": 1620,
        "close_hm": 1630,
        "tick": os.path.join(ROOT, "data", "scalp", "20261119", "tick_005930_20261119_observe_part001.csv"),
        "orderbook": os.path.join(ROOT, "data", "scalp", "20261119", "orderbook_005930_20261119_observe_part001.csv"),
        "tick_alt": os.path.join(ROOT, "data", "scalp", "tick_005930_20261119_observe_part001.csv"),
        "orderbook_alt": os.path.join(ROOT, "data", "scalp", "orderbook_005930_20261119_observe_part001.csv"),
        "stats": os.path.join(ROOT, "data", "scalp", "20261119", "collector_stats.json"),
        "jsonl": os.path.join(ROOT, "logs", "stage2c_login_only_20261119_073112_e05ee8168f8c.jsonl"),
        "expect_sha": {
            "tick": "ab12d9300fe185a02d602417f8a230630280535241f47f0f9ebd5785870281ce",
            "orderbook": "525a3ef2b59f53380e2738a1d261bc39e281a66a31a9ae5117ea73cd73fde66a",
        },
    },
    {
        "id": "D5",
        "date": "2026-11-20",
        "folder": "D5_20261120",
        "session_id": "s2c-12bc8ee0df21",
        "csat": False,
        "open_hm": 900,
        "auction_hm": 1520,
        "close_hm": 1530,
        "tick": os.path.join(ROOT, "data", "scalp", "archive", "20261120_072832_s2c-12bc8ee0df21", "tick_005930_20261120_observe_part001.csv"),
        "orderbook": os.path.join(ROOT, "data", "scalp", "archive", "20261120_072832_s2c-12bc8ee0df21", "orderbook_005930_20261120_observe_part001.csv"),
        "stats": os.path.join(ROOT, "data", "scalp", "archive", "20261120_072832_s2c-12bc8ee0df21", "collector_stats.json"),
        "jsonl": os.path.join(ROOT, "data", "scalp", "archive", "20261120_072832_s2c-12bc8ee0df21", "stage2c_login_only_20261120_072832_12bc8ee0df21.jsonl"),
        "expect_sha": {
            "tick": "875471c723dfe462b9107bacf5599d6fbccd66b9300defadb2efde18de9ad693",
            "orderbook": "de6430e4ac8e4af29862c10f07e7a747dfc1a27bc853f4257dbb6cf29f7cf8f3",
        },
    },
]


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_iso(raw: str) -> Optional[datetime]:
    s = str(raw or "").strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def hhmm(dt: datetime) -> int:
    return dt.hour * 100 + dt.minute


def bucket_of(dt: datetime, open_hm: int, auction_hm: int, close_hm: int) -> str:
    hm = hhmm(dt)
    if hm < open_hm:
        return "pre"
    if hm < auction_hm:
        return "continuous"
    if hm < close_hm:
        return "closing_auction"
    return "after"


def is_true(v: str) -> bool:
    return str(v or "").strip().lower() == "true"


def parse_signed_int(raw: str) -> Optional[int]:
    t = str(raw or "").strip().replace(",", "")
    if not t:
        return None
    try:
        return int(t)
    except ValueError:
        try:
            return int(float(t))
        except ValueError:
            return None


def abs_int(raw: str) -> Optional[int]:
    v = parse_signed_int(raw)
    if v is None:
        return None
    return abs(v)


def median(xs: List[float]) -> Optional[float]:
    if not xs:
        return None
    ys = sorted(xs)
    n = len(ys)
    mid = n // 2
    if n % 2:
        return ys[mid]
    return (ys[mid - 1] + ys[mid]) / 2.0


def relpath(path: str) -> str:
    return os.path.relpath(path, ROOT).replace("\\", "/")


def new_quality() -> Dict[str, Any]:
    return {
        "rows": 0,
        "fid_missing": 0,
        "recv_iso_reversals": 0,
        "seq_reversals": 0,
        "seq_duplicates": 0,
        "unique_seq": 0,
        "min_seq": None,
        "max_seq": None,
        "gaps_over_60s": [],
        "first_recv_iso": None,
        "last_recv_iso": None,
        "fingerprint_dup_suspect": 0,
        "hourly": {},
        "buy_count": 0,
        "sell_count": 0,
        "unsigned_count": 0,
        "buy_vol": 0,
        "sell_vol": 0,
        "unsigned_vol": 0,
        "price_first": None,
        "price_last": None,
        "price_min": None,
        "price_max": None,
        "accum_vol_first": None,
        "accum_vol_last": None,
        "strength_sum": 0.0,
        "strength_n": 0,
        "ask_qty_sum": [0, 0, 0, 0, 0],
        "bid_qty_sum": [0, 0, 0, 0, 0],
        "ask_total_sum": 0,
        "bid_total_sum": 0,
        "imbalance_sum": 0.0,
        "imbalance_n": 0,
        "ask_total_abs_delta": 0,
        "bid_total_abs_delta": 0,
        "timestamps": [],
    }


def hour_key(dt: datetime, auction_hm: int) -> str:
    if hhmm(dt) >= auction_hm:
        return "closing"
    return "%02d:00-%02d:00" % (dt.hour, dt.hour + 1)


def ensure_hour(q: Dict[str, Any], key: str) -> Dict[str, Any]:
    h = q["hourly"].get(key)
    if h is None:
        h = {
            "tick_count": 0,
            "trade_vol": 0,
            "price_first": None,
            "price_last": None,
            "buy_count": 0,
            "sell_count": 0,
            "unsigned_count": 0,
            "ask1_5_mean": None,
            "bid1_5_mean": None,
            "_ask_sum": 0,
            "_bid_sum": 0,
            "_n": 0,
        }
        q["hourly"][key] = h
    return h


def update_seq(q: Dict[str, Any], seq_s: str, seen: set) -> None:
    try:
        seq = int(str(seq_s).strip())
    except (TypeError, ValueError):
        q["seq_reversals"] += 1
        return
    if q["min_seq"] is None or seq < q["min_seq"]:
        q["min_seq"] = seq
    if q["max_seq"] is None or seq > q["max_seq"]:
        q["max_seq"] = seq
    if seq in seen:
        q["seq_duplicates"] += 1
    else:
        seen.add(seq)
        q["unique_seq"] += 1
    prev = q.get("_prev_seq")
    if prev is not None and seq < prev:
        q["seq_reversals"] += 1
    q["_prev_seq"] = seq


def note_gap(q: Dict[str, Any], prev: Optional[datetime], cur: datetime) -> None:
    if prev is None:
        return
    gap = (cur - prev).total_seconds()
    if gap >= GAP_SEC:
        q["gaps_over_60s"].append(
            {"from": prev.isoformat(), "to": cur.isoformat(), "gap_sec": round(gap, 6)}
        )
    if cur < prev:
        q["recv_iso_reversals"] += 1


def fid_missing_tick(row: Dict[str, str]) -> int:
    n = 0
    for k in ("fid10_ok", "fid13_ok", "fid15_ok", "fid20_ok", "fid228_ok"):
        if not is_true(row.get(k, "")):
            n += 1
    return n


def fid_missing_hoga(row: Dict[str, str]) -> int:
    n = 0
    for k, v in row.items():
        if k.endswith("_ok") and k.startswith("fid") and not is_true(v):
            n += 1
    return n


def process_file(
    src: str,
    dest_cont: str,
    dest_auc: str,
    kind: str,
    open_hm: int,
    auction_hm: int,
    close_hm: int,
) -> Dict[str, Any]:
    os.makedirs(os.path.dirname(dest_cont), exist_ok=True)
    os.makedirs(os.path.dirname(dest_auc), exist_ok=True)
    q_all = {"source_rows": 0, "pre": 0, "after": 0, "bad": 0}
    q_cont = new_quality()
    q_auc = new_quality()
    seen_cont: set = set()
    seen_auc: set = set()
    recent_cont: List[Tuple[str, str, str]] = []
    recent_auc: List[Tuple[str, str, str]] = []
    prev_ask = prev_bid = None

    with open(src, "r", encoding="utf-8", newline="") as inf:
        reader = csv.DictReader(inf)
        fieldnames = list(reader.fieldnames or [])
        with open(dest_cont, "w", encoding="utf-8", newline="") as c_out, open(
            dest_auc, "w", encoding="utf-8", newline=""
        ) as a_out:
            c_w = csv.DictWriter(c_out, fieldnames=fieldnames, lineterminator="\n")
            a_w = csv.DictWriter(a_out, fieldnames=fieldnames, lineterminator="\n")
            c_w.writeheader()
            a_w.writeheader()
            for row in reader:
                q_all["source_rows"] += 1
                dt = parse_iso(row.get("recv_iso", ""))
                if dt is None:
                    q_all["bad"] += 1
                    continue
                b = bucket_of(dt, open_hm, auction_hm, close_hm)
                if b == "pre":
                    q_all["pre"] += 1
                    continue
                if b == "after":
                    q_all["after"] += 1
                    continue
                q = q_cont if b == "continuous" else q_auc
                seen = seen_cont if b == "continuous" else seen_auc
                recent = recent_cont if b == "continuous" else recent_auc
                writer = c_w if b == "continuous" else a_w
                writer.writerow(row)
                q["rows"] += 1
                if q["first_recv_iso"] is None:
                    q["first_recv_iso"] = dt.isoformat()
                q["last_recv_iso"] = dt.isoformat()
                prev_dt = q.get("_prev_dt")
                note_gap(q, prev_dt, dt)
                q["_prev_dt"] = dt
                update_seq(q, row.get("seq", ""), seen)
                hk = hour_key(dt, auction_hm)
                hour = ensure_hour(q, hk)
                hour["_n"] += 1
                q["timestamps"].append(dt.timestamp())
                if kind == "tick":
                    fp = (
                        str(row.get("code") or ""),
                        str(row.get("fid20_raw") or ""),
                        str(row.get("fid15_raw") or ""),
                        str(row.get("fid10_raw") or ""),
                    )
                else:
                    fp = (
                        str(row.get("code") or ""),
                        str(row.get("fid21_raw") or ""),
                        str(row.get("fid41_raw") or ""),
                        str(row.get("fid51_raw") or ""),
                    )
                if fp in recent:
                    q["fingerprint_dup_suspect"] += 1
                recent.append(fp)
                if len(recent) > 256:
                    recent.pop(0)
                if kind == "tick":
                    q["fid_missing"] += fid_missing_tick(row)
                    price = abs_int(row.get("fid10") or row.get("fid10_raw") or "")
                    vol = abs_int(row.get("fid15") or row.get("fid15_raw") or "")
                    accum = abs_int(row.get("fid13") or row.get("fid13_raw") or "")
                    raw15 = str(row.get("fid15_raw") or "")
                    if price is not None:
                        if q["price_first"] is None:
                            q["price_first"] = price
                        q["price_last"] = price
                        q["price_min"] = price if q["price_min"] is None else min(q["price_min"], price)
                        q["price_max"] = price if q["price_max"] is None else max(q["price_max"], price)
                        if hour["price_first"] is None:
                            hour["price_first"] = price
                        hour["price_last"] = price
                    if accum is not None:
                        if q["accum_vol_first"] is None:
                            q["accum_vol_first"] = accum
                        q["accum_vol_last"] = accum
                    hour["tick_count"] += 1
                    if vol is not None:
                        hour["trade_vol"] += vol
                    if raw15.startswith("-"):
                        q["sell_count"] += 1
                        q["sell_vol"] += vol or 0
                        hour["sell_count"] += 1
                    elif raw15.startswith("+"):
                        q["buy_count"] += 1
                        q["buy_vol"] += vol or 0
                        hour["buy_count"] += 1
                    else:
                        q["unsigned_count"] += 1
                        q["unsigned_vol"] += vol or 0
                        hour["unsigned_count"] += 1
                    try:
                        st = float(str(row.get("fid228") or "").strip() or "nan")
                    except ValueError:
                        st = float("nan")
                    if st == st:
                        q["strength_sum"] += st
                        q["strength_n"] += 1
                else:
                    q["fid_missing"] += fid_missing_hoga(row)
                    asks = [abs_int(row.get("fid%d_raw" % f) or "") or 0 for f in (61, 62, 63, 64, 65)]
                    bids = [abs_int(row.get("fid%d_raw" % f) or "") or 0 for f in (71, 72, 73, 74, 75)]
                    ask_tot = abs_int(row.get("fid121_raw") or "") or sum(asks)
                    bid_tot = abs_int(row.get("fid125_raw") or "") or sum(bids)
                    for i in range(5):
                        q["ask_qty_sum"][i] += asks[i]
                        q["bid_qty_sum"][i] += bids[i]
                    q["ask_total_sum"] += ask_tot
                    q["bid_total_sum"] += bid_tot
                    den = ask_tot + bid_tot
                    if den:
                        q["imbalance_sum"] += (bid_tot - ask_tot) / den
                        q["imbalance_n"] += 1
                    if prev_ask is not None:
                        q["ask_total_abs_delta"] += abs(ask_tot - prev_ask)
                        q["bid_total_abs_delta"] += abs(bid_tot - prev_bid)
                    prev_ask, prev_bid = ask_tot, bid_tot
                    hour["_ask_sum"] += sum(asks)
                    hour["_bid_sum"] += sum(bids)

    for q in (q_cont, q_auc):
        q.pop("_prev_dt", None)
        q.pop("_prev_seq", None)
        for h in q["hourly"].values():
            n = h.get("_n") or 0
            if n and kind != "tick":
                h["ask1_5_mean"] = round(h["_ask_sum"] / n, 3)
                h["bid1_5_mean"] = round(h["_bid_sum"] / n, 3)
            h.pop("_ask_sum", None)
            h.pop("_bid_sum", None)
            h.pop("_n", None)
    return {"all": q_all, "continuous": q_cont, "closing_auction": q_auc}


def nearest_deltas(a: List[float], b: List[float]) -> Dict[str, Any]:
    if not a or not b:
        return {"n": 0, "median_sec": None, "p_within_0_1s": None, "p_within_1s": None}
    b = sorted(b)
    j = 0
    ds: List[float] = []
    for x in a:
        while j + 1 < len(b) and abs(b[j + 1] - x) <= abs(b[j] - x):
            j += 1
        ds.append(abs(b[j] - x))
    n = len(ds)
    return {
        "n": n,
        "median_sec": round(median(ds) or 0.0, 6),
        "p_within_0_1s": round(sum(1 for d in ds if d <= 0.1) / n, 6),
        "p_within_1s": round(sum(1 for d in ds if d <= 1.0) / n, 6),
    }


def slim_quality(q: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(q)
    out.pop("timestamps", None)
    n = out.get("rows") or 0
    if n:
        out["ask_qty_mean"] = [round(x / n, 3) for x in out.get("ask_qty_sum") or []]
        out["bid_qty_mean"] = [round(x / n, 3) for x in out.get("bid_qty_sum") or []]
        out["ask_total_mean"] = round((out.get("ask_total_sum") or 0) / n, 3)
        out["bid_total_mean"] = round((out.get("bid_total_sum") or 0) / n, 3)
    out["imbalance_mean"] = (
        round(out["imbalance_sum"] / out["imbalance_n"], 6) if out.get("imbalance_n") else None
    )
    out["strength_mean"] = (
        round(out["strength_sum"] / out["strength_n"], 6) if out.get("strength_n") else None
    )
    out["price_change"] = None
    if out.get("price_first") and out.get("price_last") is not None:
        out["price_change"] = out["price_last"] - out["price_first"]
        out["price_change_pct"] = round(100.0 * out["price_change"] / out["price_first"], 6)
    out["gap_count_over_60s"] = len(out.get("gaps_over_60s") or [])
    return out


def fmt_int(n: Any) -> str:
    if n is None:
        return "-"
    return f"{int(n):,}"


def write_report(manifest: Dict[str, Any], path: str) -> None:
    lines: List[str] = []
    lines.append("# Stage3.2A 삼성전자 5일 분석 데이터셋")
    lines.append("")
    lines.append("작성: %s" % manifest["created"])
    lines.append("종목: 005930")
    lines.append("원본 CSV·JSONL·stats 미수정, 미이동, 미삭제. 주문·OCX·학습 없음.")
    lines.append("")
    lines.append("## 출처와 SHA256")
    lines.append("")
    lines.append("| 날짜 | session_id | tick 원본 | orderbook 원본 | tick SHA256 | 일치 | stats |")
    lines.append("|---|---|---|---|---|---|---|")
    for day in manifest["days"]:
        src = day["sources"]
        lines.append(
            "| %s %s | `%s` | `%s` | `%s` | `%s` | %s | %s |"
            % (
                day["id"],
                day["date"],
                day["session_id"],
                src["tick"]["path"],
                src["orderbook"]["path"],
                src["tick"]["sha256"],
                "예" if src["tick"]["sha256_match"] and src["orderbook"]["sha256_match"] else "아니오",
                "없음 (추정 금지)" if day["stats_missing"] else "`%s`" % src["stats"]["path"],
            )
        )
    lines.append("")
    lines.append("D3는 collector_stats.json 원본이 없다. 생성·추정하지 않았다.")
    lines.append("D4는 `data/scalp/20261119/` 보존 복사본과 루트 CSV SHA256이 같다. 복사본을 읽기만 했고 이동·덮어쓰지 않았다.")
    lines.append("")
    lines.append("## 필터 결과")
    lines.append("")
    lines.append("| 날짜 | 창 | tick 원본 | tick 연속 | tick 종가 | tick 장전 제외 | tick 장후 제외 | hoga 원본 | hoga 연속 | hoga 종가 | hoga 장전 제외 | hoga 장후 제외 |")
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for day in manifest["days"]:
        t = day["tick"]
        h = day["orderbook"]
        lines.append(
            "| %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |"
            % (
                day["id"],
                day["window_label"],
                fmt_int(t["source_rows"]),
                fmt_int(t["continuous"]["rows"]),
                fmt_int(t["closing_auction"]["rows"]),
                fmt_int(t["excluded_pre"]),
                fmt_int(t["excluded_after"]),
                fmt_int(h["source_rows"]),
                fmt_int(h["continuous"]["rows"]),
                fmt_int(h["closing_auction"]["rows"]),
                fmt_int(h["excluded_pre"]),
                fmt_int(h["excluded_after"]),
            )
        )
    lines.append("")
    lines.append("복사본 행 수 = 연속매매 + 종가 단일가. 장전·장후는 분석 파일에 넣지 않았다.")
    lines.append("")
    lines.append("## 품질 검사")
    lines.append("")
    lines.append("| 날짜 | 구간 | 행 | FID 결측 | 시간 역전 | seq 역전 | seq 중복 | 60s+ 공백 | fingerprint dup_suspect |")
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|---:|")
    for day in manifest["days"]:
        for kind in ("tick", "orderbook"):
            for part in ("continuous", "closing_auction"):
                q = day[kind][part]
                lines.append(
                    "| %s | %s %s | %s | %s | %s | %s | %s | %s | %s |"
                    % (
                        day["id"],
                        kind,
                        "연속" if part == "continuous" else "종가",
                        fmt_int(q["rows"]),
                        fmt_int(q["fid_missing"]),
                        fmt_int(q["recv_iso_reversals"]),
                        fmt_int(q["seq_reversals"]),
                        fmt_int(q["seq_duplicates"]),
                        fmt_int(q["gap_count_over_60s"]),
                        fmt_int(q["fingerprint_dup_suspect"]),
                    )
                )
    lines.append("")
    lines.append("seq는 tick+호가 전역 번호다. 한 파일 안에서 1,2,3… 연속이라고 보지 않는다. 빈 번호는 다른 종류 레코드다.")
    lines.append("fingerprint dup_suspect는 (code, 시각FID, fid15, fid10) 최근 256건 창의 재등장 횟수다. 행을 삭제하지 않았고, 확정 중복이 아니다.")
    lines.append("")
    lines.append("### 세션 원본 dup_suspect (stats)")
    lines.append("")
    lines.append("| 날짜 | dup_suspect | 원본 tick 행 | 비율 | 비고 |")
    lines.append("|---|---:|---:|---:|---|")
    for day in manifest["days"]:
        ds = day.get("dup_suspect_original")
        rows = day["tick"]["source_rows"]
        ratio = "-" if ds is None or not rows else "%.2f%%" % (100.0 * ds / rows)
        note = day.get("dup_suspect_note") or ""
        flag = " **주의**" if day["id"] == "D5" else ""
        lines.append("| %s%s | %s | %s | %s | %s |" % (day["id"], flag, fmt_int(ds), fmt_int(rows), ratio, note))
    lines.append("")
    lines.append("D5 원본 dup_suspect=175,682. 의심 지표이며 이번 단계에서 행을 제거하지 않았다.")
    lines.append("")
    lines.append("## 연속매매 비교 (D4는 수능, 같은 시간축으로 합치지 않음)")
    lines.append("")
    lines.append("| 날짜 | 체결 건 | 가격 시가→종가 | 변화%% | 누적거래량 변화 | +체결 | -체결 | 부호없음 | 체결강도 평균 |")
    lines.append("|---|---:|---|---:|---:|---:|---:|---:|---:|")
    for day in manifest["days"]:
        q = day["tick"]["continuous"]
        px = "%s→%s" % (fmt_int(q.get("price_first")), fmt_int(q.get("price_last")))
        accum = None
        if q.get("accum_vol_first") is not None and q.get("accum_vol_last") is not None:
            accum = q["accum_vol_last"] - q["accum_vol_first"]
        tag = " (수능)" if day["csat"] else ""
        lines.append(
            "| %s%s | %s | %s | %s | %s | %s | %s | %s | %s |"
            % (
                day["id"],
                tag,
                fmt_int(q["rows"]),
                px,
                "-" if q.get("price_change_pct") is None else "%.3f" % q["price_change_pct"],
                fmt_int(accum),
                fmt_int(q.get("buy_count")),
                fmt_int(q.get("sell_count")),
                fmt_int(q.get("unsigned_count")),
                "-" if q.get("strength_mean") is None else "%.3f" % q["strength_mean"],
            )
        )
    lines.append("")
    lines.append("fid15_raw 선행 `+`/`-`를 매수·매도 체결 부호로만 집계했다. 부호 없는 행은 매수·매도로 추정하지 않았다.")
    lines.append("")
    lines.append("### 시간대별 체결 건수 (연속매매만, D4 별도)")
    lines.append("")
    hours_reg = ["%02d:00-%02d:00" % (h, h + 1) for h in range(9, 16)]
    lines.append("| 시간 | D1 | D2 | D3 | D5 |")
    lines.append("|---|---:|---:|---:|---:|")
    by_id = {d["id"]: d for d in manifest["days"]}
    for hk in hours_reg:
        cells = []
        for did in ("D1", "D2", "D3", "D5"):
            h = by_id[did]["tick"]["continuous"]["hourly"].get(hk) or {}
            cells.append(fmt_int(h.get("tick_count") or 0))
        lines.append("| %s | %s |" % (hk, " | ".join(cells)))
    lines.append("")
    lines.append("D4 수능 연속매매:")
    lines.append("")
    lines.append("| 시간 | 체결 건 | 체결량 합 | 가격 시→종 |")
    lines.append("|---|---:|---:|---|")
    for hk, h in sorted((by_id["D4"]["tick"]["continuous"]["hourly"] or {}).items()):
        if hk == "closing":
            continue
        lines.append(
            "| %s | %s | %s | %s→%s |"
            % (
                hk,
                fmt_int(h.get("tick_count")),
                fmt_int(h.get("trade_vol")),
                fmt_int(h.get("price_first")),
                fmt_int(h.get("price_last")),
            )
        )
    lines.append("")
    lines.append("### 시간대별 체결량 (연속매매, D1·D2·D3·D5)")
    lines.append("")
    lines.append("| 시간 | D1 | D2 | D3 | D5 |")
    lines.append("|---|---:|---:|---:|---:|")
    for hk in hours_reg:
        cells = []
        for did in ("D1", "D2", "D3", "D5"):
            h = by_id[did]["tick"]["continuous"]["hourly"].get(hk) or {}
            cells.append(fmt_int(h.get("trade_vol") or 0))
        lines.append("| %s | %s |" % (hk, " | ".join(cells)))
    lines.append("")
    lines.append("### 호가 1~5 잔량 평균과 불균형 (연속매매)")
    lines.append("")
    lines.append("| 날짜 | 매도1~5 평균합 | 매수1~5 평균합 | 총잔량 매도 평균 | 총잔량 매수 평균 | 불균형 (매수-매도)/(합) | 잔량 변화 합(매도/매수) |")
    lines.append("|---|---:|---:|---:|---:|---:|---|")
    for day in manifest["days"]:
        q = day["orderbook"]["continuous"]
        ask_m = sum(q.get("ask_qty_mean") or [])
        bid_m = sum(q.get("bid_qty_mean") or [])
        imb = "-" if q.get("imbalance_mean") is None else "%.4f" % q["imbalance_mean"]
        tag = " (수능)" if day["csat"] else ""
        lines.append(
            "| %s%s | %.1f | %.1f | %s | %s | %s | %s / %s |"
            % (
                day["id"],
                tag,
                ask_m,
                bid_m,
                fmt_int(round(q.get("ask_total_mean") or 0)),
                fmt_int(round(q.get("bid_total_mean") or 0)),
                imb,
                fmt_int(q.get("ask_total_abs_delta")),
                fmt_int(q.get("bid_total_abs_delta")),
            )
        )
    lines.append("")
    lines.append("불균형 > 0 이면 매수 잔량이 더 많다. 신호로 쓰지 않는다.")
    lines.append("")
    lines.append("### 체결과 호가의 시간 관계 (연속매매)")
    lines.append("")
    lines.append("| 날짜 | tick→최근 호가 중앙값(초) | 0.1초 이내 | 1초 이내 | hoga→최근 체결 중앙값(초) |")
    lines.append("|---|---:|---:|---:|---:|")
    for day in manifest["days"]:
        th = day["timing"]["tick_to_hoga"]
        ht = day["timing"]["hoga_to_tick"]
        tag = " (수능)" if day["csat"] else ""
        lines.append(
            "| %s%s | %s | %s | %s | %s |"
            % (
                day["id"],
                tag,
                "-" if th.get("median_sec") is None else "%.4f" % th["median_sec"],
                "-" if th.get("p_within_0_1s") is None else "%.1f%%" % (100 * th["p_within_0_1s"]),
                "-" if th.get("p_within_1s") is None else "%.1f%%" % (100 * th["p_within_1s"]),
                "-" if ht.get("median_sec") is None else "%.4f" % ht["median_sec"],
            )
        )
    lines.append("")
    lines.append("## 종가 단일가")
    lines.append("")
    lines.append("| 날짜 | tick | hoga | tick 60s+ 공백 | 비고 |")
    lines.append("|---|---:|---:|---:|---|")
    for day in manifest["days"]:
        tq = day["tick"]["closing_auction"]
        hq = day["orderbook"]["closing_auction"]
        note = []
        if tq["rows"] == 0:
            note.append("체결 0 (예상체결 미저장)")
        if day["id"] == "D5" and tq["rows"] == 1:
            note.append("15:29:54.633955 1건")
        if day["csat"]:
            note.append("16:20~16:30")
        lines.append(
            "| %s | %s | %s | %s | %s |"
            % (day["id"], fmt_int(tq["rows"]), fmt_int(hq["rows"]), fmt_int(tq["gap_count_over_60s"]), ", ".join(note) or "-")
        )
    lines.append("")
    lines.append("## 제외 데이터")
    lines.append("")
    lines.append("장전·장후는 정규장 분석 파일에서 제외했다. 행 수는 위 필터 표.")
    lines.append("D4를 09:00~15:30으로 자르지 않았다.")
    lines.append("거래소(KRX/NXT) 미확인 값은 붙이지 않았다.")
    lines.append("")
    lines.append("## 완료 조건")
    lines.append("")
    lines.append("- 원본 SHA256 재확인: %s" % ("통과" if manifest["originals_unchanged_after"] else "실패"))
    lines.append("- 원본 변경: 0건")
    lines.append("- 주문·OCX: 0건")
    lines.append("- 분석 복사본 경로: `data/scalp/analysis/stage31_5day/`")
    lines.append("")
    lines.append("Stage3.2B, 전략, 학습은 이 단계에서 수행하지 않았다.")
    lines.append("")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines))


def main() -> int:
    if os.path.isdir(OUT_ROOT) and os.listdir(OUT_ROOT):
        raise SystemExit("분석 폴더가 이미 있습니다. 덮어쓰지 않습니다: %s" % OUT_ROOT)
    os.makedirs(REPORT_DIR, exist_ok=True)
    created = datetime.now().replace(microsecond=0).isoformat()
    manifest: Dict[str, Any] = {
        "stage": "3.2A",
        "created": created,
        "code": CODE,
        "out_root": relpath(OUT_ROOT),
        "days": [],
        "originals_unchanged_after": False,
    }
    source_hashes: List[Tuple[str, str]] = []

    for spec in DAYS:
        tick_src = spec["tick"]
        hoga_src = spec["orderbook"]
        tick_sha = sha256_file(tick_src)
        hoga_sha = sha256_file(hoga_src)
        if tick_sha != spec["expect_sha"]["tick"]:
            raise SystemExit("tick SHA256 mismatch %s" % spec["id"])
        if hoga_sha != spec["expect_sha"]["orderbook"]:
            raise SystemExit("orderbook SHA256 mismatch %s" % spec["id"])
        source_hashes.append((tick_src, tick_sha))
        source_hashes.append((hoga_src, hoga_sha))
        d4_copy_match = None
        if spec["id"] == "D4":
            alt_t = sha256_file(spec["tick_alt"])
            alt_h = sha256_file(spec["orderbook_alt"])
            d4_copy_match = alt_t == tick_sha and alt_h == hoga_sha
            if not d4_copy_match:
                raise SystemExit("D4 copy vs root SHA256 mismatch")
            source_hashes.append((spec["tick_alt"], alt_t))
            source_hashes.append((spec["orderbook_alt"], alt_h))

        stats_obj = None
        stats_sha = None
        stats_missing = spec["stats"] is None
        if spec["stats"]:
            stats_sha = sha256_file(spec["stats"])
            with open(spec["stats"], encoding="utf-8") as fh:
                stats_obj = json.load(fh)
            source_hashes.append((spec["stats"], stats_sha))
            if str(stats_obj.get("session_id") or "") != spec["session_id"]:
                raise SystemExit("stats session_id mismatch %s" % spec["id"])

        jsonl_sha = sha256_file(spec["jsonl"]) if os.path.isfile(spec["jsonl"]) else None
        if jsonl_sha:
            source_hashes.append((spec["jsonl"], jsonl_sha))

        day_dir = os.path.join(OUT_ROOT, spec["folder"])
        tick_r = process_file(
            tick_src,
            os.path.join(day_dir, "continuous", "tick.csv"),
            os.path.join(day_dir, "closing_auction", "tick.csv"),
            "tick",
            spec["open_hm"],
            spec["auction_hm"],
            spec["close_hm"],
        )
        hoga_r = process_file(
            hoga_src,
            os.path.join(day_dir, "continuous", "orderbook.csv"),
            os.path.join(day_dir, "closing_auction", "orderbook.csv"),
            "orderbook",
            spec["open_hm"],
            spec["auction_hm"],
            spec["close_hm"],
        )
        timing = {
            "tick_to_hoga": nearest_deltas(
                tick_r["continuous"]["timestamps"], hoga_r["continuous"]["timestamps"]
            ),
            "hoga_to_tick": nearest_deltas(
                hoga_r["continuous"]["timestamps"], tick_r["continuous"]["timestamps"]
            ),
        }
        dup_orig = None
        dup_note = ""
        if stats_obj is not None:
            dup_orig = stats_obj.get("dup_suspect")
            dup_note = "collector_stats.json"
        else:
            dup_orig = spec.get("dup_suspect_from_verify")
            dup_note = "stats 없음. 검증 JSON 참고값만 기록. 추정 파일 없음"

        def pack(kind_r: Dict[str, Any], kind: str) -> Dict[str, Any]:
            return {
                "source_rows": kind_r["all"]["source_rows"],
                "excluded_pre": kind_r["all"]["pre"],
                "excluded_after": kind_r["all"]["after"],
                "bad_recv_iso": kind_r["all"]["bad"],
                "copy_rows": kind_r["continuous"]["rows"] + kind_r["closing_auction"]["rows"],
                "continuous": slim_quality(kind_r["continuous"]),
                "closing_auction": slim_quality(kind_r["closing_auction"]),
                "outputs": {
                    "continuous": relpath(os.path.join(day_dir, "continuous", "tick.csv" if kind == "tick" else "orderbook.csv")),
                    "closing_auction": relpath(os.path.join(day_dir, "closing_auction", "tick.csv" if kind == "tick" else "orderbook.csv")),
                },
            }

        day_entry = {
            "id": spec["id"],
            "date": spec["date"],
            "folder": spec["folder"],
            "session_id": spec["session_id"],
            "csat": spec["csat"],
            "window_label": "10:00~16:30" if spec["csat"] else "09:00~15:30",
            "filter": {
                "recv_iso": True,
                "continuous": "%04d <= hhmm < %04d" % (spec["open_hm"], spec["auction_hm"]),
                "closing_auction": "%04d <= hhmm < %04d" % (spec["auction_hm"], spec["close_hm"]),
            },
            "stats_missing": stats_missing,
            "dup_suspect_original": dup_orig,
            "dup_suspect_note": dup_note,
            "d4_copy_matches_root": d4_copy_match,
            "sources": {
                "tick": {
                    "path": relpath(tick_src),
                    "sha256": tick_sha,
                    "sha256_match": True,
                    "bytes": os.path.getsize(tick_src),
                },
                "orderbook": {
                    "path": relpath(hoga_src),
                    "sha256": hoga_sha,
                    "sha256_match": True,
                    "bytes": os.path.getsize(hoga_src),
                },
                "stats": None
                if stats_missing
                else {"path": relpath(spec["stats"]), "sha256": stats_sha, "bytes": os.path.getsize(spec["stats"])},
                "jsonl": None
                if not jsonl_sha
                else {"path": relpath(spec["jsonl"]), "sha256": jsonl_sha, "bytes": os.path.getsize(spec["jsonl"])},
            },
            "tick": pack(tick_r, "tick"),
            "orderbook": pack(hoga_r, "orderbook"),
            "timing": timing,
        }
        if spec["id"] == "D4":
            day_entry["sources"]["tick_root"] = {
                "path": relpath(spec["tick_alt"]),
                "sha256": sha256_file(spec["tick_alt"]),
                "same_as_copy": True,
            }
            day_entry["sources"]["orderbook_root"] = {
                "path": relpath(spec["orderbook_alt"]),
                "sha256": sha256_file(spec["orderbook_alt"]),
                "same_as_copy": True,
            }
        manifest["days"].append(day_entry)
        print("done", spec["id"], "tick_cont", tick_r["continuous"]["rows"], "hoga_cont", hoga_r["continuous"]["rows"], flush=True)

    unchanged = True
    for path, expected in source_hashes:
        if sha256_file(path) != expected:
            unchanged = False
            print("CHANGED", path, flush=True)
    manifest["originals_unchanged_after"] = unchanged

    man_path = os.path.join(REPORT_DIR, "dataset_manifest.json")
    with open(man_path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    write_report(manifest, os.path.join(REPORT_DIR, "STAGE32_DATASET_REPORT.md"))
    print("report", relpath(os.path.join(REPORT_DIR, "STAGE32_DATASET_REPORT.md")), flush=True)
    print("originals_unchanged", unchanged, flush=True)
    return 0 if unchanged else 2


if __name__ == "__main__":
    raise SystemExit(main())
