"""
r90 的測試：回測可以指定結束日期、背景任務(送出→輪詢)、連續多段、逆日線結構下部分量。
功能測試，跟 test_daily_smc 一樣獨立一個檔(舊版重跑只跑 test_lessons)。
執行：python -m unittest tests.test_backtest_jobs -v
"""
import logging
import unittest
from datetime import datetime, timezone
from unittest import mock

logging.disable(logging.CRITICAL)

from app import backtest as B  # noqa: E402
from app import backtest_jobs as J  # noqa: E402
from app import daily_smc as D  # noqa: E402
from app import db  # noqa: E402
from tests.test_daily_smc import _path_up_then_down, _raw, T0, DAY  # noqa: E402

FAKE = {"stage": "訊號", "chan": {"reason": "t"}, "profile": {"reason": "t"},
        "atr": None, "choppiness_index": None, "emas": None}


def _klines_1m(days, start):
    out, p = [], 4000.0
    for i in range(days * 1440):
        p += 0.01
        out.append([start + i * 60_000, str(p), str(p + 0.5), str(p - 0.5), str(p), "1", start + (i + 1) * 60_000 - 1])
    return out


class EndTime(unittest.TestCase):
    NOW = datetime(2026, 9, 28, 9, 30, 15, tzinfo=timezone.utc)

    def test_date_means_end_of_that_utc_day(self):
        ms = B.resolve_end_time_ms("2026-08-31", now=self.NOW)
        self.assertEqual(datetime.fromtimestamp((ms + 1) / 1000, tz=timezone.utc), datetime(2026, 9, 1, tzinfo=timezone.utc))

    def test_empty_is_now_aligned_to_closed_minute(self):
        ms = B.resolve_end_time_ms(None, now=self.NOW)
        self.assertEqual(datetime.fromtimestamp((ms + 1) / 1000, tz=timezone.utc), datetime(2026, 9, 28, 9, 30, tzinfo=timezone.utc))

    def test_future_is_clamped_to_now(self):
        self.assertEqual(B.resolve_end_time_ms("2027-01-01", now=self.NOW), B.resolve_end_time_ms(None, now=self.NOW))

    def test_bad_format_raises(self):
        with self.assertRaises(ValueError):
            B.resolve_end_time_ms("2026/08/31", now=self.NOW)

    def test_fetch_uses_given_end_time(self):
        seen = []

        class R:
            def raise_for_status(self): pass
            def json(self): return []
        with mock.patch.object(B.requests, "get", side_effect=lambda url, params=None, timeout=None: seen.append(params) or R()):
            B.fetch_historical_klines(days=3, end_time_ms=1_780_000_000_000 - 1)
        self.assertEqual(len(seen), 1, "前提：真的發了請求")
        self.assertEqual(seen[0]["endTime"], 1_780_000_000_000 - 1)
        self.assertEqual(seen[0]["startTime"], 1_780_000_000_000 - 3 * DAY)


