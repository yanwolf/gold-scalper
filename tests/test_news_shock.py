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


DAY = 1440


def bars(ranges):
    """每根 1 分K 的高低差照給的數字(開=收=4000)。T0 對齊 UTC 整點。"""
    return [[T0 + i * MIN, "4000", str(4000 + r / 2), str(4000 - r / 2), "4000", "1", T0 + (i + 1) * MIN - 1]
            for i, r in enumerate(ranges)]


def session_ranges(days, quiet=0.5, busy=3.0, busy_hour=13):
    """亞洲盤小、紐約開盤(UTC 13 點)那一小時大——跟真實黃金一樣的時段差異。"""
    out = []
    for i in range(days * DAY):
        h = ((T0 + i * MIN) // 3_600_000) % 24
        out.append(busy if h == busy_hour else quiet)
    return out


def at_hour(day, hour):
    """第 day 天(從 0 起)UTC hour 點整那根的索引。"""
    base_h = (T0 // 3_600_000) % 24
    return day * DAY + ((hour - base_h) % 24) * 60


class Detect(unittest.TestCase):
    def test_session_open_is_not_news(self):
        r = session_ranges(8)
        shocks = D.shock_times(bars(r))
        self.assertEqual(shocks, [], "紐約開盤每天都大：跟同時段比不算消息(r119 的定義會整小時都報)")
        legacy_like = sum(1 for i in range(DAY, len(r)) if r[i] >= 5 * sorted(r[i - DAY:i])[DAY // 2])
        self.assertGreater(legacy_like, 300, "對照：用「前 24 小時中位數」的舊定義，同樣的資料會報出好幾百次")

    def test_real_spike_in_quiet_hour(self):
        r = session_ranges(8)
        i = at_hour(7, 2)
        r[i] = 3.0                                                     # 安靜時段突然一根 3 點
        self.assertEqual(D.shock_times(bars(r)), [T0 + (i + 1) * MIN - 1])
        r2 = session_ranges(8)
        r2[i] = 2.0                                                    # 只有同時段的 4 倍
        self.assertEqual(D.shock_times(bars(r2)), [], "不到 5 倍不算")

    def test_must_also_beat_hourly_average(self):
        r = session_ranges(8, quiet=0.5)
        h2 = [at_hour(d, 2) + m for d in range(8) for m in range(60)]
        for j in h2:
            r[j] = 0.01                                                # UTC 2 點這一小時平常幾乎不動
        i = at_hour(7, 2) + 30
        r[i] = 0.1                                                     # 是同時段的 10 倍，但比小時K 平均波幅小
        self.assertEqual(D.shock_times(bars(r)), [], "絕對波幅太小：不算消息")
        r[i] = 0.8
        self.assertEqual(D.shock_times(bars(r)), [T0 + (i + 1) * MIN - 1], "對照：夠大就算")

    def test_needs_five_days_of_same_hour(self):
        r = session_ranges(8)
        early = at_hour(3, 2)                                          # 第 4 天：同時段只有 3 天樣本
        r[early] = 9.0
        self.assertNotIn(T0 + (early + 1) * MIN - 1, D.shock_times(bars(r)) or [])
        late = at_hour(6, 2)
        r[late] = 9.0
        self.assertIn(T0 + (late + 1) * MIN - 1, D.shock_times(bars(r)), "對照：樣本夠了就判斷")

    def test_same_hour_median_uses_last_20_days(self):
        days = 42
        r = session_ranges(days)
        for d in range(days):
            for m in range(60):
                r[at_hour(d, 2) + m] = 2.0 if d < 21 else 0.1          # 前 21 天這個時段很大，後 21 天變小
        i = at_hour(days - 1, 2) + 30
        r[i] = 0.8
        self.assertIn(T0 + (i + 1) * MIN - 1, D.shock_times(bars(r)), "只看前 20 天：更早的大波動不算進中位數")

    def test_causal(self):
        r = session_ranges(8)
        i = at_hour(7, 2)
        r[i] = 3.0
        early = D.shock_times(bars(r[:i + 1]))
        later = D.shock_times(bars(r + [50.0] * 30))
        self.assertEqual(len(early), 1, "前提：先認出一次")
        self.assertTrue(later, "前提：後面也有結果")
        self.assertEqual(later[0], early[0], "後面的K線不影響前面的判斷")

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


SHOCK_AT = 7 * DAY + 120                                           # 第 8 天，資料夠了之後
PATH = [4002.0] * SHOCK_AT + [4003.5] * 180 + [3990.0] * 30        # 震盪後一路持平，最後跌破停損出場


def spiked():
    k = kl(PATH)
    k[SHOCK_AT][2] = "4005.0"    # 這根 1 分K 高低差約 3 點，平常只有 0.1：消息震盪
    return k


def run(news):
    shock_close = T0 + (SHOCK_AT + 1) * MIN - 1

    def sig(trades, *a, **k):
        return dict(BEICHI) if trades[-1]["time"] >= shock_close else dict(NEUTRAL)
    with mock.patch.object(B, "compute_signal_from_trades", side_effect=sig), \
         mock.patch.object(B, "fetch_historical_klines", return_value=[]):
        return B.run_backtest(days=9, interval_seconds=900, klines=spiked(), daily_klines=[], trend_filter_mode=0,
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
        self.assertIn("d.by_direction", h)
        from app import backtest_jobs as J, main
        self.assertIn("news_cooldown", J.LABEL_PARAM_KEYS)
        self.assertIn("news_cooldown", main._backtest_job_params())
        row = J._window_row(0, {"total_trades": 0, "skipped_news": 3, "news_shocks": 9}, 0, 1)
        self.assertEqual((row["skipped_news"], row["news_shocks"]), (3, 9))


if __name__ == "__main__":
    unittest.main()
