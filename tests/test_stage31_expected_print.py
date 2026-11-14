"""Stage3.1.7 Fake: 주식체결과 주식예상체결 분리. OCX/main.py 없음. 기존 CSV 수정 없음."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scalp.bridge import attach_bridge  # noqa: E402
from scalp.collector.orderbook_collector import OrderbookCollector  # noqa: E402
from scalp.collector.realtime_buffer import RealtimeBuffer  # noqa: E402
from scalp.collector.session import CollectSession  # noqa: E402
from scalp.collector.stats import CollectorStats  # noqa: E402
from scalp.collector.tick_collector import TickCollector  # noqa: E402
from tests.test_scalp_bridge import EnvGuard, FakeKiwoom  # noqa: E402
from tests.test_scalp_collector import FidMap, _tick_env  # noqa: E402
from tests.test_scalp_ops_shutdown import _tick_fids  # noqa: E402


def krx_session_bucket(iso: str) -> str:
    """검증기용 시장 구간 분류안. 제품 코드에 아직 넣지 않는다."""
    dt = datetime.fromisoformat(str(iso or "").strip())
    hhmm = dt.hour * 100 + dt.minute
    if hhmm < 900:
        return "PREOPEN_AUCTION"
    if hhmm < 1520:
        return "REGULAR"
    if hhmm < 1530:
        return "CLOSING_AUCTION"
    return "CLOSE_AND_AFTER"


def _gap_kind(prev_iso: str, next_iso: str) -> str:
    a = datetime.fromisoformat(prev_iso)
    b = datetime.fromisoformat(next_iso)
    sec = (b - a).total_seconds()
    if sec < 60:
        return "NONE"
    overlap_auction = a < datetime(a.year, a.month, a.day, 15, 30) and b > datetime(
        a.year, a.month, a.day, 15, 20
    )
    if overlap_auction and krx_session_bucket(prev_iso) in ("REGULAR", "CLOSING_AUCTION"):
        if krx_session_bucket(next_iso) in ("CLOSING_AUCTION", "CLOSE_AND_AFTER"):
            return "CLOSING_AUCTION_NO_PRINT"
    return "UNEXPLAINED_GAP"


class ExpectedPrintIsolationTests(unittest.TestCase):
    def test_stock_print_is_kept(self):
        col, buf, stats, _s, _g = _tick_env()
        rec = col.handle("005930", "주식체결")
        self.assertIsNotNone(rec)
        self.assertEqual(rec["real_type"], "주식체결")
        self.assertEqual(len(buf), 1)
        self.assertEqual(stats.tick_callbacks, 1)

    def test_expected_print_is_dropped_by_tick_collector(self):
        col, buf, stats, _s, _g = _tick_env()
        rec = col.handle("005930", "주식예상체결")
        self.assertIsNone(rec)
        self.assertEqual(len(buf), 0)
        self.assertEqual(stats.tick_callbacks, 0)
        self.assertEqual(stats.callback_total, 0)

    def test_expected_print_does_not_mix_into_tick_buffer(self):
        col, buf, stats, _s, _g = _tick_env()
        col.handle("005930", "주식체결")
        col.handle("005930", "주식예상체결")
        col.handle("005930", "주식체결")
        rows = buf.peek_all()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["real_type"] == "주식체결" for r in rows))
        self.assertEqual(stats.tick_callbacks, 2)

    def test_bridge_ignores_expected_print_and_keeps_hoga(self):
        with EnvGuard("1"):
            kiwoom = FakeKiwoom()
            with tempfile.TemporaryDirectory() as td:
                b = attach_bridge(kiwoom, ["005930"], td)
                kiwoom.OnReceiveRealData.emit("005930", "주식체결", "")
                kiwoom.OnReceiveRealData.emit("005930", "주식예상체결", "")
                kiwoom.OnReceiveRealData.emit("005930", "주식호가잔량", "")
                self.assertEqual(b.stats.tick_callbacks, 1)
                self.assertEqual(b.stats.hoga_callbacks, 1)
                self.assertEqual(len(b.tick_buffer), 1)
                self.assertEqual(len(b.hoga_buffer), 1)
                self.assertEqual(b.tick_buffer.peek_all()[0]["real_type"], "주식체결")
                self.assertEqual(b.hoga_buffer.peek_all()[0]["real_type"], "주식호가잔량")
                b.close()

    def test_missing_fid_on_print_is_flagged_not_dropped(self):
        col, buf, stats, _s, _g = _tick_env(fids={10: "70000", 20: "090000", 228: "1"})
        rec = col.handle("005930", "주식체결")
        self.assertEqual(len(buf), 1)
        self.assertFalse(rec["fid13_ok"])
        self.assertFalse(rec["fid15_ok"])
        self.assertEqual(rec["real_type"], "주식체결")

    def test_seq_continues_on_hoga_while_expected_is_ignored(self):
        stats = CollectorStats()
        tb, hb = RealtimeBuffer(maxlen=50), RealtimeBuffer(maxlen=50)
        sess = CollectSession()
        sess.begin()
        tick = TickCollector(FidMap(_tick_fids()), tb, stats, sess)
        hoga = OrderbookCollector(FidMap({}), hb, stats, sess)
        tick.handle("005930", "주식체결")
        tick.handle("005930", "주식예상체결")
        hoga.handle("005930", "주식호가잔량")
        tick.handle("005930", "주식체결")
        trows = tb.peek_all()
        hrows = hb.peek_all()
        self.assertEqual([int(r["seq"]) for r in trows], [1, 3])
        self.assertEqual([int(r["seq"]) for r in hrows], [2])
        self.assertEqual(stats.tick_callbacks, 2)
        self.assertEqual(stats.hoga_callbacks, 1)


class MarketBucketDesignTests(unittest.TestCase):
    def test_buckets(self):
        self.assertEqual(krx_session_bucket("2026-11-13T08:50:00"), "PREOPEN_AUCTION")
        self.assertEqual(krx_session_bucket("2026-11-13T09:00:00"), "REGULAR")
        self.assertEqual(krx_session_bucket("2026-11-13T15:19:59"), "REGULAR")
        self.assertEqual(krx_session_bucket("2026-11-13T15:20:00"), "CLOSING_AUCTION")
        self.assertEqual(krx_session_bucket("2026-11-13T15:29:59"), "CLOSING_AUCTION")
        self.assertEqual(krx_session_bucket("2026-11-13T15:30:00"), "CLOSE_AND_AFTER")

    def test_closing_auction_gap_is_not_unexplained(self):
        kind = _gap_kind("2026-11-13T15:19:58.107301", "2026-11-13T15:30:02.391477")
        self.assertEqual(kind, "CLOSING_AUCTION_NO_PRINT")

    def test_midday_60s_gap_is_unexplained(self):
        kind = _gap_kind("2026-11-13T11:00:00", "2026-11-13T11:02:00")
        self.assertEqual(kind, "UNEXPLAINED_GAP")


if __name__ == "__main__":
    unittest.main()
