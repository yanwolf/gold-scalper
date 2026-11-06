"""
r119 的測試：消息震盪(從 1 分K 高低差認，不靠經濟日曆)的診斷分組與「震盪後 60 分鐘不進場」回測選項。
執行：python -m unittest tests.test_news_shock -v
"""
import logging
import os
import unittest
from datetime import datetime
from unittest import mock

logging.disable(logging.CRITICAL)

from app import backtest as B  # noqa: E402
from app import trade_diagnostics as D  # noqa: E402
from tests.test_split_entry import kl, T0, NEUTRAL  # noqa: E402
from tests.test_narrow_zs import BEICHI  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIN = 60_000


def bars(ranges):
    """每根 1 分K 的高低差照給的數字(開=收=4000)。"""
    return [[T0 + i * MIN, "4000", str(4000 + r / 2), str(4000 - r / 2), "4000", "1", T0 + (i + 1) * MIN - 1]
            for i, r in enumerate(ranges)]


class Detect(unittest.TestCase):
    def test_spike_after_enough_history(self):
        shocks = D.shock_times(bars([1.0] * 300 + [6.0] + [1.0] * 10))
        self.assertEqual(shocks, [T0 + 301 * MIN - 1])

    def test_not_big_enough(self):
        shocks = D.shock_times(bars([1.0] * 300 + [4.0]))
        self.assertEqual(shocks, [], "4 倍不到 5 倍")
        self.assertEqual(D.shock_times(bars([1.0] * 300 + [5.0])), [T0 + 301 * MIN - 1], "對照：剛好 5 倍算")

    def test_needs_history_and_is_causal(self):
        self.assertEqual(D.shock_times(bars([1.0] * 100 + [9.0] + [1.0] * 300)), [], "前面不到 4 小時：不判斷")
        early = D.shock_times(bars([1.0] * 300 + [6.0]))
        later = D.shock_times(bars([1.0] * 300 + [6.0] + [50.0] * 5))
        self.assertEqual(len(early), 1, "前提：先認出一次")
        self.assertTrue(later, "前提：後面那幾根也有結果")
        self.assertEqual(later[0], early[0], "後面的K線不影響前面的判斷")

    def test_median_uses_only_last_24h(self):
        shocks = D.shock_times(bars([10.0] * 1500 + [1.0] * 1500 + [6.0]))
        self.assertIn(T0 + 3001 * MIN - 1, shocks, "中位數只看前 1440 根(24 小時)：更早的大波動不算進來")

    def test_not_one_minute_bars(self):
        k = bars([1.0] * 300)
        k15 = [[T0 + i * 900_000] + x[1:6] + [T0 + (i + 1) * 900_000 - 1] for i, x in enumerate(k)]
        self.assertEqual(len(k15), 300, "前提：有 300 根")
        self.assertEqual(int(k15[1][0]) - int(k15[0][0]), 900_000, "前提：真的是 15 分K")
        self.assertIsNone(D.shock_times(k15))

    def test_recent_shock(self):
        s = [T0 + 1000 * MIN]
        self.assertIs(D.recent_shock(s, T0 + 1000 * MIN), True)
        self.assertIs(D.recent_shock(s, T0 + 1059 * MIN), True)
        self.assertIs(D.recent_shock(s, T0 + 1061 * MIN), False, "超過 60 分鐘")
        self.assertIs(D.recent_shock(s, T0 + 999 * MIN), False, "震盪之前")
        self.assertIsNone(D.recent_shock(None, T0))


SHOCK_AT = 300
PATH = [4002.0] * SHOCK_AT + [4003.5] * 180 + [3990.0] * 30   # 第 300 分鐘跳 1.5 點(遠大於平常 0.1)，最後跌破停損出場


def spiked():
    k = kl(PATH)
    k[SHOCK_AT][2] = "4005.0"    # 這根 1 分K 高低差 1.55，平常只有 0.1：消息震盪
    return k


def run(news):
    shock_close = T0 + (SHOCK_AT + 1) * MIN - 1

    def sig(trades, *a, **k):
        return dict(BEICHI) if trades[-1]["time"] >= shock_close else dict(NEUTRAL)
    with mock.patch.object(B, "compute_signal_from_trades", side_effect=sig), \
         mock.patch.object(B, "fetch_historical_klines", return_value=[]):
        return B.run_backtest(days=1, interval_seconds=900, klines=spiked(), daily_klines=[], trend_filter_mode=0,
                              use_chop_filter=False, block_market_closed=False, min_atr_points=0, use_atr=False,
                              sl_points=5.0, trail_trigger_points=50, trail_distance_points=50, reversal_confirm_count=3,
                              news_cooldown=news)


class Backtest(unittest.TestCase):
    def _entry_min(self, r):
        self.assertTrue(r["recent_trades"], "前提：有交易")
        t = r["recent_trades"][0]
        return (datetime.fromisoformat(t["entry_time"]).timestamp() * 1000 - T0) / MIN, t

    def test_off_enters_right_after_shock_and_tags_it(self):
        r = run(0)
        self.assertEqual(r["news_shocks"], 1, "前提：認出一次震盪")
        m, t = self._entry_min(r)
        self.assertLess(m, SHOCK_AT + 30, "沒開：震盪後第一步就進場")
        self.assertIs(t["diag"]["after_shock"], True)
        self.assertEqual(r["entry_diagnostics"]["news_shock"]["after"]["trades"], 1)

    def test_on_waits_60_minutes(self):
        r = run(1)
        m, t = self._entry_min(r)
        self.assertGreaterEqual(m, SHOCK_AT + 60, "開了：震盪後 60 分鐘內不進場")
        self.assertIs(t["diag"]["after_shock"], False)
        self.assertGreater(r["skipped_news"], 0)
        self.assertEqual(r["news_cooldown"], 1)

    def test_wiring(self):
        with open(os.path.join(ROOT, "app/static/dashboard.html"), encoding="utf-8") as f:
            h = f.read()
        self.assertIn("addIfPresent('news_cooldown'", h)
        self.assertIn("d.news_shock", h)
        from app import backtest_jobs as J, main
        self.assertIn("news_cooldown", J.LABEL_PARAM_KEYS)
        self.assertIn("news_cooldown", main._backtest_job_params())
        row = J._window_row(0, {"total_trades": 0, "skipped_news": 3, "news_shocks": 9}, 0, 1)
        self.assertEqual((row["skipped_news"], row["news_shocks"]), (3, 9))


if __name__ == "__main__":
    unittest.main()
