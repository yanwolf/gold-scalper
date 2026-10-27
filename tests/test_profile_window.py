"""
r112／r113 的測試：分價量表取樣(r113 加：每日換日、錨定中樞)(時間窗、即時跟回測一致)、標準 Value Area、日線大級別流動性。

背景(r112 查到的落差)：分價量表一直取「最後 3000 筆成交」。即時是真實逐筆，成交熱的時候只有幾分鐘；
回測是每根1分K拆4筆合成成交，3000 筆＝750 分鐘。同一個設定，即時跟回測看的時間差了上百倍，
回測結果因此不代表實盤。r112 加「時間窗」：即時跟回測都用1分K合成成交、看同樣的分鐘數。
預設 0＝照舊(行為跟 r111 一樣)，要用回測比較過再決定要不要套到引擎。

執行：python -m unittest tests.test_profile_window -v
"""
import logging
import os
import unittest
from unittest import mock

logging.disable(logging.CRITICAL)

from app import analysis as A  # noqa: E402
from app import backtest as B  # noqa: E402
from app import daily_smc as D  # noqa: E402
from app import paper_trading as pt  # noqa: E402
from app import settings as S  # noqa: E402
from app import signal_engine as SE  # noqa: E402
from tests.test_backtest_jobs import _klines_1m, FAKE  # noqa: E402
from tests.test_daily_smc import _path_up_then_down, _raw, T0, DAY  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIN = 60_000


def _row(level, vol):
    return {"price_level": float(level), "volume": float(vol)}


def _minute_bars(n, start=T0, base=4000.0, step=0.37):
    out, p = [], base
    for i in range(n):
        o = p
        p = base + ((i * step) % 9) - 4      # 在 base±4 之間來回，量分布有起伏
        out.append({"bucket_start": start + i * MIN, "open": o, "high": max(o, p) + 0.6,
                    "low": min(o, p) - 0.6, "close": p, "volume": 1.0 + (i % 7)})
    return out


def _bars_to_klines(bars):
    return [[b["bucket_start"], str(b["open"]), str(b["high"]), str(b["low"]), str(b["close"]), str(b["volume"]),
             b["bucket_start"] + MIN - 1] for b in bars]


class ValueArea(unittest.TestCase):
    TWO_PEAKS = [_row(4000, 10), _row(4001, 1), _row(4002, 1), _row(4003, 1), _row(4004, 9), _row(4005, 2)]

    def test_default_is_the_old_algorithm(self):
        old = A.poc_and_value_area(self.TWO_PEAKS)
        self.assertEqual(old, A.poc_and_value_area(self.TWO_PEAKS, method=A.VA_METHOD_LEGACY))
        self.assertEqual((old["poc"], old["value_area_low"], old["value_area_high"]), (4000.0, 4000.0, 4004.0),
                         "舊算法：挑最大量的 4000、4004 就湊滿 70%，區間橫跨中間的低量區")

    def test_standard_expands_contiguously_from_poc(self):
        std = A.poc_and_value_area(self.TWO_PEAKS, method=A.VA_METHOD_STANDARD)
        self.assertEqual(std["poc"], 4000.0)
        total = sum(r["volume"] for r in self.TWO_PEAKS)
        covered = sum(r["volume"] for r in self.TWO_PEAKS
                      if std["value_area_low"] <= r["price_level"] <= std["value_area_high"])
        self.assertGreaterEqual(covered, total * 0.7, "區間內的量要湊滿 70%")
        self.assertLessEqual(std["value_area_low"], std["poc"])
        self.assertGreaterEqual(std["value_area_high"], std["poc"])

    def test_standard_picks_heavier_side_first(self):
        rows = [_row(3998, 5), _row(3999, 1), _row(4000, 20), _row(4001, 8), _row(4002, 1)]
        std = A.value_area_contiguous(rows, 0.70)
        self.assertEqual((std["value_area_low"], std["value_area_high"]), (4000.0, 4001.0),
                         "20+8=28 ≥ 35×0.7=24.5：只往量大的上方併一格就夠")

    def test_far_away_cluster_changes_the_answer(self):
        rows = [_row(3990, 5), _row(3999, 1), _row(4000, 10), _row(4001, 3), _row(4002, 3)]
        old = A.poc_and_value_area(rows)
        std = A.poc_and_value_area(rows, method=A.VA_METHOD_STANDARD)
        self.assertEqual(old["value_area_low"], 3990.0, "前提：舊算法把 10 點外的 3990 拉進來")
        self.assertEqual((std["value_area_low"], std["value_area_high"]), (4000.0, 4002.0),
                         "標準算法只往相鄰擴張：4000→4001→4002 就湊滿")

    def test_standard_single_level_and_empty(self):
        self.assertEqual(A.value_area_contiguous([_row(4000, 3)])["value_area_high"], 4000.0)
        self.assertIsNone(A.poc_and_value_area([], method=1)["poc"])