class Backtest(unittest.TestCase):
    def _run(self, mode, direction, weight=0.5, progress=None, days=2):
        closes = _path_up_then_down()                 # 最後偏空
        start = T0 + len(closes) * DAY
        fake = {**FAKE, "direction": direction}
        with mock.patch.object(B, "compute_signal_from_trades", return_value=dict(fake)), \
             mock.patch.object(B, "fetch_historical_klines", side_effect=AssertionError("給了資料不能再抓")):
            return B.run_backtest(days=days, interval_seconds=900, klines=_klines_1m(days, start), daily_klines=_raw(closes),
                                  daily_smc_filter_mode=mode, daily_smc_against_weight=weight, trend_filter_mode=0,
                                  use_chop_filter=False, block_market_closed=False, min_atr_points=0, use_atr=False,
                                  sl_points=0.3, trail_trigger_points=50, trail_distance_points=50,
                                  reversal_confirm_count=3, progress_cb=progress)

    def test_half_size_scales_against_trades(self):
        full = self._run(0, "bullish")
        half = self._run(3, "bullish")
        self.assertGreater(full["total_trades"], 3, "前提：做多遇上偏空日線，開了好幾筆逆勢單")
        self.assertEqual(half["total_trades"], full["total_trades"], "下部分量不擋單：筆數一樣")
        self.assertAlmostEqual(half["total_pnl_points"], round(full["total_pnl_points"] * 0.5, 2), places=1)
        t = half["recent_trades"][0]
        self.assertEqual(t["size_weight"], 0.5)
        self.assertAlmostEqual(t["pnl_points"], t["pnl_points_full"] * 0.5)
        self.assertEqual(half["skipped_daily_smc"], 0)
        self.assertEqual(half["daily_smc_against_weight"], 0.5)

    def test_half_size_leaves_aligned_trades_alone(self):
        full = self._run(0, "bearish")
        half = self._run(3, "bearish")
        self.assertGreater(full["total_trades"], 0, "前提：有順勢單")
        self.assertEqual(half["total_pnl_points"], full["total_pnl_points"])
        self.assertTrue(all("size_weight" not in t for t in half["recent_trades"]))

    def test_weight_is_clamped_and_mode_validated(self):
        r = self._run(3, "bullish", weight=5)
        self.assertEqual(r["daily_smc_against_weight"], 1.0)
        self.assertIn("error", self._run(7, "bullish"))
        self.assertIn("error", self._run(3, "bullish", weight="abc"))

    def test_end_time_reaches_every_fetch(self):
        """指定結束時間時，1分K和日K都要抓「到那個時間為止」的資料，不能有一個還是抓到現在。"""
        closes = _path_up_then_down()
        start = T0 + 100 * DAY
        end_ms = start + 2 * DAY - 1
        seen = []

        def fake_fetch(symbol="XAUUSDT", interval="1m", days=2, max_days=None, end_time_ms=None):
            seen.append((interval, end_time_ms))
            return _raw(closes) if interval == "1d" else _klines_1m(2, start)
        D._bt_cache.clear()
        with mock.patch.object(B, "compute_signal_from_trades", return_value={**FAKE, "direction": "bullish"}), \
             mock.patch.object(B, "fetch_historical_klines", side_effect=fake_fetch):
            r = B.run_backtest(days=2, interval_seconds=900, end_time_ms=end_ms, trend_filter_mode=0,
                               use_chop_filter=False, block_market_closed=False, min_atr_points=0, use_atr=False,
                               sl_points=0.3, trail_trigger_points=50, trail_distance_points=50, reversal_confirm_count=3)
        self.assertNotIn("error", r)
        self.assertEqual({i for i, _ in seen}, {"1m", "1d"}, "前提：1分K和日K都抓了")
        self.assertTrue(all(e == end_ms for _, e in seen), f"每一次抓資料都要帶結束時間：{seen}")

    def test_bias_days_and_progress(self):
        calls = []
        closes = _path_up_then_down()
        start = T0 + 100 * DAY                    # 視窗落在日K資料中間(第 100、101 根日K在視窗內收盤)
        with mock.patch.object(B, "compute_signal_from_trades", return_value={**FAKE, "direction": "bullish"}):
            r = B.run_backtest(days=2, interval_seconds=900, klines=_klines_1m(2, start), daily_klines=_raw(closes),
                               trend_filter_mode=0, use_chop_filter=False, block_market_closed=False, min_atr_points=0,
                               use_atr=False, sl_points=0.3, trail_trigger_points=50, trail_distance_points=50,
                               reversal_confirm_count=3, progress_cb=lambda i, n: calls.append((i, n)))
        series = D.bias_series(D.klines_to_closed_candles(_raw(closes)))
        want = {"bullish": 0, "bearish": 0, "none": 0}
        for i in (100, 101):
            want[series[i]["bias"] or "none"] += 1
        self.assertEqual(sum(r["daily_bias_days"].values()), 2, "2 天的視窗，日線天數加起來是 2")
        self.assertEqual(r["daily_bias_days"], want)
        self.assertTrue(calls, "前提：有回報進度")
        self.assertEqual(calls[-1][0], calls[-1][1], "最後一次回報是 100%")


def _fake_run_factory(record, fail_index=None):
    def fake_run(days, end_time_ms, progress_cb=None, **params):
        record.append({"days": days, "end": end_time_ms, "params": params})
        n = len(record) - 1
        if fail_index is not None and n == fail_index:
            raise RuntimeError("inj-window-fail")
        if progress_cb:
            progress_cb(10, 10)
        et = datetime.fromtimestamp((end_time_ms - DAY) / 1000, tz=timezone.utc).isoformat()
        trades = [{"direction": "bearish", "entry_time": et, "exit_time": et, "pnl_points": 10.0 + n,
                   "daily_smc": {"bias": "bearish", "stale": False}}]
        return {"total_trades": 1, "win_rate": 100.0, "total_pnl_points": 10.0 + n, "profit_factor": None,
                "max_drawdown_points": 0.0, "recent_trades": trades, "daily_bias_days": {"bullish": 0, "bearish": days, "none": 0},
                "daily_smc_breakdown": [], "skipped_daily_smc": 0}
    return fake_run


