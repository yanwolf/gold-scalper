"""
r89 日線SMC結構參考的測試。這是功能測試，不是清單(BINANCE_LESSONS.md)的對照測試，所以獨立一個檔，
不放進 test_lessons(舊版重跑只跑 test_lessons；這裡測的全是 r89 才有的東西，舊版上本來就不存在)。

重點：
  - 只用已收盤日K、純因果(回測不看未來)
  - 方向＝最近一次結構突破；CHoCH 後有同方向 BOS 才算已確認
  - 交易迴圈只讀快取：開倉標記不打網路、不拋例外；抓不到時沿用上一次、會標過期
  - 純參考：日線濾網只存在回測，開倉決策完全不看日線
執行：python -m unittest tests.test_daily_smc -v
"""
import logging
import time
import unittest
from unittest import mock

logging.disable(logging.CRITICAL)

from app import daily_smc as D  # noqa: E402
from app import trading_stats as TS  # noqa: E402
from app import db  # noqa: E402

DAY = 86400 * 1000
T0 = 1_700_000_000_000 - (1_700_000_000_000 % DAY)   # 對齊 UTC 00:00


def _path_up_then_down():
    """先一段上升波浪(高點越來越高)，再轉成下降波浪(跌破前一個上升低點＝CHoCH，再破新低＝BOS)。"""
    closes, p = [], 4000.0
    for _ in range(6):                       # 上升：漲 8 天、回 4 天
        for _ in range(8):
            p += 10
            closes.append(p)
        for _ in range(4):
            p -= 6
            closes.append(p)
    for _ in range(5):                       # 下降：跌 8 天、彈 4 天
        for _ in range(8):
            p -= 10
            closes.append(p)
        for _ in range(4):
            p += 6
            closes.append(p)
    return closes


def _raw(closes, start=T0):
    """幣安原始日K格式 [open_time, o, h, l, c, v, close_time, ...]。"""
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        o = prev
        out.append([start + i * DAY, str(o), str(max(o, c) + 1), str(min(o, c) - 1), str(c), "100",
                    start + (i + 1) * DAY - 1])
        prev = c
    return out


def _candles(closes):
    return D.klines_to_closed_candles(_raw(closes), now_ms=T0 + (len(closes) + 5) * DAY)


class ClosedCandles(unittest.TestCase):
    def test_unclosed_last_candle_is_dropped(self):
        raw = _raw([4000, 4010, 4020])
        now = T0 + 2 * DAY + 3600_000          # 第三根(index 2)還在進行中
        got = D.klines_to_closed_candles(raw, now_ms=now)
        self.assertEqual(len(raw), 3, "前提：給了三根")
        self.assertEqual([c["close"] for c in got], [4000.0, 4010.0], "進行中的那根不能拿來判斷結構")

    def test_bad_rows_are_skipped(self):
        got = D.klines_to_closed_candles([["x"], None, _raw([4000])[0]], now_ms=T0 + 5 * DAY)
        self.assertEqual(len(got), 1)


class Structure(unittest.TestCase):
    def test_uptrend_then_breakdown_turns_bearish(self):
        closes = _path_up_then_down()
        candles = _candles(closes)
        self.assertGreater(len(candles), D.DAILY_MIN_CANDLES, "前提：資料夠長")
        up_part = candles[:72]
        s_up = D.summarize(up_part)
        self.assertEqual(s_up["bias"], "bullish", f"上升段結束時應偏多：{s_up.get('recent_events')}")
        s = D.summarize(candles)
        self.assertEqual(s["bias"], "bearish", f"下降段之後應偏空：{s.get('recent_events')}")
        self.assertTrue(s["confirmed"], "CHoCH 之後又破了新低，應該是已確認")
        self.assertGreaterEqual(s["bos_after_choch"], 1)
        self.assertEqual(s["recent_events"][-1]["direction"], "bearish")
        self.assertIsNotNone(s["choch"])
        self.assertEqual(s["choch"]["direction"], "bearish")
        self.assertTrue(s["choch"]["date"] and len(s["choch"]["date"]) == 10, "日期用 UTC 的 YYYY-MM-DD")

    def test_choch_only_is_not_confirmed(self):
        closes = _path_up_then_down()
        candles = _candles(closes)
        series = D.bias_series(candles)
        # 找出第一次翻空的那根：那一刻只有 CHoCH，還沒有同方向 BOS
        first = next(i for i, x in enumerate(series) if x["bias"] == "bearish")
        self.assertIsNotNone(series[first - 1]["bias"], "前提：翻空之前已經有方向")
        self.assertFalse(series[first]["confirmed"], "剛出現 CHoCH 時不能算已確認")
        s = D.summarize(candles[:first + 1])
        self.assertEqual(s["bias"], "bearish")
        self.assertFalse(s["confirmed"])
        self.assertEqual(s["bos_after_choch"], 0)

    def test_too_few_candles_is_neutral(self):
        s = D.summarize(_candles([4000 + i for i in range(10)]))
        self.assertIsNone(s["bias"])
        self.assertIn("不足", s["reason"])

    def test_series_is_causal(self):
        """第 i 根的方向只能用到 <=i 的資料：拿整段算的序列，跟只給前 k 根算的結果逐根一致。"""
        candles = _candles(_path_up_then_down())
        full = D.bias_series(candles)
        self.assertTrue(any(x["bias"] == "bullish" for x in full) and any(x["bias"] == "bearish" for x in full),
                        "前提：序列裡真的有翻向")
        for k in (40, 60, 75, 90, 110, len(candles)):
            part = D.bias_series(candles[:k])
            self.assertEqual(part, full[:k], f"前 {k} 根的結果被之後的資料改到了(看到未來)")

    def test_series_matches_summarize_at_the_end(self):
        candles = _candles(_path_up_then_down())
        full = D.bias_series(candles)
        s = D.summarize(candles)
        self.assertEqual(full[-1]["bias"], s["bias"])
        self.assertEqual(full[-1]["confirmed"], s["confirmed"])


