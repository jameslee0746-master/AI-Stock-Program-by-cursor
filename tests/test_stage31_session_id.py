"""Stage3.1 session_id 일관성. Fake 전용. main.py/OCX/로그인/수집 없음."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scalp.bridge import attach_bridge  # noqa: E402
from scalp.collector.stats import CollectorStats  # noqa: E402
from tests.test_scalp_bridge import EnvGuard  # noqa: E402
from tests.test_stage2c_safe_runner import (  # noqa: E402
    ENV_COLLECT_ONLY,
    ENV_LOGIN_ONLY,
    ENV_SAFE,
    EnvIsolatedTestCase,
    PreparedKiwoom,
    _WatchTimer,
    _audit_names,
)
from tools.stage2c_login_only_audit import load_events, new_session_id  # noqa: E402
from tools.stage2c_safe_runner import bind_watch_timer, prepare_api, shutdown_session  # noqa: E402


def _load_stats(td):
    path = os.path.join(td, "collector_stats.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _collect_env():
    os.environ[ENV_SAFE] = "1"
    os.environ[ENV_COLLECT_ONLY] = "1"
    os.environ.pop(ENV_LOGIN_ONLY, None)


class SessionIdConsistencyTests(EnvIsolatedTestCase):
    def _prepare_collect(self):
        _collect_env()
        api = PreparedKiwoom()
        sess = prepare_api(api)
        return api, sess

    def test_jsonl_start_finished_and_stats_share_one_session_id(self):
        api, sess = self._prepare_collect()
        with EnvGuard("1"):
            with tempfile.TemporaryDirectory() as td:
                b = attach_bridge(api, ["005930"], td)
                self.assertIsNotNone(b)
                api._scalp_bridge = b
                t = _WatchTimer()
                bind_watch_timer(sess, t)
                shutdown_session(sess, terminal="COMPLETE")
                events = load_events(sess.audit.path)
                start = [e for e in events if e["event"] == "SESSION_START"][0]
                fin = [e for e in events if e["event"] == "SESSION_FINISHED"][0]
                d = _load_stats(td)
                sid = sess.audit.session_id
                self.assertTrue(sid.startswith("s2c-"))
                self.assertEqual(start["session_id"], sid)
                self.assertEqual(fin["session_id"], sid)
                self.assertEqual(d["session_id"], sid)
                self.assertEqual(start["session_id"], fin["session_id"])
                self.assertEqual(d["session_start"], start["ts"])
                self.assertEqual(d["session_finished"], fin["ts"])
                self.assertEqual(d["shutdown_status"], "COMPLETE")
                self.assertEqual(d["bridge_status"], "CLOSED")
                keys = list(d.keys())[:5]
                self.assertEqual(
                    keys,
                    [
                        "session_id",
                        "session_start",
                        "session_finished",
                        "shutdown_status",
                        "bridge_status",
                    ],
                )

    def test_missing_session_id_is_not_session_finished(self):
        api, sess = self._prepare_collect()
        with EnvGuard("1"):
            with tempfile.TemporaryDirectory() as td:
                b = attach_bridge(api, ["005930"], td)
                api._scalp_bridge = b
                sess.audit.session_id = ""
                b.stats.session_id = ""
                t = _WatchTimer()
                bind_watch_timer(sess, t)
                shutdown_session(sess, terminal="COMPLETE")
                names = _audit_names(sess)
                self.assertIn("SESSION_INCOMPLETE", names)
                self.assertNotIn("SESSION_FINISHED", names)
                d = _load_stats(td)
                self.assertNotEqual(d["shutdown_status"], "COMPLETE")

    def test_stats_dump_failure_is_not_session_finished(self):
        api, sess = self._prepare_collect()
        with EnvGuard("1"):
            with tempfile.TemporaryDirectory() as td:
                b = attach_bridge(api, ["005930"], td)
                api._scalp_bridge = b
                b.stats.dump_json = lambda path: False  # type: ignore
                t = _WatchTimer()
                bind_watch_timer(sess, t)
                shutdown_session(sess, terminal="COMPLETE")
                names = _audit_names(sess)
                self.assertIn("SESSION_INCOMPLETE", names)
                self.assertNotIn("SESSION_FINISHED", names)
                self.assertEqual(b.stats.shutdown_status, "STATS_DUMP_FAILED")
                self.assertNotEqual(b.stats.shutdown_status, "COMPLETE")

    def test_does_not_migrate_existing_stats_file(self):
        api, sess = self._prepare_collect()
        with EnvGuard("1"):
            with tempfile.TemporaryDirectory() as td:
                old_dir = os.path.join(td, "old")
                os.makedirs(old_dir)
                old_path = os.path.join(old_dir, "collector_stats.json")
                original = {"write_ok": 9, "shutdown_status": "COMPLETE"}
                with open(old_path, "w", encoding="utf-8") as f:
                    json.dump(original, f)
                new_dir = os.path.join(td, "new")
                b = attach_bridge(api, ["005930"], new_dir)
                api._scalp_bridge = b
                t = _WatchTimer()
                bind_watch_timer(sess, t)
                shutdown_session(sess, terminal="COMPLETE")
                with open(old_path, encoding="utf-8") as f:
                    leftover = json.load(f)
                self.assertEqual(leftover, original)
                self.assertNotIn("session_id", leftover)
                dumped = _load_stats(new_dir)
                self.assertEqual(dumped["session_id"], sess.audit.session_id)

    def test_stats_does_not_mint_session_id(self):
        stats = CollectorStats()
        stats.bind_session("")
        d = stats.as_dict()
        self.assertEqual(d["session_id"], "")
        self.assertEqual(d["session_start"], "")
        import inspect
        import scalp.collector.stats as stmod

        src = inspect.getsource(stmod)
        self.assertNotIn("uuid", src)
        self.assertNotIn("s2c-", src)

    def test_one_generator_is_audit_start_session(self):
        a = new_session_id()
        b = new_session_id()
        self.assertTrue(a.startswith("s2c-"))
        self.assertNotEqual(a, b)
        api, sess = self._prepare_collect()
        self.assertEqual(sess.audit.session_id, load_events(sess.audit.path)[0]["session_id"])

    def test_jsonl_and_stats_timestamps_are_microseconds_and_equal(self):
        api, sess = self._prepare_collect()
        start = [e for e in load_events(sess.audit.path) if e["event"] == "SESSION_START"][0]
        self.assertRegex(start["ts"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}$")
        with EnvGuard("1"):
            with tempfile.TemporaryDirectory() as td:
                b = attach_bridge(api, ["005930"], td)
                api._scalp_bridge = b
                t = _WatchTimer()
                bind_watch_timer(sess, t)
                shutdown_session(sess, terminal="COMPLETE")
                events = load_events(sess.audit.path)
                start = [e for e in events if e["event"] == "SESSION_START"][0]
                fin = [e for e in events if e["event"] == "SESSION_FINISHED"][0]
                d = _load_stats(td)
                self.assertEqual(d["session_start"], start["ts"])
                self.assertEqual(d["session_finished"], fin["ts"])
                self.assertRegex(d["session_start"], r"\.\d{6}$")
                self.assertRegex(d["session_finished"], r"\.\d{6}$")
                self.assertEqual(d["stats_dump_ok"], True)

    def test_session_finished_after_writer_close(self):
        api, sess = self._prepare_collect()
        with EnvGuard("1"):
            with tempfile.TemporaryDirectory() as td:
                b = attach_bridge(api, ["005930"], td)
                api._scalp_bridge = b
                t = _WatchTimer()
                bind_watch_timer(sess, t)
                shutdown_session(sess, terminal="COMPLETE")
                d = _load_stats(td)
                trace = d["shutdown_trace"]
                self.assertLess(trace.index("writer_close"), trace.index("session_finished"))
                self.assertLess(trace.index("session_finished"), trace.index("stats_dump"))
                self.assertTrue(b.writer._closed)
                self.assertTrue(d["session_finished"])

    def test_dump_json_records_success_in_file(self):
        stats = CollectorStats()
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "collector_stats.json")
            self.assertTrue(stats.dump_json(path))
            with open(path, encoding="utf-8") as f:
                dumped = json.load(f)
            self.assertIs(dumped["stats_dump_ok"], True)
            self.assertIs(stats.stats_dump_ok, True)


if __name__ == "__main__":
    unittest.main()