class Jobs(unittest.TestCase):
    def setUp(self):
        J._jobs.clear()
        J._order.clear()

    def test_windows_are_contiguous_oldest_first(self):
        rec = []
        with mock.patch.object(J.backtest_module, "run_backtest", side_effect=_fake_run_factory(rec)), \
             mock.patch.object(db, "_enabled", False):
            out = J.start_job({"interval_seconds": 900, "daily_smc_filter_mode": 1}, days=30, windows=3, end_date="2026-08-31",
                              now=datetime(2026, 9, 28, tzinfo=timezone.utc))
            self.assertIn("job_id", out)
            self.assertTrue(J.wait_idle(10), "前提：背景任務跑完")
            job = J.get_job(out["job_id"])
        self.assertEqual(len(rec), 3, "三段各跑一次")
        ends = [r["end"] for r in rec]
        self.assertEqual(ends, sorted(ends), "最舊的一段先跑")
        self.assertEqual(ends[1] - ends[0], 30 * DAY)
        self.assertEqual(ends[2] - ends[1], 30 * DAY)
        self.assertEqual(ends[2], B.resolve_end_time_ms("2026-08-31", now=datetime(2026, 9, 28, tzinfo=timezone.utc)))
        self.assertEqual(rec[0]["params"], {"interval_seconds": 900, "daily_smc_filter_mode": 1}, "參數原封不動傳給回測")
        self.assertEqual(job["status"], "done")
        self.assertEqual(len(job["window_results"]), 3)
        self.assertEqual(job["aggregate"]["total_trades"], 3)
        self.assertEqual(job["aggregate"]["total_pnl_points"], 33.0)
        aligned = next(r for r in job["aggregate"]["daily_smc_breakdown"] if r["key"] == "aligned")
        self.assertEqual(aligned["total_trades"], 3, "合計的日線分組用全部段落的交易")
        self.assertEqual(J.list_jobs()[0]["job_id"], out["job_id"])

    def test_one_window_failing_does_not_stop_others(self):
        rec = []
        with mock.patch.object(J.backtest_module, "run_backtest", side_effect=_fake_run_factory(rec, fail_index=1)), \
             mock.patch.object(db, "_enabled", False):
            out = J.start_job({}, days=7, windows=3)
            self.assertTrue(J.wait_idle(10))
            job = J.get_job(out["job_id"])
        self.assertEqual(len(rec), 3, "壞掉的那段之後照樣跑")
        self.assertEqual(job["status"], "done")
        self.assertIn("inj-window-fail", job["window_results"][1]["error"])
        self.assertEqual(job["aggregate"]["total_trades"], 2, "壞掉的那段不算進合計")

    def test_validation(self):
        self.assertIn("error", J.start_job({}, windows=0))
        self.assertIn("error", J.start_job({}, windows=J.MAX_WINDOWS + 1))
        self.assertIn("error", J.start_job({}, end_date="31-08-2026"))
        self.assertIn("error", J.start_job({"end_time_ms": 1}))
        self.assertEqual(J._jobs, {}, "參數錯就不建任務")

    def test_result_survives_restart_via_db_and_running_becomes_interrupted(self):
        from tests.test_lessons import _sqlite_pool
        pool, conn = _sqlite_pool()
        conn.execute("CREATE TABLE backtest_jobs (id TEXT PRIMARY KEY, created_at TEXT DEFAULT CURRENT_TIMESTAMP, "
                     "updated_at TEXT, status TEXT, summary TEXT, payload TEXT)")
        rec = []
        with mock.patch.object(db, "_enabled", True), mock.patch.object(db, "_pool", pool, create=True), \
             mock.patch.object(J.backtest_module, "run_backtest", side_effect=_fake_run_factory(rec)), \
             mock.patch.object(J, "_ensure_worker"):
            # sqlite 連線不能跨執行緒：這一項在同一個執行緒裡直接跑任務(背景執行緒那條路其他測試有測)
            out = J.start_job({}, days=7, windows=2)
            self.assertEqual(J._queue.get_nowait(), out["job_id"], "前提：任務有進佇列")
            J._queue.task_done()
            J._run(out["job_id"])
            self.assertEqual(len(rec), 2, "前提：兩段都跑了")
            J._jobs.clear()
            J._order.clear()                      # 模擬重啟：記憶體沒了
            job = J.get_job(out["job_id"])
            self.assertIsNotNone(job, "前提：資料庫裡有")
            self.assertEqual(job["status"], "done")
            self.assertEqual(job["aggregate"]["total_trades"], 2)
            self.assertEqual([j["job_id"] for j in J.list_jobs()], [out["job_id"]])
            # 另一筆在重啟前還在跑
            db.save_backtest_job("deadbeef", "running", '{"job_id": "deadbeef", "status": "running", "created_at": "2026-09-28"}',
                                 '{"job_id": "deadbeef", "status": "running", "window_results": []}')
            stuck = J.get_job("deadbeef")
            listed = {j["job_id"]: j for j in J.list_jobs()}
        self.assertEqual(stuck["status"], "interrupted")
        self.assertIn("重啟", stuck["error"])
        self.assertEqual(listed["deadbeef"]["status"], "interrupted")

    def test_list_shows_key_params_using_actual_values(self):
        """r91：清單要分得出哪個是哪組設定；參數留空(沿用正式設定)時用回測實際用到的值。"""
        rec = []
        base = _fake_run_factory(rec)

        def run(**kw):
            r = base(**kw)
            r.update({"trend_filter_mode": 2, "trend_interval_seconds": 14400, "trend_slow_multiplier": 3.0,
                      "daily_smc_filter_mode": kw.get("daily_smc_filter_mode", 0), "strategy_type": "chan_profile"})
            return r
        with mock.patch.object(J.backtest_module, "run_backtest", side_effect=run), mock.patch.object(db, "_enabled", False):
            a = J.start_job({"interval_seconds": 900, "daily_smc_filter_mode": 0}, days=7, windows=2)
            b = J.start_job({"interval_seconds": 900, "daily_smc_filter_mode": 3, "daily_smc_against_weight": 0.5}, days=7, windows=2)
            self.assertTrue(J.wait_idle(10))
            listed = {j["job_id"]: j["label_params"] for j in J.list_jobs()}
        self.assertEqual(listed[a["job_id"]]["daily_smc_filter_mode"], 0)
        self.assertEqual(listed[b["job_id"]]["daily_smc_filter_mode"], 3)
        self.assertEqual(listed[b["job_id"]]["daily_smc_against_weight"], 0.5)
        self.assertEqual(listed[a["job_id"]]["trend_filter_mode"], 2, "送出時沒指定，要顯示回測實際用到的")
        self.assertEqual(listed[a["job_id"]]["trend_interval_seconds"], 14400)
        self.assertEqual(listed[a["job_id"]]["interval_seconds"], 900)

    def test_old_summary_without_params_is_backfilled(self):
        """r90 存進資料庫的任務 summary 沒有參數：清單要從完整內容補上，舊的三個任務才分得出來。"""
        import json
        full = {"job_id": "old1", "status": "done", "created_at": "2026-09-28T11:26:59", "params": {"interval_seconds": 900,
                "daily_smc_filter_mode": 1}, "window_results": [{"result": {"trend_filter_mode": 2}}]}
        summary = {"job_id": "old1", "status": "done", "created_at": "2026-09-28T11:26:59"}
        with mock.patch.object(db, "list_backtest_jobs", return_value=(True, [json.dumps(summary)])), \
             mock.patch.object(db, "load_backtest_job", return_value=(True, json.dumps(full))):
            got = J.list_jobs()
        self.assertEqual(got[0]["label_params"]["daily_smc_filter_mode"], 1)
        self.assertEqual(got[0]["label_params"]["trend_filter_mode"], 2)

    def test_single_window_job_has_headline_in_list(self):
        """r92：只跑一段的任務沒有合計，清單上用那一段的結果當摘要。"""
        rec = []
        with mock.patch.object(J.backtest_module, "run_backtest", side_effect=_fake_run_factory(rec)), \
             mock.patch.object(db, "_enabled", False):
            one = J.start_job({}, days=7, windows=1)
            multi = J.start_job({}, days=7, windows=2)
            self.assertTrue(J.wait_idle(10))
            listed = {j["job_id"]: j for j in J.list_jobs()}
        self.assertEqual(listed[one["job_id"]]["headline"]["total_trades"], 1)
        self.assertEqual(listed[one["job_id"]]["headline"]["total_pnl_points"], 10.0)
        self.assertIsNone(listed[multi["job_id"]]["headline"], "多段的用合計，不另外給")
        self.assertIsNotNone(listed[multi["job_id"]]["aggregate"])

    def test_unknown_job(self):
        with mock.patch.object(db, "_enabled", False):
            self.assertIsNone(J.get_job("does-not-exist"))


