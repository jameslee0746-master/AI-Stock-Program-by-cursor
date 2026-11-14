"""정규장 실행기 가드 테스트. 키움/Qt 없이 preflight 만 확인한다."""
import datetime
import importlib.util
import os
import subprocess
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_P = os.path.join(_ROOT, "tools", "stage05_regular_run.py")
_spec = importlib.util.spec_from_file_location("stage05_regular_run_mod", _P)
R = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(R)


class PreflightTests(unittest.TestCase):
    def test_weekday_morning_ok(self):
        ok, _, rc = R.preflight(["stage05_regular_run.py"], datetime.datetime(2026, 11, 6, 9, 5))
        self.assertTrue(ok)
        self.assertEqual(rc, 0)

    def test_offhours_rejected(self):
        ok, reason, rc = R.preflight(["stage05_regular_run.py"], datetime.datetime(2026, 11, 6, 22, 21))
        self.assertFalse(ok)
        self.assertEqual(rc, R.RC_OFFHOURS)
        self.assertIn("장외", reason)

    def test_weekend_rejected(self):
        ok, _, rc = R.preflight(["stage05_regular_run.py"], datetime.datetime(2026, 11, 7, 10, 0))
        self.assertFalse(ok)
        self.assertEqual(rc, R.RC_OFFHOURS)

    def test_force_offhours_rejected(self):
        ok, reason, rc = R.preflight(
            ["stage05_regular_run.py", "--force-offhours"], datetime.datetime(2026, 11, 6, 10, 0)
        )
        self.assertFalse(ok)
        self.assertEqual(rc, R.RC_FORBIDDEN)
        self.assertIn("force-offhours", reason)

    def test_disconnect_rejected(self):
        ok, reason, rc = R.preflight(
            ["x", "--phases", "disconnect"], datetime.datetime(2026, 11, 6, 10, 0)
        )
        self.assertFalse(ok)
        self.assertEqual(rc, R.RC_FORBIDDEN)
        self.assertIn("단절", reason)

    def test_login_check_allowed_after_close(self):
        ok, mode, rc = R.preflight(
            ["stage05_regular_run.py", "--login-check"], datetime.datetime(2026, 11, 6, 22, 41)
        )
        self.assertTrue(ok)
        self.assertEqual(mode, "login-check")
        self.assertEqual(rc, 0)

    def test_login_check_not_with_wait(self):
        ok, _, rc = R.preflight(
            ["x", "--login-check", "--login-now-wait"], datetime.datetime(2026, 11, 6, 7, 50)
        )
        self.assertFalse(ok)
        self.assertEqual(rc, R.RC_FORBIDDEN)

    def test_weekday_0700_without_flag_is_wait(self):
        ok, mode, rc = R.preflight(["stage05_regular_run.py"], datetime.datetime(2026, 11, 10, 7, 5))
        self.assertTrue(ok)
        self.assertEqual(mode, "wait")
        self.assertEqual(rc, 0)

    def test_weekday_0659_without_flag_rejected(self):
        ok, reason, rc = R.preflight(["stage05_regular_run.py"], datetime.datetime(2026, 11, 10, 6, 59))
        self.assertFalse(ok)
        self.assertEqual(rc, R.RC_OFFHOURS)
        self.assertIn("07:00", reason)

    def test_weekday_0859_without_flag_is_wait(self):
        ok, mode, rc = R.preflight(["stage05_regular_run.py"], datetime.datetime(2026, 11, 10, 8, 59))
        self.assertTrue(ok)
        self.assertEqual(mode, "wait")

    def test_login_now_wait_before_open_ok(self):
        ok, mode, rc = R.preflight(
            ["stage05_regular_run.py", "--login-now-wait"], datetime.datetime(2026, 11, 6, 7, 50)
        )
        self.assertTrue(ok)
        self.assertEqual(mode, "wait")
        self.assertEqual(rc, 0)

    def test_login_now_wait_after_close_rejected(self):
        ok, _, rc = R.preflight(
            ["stage05_regular_run.py", "--login-now-wait"], datetime.datetime(2026, 11, 6, 16, 0)
        )
        self.assertFalse(ok)
        self.assertEqual(rc, R.RC_OFFHOURS)

    def test_login_now_wait_weekend_rejected(self):
        ok, _, rc = R.preflight(
            ["stage05_regular_run.py", "--login-now-wait"], datetime.datetime(2026, 11, 7, 7, 50)
        )
        self.assertFalse(ok)

    def test_login_now_wait_still_blocks_force_offhours(self):
        ok, _, rc = R.preflight(
            ["x", "--login-now-wait", "--force-offhours"], datetime.datetime(2026, 11, 6, 7, 50)
        )
        self.assertFalse(ok)
        self.assertEqual(rc, R.RC_FORBIDDEN)

    def test_live_subprocess_offhours_does_not_login(self):
        """지금이 장외면 실제 실행기가 키움 없이 거부해야 한다."""
        p = subprocess.run(
            [sys.executable, "-X", "utf8", _P],
            cwd=_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        )
        now = datetime.datetime.now()
        hm = now.hour * 60 + now.minute
        in_session = now.weekday() < 5 and 9 * 60 <= hm < 15 * 60 + 30
        pre_open_wait = now.weekday() < 5 and 7 * 60 <= hm < 9 * 60
        if in_session:
            self.skipTest("정규장이라 실제 거부를 확인할 수 없음")
        if pre_open_wait:
            self.skipTest("07:00~09:00 는 장전 대기로 키움에 접속할 수 있어 거부 테스트를 건너뜀")
        self.assertEqual(p.returncode, R.RC_OFFHOURS)
        self.assertIn("거부", p.stdout)
        self.assertNotIn("CommConnect", p.stdout)
        self.assertNotIn("로그인 시도", p.stdout)


if __name__ == "__main__":
    unittest.main()
