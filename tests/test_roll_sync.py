"""
r125 的測試：滾倉同步(app/roll_sync.py、/tools/roll-sync、/tools/roll-account、/tools/roll-rules、唯讀 EA)。
執行：python -m unittest tests.test_roll_sync -v
"""
import json
import logging
import os
import unittest
from unittest import mock

logging.disable(logging.CRITICAL)

from app import roll_sync as S  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN = "roll-test-token-123"
T0 = 1_800_000_000.0


def pos(ticket, volume, price, sl, typ=0, symbol="XAUUSD+"):
    return {"ticket": ticket, "symbol": symbol, "type": typ, "volume": volume, "price": price, "sl": sl, "tp": 0,
            "profit": 0, "swap": 0, "magic": 0, "time": 0}


def snap(positions, balance=788.77, equity=1676.27, bid=3672.09, ask=3672.29):
    return {"ea": "TzujenRollSync", "login": 12345678, "server": "VantageInternational-Live", "company": "Vantage",
            "currency": "USD", "trade_mode": 2, "leverage": 2000, "balance": balance, "equity": equity,
            "positions": positions, "ticks": {"XAUUSD+": {"bid": bid, "ask": ask, "time": 0}}}


def fresh():
    S._state.update({"snapshot": None, "received_at": None, "rules": None, "alerts": {}, "disconnected": False,
                     "had_positions": False, "add_keys": set(), "last_eval": None, "sent": []})


class Base(unittest.TestCase):
    def setUp(self):
        fresh()
        self.env = mock.patch.dict(os.environ, {"ROLL_SYNC_TOKEN": TOKEN})
        self.env.start()
        # 規則固定用預設值，不讀資料庫
        self.rules = mock.patch.object(S, "get_rules", return_value=S.default_rules())
        self.rules.start()

    def tearDown(self):
        self.rules.stop()
        self.env.stop()
        fresh()

    def sent(self):
        return list(S._state["sent"])


class Receive(Base):
    def test_token_and_format(self):
        ok, err = S.receive(snap([]), "wrong-token", now=T0)
        self.assertIs(ok, False)
        self.assertEqual(err, "token 不對")
        ok, err = S.receive({"balance": 1, "equity": 1}, TOKEN, now=T0)
        self.assertIs(ok, False)
        self.assertIn("positions", err)
        self.assertIsNone(S._state["snapshot"])
        ok, err = S.receive(snap([]), TOKEN, now=T0)
        self.assertIs(ok, True)
        self.assertEqual(S._state["received_at"], T0)

    def test_disabled_without_token(self):
        with mock.patch.dict(os.environ, {"ROLL_SYNC_TOKEN": ""}):
            ok, err = S.receive(snap([]), "", now=T0)
            self.assertIs(ok, False)
            self.assertIn("沒有啟用", err)
            self.assertIs(S.start(), False)


class View(Base):
    def test_inputs_from_positions(self):
        S.receive(snap([pos(1, 0.04, 3447.56, 3600.0)]), TOKEN, now=T0)
        v = S.account_view(now=T0 + 5)
        self.assertIs(v["connected"], True)
        self.assertEqual(v["inputs"]["direction"], "long")
        self.assertEqual(v["inputs"]["price"], 3672.09)                 # 多單用 bid(平倉價)
        self.assertEqual(v["inputs"]["stop"], 3600.0)
        self.assertEqual(v["inputs"]["positions"], [{"lots": 0.04, "entry": 3447.56}])
        self.assertEqual(v["account"]["company"], "Vantage")

    def test_sl_mismatch_uses_lowest_and_names_target(self):
        S.receive(snap([pos(1, 0.04, 3447.56, 3600.0), pos(2, 0.01, 3733.0, 3660.0)], bid=3740), TOKEN, now=T0)
        v = S.account_view(now=T0)
        self.assertEqual(v["inputs"]["stop"], 3600.0)                   # 計算用最保守的
        mis = [i for i in v["issues"] if i["key"] == "sl_mismatch"]
        self.assertEqual(len(mis), 1)
        self.assertEqual(mis[0]["target"], 3660.0)
        self.assertIn("3447.56 那筆還在 3600", mis[0]["text"])

    def test_short_uses_ask_and_highest_stop(self):
        S.receive(snap([pos(1, 0.02, 3700, 3720, typ=1), pos(2, 0.01, 3650, 3690, typ=1)], bid=3640, ask=3640.3),
                  TOKEN, now=T0)
        v = S.account_view(now=T0)
        self.assertEqual(v["inputs"]["direction"], "short")
        self.assertEqual(v["inputs"]["price"], 3640.3)
        self.assertEqual(v["inputs"]["stop"], 3720.0)

    def test_other_symbols_and_mixed(self):
        S.receive(snap([pos(1, 0.04, 3447.56, 3600.0), pos(9, 1, 20000, 0, symbol="NAS100")]), TOKEN, now=T0)
        v = S.account_view(now=T0)
        self.assertIn("others", [i["key"] for i in v["issues"]])
        self.assertEqual(len(v["inputs"]["positions"]), 1)
        S.receive(snap([pos(1, 0.04, 3447.56, 3600.0), pos(2, 0.01, 3700, 3720, typ=1)]), TOKEN, now=T0)
        v = S.account_view(now=T0)
        self.assertIn("mixed", [i["key"] for i in v["issues"]])
        self.assertIsNone(v["inputs"])

    def test_stale_is_not_connected(self):
        S.receive(snap([pos(1, 0.04, 3447.56, 3600.0)]), TOKEN, now=T0)
        v = S.account_view(now=T0 + S.STALE_SECONDS + 1)
        self.assertIs(v["connected"], False)
        self.assertGreater(v["age_seconds"], S.STALE_SECONDS)


