"""
r99 的測試：回拉轉強策略(app/pullback_resume.py)，只存在回測。
執行：python -m unittest tests.test_pullback_resume -v
"""
import logging
import os
import unittest
from datetime import datetime
from unittest import mock

logging.disable(logging.CRITICAL)

from app import backtest as B  # noqa: E402
from app import pullback_resume as P  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOUR = 3_600_000
START = 494_444 * HOUR


def _bars(closes, first_open=None):
    out = []
    for i, c in enumerate(closes):
        o = closes[i - 1] if i else (first_open if first_open is not None else c - 1)
        out.append({"bucket_start": START + i * HOUR, "open": o, "close": c,
                    "high": max(o, c) + 0.3, "low": min(o, c) - 0.3, "volume": 1.0})
    return out


UP = [4000.0 + i for i in range(40)]            # 0..39 一路陽線，收在 4039
DOWN = [4100.0 - i for i in range(40)]          # 一路陰線，收在 4061


def cfg(**kw):
    c, err = P.build_cfg(kw)
    assert err is None, err
    return c


class Pattern(unittest.TestCase):
    def test_long_after_pullback_reclaims_open(self):
        bars = _bars(UP + [4036.5, 4042.0])            # 40 陰線(開 4039)、41 陽線收回 4039 之上
        r = P.evaluate_at(P.precompute(bars), 41, cfg())
        self.assertEqual((r["stage"], r["direction"], r["mr"]["setup"]), ("訊號", "bullish", "型態多"))
        self.assertIsNone(r["mr"]["tp_price"])
        self.assertEqual(r["mr"]["sl_points"], P.NO_STOP_POINTS, "預設不設停損")

    def test_no_signal_if_close_does_not_reclaim_prev_open(self):
        r = P.evaluate_at(P.precompute(_bars(UP + [4036.5, 4038.5])), 41, cfg())
        self.assertEqual(r["stage"], "中性")
        self.assertIn("沒有收回", r["chan"]["reason"])

    def test_no_signal_without_pullback_but_baseline_fires(self):
        bars = _bars(UP + [4040.0, 4041.0])
        pre = P.precompute(bars)
        self.assertEqual(P.evaluate_at(pre, 41, cfg())["stage"], "中性")
        b = P.evaluate_at(pre, 41, cfg(mode=1))
        self.assertEqual((b["direction"], b["mr"]["setup"]), ("bullish", "基準多"))

    def test_trend_filter_blocks_and_can_be_turned_off(self):
        closes = DOWN + [4058.0, 4063.0]               # 下跌趨勢裡：一根陰線後陽線收回
        pre = P.precompute(_bars(closes))
        self.assertNotEqual(P.evaluate_at(pre, 41, cfg(sides=1))["direction"], "bullish")
        self.assertEqual(P.evaluate_at(pre, 41, cfg(sides=1, trend=2))["direction"], "bullish")

    def test_bollinger_mid_trend_option(self):
        pre = P.precompute(_bars(UP + [4036.5, 4042.0]))
        self.assertEqual(P.evaluate_at(pre, 41, cfg(trend=1))["direction"], "bullish")
        pre2 = P.precompute(_bars(UP + [4036.5, 4042.0]))
        pre2["sma"][41] = 5000.0                       # 收盤在中軌之下
        self.assertEqual(P.evaluate_at(pre2, 41, cfg(trend=1, sides=1))["stage"], "中性")

    def test_trend_line_must_slope_with_direction(self):
        pre = P.precompute(_bars(UP + [4036.5, 4042.0]))
        pre["ema"][41 - P.SLOPE_BARS] = pre["ema"][41] + 1   # 收盤還在 EMA 之上，但 EMA 往下
        self.assertEqual(P.evaluate_at(pre, 41, cfg(sides=1))["stage"], "中性")

    def test_trigger_bar_must_be_bullish(self):
        bars = _bars(UP + [4036.5, 4040.0])
        bars[41]["open"] = 4045.0                            # 跳空開高、收黑：收盤雖然高於前一根開盤，不算轉強
        r = P.evaluate_at(P.precompute(bars), 41, cfg(sides=1))
        self.assertEqual(r["stage"], "中性")

    def test_min_pullback_bars(self):
        one = P.precompute(_bars(UP + [4036.5, 4042.0]))
        self.assertEqual(P.evaluate_at(one, 41, cfg(min_pullback_bars=2))["stage"], "中性")
        two = P.precompute(_bars(UP + [4037.0, 4035.0, 4038.0]))   # 兩根陰線，第三根收回 4037 之上
        self.assertEqual(P.evaluate_at(two, 42, cfg(min_pullback_bars=2))["direction"], "bullish")

    def test_min_pullback_atr(self):
        pre = P.precompute(_bars(UP + [4036.5, 4042.0]))
        self.assertEqual(P.evaluate_at(pre, 41, cfg(min_pullback_atr=5))["stage"], "中性")
        self.assertEqual(P.evaluate_at(pre, 41, cfg(min_pullback_atr=0.5))["direction"], "bullish")

    def test_short_mirror_and_sides(self):
        closes = DOWN + [4063.5, 4058.0]               # 40 陽線反彈(開 4061)、41 陰線收在 4061 之下
        pre = P.precompute(_bars(closes))
        r = P.evaluate_at(pre, 41, cfg())
        self.assertEqual((r["direction"], r["mr"]["setup"]), ("bearish", "型態空"))
        self.assertEqual(P.evaluate_at(pre, 41, cfg(sides=1))["stage"], "中性")

    def test_stop_at_pullback_low(self):
        bars = _bars(UP + [4036.5, 4042.0])
        r = P.evaluate_at(P.precompute(bars), 41, cfg(sl_mode=1), current_price=4042.0)
        self.assertAlmostEqual(r["mr"]["sl_points"], 4042.0 - bars[40]["low"])
        s = P.evaluate_at(P.precompute(_bars(DOWN + [4063.5, 4058.0])), 41, cfg(sl_mode=1), current_price=4058.0)
        self.assertAlmostEqual(s["mr"]["sl_points"], 4063.8 - 4058.0)

    def test_causal(self):
        closes = UP + [4036.5, 4042.0, 4030.0, 4050.0, 4020.0]
        short = P.precompute(_bars(closes[:42]))
        full = P.precompute(_bars(closes))
        for i in range(30, 42):
            self.assertEqual(P.evaluate_at(short, i, cfg()), P.evaluate_at(full, i, cfg()))

    def test_warmup(self):
        r = P.evaluate_at(P.precompute(_bars(UP[:20] + [4016.0, 4022.0])), 21, cfg())
        self.assertEqual(r["stage"], "中性")
        self.assertIn("資料不足", r["chan"]["reason"])


