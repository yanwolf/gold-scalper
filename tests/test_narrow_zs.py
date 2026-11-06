"""
r117 的測試：中樞突破只做窄中樞(中樞高度 < N 倍 ATR，只在回測)。
執行：python -m unittest tests.test_narrow_zs -v
"""
import logging
import os
import unittest
from unittest import mock

logging.disable(logging.CRITICAL)

from app import backtest as B  # noqa: E402
from tests.test_split_entry import kl, BREAK, NEUTRAL  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = [4002.0] * 30 + [4003.0] * 30 + [3990.0] * 30     # 進場後最後跌破 5 點停損：有進場就會有一筆平倉


def run(signal, max_h):
    calls = {"n": 0}

    def sig(*a, **k):
        calls["n"] += 1
        return dict(signal) if calls["n"] == 1 else dict(NEUTRAL)
    with mock.patch.object(B, "compute_signal_from_trades", side_effect=sig), \
         mock.patch.object(B, "fetch_historical_klines", return_value=[]):
        r = B.run_backtest(days=1, interval_seconds=900, klines=kl(PATH), daily_klines=[], trend_filter_mode=0,
                           use_chop_filter=False, block_market_closed=False, min_atr_points=0, use_atr=False,
                           sl_points=5.0, trail_trigger_points=50, trail_distance_points=50, reversal_confirm_count=3,
                           max_zs_height_atr=max_h)
    return r, calls["n"]


WIDE = BREAK                                                     # 中樞 3990～4000，ATR 2 → 高度 5 倍 ATR
NARROW = dict(BREAK, chan_detail={"latest_zhongshu": {"zg": 4000.0, "zd": 3999.0}})   # 高度 0.5 倍 ATR
BEICHI = dict(BREAK, chan={"reason": "下跌段出現盤整背馳，動能較前一段減弱，留意反彈向上"})


class NarrowZs(unittest.TestCase):
    def test_wide_breakout_is_skipped(self):
        r, n = run(WIDE, 1.0)
        self.assertGreater(n, 3, "前提：每一步都有算訊號")
        self.assertEqual(r["total_trades"], 0)
        self.assertEqual(r["skipped_zs_height"], 1)
        self.assertEqual(r["max_zs_height_atr"], 1.0)

    def test_control_without_limit_enters(self):
        r, _ = run(WIDE, None)
        self.assertEqual(r["total_trades"], 1, "對照：不設上限就進場")
        self.assertEqual((r["max_zs_height_atr"], r["skipped_zs_height"]), (0.0, 0))
        r2, _ = run(WIDE, 6.0)
        self.assertEqual(r2["total_trades"], 1, "上限比中樞高度大：照進")

    def test_narrow_breakout_enters(self):
        r, _ = run(NARROW, 1.0)
        self.assertEqual(r["total_trades"], 1)
        self.assertAlmostEqual(r["recent_trades"][0]["diag"]["zs_height_atr"], 0.5)

    def test_beichi_not_affected(self):
        r, _ = run(BEICHI, 1.0)
        self.assertEqual(r["total_trades"], 1, "背馳進場不受中樞高度限制")
        self.assertEqual(r["recent_trades"][0]["diag"]["kind"], "beichi")

    def test_no_atr_counts_as_not_narrow(self):
        r, _ = run(dict(NARROW, atr=None), 1.0)
        self.assertEqual(r["total_trades"], 0, "算不出高度：不當成窄中樞")
        self.assertEqual(r["skipped_zs_height"], 1)

    def test_other_strategies_ignore_it(self):
        from tests.test_pullback_resume import _run, BAND_PATH
        res = _run(path=BAND_PATH, pr_mode=2, pr_exit_mode=1, pr_sides=1, max_zs_height_atr=1.0)
        self.assertGreater(res["total_trades"], 0, "前提：有交易")
        self.assertEqual(res["max_zs_height_atr"], 0.0)

    def test_wiring(self):
        with open(os.path.join(ROOT, "app/static/dashboard.html"), encoding="utf-8") as f:
            h = f.read()
        self.assertIn("addIfPresent('max_zs_height_atr'", h)
        self.assertIn("r.skipped_zs_height", h)
        from app import backtest_jobs as J, main
        self.assertIn("max_zs_height_atr", J.LABEL_PARAM_KEYS)
        self.assertIn("max_zs_height_atr", main._backtest_job_params())
        self.assertEqual(J._window_row(0, {"total_trades": 0, "skipped_zs_height": 7}, 0, 1)["skipped_zs_height"], 7)


if __name__ == "__main__":
    unittest.main()
