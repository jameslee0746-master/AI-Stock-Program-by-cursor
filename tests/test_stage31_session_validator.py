"""Stage3.1 세션 검증기 Fake 테스트. 입력 파일 수정 없음. OCX/main.py 없음."""
from __future__ import annotations

import csv
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scalp.collector import HOGA_FIDS  # noqa: E402
from scalp.collector.orderbook_collector import HOGA_FIELDS  # noqa: E402
from scalp.collector.tick_collector import TICK_FIELDS  # noqa: E402
from tools.verify_stage31_session import (  # noqa: E402
    VERDICT_FAIL,
    VERDICT_INSUFFICIENT,
    VERDICT_PASS,
    VERDICT_WARN,
    file_sha256,
    main as verify_main,
    verify_session,
)

PILOT_DIR = os.path.join(ROOT, "data", "scalp", "archive", "20261113_115244_3eaf5d64f86d")
PILOT_JSONL = os.path.join(PILOT_DIR, "stage2c_login_only_20261113_115244_3eaf5d64f86d.jsonl")
PILOT_STATS = os.path.join(PILOT_DIR, "collector_stats.json")
PILOT_TICK = os.path.join(PILOT_DIR, "tick_005930_20261113_observe_part001.csv")
PILOT_HOGA = os.path.join(PILOT_DIR, "orderbook_005930_20261113_observe_part001.csv")

ARCH_B_DIR = os.path.join(ROOT, "data", "scalp", "archive", "20261113_124557_s2c-c9461b44d92c")
ARCH_B = (
    os.path.join(ARCH_B_DIR, "stage2c_login_only_20261113_124557_c9461b44d92c.jsonl"),
    os.path.join(ARCH_B_DIR, "collector_stats.json"),
    os.path.join(ARCH_B_DIR, "tick_005930_20261113_observe_part001.csv"),
    os.path.join(ARCH_B_DIR, "orderbook_005930_20261113_observe_part001.csv"),
)
ARCH_C_DIR = os.path.join(ROOT, "data", "scalp", "archive", "20261113_131247_s2c-3245443baac9")
ARCH_C = (
    os.path.join(ARCH_C_DIR, "stage2c_login_only_20261113_131247_3245443baac9.jsonl"),
    os.path.join(ARCH_C_DIR, "collector_stats.json"),
    os.path.join(ARCH_C_DIR, "tick_005930_20261113_observe_part001.csv"),
    os.path.join(ARCH_C_DIR, "orderbook_005930_20261113_observe_part001.csv"),
)

SID = "s2c-testdeadbeef"
START = "2026-11-13T12:00:00"
FINISH = "2026-11-13T12:05:00"


def _hashes(paths):
    return {p: file_sha256(p) for p in paths}


def _tick_row(iso: str, seq: int, **over):
    rec = {k: "" for k in TICK_FIELDS}
    rec.update(
        {
            "recv_ns": str(seq * 1000),
            "recv_iso": iso,
            "code": "005930",
            "real_type": "주식체결",
            "epoch": "1",
            "phase": "observe",
            "fid10_raw": "210000",
            "fid10": "210000",
            "fid10_ok": "True",
            "fid13_raw": "100",
            "fid13": "100",
            "fid13_ok": "True",
            "fid15_raw": "10",
            "fid15": "10",
            "fid15_ok": "True",
            "fid20_raw": "120000",
            "fid20_sod": "43200",
            "fid20_ok": "True",
            "fid228_raw": "99.0",
            "fid228": "99.0",
            "fid228_ok": "True",
            "clock_offset_sec": "0",
            "seq": str(seq),
        }
    )
    rec.update(over)
    return rec


