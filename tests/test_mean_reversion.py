"""
r96 的測試：均值回歸策略(app/mean_reversion.py)，只存在回測。
執行：python -m unittest tests.test_mean_reversion -v
"""
import logging
import math
import os
import unittest
from datetime import datetime, timezone
from unittest import mock

logging.disable(logging.CRITICAL)

from app import backtest as B  # noqa: E402
from app import mean_reversion as M  # noqa: E402
from app import analysis as A  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOUR = 3_600_000
START = 494_444 * HOUR   # 整點對齊


def _bars(closes, wick=0.5):
    out = []
    for i, c in enumerate(closes):
        o = closes[i - 1] if i else c
        out.append({"bucket_start": START + i * HOUR, "open": o, "close": c,
                    "high": max(o, c) + wick, "low": min(o, c) - wick, "volume": 1.0})
    return out


def _calm(n, base=4000.0, amp=1.0):
    return [base + amp * math.sin(i / 1.6) for i in range(n)]


def _price_at_minute(m, crash_hour=75, drop=40.0, rise=45.0):
    """1分K路徑：前段小幅震盪、crash_hour 那一小時急跌 drop 點、之後 5 小時漲回 rise 點。"""
    h = m / 60
    calm = 4000.0 + 1.0 * math.sin(h / 1.6)
    if h < crash_hour:
        return calm
    base = 4000.0 + 1.0 * math.sin(crash_hour / 1.6)
    if h < crash_hour + 1:
        return base - drop * (h - crash_hour)
    bottom = base - drop
    return bottom + rise * min(1.0, (h - crash_hour - 1) / 5)


def _klines(hours=96, **kw):
    out = []
    for m in range(hours * 60):
        p = _price_at_minute(m, **kw)
        q = _price_at_minute(m + 1, **kw)
        out.append([START + m * 60_000, str(p), str(max(p, q) + 0.2), str(min(p, q) - 0.2), str(q), "1",
                    START + (m + 1) * 60_000 - 1])
    return out


def _run(klines, **kw):
    params = dict(days=len(klines) // 1440 or 1, strategy_type="mean_reversion", klines=klines, daily_klines=[],
                  trend_filter_mode=0, use_chop_filter=False, block_market_closed=False, min_atr_points=0,
                  use_atr=False, reversal_confirm_count=99)
    params.update(kw)
    with mock.patch.object(B, "fetch_historical_klines", return_value=[]):
        return B.run_backtest(**params)


class Indicators(unittest.TestCase):
    def test_rsi_and_atr_match_the_shared_functions(self):
        bars = _bars(_calm(80) + [3960.0, 3955.0, 3970.0])
        pre = M.precompute(bars)
        for i in (30, 60, 82):
            self.assertAlmostEqual(pre["rsi"][i], A.compute_rsi(bars[:i + 1]), places=9)
            self.assertAlmostEqual(pre["atr"][i], A.compute_atr(bars[:i + 1]), places=9)

    def test_causal_future_bars_do_not_change_past_results(self):
        closes = _calm(80) + [3960.0, 3950.0, 3990.0, 4010.0, 3940.0]
        cfg, _ = M.build_cfg({})
        short = M.precompute(_bars(closes[:81]))
        full = M.precompute(_bars(closes))
        for i in range(56, 81):
            self.assertEqual(M.evaluate_at(short, i, cfg), M.evaluate_at(full, i, cfg))