class Sampling(unittest.TestCase):
    def test_synthetic_trades_match_backtest_split(self):
        bars = _minute_bars(5)
        live = A.candles_to_synthetic_trades(bars)
        bt = B.klines_to_synthetic_trades(_bars_to_klines(bars))
        self.assertEqual(len(live), 20, "前提：每根拆 4 筆")
        self.assertEqual(live, bt, "即時用的合成成交要跟回測一模一樣，回測結果才代表實盤")

    def test_window_keeps_exactly_n_minutes(self):
        trades = A.candles_to_synthetic_trades(_minute_bars(100))
        got = A.trades_in_window(trades, 30)
        self.assertEqual(len(trades), 400, "前提")
        self.assertEqual(len(got), 30 * 4)
        self.assertEqual(got[0]["time"], T0 + 70 * MIN, "從倒數第 30 根的開盤開始")
        self.assertEqual(A.trades_in_window(trades, 0), trades, "0＝不切")

    def test_zero_window_is_unchanged_from_r111(self):
        trades = A.candles_to_synthetic_trades(_minute_bars(1200))
        r = SE.compute_signal_from_trades(trades, interval_seconds=900, trade_limit=3000)
        ref = A.poc_and_value_area(A.compute_volume_profile(trades[-3000:]))
        pd = r["profile_detail"]
        self.assertEqual(len(trades), 4800, "前提：比 3000 筆多")
        self.assertEqual((pd["poc"], pd["value_area_high"], pd["value_area_low"]),
                         (ref["poc"], ref["value_area_high"], ref["value_area_low"]))
        self.assertEqual((pd["source"], pd["trade_count"], pd["window_minutes"]), ("trades", 3000, None))
        self.assertAlmostEqual(pd["covered_minutes"], 750 - 1 / 60000 * 1, delta=1, msg="回測的 3000 筆＝750 分鐘")

    def test_window_uses_given_profile_trades(self):
        trades = A.candles_to_synthetic_trades(_minute_bars(50))          # 纏論用的(短)
        profile_trades = A.candles_to_synthetic_trades(_minute_bars(600))  # 分價量表用的(長)
        r = SE.compute_signal_from_trades(trades, interval_seconds=900, profile_window_minutes=240,
                                          value_area_method=1, profile_trades=profile_trades)
        pd = r["profile_detail"]
        self.assertEqual((pd["source"], pd["window_minutes"], pd["value_area_method"], pd["trade_count"]),
                         ("candles", 240, 1, 240 * 4))
        ref = A.poc_and_value_area(A.compute_volume_profile(A.trades_in_window(profile_trades, 240)), method=1)
        self.assertEqual((pd["poc"], pd["value_area_low"], pd["value_area_high"]),
                         (ref["poc"], ref["value_area_low"], ref["value_area_high"]))

    def test_live_and_backtest_see_the_same_profile(self):
        """同一段1分K：即時路徑(1分K快取→合成)跟回測路徑(K線→合成→切窗)算出同一個 POC/VA。"""
        bars = _minute_bars(900)
        self.assertEqual(len(bars), 900, "前提")

        class FakeStream:
            def get_recent_trades(self, limit=0):
                return [{"time": bars[-1]["bucket_start"] + 59_000, "price": bars[-1]["close"], "qty": 5.0}] * 10

            def get_book_freshness(self):
                return {"stale": True}

            def get_latest(self):
                return None

            def get_recent_candles(self, interval_seconds=60, limit=None):
                src = bars if interval_seconds == 60 else A.resample_candles(bars, interval_seconds)
                return src[-limit:] if limit else src

        with mock.patch.object(SE, "binance_streamer", FakeStream()):
            live = SE.compute_full_signal(interval_seconds=900, profile_window_minutes=600, value_area_method=1)
        bt_trades = B.klines_to_synthetic_trades(_bars_to_klines(bars))
        bt = SE.compute_signal_from_trades(bt_trades, interval_seconds=900, profile_window_minutes=600, value_area_method=1)
        lp, bp = live["profile_detail"], bt["profile_detail"]
        self.assertEqual(lp["source"], "candles", "前提：即時真的走時間窗")
        self.assertEqual((lp["poc"], lp["value_area_low"], lp["value_area_high"], lp["trade_count"]),
                         (bp["poc"], bp["value_area_low"], bp["value_area_high"], bp["trade_count"]))

    def test_live_window_is_capped(self):
        seen = []

        class FakeStream:
            def get_recent_trades(self, limit=0):
                return [{"time": T0, "price": 4000.0, "qty": 1.0}] * 30

            def get_book_freshness(self):
                return {"stale": True}

            def get_latest(self):
                return None

            def get_recent_candles(self, interval_seconds=60, limit=None):
                seen.append((interval_seconds, limit))
                return _minute_bars(30)

        with mock.patch.object(SE, "binance_streamer", FakeStream()):
            SE.compute_full_signal(interval_seconds=900, profile_window_minutes=99999)
        self.assertIn((60, SE.PROFILE_WINDOW_MAX_MINUTES), seen)


