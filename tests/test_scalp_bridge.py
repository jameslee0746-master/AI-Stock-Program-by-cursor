"""HFT Bridge 단위 테스트. 키움 OCX 불필요. main.py 미연결."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scalp.bridge import (  # noqa: E402
    ENV_SCALP,
    MAX_CODES,
    ST_ACTIVE,
    ST_ERROR,
    ScalpBridge,
    attach_bridge,
    attach_meta,
    ensure_bridge,
    scalp_enabled,
    select_codes,
)
from scalp.collector.stats import (  # noqa: E402
    BRIDGE_CLOSED,
    BRIDGE_DEGRADED,
    BRIDGE_ERROR,
    BRIDGE_RUNNING,
    CollectorStats,
    ERROR_HISTORY_MAX,
)
from scalp.collector import HOGA_FIDS_STR, SCREEN_HOGA, SCREEN_TICK, TICK_FIDS  # noqa: E402


class FakeSignal:
    def __init__(self) -> None:
        self.slots = []

    def connect(self, fn) -> None:
        self.slots.append(fn)

    def disconnect(self, fn) -> None:
        self.slots.remove(fn)

    def emit(self, *a) -> None:
        for fn in list(self.slots):
            fn(*a)


class FakeKiwoom:
    def __init__(self) -> None:
        self.OnReceiveRealData = FakeSignal()
        self.calls = []
        self.fids = {
            10: "70000",
            13: "100",
            15: "3",
            20: "090000",
            228: "50",
            21: "090000",
        }
        self.product_got = []
        self.fail_reg_screens = set()
        self.fail_unreg_screens = set()
        self.OnReceiveRealData.connect(self._on_receive_real_data)

    def _on_receive_real_data(self, code, real_type, real_data=""):
        self.product_got.append((str(code), str(real_type)))

    def dynamicCall(self, name, *args):
        self.calls.append((str(name),) + tuple(args))
        if str(name).startswith("SetRealReg") and args and str(args[0]) in self.fail_reg_screens:
            raise RuntimeError("reg fail %s" % (args[0],))
        if str(name).startswith("SetRealRemove") and args and str(args[0]) in self.fail_unreg_screens:
            raise RuntimeError("unreg fail %s" % (args[0],))
        if str(name).startswith("GetCommRealData"):
            fid = args[1] if len(args) > 1 else None
            try:
                fid_i = int(fid)
            except (TypeError, ValueError):
                fid_i = fid
            return str(self.fids.get(fid_i, self.fids.get(fid, "")))
        return 0


class EnvGuard:
    def __init__(self, value=None) -> None:
        self.value = value
        self._old = os.environ.get(ENV_SCALP)

    def __enter__(self):
        if self.value is None:
            os.environ.pop(ENV_SCALP, None)
        else:
            os.environ[ENV_SCALP] = self.value
        return self

    def __exit__(self, *exc):
        if self._old is None:
            os.environ.pop(ENV_SCALP, None)
        else:
            os.environ[ENV_SCALP] = self._old


def _regs(calls, screen):
    return [
        c for c in calls
        if str(c[0]).startswith("SetRealReg") and len(c) > 1 and str(c[1]) == screen
    ]


def _rems(calls):
    return [c for c in calls if str(c[0]).startswith("SetRealRemove")]


class ScalpEnabledTests(unittest.TestCase):
    def test_unset_is_disabled(self):
        with EnvGuard(None):
            self.assertFalse(scalp_enabled())
            kiwoom = FakeKiwoom()
            with tempfile.TemporaryDirectory() as td:
                b = attach_bridge(kiwoom, ["005930"], td)
            self.assertIsNone(b)
            self.assertEqual(len(kiwoom.OnReceiveRealData.slots), 1)
            self.assertEqual(_regs(kiwoom.calls, SCREEN_TICK), [])

    def test_zero_is_disabled(self):
        with EnvGuard("0"):
            self.assertFalse(scalp_enabled())
            kiwoom = FakeKiwoom()
            with tempfile.TemporaryDirectory() as td:
                b = attach_bridge(kiwoom, ["005930"], td)
            self.assertIsNone(b)
            self.assertEqual(len(kiwoom.OnReceiveRealData.slots), 1)
            self.assertFalse(any(str(c[0]).startswith("SetRealReg") for c in kiwoom.calls))

    def test_one_is_enabled(self):
        with EnvGuard("1"):
            self.assertTrue(scalp_enabled())
            kiwoom = FakeKiwoom()
            with tempfile.TemporaryDirectory() as td:
                b = attach_bridge(kiwoom, ["005930"], td)
                self.assertIsInstance(b, ScalpBridge)
                self.assertTrue(b.attached)
                b.close()


class SlotAndDispatchTests(unittest.TestCase):
    def test_product_handler_kept_and_collector_added(self):
        with EnvGuard("1"):
            kiwoom = FakeKiwoom()
            product = kiwoom._on_receive_real_data
            with tempfile.TemporaryDirectory() as td:
                b = attach_bridge(kiwoom, ["005930"], td)
                slots = kiwoom.OnReceiveRealData.slots
                self.assertIn(product, slots)
                self.assertIn(b._slot, slots)
                self.assertEqual(len(slots), 2)
                b.close()

    def test_duplicate_attach_prevented(self):
        with EnvGuard("1"):
            kiwoom = FakeKiwoom()
            with tempfile.TemporaryDirectory() as td:
                a = attach_bridge(kiwoom, ["005930"], td)
                b = attach_bridge(kiwoom, ["000660"], td)
                self.assertIs(a, b)
                self.assertEqual(len(kiwoom.OnReceiveRealData.slots), 2)
                a.close()

    def test_tick_and_hoga_branch_and_fid_read(self):
        with EnvGuard("1"):
            kiwoom = FakeKiwoom()
            with tempfile.TemporaryDirectory() as td:
                b = attach_bridge(kiwoom, ["005930"], td)
                kiwoom.OnReceiveRealData.emit("005930", "주식체결", "")
                kiwoom.OnReceiveRealData.emit("005930", "주식호가잔량", "")
                kiwoom.OnReceiveRealData.emit("005930", "주식예상체결", "")
                self.assertEqual(b.stats.tick_callbacks, 1)
                self.assertEqual(b.stats.hoga_callbacks, 1)
                self.assertEqual(len(b.tick_buffer), 1)
                rec = b.tick_buffer.peek_all()[0]
                self.assertEqual(rec["fid10_raw"], "70000")
                self.assertEqual(rec["fid13_raw"], "100")
                self.assertEqual(rec["fid15_raw"], "3")
                self.assertEqual(rec["fid20_raw"], "090000")
                self.assertEqual(rec["fid228_raw"], "50")
                self.assertEqual(kiwoom.product_got[0], ("005930", "주식체결"))
                self.assertGreaterEqual(len(b.dup_diag), 1)
                b.close()

    def test_collector_exception_isolated(self):
        with EnvGuard("1"):
            kiwoom = FakeKiwoom()
            with tempfile.TemporaryDirectory() as td:
                b = attach_bridge(kiwoom, ["005930"], td)

                def boom(*_a, **_k):
                    raise RuntimeError("collector boom")

                b.tick.handle = boom  # type: ignore
                kiwoom.OnReceiveRealData.emit("005930", "주식체결", "")
                self.assertEqual(kiwoom.product_got[-1], ("005930", "주식체결"))
                self.assertGreaterEqual(b.stats.handler_exceptions, 1)
                b.close()


class ScreenRegTests(unittest.TestCase):
    def test_register_8101_8102_and_remove_only_those(self):
        with EnvGuard("1"):
            kiwoom = FakeKiwoom()
            with tempfile.TemporaryDirectory() as td:
                b = attach_bridge(kiwoom, ["005930", "000660"], td)
                r1 = _regs(kiwoom.calls, SCREEN_TICK)
                r2 = _regs(kiwoom.calls, SCREEN_HOGA)
                self.assertEqual(len(r1), 1)
                self.assertEqual(len(r2), 1)
                self.assertEqual(r1[0][3], TICK_FIDS)
                self.assertEqual(r2[0][3], HOGA_FIDS_STR)
                self.assertIn("005930", r1[0][2])
                b.close()
                rems = _rems(kiwoom.calls)
                self.assertTrue(rems)
                for c in rems:
                    self.assertIn(str(c[1]), (SCREEN_TICK, SCREEN_HOGA))
                    self.assertNotEqual(str(c[1]), "ALL")

    def test_all_all_never_called(self):
        with EnvGuard("1"):
            kiwoom = FakeKiwoom()
            with tempfile.TemporaryDirectory() as td:
                b = attach_bridge(kiwoom, ["005930"], td)
                b.sync_targets(["000660"])
                b.reregister_hft_screens()
                b.close()
            for c in kiwoom.calls:
                if str(c[0]).startswith("SetRealRemove"):
                    self.assertNotEqual((str(c[1]), str(c[2])), ("ALL", "ALL"))
            with open(os.path.join(ROOT, "scalp", "bridge.py"), encoding="utf-8") as f:
                src = f.read()
            self.assertNotIn('SetRealRemove(QString, QString)", "ALL", "ALL"', src)
            self.assertNotIn('("ALL", "ALL")', src)

    def test_reregister_after_product_all_all(self):
        with EnvGuard("1"):
            kiwoom = FakeKiwoom()
            with tempfile.TemporaryDirectory() as td:
                b = attach_bridge(kiwoom, ["005930"], td)
                kiwoom.dynamicCall("SetRealRemove(QString, QString)", "ALL", "ALL")
                before = len(_regs(kiwoom.calls, SCREEN_TICK))
                b.reregister_hft_screens()
                after = len(_regs(kiwoom.calls, SCREEN_TICK))
                self.assertGreater(after, before)
                last = _regs(kiwoom.calls, SCREEN_TICK)[-1]
                self.assertEqual(last[1], SCREEN_TICK)
                last_h = _regs(kiwoom.calls, SCREEN_HOGA)[-1]
                self.assertEqual(last_h[1], SCREEN_HOGA)
                b.close()

    def test_sync_targets(self):
        with EnvGuard("1"):
            kiwoom = FakeKiwoom()
            with tempfile.TemporaryDirectory() as td:
                b = attach_bridge(kiwoom, ["005930"], td)
                b.sync_targets(["000660", "035420"])
                self.assertEqual(b.codes, ["000660", "035420"])
                last = _regs(kiwoom.calls, SCREEN_TICK)[-1]
                self.assertIn("000660", last[2])
                self.assertNotIn("005930", last[2])
                b.close()

    def test_max_five_codes(self):
        with EnvGuard("1"):
            kiwoom = FakeKiwoom()
            many = [f"{i:06d}" for i in range(1, 12)]
            logs: list = []
            with tempfile.TemporaryDirectory() as td:
                b = attach_bridge(kiwoom, many, td, on_log=logs.append)
                self.assertEqual(len(b.codes), MAX_CODES)
                self.assertEqual(b.codes, [f"{i:06d}" for i in range(1, 6)])
                self.assertEqual(b.excluded_codes, [f"{i:06d}" for i in range(6, 12)])
                joined = " ".join(logs)
                self.assertIn("000001", joined)
                self.assertIn("000006", joined)
                self.assertIn("excluded=", joined)
                b.close()


class RollbackAndErrorTests(unittest.TestCase):
    def test_init_failure_rolls_back_and_keeps_product(self):
        with EnvGuard("1"):
            kiwoom = FakeKiwoom()
            product = kiwoom._on_receive_real_data

            def boom(name, *args):
                kiwoom.calls.append((str(name),) + tuple(args))
                if str(name).startswith("SetRealReg"):
                    raise RuntimeError("reg fail")
                if str(name).startswith("GetCommRealData"):
                    return "1"
                return 0

            kiwoom.dynamicCall = boom
            with tempfile.TemporaryDirectory() as td:
                raw = ScalpBridge(kiwoom, ["005930"], td)
                self.assertFalse(raw.attach())
                self.assertEqual(raw.stats.bridge_status, BRIDGE_ERROR)
                self.assertEqual(raw.stats.shutdown_status, "INCOMPLETE")
                self.assertNotEqual(raw.stats.shutdown_status, "COMPLETE")
                self.assertFalse(attach_meta(kiwoom)["session_closed"])
                b = attach_bridge(kiwoom, ["005930"], td)
            self.assertIsNone(b)
            self.assertIsNone(getattr(kiwoom, "_scalp_bridge", None))
            self.assertIn(product, kiwoom.OnReceiveRealData.slots)
            self.assertEqual(len(kiwoom.OnReceiveRealData.slots), 1)
            rems = _rems(kiwoom.calls)
            self.assertTrue(rems)
            for c in rems:
                self.assertIn(str(c[1]), (SCREEN_TICK, SCREEN_HOGA))

    def test_select_codes_order_and_cap(self):
        sel, exc = select_codes(
            ["005930", "bad", "005930", "000660", "035420", "051910", "068270", "035720"]
        )
        self.assertEqual(sel, ["005930", "000660", "035420", "051910", "068270"])
        self.assertEqual(exc, ["035720"])


class ScreenRecoveryTests(unittest.TestCase):
    def test_same_codes_sync_skips_when_active(self):
        with EnvGuard("1"):
            kiwoom = FakeKiwoom()
            with tempfile.TemporaryDirectory() as td:
                b = attach_bridge(kiwoom, ["005930"], td)
                n_tick = len(_regs(kiwoom.calls, SCREEN_TICK))
                n_hoga = len(_regs(kiwoom.calls, SCREEN_HOGA))
                b.sync_targets(["005930"])
                self.assertEqual(len(_regs(kiwoom.calls, SCREEN_TICK)), n_tick)
                self.assertEqual(len(_regs(kiwoom.calls, SCREEN_HOGA)), n_hoga)
                self.assertEqual(b.screen_state[SCREEN_TICK], ST_ACTIVE)
                self.assertEqual(b.screen_state[SCREEN_HOGA], ST_ACTIVE)
                b.close()

    def test_reregister_fail_same_codes_sync_retries(self):
        with EnvGuard("1"):
            kiwoom = FakeKiwoom()
            with tempfile.TemporaryDirectory() as td:
                b = attach_bridge(kiwoom, ["005930"], td)
                kiwoom.fail_reg_screens = {SCREEN_TICK, SCREEN_HOGA}
                with self.assertRaises(RuntimeError):
                    b.reregister_hft_screens()
                self.assertEqual(b.screen_state[SCREEN_TICK], ST_ERROR)
                self.assertEqual(b.screen_state[SCREEN_HOGA], ST_ERROR)
                self.assertFalse(b.screens_active)
                n_tick = len(_regs(kiwoom.calls, SCREEN_TICK))
                kiwoom.fail_reg_screens = set()
                b.sync_targets(["005930"])
                self.assertGreater(len(_regs(kiwoom.calls, SCREEN_TICK)), n_tick)
                self.assertEqual(b.screen_state[SCREEN_TICK], ST_ACTIVE)
                self.assertEqual(b.screen_state[SCREEN_HOGA], ST_ACTIVE)
                self.assertTrue(b.screens_active)
                b.close()

    def test_hoga_fail_retries_only_hoga(self):
        with EnvGuard("1"):
            kiwoom = FakeKiwoom()
            with tempfile.TemporaryDirectory() as td:
                b = attach_bridge(kiwoom, ["005930"], td)
                kiwoom.fail_reg_screens = {SCREEN_HOGA}
                with self.assertRaises(RuntimeError):
                    b.reregister_hft_screens()
                self.assertEqual(b.screen_state[SCREEN_TICK], ST_ACTIVE)
                self.assertEqual(b.screen_state[SCREEN_HOGA], ST_ERROR)
                self.assertFalse(b.screens_active)
                n_tick = len(_regs(kiwoom.calls, SCREEN_TICK))
                n_hoga = len(_regs(kiwoom.calls, SCREEN_HOGA))
                kiwoom.fail_reg_screens = set()
                b.sync_targets(["005930"])
                self.assertEqual(len(_regs(kiwoom.calls, SCREEN_TICK)), n_tick)
                self.assertGreater(len(_regs(kiwoom.calls, SCREEN_HOGA)), n_hoga)
                self.assertEqual(b.screen_state[SCREEN_TICK], ST_ACTIVE)
                self.assertEqual(b.screen_state[SCREEN_HOGA], ST_ACTIVE)
                b.close()

    def test_both_fail_then_retry_succeeds_active(self):
        with EnvGuard("1"):
            kiwoom = FakeKiwoom()
            with tempfile.TemporaryDirectory() as td:
                b = attach_bridge(kiwoom, ["005930"], td)
                kiwoom.fail_reg_screens = {SCREEN_TICK, SCREEN_HOGA}
                with self.assertRaises(RuntimeError):
                    b.reregister_hft_screens()
                self.assertFalse(b.screens_active)
                kiwoom.fail_reg_screens = set()
                b.sync_targets(["005930"])
                self.assertEqual(b.screen_state[SCREEN_TICK], ST_ACTIVE)
                self.assertEqual(b.screen_state[SCREEN_HOGA], ST_ACTIVE)
                self.assertTrue(b.screens_active)
                b.close()

    def test_failure_is_not_reported_active(self):
        with EnvGuard("1"):
            kiwoom = FakeKiwoom()
            with tempfile.TemporaryDirectory() as td:
                b = attach_bridge(kiwoom, ["005930"], td)
                kiwoom.fail_reg_screens = {SCREEN_HOGA}
                with self.assertRaises(RuntimeError):
                    b.reregister_hft_screens()
                self.assertEqual(b.screen_state[SCREEN_HOGA], ST_ERROR)
                self.assertNotEqual(b.screen_state[SCREEN_HOGA], ST_ACTIVE)
                self.assertFalse(b.screens_active)
                self.assertEqual(b.stats.bridge_status, BRIDGE_DEGRADED)
                self.assertEqual(b.stats.shutdown_status, "RUNNING")
                b.close()

    def test_bridge_never_calls_all_all(self):
        with EnvGuard("1"):
            kiwoom = FakeKiwoom()
            with tempfile.TemporaryDirectory() as td:
                b = attach_bridge(kiwoom, ["005930"], td)
                kiwoom.fail_reg_screens = {SCREEN_HOGA}
                try:
                    b.reregister_hft_screens()
                except RuntimeError:
                    pass
                kiwoom.fail_reg_screens = set()
                b.sync_targets(["005930"])
                b.close()
            for c in kiwoom.calls:
                if str(c[0]).startswith("SetRealRemove"):
                    self.assertNotEqual((str(c[1]), str(c[2])), ("ALL", "ALL"))
            with open(os.path.join(ROOT, "scalp", "bridge.py"), encoding="utf-8") as f:
                src = f.read()
            self.assertNotIn('SetRealRemove(QString, QString)", "ALL", "ALL"', src)

    def test_close_then_sync_does_not_register(self):
        with EnvGuard("1"):
            kiwoom = FakeKiwoom()
            with tempfile.TemporaryDirectory() as td:
                b = attach_bridge(kiwoom, ["005930"], td)
                b.close()
                n_tick = len(_regs(kiwoom.calls, SCREEN_TICK))
                n_hoga = len(_regs(kiwoom.calls, SCREEN_HOGA))
                b.sync_targets(["005930"])
                b.sync_targets(["000660"])
                self.assertEqual(len(_regs(kiwoom.calls, SCREEN_TICK)), n_tick)
                self.assertEqual(len(_regs(kiwoom.calls, SCREEN_HOGA)), n_hoga)


class CloseAndSafetyTests(unittest.TestCase):
    def test_close_idempotent_and_writer(self):
        with EnvGuard("1"):
            kiwoom = FakeKiwoom()
            with tempfile.TemporaryDirectory() as td:
                b = attach_bridge(kiwoom, ["005930"], td)
                kiwoom.OnReceiveRealData.emit("005930", "주식체결", "")
                b.close()
                status = b.stats.shutdown_status
                nrem = len(_rems(kiwoom.calls))
                b.close()
                b.close()
                self.assertEqual(len(_rems(kiwoom.calls)), nrem)
                self.assertEqual(b.stats.shutdown_status, status)
                self.assertNotEqual(status, "RUNNING")
                if status == "COMPLETE":
                    self.assertTrue(b.stats.identity_ok())
                else:
                    self.assertNotEqual(status, "COMPLETE")
                self.assertTrue(os.path.isfile(os.path.join(td, "collector_stats.json")))
                self.assertTrue(b.writer._closed or b.stats.shutdown_status == "INCOMPLETE")
                self.assertIn(kiwoom._on_receive_real_data, kiwoom.OnReceiveRealData.slots)
                self.assertNotIn(b._slot, kiwoom.OnReceiveRealData.slots)
                self.assertFalse(any(str(c[0]).startswith("SendOrder") for c in kiwoom.calls))

    def test_no_order_paths_in_bridge_source(self):
        with open(os.path.join(ROOT, "scalp", "bridge.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn("SendOrder(", src)
        self.assertNotIn("send_order_market", src)
        self.assertNotIn("cancel_order", src)
        self.assertNotIn("guarded_send_order", src)


class EnsureBridgeTests(unittest.TestCase):
    def test_existing_bridge_not_recreated(self):
        with EnvGuard("1"):
            kiwoom = FakeKiwoom()
            with tempfile.TemporaryDirectory() as td:
                a = attach_bridge(kiwoom, ["005930"], td)
                b = ensure_bridge(kiwoom, ["000660"], td)
                self.assertIs(a, b)
                self.assertEqual(a.codes, ["005930"])
                a.close()

    def test_boot_fail_then_first_codes_retries(self):
        with EnvGuard("1"):
            kiwoom = FakeKiwoom()
            kiwoom.fail_reg_screens = {SCREEN_TICK, SCREEN_HOGA}
            with tempfile.TemporaryDirectory() as td:
                first = attach_bridge(kiwoom, ["005930"], td)
                self.assertIsNone(first)
                meta = attach_meta(kiwoom)
                self.assertGreaterEqual(meta["attach_attempt_count"], 1)
                self.assertEqual(meta["attach_status"], "ERROR")
                self.assertTrue(meta["last_attach_error"])
                self.assertFalse(meta["session_closed"])
                kiwoom.fail_reg_screens = set()
                b = ensure_bridge(kiwoom, ["005930"], td)
                self.assertIsInstance(b, ScalpBridge)
                self.assertTrue(b.screens_active)
                self.assertTrue(attach_meta(kiwoom)["lazy_attach_attempted"])
                b.close()

    def test_empty_codes_do_not_consume_lazy(self):
        with EnvGuard("1"):
            kiwoom = FakeKiwoom()
            with tempfile.TemporaryDirectory() as td:
                self.assertIsNone(ensure_bridge(kiwoom, [], td))
                self.assertFalse(attach_meta(kiwoom)["lazy_attach_attempted"])
                b = ensure_bridge(kiwoom, ["005930"], td)
                self.assertIsInstance(b, ScalpBridge)
                self.assertTrue(attach_meta(kiwoom)["lazy_attach_attempted"])
                b.close()

    def test_lazy_fail_blocks_second_retry(self):
        with EnvGuard("1"):
            kiwoom = FakeKiwoom()
            kiwoom.fail_reg_screens = {SCREEN_TICK}
            with tempfile.TemporaryDirectory() as td:
                self.assertIsNone(ensure_bridge(kiwoom, ["005930"], td))
                self.assertTrue(attach_meta(kiwoom)["lazy_attach_attempted"])
                nreg = len(_regs(kiwoom.calls, SCREEN_TICK))
                kiwoom.fail_reg_screens = set()
                again = ensure_bridge(kiwoom, ["005930"], td)
                again2 = ensure_bridge(kiwoom, ["000660"], td)
                self.assertIsNone(again)
                self.assertIsNone(again2)
                self.assertEqual(len(_regs(kiwoom.calls, SCREEN_TICK)), nreg)

    def test_two_ensure_calls_create_once(self):
        with EnvGuard("1"):
            kiwoom = FakeKiwoom()
            with tempfile.TemporaryDirectory() as td:
                a = ensure_bridge(kiwoom, ["005930"], td)
                count = attach_meta(kiwoom)["attach_attempt_count"]
                b = ensure_bridge(kiwoom, ["000660", "035420"], td)
                self.assertIs(a, b)
                self.assertEqual(attach_meta(kiwoom)["attach_attempt_count"], count)
                a.close()

    def test_rollback_allows_ensure_retry(self):
        with EnvGuard("1"):
            kiwoom = FakeKiwoom()
            kiwoom.fail_reg_screens = {SCREEN_HOGA}
            with tempfile.TemporaryDirectory() as td:
                self.assertIsNone(attach_bridge(kiwoom, ["005930"], td))
                self.assertFalse(attach_meta(kiwoom)["session_closed"])
                kiwoom.fail_reg_screens = set()
                b = ensure_bridge(kiwoom, ["005930"], td)
                self.assertIsInstance(b, ScalpBridge)
                self.assertEqual(b.stats.bridge_status, BRIDGE_RUNNING)
                b.close()

    def test_close_blocks_ensure(self):
        with EnvGuard("1"):
            kiwoom = FakeKiwoom()
            with tempfile.TemporaryDirectory() as td:
                b = attach_bridge(kiwoom, ["005930"], td)
                b.close()
                self.assertTrue(attach_meta(kiwoom)["session_closed"])
                self.assertIsNone(ensure_bridge(kiwoom, ["000660"], td))
                self.assertEqual(b.stats.bridge_status, BRIDGE_CLOSED)

    def test_scalp_disabled_ensure_is_noop(self):
        with EnvGuard("0"):
            kiwoom = FakeKiwoom()
            with tempfile.TemporaryDirectory() as td:
                self.assertIsNone(ensure_bridge(kiwoom, ["005930"], td))
                self.assertEqual(len(kiwoom.OnReceiveRealData.slots), 1)
                self.assertFalse(any(str(c[0]).startswith("SetRealReg") for c in kiwoom.calls))

    def test_main_py_gates_scalp_import(self):
        path = os.path.join(ROOT, "main.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        self.assertIn('os.environ.get("SCALP_ENABLED", "")).strip() == "1"', src)
        self.assertIn("from scalp.bridge import attach_bridge", src)


class StatusSeparationTests(unittest.TestCase):
    def test_one_screen_fail_is_degraded_not_shutdown_error(self):
        with EnvGuard("1"):
            kiwoom = FakeKiwoom()
            with tempfile.TemporaryDirectory() as td:
                b = attach_bridge(kiwoom, ["005930"], td)
                kiwoom.fail_reg_screens = {SCREEN_HOGA}
                with self.assertRaises(RuntimeError):
                    b.reregister_hft_screens()
                self.assertEqual(b.stats.bridge_status, BRIDGE_DEGRADED)
                self.assertEqual(b.stats.shutdown_status, "RUNNING")
                err = b.last_error
                self.assertTrue(err)
                kiwoom.fail_reg_screens = set()
                b.sync_targets(["005930"])
                self.assertEqual(b.stats.bridge_status, BRIDGE_RUNNING)
                self.assertEqual(b.last_error, err)
                self.assertTrue(b.stats.error_history)
                self.assertEqual(b.stats.shutdown_status, "RUNNING")
                b.close()
                self.assertEqual(b.stats.shutdown_status, "COMPLETE")
                self.assertEqual(b.stats.bridge_status, BRIDGE_CLOSED)

    def test_error_history_capped_at_32(self):
        stats = CollectorStats()
        for i in range(40):
            stats.record_error("screen_reg", "e%d" % i, screen="8101")
        self.assertEqual(len(stats.error_history), ERROR_HISTORY_MAX)
        self.assertEqual(stats.last_error, "e39")
        self.assertEqual(stats.error_history[0]["message"], "e8")

    def test_rollback_not_complete_and_normal_close_is(self):
        with EnvGuard("1"):
            kiwoom = FakeKiwoom()
            kiwoom.fail_reg_screens = {SCREEN_TICK}
            with tempfile.TemporaryDirectory() as td:
                raw = ScalpBridge(kiwoom, ["005930"], td)
                self.assertFalse(raw.attach())
                self.assertEqual(raw.stats.bridge_status, BRIDGE_ERROR)
                self.assertEqual(raw.stats.shutdown_status, "INCOMPLETE")
                self.assertNotEqual(raw.stats.shutdown_status, "COMPLETE")
            kiwoom2 = FakeKiwoom()
            with tempfile.TemporaryDirectory() as td:
                b = attach_bridge(kiwoom2, ["005930"], td)
                b.close()
                b.close()
                self.assertEqual(b.stats.shutdown_status, "COMPLETE")
                self.assertEqual(b.stats.bridge_status, BRIDGE_CLOSED)


if __name__ == "__main__":
    unittest.main()