class Endpoints(unittest.TestCase):
    def setUp(self):
        J._jobs.clear()
        J._order.clear()

    def test_start_returns_immediately_and_poll_gets_result(self):
        from fastapi.testclient import TestClient
        import app.main as m
        rec = []
        with mock.patch.object(J.backtest_module, "run_backtest", side_effect=_fake_run_factory(rec)), \
             mock.patch.object(db, "_enabled", False):
            c = TestClient(m.app)
            r = c.post("/backtest/jobs?days=30&windows=2&interval_seconds=900&daily_smc_filter_mode=3&daily_smc_against_weight=0.3")
            body = r.json()
            self.assertIn("job_id", body, body)
            self.assertTrue(J.wait_idle(10))
            job = c.get(f"/backtest/jobs/{body['job_id']}").json()
            lst = c.get("/backtest/jobs").json()
        self.assertEqual(job["status"], "done")
        self.assertEqual(rec[0]["params"]["daily_smc_filter_mode"], 3)
        self.assertEqual(rec[0]["params"]["daily_smc_against_weight"], 0.3)
        self.assertEqual(rec[0]["params"]["interval_seconds"], 900)
        self.assertNotIn("days", rec[0]["params"])
        self.assertEqual(lst["jobs"][0]["job_id"], body["job_id"])

    def test_live_role_blocks_jobs(self):
        from app import role as R
        with mock.patch.object(R, "APP_ROLE", "live"):
            self.assertTrue(R.is_path_blocked("POST", "/backtest/jobs"))
            self.assertTrue(R.is_path_blocked("GET", "/backtest/jobs/abc"))


