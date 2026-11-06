"""
r120 的測試：斐波那契回檔掛單(app/fib_pullback.py，1小時K，只存在回測)與回測迴圈的限價單成交。
執行：python -m unittest tests.test_fib_pullback -v
"""
import logging
import os
import unittest
from unittest import mock

logging.disable(logging.CRITICAL)

from app import backtest as B  # noqa: E402
from app import fib_pullback as F  # noqa: E402
from tests.test_breakout_trend import hourly, _klines  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RISE = [3900.0 + i for i in range(60)]                         # 60 小時緩漲：EMA50 向上、收盤在上
DIP = [3958.0, 3954.0, 3950.0]                                  # 擺動低點(3950 附近)
RALLY = [3958.0, 3966.0, 3974.0, 3982.0, 3990.0]                # 推動段到 3990 附近
BASE = RISE + DIP + RALLY + [3988.0]                            # 小回一點，還沒到掛單價


def cfg(**kw):
    c, err = F.build_cfg(kw)
    assert err is None, err
    return c


class Rules(unittest.TestCase):
    def test_long_setup_levels(self):
        pre = F.precompute(hourly(BASE))
        i = len(BASE) - 1
        r = F.evaluate_at(pre, i, cfg())
        self.assertEqual((r["stage"], r["direction"]), ("訊號", "bullish"), r["chan"]["reason"])
        lo, hi = min(pre["low"][60:63]), max(pre["high"][63:i + 1])
        rng = hi - lo
        m = r["mr"]
        self.assertAlmostEqual(m["limit_price"], hi - 0.618 * rng, places=6)
        self.assertAlmostEqual(m["sl_price"], hi - 0.786 * rng - 0.1 * pre["atr"][i], places=6)
        self.assertAlmostEqual(m["tp_price"], hi, places=6)
        self.assertAlmostEqual(m["sl_points"], m["limit_price"] - m["sl_price"], places=6)

    def test_pivot_needs_confirmation(self):
        pre = F.precompute(hourly(BASE))
        self.assertTrue(pre["piv_lo"][62], "前提：用整段資料看，第 62 根是擺動低點")
        r = F.evaluate_at(pre, 64, cfg(sides=1))                       # 第 64 根時低點右邊只收了 2 根：還不能用
        self.assertEqual(r["stage"], "中性")
        self.assertIn("還沒有確認的擺動低點", r["chan"]["reason"])

    def test_causal(self):
        full = F.precompute(hourly(BASE + [3940.0, 3900.0, 4100.0]))
        part = F.precompute(hourly(BASE))
        for i in range(55, len(BASE)):
            self.assertEqual(F.evaluate_at(part, i, cfg()), F.evaluate_at(full, i, cfg()), i)

    def test_no_order_when_price_already_through_or_touched(self):
        deep = BASE[:-1] + [3960.0]                                    # 現價已經在掛單價下面
        r = F.evaluate_at(F.precompute(hourly(deep)), len(deep) - 1, cfg(sides=1))
        self.assertIn("過了掛單價", r["chan"]["reason"])
        touched = BASE[:-1] + [3962.0, 3985.0]                         # 碰過掛單價又彈回
        r2 = F.evaluate_at(F.precompute(hourly(touched)), len(touched) - 1, cfg(sides=1))
        self.assertIn("機會過了", r2["chan"]["reason"])

    def test_small_impulse_and_trend(self):
        r = F.evaluate_at(F.precompute(hourly(BASE)), len(BASE) - 1, cfg(sides=1, impulse_atr=10))
        self.assertEqual(r["stage"], "中性")
        self.assertIn("推動段", r["chan"]["reason"])
        down = [4100.0 - i for i in range(60)] + [4038.0, 4034.0, 4030.0, 4038.0, 4046.0, 4054.0, 4062.0, 4070.0, 4068.0]
        r2 = F.evaluate_at(F.precompute(hourly(down)), len(down) - 1, cfg(sides=1))
        self.assertIn("EMA50", r2["chan"]["reason"])

    def test_short_mirror(self):
        mirror = [8000.0 - x for x in BASE]
        r = F.evaluate_at(F.precompute(hourly(mirror)), len(mirror) - 1, cfg())
        self.assertEqual(r["direction"], "bearish")
        self.assertGreater(r["mr"]["sl_price"], r["mr"]["limit_price"])
        self.assertLess(r["mr"]["tp_price"], r["mr"]["limit_price"])

    def test_cfg(self):
        c = cfg()
        self.assertEqual((c["impulse_atr"], c["entry_fib"], c["stop_fib"], c["max_hold_hours"], c["trail_atr"]),
                         (2.0, 0.618, 0.786, 48, 0.0))
        for bad in ({"entry_fib": 0.8, "stop_fib": 0.7}, {"sides": 4}, {"nope": 1}):
            c2, err = F.build_cfg(bad)
            self.assertIsNone(c2, bad)
            self.assertTrue(err)