class Zones(unittest.TestCase):
    def test_locate_zones_by_price(self):
        zones = [{"kind": "OB", "side": "bearish", "top": 4490, "bot": 4330},
                 {"kind": "OB", "side": "bullish", "top": 4080, "bot": 3980},
                 {"kind": "FVG", "side": "bearish", "top": 4200, "bot": 4150}]
        above, below, inside = D.locate_zones(zones, 4153.6)
        self.assertEqual([z["bot"] for z in inside], [4150])
        self.assertEqual([z["bot"] for z in above], [4330])
        self.assertEqual([z["top"] for z in below], [4080])
        self.assertEqual(D.locate_zones(zones, None), ([], [], []))


class Cache(unittest.TestCase):
    def setUp(self):
        self._saved = dict(D._state)
        for k in D._state:
            D._state[k] = None

    def tearDown(self):
        D._state.update(self._saved)

    def test_tag_without_data_is_unknown(self):
        tag = D.tag_for_trade()
        self.assertIsNone(tag["bias"])
        self.assertTrue(tag["stale"])
        self.assertEqual(TS.daily_smc_alignment(tag, "bullish"), "unknown")

    def test_refresh_then_tag_does_not_touch_network(self):
        closes = _path_up_then_down()
        now = time.time()
        # 最後一根收盤在 1 小時前：新鮮
        start = int(now * 1000) - len(closes) * DAY - 3600_000
        snap = D.refresh("XAUUSDT", fetcher=lambda symbol, limit: _raw(closes, start=start))
        self.assertIsNotNone(snap, "前提：刷新成功")
        self.assertEqual(snap["bias"], "bearish")
        with mock.patch.object(D, "fetch_daily_klines", side_effect=AssertionError("交易路徑不能打網路")):
            tag = D.tag_for_trade()
            view = D.get_snapshot(current_price=snap["last_close"])
        self.assertEqual(tag["bias"], "bearish")
        self.assertFalse(tag["stale"])
        self.assertTrue(view["available"])
        self.assertTrue(view["reference_only"])

    def test_refresh_failure_keeps_last_snapshot_and_marks_error(self):
        closes = _path_up_then_down()
        start = int(time.time() * 1000) - len(closes) * DAY - 3600_000
        first = D.refresh("XAUUSDT", fetcher=lambda symbol, limit: _raw(closes, start=start))
        self.assertEqual(first["bias"], "bearish", "前提：先有一份好的快照")
        again = D.refresh("XAUUSDT", fetcher=mock.Mock(side_effect=RuntimeError("inj-timeout")))
        self.assertEqual(again["bias"], "bearish", "抓失敗要沿用上一次，不能變成沒有")
        self.assertIn("inj-timeout", D._state["last_error"])
        self.assertIn("inj-timeout", D.get_snapshot()["last_error"])

    def test_old_snapshot_is_stale(self):
        closes = _path_up_then_down()
        start = int(time.time() * 1000) - len(closes) * DAY - 3 * DAY   # 最後一根收盤在 3 天前
        D.refresh("XAUUSDT", fetcher=lambda symbol, limit: _raw(closes, start=start))
        tag = D.tag_for_trade()
        self.assertEqual(tag["bias"], "bearish", "前提：有資料")
        self.assertTrue(tag["stale"])
        self.assertEqual(TS.daily_smc_alignment(tag, "bearish"), "unknown", "過期的標記不能算順勢")

    def test_intraday_note_only_hints(self):
        closes = _path_up_then_down()
        start = int(time.time() * 1000) - len(closes) * DAY - 3600_000
        snap = D.refresh("XAUUSDT", fetcher=lambda symbol, limit: _raw(closes, start=start))
        self.assertIsNotNone(snap["break_down_level"], "前提：有往下破結構的參考價")
        view = D.get_snapshot(current_price=snap["break_down_level"] - 5)
        self.assertIn("收盤才算數", view["intraday_note"])
        self.assertEqual(view["bias"], snap["bias"], "盤中越過不能改方向")