class Config(unittest.TestCase):
    def test_defaults(self):
        c = cfg()
        self.assertEqual((c["sides"], c["trend"], c["mode"], c["hold_bars"], c["max_hold_hours"], c["sl_mode"]),
                         (3, 0, 0, 1, 1, 0))

    def test_rejects_bad(self):
        for bad in ({"sides": 0}, {"hold_bars": 7}, {"trend": 1.5}, {"min_pullback_atr": "x"}, {"zzz": 1}):
            c, err = P.build_cfg(bad)
            self.assertIsNone(c, bad)
            self.assertTrue(err)


def _price(m, path):
    h, f = divmod(m, 60)
    prev = path[h - 1] if h else path[0] - 1
    return prev + (path[h] - prev) * (f + 1) / 60


def _klines(path):
    out = []
    for m in range(len(path) * 60):
        o = _price(m - 1, path) if m else path[0] - 1
        c = _price(m, path)
        out.append([START + m * 60_000, str(o), str(max(o, c) + 0.05), str(min(o, c) - 0.05), str(c), "1",
                    START + (m + 1) * 60_000 - 1])
    return out


PATH = UP + [4036.5, 4042.0, 4046.0, 4047.0, 4048.0, 4049.0, 4050.0, 4051.0]   # 48 小時


def _run(path=PATH, **kw):
    params = dict(days=2, strategy_type="pullback_resume", klines=_klines(path), daily_klines=[],
                  trend_filter_mode=0, use_chop_filter=False, block_market_closed=False, min_atr_points=0,
                  use_atr=False, spread_cost_points=0)
    params.update(kw)
    with mock.patch.object(B, "fetch_historical_klines", return_value=[]):
        return B.run_backtest(**params)


