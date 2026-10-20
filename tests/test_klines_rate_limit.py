"""
r97／r98 的測試：幣安 IP 權重協調(app/binance_weight.py，BINANCE_LESSONS.md 第 6 條 r97／r98)。
r98 對照 r97 的五點：實盤K線不排研究端的隊、記錄所有幣安回應的權重標頭與 429/418、
限流不寫進快取、批次遇到限流整輪讓開、自檢加「IP 權重（K 線）」。
執行：python -m unittest tests.test_klines_rate_limit -v
"""
import inspect
import logging
import os
import threading
import time
import unittest
from unittest import mock

logging.disable(logging.CRITICAL)

from app import backtest as B  # noqa: E402
from app import backtest_jobs as J  # noqa: E402
from app import binance_client as BC  # noqa: E402
from app import binance_weight as W  # noqa: E402
from app import daily_smc as D  # noqa: E402
from app import execution as E  # noqa: E402
from app import preflight as P  # noqa: E402
from app import smc_structure as S  # noqa: E402
from app import sweep as SW  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = [[1, "1", "1", "1", "1", "1", 60_000]]


class R:
    def __init__(self, status=200, headers=None, data=None):
        self.status_code, self.headers, self._data = status, headers or {}, data if data is not None else PAGE

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._data


class Base(unittest.TestCase):
    def setUp(self):
        W.reset_for_tests()
        self.calls, self.sleeps = [], []
        self._responses = iter([])
        p1 = mock.patch.object(W.requests, "get", side_effect=self._get)
        p2 = mock.patch.object(W, "_sleep", side_effect=self.sleeps.append)
        p1.start()
        p2.start()
        self.addCleanup(p1.stop)
        self.addCleanup(p2.stop)
        self.addCleanup(W.reset_for_tests)

    def _get(self, url, params=None, timeout=None, **kw):
        self.calls.append(params)
        return next(self._responses)

    def respond(self, *rs):
        self._responses = iter(rs)

    def run_page(self, mode="batch"):
        try:
            return W.get_klines({"x": 1}, mode=mode), None
        except Exception as e:   # noqa: BLE001
            return None, e

    def wait_sweep(self, jid):
        for _ in range(200):
            j = SW.get_job(jid)
            if j["status"] != "running":
                return j
            time.sleep(0.05)
        return SW.get_job(jid)


class BatchPage(Base):
    def test_429_waits_retry_after_then_succeeds(self):
        self.respond(R(429, {"Retry-After": "7"}), R(200))
        data, err = self.run_page()
        self.assertIsNone(err)
        self.assertEqual(data, PAGE)
        self.assertEqual((len(self.calls), self.sleeps), (2, [7.0]))

    def test_429_gives_up_after_retries_with_clear_message(self):
        self.respond(*[R(429)] * 10)
        _, err = self.run_page()
        self.assertIsInstance(err, W.BinanceRateLimited)
        self.assertIn("429", str(err))
        self.assertIn("暫時性", str(err))
        self.assertEqual(len(self.calls), W.MAX_RETRIES + 1)
        self.assertEqual(self.sleeps, [float(W.DEFAULT_429_WAIT)] * W.MAX_RETRIES)

    def test_418_stops_immediately_and_blocks_everything_after(self):
        self.respond(R(418, {"Retry-After": "300"}), R(200), R(200))
        _, err = self.run_page()
        self.assertIsInstance(err, W.BinanceRateLimited)
        self.assertIn("418", str(err))
        self.assertEqual((len(self.calls), self.sleeps), (1, []), "封 IP 時再打只會延長封鎖")
        for mode in ("batch", "live"):
            _, err2 = self.run_page(mode)
            self.assertIsInstance(err2, W.BinanceRateLimited, mode)
        self.assertEqual(len(self.calls), 1, "封鎖期間 batch、live 都不送")

    def test_retry_after_is_clamped(self):
        self.respond(R(429, {"Retry-After": "9999"}), R(200))
        _, err = self.run_page()
        self.assertIsNone(err)
        self.assertEqual(self.sleeps, [float(W.MAX_WAIT)])

    def test_high_used_weight_yields_until_next_minute(self):
        self.respond(R(200, {"X-MBX-USED-WEIGHT-1M": str(W.PAUSE_AT)}), R(200))
        self.run_page()
        self.assertEqual(self.sleeps, [], "看到標頭之前不用等")
        self.run_page()
        self.assertEqual(len(self.sleeps), 1)
        self.assertTrue(1 <= self.sleeps[0] <= 61)

    def test_low_used_weight_does_not_pause(self):
        self.respond(R(200, {"X-MBX-USED-WEIGHT-1M": "100"}), R(200))
        self.run_page()
        self.run_page()
        self.assertEqual(self.sleeps, [])

    def test_other_http_errors_raise_without_retry(self):
        self.respond(R(500), R(200))
        _, err = self.run_page()
        self.assertIsNotNone(err)
        self.assertNotIsInstance(err, W.BinanceRateLimited)
        self.assertEqual((len(self.calls), self.sleeps), (1, []))

    def test_fetch_never_returns_partial_pages_and_passes_mode(self):
        end = 1_780_000_000_000 - 1
        st = end - 2 * 86_400_000 + 1
        full = [[st + i, "1", "1", "1", "1", "1", st + i + 59_999] for i in range(0, 1500 * 60_000, 60_000)]
        self.respond(R(200, data=full), *[R(429)] * 5)
        with self.assertRaises(W.BinanceRateLimited):
            B.fetch_historical_klines(days=2, end_time_ms=1_780_000_000_000 - 1)
        self.assertEqual(len(self.calls), 1 + W.MAX_RETRIES + 1, "前提：第一頁拿到了、第二頁被擋")
        with mock.patch.object(W, "get_klines", return_value=[]) as g:
            B.fetch_historical_klines(days=1, end_time_ms=1_780_000_000_000 - 1, mode="live")
        self.assertEqual(g.call_args.kwargs["mode"], "live")


