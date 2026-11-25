"""
r123 的測試：lab 引擎的部位回報給 cfd-live(app/ext_signal.py)。
執行：python -m unittest tests.test_ext_signal -v
"""
import logging
import os
import unittest
from collections import deque
from unittest import mock

logging.disable(logging.CRITICAL)

from app import ext_signal as X  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POS = {"direction": "bullish", "entry_time": "2026-11-25T03:00:00+00:00", "entry_price": 4100.0, "sl_price": 4094.0,
       "trailing_active": False, "chan_reason": "價格站上中樞上界 4098.00，突破確立"}


class Eng:
    def __init__(self, pos=None, closed=()):
        self.engine_id = "chan_profile_900"
        self._position = dict(pos) if pos else None
        self._closed_trades_memory = deque(closed, maxlen=200)


class Resp:
    def __init__(self, code=200, text="{}"):
        self.status_code, self.text = code, text


def fresh():
    X._state.clear()


class Payload(unittest.TestCase):
    def setUp(self):
        fresh()

    def test_flat_and_position(self):
        p = X.build_payload(Eng(), {})
        self.assertEqual((p["position"], p["engine_id"], p["source"]), (None, "chan_profile_900", "gold-scalper"))
        st = {}
        p2 = X.build_payload(Eng(POS), st)
        pos = p2["position"]
        self.assertIsNotNone(pos, "前提：有部位")
        self.assertEqual(pos["key"], "chan_profile_900:2026-11-25T03:00:00+00:00")
        self.assertEqual((pos["direction"], pos["entry_price"], pos["sl_points"]), ("bullish", 4100.0, 6.0))

    def test_initial_stop_distance_survives_trailing(self):
        st = {}
        e = Eng(POS)
        self.assertEqual(X.build_payload(e, st)["position"]["sl_points"], 6.0)
        e._position["sl_price"] = 4105.0                    # 移動停損墊高了
        p = X.build_payload(e, st)["position"]
        self.assertEqual((p["sl_points"], p["sl_price"]), (6.0, 4105.0), "安全停損用初始距離，現在的停損價照實回報")

    def test_pending_close_counts_as_flat(self):
        self.assertIsNone(X.build_payload(Eng(dict(POS, pending_close=True)), {})["position"])
        self.assertIsNotNone(X.build_payload(Eng(POS), {})["position"], "對照：沒有在平倉中就有部位")

    def test_recent_closed(self):
        closed = [{"entry_time": f"2026-11-24T0{i}:00:00+00:00", "exit_time": "x", "direction": "bearish",
                   "entry_price": 1, "exit_price": 2, "pnl_points": -1.0, "exit_reason": "觸及停損"} for i in range(5)]
        closed.append({"entry_time": "2026-11-24T09:00:00+00:00", "pnl_points": None})
        p = X.build_payload(Eng(closed=closed), {})
        self.assertEqual(len(p["recent_closed"]), 5, "損益未知的不送")
        self.assertEqual(p["recent_closed"][-1]["key"], "chan_profile_900:2026-11-24T04:00:00+00:00")


