"""
r116 的測試：分批進場(只在回測、只對纏論中樞突破)與「進場當下特徵」診斷。
執行：python -m unittest tests.test_split_entry -v
"""
import logging
import os
import unittest
from datetime import datetime, timezone
from unittest import mock

logging.disable(logging.CRITICAL)

from app import backtest as B  # noqa: E402
from app import trade_diagnostics as D  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
T0 = 1_780_000_000_000 - (1_780_000_000_000 % 900_000)   # 對齊 15 分
BREAK = {"stage": "訊號", "direction": "bullish", "chan": {"reason": "價格站上中樞上界 4000.00，突破確立"},
         "chan_detail": {"latest_zhongshu": {"zg": 4000.0, "zd": 3990.0}}, "profile": {"reason": "t"},
         "atr": 2.0, "choppiness_index": None, "emas": None}
NEUTRAL = {"stage": "中性", "direction": None, "chan": {"reason": "t"}, "profile": {"reason": "t"},
           "atr": 2.0, "choppiness_index": None, "emas": None}


def sp(sign=1, level=4000.0, atr=2.0, extreme=4002.0, deadline=10_000):
    return {"sign": sign, "level": level, "atr": atr, "extreme": extreme, "deadline": deadline, "half_weight": 0.5}


class Decision(unittest.TestCase):
    def test_add_when_retest_holds_and_bounces(self):
        self.assertEqual(B.split_add_decision(sp(extreme=4000.2), 4001.0, 0), "add")

    def test_wait_until_bounce(self):
        self.assertIsNone(B.split_add_decision(sp(extreme=4000.2), 4000.5, 0), "還沒彈回 0.3 ATR")
        self.assertIsNone(B.split_add_decision(sp(extreme=4001.5), 4003.0, 0), "還沒回抽到邊界附近")
        self.assertEqual(B.split_add_decision(sp(extreme=4000.2), 4001.0, 0), "add", "對照：彈回了就加")

    def test_deep_retest_invalidates(self):
        self.assertEqual(B.split_add_decision(sp(extreme=3998.9), 4001.0, 0), "invalidated")
        self.assertEqual(B.split_add_decision(sp(extreme=3999.1), 4001.0, 0), "add", "對照：沒超過 0.5 ATR 還算")

    def test_expired(self):
        self.assertEqual(B.split_add_decision(sp(extreme=4003.0, deadline=5), 4004.0, 5), "expired")

    def test_short_mirror(self):
        self.assertEqual(B.split_add_decision(sp(sign=-1, level=4000.0, extreme=3999.8), 3999.0, 0), "add")
        self.assertEqual(B.split_add_decision(sp(sign=-1, level=4000.0, extreme=4001.1), 3999.0, 0), "invalidated")


def kl(prices, start=T0):
    return [[start + i * 60_000, str(p), str(p + 0.05), str(p - 0.05), str(p), "1", start + (i + 1) * 60_000 - 1]
            for i, p in enumerate(prices)]


def ramp(a, b, n):
    return [a + (b - a) * (i + 1) / n for i in range(n)]


def run(prices, split, sl=5.0):
    calls = {"n": 0}

    def sig(*a, **k):
        calls["n"] += 1
        return dict(BREAK) if calls["n"] == 1 else dict(NEUTRAL)
    with mock.patch.object(B, "compute_signal_from_trades", side_effect=sig), \
         mock.patch.object(B, "fetch_historical_klines", return_value=[]):
        r = B.run_backtest(days=1, interval_seconds=900, klines=kl(prices), daily_klines=[], trend_filter_mode=0,
                           use_chop_filter=False, block_market_closed=False, min_atr_points=0, use_atr=False,
                           sl_points=sl, trail_trigger_points=50, trail_distance_points=50, reversal_confirm_count=3,
                           split_entry=split)
    return r, calls["n"]