class LivePage(Base):
    def test_live_does_not_wait_for_high_weight(self):
        W.record(R(200, {"X-MBX-USED-WEIGHT-1M": "2000"}), "x")
        self.respond(R(200))
        self.assertEqual(self.run_page("live"), (PAGE, None))
        self.assertEqual(self.sleeps, [])

    def test_live_skips_during_cooldown_without_sending(self):
        W.record(R(429, {"Retry-After": "30"}), "signed /fapi/v1/order")
        _, err = self.run_page("live")
        self.assertIsInstance(err, W.BinanceRateLimited)
        self.assertEqual((self.calls, self.sleeps), ([], []))
        self.assertEqual(W.snapshot()["skipped_live"], 1)

    def test_live_429_is_not_retried(self):
        self.respond(R(429), R(200))
        _, err = self.run_page("live")
        self.assertIsInstance(err, W.BinanceRateLimited)
        self.assertEqual((len(self.calls), self.sleeps), (1, []))

    def test_live_does_not_queue_behind_batch(self):
        W._batch_lock.acquire()   # 研究端正在排隊/等待
        try:
            self.respond(R(200))
            out = {}
            t = threading.Thread(target=lambda: out.update(r=self.run_page("live")))
            t.start()
            t.join(2)
            self.assertFalse(t.is_alive(), "實盤K線被研究端的鎖卡住")
            self.assertEqual(out["r"], (PAGE, None))
        finally:
            W._batch_lock.release()

    def test_live_callers_use_live_mode(self):
        self.assertIn('mode="live"', inspect.getsource(BC.BinanceGoldStreamer._fetch_minute_bars_from_rest))
        self.respond(R(200, data=[[1, "1", "2", "0.5", "1.5", "3", 2]]))
        S.fetch_hourly_klines()
        self.respond(R(200))
        D.fetch_daily_klines()
        W.record(R(429), "x")
        with self.assertRaises(W.BinanceRateLimited):
            S.fetch_hourly_klines()
        with self.assertRaises(W.BinanceRateLimited):
            D.fetch_daily_klines()
        self.assertEqual((len(self.calls), self.sleeps), (2, []), "冷卻中即時路徑不送也不等")


