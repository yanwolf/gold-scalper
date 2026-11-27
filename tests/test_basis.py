"""
r128 的測試：幣安↔券商價差紀錄(app/basis.py)、休市預警(app/roll_sync.py)、/tools/basis。
執行：python -m unittest tests.test_basis -v
"""
import logging
import os
import unittest
from unittest import mock

logging.disable(logging.CRITICAL)

from app import basis as B  # noqa: E402
from app import roll_sync as S  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
T0 = 1_800_000_000.0 - (1_800_000_000.0 % 60)      # 對齊整分，方便算分鐘
TOKEN = "roll-test-token-123"


def tick(mid, t, spread=0.2):
    return {"bid": mid - spread / 2, "ask": mid + spread / 2, "time": t}


class Base(unittest.TestCase):
    def setUp(self):
        B.reset()
        B._state["loaded"] = True
        self.saved, self.events = [], []
        self.p1 = mock.patch("app.db.save_basis_minute", side_effect=lambda r: self.saved.append(dict(r)))
        self.p2 = mock.patch("app.db.insert_basis_event", side_effect=lambda e: self.events.append(dict(e)))
        self.p1.start()
        self.p2.start()

    def tearDown(self):
        self.p1.stop()
        self.p2.stop()
        B.reset()


class Record(Base):
    def test_basis_and_minute_rollup(self):
        b = B.record(tick(3677.6, 1), now=T0, bn_mid=3673.0)
        self.assertAlmostEqual(b, 4.6, places=6)
        B.record(tick(3678.0, 2), now=T0 + 3, bn_mid=3673.2)               # 4.8
        B.record(tick(3679.0, 3), now=T0 + 61, bn_mid=3674.0)              # 下一分鐘：上一分鐘收進來
        self.assertEqual(len(self.saved), 1)
        self.assertEqual(self.saved[0]["n"], 2)
        self.assertAlmostEqual(self.saved[0]["sum"], 9.4, places=6)
        self.assertAlmostEqual(self.saved[0]["max"], 4.8, places=6)
        st = B.stats(3600, now=T0 + 62)
        self.assertEqual(st["samples"], 3)
        self.assertAlmostEqual(st["mean"], (4.6 + 4.8 + 5.0) / 3, places=2)
        self.assertGreater(st["std"], 0)

    def test_no_binance_no_record(self):
        with mock.patch.object(B, "_binance_mid", return_value=None) as bm:
            self.assertIsNone(B.record(tick(3677.6, 1), now=T0))
        self.assertTrue(bm.called)                                          # 前提：真的去問了幣安
        self.assertEqual(B.stats(3600, now=T0)["samples"], 0)
        self.assertIsNone(B.current(now=T0)["basis"])

    def test_drift_over_horizon(self):
        # 70 分鐘，每分鐘一筆；價差從 4.0 線性漂到 4.69
        for i in range(70):
            B.record(tick(3600 + i, i), now=T0 + 60 * i, bn_mid=3600 + i - (4.0 + 0.01 * i))
        st = B.stats(7200, now=T0 + 60 * 70)
        self.assertEqual(st["drift"]["15"]["max"], 0.15)
        self.assertEqual(st["drift"]["60"]["max"], 0.6)
        self.assertGreater(st["drift"]["15"]["count"], 0)

    def test_drift_counts_both_directions(self):
        for i in range(30):                                                # 價差往下漂：4.0 → 3.71
            B.record(tick(3600 + i, i), now=T0 + 60 * i, bn_mid=3600 + i - (4.0 - 0.01 * i))
        st = B.stats(7200, now=T0 + 60 * 30)
        self.assertEqual(st["drift"]["15"]["max"], 0.15)
        self.assertEqual(st["drift"]["15"]["p50"], 0.15)

    def test_close_then_projection_then_reopen_event(self):
        B.record(tick(3677.6, 100), now=T0, bn_mid=3673.0)                 # 價差 4.6
        B.record(tick(3677.6, 100), now=T0 + 60)
        self.assertIs(B.is_closed(), False)
        B.record(tick(3677.6, 100), now=T0 + B.STALE_CFD_SECONDS + 5)     # 報價不動超過 2 分鐘＝休市
        self.assertIs(B.is_closed(), True)
        self.assertEqual(B.current(now=T0 + 200)["source"], "close")
        self.assertAlmostEqual(B.current(now=T0 + 200)["basis"], 4.6, places=2)
        self.assertIsNone(B.record(tick(3677.6, 100), now=T0 + 3000, bn_mid=3650.0))   # 休市中不記
        pr = B.projected_cfd(now=T0 + 3000, bn_mid=3650.0)
        self.assertAlmostEqual(pr["projected"], 3654.6, places=6)
        # 兩天後開盤：券商開在 3655.5，幣安 3650.2
        B.record(tick(3655.5, 999), now=T0 + 2 * 86400, bn_mid=3650.2)
        self.assertIs(B.is_closed(), False)
        self.assertEqual(len(self.events), 1)
        ev = self.events[0]
        self.assertAlmostEqual(ev["cfd_gap"], -22.1, places=2)
        self.assertAlmostEqual(ev["bn_move"], -22.8, places=2)
        self.assertAlmostEqual(ev["projection_error"], 0.7, places=2)       # 3655.5 − (3650.2 + 4.6)
        self.assertEqual(len(B.report(now=T0 + 2 * 86400)["events"]), 1)

    def test_short_pause_is_not_an_event(self):
        B.record(tick(3677.6, 100), now=T0, bn_mid=3673.0)
        B.record(tick(3677.6, 100), now=T0 + 200)                          # 休市
        B.record(tick(3678.0, 101), now=T0 + 600, bn_mid=3673.3)          # 10 分鐘就回來
        self.assertIs(B.is_closed(), False)
        self.assertEqual(len(self.events), 0)
        self.assertAlmostEqual(B.current(now=T0 + 600)["basis"], 4.7, places=2)

    def test_load_from_db(self):
        B.reset()
        rows = [{"m": int(T0 // 60) - 5, "n": 4, "sum": 18.0, "sumsq": 81.0, "min": 4.5, "max": 4.5, "cfd": 1, "bn": 1}]
        with mock.patch("app.db.load_basis_minutes", return_value=rows), \
                mock.patch("app.db.load_basis_events", return_value=[{"cfd_gap": 3.0}]):
            st = B.stats(3600, now=T0)
            self.assertEqual(st["samples"], 4)
            self.assertAlmostEqual(st["mean"], 4.5, places=6)
            self.assertEqual(B.report(now=T0)["events"], [{"cfd_gap": 3.0}])


def pos(ticket, volume, price, sl, typ=0):
    return {"ticket": ticket, "symbol": "XAUUSD+", "type": typ, "volume": volume, "price": price, "sl": sl, "tp": 0,
            "time": 1_790_000_000 + ticket}


def snap(positions, bid, t, balance=788.71, equity=1699.79):
    return {"login": 1, "company": "Vantage", "trade_mode": 2, "balance": balance, "equity": equity,
            "positions": positions, "ticks": {"XAUUSD+": {"bid": bid, "ask": bid + 0.2, "time": t}}}


class Weekend(Base):
    def setUp(self):
        super().setUp()
        S._state.update({"snapshot": None, "received_at": None, "rules": None, "alerts": {}, "disconnected": False,
                         "had_positions": False, "add_keys": set(), "last_eval": None, "sent": []})
        self.env = mock.patch.dict(os.environ, {"ROLL_SYNC_TOKEN": TOKEN})
        self.env.start()
        self.rules = mock.patch.object(S, "get_rules", return_value={**S.default_rules(), "max_leverage": 15,
                                                                      "lock_pct": 30})
        self.rules.start()

    def tearDown(self):
        self.rules.stop()
        self.env.stop()
        S._state["sent"] = []
        S._state["alerts"] = {}
        super().tearDown()

    def test_weekend_projection_crosses_stop(self):
        ps = [pos(1, 0.04, 3447.56, 3582.28), pos(2, 0.02, 3676.83, 3646.0)]
        with mock.patch.object(B, "_binance_mid", return_value=3673.0):
            S.receive(snap(ps, 3677.5, 100), TOKEN, now=T0)                     # 價差 ≈ 4.6
            S.receive(snap(ps, 3677.5, 100), TOKEN, now=T0 + 200)               # 休市
        self.assertIs(B.is_closed(), True)
        with mock.patch.object(B, "_binance_mid", return_value=3660.0):        # 幣安跌一點，還沒越過 3646
            S.receive(snap(ps, 3677.5, 100), TOKEN, now=T0 + 3600)
        self.assertEqual([m for m in S._state["sent"] if "休市預警" in m], [])
        with mock.patch.object(B, "_binance_mid", return_value=3630.0):        # 換算 ≈ 3634.6 < 3646
            S.receive(snap(ps, 3677.5, 100), TOKEN, now=T0 + 7200)
            S.receive(snap(ps, 3677.5, 100), TOKEN, now=T0 + 7203)
        warns = [m for m in S._state["sent"] if "休市預警" in m]
        self.assertEqual(len(warns), 1)
        self.assertIn("3676.83 那筆停損 3646", warns[0])
        self.assertNotIn("3582.28", warns[0])
        self.assertIn("多吐回約 22.", warns[0])                                 # 0.02 × 100 × (3646 − 3634.6..)
        # 開盤後預警解除
        with mock.patch.object(B, "_binance_mid", return_value=3640.0):
            S.receive(snap(ps, 3644.0, 999), TOKEN, now=T0 + 9000)
        self.assertIs(B.is_closed(), False)
        self.assertTrue(any("✅ 已處理" in m and "休市預警" in m for m in S._state["sent"]), S._state["sent"])


class Endpoints(Base):
    def test_basis_endpoint_and_levels(self):
        from fastapi.testclient import TestClient
        from app.main import app
        from app import main as M
        c = TestClient(app)
        B.record(tick(3677.6, 1), now=__import__("time").time(), bn_mid=3673.0)
        r = c.get("/tools/basis").json()
        self.assertEqual(r["current"]["source"], "live")
        self.assertAlmostEqual(r["current"]["basis"], 4.6, places=2)
        self.assertIn("week", r)
        with mock.patch.object(M.binance_streamer, "get_latest", return_value={"bid": 3672.9, "ask": 3673.1}), \
                mock.patch.object(M.binance_streamer, "get_recent_candles", return_value=[]), \
                mock.patch.object(M.daily_smc, "get_snapshot", return_value={}):
            lv = c.get("/tools/roll-levels").json()
        self.assertAlmostEqual(lv["basis"]["basis"], 4.6, places=2)
        with open(os.path.join(ROOT, "app", "static", "roll.html"), encoding="utf-8") as f:
            page = f.read()
        self.assertIn("/tools/basis", page)
        self.assertIn("levels.basis.basis", page)


if __name__ == "__main__":
    unittest.main()