class Signals(unittest.TestCase):
    def setUp(self):
        self.cfg, _ = M.build_cfg({})

    def test_setup_a_long_after_crash(self):
        bars = _bars(_calm(80) + [3960.0])
        r = M.evaluate_at(M.precompute(bars), 80, self.cfg)
        self.assertEqual((r["stage"], r["direction"], r["mr"]["setup"]), ("訊號", "bullish", "A"))
        self.assertAlmostEqual(r["mr"]["tp_price"], r["mr"]["fast"], msg="預設停利是回到快線")
        self.assertAlmostEqual(r["mr"]["sl_points"], 1.5 * r["mr"]["atr"])

    def test_setup_a_short_after_spike(self):
        bars = _bars(_calm(80) + [4040.0])
        r = M.evaluate_at(M.precompute(bars), 80, self.cfg)
        self.assertEqual((r["direction"], r["mr"]["setup"]), ("bearish", "A"))

    def test_setups_2_disables_a(self):
        cfg, _ = M.build_cfg({"setups": 2})
        r = M.evaluate_at(M.precompute(_bars(_calm(80) + [3960.0])), 80, cfg)
        self.assertEqual(r["stage"], "中性")

    def test_setup_b_short_on_rally_into_falling_slow_line(self):
        closes = [4100.0 - i * 1.0 for i in range(70)]        # 一路往下：快線在慢線下、慢線下彎
        pre0 = M.precompute(_bars(closes))
        slow = pre0["slow"][69]
        closes += [slow - 2.0, slow - 4.0]                     # 從遠處彈到慢線下方，下一根上影線碰慢線、收黑
        bars = _bars(closes)
        bars[71]["high"] = slow + 1.0
        pre = M.precompute(bars)
        r = M.evaluate_at(pre, 71, self.cfg)
        self.assertEqual((r["stage"], r["direction"], r["mr"]["setup"]), ("訊號", "bearish", "B"))
        cfg_a, _ = M.build_cfg({"setups": 1})
        self.assertEqual(M.evaluate_at(pre, 71, cfg_a)["stage"], "中性", "只做A時B不觸發")

    def test_setup_b_not_when_close_stays_above_slow(self):
        closes = [4100.0 - i * 1.0 for i in range(70)]
        slow = M.precompute(_bars(closes))["slow"][69]
        pre = M.precompute(_bars(closes + [slow + 5.0]))
        self.assertGreater(pre["candles"][70]["close"], pre["slow"][70], "前提：這根收在慢線之上")
        r = M.evaluate_at(pre, 70, self.cfg)
        self.assertNotEqual(r.get("direction"), "bearish")

    def test_tp_falls_back_when_fast_line_too_close(self):
        bars = _bars(_calm(80) + [3960.0])
        pre = M.precompute(bars)
        fast = pre["fast"][80]
        r = M.evaluate_at(pre, 80, self.cfg, current_price=fast - 0.1)   # 進場價幾乎就在快線
        self.assertAlmostEqual(r["mr"]["tp_price"], fast - 0.1 + 1.5 * r["mr"]["atr"])
        cfg1, _ = M.build_cfg({"tp_mode": 1, "tp_atr": 2})
        r1 = M.evaluate_at(pre, 80, cfg1, current_price=3960.0)
        self.assertAlmostEqual(r1["mr"]["tp_price"], 3960.0 + 2 * r1["mr"]["atr"])

    def test_setup_a_needs_rsi_extreme(self):
        pre = M.precompute(_bars(_calm(80) + [3990.0]))
        self.assertGreater(pre["rsi"][80], 10, "前提：這根的RSI沒有低到10")
        default_cfg, _ = M.build_cfg({"setups": 1})
        self.assertEqual(M.evaluate_at(pre, 80, default_cfg)["direction"], "bullish", "前提：預設門檻30會進場")
        cfg, _ = M.build_cfg({"rsi_level": 10, "setups": 1})
        self.assertEqual(M.evaluate_at(pre, 80, cfg)["stage"], "中性", "離快線夠遠但RSI沒到門檻：不進場")

    def test_warmup_bar_54_is_neutral_even_if_crash(self):
        bars = _bars(_calm(54) + [3960.0])
        r = M.evaluate_at(M.precompute(bars), 54, self.cfg)
        self.assertEqual(r["stage"], "中性")
        self.assertIn("資料不足", r["chan"]["reason"])

    def test_warmup_is_neutral(self):
        self.assertEqual(M.evaluate_at(M.precompute(_bars(_calm(40))), 39, self.cfg)["stage"], "中性")


class Config(unittest.TestCase):
    def test_defaults_and_overrides(self):
        cfg, err = M.build_cfg({"rsi_level": "25", "setups": None, "max_hold_hours": "6"})
        self.assertIsNone(err)
        self.assertEqual((cfg["rsi_level"], cfg["setups"], cfg["max_hold_hours"]), (25.0, 3, 6))

    def test_rejects_bad_values(self):
        for bad in ({"setups": 4}, {"rsi_level": 60}, {"sl_atr": "x"}, {"setups": 1.5}, {"nope": 1}):
            cfg, err = M.build_cfg(bad)
            self.assertIsNone(cfg, bad)
            self.assertTrue(err, bad)

    def test_backtest_returns_error_for_bad_param(self):
        res = _run(_klines(hours=24), mr_rsi_level=80)
        self.assertIn("error", res)


