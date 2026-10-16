"""
r102 的測試：布林觸軌轉強的前瞻測試引擎(band_touch_3600，lab 純模擬、開平倉發 Telegram)。
執行：python -m unittest tests.test_band_touch_engine -v
"""
import logging
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

logging.disable(logging.CRITICAL)

from app import backtest as B  # noqa: E402
from app import paper_trading as pt  # noqa: E402
from app import pullback_resume as P  # noqa: E402
from app import signal_engine as SE  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOUR = 3_600_000
START = 494_444 * HOUR
CALM = [4000.0 + (i % 2) * 2 for i in range(39)]
PATH = CALM + [3995.0, 3990.0, 3984.0, 3991.0]          # 第 42 根收盤：碰下軌後轉強(做多)


def _bars(closes):
    out = []
    for i, c in enumerate(closes):
        o = closes[i - 1] if i else c - 1
        out.append({"bucket_start": START + i * HOUR, "open": o, "close": c,
                    "high": max(o, c) + 0.3, "low": min(o, c) - 0.3, "volume": 1.0})
    return out


def _now_ms():
    return int(datetime.now(timezone.utc).timestamp() * 1000)


class LiveSignal(unittest.TestCase):
    def test_cfg_is_the_backtested_one(self):
        c = SE.band_touch_cfg()
        self.assertEqual((c["mode"], c["exit_mode"], c["sides"], c["max_hold_hours"]), (2, 1, 3, P.MID_MAX_HOLD))
        self.assertEqual(pt.BAND_MAX_HOLD_SECONDS, P.MID_MAX_HOLD * 3600)

    def test_signal_on_last_closed_bar_matches_backtest(self):
        bars = _bars(PATH)
        now = START + 43 * HOUR + 60_000                    # 第 42 根剛收盤 1 分鐘
        r = SE.signal_band_touch(bars, current_price=3991.0, now_ms=now)
        self.assertEqual((r["stage"], r["direction"]), ("訊號", "bullish"))
        self.assertEqual(r["band"]["bar_close_ms"], START + 43 * HOUR)
        ref = P.evaluate_at(P.precompute(bars), 42, SE.band_touch_cfg(), current_price=3991.0)
        self.assertEqual(r["mr"], ref["mr"], "跟回測同一套判斷")
        self.assertEqual((r["band"]["sl_points"], r["band"]["tp_price"]), (ref["mr"]["sl_points"], ref["mr"]["tp_price"]))

    def test_in_progress_bar_is_ignored(self):
        bars = _bars(PATH)
        now = START + 42 * HOUR + 30 * 60_000                # 第 42 根還在進行中
        r = SE.signal_band_touch(bars, current_price=3991.0, now_ms=now)
        self.assertEqual(r["stage"], "中性", "只看已收盤的K：進行中那根不算")
        self.assertEqual(r["band"]["bar_close_ms"], START + 42 * HOUR)

    def test_entry_timing(self):
        now = _now_ms()
        ok = {"band": {"bar_close_ms": now - 60_000}}
        self.assertTrue(pt.band_entry_allowed(ok, None, now_ms=now)[0])
        self.assertFalse(pt.band_entry_allowed(ok, now - 60_000, now_ms=now)[0], "同一根K只進一次")
        late = {"band": {"bar_close_ms": now - (pt.BAND_ENTRY_WINDOW_SECONDS + 1) * 1000}}
        self.assertFalse(pt.band_entry_allowed(late, None, now_ms=now)[0], "收盤太久不追")
        self.assertFalse(pt.band_entry_allowed({"band": {}}, None, now_ms=now)[0])

    def test_backtest_rejects_live_strategy_name(self):
        res = B.run_backtest(days=1, strategy_type="band_touch", klines=[], daily_klines=[])
        self.assertIn("回拉轉強", res["error"])


