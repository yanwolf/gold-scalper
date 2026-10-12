"""
r97 的測試：抓歷史K線遇到幣安限流(429/418)、已用權重偏高時的處理(BINANCE_LESSONS.md 第 6 條 r97)。
執行：python -m unittest tests.test_klines_rate_limit -v
"""
import logging
import unittest
from unittest import mock

logging.disable(logging.CRITICAL)

from app import backtest as B  # noqa: E402
from app import backtest_jobs as J  # noqa: E402

PAGE = [[1, "1", "1", "1", "1", "1", 60_000]]


class R:
    def __init__(self, status=200, headers=None, data=None):
        self.status_code, self.headers, self._data = status, headers or {}, data if data is not None else PAGE

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._data


class Page(unittest.TestCase):
    def _go(self, responses):
        calls, sleeps = [], []
        it = iter(responses)

        def get(url, params=None, timeout=None):
            calls.append(params)
            return next(it)
        with mock.patch.object(B.requests, "get", side_effect=get), mock.patch.object(B, "_sleep", side_effect=sleeps.append):
            try:
                return B._get_klines_page({"x": 1}), calls, sleeps, None
            except Exception as e:   # noqa: BLE001
                return None, calls, sleeps, e

    def test_429_waits_retry_after_then_succeeds(self):
        data, calls, sleeps, err = self._go([R(429, {"Retry-After": "7"}), R(200)])
        self.assertIsNone(err)
        self.assertEqual(data, PAGE)
        self.assertEqual(len(calls), 2)
        self.assertEqual(sleeps, [7.0])

    def test_429_gives_up_after_retries_with_clear_message(self):
        data, calls, sleeps, err = self._go([R(429)] * 10)
        self.assertIsInstance(err, B.BinanceRateLimited)
        self.assertIn("429", str(err))
        self.assertIn("暫時性", str(err))
        self.assertEqual(len(calls), B.KLINES_429_MAX_RETRIES + 1)
        self.assertEqual(sleeps, [float(B.KLINES_429_DEFAULT_WAIT)] * B.KLINES_429_MAX_RETRIES)

    def test_418_stops_immediately(self):
        data, calls, sleeps, err = self._go([R(418, {"Retry-After": "3"}), R(200)])
        self.assertIsInstance(err, B.BinanceRateLimited)
        self.assertIn("418", str(err))
        self.assertEqual((len(calls), sleeps), (1, []), "封 IP 時再打只會延長封鎖")

    def test_retry_after_is_clamped(self):
        _, _, sleeps, err = self._go([R(429, {"Retry-After": "9999"}), R(200)])
        self.assertIsNone(err)
        self.assertEqual(sleeps, [float(B.KLINES_429_MAX_WAIT)])

    def test_high_used_weight_pauses_until_next_minute(self):
        data, calls, sleeps, err = self._go([R(200, {"X-MBX-USED-WEIGHT-1M": str(B.KLINES_WEIGHT_PAUSE_AT)})])
        self.assertEqual(data, PAGE)
        self.assertEqual(len(sleeps), 1)
        self.assertTrue(1 <= sleeps[0] <= 61)

    def test_low_used_weight_does_not_pause(self):
        _, _, sleeps, _ = self._go([R(200, {"X-MBX-USED-WEIGHT-1M": "100"})])
        self.assertEqual(sleeps, [])

    def test_other_http_errors_still_raise_without_retry(self):
        _, calls, sleeps, err = self._go([R(500), R(200)])
        self.assertIsNotNone(err)
        self.assertNotIsInstance(err, B.BinanceRateLimited)
        self.assertEqual((len(calls), sleeps), (1, []))

    def test_fetch_uses_the_guarded_page(self):
        with mock.patch.object(B, "_get_klines_page", return_value=[]) as g:
            B.fetch_historical_klines(days=1, end_time_ms=1_780_000_000_000 - 1)
        self.assertEqual(g.call_count, 1)


class Job(unittest.TestCase):
    def test_rate_limited_window_is_error_not_no_data(self):
        with mock.patch.object(B, "run_backtest", side_effect=B.BinanceRateLimited("幣安限流(429)：暫時性")), \
             mock.patch.object(J.db, "save_backtest_job", return_value=None):
            out = J.start_job({"interval_seconds": 3600}, days=1, windows=2)
            self.assertNotIn("error", out)
            self.assertTrue(J.wait_idle(10))
            job = J.get_job(out["job_id"])
        self.assertEqual(len(job["window_results"]), 2)
        for w in job["window_results"]:
            self.assertIn("429", w["error"])
            self.assertFalse(w["no_data"])


if __name__ == "__main__":
    unittest.main()