def _utc_ms(iso):
    from datetime import datetime, timezone
    return int(datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp() * 1000)


class Anchor(unittest.TestCase):
    """r113：分價量表從「交易日開始」或「最新中樞開始」算(只存在回測)。"""

    def test_ny_session_start_handles_dst(self):
        self.assertEqual(A.ny_session_start_ms(_utc_ms("2026-10-27T22:30:00")), _utc_ms("2026-10-27T21:00:00"),
                         "夏令：紐約17:00＝UTC 21:00")
        self.assertEqual(A.ny_session_start_ms(_utc_ms("2026-10-27T20:59:00")), _utc_ms("2026-10-26T21:00:00"),
                         "還沒到17:00 → 前一天17:00")
        self.assertEqual(A.ny_session_start_ms(_utc_ms("2026-10-27T21:00:00")), _utc_ms("2026-10-27T21:00:00"), "剛好換日那一刻算新的一天")
        self.assertEqual(A.ny_session_start_ms(_utc_ms("2026-01-15T21:30:00")), _utc_ms("2026-01-14T22:00:00"),
                         "冬令：紐約17:00＝UTC 22:00")

    def test_session_anchor_and_just_rolled_over(self):
        last = _utc_ms("2026-10-27T23:00:00")
        start, note = A.profile_anchor_start_ms(A.PROFILE_ANCHOR_SESSION, last)
        self.assertEqual((start, note), (_utc_ms("2026-10-27T21:00:00"), "今日交易日起"))
        early = _utc_ms("2026-10-27T21:30:00")
        start, note = A.profile_anchor_start_ms(A.PROFILE_ANCHOR_SESSION, early)
        self.assertEqual(start, _utc_ms("2026-10-26T21:00:00"), "換日不到60分鐘：連前一個交易日一起算")
        self.assertIn("前一交易日", note)

    def test_zhongshu_anchor_and_fallback(self):
        last = T0 + 1000 * MIN
        start, note = A.profile_anchor_start_ms(A.PROFILE_ANCHOR_ZHONGSHU, last,
                                                {"latest_zhongshu": {"start_time": T0 + 400 * MIN}}, 900)
        self.assertEqual((start, note), (T0 + 400 * MIN, "最新中樞起"))
        start, note = A.profile_anchor_start_ms(A.PROFILE_ANCHOR_ZHONGSHU, last, {"latest_zhongshu": None}, 900)
        self.assertEqual(start, last - 50 * 900 * 1000, "沒有中樞：退回最近50根15分K")
        self.assertIn("退回", note)
        with self.assertRaises(ValueError):
            A.profile_anchor_start_ms(9, last)

    def test_trades_since(self):
        trades = A.candles_to_synthetic_trades(_minute_bars(10))
        got = A.trades_since(trades, T0 + 6 * MIN)
        self.assertEqual(len(trades), 40, "前提")
        self.assertEqual(len(got), 16, "從第6根的開盤(含)開始，4根×4筆")
        self.assertEqual(A.trades_since([], 0), [])

    def test_signal_uses_latest_zhongshu_start(self):
        trades = A.candles_to_synthetic_trades(_minute_bars(1200))
        self.assertEqual(len(trades), 4800, "前提")
        zs_start = trades[-1]["time"] - 300 * MIN + 1
        real = SE.analyze_chan

        def fake_chan(candles):
            out = real(candles)
            out["latest_zhongshu"] = {"start_time": zs_start}
            return out
        with mock.patch.object(SE, "analyze_chan", side_effect=fake_chan):
            r = SE.compute_signal_from_trades(trades, interval_seconds=900, profile_anchor=2, value_area_method=1)
        pd = r["profile_detail"]
        self.assertEqual((pd["source"], pd["profile_anchor"], pd["anchor_start_ms"]), ("anchor", 2, zs_start))
        ref = A.poc_and_value_area(A.compute_volume_profile(A.trades_since(trades, zs_start)), method=1)
        self.assertEqual((pd["poc"], pd["value_area_low"], pd["value_area_high"]),
                         (ref["poc"], ref["value_area_low"], ref["value_area_high"]))
        self.assertLess(pd["trade_count"], len(trades), "只取中樞之後的")

    def test_anchor_overrides_window(self):
        trades = A.candles_to_synthetic_trades(_minute_bars(3000))
        self.assertEqual(len(trades), 12000, "前提")
        r = SE.compute_signal_from_trades(trades, interval_seconds=900, profile_window_minutes=60, profile_anchor=1)
        pd = r["profile_detail"]
        self.assertEqual(pd["source"], "anchor")
        start = A.ny_session_start_ms(trades[-1]["time"])
        if trades[-1]["time"] - start < A.SESSION_MIN_MINUTES * MIN:
            start = A.ny_session_start_ms(start - 1)
        self.assertEqual(pd["anchor_start_ms"], start)
        self.assertEqual(pd["trade_count"], len(A.trades_since(trades, start)))

    def test_not_an_engine_setting(self):
        self.assertIn("paper_profile_window_minutes", S.FIELD_META, "前提：時間窗是引擎設定(對照組)")
        self.assertNotIn("profile_anchor", S.FIELD_META, "只存在回測：一鍵套用不會帶過去")
        self.assertNotIn("paper_profile_anchor", S.FIELD_META)