class Backtest(unittest.TestCase):
    # 第 30 分鐘那一步突破進場(4002)；回抽到 4000.2；彈回 4001；再跌到 3999
    ADD_PATH = [4002.0] * 30 + ramp(4002.0, 4000.2, 15) + ramp(4000.2, 4001.0, 15) + ramp(4001.0, 3999.0, 30)

    def test_adds_second_half_then_tight_stop(self):
        r, n = run(self.ADD_PATH, 1)
        self.assertGreater(n, 3, "前提：每一步都有算訊號")
        self.assertEqual(r["split_stats"]["added"], 1)
        self.assertEqual(r["total_trades"], 1, "加碼後停損收緊到回抽低點外，跌下來就出場")
        t = r["recent_trades"][0]
        self.assertAlmostEqual(t["entry_price"], (4002.0 + 4001.0) / 2, places=6, msg="兩批各半的均價")
        self.assertNotIn("size_weight", t, "加滿了＝全倉")
        self.assertAlmostEqual(t["split_added"]["price"], 4001.0, places=6)
        ext = t["split_added"]["extreme"]
        self.assertAlmostEqual(t["exit_price"], ext - 0.2 * 2.0, places=6)
        self.assertEqual(t["exit_reason"], "觸及停損")

    def test_without_split_full_entry_and_wide_stop(self):
        r, n = run(self.ADD_PATH, 0)
        self.assertGreater(n, 3, "前提：每一步都有算訊號")
        self.assertEqual(r["split_entry"], 0)
        self.assertIsNone(r["split_stats"])
        self.assertEqual(r["total_trades"], 0, "對照：原本的 5 點停損沒被碰到，還在場內")

    def test_deep_retest_keeps_half(self):
        path = [4002.0] * 30 + ramp(4002.0, 3998.5, 15) + ramp(3998.5, 3996.0, 15) + [3996.0] * 15
        r, _ = run(path, 1)
        self.assertEqual(r["split_stats"]["invalidated"], 1)
        self.assertEqual(r["total_trades"], 1, "前提：最後停損出場")
        t = r["recent_trades"][0]
        self.assertEqual(t["size_weight"], 0.5)
        self.assertAlmostEqual(t["pnl_points"], -5.0 * 0.5, places=6, msg="只虧半倉")

    def test_other_strategies_ignore_it(self):
        from tests.test_pullback_resume import _run, BAND_PATH
        res = _run(path=BAND_PATH, pr_mode=2, pr_exit_mode=1, pr_sides=1, split_entry=1)
        self.assertGreater(res["total_trades"], 0, "前提：有交易")
        self.assertEqual(res["split_entry"], 0)


class Features(unittest.TestCase):
    def test_entry_meta_features(self):
        m = B.trade_diagnostics.entry_meta("chan_profile", BREAK, 2.0, 4.0, price=4003.0, direction="bullish")
        self.assertEqual((m["kind"], m["break_atr"], m["zs_height_atr"]), ("breakout", 1.5, 5.0))
        short = dict(BREAK, chan={"reason": "價格跌破中樞下界 3990.00，破壞確立"})
        m2 = D.entry_meta("chan_profile", short, 2.0, 4.0, price=3989.0, direction="bearish")
        self.assertEqual((m2["level"], m2["break_atr"]), (3990.0, 0.5), "空單：往下離邊界越遠越大")

    def test_momentum_uses_only_bars_before_entry(self):
        prices = ramp(3996.0, 4002.0, 60) + [3980.0] * 200
        t = {"direction": "bullish", "entry_price": 4002.0, "pnl_points": 1.0,
             "entry_time": datetime.fromtimestamp((T0 + 60 * 60_000 - 1) / 1000, tz=timezone.utc).isoformat(),
             "exit_time": datetime.fromtimestamp((T0 + 70 * 60_000) / 1000, tz=timezone.utc).isoformat(),
             "diag": {"kind": "breakout", "level": 4000.0, "atr": 2.0, "sl_points": 4.0}}
        D.annotate([t], kl(prices))
        self.assertGreater(t["diag"]["momentum_atr"], 2.5, "進場前 1 小時漲了約 6 點＝3 倍 ATR，之後的大跌不算進來")
        self.assertLess(t["diag"]["momentum_atr"], 3.2)

    def test_feature_buckets(self):
        def tr(b, retest, pnl):
            return {"pnl_points": pnl, "diag": {"kind": "breakout", "break_atr": b}, "diag_retest": retest}
        groups = D.feature_buckets([tr(0.1, True, -5), tr(0.2, True, -4), tr(1.5, False, 9), tr(0.25, False, 2)])
        self.assertEqual(len(groups), 3, "前提：三個特徵")
        rows = {r["label"]: r for r in groups[0]["rows"]}
        self.assertEqual(rows["0.25~0.5"]["trades"], 1, "剛好 0.25：算進「0.25~0.5」(下界含、上界不含)")
        self.assertEqual((rows["<0.25"]["trades"], rows["<0.25"]["retest_pct"], rows["<0.25"]["total_pnl_points"]), (2, 100.0, -9.0))
        self.assertEqual((rows["1~2"]["trades"], rows["1~2"]["retest_pct"]), (1, 0.0))
        self.assertEqual(rows["≥2"]["trades"], 0)

    def test_dashboard_and_labels(self):
        with open(os.path.join(ROOT, "app/static/dashboard.html"), encoding="utf-8") as f:
            h = f.read()
        self.assertIn("addIfPresent('split_entry'", h)
        self.assertIn("diagFeaturesHtml(b.features)", h)
        self.assertIn("splitStatsHtml(r.split_stats)", h)
        from app import backtest_jobs as J, main
        self.assertIn("split_entry", J.LABEL_PARAM_KEYS)
        self.assertIn("split_entry", main._backtest_job_params())
        ag = J._aggregate([{"recent_trades": [], "split_stats": {"first": 2, "added": 1, "invalidated": 1, "expired": 0,
                                                                  "closed_before": 0}},
                           {"recent_trades": [], "split_stats": {"first": 1, "added": 1, "invalidated": 0, "expired": 0,
                                                                  "closed_before": 0}}])
        self.assertEqual((ag["split_stats"]["first"], ag["split_stats"]["added"]), (3, 2))


if __name__ == "__main__":
    unittest.main()