def _hoga_row(iso: str, seq: int, **over):
    rec = {k: "" for k in HOGA_FIELDS}
    rec.update(
        {
            "recv_ns": str(seq * 1000),
            "recv_iso": iso,
            "code": "005930",
            "real_type": "주식호가잔량",
            "epoch": "1",
            "phase": "observe",
            "seq": str(seq),
        }
    )
    for fid in HOGA_FIDS:
        rec["fid%s_raw" % fid] = "1"
        rec["fid%s_ok" % fid] = "True"
    rec.update(over)
    return rec


def _write_csv(path: str, fields, rows):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(fields), extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow(row)


def _write_jsonl(path: str, events):
    with open(path, "w", encoding="utf-8") as fh:
        for ev in events:
            fh.write(json.dumps(ev, ensure_ascii=False, separators=(",", ":")) + "\n")


def _base_stats(tick_n: int, hoga_n: int, **over):
    payload = {
        "session_id": SID,
        "session_start": START,
        "session_finished": FINISH,
        "shutdown_status": "COMPLETE",
        "bridge_status": "CLOSED",
        "screens_active": False,
        "screen_state": {"8101": "NOT_REGISTERED", "8102": "NOT_REGISTERED"},
        "tick_buffer_pending": 0,
        "orderbook_buffer_pending": 0,
        "writer_queue_pending": 0,
        "writer_inflight": 0,
        "pending_buffer": 0,
        "pending_queue": 0,
        "pending_inflight": 0,
        "pending_records": 0,
        "terminal_losses": 0,
        "write_ok": tick_n + hoga_n,
        "tick_callbacks": tick_n,
        "hoga_callbacks": hoga_n,
        "dup_suspect": 0,
        "stats_dump_ok": True,
        "files": [
            {"path": r"C:\tmp\tick_005930_20261113_observe_part001.csv", "code": "005930", "rows": tick_n},
            {"path": r"C:\tmp\orderbook_005930_20261113_observe_part001.csv", "code": "005930", "rows": hoga_n},
        ],
        "fid13_15": {"fid13_missing": 0, "fid15_missing": 0, "fid13_ok": tick_n, "fid15_ok": tick_n},
        "orig_ocx_by_method": {
            "SetRealRemove": {"probe": 0, "runtime": 2, "total": 2},
            "GetCommRealData": {"probe": 0, "runtime": 10, "total": 10},
        },
        "orig_ocx_setrealremove": {
            "8101": {"probe": 0, "runtime": 1, "total": 1},
            "8102": {"probe": 0, "runtime": 1, "total": 1},
            "other": {"probe": 0, "runtime": 0, "total": 0},
        },
    }
    payload.update(over)
    return payload


def _base_events():
    return [
        {"ts": START, "session_id": SID, "event": "SESSION_START", "status": "start"},
        {"ts": "2026-11-13T12:00:01", "session_id": SID, "event": "COLLECT_BRIDGE_ATTACHED", "status": "ok"},
        {"ts": FINISH, "session_id": SID, "event": "SESSION_FINISHED", "status": "COMPLETE"},
    ]