class RecordAll(Base):
    def _signed(self, resp, testnet):
        with mock.patch.object(E.requests, "request", return_value=resp), \
             mock.patch.object(E, "_get_credentials", return_value=("k", "s")), \
             mock.patch.object(E, "use_testnet", return_value=testnet):
            return E._signed_request("GET", "/fapi/v2/positionRisk", {}, account="main")

    def test_signed_request_429_makes_klines_yield(self):
        self._signed(R(429, {"Retry-After": "20"}), testnet=False)
        self.assertEqual(W.blocked()[0], "cooldown")
        self.respond(R(200))
        self.run_page("batch")
        self.assertEqual(self.sleeps, [20.0], "下單/查部位被 429，研究端K線也要等")

    def test_signed_request_418_blocks_klines(self):
        self._signed(R(418), testnet=False)
        self.assertEqual(W.blocked()[0], "banned")

    def test_testnet_responses_are_not_counted(self):
        self._signed(R(418, {"X-MBX-USED-WEIGHT-1M": "2300"}), testnet=True)
        self.assertEqual((W.blocked()[0], W.used_this_minute()), (None, 0))

    def test_public_endpoints_are_recorded(self):
        with mock.patch.object(E.requests, "get",
                               return_value=R(200, {"X-MBX-USED-WEIGHT-1M": "77"}, data={"bidPrice": "1", "askPrice": "2"})), \
             mock.patch.object(E, "use_testnet", return_value=False):
            E.get_book_ticker("XAUUSDT")
        self.assertEqual(W.used_this_minute(), 77)

    def test_used_weight_resets_next_minute(self):
        t = [600.0]
        with mock.patch.object(W, "_now", side_effect=lambda: t[0]):
            W.record(R(200, {"X-MBX-USED-WEIGHT-1M": "1500"}), "x")
            self.assertEqual(W.used_this_minute(), 1500)
            t[0] = 660.0
            self.assertEqual(W.used_this_minute(), 0)


class Caches(Base):
    def test_hourly_cache_kept_and_no_request_during_cooldown(self):
        with mock.patch.dict(S._hist_cache, {"candles": [{"bucket_start": 1}], "fetched_at": 0.0}):
            W.record(R(429, {"Retry-After": "30"}), "x")
            out = S.get_hourly_history(force=True)
            self.assertEqual(out, [{"bucket_start": 1}], "沿用上次成功的快取")
            self.assertEqual(S._hist_cache["candles"], [{"bucket_start": 1}])
        self.assertEqual(self.calls, [], "冷卻中不送")

    def test_daily_refresh_keeps_snapshot_on_rate_limit(self):
        with mock.patch.dict(D._state, {"snapshot": {"bias": "bullish"}, "last_error": None}):
            self.respond(R(429))
            snap = D.refresh()
            self.assertEqual(snap, {"bias": "bullish"})
            self.assertIn("429", D._state["last_error"])

    def test_backtest_daily_series_not_cached_on_rate_limit(self):
        n = []

        def fetcher(sym, d):
            n.append(1)
            raise W.BinanceRateLimited("429")
        D._bt_cache.clear()
        for _ in range(2):
            with self.assertRaises(W.BinanceRateLimited):
                D.backtest_series("XAUUSDT", 5, fetcher, end_key=123)
        self.assertEqual(len(n), 2, "沒有把失敗寫進快取")
        self.assertEqual(D._bt_cache, {})

    def _kl(self, hours=30):
        start = 494_444 * 3_600_000
        return [[start + m * 60_000, "4000", "4000.5", "3999.5", "4000", "1", start + (m + 1) * 60_000 - 1]
                for m in range(hours * 60)]

    def test_backtest_aborts_when_daily_klines_rate_limited(self):
        D._bt_cache.clear()
        with mock.patch.object(B, "fetch_historical_klines", side_effect=W.BinanceRateLimited("429")):
            with self.assertRaises(W.BinanceRateLimited):
                B.run_backtest(days=1, klines=self._kl(), strategy_type="chan_profile", interval_seconds=900,
                               end_time_ms=987654321)

    def test_backtest_aborts_when_hourly_warmup_rate_limited(self):
        with mock.patch.object(B, "fetch_historical_klines", side_effect=W.BinanceRateLimited("429")):
            with self.assertRaises(W.BinanceRateLimited):
                B.run_backtest(days=1, klines=self._kl(), daily_klines=[], strategy_type="mean_reversion")

    def test_warmup_other_error_is_visible(self):
        with mock.patch.object(B, "fetch_historical_klines", side_effect=RuntimeError("boom")):
            res = B.run_backtest(days=1, klines=self._kl(), daily_klines=[], strategy_type="mean_reversion",
                                 block_market_closed=False, trend_filter_mode=0)
        self.assertIn("boom", res["warmup_error"])
        with mock.patch.object(B, "fetch_historical_klines", return_value=[]):
            res = B.run_backtest(days=1, klines=self._kl(), daily_klines=[], strategy_type="mean_reversion",
                                 block_market_closed=False, trend_filter_mode=0)
        self.assertIsNone(res["warmup_error"])