class Send(unittest.TestCase):
    def setUp(self):
        fresh()
        self.calls = []

    def post(self, code=200, exc=None):
        def f(url, json=None, headers=None, timeout=None):
            self.calls.append({"url": url, "json": json, "headers": headers})
            if exc:
                raise exc
            return Resp(code)
        return f

    def test_send_on_change_and_heartbeat(self):
        e = Eng()
        with mock.patch.object(X, "URL", "http://cfd/ext/gold-scalper"), mock.patch.object(X, "TOKEN", "tok"):
            self.assertIs(X.maybe_send(e, now=1000, post=self.post()), True, "第一次一定送")
            self.assertIsNone(X.maybe_send(e, now=1005, post=self.post()), "沒變化、還沒到心跳時間：不送")
            e._position = dict(POS)
            self.assertIs(X.maybe_send(e, now=1006, post=self.post()), True, "一開倉馬上送")
            self.assertIsNone(X.maybe_send(e, now=1010, post=self.post()))
            self.assertIs(X.maybe_send(e, now=1006 + X.HEARTBEAT_SECONDS, post=self.post()), True, "心跳")
            e._position = None
            self.assertIs(X.maybe_send(e, now=1030, post=self.post()), True, "一平倉馬上送")
        self.assertEqual(len(self.calls), 4)
        self.assertEqual([c["json"]["seq"] for c in self.calls], [1, 2, 3, 4])
        self.assertEqual(self.calls[1]["json"]["position"]["direction"], "bullish")
        self.assertIsNone(self.calls[3]["json"]["position"])
        self.assertEqual(self.calls[0]["headers"], {"x-token": "tok"})
        self.assertEqual(self.calls[0]["url"], "http://cfd/ext/gold-scalper")

    def test_failure_retries_every_poll(self):
        e = Eng(POS)
        with mock.patch.object(X, "URL", "http://cfd"), mock.patch.object(X, "TOKEN", "t"):
            self.assertIs(X.maybe_send(e, now=1000, post=self.post(exc=ConnectionError("down"))), False)
            self.assertIs(X.maybe_send(e, now=1002, post=self.post(code=500)), False, "失敗後下一輪就再送，不等心跳")
            self.assertIs(X.maybe_send(e, now=1004, post=self.post()), True)
            self.assertIsNone(X.maybe_send(e, now=1006, post=self.post()), "成功後恢復正常節奏")
        self.assertEqual(len(self.calls), 3)

    def test_failed_heartbeat_retries_next_poll(self):
        e = Eng(POS)
        with mock.patch.object(X, "URL", "http://cfd"), mock.patch.object(X, "TOKEN", "t"):
            self.assertIs(X.maybe_send(e, now=1000, post=self.post()), True)
            self.assertIs(X.maybe_send(e, now=1000 + X.HEARTBEAT_SECONDS, post=self.post(code=502)), False, "心跳送失敗")
            self.assertIs(X.maybe_send(e, now=1002 + X.HEARTBEAT_SECONDS, post=self.post()), True,
                          "部位沒變也要在下一輪補送(不等下一次心跳)")
        self.assertEqual(len(self.calls), 3)

    def test_status(self):
        with mock.patch.object(X, "ENGINES", ["chan_profile_900"]), mock.patch.object(X, "URL", "http://cfd"), \
                mock.patch.object(X, "TOKEN", "t"), mock.patch.object(X.role_module, "is_live", return_value=False):
            self.assertIsNone(X.status("chan_profile_300"), "沒有要回報的引擎：不顯示")
            s0 = X.status("chan_profile_900")
            self.assertEqual((s0["enabled"], s0["last_ok_ago"]), (True, None))
            X.maybe_send(Eng(), post=self.post(code=401))
            s1 = X.status("chan_profile_900")
            self.assertEqual(s1["fails"], 1)
            self.assertIn("HTTP 401", s1["last_error"])


class Enable(unittest.TestCase):
    def test_needs_all_settings_and_lab(self):
        with mock.patch.object(X, "URL", ""), mock.patch.object(X, "TOKEN", "t"), mock.patch.object(X, "ENGINES", ["a"]):
            on, why = X.enabled_reason()
            self.assertEqual(on, False)
            self.assertIn("EXT_SIGNAL_URL", why)
        with mock.patch.object(X, "URL", "u"), mock.patch.object(X, "TOKEN", "t"), mock.patch.object(X, "ENGINES", ["a"]):
            with mock.patch.object(X.role_module, "is_live", return_value=True):
                on, why = X.enabled_reason()
                self.assertEqual(on, False)
                self.assertIn("lab", why)
                self.assertIs(X.start(lambda: {}), False, "正式端不啟動")
            with mock.patch.object(X.role_module, "is_live", return_value=False):
                self.assertEqual(X.enabled_reason(), (True, None))

    def test_start_runs_thread_and_sends(self):
        sent = []
        eng = Eng(POS)
        with mock.patch.object(X, "URL", "u"), mock.patch.object(X, "TOKEN", "t"), \
                mock.patch.object(X, "ENGINES", ["chan_profile_900"]), mock.patch.object(X, "POLL_SECONDS", 0.01), \
                mock.patch.object(X.role_module, "is_live", return_value=False), \
                mock.patch.object(X.requests, "post", side_effect=lambda *a, **k: sent.append(k["json"]) or Resp()):
            fresh()
            X._thread = None
            self.assertIs(X.start(lambda: {"chan_profile_900": eng}), True)
            import time
            for _ in range(200):
                if sent:
                    break
                time.sleep(0.01)
        self.assertTrue(sent, "背景執行緒有送出")
        self.assertEqual(sent[0]["position"]["key"], "chan_profile_900:2026-11-25T03:00:00+00:00")


class Wiring(unittest.TestCase):
    def test_summary_and_dashboard(self):
        from app import paper_trading as pt
        eng = pt.PaperTradingEngine(interval_seconds=900, label="t", engine_id="ext_sig_test", execution_index=97)
        with mock.patch.object(pt.db, "is_enabled", return_value=False), \
                mock.patch.object(pt.settings_module, "get_settings_changed_at", create=True, return_value=None), \
                mock.patch.object(pt.ext_signal, "status", return_value={"enabled": True, "last_ok_ago": 3}):
            s = eng.get_summary()
        self.assertEqual(s["ext_signal"], {"enabled": True, "last_ok_ago": 3})
        with open(os.path.join(ROOT, "app/static/dashboard.html"), encoding="utf-8") as f:
            h = f.read()
        self.assertIn("回報給 MT5(cfd-live)", h)
        with open(os.path.join(ROOT, "app/main.py"), encoding="utf-8") as f:
            self.assertIn("ext_signal.start(lambda: PAPER_TRADING_ENGINES)", f.read())


if __name__ == "__main__":
    unittest.main()