class Breakdown(unittest.TestCase):
    def test_groups(self):
        fresh = lambda b: {"bias": b, "confirmed": True, "as_of": "2026-09-27", "stale": False}
        trades = [
            {"direction": "bearish", "entry_time": "2026-09-27T01:00:00", "pnl_points": 30, "daily_smc": fresh("bearish")},
            {"direction": "bearish", "entry_time": "2026-09-27T02:00:00", "pnl_points": -10, "daily_smc": fresh("bearish")},
            {"direction": "bullish", "entry_time": "2026-09-27T03:00:00", "pnl_points": -20, "daily_smc": fresh("bearish")},
            {"direction": "bullish", "entry_time": "2026-09-27T04:00:00", "pnl_points": 5},   # r89 以前的舊單
        ]
        rows = {r["key"]: r for r in TS.compute_daily_smc_breakdown(trades)}
        self.assertEqual(rows["aligned"]["total_trades"], 2)
        self.assertEqual(rows["aligned"]["total_pnl_points"], 20)
        self.assertEqual(rows["aligned"]["profit_factor"], 3.0)
        self.assertEqual(rows["aligned"]["confirmed_count"], 2)
        self.assertEqual(rows["against"]["total_trades"], 1)
        self.assertEqual(rows["unknown"]["total_trades"], 1)
        self.assertEqual(sum(r["total_trades"] for r in rows.values()), len(trades), "每一筆都要分到某一組")

    def test_all_wins_marks_infinite(self):
        rows = TS.compute_daily_smc_breakdown([{"direction": "bullish", "entry_time": "x", "pnl_points": 5,
                                                "daily_smc": {"bias": "bullish", "stale": False}}])
        aligned = next(r for r in rows if r["key"] == "aligned")
        self.assertIsNone(aligned["profit_factor"])
        self.assertTrue(aligned["profit_factor_infinite"])


class DbRoundTrip(unittest.TestCase):
    def test_dump_and_parse(self):
        tag = {"bias": "bearish", "confirmed": False, "as_of": "2026-09-27", "stale": False}
        self.assertEqual(db._parse_daily_smc(db._dump_daily_smc(tag)), tag)
        self.assertIsNone(db._dump_daily_smc(None))
        self.assertIsNone(db._parse_daily_smc(None))
        self.assertIsNone(db._parse_daily_smc("{not json"))
        self.assertIsNone(db._parse_daily_smc("[1,2]"))

    def test_column_is_created_inserted_and_restored(self):
        import inspect
        src = inspect.getsource(db)
        self.assertIn("ADD COLUMN IF NOT EXISTS daily_smc TEXT", src)
        ins = inspect.getsource(db.insert_open_paper_trade)
        self.assertIn("daily_smc", ins)
        import re
        cols = re.search(r"INSERT INTO paper_trades\s*\((.*?)\)\s*VALUES", ins, re.S).group(1)
        vals = re.search(r"VALUES \((.*?)\)", ins, re.S).group(1)
        n_cols = len([c for c in cols.split(",") if c.strip()])
        self.assertEqual(n_cols, 16, "前提：解析到欄位清單(含 daily_smc)")
        self.assertEqual(len([v for v in vals.split(",") if v.strip()]), n_cols, "VALUES 的數量要跟欄位一樣")
        self.assertEqual(vals.count("%s"), n_cols - 1, "只有 status 是寫死的 'open'，其他都要有佔位")
        self.assertIn('"daily_smc": _parse_daily_smc(row[21])', inspect.getsource(db.load_open_paper_trade))
        self.assertIn('"daily_smc": _parse_daily_smc(r[23])', inspect.getsource(db.load_closed_paper_trades))


