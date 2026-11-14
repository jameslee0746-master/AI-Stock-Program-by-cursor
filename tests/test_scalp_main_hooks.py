"""main.py HFT 훅 검사. 실제 키움 로그인/OCX 연결 없음."""
from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from unittest import mock

from scalp.bridge import attach_bridge, attach_meta  # noqa: E402
from scalp.collector import SCREEN_HOGA, SCREEN_TICK  # noqa: E402
from tests.test_scalp_bridge import EnvGuard, FakeKiwoom, _regs  # noqa: E402


def _load_main():
    from PyQt5.QtWidgets import QApplication

    _app = QApplication.instance() or QApplication([])
    import main as m

    return m, _app


class MainSourceHookTests(unittest.TestCase):
    def setUp(self):
        path = os.path.join(ROOT, "main.py")
        with open(path, encoding="utf-8") as f:
            self.src = f.read()

    def test_hooks_present_without_product_disconnect(self):
        self.assertIn("attach_bridge(", self.src)
        self.assertIn("_scalp_try_reregister(self)", self.src)
        self.assertIn("_scalp_try_sync(self.kiwoom, self.stock_codes)", self.src)
        self.assertIn("def closeEvent(", self.src)
        self.assertIn("aboutToQuit.connect", self.src)
        self.assertNotIn("OnReceiveRealData.disconnect", self.src)
        self.assertNotIn("disconnect(self._on_receive_real_data)", self.src)
        self.assertIn('os.environ.get("SCALP_ENABLED", "")).strip() == "1"', self.src)

    def test_all_all_only_in_product_reregister(self):
        self.assertIn('SetRealRemove(QString, QString)", "ALL", "ALL"', self.src)
        self.assertEqual(self.src.count('"ALL", "ALL"'), 1)

    def test_order_screens_unchanged(self):
        self.assertIn('SCREEN_NO = "0101"', self.src)
        self.assertIn('REAL_SCREEN_NO = "9001"', self.src)
        self.assertIn('VI_REAL_SCREEN_NO = "9051"', self.src)

    def test_new_hooks_have_no_sendorder(self):
        for name in ("_scalp_try_close", "_scalp_try_sync", "_scalp_try_reregister", "attach_bridge", "ensure_bridge"):
            self.assertIn(name, self.src)
        start = self.src.find("def _scalp_log")
        end = self.src.find("def _parse_chejan_code9001") if "def _parse_chejan_code9001" in self.src else start + 4000
        chunk = self.src[start:start + 3500] if start >= 0 else ""
        self.assertNotIn("SendOrder(", chunk)
        self.assertNotIn("send_order_market", chunk)


class MainHelperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m, cls.app = _load_main()

    def test_scalp_disabled_helpers_are_noop(self):
        with EnvGuard("0"):
            k = FakeKiwoom()
            self.assertIsNone(self.m._scalp_bridge_of(k))
            self.m._scalp_try_reregister(k)
            self.m._scalp_try_sync(k, ["005930"])
            self.m._scalp_try_close(k)
            self.assertEqual(len(k.OnReceiveRealData.slots), 1)

    def test_reregister_and_sync_helpers(self):
        with EnvGuard("1"):
            k = FakeKiwoom()
            with tempfile.TemporaryDirectory() as td:
                b = attach_bridge(k, ["005930"], td)
                nreg = sum(1 for c in k.calls if str(c[0]).startswith("SetRealReg"))
                self.m._scalp_try_reregister(k)
                nreg2 = sum(1 for c in k.calls if str(c[0]).startswith("SetRealReg"))
                self.assertGreater(nreg2, nreg)
                self.m._scalp_try_sync(k, ["000660", "035420"])
                self.assertEqual(b.codes, ["000660", "035420"])
                b.close()

    def test_sync_error_does_not_propagate_or_set_shutdown_error(self):
        st = {"v": "RUNNING"}

        class Boom:
            closed = False
            stats = types.SimpleNamespace(
                shutdown_status="RUNNING",
                set_shutdown=lambda s: st.__setitem__("v", s),
            )

            def sync_targets(self, _codes):
                raise RuntimeError("sync fail")

        k = types.SimpleNamespace(_scalp_bridge=Boom())
        self.m._scalp_try_sync(k, ["005930"])
        self.assertEqual(st["v"], "RUNNING")

    def test_scalp_disabled_does_not_call_ensure(self):
        with EnvGuard("0"):
            k = FakeKiwoom()
            product = k._on_receive_real_data
            with mock.patch("scalp.bridge.ensure_bridge") as m_ensure:
                self.m._scalp_try_sync(k, ["005930"])
            m_ensure.assert_not_called()
            self.assertIn(product, k.OnReceiveRealData.slots)
            self.assertEqual(len(k.OnReceiveRealData.slots), 1)
            self.assertFalse(any(str(c[0]).startswith("SetRealReg") for c in k.calls))
            self.assertFalse(any(str(c[0]).startswith("SendOrder") for c in k.calls))

    def test_live_bridge_syncs_without_new_attach(self):
        with EnvGuard("1"):
            k = FakeKiwoom()
            product = k._on_receive_real_data
            with tempfile.TemporaryDirectory() as td:
                k._scalp_data_dir = td
                b = attach_bridge(k, ["005930"], td)
                n_tick = len(_regs(k.calls, SCREEN_TICK))
                n_hoga = len(_regs(k.calls, SCREEN_HOGA))
                with mock.patch("scalp.bridge.ensure_bridge") as m_ensure:
                    self.m._scalp_try_sync(k, ["005930"])
                m_ensure.assert_not_called()
                self.assertEqual(len(_regs(k.calls, SCREEN_TICK)), n_tick)
                self.assertEqual(len(_regs(k.calls, SCREEN_HOGA)), n_hoga)
                self.assertIn(product, k.OnReceiveRealData.slots)
                self.assertFalse(any(str(c[0]).startswith("SendOrder") for c in k.calls))
                b.close()

    def test_boot_fail_then_first_codes_retries_once(self):
        with EnvGuard("1"):
            k = FakeKiwoom()
            product = k._on_receive_real_data
            k.fail_reg_screens = {SCREEN_TICK, SCREEN_HOGA}
            with tempfile.TemporaryDirectory() as td:
                k._scalp_data_dir = td
                self.assertIsNone(attach_bridge(k, ["005930"], td))
                k.fail_reg_screens = set()
                self.m._scalp_try_sync(k, ["005930"])
                b = self.m._scalp_bridge_of(k)
                self.assertIsNotNone(b)
                self.assertTrue(attach_meta(k)["lazy_attach_attempted"])
                n_tick = len(_regs(k.calls, SCREEN_TICK))
                self.m._scalp_try_sync(k, ["005930"])
                self.assertEqual(len(_regs(k.calls, SCREEN_TICK)), n_tick)
                self.assertIn(product, k.OnReceiveRealData.slots)
                self.assertFalse(any(str(c[0]).startswith("SendOrder") for c in k.calls))
                b.close()

    def test_empty_codes_do_not_lazy_attach(self):
        with EnvGuard("1"):
            k = FakeKiwoom()
            with tempfile.TemporaryDirectory() as td:
                k._scalp_data_dir = td
                self.m._scalp_try_sync(k, [])
                self.assertFalse(attach_meta(k)["lazy_attach_attempted"])
                self.assertIsNone(self.m._scalp_bridge_of(k))
                self.assertFalse(any(str(c[0]).startswith("SetRealReg") for c in k.calls))

    def test_second_lazy_attach_forbidden_and_close_blocks(self):
        with EnvGuard("1"):
            k = FakeKiwoom()
            k.fail_reg_screens = {SCREEN_TICK}
            with tempfile.TemporaryDirectory() as td:
                k._scalp_data_dir = td
                self.m._scalp_try_sync(k, ["005930"])
                self.assertIsNone(self.m._scalp_bridge_of(k))
                self.assertTrue(attach_meta(k)["lazy_attach_attempted"])
                k.fail_reg_screens = set()
                self.m._scalp_try_sync(k, ["000660"])
                self.assertIsNone(self.m._scalp_bridge_of(k))
            k2 = FakeKiwoom()
            with tempfile.TemporaryDirectory() as td:
                k2._scalp_data_dir = td
                b = attach_bridge(k2, ["005930"], td)
                b.close()
                self.m._scalp_try_sync(k2, ["000660"])
                self.assertIsNone(self.m._scalp_bridge_of(k2))
                self.assertTrue(attach_meta(k2)["session_closed"])

    def test_reregister_error_sets_error_status(self):
        st = {"v": "RUNNING"}

        class Boom:
            stats = types.SimpleNamespace(
                shutdown_status="RUNNING",
                set_shutdown=lambda s: st.__setitem__("v", s),
            )

            def reregister_hft_screens(self):
                raise RuntimeError("hft reg fail")

        k = types.SimpleNamespace(_scalp_bridge=Boom())
        self.m._scalp_try_reregister(k)
        self.assertEqual(st["v"], "ERROR")

    def test_closeEvent_and_duplicate_close(self):
        with EnvGuard("1"):
            k = FakeKiwoom()
            with tempfile.TemporaryDirectory() as td:
                b = attach_bridge(k, ["005930"], td)
                from PyQt5.QtGui import QCloseEvent

                w = self.m.TradingWindow()
                w.kiwoom = k
                w.closeEvent(QCloseEvent())
                status = b.stats.shutdown_status
                self.m._scalp_try_close(k)
                self.m._scalp_try_close(k)
                self.assertEqual(b.stats.shutdown_status, status)
                self.assertNotEqual(status, "RUNNING")
                w.deleteLater()
                self.assertFalse(any(str(c[0]).startswith("SendOrder") for c in k.calls))


if __name__ == "__main__":
    unittest.main()
