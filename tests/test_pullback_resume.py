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


class R100Labels(unittest.TestCase):
    """r100：沒有資料的段落不算「出錯」；清單標籤分得出回拉轉強有沒有開保護停損、持有幾根。"""

    def _job(self, side_effect, params=None):
        from app import backtest_jobs as J
        with mock.patch.object(B, "run_backtest", side_effect=side_effect), \
             mock.patch.object(J.db, "save_backtest_job", return_value=None):
            out = J.start_job(dict({"interval_seconds": 3600}, **(params or {})), days=1, windows=len(side_effect))
            self.assertTrue(J.wait_idle(10))
            return J.get_job(out["job_id"]), J

    NO_DATA = {"error": "這段期間幣安沒有 XAUUSDT 的K線資料", "no_data": True}
    GOOD = {"total_trades": 0, "recent_trades": []}

    def test_no_data_is_not_counted_as_error(self):
        job, _ = self._job([dict(self.NO_DATA), dict(self.GOOD), dict(self.GOOD)])
        ag = job["aggregate"]
        self.assertNotIn("missing_windows", ag)
        self.assertEqual(ag["no_data_windows"], 1)

    def test_no_data_and_error_are_separate(self):
        job, _ = self._job([dict(self.NO_DATA), RuntimeError("x"), dict(self.GOOD)])
        ag = job["aggregate"]
        self.assertEqual((ag["missing_windows"], ag["no_data_windows"]), (1, 1))

    def test_label_params_take_actual_pr_cfg(self):
        res = dict(self.GOOD, strategy_type="pullback_resume",
                   mr_cfg={"mode": 0, "sides": 3, "trend": 0, "sl_mode": 1, "hold_bars": 2})
        job, J = self._job([res], params={"strategy_type": "pullback_resume"})
        lp = J._label_params(job)
        self.assertEqual((lp["pr_sl_mode"], lp["pr_hold_bars"], lp["pr_mode"]), (1, 2, 0))

    def test_label_params_fall_back_to_sent_params(self):
        job, J = self._job([RuntimeError("x")], params={"strategy_type": "pullback_resume", "pr_sl_mode": 1})
        self.assertEqual(J._label_params(job)["pr_sl_mode"], 1)

    def test_dashboard_shows_stop_and_no_data_note(self):
        with open(os.path.join(ROOT, "app/static/dashboard.html"), encoding="utf-8") as f:
            h = f.read()
        self.assertIn("'・回拉低點停損' : '・無停損'", h)
        self.assertIn("p.pr_sl_mode = first.result.mr_cfg.sl_mode", h)
        self.assertIn("ag.no_data_windows", h)


CALM = [4000.0 + (i % 2) * 2 for i in range(39)]          # 0..38 陰陽交錯，最後收 4000
BAND_PATH = CALM + [3995.0, 3990.0, 3984.0, 3991.0, 3996.0, 4000.0, 4005.0, 4006.0, 4007.0, 4008.0]  # 48 小時