class OpenPositionTag(unittest.TestCase):
    """開倉會記下日線標記，而且日線方向不會改變「開不開」——逆日線結構照樣開(純參考)。"""
    def setUp(self):
        self._saved = dict(D._state)

    def tearDown(self):
        D._state.update(self._saved)

    def _open(self, bias, direction):
        from app import paper_trading as pt
        eng = next(iter(pt.PAPER_TRADING_ENGINES.values()))
        saved_pos, saved_seed = eng._position, eng._seeded_from_db
        D._state["snapshot"] = {"bias": bias, "confirmed": True, "as_of": "2026-09-27",
                                "last_close_time": time.time() * 1000 - 3600_000}
        inserted = []
        try:
            eng._seeded_from_db, eng._position = True, None
            with mock.patch.object(pt.settings_module, "settings_loaded", return_value=True), \
                 mock.patch.object(eng, "_is_execution_engine", return_value=False), \
                 mock.patch.object(pt.db, "insert_open_paper_trade", side_effect=lambda p: inserted.append(dict(p)) or 7), \
                 mock.patch.object(pt.notifier_module.notifier, "notify_trade_event"), \
                 mock.patch.object(D, "fetch_daily_klines", side_effect=AssertionError("開倉不能打網路")):
                eng._open_position({"direction": direction, "chan": {"reason": "t"}, "profile": {"reason": "t"}}, 4390.0, 10.0)
            opened = eng._position
        finally:
            eng._position, eng._seeded_from_db = saved_pos, saved_seed
        return inserted, opened

    def test_tag_is_recorded(self):
        inserted, opened = self._open("bearish", "bearish")
        self.assertEqual(len(inserted), 1, "前提：真的寫了一筆開倉紀錄")
        self.assertEqual(inserted[0]["daily_smc"]["bias"], "bearish")
        self.assertEqual(opened["daily_smc"]["bias"], "bearish")

    def test_against_daily_still_opens(self):
        inserted, opened = self._open("bearish", "bullish")
        self.assertEqual(len(inserted), 1, "逆日線結構也要照開：這版日線只是參考")
        self.assertEqual(opened["direction"], "bullish")
        self.assertEqual(TS.daily_smc_alignment(opened["daily_smc"], opened["direction"]), "against")

    def test_live_decision_code_does_not_read_daily(self):
        import inspect
        from app import paper_trading as pt
        tick = inspect.getsource(pt.PaperTradingEngine._tick)
        self.assertGreater(len(tick), 1000, "前提：真的拿到了 _tick 的原始碼")
        self.assertNotIn("daily_smc", tick, "進出場判斷(_tick)不能看日線——r89 只當參考")