class BacktestIntegration(unittest.TestCase):
    def test_crash_long_hits_take_profit(self):
        res = _run(_klines(), mr_setups=1)
        self.assertNotIn("error", res)
        self.assertEqual(res["signal_interval_seconds"], 3600, "訊號步固定1小時")
        longs = [t for t in res["recent_trades"] if t["direction"] == "bullish" and t.get("mr_setup") == "A"]
        self.assertEqual(len(longs), 1, f"前提：急跌那根收盤後做多一次 {res['recent_trades']}")
        t = longs[0]
        entry_ms = datetime.fromisoformat(t["entry_time"]).timestamp() * 1000
        self.assertEqual((entry_ms + 1) % HOUR, 0, "在1小時K收盤時進場")
        self.assertEqual(round((entry_ms + 1 - START) / HOUR), 76, "急跌那根(第75根)收盤後")
        self.assertEqual(t["exit_reason"], "觸及停利")
        self.assertAlmostEqual(t["exit_price"], t["tp_price"])
        self.assertGreater(t["pnl_points"], 0)
        self.assertEqual(res["mr_cfg"]["setups"], 1)

    def test_max_hold_exits(self):
        res = _run(_klines(rise=3.0), mr_setups=1, mr_max_hold_hours=2, mr_trail_atr=0, mr_sl_atr=8)
        longs = [t for t in res["recent_trades"] if t.get("mr_setup") == "A"]
        self.assertTrue(longs, "前提：有進場")
        self.assertEqual(longs[0]["exit_reason"], "超過持有時間")
        held = (datetime.fromisoformat(longs[0]["exit_time"]) - datetime.fromisoformat(longs[0]["entry_time"]))
        self.assertEqual(held.total_seconds(), 2 * 3600)

    def test_each_bar_enters_at_most_once(self):
        # 停損極小、進場後續跌：馬上停損出場，之後不能再用同一根急跌K重開
        res = _run(_klines(rise=-5.0), mr_setups=1, mr_sl_atr=0.2, mr_trail_atr=0, mr_max_hold_hours=0)
        self.assertTrue(all(t["exit_reason"] == "觸及停損" for t in res["recent_trades"]), "前提：是停損出場")
        entries = [t["entry_time"] for t in res["recent_trades"] if t.get("mr_setup") == "A"]
        self.assertGreaterEqual(len(entries), 2, "前提：續跌時每根新K都可能再給訊號")
        self.assertEqual(len(entries), len(set(entries)), "同一根K只進場一次")

    def test_chop_filter_not_applied(self):
        a = _run(_klines(), mr_setups=1, use_chop_filter=True, chop_threshold=0)
        b = _run(_klines(), mr_setups=1, use_chop_filter=False)
        self.assertEqual(a["total_trades"], b["total_trades"])
        self.assertGreater(a["total_trades"], 0)


class Isolation(unittest.TestCase):
    def _src(self, rel):
        with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
            return f.read()

    def test_live_paths_do_not_use_it(self):
        for rel in ("app/paper_trading.py", "app/execution.py", "app/signal_engine.py", "app/notifier.py"):
            self.assertIn("def ", self._src(rel), f"前提：讀到了 {rel}")
            self.assertNotIn("mean_reversion", self._src(rel), rel)

    def test_not_a_settings_field(self):
        self.assertIn("def get_settings", self._src("app/settings.py"), "前提：讀到了設定檔")
        self.assertNotIn("mr_", self._src("app/settings.py"))

    def test_endpoints_accept_params(self):
        from app import main
        params = main._backtest_job_params()
        for k in ("mr_setups", "mr_rsi_level", "mr_stretch_atr", "mr_touch_atr", "mr_tp_mode", "mr_tp_atr",
                  "mr_sl_atr", "mr_trail_atr", "mr_max_hold_hours"):
            self.assertIn(k, params)

    def test_dashboard_option_and_params(self):
        h = self._src("app/static/dashboard.html")
        self.assertIn('<option value="mean_reversion">', h)
        for k in ("mr_setups", "mr_rsi_level", "mr_sl_atr", "mr_max_hold_hours"):
            self.assertIn(f"addIfPresent('{k}'", h)
        self.assertIn("(data.strategy_type === 'mean_reversion' || data.strategy_type === 'pullback_resume'", h, "沒有引擎：不顯示套用按鈕")


if __name__ == "__main__":
    unittest.main()