def _session_dir(td, *, tick_rows=None, hoga_rows=None, events=None, stats=None):
    ticks = tick_rows or [
        _tick_row("2026-11-13T12:00:01.000000", 2),
        _tick_row("2026-11-13T12:00:02.000000", 4),
    ]
    hogas = hoga_rows or [
        _hoga_row("2026-11-13T12:00:01.100000", 1),
        _hoga_row("2026-11-13T12:00:02.100000", 3),
    ]
    jsonl = os.path.join(td, "session.jsonl")
    st = os.path.join(td, "collector_stats.json")
    tick = os.path.join(td, "tick_005930_20261113_observe_part001.csv")
    hoga = os.path.join(td, "orderbook_005930_20261113_observe_part001.csv")
    _write_jsonl(jsonl, events if events is not None else _base_events())
    payload = stats if stats is not None else _base_stats(len(ticks), len(hogas))
    with open(st, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    _write_csv(tick, TICK_FIELDS, ticks)
    _write_csv(hoga, HOGA_FIELDS, hogas)
    return jsonl, st, tick, hoga


class Stage31SessionValidatorTests(unittest.TestCase):
    def _assert_unchanged(self, paths, before):
        after = _hashes(paths)
        self.assertEqual(after, before)

    def test_normal_session_pass(self):
        with tempfile.TemporaryDirectory() as td:
            paths = _session_dir(td)
            before = _hashes(paths)
            result = verify_session(*paths)
            self.assertEqual(result["verdict"], VERDICT_PASS)
            self.assertEqual(result["session_id"], SID)
            self._assert_unchanged(paths, before)

    def test_session_id_mismatch_fail(self):
        with tempfile.TemporaryDirectory() as td:
            stats = _base_stats(2, 2, session_id="s2c-otherid0000")
            paths = _session_dir(td, stats=stats)
            before = _hashes(paths)
            result = verify_session(*paths)
            self.assertEqual(result["verdict"], VERDICT_FAIL)
            names = [c["name"] for c in result["checks"] if c["status"] == "FAIL"]
            self.assertIn("stats_session_id_match", names)
            self._assert_unchanged(paths, before)

    def test_missing_session_finished_fail(self):
        with tempfile.TemporaryDirectory() as td:
            events = [e for e in _base_events() if e["event"] != "SESSION_FINISHED"]
            paths = _session_dir(td, events=events)
            before = _hashes(paths)
            result = verify_session(*paths)
            self.assertEqual(result["verdict"], VERDICT_FAIL)
            names = [c["name"] for c in result["checks"] if c["status"] == "FAIL"]
            self.assertIn("jsonl_session_finished_count", names)
            self._assert_unchanged(paths, before)

    def test_stats_dump_failure_fail(self):
        with tempfile.TemporaryDirectory() as td:
            stats = _base_stats(2, 2, stats_dump_ok=False, shutdown_status="STATS_DUMP_FAILED")
            paths = _session_dir(td, stats=stats)
            before = _hashes(paths)
            result = verify_session(*paths)
            self.assertEqual(result["verdict"], VERDICT_FAIL)
            names = [c["name"] for c in result["checks"] if c["status"] == "FAIL"]
            self.assertIn("stats_dump_ok", names)
            self.assertIn("shutdown_complete", names)
            self._assert_unchanged(paths, before)

    def test_csv_row_count_mismatch_fail(self):
        with tempfile.TemporaryDirectory() as td:
            stats = _base_stats(9, 2)
            paths = _session_dir(td, stats=stats)
            before = _hashes(paths)
            result = verify_session(*paths)
            self.assertEqual(result["verdict"], VERDICT_FAIL)
            names = [c["name"] for c in result["checks"] if c["status"] == "FAIL"]
            self.assertIn("tick_rows_vs_write_ok", names)
            self._assert_unchanged(paths, before)

    def test_seq_reversal_fail(self):
        with tempfile.TemporaryDirectory() as td:
            ticks = [
                _tick_row("2026-11-13T12:00:01.000000", 5),
                _tick_row("2026-11-13T12:00:02.000000", 3),
            ]
            paths = _session_dir(td, tick_rows=ticks)
            before = _hashes(paths)
            result = verify_session(*paths)
            self.assertEqual(result["verdict"], VERDICT_FAIL)
            names = [c["name"] for c in result["checks"] if c["status"] == "FAIL"]
            self.assertIn("tick_seq_monotonic", names)
            self._assert_unchanged(paths, before)

    def test_fid_missing_fail(self):
        with tempfile.TemporaryDirectory() as td:
            ticks = [
                _tick_row("2026-11-13T12:00:01.000000", 2, fid13_ok="False", fid13=""),
                _tick_row("2026-11-13T12:00:02.000000", 4),
            ]
            paths = _session_dir(td, tick_rows=ticks)
            before = _hashes(paths)
            result = verify_session(*paths)
            self.assertEqual(result["verdict"], VERDICT_FAIL)
            names = [c["name"] for c in result["checks"] if c["status"] == "FAIL"]
            self.assertIn("fid_missing_zero", names)
            self._assert_unchanged(paths, before)

    def test_gap_over_60s_warn(self):
        with tempfile.TemporaryDirectory() as td:
            ticks = [
                _tick_row("2026-11-13T12:00:01.000000", 2),
                _tick_row("2026-11-13T12:01:20.000000", 4),
            ]
            hogas = [
                _hoga_row("2026-11-13T12:00:01.100000", 1),
                _hoga_row("2026-11-13T12:01:20.100000", 3),
            ]
            paths = _session_dir(td, tick_rows=ticks, hoga_rows=hogas)
            before = _hashes(paths)
            result = verify_session(*paths)
            self.assertEqual(result["verdict"], VERDICT_WARN)
            warn = [c["name"] for c in result["checks"] if c["status"] == "WARN"]
            self.assertIn("gap_over_60s", warn)
            fail = [c for c in result["checks"] if c["status"] == "FAIL"]
            self.assertEqual(fail, [])
            self._assert_unchanged(paths, before)

    def test_dup_suspect_is_not_data_loss_fail(self):
        with tempfile.TemporaryDirectory() as td:
            stats = _base_stats(2, 2, dup_suspect=999, terminal_losses=0)
            paths = _session_dir(td, stats=stats)
            before = _hashes(paths)
            result = verify_session(*paths)
            self.assertEqual(result["verdict"], VERDICT_PASS)
            self.assertEqual(result["notes"]["dup_suspect"], 999)
            self.assertNotEqual(result["counts"]["terminal_losses"], result["notes"]["dup_suspect"])
            fail_names = [c["name"] for c in result["checks"] if c["status"] == "FAIL"]
            self.assertNotIn("terminal_losses_zero", fail_names)
            self._assert_unchanged(paths, before)

    def test_pilot_missing_session_id_is_insufficient(self):
        self.assertTrue(os.path.isfile(PILOT_JSONL))
        paths = (PILOT_JSONL, PILOT_STATS, PILOT_TICK, PILOT_HOGA)
        before = _hashes(paths)
        result = verify_session(*paths)
        self.assertEqual(result["verdict"], VERDICT_INSUFFICIENT)
        self.assertIn("session_id", result["insufficient_reason"])
        with open(PILOT_STATS, encoding="utf-8") as fh:
            stats = json.load(fh)
        self.assertNotIn("session_id", stats)
        self._assert_unchanged(paths, before)

    def test_sha256_unchanged_after_verify(self):
        with tempfile.TemporaryDirectory() as td:
            paths = _session_dir(td)
            before = _hashes(paths)
            verify_session(*paths)
            report = os.path.join(td, "report.json")
            rc = verify_main(["--jsonl", paths[0], "--stats", paths[1], "--tick", paths[2], "--orderbook", paths[3], "--report", report])
            self.assertEqual(rc, 0)
            self.assertTrue(os.path.isfile(report))
            self._assert_unchanged(paths, before)

    def test_microsecond_timestamps_must_match_exactly(self):
        start = "2026-11-13T12:00:00.123456"
        finish = "2026-11-13T12:05:00.654321"
        events = [
            {"ts": start, "session_id": SID, "event": "SESSION_START", "status": "start"},
            {"ts": finish, "session_id": SID, "event": "SESSION_FINISHED", "status": "COMPLETE"},
        ]
        ticks = [
            _tick_row("2026-11-13T12:00:00.200000", 2),
            _tick_row("2026-11-13T12:00:01.000000", 4),
        ]
        hogas = [
            _hoga_row("2026-11-13T12:00:00.210000", 1),
            _hoga_row("2026-11-13T12:00:01.100000", 3),
        ]
        with tempfile.TemporaryDirectory() as td:
            stats = _base_stats(2, 2, session_start=start, session_finished=finish)
            paths = _session_dir(td, tick_rows=ticks, hoga_rows=hogas, events=events, stats=stats)
            before = _hashes(paths)
            result = verify_session(*paths)
            self.assertEqual(result["verdict"], VERDICT_PASS)
            self.assertEqual(result["session_start"], start)
            self.assertEqual(result["session_finished"], finish)
            self.assertIn(".", result["session_start"])
            self.assertIn(".", result["session_finished"])
            self._assert_unchanged(paths, before)

    def test_csv_last_before_finish_is_pass(self):
        with tempfile.TemporaryDirectory() as td:
            ticks = [
                _tick_row("2026-11-13T12:04:59.000000", 2),
                _tick_row("2026-11-13T12:04:59.999999", 4),
            ]
            hogas = [
                _hoga_row("2026-11-13T12:04:59.100000", 1),
                _hoga_row("2026-11-13T12:04:59.500000", 3),
            ]
            paths = _session_dir(td, tick_rows=ticks, hoga_rows=hogas)
            before = _hashes(paths)
            result = verify_session(*paths)
            self.assertEqual(result["verdict"], VERDICT_PASS)
            self._assert_unchanged(paths, before)

    def test_csv_after_finish_fail(self):
        with tempfile.TemporaryDirectory() as td:
            ticks = [
                _tick_row("2026-11-13T12:00:01.000000", 2),
                _tick_row("2026-11-13T12:05:00.000001", 4),
            ]
            hogas = [
                _hoga_row("2026-11-13T12:00:01.100000", 1),
                _hoga_row("2026-11-13T12:04:59.000000", 3),
            ]
            paths = _session_dir(td, tick_rows=ticks, hoga_rows=hogas)
            before = _hashes(paths)
            result = verify_session(*paths)
            self.assertEqual(result["verdict"], VERDICT_FAIL)
            names = [c["name"] for c in result["checks"] if c["status"] == "FAIL"]
            self.assertIn("csv_recv_iso_in_session_window", names)
            self._assert_unchanged(paths, before)

    def test_dump_ok_null_is_not_success(self):
        with tempfile.TemporaryDirectory() as td:
            stats = _base_stats(2, 2, stats_dump_ok=None)
            paths = _session_dir(td, stats=stats)
            before = _hashes(paths)
            result = verify_session(*paths)
            self.assertEqual(result["verdict"], VERDICT_FAIL)
            names = [c["name"] for c in result["checks"] if c["status"] == "FAIL"]
            self.assertIn("stats_dump_ok", names)
            self._assert_unchanged(paths, before)

    def test_report_refuses_to_overwrite_input(self):
        with tempfile.TemporaryDirectory() as td:
            paths = _session_dir(td)
            before = _hashes(paths)
            rc = verify_main(["--jsonl", paths[0], "--stats", paths[1], "--tick", paths[2], "--orderbook", paths[3], "--report", paths[1]])
            self.assertEqual(rc, 1)
            self._assert_unchanged(paths, before)

    def test_pending_residual_fail(self):
        with tempfile.TemporaryDirectory() as td:
            stats = _base_stats(2, 2, writer_queue_pending=3, pending_records=3)
            paths = _session_dir(td, stats=stats)
            before = _hashes(paths)
            result = verify_session(*paths)
            self.assertEqual(result["verdict"], VERDICT_FAIL)
            names = [c["name"] for c in result["checks"] if c["status"] == "FAIL"]
            self.assertIn("pending_zero", names)
            self._assert_unchanged(paths, before)

    def test_terminal_losses_fail(self):
        with tempfile.TemporaryDirectory() as td:
            stats = _base_stats(2, 2, terminal_losses=4, dropped_overflow=4)
            paths = _session_dir(td, stats=stats)
            before = _hashes(paths)
            result = verify_session(*paths)
            self.assertEqual(result["verdict"], VERDICT_FAIL)
            names = [c["name"] for c in result["checks"] if c["status"] == "FAIL"]
            self.assertIn("terminal_losses_zero", names)
            self._assert_unchanged(paths, before)

    def test_setrealremove_missing_fail(self):
        with tempfile.TemporaryDirectory() as td:
            stats = _base_stats(
                2,
                2,
                orig_ocx_setrealremove={
                    "8101": {"probe": 0, "runtime": 0, "total": 0},
                    "8102": {"probe": 0, "runtime": 0, "total": 0},
                    "other": {"probe": 0, "runtime": 0, "total": 0},
                },
            )
            paths = _session_dir(td, stats=stats)
            before = _hashes(paths)
            result = verify_session(*paths)
            self.assertEqual(result["verdict"], VERDICT_FAIL)
            names = [c["name"] for c in result["checks"] if c["status"] == "FAIL"]
            self.assertIn("setrealremove_8101", names)
            self.assertIn("setrealremove_8102", names)
            self._assert_unchanged(paths, before)

    def test_sendorder_orig_fail(self):
        with tempfile.TemporaryDirectory() as td:
            stats = _base_stats(
                2,
                2,
                orig_ocx_by_method={
                    "SendOrder": {"probe": 0, "runtime": 1, "total": 1},
                    "SetRealRemove": {"probe": 0, "runtime": 2, "total": 2},
                },
            )
            paths = _session_dir(td, stats=stats)
            before = _hashes(paths)
            result = verify_session(*paths)
            self.assertEqual(result["verdict"], VERDICT_FAIL)
            names = [c["name"] for c in result["checks"] if c["status"] == "FAIL"]
            self.assertIn("sendorder_orig_zero", names)
            self._assert_unchanged(paths, before)

    def test_commterminate_orig_fail(self):
        with tempfile.TemporaryDirectory() as td:
            stats = _base_stats(
                2,
                2,
                orig_ocx_by_method={
                    "CommTerminate": {"probe": 0, "runtime": 1, "total": 1},
                    "SetRealRemove": {"probe": 0, "runtime": 2, "total": 2},
                },
            )
            paths = _session_dir(td, stats=stats)
            before = _hashes(paths)
            result = verify_session(*paths)
            self.assertEqual(result["verdict"], VERDICT_FAIL)
            names = [c["name"] for c in result["checks"] if c["status"] == "FAIL"]
            self.assertIn("commterminate_orig_zero", names)
            self._assert_unchanged(paths, before)

    def test_archive_124557_is_fail_without_rewrite(self):
        self.assertTrue(all(os.path.isfile(p) for p in ARCH_B))
        before = _hashes(ARCH_B)
        result = verify_session(*ARCH_B)
        self.assertEqual(result["verdict"], VERDICT_FAIL)
        names = [c["name"] for c in result["checks"] if c["status"] == "FAIL"]
        self.assertIn("stats_dump_ok", names)
        self.assertIn("csv_recv_iso_in_session_window", names)
        rc = verify_main(
            ["--jsonl", ARCH_B[0], "--stats", ARCH_B[1], "--tick", ARCH_B[2], "--orderbook", ARCH_B[3]]
        )
        self.assertEqual(rc, 2)
        self._assert_unchanged(ARCH_B, before)

    def test_archive_131247_is_pass_without_rewrite(self):
        self.assertTrue(all(os.path.isfile(p) for p in ARCH_C))
        before = _hashes(ARCH_C)
        result = verify_session(*ARCH_C)
        self.assertEqual(result["verdict"], VERDICT_PASS)
        rc = verify_main(
            ["--jsonl", ARCH_C[0], "--stats", ARCH_C[1], "--tick", ARCH_C[2], "--orderbook", ARCH_C[3]]
        )
        self.assertEqual(rc, 0)
        self._assert_unchanged(ARCH_C, before)


if __name__ == "__main__":
    unittest.main()