class Backtest(unittest.TestCase):
    """回測：每筆交易都有日線標記；濾網只在回測、擋得掉逆勢；標記不看未來。訊號用假的，只測日線這層。"""
    def _klines_1m(self, days=2, start=None):
        start = start if start is not None else T0 + 150 * DAY
        out, p = [], 4000.0
        for i in range(days * 1440):
            p += 0.01
            out.append([start + i * 60_000, str(p), str(p + 0.5), str(p - 0.5), str(p), "1", start + (i + 1) * 60_000 - 1])
        return out

    def _run(self, mode, direction="bullish"):
        from app import backtest as B
        closes = _path_up_then_down()           # 132 根日K，最後偏空
        daily = _raw(closes)                     # 從 T0 開始；回測視窗在最後一根日K之後
        fake = {"stage": "訊號", "direction": direction, "chan": {"reason": "t"}, "profile": {"reason": "t"},
                "atr": None, "choppiness_index": None, "emas": None}
        with mock.patch.object(B, "compute_signal_from_trades", return_value=dict(fake)), \
             mock.patch.object(B, "fetch_historical_klines", side_effect=AssertionError("給了資料就不能再抓")):
            return B.run_backtest(days=2, interval_seconds=900, klines=self._klines_1m(start=T0 + len(closes) * DAY),
                                  daily_klines=daily, daily_smc_filter_mode=mode, trend_filter_mode=0,
                                  use_chop_filter=False, block_market_closed=False, min_atr_points=0,
                                  use_atr=False, sl_points=5, trail_trigger_points=5, trail_distance_points=3,
                                  reversal_confirm_count=3)

    def test_tags_and_breakdown_without_filter(self):
        r = self._run(0)
        self.assertNotIn("error", r, r.get("error"))
        self.assertGreater(r["total_trades"] + (1 if r.get("open_position_at_end") else 0), 0, "前提：假訊號真的開了倉")
        self.assertIsNone(r["daily_smc_error"])
        self.assertGreater(r["daily_smc_candle_count"], D.DAILY_MIN_CANDLES)
        for t in r["recent_trades"]:
            self.assertEqual(t["daily_smc"]["bias"], "bearish")
        rows = {x["key"]: x for x in r["daily_smc_breakdown"]}
        self.assertEqual(rows["against"]["total_trades"], r["total_trades"], "做多遇上偏空日線＝逆勢")

    def test_filter_blocks_against_trades(self):
        r = self._run(1)
        self.assertNotIn("error", r, r.get("error"))
        self.assertEqual(r["total_trades"], 0)
        self.assertIsNone(r["open_position_at_end"])
        self.assertGreater(r["skipped_daily_smc"], 0, "前提：濾網真的擋了東西，不是沒有訊號")

    def test_filter_lets_aligned_trades_through(self):
        r = self._run(1, direction="bearish")
        self.assertGreater(r["total_trades"] + (1 if r.get("open_position_at_end") else 0), 0)
        self.assertEqual(r["skipped_daily_smc"], 0)

    def test_filter_without_daily_data_reports_error(self):
        from app import backtest as B
        with mock.patch.object(B, "fetch_historical_klines", side_effect=RuntimeError("inj-no-network")):
            D._bt_cache.clear()
            r = B.run_backtest(days=2, interval_seconds=900, klines=self._klines_1m(), daily_smc_filter_mode=1)
        self.assertIn("error", r)
        self.assertIn("日線", r["error"])

    def test_tag_uses_only_closed_daily_before_entry(self):
        """回測視窗落在日K序列中間：標記要等於「只給進場前已收盤日K」算出來的方向，不能用到之後的日K。"""
        from app import backtest as B
        closes = _path_up_then_down()
        daily = _raw(closes)
        first_bear = next(i for i, x in enumerate(D.bias_series(_candles(closes))) if x["bias"] == "bearish")
        start = T0 + (first_bear - 2) * DAY      # 視窗從翻空前兩天開始
        fake = {"stage": "訊號", "direction": "bullish", "chan": {"reason": "t"}, "profile": {"reason": "t"},
                "atr": None, "choppiness_index": None, "emas": None}
        with mock.patch.object(B, "compute_signal_from_trades", return_value=dict(fake)):
            r = B.run_backtest(days=4, interval_seconds=900, klines=self._klines_1m(days=4, start=start),
                               daily_klines=daily, trend_filter_mode=0, use_chop_filter=False,
                               block_market_closed=False, min_atr_points=0, use_atr=False,
                               sl_points=0.3, trail_trigger_points=50, trail_distance_points=50, reversal_confirm_count=3)
        trades = r["recent_trades"]
        self.assertGreater(len(trades), 3, "前提：視窗裡開了好幾筆")
        seen = {t["daily_smc"]["bias"] for t in trades}
        self.assertEqual(seen, {"bullish", "bearish"}, "視窗橫跨翻空那天：前面偏多、後面偏空")
        for t in trades:
            ms = int(__import__("datetime").datetime.fromisoformat(t["entry_time"]).timestamp() * 1000)
            # 日K的 close_time 是當天最後一毫秒(xx:59:59.999)：1分K在同一毫秒收盤時，這根日K也已經收盤
            closed = [c for c in _candles(closes) if c["close_time"] <= ms]
            self.assertEqual(t["daily_smc"]["bias"], D.bias_series(closed)[-1]["bias"], t["entry_time"])


class Endpoint(unittest.TestCase):
    def test_endpoint_reads_cache(self):
        from fastapi.testclient import TestClient
        import app.main as m
        with mock.patch.object(D, "fetch_daily_klines", side_effect=AssertionError("refresh=false 不能打網路")):
            res = TestClient(m.app).get("/analysis/daily-smc")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["reference_only"])

    def test_live_role_allows_endpoint(self):
        from app import role as R
        with mock.patch.object(R, "APP_ROLE", "live"):
            self.assertTrue(R.is_path_blocked("GET", "/backtest/run"), "前提：live 真的會擋研究端點")
            self.assertFalse(R.is_path_blocked("GET", "/analysis/daily-smc"))
        self.assertIn("/analysis/daily-smc", R.LIVE_PROXY_ALLOWED_PREFIXES)


if __name__ == "__main__":
    unittest.main()