class Alerts(Base):
    def test_no_stop_alerts_repeats_and_resolves(self):
        S.receive(snap([pos(1, 0.04, 3447.56, 0)]), TOKEN, now=T0)
        first = [m for m in self.sent() if "沒有停損" in m]
        self.assertEqual(len(first), 1)
        S.receive(snap([pos(1, 0.04, 3447.56, 0)]), TOKEN, now=T0 + 60)
        self.assertEqual(len([m for m in self.sent() if "沒有停損" in m]), 1)      # 30 分鐘內不重發
        S.receive(snap([pos(1, 0.04, 3447.56, 0)]), TOKEN, now=T0 + S.REPEAT_SECONDS + 1)
        again = [m for m in self.sent() if "沒有停損" in m]
        self.assertEqual(len(again), 2)
        self.assertIn("第 2 次提醒", again[-1])
        S.receive(snap([pos(1, 0.04, 3447.56, 3600.0)]), TOKEN, now=T0 + S.REPEAT_SECONDS + 10)
        self.assertTrue(any("✅ 已處理" in m and "沒有停損" in m for m in self.sent()), self.sent())
        self.assertNotIn("no_sl", S._state["alerts"])

    def test_over_leverage_does_not_spam_when_numbers_move(self):
        # 截圖那時候：兩筆各 0.05，23 倍
        p2 = [pos(1, 0.05, 3447.56, 3600.0), pos(2, 0.05, 3621.87, 3600.0)]
        S.receive(snap(p2, balance=300.01, equity=1567.48, bid=3662.79), TOKEN, now=T0)
        S.receive(snap(p2, balance=300.01, equity=1580.0, bid=3664.0), TOKEN, now=T0 + 3)
        S.receive(snap(p2, balance=300.01, equity=1590.0, bid=3665.0), TOKEN, now=T0 + 6)
        over = [m for m in self.sent() if "超過上限" in m]
        self.assertEqual(len(over), 1)
        self.assertIn("3621.87 那筆平 0.05 手", over[0])
        self.assertIn("over_leverage", S._state["alerts"])

    def test_sl_mismatch_alert(self):
        S.receive(snap([pos(1, 0.04, 3447.56, 3600.0), pos(2, 0.01, 3733.0, 3660.0)], equity=1800, bid=3740),
                  TOKEN, now=T0)
        self.assertTrue(any("⚠️ 各單停損不一致" in m and "3660" in m for m in self.sent()), self.sent())

    def test_add_point_notified_once_per_anchor(self):
        # 淨值夠大、停損夠緊，讓規則允許加碼；現價離 3447.56 早就超過 30 點
        p1 = [pos(1, 0.04, 3447.56, 3640.0)]
        S.receive(snap(p1, balance=1788.77, equity=2676.27), TOKEN, now=T0)
        adds = [m for m in self.sent() if "到加碼點了" in m]
        self.assertEqual(len(adds), 1)
        self.assertIn("停損 3640", adds[0])
        self.assertIn("舊單停損", adds[0])
        S.receive(snap(p1, balance=1788.77, equity=2680.0, bid=3673.0), TOKEN, now=T0 + 3)
        self.assertEqual(len([m for m in self.sent() if "到加碼點了" in m]), 1)
        # 加碼成交後最有利的進場價變了：還沒離 30 點就不通知
        p2 = p1 + [pos(2, 0.01, 3672.5, 3640.0)]
        S.receive(snap(p2, balance=1788.77, equity=2680.0, bid=3680.0), TOKEN, now=T0 + 6)
        self.assertEqual(len([m for m in self.sent() if "到加碼點了" in m]), 1)

    def test_disconnect_once_and_recover(self):
        S.receive(snap([pos(1, 0.04, 3447.56, 3600.0)]), TOKEN, now=T0)
        S.evaluate(now=T0 + S.STALE_SECONDS + 5)
        S.evaluate(now=T0 + S.STALE_SECONDS + 60)
        off = [m for m in self.sent() if "沒回報" in m]
        self.assertEqual(len(off), 1)
        S.receive(snap([pos(1, 0.04, 3447.56, 3600.0)]), TOKEN, now=T0 + S.STALE_SECONDS + 90)
        self.assertTrue(any("EA 恢復回報" in m for m in self.sent()))
        self.assertIs(S._state["disconnected"], False)

    def test_no_disconnect_alert_without_positions(self):
        S.receive(snap([]), TOKEN, now=T0)
        S.evaluate(now=T0 + S.STALE_SECONDS + 5)
        self.assertEqual(len(S._state["sent"]), 0)
        self.assertIs(S._state["had_positions"], False)