class EngineTick(unittest.TestCase):
    def setUp(self):
        self.eng = pt.PaperTradingEngine(interval_seconds=3600, label="測試布林", strategy_type="band_touch",
                                         execution_index=99, engine_id="band_touch_test", notify_sim=True)
        self.eng._seeded_from_db = True
        self.notes = []
        real = pt.settings_module.get_settings
        self.settings_over = {}
        self.patches = [
            mock.patch.object(pt.settings_module, "settings_loaded", return_value=True),
            mock.patch.object(pt.settings_module, "get_settings",
                              side_effect=lambda *a, **k: dict(real(*a, **k), **self.settings_over)),
            mock.patch.object(pt.trading_core, "is_gold_market_closed", return_value=(False, "")),
            mock.patch.object(pt.notifier_module.notifier, "notify_trade_event",
                              side_effect=lambda **k: self.notes.append(k)),
            mock.patch.object(pt.db, "load_closed_paper_trades", return_value=(True, [])),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def _result(self, stage="訊號", direction="bullish", price=4000.0, bar_ago_s=60, sl=5.0, tp=4008.0, **kw):
        r = {"stage": stage, "direction": direction if stage == "訊號" else None, "current_price": price,
             "chan": {"reason": "測試"}, "profile": {"reason": ""}, "atr": 10.0, "choppiness_index": 99.0,
             "trend_filter": {"direction": "bearish"}, "bid": price - 0.1, "ask": price + 0.1, "book_stale": None,
             "band": {"bar_close_ms": _now_ms() - bar_ago_s * 1000}}
        if stage == "訊號":
            r["band"].update({"sl_points": sl, "tp_price": tp, "setup": "觸軌多"})
        r.update(kw)
        return r

    def tick(self, result):
        with mock.patch.object(pt, "compute_full_signal", return_value=result):
            self.eng._tick()

    def test_opens_with_band_stop_and_mid_target_and_notifies(self):
        self.tick(self._result(sl=7.3))
        pos = self.eng._position
        self.assertIsNotNone(pos, "前提：開倉了")
        self.assertAlmostEqual(pos["sl_price"], 4000.0 - 7.3, msg="停損用回拉極值算出來的距離，不是設定的點數")
        self.assertEqual(pos["tp_price"], 4008.0)
        self.assertEqual([n["action"] for n in self.notes], ["open"], "純模擬也通知")
        self.assertIsNone(self.notes[0]["executed"], "通知標成純模擬")

    def test_trend_and_chop_filters_are_ignored(self):
        self.settings_over = {"paper_trend_filter_mode": 2, "paper_use_chop_filter": 1, "paper_chop_threshold": 0}
        self.tick(self._result())
        self.assertIsNotNone(self.eng._position, "規則寫死不看趨勢/震盪濾網")

    def test_same_bar_and_late_bar_do_not_open(self):
        r = self._result()
        self.tick(r)
        self.eng._position = None                            # 假裝馬上停損出場
        self.tick(r)
        self.assertIsNone(self.eng._position, "同一根K不再進")
        self.tick(self._result(bar_ago_s=pt.BAND_ENTRY_WINDOW_SECONDS + 30))
        self.assertIsNone(self.eng._position, "訊號K收盤太久不追")

    def test_mid_band_take_profit(self):
        self.tick(self._result())
        self.tick(self._result(stage="中性", price=4008.5))
        self.assertIsNone(self.eng._position)
        close = [n for n in self.notes if n["action"] == "close"]
        self.assertEqual(close[0]["exit_reason"], "觸及中軌停利")

    def test_max_hold_exit(self):
        self.tick(self._result(tp=None))
        self.eng._position["entry_time"] = (datetime.now(timezone.utc) - timedelta(hours=12, seconds=1)).isoformat()
        self.tick(self._result(stage="中性", price=4001.0))
        self.assertIsNone(self.eng._position)
        self.assertEqual([n for n in self.notes if n["action"] == "close"][0]["exit_reason"], "持有時間到")

    def test_no_trailing_and_no_reversal_exit(self):
        self.settings_over = {"paper_reversal_confirm_count": 1, "paper_use_atr_stops": 0,
                              "paper_trail_trigger_points": 1, "paper_trail_distance_points": 1}
        self.tick(self._result(tp=None))
        pos = self.eng._position
        self.tick(self._result(stage="中性", price=4030.0))
        self.assertAlmostEqual(pos["sl_price"], 3995.0, msg="不移動停損")
        self.tick(self._result(direction="bearish", price=4001.0, bar_ago_s=10))
        self.assertIs(self.eng._position, pos, "反向訊號不提前出場")

    def test_restart_guard_reads_last_trade(self):
        bar_close = (_now_ms() // HOUR) * HOUR
        entry = datetime.fromtimestamp((bar_close + 30_000) / 1000, tz=timezone.utc).isoformat()
        with mock.patch.object(pt.db, "load_closed_paper_trades", return_value=(True, [{"entry_time": entry}])):
            self.eng._band_last_bar = None
            r = self._result()
            r["band"]["bar_close_ms"] = bar_close
            self.tick(r)
        self.assertIsNone(self.eng._position, "重啟後同一根K不再進")
        self.assertEqual(self.eng._band_last_bar, bar_close)

    def test_restart_guard_db_failure_does_not_block(self):
        with mock.patch.object(pt.db, "load_closed_paper_trades", side_effect=RuntimeError("db")):
            self.eng._band_last_bar = None
            self.tick(self._result())
        self.assertIsNotNone(self.eng._position)


class Registry(unittest.TestCase):
    def test_engine_registered_lab_only_and_others_unchanged(self):
        eng = pt.ALL_PAPER_TRADING_ENGINES["band_touch_3600"]
        self.assertEqual((eng.strategy_type, eng.interval_seconds, eng.notify_sim), ("band_touch", 3600, True))
        from app import role
        self.assertNotIn("band_touch_3600", role.LIVE_ENGINE_IDS)
        for eid, e in pt.ALL_PAPER_TRADING_ENGINES.items():
            if eid != "band_touch_3600":
                self.assertFalse(e.notify_sim, f"{eid} 維持只有真實下單才通知")
        idx = [e.execution_index for e in pt.ALL_PAPER_TRADING_ENGINES.values()]
        self.assertEqual(len(idx), len(set(idx)), "execution_index 不能重複")

    def test_dashboard_options(self):
        with open(os.path.join(ROOT, "app/static/dashboard.html"), encoding="utf-8") as f:
            h = f.read()
        self.assertEqual(h.count('<option value="band_touch_3600">'), 2)


if __name__ == "__main__":
    unittest.main()