class SettingsAndEngine(unittest.TestCase):
    def test_defaults_keep_old_behaviour(self):
        for k in ("paper_profile_window_minutes", "paper_value_area_method"):
            self.assertIn(k, S.FIELD_META)
            self.assertEqual(S.FIELD_META[k]["default"], 0, f"{k} 預設要是 0(跟 r111 一樣)")
            self.assertIn(k, S.TRADING_RELEVANT_KEYS, "改了會影響交易，要算進設定變更時間")
        self.assertEqual(S.FIELD_META["paper_profile_window_minutes"]["max"], SE.PROFILE_WINDOW_MAX_MINUTES)

    def test_engine_passes_settings_to_signal(self):
        eng = pt.PaperTradingEngine(interval_seconds=900, label="測試15分", strategy_type="chan_profile",
                                    execution_index=99, engine_id="chan_profile_test")
        eng._seeded_from_db = True
        real = pt.settings_module.get_settings
        calls = []
        neutral = {"stage": "中性", "direction": None, "current_price": None}
        with mock.patch.object(pt.settings_module, "settings_loaded", return_value=True), \
             mock.patch.object(pt.settings_module, "get_settings",
                               side_effect=lambda *a, **k: dict(real(*a, **k), paper_profile_window_minutes=750,
                                                                paper_value_area_method=1)), \
             mock.patch.object(pt.db, "load_closed_paper_trades", return_value=(True, [])), \
             mock.patch.object(pt, "compute_full_signal", side_effect=lambda **k: calls.append(k) or dict(neutral)):
            eng._tick()
        self.assertEqual(len(calls), 1, "前提：這一輪真的算了訊號")
        self.assertEqual((calls[0]["profile_window_minutes"], calls[0]["value_area_method"]), (750, 1))