class DailyCacheKey(unittest.TestCase):
    def test_different_end_times_do_not_share_cache(self):
        D._bt_cache.clear()
        calls = []
        f = lambda sym, d: calls.append(d) or _raw(_path_up_then_down())
        D.backtest_series("XAUUSDT", 30, f, end_key=1)
        D.backtest_series("XAUUSDT", 30, f, end_key=1)
        D.backtest_series("XAUUSDT", 30, f, end_key=2)
        self.assertEqual(len(calls), 2, "同一個結束時間共用、不同的要重抓")


if __name__ == "__main__":
    unittest.main()


class MobileLayout(unittest.TestCase):
    """r92：iOS 的下拉選單寬度＝最長選項，選項一長整頁就被撐寬(手機會整頁縮小)。這裡只能靜態檢查；
    實際排版是用無頭瀏覽器在 390px 寬、把下拉選單設成「寬度＝最長選項」模擬 iOS 量過的(r91 撐到 698px、r92 維持 390px)。"""
    def setUp(self):
        import os
        self.html = open(os.path.join(os.path.dirname(__file__), "..", "app", "static", "dashboard.html"), encoding="utf-8").read()

    def test_recent_jobs_is_not_a_select(self):
        self.assertIn('id="backtestJobList" class="bt-job-list"', self.html)
        self.assertNotIn('<select id="backtestJobList"', self.html)

    def test_form_controls_cannot_exceed_container(self):
        self.assertIn("select, input{ max-width: 100%; }", self.html)

    def test_no_undefined_border_variable(self):
        self.assertNotIn("var(--border)", self.html, "沒有這個 CSS 變數(r90 用了、邊框沒顯示)")