class R101BandTouch(unittest.TestCase):
    """r101：布林觸軌轉強(mode=2)與回中軌出場(exit_mode=1)。"""

    def test_bollinger_is_population_std(self):
        closes = [float(x) for x in range(1, 21)]
        mid, up, lo = P._bollinger(closes)
        m = sum(closes) / 20
        sd = (sum((x - m) ** 2 for x in closes) / 20) ** 0.5
        self.assertAlmostEqual(mid[19], m)
        self.assertAlmostEqual(up[19], m + 2 * sd)
        self.assertAlmostEqual(lo[19], m - 2 * sd)
        self.assertIsNone(mid[18])

    def _pre(self, closes=None):
        return P.precompute(_bars(closes or BAND_PATH[:43]))

    def test_long_after_touching_lower_band(self):
        pre = self._pre()
        self.assertLessEqual(pre["candles"][41]["low"], pre["bb_lo"][41], "前提：第 41 根碰到下軌")
        r = P.evaluate_at(pre, 42, cfg(mode=2))
        self.assertEqual((r["direction"], r["mr"]["setup"]), ("bullish", "觸軌多"))
        self.assertIn("碰布林下軌", r["chan"]["reason"])

    def test_no_touch_no_signal(self):
        wide = [4000.0 + (i % 2) * 10 for i in range(39)]      # 波動大、通道寬
        closes = wide + [3999.0, 3998.5, 3998.0, 4001.0]       # 一樣回拉三根再收回，但沒碰到下軌
        pre = P.precompute(_bars(closes))
        self.assertTrue(all(pre["candles"][j]["low"] > pre["bb_lo"][j] for j in (39, 40, 41)), "前提")
        r = P.evaluate_at(pre, 42, cfg(mode=2))
        self.assertEqual(r["stage"], "中性")
        self.assertIn("沒有碰到", r["chan"]["reason"])

    def test_wick_touch_on_first_pullback_bar_counts(self):
        wide = [4000.0 + (i % 2) * 10 for i in range(39)]
        bars = _bars(wide + [3999.0, 3998.5, 3998.0, 4001.0])
        pre = P.precompute(bars)
        bars[39]["low"] = pre["bb_lo"][39] - 0.5               # 只有第一根回拉K的下影線碰到，收盤沒碰到
        self.assertGreater(bars[39]["close"], pre["bb_lo"][39], "前提：收盤在下軌之上")
        r = P.evaluate_at(pre, 42, cfg(mode=2))
        self.assertEqual(r["direction"], "bullish")

    def test_trend_is_ignored_and_stop_is_forced(self):
        pre = self._pre()
        pre["ema"][42] = 9999.0                               # 一般模式下趨勢不成立
        self.assertEqual(P.evaluate_at(pre, 42, cfg(mode=0, sides=1))["stage"], "中性", "前提")
        r = P.evaluate_at(pre, 42, cfg(mode=2, sl_mode=0), current_price=3991.0)
        self.assertEqual(r["direction"], "bullish")
        lowest = min(pre["candles"][j]["low"] for j in (39, 40, 41))
        self.assertAlmostEqual(r["mr"]["sl_points"], 3991.0 - lowest, msg="停損固定在回拉極值，不管 sl_mode")

    def test_short_mirror_touching_upper_band(self):
        closes = CALM[:-1] + [4002.0, 4007.0, 4012.0, 4018.0, 4011.0]   # 陽線三根衝上軌後轉弱
        closes = [c for c in closes]
        pre = P.precompute(_bars(closes))
        i = len(closes) - 1
        self.assertGreaterEqual(pre["candles"][i - 1]["high"], pre["bb_up"][i - 1], "前提")
        r = P.evaluate_at(pre, i, cfg(mode=2))
        self.assertEqual((r["direction"], r["mr"]["setup"]), ("bearish", "觸軌空"))

    def test_mid_band_exit_target(self):
        pre = self._pre()
        r = P.evaluate_at(pre, 42, cfg(mode=2, exit_mode=1), current_price=3991.0)
        self.assertAlmostEqual(r["mr"]["tp_price"], pre["bb_mid"][42])
        self.assertGreater(r["mr"]["tp_price"], 3991.0)
        r2 = P.evaluate_at(pre, 42, cfg(mode=2, exit_mode=1), current_price=pre["bb_mid"][42] + 1)
        self.assertIsNone(r2["mr"]["tp_price"], "中軌不在獲利方向：沒有停利價")
        self.assertIsNone(P.evaluate_at(pre, 42, cfg(mode=2))["mr"]["tp_price"], "exit_mode=0 沒有停利價")

    def test_max_hold_follows_exit_mode(self):
        self.assertEqual(cfg(exit_mode=1)["max_hold_hours"], P.MID_MAX_HOLD)
        self.assertEqual(cfg(exit_mode=0, hold_bars=3)["max_hold_hours"], 3)
        self.assertIsNone(P.build_cfg({"mode": 3})[0])
        self.assertIsNone(P.build_cfg({"exit_mode": 2})[0])

    def test_backtest_hits_mid_band(self):
        res = _run(path=BAND_PATH, pr_mode=2, pr_exit_mode=1, pr_sides=1)
        trades = sorted(res["recent_trades"], key=lambda t: t["entry_time"])
        self.assertTrue(trades, "前提：有進場")
        t = trades[0]
        self.assertEqual(t["mr_setup"], "觸軌多")
        self.assertAlmostEqual(t["entry_price"], 3991.0, places=6)
        self.assertEqual(t["exit_reason"], "觸及停利")
        self.assertAlmostEqual(t["exit_price"], t["tp_price"])
        self.assertEqual(res["mr_cfg"]["max_hold_hours"], P.MID_MAX_HOLD)

    def test_backtest_hold_one_bar(self):
        res = _run(path=BAND_PATH, pr_mode=2, pr_sides=1)
        t = sorted(res["recent_trades"], key=lambda t: t["entry_time"])[0]
        self.assertEqual(t["exit_reason"], "持有時間到")
        self.assertAlmostEqual(t["exit_price"], 3996.0, places=6)

    def test_labels_and_dashboard(self):
        from app import backtest_jobs as J
        self.assertIn("pr_exit_mode", J.LABEL_PARAM_KEYS)
        self.assertEqual(J._MR_CFG_LABEL["pullback_resume"]["exit_mode"], "pr_exit_mode")
        from app import main
        self.assertIn("pr_exit_mode", main._backtest_job_params())
        with open(os.path.join(ROOT, "app/static/dashboard.html"), encoding="utf-8") as f:
            h = f.read()
        self.assertIn('<option value="2">布林觸軌轉強', h)
        self.assertIn("addIfPresent('pr_exit_mode'", h)
        self.assertIn("'・回中軌出場'", h)
