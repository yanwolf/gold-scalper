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


from app import paper_trading as pt  # noqa: E402
from app import settings as S  # noqa: E402
from app import trade_diagnostics as TD  # noqa: E402


class SharedRule(unittest.TestCase):
    def test_allows(self):
        self.assertEqual(TD.narrow_zs_allows("chan_profile", WIDE, 0), (True, None), "0＝關閉")
        ok, note = TD.narrow_zs_allows("chan_profile", WIDE, 1.0)
        self.assertEqual(ok, False)
        self.assertIn("5.00 倍 ATR", note)
        self.assertEqual(TD.narrow_zs_allows("chan_profile", NARROW, 1.0), (True, None))
        self.assertEqual(TD.narrow_zs_allows("chan_profile", BEICHI, 1.0), (True, None), "背馳不受影響")
        self.assertEqual(TD.narrow_zs_allows("band_touch", WIDE, 1.0), (True, None), "別的策略不受影響")
        ok2, note2 = TD.narrow_zs_allows("chan_profile", dict(NARROW, atr=None), 1.0)
        self.assertEqual(ok2, False, "算不出高度：不當成窄中樞")
        self.assertIn("算不出", note2)

    def test_setting(self):
        meta = S.FIELD_META["paper_max_zs_height_atr"]
        self.assertEqual((meta["type"], meta["min"]), ("float", 0))
        self.assertEqual(float(meta["default"]), 0.0, "預設不限制：既有引擎行為不變")
        self.assertIn("paper_max_zs_height_atr", S.TRADING_RELEVANT_KEYS, "改了要重新分界績效統計")

    def test_backtest_defaults_to_engine_setting(self):
        real = B.settings_module.get_settings
        with mock.patch.object(B.settings_module, "get_settings",
                               side_effect=lambda *a, **k: dict(real(*a, **k), paper_max_zs_height_atr=1.0)):
            r, _ = run(WIDE, None)
        self.assertEqual(r["max_zs_height_atr"], 1.0, "沒填就沿用正式設定")
        self.assertEqual(r["total_trades"], 0)
        with mock.patch.object(B.settings_module, "get_settings",
                               side_effect=lambda *a, **k: dict(real(*a, **k), paper_max_zs_height_atr=1.0)):
            r2, _ = run(WIDE, 0)
        self.assertEqual(r2["total_trades"], 1, "填 0：明確關閉，蓋過正式設定")


class LiveEngine(unittest.TestCase):
    def setUp(self):
        self.eng = pt.PaperTradingEngine(interval_seconds=900, label="測試纏論", engine_id="chan_zs_test", execution_index=98)
        self.eng._seeded_from_db = True
        real = pt.settings_module.get_settings
        self.over = {"paper_trend_filter_mode": 0, "paper_use_chop_filter": 0, "paper_min_atr_points": 0}
        self.ps = [
            mock.patch.object(pt.settings_module, "settings_loaded", return_value=True),
            mock.patch.object(pt.settings_module, "get_settings", side_effect=lambda *a, **k: dict(real(*a, **k), **self.over)),
            mock.patch.object(pt.trading_core, "is_gold_market_closed", return_value=(False, "")),
            mock.patch.object(pt.notifier_module.notifier, "notify_trade_event"),
            mock.patch.object(pt.db, "load_closed_paper_trades", return_value=(True, [])),
        ]
        for p in self.ps:
            p.start()

    def tearDown(self):
        for p in self.ps:
            p.stop()

    def tick(self, sig):
        r = dict(sig, strategy_type="chan_profile", current_price=4002.0, bid=4001.9, ask=4002.1, book_stale=None,
                 trend_filter={"direction": None})
        with mock.patch.object(pt, "compute_full_signal", return_value=r):
            self.eng._tick()

    def test_off_by_default_opens_wide(self):
        self.tick(WIDE)
        self.assertIsNotNone(self.eng._position, "設定 0：寬中樞照開(既有行為不變)")

    def test_setting_blocks_wide_and_counts(self):
        self.over["paper_max_zs_height_atr"] = 1.0
        self.tick(WIDE)
        self.assertIsNone(self.eng._position)
        self.assertEqual(self.eng._zs_skips, 1)
        with mock.patch.object(pt.db, "is_enabled", return_value=False):
            self.assertEqual(self.eng.get_summary()["zs_skips"], 1)

    def test_setting_allows_narrow_and_beichi(self):
        self.over["paper_max_zs_height_atr"] = 1.0
        self.tick(NARROW)
        self.assertIsNotNone(self.eng._position, "窄中樞照開")
        self.eng._position = None
        self.tick(BEICHI)
        self.assertIsNotNone(self.eng._position, "背馳不受影響")

    def test_dashboard_settings_wiring(self):
        with open(os.path.join(ROOT, "app/static/dashboard.html"), encoding="utf-8") as f:
            h = f.read()
        self.assertIn("paper_max_zs_height_atr: 'backtestMaxZsHeight'", h)
        self.assertIn("paper_max_zs_height_atr: data.max_zs_height_atr", h)
        self.assertIn("keys: ['paper_max_zs_height_atr']", h)
        self.assertIn("data.zs_skips", h)