class Integration(unittest.TestCase):
    def test_enters_at_trigger_close_and_exits_next_close(self):
        res = _run()
        self.assertNotIn("error", res)
        self.assertEqual(res["signal_interval_seconds"], 3600)
        trades = res["recent_trades"]
        self.assertEqual(len(trades), 1, trades)
        t = trades[0]
        entry_ms = datetime.fromisoformat(t["entry_time"]).timestamp() * 1000
        exit_ms = datetime.fromisoformat(t["exit_time"]).timestamp() * 1000
        self.assertEqual(round((entry_ms + 1 - START) / HOUR), 42, "第 41 根收盤進場")
        self.assertEqual(exit_ms - entry_ms, HOUR, "持有一根")
        self.assertEqual(t["exit_reason"], "持有時間到")
        self.assertAlmostEqual(t["entry_price"], 4042.0, places=6)
        self.assertAlmostEqual(t["exit_price"], 4046.0, places=6)
        self.assertEqual(res["mr_cfg"]["hold_bars"], 1)
        self.assertIsNone(t.get("tp_price"), "沒有停利價")

    def test_hold_two_bars(self):
        t = _run(pr_hold_bars=2)["recent_trades"][0]
        held = datetime.fromisoformat(t["exit_time"]) - datetime.fromisoformat(t["entry_time"])
        self.assertEqual(held.total_seconds(), 2 * 3600)
        self.assertAlmostEqual(t["exit_price"], 4047.0, places=6)

    def test_reversal_signal_does_not_exit_early(self):
        # 持有 2 根：第一根收盤就出現反向訊號也不提前出場——「賺賠都走」只看時間
        real = P.evaluate_at

        def fake(pre, i, c, current_price=None):
            r = real(pre, 41, c, current_price) if i in (41, 42) else P._neutral("x")
            if i == 42:
                r = dict(r, direction="bearish")
            return r
        with mock.patch.object(P, "evaluate_at", side_effect=fake):
            res = _run(pr_hold_bars=2, reversal_confirm_count=1)
        t = sorted(res["recent_trades"], key=lambda t: t["entry_time"])[0]
        self.assertEqual((t["direction"], t["exit_reason"]), ("bullish", "持有時間到"))
        held = datetime.fromisoformat(t["exit_time"]) - datetime.fromisoformat(t["entry_time"])
        self.assertEqual(held.total_seconds(), 2 * 3600)

    def test_baseline_trades_every_bullish_bar_in_trend(self):
        res = _run(pr_mode=1, pr_sides=1)
        self.assertGreater(res["total_trades"], 10)
        self.assertTrue(all(t["exit_reason"] == "持有時間到" for t in res["recent_trades"]))

    def test_stop_at_pullback_low_is_used_intrabar(self):
        path = UP + [4036.5, 4042.0, 4030.0, 4031.0, 4032.0, 4033.0, 4034.0, 4035.0]
        t = _run(path=path, pr_sl_mode=1)["recent_trades"][0]
        self.assertEqual(t["exit_reason"], "觸及停損")
        self.assertLess(t["exit_price"], 4036.5)

    def test_bad_param_errors(self):
        self.assertIn("error", _run(pr_hold_bars=9))

    def test_chop_filter_not_applied(self):
        a = _run(use_chop_filter=True, chop_threshold=0)
        self.assertEqual(a["total_trades"], 1)


class Isolation(unittest.TestCase):
    def _src(self, rel):
        with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
            return f.read()

    def test_live_paths_do_not_use_it(self):
        for rel in ("app/paper_trading.py", "app/execution.py", "app/signal_engine.py", "app/notifier.py"):
            self.assertNotIn("pullback_resume", self._src(rel), rel)

    def test_endpoints_and_dashboard(self):
        from app import main
        params = main._backtest_job_params()
        for k in ("pr_sides", "pr_trend", "pr_mode", "pr_min_pullback_bars", "pr_min_pullback_atr", "pr_sl_mode", "pr_hold_bars"):
            self.assertIn(k, params)
        h = self._src("app/static/dashboard.html")
        self.assertIn('<option value="pullback_resume">', h)
        for k in ("pr_sides", "pr_mode", "pr_hold_bars", "pr_sl_mode"):
            self.assertIn(f"addIfPresent('{k}'", h)


if __name__ == "__main__":
    unittest.main()