class Reentry(unittest.TestCase):
    """r93：依「上一筆賺或賠、隔多久才再進場」分組。"""
    @staticmethod
    def _t(entry, exit_, pnl, direction="bearish"):
        return {"entry_time": f"2026-06-11T{entry}:00+00:00", "exit_time": f"2026-06-11T{exit_}:00+00:00",
                "pnl_points": pnl, "direction": direction}

    def _groups(self, trades):
        from app import trading_stats as TS
        return {g["key"]: g for g in TS.compute_reentry_breakdown(trades)["groups"]}

    def test_quick_reentry_after_win_is_grouped(self):
        trades = [
            self._t("01:00", "06:30", 74.45),          # 第一筆 → 其他
            self._t("07:00", "09:16", -30.35),         # 賺後 0.5h
            self._t("11:30", "12:15", 20.35, "bullish"),  # 賠後 2.2h、反方向
            self._t("20:00", "21:00", -5.0, "bullish"),  # 賺後 7.75h → 其他
        ]
        g = self._groups(list(reversed(trades)))       # 順序打亂也要先依開倉時間排
        self.assertEqual(g["after_win_1h"]["total_trades"], 1)
        self.assertEqual(g["after_win_1h"]["total_pnl_points"], -30.35)
        self.assertEqual(g["after_win_1h"]["same_dir_ratio"], 100.0)
        self.assertEqual(g["after_win_3h"]["total_trades"], 1, "≤3h 包含 ≤1h 的")
        self.assertEqual(g["after_loss_1h"]["total_trades"], 0)
        self.assertEqual(g["after_loss_3h"]["total_trades"], 1)
        self.assertEqual(g["after_loss_3h"]["avg_pnl_points"], 20.35)
        self.assertEqual(g["after_loss_3h"]["same_dir_ratio"], 0.0, "跟上一筆反方向")
        self.assertEqual(g["rest"]["total_trades"], 2, "第一筆＋間隔超過 6 小時的")
        self.assertIsNone(g["rest"]["same_dir_ratio"])

    def test_every_trade_counted_once_in_rest_or_longest_window(self):
        import random
        rnd = random.Random(7)
        trades, t = [], datetime(2026, 6, 1, tzinfo=timezone.utc)
        from datetime import timedelta
        for _ in range(60):
            t += timedelta(minutes=rnd.randint(5, 900))
            x = t + timedelta(minutes=rnd.randint(15, 300))
            trades.append({"entry_time": t.isoformat(), "exit_time": x.isoformat(), "pnl_points": rnd.uniform(-20, 20),
                           "direction": rnd.choice(["bullish", "bearish"])})
            t = x
        g = self._groups(trades)
        self.assertEqual(g["after_win_6h"]["total_trades"] + g["after_loss_6h"]["total_trades"] + g["rest"]["total_trades"], 60)
        self.assertLessEqual(g["after_win_1h"]["total_trades"], g["after_win_3h"]["total_trades"])
        self.assertLessEqual(g["after_win_3h"]["total_trades"], g["after_win_6h"]["total_trades"])

    def test_unreadable_times_are_skipped_not_guessed(self):
        from app import trading_stats as TS
        r = TS.compute_reentry_breakdown([{"entry_time": None, "exit_time": "x", "pnl_points": 1}, self._t("01:00", "02:00", 3)])
        self.assertEqual(r["skipped"], 1)
        self.assertEqual(sum(g["total_trades"] for g in r["groups"] if g["key"] in ("after_win_6h", "after_loss_6h", "rest")), 1)

    def test_backtest_job_and_live_summary_include_it(self):
        from tests.test_daily_smc import _path_up_then_down as _p
        closes = _p()
        start = T0 + len(closes) * DAY
        with mock.patch.object(B, "compute_signal_from_trades", return_value={**FAKE, "direction": "bearish"}):
            r = B.run_backtest(days=2, interval_seconds=900, klines=_klines_1m(2, start), daily_klines=_raw(closes),
                               trend_filter_mode=0, use_chop_filter=False, block_market_closed=False, min_atr_points=0,
                               use_atr=False, sl_points=0.3, trail_trigger_points=50, trail_distance_points=50, reversal_confirm_count=3)
        self.assertGreater(r["total_trades"], 1, "前提：有好幾筆")
        self.assertEqual(sum(g["total_trades"] for g in r["reentry_breakdown"]["groups"]
                             if g["key"] in ("after_win_6h", "after_loss_6h", "rest")), r["total_trades"])
        agg = J._aggregate([r, r])
        self.assertIn("reentry_breakdown", agg)
        from app import paper_trading as pt
        eng = next(iter(pt.PAPER_TRADING_ENGINES.values()))
        mem = [self._t("07:00", "09:16", -30.35), self._t("01:00", "06:30", 74.45)]
        with mock.patch.object(eng, "_closed_trades_memory", mem), mock.patch.object(pt.db, "_enabled", False):
            summ = eng.get_summary()
        g = {x["key"]: x["total_trades"] for x in summ["reentry_breakdown"]["groups"]}
        self.assertEqual(g["after_win_1h"], 1, "即時模擬單的統計也有這張表")
        self.assertEqual(g["rest"], 1)