class Backtest(unittest.TestCase):
    def _run(self, **kw):
        closes = _path_up_then_down()
        start = T0 + len(closes) * DAY
        seen = []
        with mock.patch.object(B, "compute_signal_from_trades",
                               side_effect=lambda *a, **k: seen.append(k) or dict(FAKE, direction=None, stage="中性")), \
             mock.patch.object(B, "fetch_historical_klines", side_effect=AssertionError("給了資料不能再抓")):
            r = B.run_backtest(days=1, interval_seconds=900, klines=_klines_1m(1, start), daily_klines=_raw(closes),
                               trend_filter_mode=0, use_chop_filter=False, block_market_closed=False, min_atr_points=0,
                               use_atr=False, **kw)
        return r, seen

    def test_params_reach_the_signal_and_are_echoed(self):
        r, seen = self._run(profile_window_minutes=750, value_area_method=1)
        self.assertNotIn("error", r)
        self.assertTrue(seen, "前提：訊號步真的跑了")
        self.assertTrue(all(k["profile_window_minutes"] == 750 and k["value_area_method"] == 1 for k in seen))
        self.assertEqual((r["profile_window_minutes"], r["value_area_method"]), (750, 1),
                         "結果要帶實際用的值(一鍵套用、任務標籤都靠它)")

    def test_blank_uses_engine_settings(self):
        r, seen = self._run()
        self.assertTrue(seen, "前提")
        self.assertEqual((r["profile_window_minutes"], r["value_area_method"]), (0, 0))

    def test_bad_values_are_rejected(self):
        for kw in ({"profile_window_minutes": -5}, {"profile_window_minutes": 99999}, {"value_area_method": 2},
                   {"profile_window_minutes": "abc"}):
            r, seen = self._run(**kw)
            self.assertIn("error", r, kw)
            self.assertEqual(seen, [], "參數錯就不要跑")

    def test_anchor_reaches_signal_and_is_echoed(self):
        r, seen = self._run(profile_anchor=2)
        self.assertTrue(seen, "前提")
        self.assertTrue(all(k["profile_anchor"] == 2 for k in seen))
        self.assertEqual(r["profile_anchor"], 2)
        r, seen = self._run()
        self.assertTrue(seen, "前提")
        self.assertEqual(r["profile_anchor"], 0, "預設 0＝時間窗(跟 r112 一樣)")
        for bad in (3, -1, "x"):
            r, seen = self._run(profile_anchor=bad)
            self.assertIn("error", r, bad)
            self.assertEqual(seen, [])

    def test_job_label_keys(self):
        from app import backtest_jobs as J
        self.assertIn("profile_window_minutes", J.LABEL_PARAM_KEYS)
        self.assertIn("value_area_method", J.LABEL_PARAM_KEYS)
        self.assertIn("profile_anchor", J.LABEL_PARAM_KEYS)


