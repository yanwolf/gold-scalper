"""
r105 的測試：大週期突破(app/breakout_trend.py，4小時K大行情，只存在回測)。
執行：python -m unittest tests.test_breakout_trend -v
"""
import logging
import os
import unittest
from datetime import datetime
from unittest import mock

logging.disable(logging.CRITICAL)

from app import backtest as B  # noqa: E402
from app import breakout_trend as BO  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOUR = 3_600_000
START = 494_444 * HOUR          # 剛好對齊 UTC 4 小時
CALM = [4000.0 + ((i // 4) % 2) * 2 for i in range(240)]       # 60 根 4 小時K，陰陽交錯在 4000～4002
UP = CALM + [4006.0, 4014.0, 4022.0, 4030.0]                    # 第 60 根：大陽線突破
SMALL = CALM + [4002.2, 4002.3, 4002.4, 4002.5]                 # 第 60 根：剛好突破、但波幅很小


def hourly(closes, start=START):
    out = []
    for i, c in enumerate(closes):
        o = closes[i - 1] if i else c
        out.append({"bucket_start": start + i * HOUR, "open": o, "close": c,
                    "high": max(o, c) + 0.3, "low": min(o, c) - 0.3, "volume": 1.0})
    return out


def cfg(**kw):
    c, err = BO.build_cfg(kw)
    assert err is None, err
    return c


class FourHour(unittest.TestCase):
    def test_aggregation_and_close_points(self):
        self.assertEqual(START % BO.TF_MS, 0, "前提：起點對齊4小時")
        h = hourly([1.0, 5.0, 2.0, 3.0, 4.0, 6.0, 1.0, 2.0])
        four, close_idx, last = BO.build_4h(h)
        self.assertEqual(len(four), 2)
        self.assertEqual((four[0]["open"], four[0]["close"], four[0]["high"], four[0]["low"]), (1.0, 3.0, 5.3, 0.7))
        self.assertEqual(close_idx, [None, None, None, 0, None, None, None, 1])
        self.assertEqual(last, [None, None, None, 0, 0, 0, 0, 1])

    def test_missing_last_hour_never_closes(self):
        h = hourly([1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
        del h[3]                                             # 第一個4小時區間的最後一小時沒有資料
        _, close_idx, _ = BO.build_4h(h)
        self.assertEqual(close_idx, [None] * 5)

    def test_causal(self):
        full = BO.precompute(hourly(UP + [4040.0, 4050.0, 3990.0, 3980.0]))
        part = BO.precompute(hourly(UP))
        for i in range(200, 244):
            self.assertEqual(BO.evaluate_at(part, i, cfg(), current_price=4000.0),
                             BO.evaluate_at(full, i, cfg(), current_price=4000.0), i)


class Signals(unittest.TestCase):
    def test_big_breakout_long(self):
        pre = BO.precompute(hourly(UP))
        r = BO.evaluate_at(pre, 243, cfg(), current_price=4030.0)
        self.assertEqual((r["stage"], r["direction"], r["mr"]["setup"]), ("訊號", "bullish", "突破多"))
        self.assertAlmostEqual(r["mr"]["sl_points"], 2 * pre["atr"][60])
        self.assertIsNone(r["mr"]["tp_price"])
        self.assertEqual(r["hr_atr"], pre["atr"][60])

    def test_only_at_four_hour_close(self):
        pre = BO.precompute(hourly(UP))
        for i in (240, 241, 242):
            r = BO.evaluate_at(pre, i, cfg())
            self.assertEqual(r["stage"], "中性")
            self.assertEqual(r["hr_atr"], pre["atr"][59], "還沒收盤：移動停損用上一根已收盤4小時K的ATR")

    def test_small_breakout_is_not_big_move(self):
        pre = BO.precompute(hourly(SMALL))
        r = BO.evaluate_at(pre, 243, cfg(sides=1))
        self.assertEqual(r["stage"], "中性")
        self.assertIn("不算大行情", r["chan"]["reason"])
        self.assertEqual(BO.evaluate_at(pre, 243, cfg(sides=1, big_bar_atr=0))["direction"], "bullish", "不限K大小就進")

    def test_uses_previous_bar_atr_for_big_bar(self):
        pre = BO.precompute(hourly(CALM + [4003.0, 4005.0, 4007.0, 4009.0]))
        tr = BO._tr(pre["four_h"], 60)
        r_prev, r_own = tr / pre["atr"][59], tr / pre["atr"][60]
        self.assertLess(r_own, r_prev, "前提：用這根自己的ATR算倍數會變小")
        mid = (r_prev + r_own) / 2
        self.assertEqual(BO.evaluate_at(pre, 243, cfg(sides=1, big_bar_atr=mid))["direction"], "bullish",
                         "用前一根ATR：波幅倍數夠")

    def test_trend_filter(self):
        down = [4100.0 - i * 0.5 for i in range(156)] + [3980.0 + ((i // 4) % 2) for i in range(84)]
        closes = down + [3983.0, 3986.0, 3989.0, 3992.0]      # 下跌後橫盤20根，往上突破，但還在EMA50之下
        pre = BO.precompute(hourly(closes))
        hi = max(b["high"] for b in pre["four_h"][40:60])
        self.assertGreater(3992.0, hi, "前提：真的突破了前20根高點")
        self.assertLess(3992.0, pre["ema"][60], "前提：收盤在EMA50之下")
        r = BO.evaluate_at(pre, 243, cfg(sides=1))
        self.assertEqual(r["stage"], "中性")
        self.assertIn("EMA50", r["chan"]["reason"])
        self.assertEqual(BO.evaluate_at(pre, 243, cfg(sides=1, trend=0))["direction"], "bullish")

    def test_short_mirror_and_sides(self):
        down_break = CALM + [3994.0, 3986.0, 3978.0, 3970.0]
        pre = BO.precompute(hourly(down_break))
        r = BO.evaluate_at(pre, 243, cfg())
        self.assertEqual((r["direction"], r["mr"]["setup"]), ("bearish", "突破空"))
        self.assertEqual(BO.evaluate_at(pre, 243, cfg(sides=1))["stage"], "中性")

    def test_needs_full_ema_history(self):
        pre = BO.precompute(hourly(CALM[:200] + [4006.0, 4014.0, 4022.0, 4030.0]))   # 第50根4小時K就突破
        self.assertIsNotNone(pre["ema"][50], "前提：EMA50 在第50根已經算得出來")
        r = BO.evaluate_at(pre, 203, cfg())
        self.assertEqual(r["stage"], "中性")
        self.assertIn("資料不足", r["chan"]["reason"])

    def test_warmup(self):
        pre = BO.precompute(hourly(CALM[:100] + [4040.0] * 4))
        r = BO.evaluate_at(pre, 103, cfg())
        self.assertIn("資料不足", r["chan"]["reason"])


class Config(unittest.TestCase):
    def test_defaults(self):
        c = cfg()
        self.assertEqual((c["channel"], c["big_bar_atr"], c["trend"], c["sl_atr"], c["trail_trigger_atr"], c["trail_atr"],
                          c["max_hold_hours"]), (20, 1.0, 1, 2.0, 2.0, 3.0, 0))
        self.assertEqual(cfg(max_hold_days=5)["max_hold_hours"], 120)

    def test_rejects(self):
        for bad in ({"channel": 2}, {"sides": 4}, {"trend": 0.5}, {"sl_atr": "x"}, {"nope": 1}):
            bad_cfg, _err = BO.build_cfg(bad)  # 固定長度
            self.assertIsNone(bad_cfg, bad)
            self.assertTrue(_err, f"要講原因：{bad}")


def _price(m, path):
    h, f = divmod(m, 60)
    prev = path[h - 1] if h else path[0]
    return prev + (path[h] - prev) * (f + 1) / 60


def _klines(path):
    out = []
    for m in range(len(path) * 60):
        o = _price(m - 1, path) if m else path[0]
        c = _price(m, path)
        out.append([START + m * 60_000, str(o), str(max(o, c) + 0.05), str(min(o, c) - 0.05), str(c), "1",
                    START + (m + 1) * 60_000 - 1])
    return out


def _run(path, **kw):
    params = dict(days=len(path) // 24, strategy_type="breakout_trend", klines=_klines(path), daily_klines=[],
                  trend_filter_mode=0, use_chop_filter=False, block_market_closed=False, min_atr_points=0,
                  use_atr=False, spread_cost_points=0)
    params.update(kw)
    with mock.patch.object(B, "fetch_historical_klines", return_value=[]):
        return B.run_backtest(**params)


class Integration(unittest.TestCase):
    def test_trend_ride_exits_by_trailing_with_profit(self):
        rally = [4030.0 + 6 * k for k in range(1, 25)]                # 固定長度(突破後一路漲 24 小時)
        drop = [rally[-1] - 3 * k for k in range(1, 25)]              # 再一路跌回來
        path = UP + rally + drop
        res = _run(path)
        self.assertNotIn("error", res)
        trades = sorted(res["recent_trades"], key=lambda t: t["entry_time"])
        self.assertTrue(trades, "前提：有進場")
        t = trades[0]
        entry_h = (datetime.fromisoformat(t["entry_time"]).timestamp() * 1000 + 1 - START) / HOUR
        self.assertEqual(round(entry_h), 244, "在第60根4小時K收盤(第244小時)進場")
        self.assertEqual(t["exit_reason"], "觸及移動停損")
        self.assertGreater(t["pnl_points"], 50, "大行情抱住了")
        self.assertEqual(res["mr_cfg"]["trail_atr"], 3.0)

    def test_trailing_uses_four_hour_atr(self):
        rally = [4030.0 + 6 * k for k in range(1, 25)]  # 固定長度
        drop = [rally[-1] - 3 * k for k in range(1, 25)]
        res = _run(UP + rally + drop)
        trades = sorted(res["recent_trades"], key=lambda t: t["entry_time"])
        self.assertTrue(trades, "前提：有交易")
        t = trades[0]
        peak = max(rally)
        giveback = peak - t["exit_price"]
        pre = BO.precompute(hourly(UP + rally + drop))
        atr4 = pre["atr"][pre["last_closed"][len(UP) + len(rally) - 1]]
        self.assertGreater(giveback, 2.0 * atr4, "回吐距離是 3 倍『4小時』ATR(1小時ATR小得多)")

    def test_trail_trigger_is_separate_from_distance(self):
        rally = [4030.0 + 6 * k for k in range(1, 25)]  # 固定長度
        drop = [rally[-1] - 3 * k for k in range(1, 25)]
        base = _run(UP + rally + drop)
        self.assertTrue(any(t["exit_reason"] == "觸及移動停損" for t in base["recent_trades"]), "前提：預設那組會被移動停損出場")
        res = _run(UP + rally + drop, bo_trail_trigger_atr=20)
        self.assertFalse(any(t["exit_reason"] == "觸及移動停損" for t in res["recent_trades"]),
                         "浮盈沒到 20 倍ATR：移動停損不啟動(預設那組同一段行情會被移動停損出場)")

    def test_opposite_signal_does_not_exit(self):
        real = BO.evaluate_at

        def fake(pre, i, c, current_price=None):
            r = real(pre, i, c, current_price)
            if i == 247:   # 進場後下一根4小時K收盤：假裝出現空方突破
                r = dict(r, stage="訊號", direction="bearish",
                         mr={"setup": "突破空", "sl_points": 5.0, "tp_price": None, "atr": 4.0})
            return r
        # 最後一路跌：部位最後被停損出場(有一筆平倉紀錄可以看出場原因)
        path = UP + [4031.0, 4032.0, 4033.0, 4034.0] + [4034.0] * 8 + [4010.0, 3980.0, 3950.0, 3920.0]
        with mock.patch.object(BO, "evaluate_at", side_effect=fake):
            res = _run(path, reversal_confirm_count=1)
        t = sorted(res["recent_trades"], key=lambda t: t["entry_time"])
        self.assertEqual(len(t), 1, f"前提：在第244小時進場、最後出場一次 {t}")
        self.assertEqual(t[0]["exit_reason"], "觸及停損", "反向訊號不提前出場(只靠停損/移動停損)")

    def test_stop_loss_when_breakout_fails(self):
        path = UP + [4030.0 - 4 * k for k in range(1, 13)]
        trades = sorted(_run(path)["recent_trades"], key=lambda t: t["entry_time"])
        self.assertTrue(trades, "前提：有交易")
        t = trades[0]
        self.assertEqual(t["exit_reason"], "觸及停損")
        self.assertLess(t["pnl_points"], 0)

    def test_signal_steps_are_hourly(self):
        res = _run(UP + [4030.0] * 8)
        self.assertEqual(res["signal_interval_seconds"], 3600)

    def test_bad_param(self):
        self.assertIn("error", _run(UP, bo_channel=1))


class Wiring(unittest.TestCase):
    def _src(self, rel):
        with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
            return f.read()

    def test_not_in_live_paths(self):
        for rel in ("app/paper_trading.py", "app/execution.py", "app/signal_engine.py", "app/notifier.py"):
            self.assertIn("def ", self._src(rel), f"前提：讀到了 {rel}")
            self.assertNotIn("breakout_trend", self._src(rel), rel)

    def test_endpoints_jobs_dashboard(self):
        from app import main, backtest_jobs as J
        p = main._backtest_job_params()
        for k in ("bo_channel", "bo_big_bar_atr", "bo_trend", "bo_sides", "bo_sl_atr", "bo_trail_trigger_atr",
                  "bo_trail_atr", "bo_max_hold_days"):
            self.assertIn(k, p)
        self.assertIn("bo_channel", J.LABEL_PARAM_KEYS)
        self.assertIn("breakout_trend", J._MR_CFG_LABEL)
        h = self._src("app/static/dashboard.html")
        self.assertIn('<option value="breakout_trend">', h)
        for k in ("bo_channel", "bo_big_bar_atr", "bo_trail_atr", "bo_max_hold_days"):
            self.assertIn(f"addIfPresent('{k}'", h)
        self.assertIn("4小時K突破", h)


if __name__ == "__main__":
    unittest.main()