def _run(path, **kw):
    params = dict(days=max(1, len(path) // 24), strategy_type="fib_pullback", klines=_klines(path), daily_klines=[],
                  trend_filter_mode=0, use_chop_filter=False, block_market_closed=False, min_atr_points=0,
                  use_atr=False, spread_cost_points=0)
    params.update(kw)
    with mock.patch.object(B, "fetch_historical_klines", return_value=[]):
        return B.run_backtest(**params)


class Integration(unittest.TestCase):
    def test_fills_at_limit_and_takes_profit(self):
        path = BASE + [3975.0, 3962.0, 3975.0, 3992.0, 3995.0, 3995.0]
        res = _run(path, fp_sides=1)
        self.assertNotIn("error", res)
        self.assertGreaterEqual(res["limit_stats"]["filled"], 1)
        trades = sorted(res["recent_trades"], key=lambda t: t["entry_time"])
        self.assertTrue(trades, "前提：有成交")
        t = trades[0]
        self.assertAlmostEqual(t["entry_price"], t["limit_order"]["price"], places=6, msg="以限價成交，不是收盤價")
        self.assertGreater(t["entry_time"], t["limit_order"]["placed"], "成交在掛單之後")
        self.assertEqual(t["exit_reason"], "觸及停利")
        self.assertGreater(t["pnl_points"], 0)

    def test_stop_inside_fill_hour(self):
        path = BASE + [3975.0, 3962.0, 3975.0, 3992.0, 3995.0, 3995.0]
        k = _klines(path)
        lim_hour = len(BASE) + 1                                        # 3975→3962 這一小時會碰到限價
        dip = lim_hour * 60 + 50                                        # 這一小時第 50 分鐘插一根長下影線
        k[dip][3] = "3930.0"
        with mock.patch.object(B, "fetch_historical_klines", return_value=[]):
            res = B.run_backtest(days=1, strategy_type="fib_pullback", klines=k, daily_klines=[], trend_filter_mode=0,
                                 use_chop_filter=False, block_market_closed=False, min_atr_points=0, use_atr=False,
                                 spread_cost_points=0, fp_sides=1)
        trades = sorted(res["recent_trades"], key=lambda t: t["entry_time"])
        self.assertTrue(trades, "前提：有成交")
        self.assertEqual(trades[0]["exit_reason"], "觸及停損", "成交那一小時裡碰到停損就出場(不是等下一小時)")

    def test_no_pullback_no_fill(self):
        path = BASE + [3989.0, 3990.0, 3991.0, 3992.0]
        res = _run(path, fp_sides=1)
        self.assertGreater(res["limit_stats"]["placed"], 0, "前提：有掛單")
        self.assertEqual((res["limit_stats"]["filled"], res["total_trades"]), (0, 0))

    def test_stop_loss_at_stop_price(self):
        path = BASE + [3975.0, 3962.0, 3950.0, 3930.0, 3920.0]
        res = _run(path, fp_sides=1)
        trades = sorted(res["recent_trades"], key=lambda t: t["entry_time"])
        self.assertTrue(trades, "前提：有成交")
        t = trades[0]
        self.assertEqual(t["exit_reason"], "觸及停損")
        self.assertLess(t["exit_price"], t["entry_price"])
        self.assertLess(t["pnl_points"], 0)

    def test_signal_steps_hourly_and_cfg_reported(self):
        res = _run(BASE + [3989.0] * 4, fp_sides=1)
        self.assertEqual(res["signal_interval_seconds"], 3600)
        self.assertEqual(res["mr_cfg"]["entry_fib"], 0.618)

    def test_wiring(self):
        with open(os.path.join(ROOT, "app/static/dashboard.html"), encoding="utf-8") as f:
            h = f.read()
        self.assertIn('<option value="fib_pullback">', h)
        for k in ("fp_impulse_atr", "fp_entry_fib", "fp_stop_fib", "fp_sides", "fp_max_hold_hours"):
            self.assertIn(f"addIfPresent('{k}'", h)
        self.assertIn("r.limit_stats", h)
        from app import backtest_jobs as J, main
        self.assertIn("fp_entry_fib", J.LABEL_PARAM_KEYS)
        self.assertIn("fp_entry_fib", main._backtest_job_params())
        for rel in ("app/paper_trading.py", "app/execution.py", "app/signal_engine.py"):
            with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
                src = f.read()
            self.assertIn("def ", src, f"前提：讀到了 {rel}")
            self.assertNotIn("fib_pullback", src, "只存在回測")


if __name__ == "__main__":
    unittest.main()