class BatchLoops(Base):
    def _job(self, side_effect, windows):
        with mock.patch.object(B, "run_backtest", side_effect=side_effect) as rb, \
             mock.patch.object(J.db, "save_backtest_job", return_value=None):
            out = J.start_job({"interval_seconds": 3600}, days=1, windows=windows)
            self.assertTrue(J.wait_idle(10))
            return J.get_job(out["job_id"]), rb.call_count

    def test_job_stops_after_first_rate_limited_window(self):
        job, n = self._job(W.BinanceRateLimited("幣安限流(429)：暫時性"), 3)
        self.assertEqual(n, 1, "第一段被限流後，後面的段不再各自抓、各自等")
        self.assertEqual(len(job["window_results"]), 3)
        self.assertTrue(all(w["rate_limited"] and not w["no_data"] for w in job["window_results"]))
        self.assertEqual((job["status"], job["aggregate"]), ("error", None), "限流的任務不算合計")
        self.assertIn("限流", job["error"])

    def test_job_rate_limited_in_middle_keeps_done_windows_but_no_aggregate(self):
        good = {"total_trades": 0, "recent_trades": []}
        job, n = self._job([good, W.BinanceRateLimited("429"), good], 3)
        self.assertEqual(n, 2)
        self.assertFalse(job["window_results"][0].get("error"))
        self.assertTrue(job["window_results"][2]["rate_limited"])
        self.assertIsNone(job["aggregate"])

    def test_job_other_error_marks_missing_windows(self):
        good = {"total_trades": 0, "recent_trades": []}
        job, n = self._job([good, RuntimeError("x"), good], 3)
        self.assertEqual((n, job["status"]), (3, "done"))
        self.assertEqual(job["aggregate"]["missing_windows"], 1)

    def test_sweep_aborts_when_shared_fetch_rate_limited(self):
        with mock.patch.object(B, "fetch_historical_klines", side_effect=W.BinanceRateLimited("429")), \
             mock.patch.object(B, "run_backtest") as rb:
            j = self.wait_sweep(SW.start_sweep(days=1, interval_seconds=900))
        self.assertEqual(rb.call_count, 0)
        self.assertEqual(j["status"], "error")
        self.assertIn("限流", j["error"])

    def test_sweep_stops_remaining_combos(self):
        with mock.patch.object(B, "fetch_historical_klines", return_value=PAGE), \
             mock.patch.object(B, "run_backtest", side_effect=W.BinanceRateLimited("429")) as rb:
            j = self.wait_sweep(SW.start_sweep(days=1, interval_seconds=900))
        self.assertEqual(rb.call_count, 1)
        self.assertEqual(j["status"], "error")
        self.assertIn("剩下的沒跑", j["error"])


class Preflight(Base):
    def test_banned_is_fail_without_probe(self):
        W.record(R(418, {"Retry-After": "100"}), "x")
        r = P.ip_weight_check()
        self.assertEqual((r["item"], r["status"]), ("IP 權重（K 線）", "fail"))
        self.assertIn("封鎖中(418)", r["msg"])
        self.assertIn("不送探測", r["msg"])
        self.assertEqual(self.calls, [])

    def test_cooldown_is_warn_without_probe(self):
        W.record(R(429, {"Retry-After": "100"}), "x")
        self.assertEqual(P.ip_weight_check()["status"], "warn")
        self.assertEqual(self.calls, [])

    def test_normal_probe_reports_used_weight(self):
        self.respond(R(200, {"X-MBX-USED-WEIGHT-1M": "42"}))
        r = P.ip_weight_check()
        self.assertEqual(r["status"], "ok")
        self.assertIn("42/2400", r["msg"])
        self.assertEqual(self.calls[0]["limit"], 1, "只抓一根(權重 1)")

    def test_probe_hitting_429_is_warn(self):
        self.respond(R(429))
        self.assertEqual(P.ip_weight_check()["status"], "warn")

    def test_check_includes_item_and_version_matches_lessons(self):
        self.respond(R(200))
        with mock.patch.object(P, "entry_price_check", return_value={"item": "x", "status": "ok", "msg": ""}), \
             mock.patch.object(P.execution_module, "get_symbol_filters", return_value={}), \
             mock.patch.object(P.execution_module, "_get_credentials", return_value=(None, None)):
            items = [x["item"] for x in P.check()]
        self.assertIn("IP 權重（K 線）", items)
        self.assertRegex(P.VERSION, r"r(9[8-9]|\d{3,})$", "r98 起(之後的版本也行)；跟清單一致由下一行檢查")
        with open(os.path.join(ROOT, "BINANCE_LESSONS.md"), encoding="utf-8") as f:
            self.assertIn(f"版本 {P.VERSION}", f.read(300))