def _daily(lows_highs, start=T0):
    """[(low, high), ...] → 專案日K格式。"""
    out = []
    for i, (lo, hi) in enumerate(lows_highs):
        mid = (lo + hi) / 2
        out.append({"bucket_start": start + i * DAY, "open": mid, "high": hi, "low": lo, "close": mid,
                    "volume": 1.0, "close_time": start + (i + 1) * DAY - 1})
    return out


def _v_shape(depth_at, n=60, base=4200.0, bottom=3900.0):
    """在 depth_at 那根做一個明顯的低點，其他時間在 base 附近小幅波動。"""
    rows = []
    for i in range(n):
        d = abs(i - depth_at)
        lo = bottom + d * 15 if d < 20 else base - 30 + (i % 3) * 5
        rows.append((lo, lo + 40))
    return rows


class Liquidity(unittest.TestCase):
    def test_untaken_low_is_listed_and_swept_low_is_not(self):
        rows = _v_shape(20)
        candles = _daily(rows)
        got = D.liquidity_levels(candles, 4150.0, major_n=5)
        self.assertTrue(any(abs(lv["price"] - 3900.0) < 1e-6 for lv in got["below"]), got["below"])
        swept = _daily(rows + [(3880.0, 3950.0)] + [(4100.0, 4140.0)] * 6)
        got2 = D.liquidity_levels(swept, 4150.0, major_n=5)
        self.assertFalse(any(abs(lv["price"] - 3900.0) < 1e-6 for lv in got2["below"]),
                         "之後有日K跌破 3900 → 那裡的停損已經被掃過，不再是流動性")

    def test_equal_lows_merge(self):
        rows = _v_shape(15, n=40) + [(lo + 0, hi) for lo, hi in _v_shape(15, n=40, bottom=3905.0)]
        got = D.liquidity_levels(_daily(rows), 4150.0, major_n=5)
        self.assertTrue(got["below"], "前提：有低點")
        eq = got["below"][0]
        self.assertEqual(eq["equal_count"], 2, "3900 和 3905 相差 0.13%：等低點")
        self.assertEqual(eq["price"], 3900.0, "取比較低的那個(停損在更下面)")
        self.assertEqual(len(eq["dates"]), 2)

    def test_void_to_next_level(self):
        rows = _v_shape(15, n=40, bottom=3500.0) + _v_shape(15, n=40, bottom=3900.0)
        got = D.liquidity_levels(_daily(rows), 4150.0, major_n=5)
        self.assertEqual([lv["price"] for lv in got["below"][:2]], [3900.0, 3500.0])
        self.assertEqual((got["void_below"]["from"], got["void_below"]["to"], got["void_below"]["points"]),
                         (3900.0, 3500.0, 400.0))

    def test_no_lower_level_says_so(self):
        got = D.liquidity_levels(_daily(_v_shape(20)), 4150.0, major_n=5)
        self.assertIsNotNone(got["void_below"], "前提：有最近的防守點")
        self.assertIsNone(got["void_below"]["to"])
        self.assertFalse(got["long_used"])

    def test_long_history_only_fills_older_untaken_levels(self):
        xau = _daily(_v_shape(20, bottom=3900.0), start=T0 + 100 * DAY)
        older_ok = _daily(_v_shape(30, n=100, bottom=3300.0), start=T0)            # 3300：XAU 沒跌到過
        got = D.liquidity_levels(xau, 4150.0, long_candles=older_ok, major_n=5)
        prices = [lv["price"] for lv in got["below"]]
        self.assertTrue(got["long_used"])
        self.assertIn(3300.0, prices)
        self.assertEqual(got["void_below"]["to"], 3300.0)
        self.assertEqual(got["void_below"]["to_source"], D.LONG_HISTORY_SYMBOL)
        xau_deep = _daily(_v_shape(20, bottom=3250.0), start=T0 + 100 * DAY)    # XAU 自己跌破過 3300
        got2 = D.liquidity_levels(xau_deep, 4150.0, long_candles=older_ok, major_n=5)
        self.assertNotIn(3300.0, [lv["price"] for lv in got2["below"]], "XAUUSDT 期間已經掃過，不能再列")
        overlap = _daily(_v_shape(150, n=200, bottom=3000.0), start=T0)        # 低點在 XAU 開始之後：不採用
        got3 = D.liquidity_levels(xau, 4150.0, long_candles=overlap, major_n=5)
        self.assertNotIn(3000.0, [lv["price"] for lv in got3["below"]], "XAUUSDT 有資料的期間一律以 XAUUSDT 為準")

    def test_snapshot_exposes_liquidity_not_internal_candles(self):
        closes = _path_up_then_down()
        self.assertGreater(len(closes), D.DAILY_MIN_CANDLES, "前提：日K夠判斷")
        D.refresh(fetcher=lambda symbol, limit: _raw(closes))
        snap = D.get_snapshot(current_price=closes[-1])
        self.assertTrue(snap["available"], "前提")
        self.assertIn("liquidity", snap)
        self.assertNotIn("_levels_candles", snap, "內部用的日K不放進 API")
        self.assertIsNotNone(D._state["snapshot"].get("_levels_candles"), "快取裡還在，下一次查詢還能算")
        self.assertIn("liquidity", D.get_snapshot(current_price=closes[-1]), "第二次查詢一樣有")

    def test_long_history_failure_keeps_previous_and_waits(self):
        saved = dict(D._state)
        try:
            D._state.update({"long_candles": [{"bucket_start": 1, "high": 2.0, "low": 1.0}], "long_fetched_at": None})
            calls = []

            def boom():
                calls.append(1)
                raise RuntimeError("x")
            got = D.refresh_long_history(fetcher=boom, now=1000.0)
            self.assertEqual(len(calls), 1, "前提：真的試著抓了")
            self.assertEqual(got, [{"bucket_start": 1, "high": 2.0, "low": 1.0}], "失敗沿用上一次")
            self.assertIn("RuntimeError", D._state["long_error"])
            D.refresh_long_history(fetcher=boom, now=1000.0 + 60)
            self.assertEqual(len(calls), 1, "失敗後也要等一輪，不能每 30 分鐘一直打")
        finally:
            D._state.clear()
            D._state.update(saved)

    def test_long_history_uses_spot_batch(self):
        seen = []
        with mock.patch("app.binance_weight.get_klines", side_effect=lambda p, **k: seen.append((p, k)) or []):
            D._fetch_long_history()
        self.assertEqual(len(seen), 1, "前提")
        self.assertEqual((seen[0][0]["symbol"], seen[0][1]["market"], seen[0][1]["mode"]), ("PAXGUSDT", "spot", "batch"),
                         "現貨K線只能走 batch(binance_weight 的規定)")


class Dashboard(unittest.TestCase):
    def test_dashboard_has_new_fields(self):
        with open(os.path.join(ROOT, "app/static/dashboard.html"), encoding="utf-8") as f:
            html = f.read()
        for needle in ("backtestProfileWindow", "backtestVaMethod", "addIfPresent('profile_window_minutes'",
                       "addIfPresent('value_area_method'", "paper_profile_window_minutes: data.profile_window_minutes",
                       "'paper_profile_window_minutes', 'paper_value_area_method'", "function profileSampleText",
                       "function chanIntervalText", "function dailyLiquidityLines", "下方沒有還有效的多方 OB/FVG",
                       "backtestProfileAnchor", "addIfPresent('profile_anchor'", "量表:錨定中樞", "分價量表錨點"):
            self.assertIn(needle, html, needle)
        self.assertEqual(html.count("paper_profile_window_minutes: data.profile_window_minutes"), 2,
                         "回測結果的兩個「套用」入口都要帶過去")


if __name__ == "__main__":
    unittest.main()
