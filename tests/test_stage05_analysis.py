"""0.5단계 하네스의 순수 분석 함수 테스트(키움/Qt 불필요). 실제 키움 검증 결과가 아니다."""
import datetime
import importlib.util
import os
import unittest

_P = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "stage05_kiwoom_verify.py")
_spec = importlib.util.spec_from_file_location("stage05_verify_mod", _P)
S = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(S)


def ev(t, wall, f10="70000", f20="090000", f228="100.0"):
    return {"t": t, "wall": wall, "fid10": f10, "fid20": f20, "fid228": f228}


class AnalysisTests(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(S.analyze_ticks([])["count"], 0)

    def test_gap_and_fid20(self):
        evs = [ev(0, 32400, f20="090000"), ev(1, 32401, f20="090001"), ev(15, 32415, f20="090015"), ev(16, 32416, f20="090014")]
        r = S.analyze_ticks(evs, gap_warn_sec=10)
        self.assertEqual(r["count"], 4)
        self.assertEqual(r["gap_max"], 14.0)
        self.assertEqual(len(r["gaps_over_warn"]), 1)
        self.assertEqual(r["fid20_backwards"], 1)
        self.assertEqual(r["fid20_jumps_over_warn"], 1)
        self.assertEqual(r["fid20_bad_format"], 0)

    def test_bad_fid20_and_delay(self):
        evs = [ev(0, 32402, f20="090000"), ev(1, 32403, f20="xx")]
        r = S.analyze_ticks(evs)
        self.assertEqual(r["fid20_bad_format"], 1)
        self.assertEqual(r["fid20_valid_format"], 1)
        self.assertEqual(r["delay_median"], 2.0)

    def test_parse_hhmmss(self):
        self.assertEqual(S.parse_hhmmss("093015"), 9 * 3600 + 30 * 60 + 15)
        self.assertEqual(S.parse_hhmmss("09:30:15"), 9 * 3600 + 30 * 60 + 15)
        self.assertIsNone(S.parse_hhmmss("256000"))
        self.assertIsNone(S.parse_hhmmss(""))

    def test_hoga(self):
        e = {"fids": {f: "1" for f in S.HOGA_FIDS}}
        e2 = {"fids": {f: "" for f in S.HOGA_FIDS}}
        r = S.analyze_hoga([e, e2])
        self.assertEqual(r["per_fid_nonempty"][41], 1)

    def test_reg_ok(self):
        self.assertTrue(S.reg_ok(0))
        self.assertTrue(S.reg_ok("0"))
        self.assertFalse(S.reg_ok(-100))
        self.assertIsNone(S.reg_ok(None))

    def test_session(self):
        self.assertTrue(S.is_regular_session(datetime.datetime(2026, 11, 4, 10, 0)))   # 수
        self.assertFalse(S.is_regular_session(datetime.datetime(2026, 11, 4, 23, 0)))
        self.assertFalse(S.is_regular_session(datetime.datetime(2026, 11, 7, 10, 0)))  # 토

    def test_harness_never_calls_order_functions(self):
        with open(_P, encoding="utf-8") as f:
            src = f.read()
        for bad in ("send_order_market(", "cancel_order(", "guarded_send_order(", "dynamicCall(\"SendOrder"):
            self.assertNotIn(bad, src.replace("# ", ""), bad)


class RealSlotGuardTests(unittest.TestCase):
    def _product(self):
        return lambda *a: "product"

    def _harness(self):
        return lambda *a: "harness"

    def test_restore_after_normal_attach(self):
        product, harness = self._product(), self._harness()
        sig = S.FakeSignal()
        sig.connect(product)
        g = S.RealSlotGuard(sig, product)
        ok, msg = g.attach(harness)
        self.assertTrue(ok, msg)
        self.assertEqual(sig.slots, [harness])
        ok_r, msgs = g.restore()
        self.assertTrue(ok_r, msgs)
        self.assertEqual(sig.slots, [product])
        self.assertIn("제품 핸들러 재연결", "".join(msgs))

    def test_restore_on_exception_path(self):
        product, harness = self._product(), self._harness()
        sig = S.FakeSignal()
        sig.connect(product)
        g = S.RealSlotGuard(sig, product)
        g.attach(harness)
        with self.assertRaises(RuntimeError):
            try:
                raise RuntimeError("observe boom")
            finally:
                ok_r, msgs = g.restore()
                self.assertTrue(ok_r, msgs)
        self.assertEqual(sig.slots, [product])
        self.assertNotIn(harness, sig.slots)

    def test_duplicate_connect_refused_when_product_disconnect_fails(self):
        product, harness = self._product(), self._harness()

        class StickyProduct(S.FakeSignal):
            def disconnect(self, fn):
                if fn is product:
                    raise RuntimeError("cannot disconnect product")
                return super().disconnect(fn)

        sig = StickyProduct()
        sig.connect(product)
        g = S.RealSlotGuard(sig, product)
        ok, msg = g.attach(harness)
        self.assertFalse(ok)
        self.assertIn("disconnect 실패", msg)
        self.assertEqual(sig.slots, [product])
        self.assertNotIn(harness, sig.slots)

    def test_restore_does_not_reconnect_if_product_already_connected(self):
        product, harness = self._product(), self._harness()
        sig = S.FakeSignal()
        sig.connect(product)
        g = S.RealSlotGuard(sig, product)
        g._product_ours = False
        ok_r, msgs = g.restore()
        self.assertTrue(ok_r, msgs)
        self.assertEqual(sig.slots, [product])
        self.assertEqual(sig.slots.count(product), 1)
        self.assertIn("재연결 생략", "".join(msgs))

    def test_attach_skips_second_connect(self):
        product, harness = self._product(), self._harness()
        sig = S.FakeSignal()
        sig.connect(product)
        g = S.RealSlotGuard(sig, product)
        self.assertTrue(g.attach(harness)[0])
        ok, msg = g.attach(harness)
        self.assertTrue(ok, msg)
        self.assertEqual(sig.slots, [harness])
        self.assertEqual(sig.slots.count(harness), 1)

    def test_restore_failure_is_reported(self):
        product, harness = self._product(), self._harness()

        class NoReconnect(S.FakeSignal):
            def __init__(self):
                super().__init__()
                self.block_product = False

            def connect(self, fn):
                if self.block_product and fn is product:
                    raise RuntimeError("cannot restore")
                return super().connect(fn)

        sig = NoReconnect()
        sig.connect(product)
        g = S.RealSlotGuard(sig, product)
        self.assertTrue(g.attach(harness)[0])
        sig.block_product = True
        ok_r, msgs = g.restore()
        self.assertFalse(ok_r)
        self.assertTrue(any("재연결 실패" in m for m in msgs), msgs)


class SetRealLogTests(unittest.TestCase):
    def test_setrealremove_json_uses_registration_fids(self):
        parsed = S.parse_setreal_args("SetRealRemove(QString, QString)", ["9001", "005930"])
        self.assertEqual(parsed["screen"], "9001")
        self.assertEqual(parsed["code"], "005930")
        self.assertEqual(parsed["fids"], "")
        screens = {"9001": {"fids": {10, 20, 228}, "real_type": "0"}}
        fids = parsed["fids"] or S.fids_set_to_str(screens["9001"]["fids"])
        entry = S.make_setreal_entry(
            "SetRealRemove",
            screen=parsed["screen"],
            code=parsed["code"],
            fids=fids,
            real_type=str(screens["9001"]["real_type"]),
            ret=0,
            args=["9001", "005930"],
        )
        for key in ("at", "screen", "code", "fids", "real_type", "ret"):
            self.assertIn(key, entry)
        self.assertEqual(entry["screen"], "9001")
        self.assertEqual(entry["code"], "005930")
        self.assertEqual(entry["fids"], "10;20;228")
        self.assertEqual(entry["real_type"], "0")
        self.assertEqual(entry["ret"], 0)
        self.assertEqual(entry["args"], ["9001", "005930"])

    def test_setrealreg_structured_fields_keep_args(self):
        parsed = S.parse_setreal_args(
            "SetRealReg(QString, QString, QString, QString)",
            ["5202", "005930", "21;41;51", "0"],
        )
        entry = S.make_setreal_entry(
            "SetRealReg",
            screen=parsed["screen"],
            code=parsed["code"],
            fids=parsed["fids"],
            real_type=parsed["real_type"],
            ret=0,
            args=["5202", "005930", "21;41;51", "0"],
        )
        self.assertEqual(entry["screen"], "5202")
        self.assertEqual(entry["fids"], "21;41;51")
        self.assertEqual(entry["real_type"], "0")
        self.assertEqual(entry["args"][0], "5202")

    def test_guard_and_remove_share_make_setreal_entry_keys(self):
        with open(_P, encoding="utf-8") as f:
            src = f.read()
        self.assertIn("setrealremove_log", src)
        self.assertIn("make_setreal_entry", src)
        self.assertIn("try:", src)
        self.assertIn("restore_product_real_slot()", src)
        self.assertIn("os._exit", src)
        exit_at = src.rfind("os._exit")
        restore_at = src.rfind("_RESTORE_HOOK")
        self.assertGreater(exit_at, 0)
        self.assertGreater(restore_at, 0)
        self.assertLess(src.find("if _RESTORE_HOOK is not None"), exit_at)


if __name__ == "__main__":
    unittest.main()