class Rules(unittest.TestCase):
    def setUp(self):
        fresh()

    def tearDown(self):
        fresh()

    def test_save_and_load(self):
        saved = {}
        with mock.patch("app.db.save_app_settings", side_effect=lambda d: saved.update(d)):
            r = S.save_rules({"max_leverage": "15", "lock_pct": 40, "add_step": 25, "add_lots": "", "trail_stop": "false"})
        self.assertEqual(r["max_leverage"], 15.0)
        self.assertIs(r["trail_stop"], False)
        self.assertIsNone(r["add_lots"])
        self.assertEqual(json.loads(saved[S.RULES_DB_KEY])["add_step"], 25.0)
        fresh()
        with mock.patch("app.db.get_app_settings", return_value=saved):
            got = S.get_rules()
        self.assertEqual(got["lock_pct"], 40.0)
        self.assertEqual(got["cost_points"], 0.4)                          # 沒存的欄位用預設

    def test_invalid_rejected(self):
        with mock.patch("app.db.save_app_settings") as sv:
            with self.assertRaises(ValueError):
                S.save_rules({"lock_pct": 150})
            self.assertEqual(sv.call_count, 0)


class Endpoints(unittest.TestCase):
    def setUp(self):
        fresh()
        from fastapi.testclient import TestClient
        from app.main import app
        self.c = TestClient(app)

    def tearDown(self):
        fresh()

    def test_sync_disabled_404(self):
        with mock.patch.dict(os.environ, {"ROLL_SYNC_TOKEN": ""}):
            r = self.c.post("/tools/roll-sync", json=snap([]), headers={"X-Token": "x"})
            self.assertEqual(r.status_code, 404)
            self.assertIs(self.c.post("/tools/roll-account", json={}).json()["enabled"], False)

    def test_sync_token_and_account_password(self):
        env = {"ROLL_SYNC_TOKEN": TOKEN, "SETTINGS_PASSWORD": "pw-123"}
        with mock.patch.dict(os.environ, env), mock.patch.object(S, "get_rules", return_value=S.default_rules()):
            self.assertEqual(self.c.post("/tools/roll-sync", json=snap([]), headers={"X-Token": "bad"}).status_code, 401)
            r = self.c.post("/tools/roll-sync", json=snap([pos(1, 0.04, 3447.56, 3600.0)]), headers={"X-Token": TOKEN})
            self.assertEqual(r.status_code, 200)
            r = self.c.post("/tools/roll-account", json={"password": "nope"}).json()
            self.assertIs(r["success"], False)
            self.assertNotIn("inputs", r)                                   # 密碼錯就看不到持倉
            r = self.c.post("/tools/roll-account", json={"password": "pw-123"}).json()
            self.assertIs(r["success"], True)
            self.assertEqual(r["inputs"]["stop"], 3600.0)

    def test_rules_endpoint_needs_password(self):
        with mock.patch.dict(os.environ, {"SETTINGS_PASSWORD": "pw-123"}), \
                mock.patch("app.db.save_app_settings"), mock.patch("app.db.insert_settings_audit"):
            r = self.c.post("/tools/roll-rules", json={"password": "x", "rules": {"max_leverage": 12}}).json()
            self.assertIs(r["success"], False)
            r = self.c.post("/tools/roll-rules", json={"password": "pw-123", "rules": {"max_leverage": 12}}).json()
            self.assertIs(r["success"], True)
            self.assertEqual(r["rules"]["max_leverage"], 12.0)


class EaFile(unittest.TestCase):
    def test_ea_is_read_only(self):
        with open(os.path.join(ROOT, "windows", "TzujenRollSync.mq5"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn('"/tools/roll-sync"', src)
        self.assertIn("X-Token", src)
        for banned in ("#include", "OrderSend", "CTrade", "PositionClose", "PositionModify", "OrderModify"):
            self.assertNotIn(banned, src)

    def test_installer_cmd_is_ascii(self):
        with open(os.path.join(ROOT, "windows", "install_roll_sync.cmd"), "rb") as f:
            raw = f.read()
        self.assertIn(b"install_roll_sync.ps1", raw)
        self.assertTrue(all(b < 128 for b in raw))
        with open(os.path.join(ROOT, "windows", "install_roll_sync.ps1"), "rb") as f:
            self.assertEqual(f.read(3), b"\xef\xbb\xbf")                    # PowerShell 5 要 BOM 才讀得懂中文


if __name__ == "__main__":
    unittest.main()