if __name__ == "__main__":
    unittest.main()


class SpotKlines(Base):
    """r106：現貨K線(PAXGUSDT@spot，長歷史回測用)。"""

    def test_split_symbol(self):
        self.assertEqual(B.split_symbol("PAXGUSDT@spot"), ("PAXGUSDT", "spot"))
        self.assertEqual(B.split_symbol("PAXGUSDT"), ("PAXGUSDT", "futures"))
        self.assertEqual(B.split_symbol("XAUUSDT"), ("XAUUSDT", "futures"))

    def test_fetch_spot_uses_spot_symbol_limit_and_market(self):
        calls = []
        end = 1_780_000_000_000 - 1
        st = end - 86_400_000 + 1
        page = [[st + i * 60_000, "1", "1", "1", "1", "1", st + i * 60_000 + 59_999] for i in range(1000)]

        def fake(params, mode="batch", market="futures", source=None):
            calls.append((dict(params), mode, market))
            return page if len(calls) == 1 else page[:440]
        with mock.patch.object(W, "get_klines", side_effect=fake):
            out = B.fetch_historical_klines(symbol="PAXGUSDT@spot", days=1, end_time_ms=end)
        self.assertEqual([c[2] for c in calls], ["spot", "spot"])
        self.assertEqual(calls[0][0]["symbol"], "PAXGUSDT", "送出去的商品代號不帶 @spot")
        self.assertEqual(calls[0][0]["limit"], 1000, "現貨一頁最多 1000 根")
        self.assertEqual(len(calls), 2, "第二頁不滿 1000 根就停")
        self.assertEqual(len(out), 1440)

    def test_futures_still_uses_futures(self):
        with mock.patch.object(W, "get_klines", return_value=[]) as g:
            B.fetch_historical_klines(symbol="PAXGUSDT", days=1, end_time_ms=1_780_000_000_000 - 1)
        self.assertEqual(g.call_args.kwargs["market"], "futures")
        self.assertEqual(g.call_args.args[0]["limit"], B.MAX_KLINES_PER_REQUEST)

    def test_spot_url_and_separate_weight(self):
        urls = []

        def get(url, params=None, timeout=None, **kw):
            urls.append(url)
            return R(200, {"X-MBX-USED-WEIGHT-1M": "2500"})
        with mock.patch.object(W.requests, "get", side_effect=get):
            W.get_klines({"x": 1}, mode="batch", market="spot")
            W.get_klines({"x": 1}, mode="batch", market="spot")
        self.assertEqual(urls, [W.SPOT_KLINES_URL] * 2)
        self.assertEqual(self.sleeps, [], "現貨上限 6000，用到 2500 不用等")
        self.assertEqual(W.used_this_minute("spot"), 2500)
        self.assertEqual(W.used_this_minute(), 0, "現貨的權重不算進期貨(期貨即時路徑不受影響)")

    def test_spot_pauses_at_its_own_threshold(self):
        self.respond(R(200, {"X-MBX-USED-WEIGHT-1M": str(W.SPOT_PAUSE_AT)}), R(200))
        W.get_klines({"x": 1}, market="spot")
        W.get_klines({"x": 1}, market="spot")
        self.assertEqual(len(self.sleeps), 1)

    def test_spot_429_blocks_everything(self):
        self.respond(R(429, {"Retry-After": "30"}), R(200))
        W.get_klines({"x": 1}, market="spot")
        self.assertEqual(self.sleeps, [30.0])
        self.respond(R(429, {"Retry-After": "30"}))
        with self.assertRaises(W.BinanceRateLimited):
            W.get_klines({"x": 1}, mode="live")
        self.assertEqual(len(self.calls), 2, "現貨被 429 之後的冷卻，期貨即時路徑一樣不送")

    def test_spot_is_batch_only(self):
        with self.assertRaises(ValueError):
            W.get_klines({"x": 1}, mode="live", market="spot")

    def test_dashboard_and_label(self):
        with open(os.path.join(ROOT, "app/static/dashboard.html"), encoding="utf-8") as f:
            h = f.read()
        self.assertIn('<option value="PAXGUSDT@spot">', h)
        self.assertIn('<option value="PAXGUSDT">', h)
        self.assertIn("p.symbol.replace('@spot', '現貨')".replace("p.symbol", "String(p.symbol)"), h)
        self.assertIn("symbol", J.LABEL_PARAM_KEYS)
