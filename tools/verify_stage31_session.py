"""Stage3.1 COLLECT_ONLY 세션 읽기 전용 검증기.

JSONL / collector_stats.json / tick CSV / orderbook CSV 가 같은 세션인지 확인한다.
입력 파일은 수정·이동·삭제하지 않는다. 키움 OCX·main.py·수집은 실행하지 않는다.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

VERDICT_PASS = "PASS"
VERDICT_WARN = "WARN"
VERDICT_FAIL = "FAIL"
VERDICT_INSUFFICIENT = "INSUFFICIENT_METADATA"

PENDING_KEYS = (
    "tick_buffer_pending",
    "orderbook_buffer_pending",
    "writer_queue_pending",
    "writer_inflight",
    "pending_buffer",
    "pending_queue",
    "pending_inflight",
    "pending_records",
)
TICK_FID_OK = ("fid10_ok", "fid13_ok", "fid15_ok", "fid20_ok", "fid228_ok")
TICK_FID_VALUE = ("fid13", "fid15")
HOGA_FID_OK = (
    "fid21_ok",
    "fid41_ok", "fid42_ok", "fid43_ok", "fid44_ok", "fid45_ok",
    "fid51_ok", "fid52_ok", "fid53_ok", "fid54_ok", "fid55_ok",
    "fid61_ok", "fid62_ok", "fid63_ok", "fid64_ok", "fid65_ok",
    "fid71_ok", "fid72_ok", "fid73_ok", "fid74_ok", "fid75_ok",
    "fid121_ok", "fid125_ok",
)
GAP_WARN_SEC = 60.0


def file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_json(path: str) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError("stats JSON 최상위가 object 가 아님")
    return data


def _load_jsonl(path: str) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if isinstance(rec, dict):
                events.append(rec)
    return events


def _load_csv(path: str) -> Tuple[List[str], List[Dict[str, str]]]:
    with open(path, encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        header = list(reader.fieldnames or [])
        rows = list(reader)
    return header, rows


def _iso_dt(raw: str) -> Optional[datetime]:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _in_window(recv_iso: str, start_ts: str, finish_ts: str) -> bool:
    """초 단위로 자른 시각을 보정하지 않는다. CSV 가 finish 보다 늦으면 False."""
    iso_dt = _iso_dt(recv_iso)
    start_dt = _iso_dt(start_ts)
    finish_dt = _iso_dt(finish_ts)
    if iso_dt is None or start_dt is None or finish_dt is None:
        return False
    return start_dt <= iso_dt <= finish_dt


def _truthy_ok(value: Any) -> bool:
    return str(value or "").strip() in ("True", "true", "1")


def _csv_kind(path: str) -> str:
    name = os.path.basename(path).lower()
    if name.startswith("tick_"):
        return "tick"
    if name.startswith("orderbook_"):
        return "orderbook"
    return ""


def _files_rows(stats: Dict[str, Any], kind: str) -> Optional[int]:
    for item in stats.get("files") or []:
        if not isinstance(item, dict):
            continue
        if _csv_kind(str(item.get("path") or "")) == kind:
            try:
                return int(item.get("rows"))
            except (TypeError, ValueError):
                return None
    return None


def _method_total(by_method: Any, name: str) -> int:
    if not isinstance(by_method, dict):
        return 0
    n = 0
    for key, payload in by_method.items():
        if str(key) == name or str(key).startswith(name):
            if isinstance(payload, dict):
                try:
                    n += int(payload.get("total", 0) or 0)
                except (TypeError, ValueError):
                    pass
            else:
                try:
                    n += int(payload or 0)
                except (TypeError, ValueError):
                    pass
    return n


def _screen_remove_total(payload: Any, screen: str) -> int:
    if not isinstance(payload, dict):
        return 0
    rec = payload.get(screen)
    if not isinstance(rec, dict):
        return 0
    try:
        return int(rec.get("total", rec.get("runtime", 0)) or 0)
    except (TypeError, ValueError):
        return 0


def _seq_reversals(rows: List[Dict[str, str]]) -> int:
    n = 0
    prev: Optional[int] = None
    for row in rows:
        try:
            seq = int(str(row.get("seq") or "").strip())
        except (TypeError, ValueError):
            continue
        if prev is not None and seq < prev:
            n += 1
        prev = seq
    return n


def _iso_reversals(rows: List[Dict[str, str]]) -> int:
    n = 0
    prev = ""
    for row in rows:
        iso = str(row.get("recv_iso") or "")
        if prev and iso and iso < prev:
            n += 1
        if iso:
            prev = iso
    return n


def _gaps(rows: List[Dict[str, str]], min_sec: float) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    prev_dt: Optional[datetime] = None
    prev_iso = ""
    for row in rows:
        iso = str(row.get("recv_iso") or "")
        dt = _iso_dt(iso)
        if prev_dt is not None and dt is not None:
            delta = (dt - prev_dt).total_seconds()
            if delta >= min_sec:
                out.append(
                    {
                        "from": prev_iso,
                        "to": iso,
                        "gap_sec": round(delta, 6),
                    }
                )
        if dt is not None:
            prev_dt = dt
            prev_iso = iso
    return out


def _fid_missing(rows: List[Dict[str, str]], header: List[str], ok_cols: Sequence[str], value_cols: Sequence[str] = ()) -> int:
    missing = 0
    present_ok = [c for c in ok_cols if c in header]
    present_val = [c for c in value_cols if c in header]
    if len(present_ok) != len(ok_cols):
        return max(1, len(rows) * (len(ok_cols) - len(present_ok)))
    for row in rows:
        bad = False
        for col in present_ok:
            if not _truthy_ok(row.get(col)):
                bad = True
                break
        if not bad:
            for col in present_val:
                if str(row.get(col) or "").strip() in ("", "None"):
                    bad = True
                    break
        if bad:
            missing += 1
    return missing


def _check(checks: List[Dict[str, Any]], name: str, ok: bool, detail: str, *, level: str = "fail") -> None:
    if ok:
        status = "PASS"
    elif level == "warn":
        status = "WARN"
    else:
        status = "FAIL"
    checks.append({"name": name, "status": status, "detail": detail})


def _meta_present(stats: Dict[str, Any], events: List[Dict[str, Any]]) -> Tuple[bool, str]:
    sid = str(stats.get("session_id") or "").strip()
    start = str(stats.get("session_start") or "").strip()
    finished = str(stats.get("session_finished") or "").strip()
    jsonl_sids = [str(e.get("session_id") or "").strip() for e in events]
    if "session_id" not in stats or not sid:
        return False, "stats.session_id 없음"
    if "session_start" not in stats or not start:
        return False, "stats.session_start 없음"
    if "session_finished" not in stats or not finished:
        return False, "stats.session_finished 없음"
    if not events:
        return False, "JSONL 이벤트 없음"
    if any(not s for s in jsonl_sids):
        return False, "JSONL session_id 공백"
    return True, ""


def verify_session(
    jsonl_path: str,
    stats_path: str,
    tick_path: str,
    orderbook_path: str,
) -> Dict[str, Any]:
    """네 파일을 읽기 전용으로 검사한다. 입력 경로는 열기만 한다."""
    checks: List[Dict[str, Any]] = []
    notes: Dict[str, Any] = {}
    paths = {
        "jsonl": os.path.abspath(jsonl_path),
        "stats": os.path.abspath(stats_path),
        "tick": os.path.abspath(tick_path),
        "orderbook": os.path.abspath(orderbook_path),
    }
    events = _load_jsonl(jsonl_path)
    stats = _load_json(stats_path)
    tick_header, tick_rows = _load_csv(tick_path)
    hoga_header, hoga_rows = _load_csv(orderbook_path)

    meta_ok, meta_reason = _meta_present(stats, events)
    starts = [e for e in events if e.get("event") == "SESSION_START"]
    fins = [e for e in events if e.get("event") == "SESSION_FINISHED"]
    jsonl_sids = sorted(set(str(e.get("session_id") or "") for e in events))
    start_ts = str(starts[0].get("ts") or "") if len(starts) == 1 else ""
    fin_ts = str(fins[0].get("ts") or "") if len(fins) == 1 else ""
    jsonl_sid = jsonl_sids[0] if len(jsonl_sids) == 1 else ""

    _check(checks, "jsonl_session_start_count", len(starts) == 1, "SESSION_START=%s" % len(starts))
    _check(checks, "jsonl_session_finished_count", len(fins) == 1, "SESSION_FINISHED=%s" % len(fins))
    _check(
        checks,
        "jsonl_session_id_unique",
        len(jsonl_sids) == 1 and bool(jsonl_sid),
        "session_ids=%s" % jsonl_sids,
    )
    stats_sid = str(stats.get("session_id") or "")
    stats_start = str(stats.get("session_start") or "")
    stats_finished = str(stats.get("session_finished") or "")
    _check(
        checks,
        "stats_session_id_match",
        bool(stats_sid) and stats_sid == jsonl_sid,
        "stats=%r jsonl=%r" % (stats_sid, jsonl_sid),
    )
    _check(
        checks,
        "stats_session_start_match",
        bool(stats_start) and stats_start == start_ts,
        "stats=%r start.ts=%r" % (stats_start, start_ts),
    )
    _check(
        checks,
        "stats_session_finished_match",
        bool(stats_finished) and stats_finished == fin_ts,
        "stats=%r finished.ts=%r" % (stats_finished, fin_ts),
    )

    win_start = stats_start or start_ts
    win_finish = stats_finished or fin_ts
    tick_out = [r.get("recv_iso") for r in tick_rows if not _in_window(str(r.get("recv_iso") or ""), win_start, win_finish)]
    hoga_out = [r.get("recv_iso") for r in hoga_rows if not _in_window(str(r.get("recv_iso") or ""), win_start, win_finish)]
    _check(
        checks,
        "csv_recv_iso_in_session_window",
        not tick_out and not hoga_out and bool(win_start) and bool(win_finish),
        "tick_out=%s hoga_out=%s window=%s..%s" % (len(tick_out), len(hoga_out), win_start, win_finish),
    )

    shutdown = str(stats.get("shutdown_status") or "")
    dump_ok = stats.get("stats_dump_ok")
    _check(checks, "shutdown_complete", shutdown == "COMPLETE", "shutdown_status=%r" % shutdown)
    _check(
        checks,
        "stats_dump_ok",
        dump_ok is True and shutdown != "STATS_DUMP_FAILED",
        "stats_dump_ok=%r shutdown_status=%r" % (dump_ok, shutdown),
    )
    _check(checks, "bridge_closed", str(stats.get("bridge_status") or "") == "CLOSED", "bridge_status=%r" % stats.get("bridge_status"))
    screen = stats.get("screen_state") if isinstance(stats.get("screen_state"), dict) else {}
    _check(checks, "screen_8101", str(screen.get("8101") or "") == "NOT_REGISTERED", "8101=%r" % screen.get("8101"))
    _check(checks, "screen_8102", str(screen.get("8102") or "") == "NOT_REGISTERED", "8102=%r" % screen.get("8102"))
    _check(checks, "screens_active_false", stats.get("screens_active") is False, "screens_active=%r" % stats.get("screens_active"))

    pending_bad = []
    for key in PENDING_KEYS:
        try:
            val = int(stats.get(key, 0) or 0)
        except (TypeError, ValueError):
            val = -1
        if val != 0:
            pending_bad.append("%s=%s" % (key, val))
    _check(checks, "pending_zero", not pending_bad, ",".join(pending_bad) or "all=0")

    try:
        losses = int(stats.get("terminal_losses", 0) or 0)
    except (TypeError, ValueError):
        losses = -1
    _check(checks, "terminal_losses_zero", losses == 0, "terminal_losses=%s" % losses)

    remove = stats.get("orig_ocx_setrealremove")
    _check(checks, "setrealremove_8101", _screen_remove_total(remove, "8101") == 1, "8101=%s" % _screen_remove_total(remove, "8101"))
    _check(checks, "setrealremove_8102", _screen_remove_total(remove, "8102") == 1, "8102=%s" % _screen_remove_total(remove, "8102"))
    by_method = stats.get("orig_ocx_by_method")
    send_n = _method_total(by_method, "SendOrder")
    term_n = _method_total(by_method, "CommTerminate")
    _check(checks, "sendorder_orig_zero", send_n == 0, "SendOrder total=%s" % send_n)
    _check(checks, "commterminate_orig_zero", term_n == 0, "CommTerminate total=%s" % term_n)

    tick_n = len(tick_rows)
    hoga_n = len(hoga_rows)
    files_tick = _files_rows(stats, "tick")
    files_hoga = _files_rows(stats, "orderbook")
    try:
        write_ok = int(stats.get("write_ok", 0) or 0)
    except (TypeError, ValueError):
        write_ok = -1
    try:
        tick_cb = int(stats.get("tick_callbacks", 0) or 0)
    except (TypeError, ValueError):
        tick_cb = -1
    try:
        hoga_cb = int(stats.get("hoga_callbacks", 0) or 0)
    except (TypeError, ValueError):
        hoga_cb = -1
    tick_expected = files_tick if files_tick is not None else tick_cb
    hoga_expected = files_hoga if files_hoga is not None else hoga_cb
    _check(checks, "tick_rows_vs_write_ok", tick_n == tick_expected, "csv=%s expected=%s files.rows=%s tick_callbacks=%s" % (tick_n, tick_expected, files_tick, tick_cb))
    _check(checks, "orderbook_rows_vs_write_ok", hoga_n == hoga_expected, "csv=%s expected=%s files.rows=%s hoga_callbacks=%s" % (hoga_n, hoga_expected, files_hoga, hoga_cb))
    _check(checks, "write_ok_sum", tick_n + hoga_n == write_ok, "csv_sum=%s write_ok=%s" % (tick_n + hoga_n, write_ok))

    tick_seq_back = _seq_reversals(tick_rows)
    hoga_seq_back = _seq_reversals(hoga_rows)
    _check(checks, "tick_seq_monotonic", tick_seq_back == 0, "reversals=%s" % tick_seq_back)
    _check(checks, "orderbook_seq_monotonic", hoga_seq_back == 0, "reversals=%s" % hoga_seq_back)
    tick_iso_back = _iso_reversals(tick_rows)
    hoga_iso_back = _iso_reversals(hoga_rows)
    _check(checks, "tick_recv_iso_monotonic", tick_iso_back == 0, "reversals=%s" % tick_iso_back)
    _check(checks, "orderbook_recv_iso_monotonic", hoga_iso_back == 0, "reversals=%s" % hoga_iso_back)

    fid13_15 = stats.get("fid13_15") if isinstance(stats.get("fid13_15"), dict) else {}
    try:
        stats_fid13_missing = int(fid13_15.get("fid13_missing", 0) or 0)
        stats_fid15_missing = int(fid13_15.get("fid15_missing", 0) or 0)
    except (TypeError, ValueError):
        stats_fid13_missing = -1
        stats_fid15_missing = -1
    tick_fid_miss = _fid_missing(tick_rows, tick_header, TICK_FID_OK, TICK_FID_VALUE)
    hoga_fid_miss = _fid_missing(hoga_rows, hoga_header, HOGA_FID_OK)
    _check(
        checks,
        "fid_missing_zero",
        tick_fid_miss == 0 and hoga_fid_miss == 0 and stats_fid13_missing == 0 and stats_fid15_missing == 0,
        "tick_csv=%s hoga_csv=%s fid13_missing=%s fid15_missing=%s" % (tick_fid_miss, hoga_fid_miss, stats_fid13_missing, stats_fid15_missing),
    )

    tick_gaps = _gaps(tick_rows, GAP_WARN_SEC)
    hoga_gaps = _gaps(hoga_rows, GAP_WARN_SEC)
    merged = sorted(
        [{"recv_iso": r.get("recv_iso"), "kind": "tick"} for r in tick_rows]
        + [{"recv_iso": r.get("recv_iso"), "kind": "orderbook"} for r in hoga_rows],
        key=lambda x: str(x.get("recv_iso") or ""),
    )
    merged_gaps = _gaps(merged, GAP_WARN_SEC)
    notes["gaps"] = {"tick": tick_gaps, "orderbook": hoga_gaps, "merged": merged_gaps}
    gap_n = len(tick_gaps) + len(hoga_gaps) + len(merged_gaps)
    _check(
        checks,
        "gap_over_60s",
        gap_n == 0,
        "tick=%s orderbook=%s merged=%s" % (len(tick_gaps), len(hoga_gaps), len(merged_gaps)),
        level="warn",
    )

    tick_first = str(tick_rows[0].get("recv_iso") or "") if tick_rows else ""
    tick_last = str(tick_rows[-1].get("recv_iso") or "") if tick_rows else ""
    hoga_first = str(hoga_rows[0].get("recv_iso") or "") if hoga_rows else ""
    hoga_last = str(hoga_rows[-1].get("recv_iso") or "") if hoga_rows else ""
    t0, h0 = _iso_dt(tick_first), _iso_dt(hoga_first)
    t1, h1 = _iso_dt(tick_last), _iso_dt(hoga_last)
    notes["tick_hoga_delta_sec"] = {
        "tick_first": tick_first,
        "tick_last": tick_last,
        "orderbook_first": hoga_first,
        "orderbook_last": hoga_last,
        "start_delta_sec": round((t0 - h0).total_seconds(), 6) if t0 and h0 else None,
        "end_delta_sec": round((t1 - h1).total_seconds(), 6) if t1 and h1 else None,
    }
    notes["dup_suspect"] = stats.get("dup_suspect")
    notes["dup_suspect_note"] = "dup_suspect 는 참고 지표이며 데이터 손실(terminal_losses)로 판정하지 않는다."

    fail = [c for c in checks if c["status"] == "FAIL" and c["name"] != "gap_over_60s"]
    warn = [c for c in checks if c["status"] == "WARN"]
    if not meta_ok:
        verdict = VERDICT_INSUFFICIENT
    elif fail:
        verdict = VERDICT_FAIL
    elif warn:
        verdict = VERDICT_WARN
    else:
        verdict = VERDICT_PASS

    return {
        "verdict": verdict,
        "insufficient_reason": meta_reason if not meta_ok else "",
        "session_id": stats_sid or jsonl_sid,
        "session_start": stats_start or start_ts,
        "session_finished": stats_finished or fin_ts,
        "paths": paths,
        "checks": checks,
        "notes": notes,
        "counts": {
            "jsonl_events": len(events),
            "tick_rows": tick_n,
            "orderbook_rows": hoga_n,
            "write_ok": write_ok,
            "dup_suspect": stats.get("dup_suspect"),
            "terminal_losses": losses,
        },
    }


def format_report(result: Dict[str, Any]) -> str:
    lines = [
        "Stage3.1 session verify: %s" % result.get("verdict"),
        "session_id=%s" % result.get("session_id"),
        "window=%s .. %s" % (result.get("session_start"), result.get("session_finished")),
    ]
    if result.get("insufficient_reason"):
        lines.append("insufficient: %s" % result["insufficient_reason"])
    for c in result.get("checks") or []:
        lines.append("  [%s] %s — %s" % (c["status"], c["name"], c["detail"]))
    notes = result.get("notes") or {}
    lines.append("dup_suspect=%s (%s)" % (notes.get("dup_suspect"), notes.get("dup_suspect_note")))
    delta = notes.get("tick_hoga_delta_sec") or {}
    lines.append(
        "tick_hoga_delta start=%s end=%s"
        % (delta.get("start_delta_sec"), delta.get("end_delta_sec"))
    )
    gaps = notes.get("gaps") or {}
    lines.append(
        "gaps>=60s tick=%s orderbook=%s merged=%s"
        % (len(gaps.get("tick") or []), len(gaps.get("orderbook") or []), len(gaps.get("merged") or []))
    )
    return "\n".join(lines)


def _refuse_if_input(report_path: str, inputs: Sequence[str]) -> None:
    report_abs = os.path.normcase(os.path.abspath(report_path))
    for p in inputs:
        if os.path.normcase(os.path.abspath(p)) == report_abs:
            raise ValueError("보고서 경로가 검사 대상 파일과 같습니다: %s" % report_path)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="COLLECT_ONLY 세션 JSONL/stats/CSV 읽기 전용 검증 (파일 수정·이동·삭제 없음)",
    )
    parser.add_argument("--jsonl", required=True, help="stage2c_login_only_*.jsonl")
    parser.add_argument("--stats", required=True, help="collector_stats.json")
    parser.add_argument("--tick", required=True, help="tick_*.csv")
    parser.add_argument("--orderbook", required=True, help="orderbook_*.csv")
    parser.add_argument("--report", default="", help="검증 결과 JSON 저장 경로 (입력 파일과 겹치면 거부)")
    ns = parser.parse_args(list(argv) if argv is not None else None)
    try:
        result = verify_session(ns.jsonl, ns.stats, ns.tick, ns.orderbook)
    except Exception as e:
        print("검증 실패(읽기 오류): %s: %s" % (type(e).__name__, e), flush=True)
        return 1
    print(format_report(result), flush=True)
    if ns.report:
        try:
            _refuse_if_input(ns.report, (ns.jsonl, ns.stats, ns.tick, ns.orderbook))
            parent = os.path.dirname(os.path.abspath(ns.report))
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(ns.report, "w", encoding="utf-8") as fh:
                json.dump(result, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
            print("report_saved=%s" % os.path.abspath(ns.report), flush=True)
        except Exception as e:
            print("보고서 저장 실패: %s: %s" % (type(e).__name__, e), flush=True)
            return 1
    verdict = result.get("verdict")
    if verdict == VERDICT_PASS:
        return 0
    if verdict == VERDICT_WARN:
        return 0
    if verdict == VERDICT_INSUFFICIENT:
        return 3
    return 2


if __name__ == "__main__":
    sys.exit(main())
