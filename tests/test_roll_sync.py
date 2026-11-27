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


def pos(ticket, volume, price, sl, typ=0, symbol="XAUUSD+", time=None):
    return {"ticket": ticket, "symbol": symbol, "type": typ, "volume": volume, "price": price, "sl": sl, "tp": 0,
            "profit": 0, "swap": 0, "magic": 0, "time": 1_790_000_000 + ticket * 60 if time is None else time}


def snap(positions, balance=788.77, equity=1676.27, bid=3672.09, ask=3672.29):
    return {"ea": "TzujenRollSync", "login": 12345678, "server": "VantageInternational-Live", "company": "Vantage",
            "currency": "USD", "trade_mode": 2, "leverage": 2000, "balance": balance, "equity": equity,
            "positions": positions, "ticks": {"XAUUSD+": {"bid": bid, "ask": ask, "time": 0}}}


def fresh():
    from app import basis
    basis.reset()
    basis._state["loaded"] = True          # 不讀資料庫
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
        self.assertEqual(v["inputs"]["core_stop"], 3600.0)              # 只有一筆＝核心單
        self.assertIsNone(v["inputs"]["stop"])
        self.assertEqual(v["inputs"]["positions"], [{"lots": 0.04, "entry": 3447.56, "core": True}])
        self.assertEqual(v["account"]["company"], "Vantage")

    def test_sl_mismatch_among_adds_uses_lowest_and_names_target(self):
        ps = [pos(1, 0.04, 3447.56, 3573.0), pos(2, 0.01, 3703.0, 3630.0), pos(3, 0.01, 3733.0, 3660.0)]
        S.receive(snap(ps, bid=3740), TOKEN, now=T0)
        v = S.account_view(now=T0)
        self.assertEqual(v["inputs"]["core_stop"], 3573.0)
        self.assertEqual(v["inputs"]["stop"], 3630.0)                   # 加碼單裡最保守的
        mis = [i for i in v["issues"] if i["key"] == "sl_mismatch"]
        self.assertEqual(len(mis), 1)
        self.assertEqual(mis[0]["target"], 3660.0)
        self.assertIn("3703 那筆還在 3630", mis[0]["text"])
        self.assertNotIn("3447.56", mis[0]["text"])                     # 核心單停損本來就不同，不算

    def test_core_and_add_different_stops_is_not_mismatch(self):
        S.receive(snap([pos(1, 0.04, 3447.56, 3573.0), pos(2, 0.01, 3703.0, 3660.0)], bid=3740), TOKEN, now=T0)
        v = S.account_view(now=T0)
        self.assertEqual(v["inputs"]["stop"], 3660.0)
        self.assertEqual([p["core"] for p in v["inputs"]["positions"]], [True, False])
        self.assertNotIn("sl_mismatch", [i["key"] for i in v["issues"]])

    def test_core_is_earliest_by_open_time_not_list_order(self):
        ps = [pos(7, 0.01, 3703.0, 3660.0, time=1_790_000_900), pos(3, 0.04, 3447.56, 3573.0, time=1_790_000_100)]
        S.receive(snap(ps, bid=3740), TOKEN, now=T0)
        v = S.account_view(now=T0)
        self.assertEqual(v["core_ticket"], 3)
        self.assertEqual(v["inputs"]["core_stop"], 3573.0)

    def test_core_mode_none_treats_all_alike(self):
        rules = {**S.default_rules(), "core_mode": "none"}
        with mock.patch.object(S, "get_rules", return_value=rules):
            S.receive(snap([pos(1, 0.04, 3447.56, 3600.0), pos(2, 0.01, 3733.0, 3660.0)], bid=3740), TOKEN, now=T0)
            v = S.account_view(now=T0)
        self.assertEqual(v["inputs"]["stop"], 3600.0)
        self.assertIsNone(v["inputs"]["core_stop"])
        self.assertIn("sl_mismatch", [i["key"] for i in v["issues"]])
        self.assertIn("3447.56 那筆還在 3600", next(i["text"] for i in v["issues"] if i["key"] == "sl_mismatch"))

    def test_short_uses_ask_and_highest_stop(self):
        S.receive(snap([pos(1, 0.02, 3700, 3720, typ=1), pos(2, 0.01, 3650, 3690, typ=1)], bid=3640, ask=3640.3),
                  TOKEN, now=T0)
        v = S.account_view(now=T0)
        self.assertEqual(v["inputs"]["direction"], "short")
        self.assertEqual(v["inputs"]["price"], 3640.3)
        self.assertEqual(v["inputs"]["core_stop"], 3720.0)
        self.assertEqual(v["inputs"]["stop"], 3690.0)

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
        ps = [pos(1, 0.04, 3447.56, 3573.0), pos(2, 0.01, 3703.0, 3630.0), pos(3, 0.01, 3733.0, 3660.0)]
        S.receive(snap(ps, equity=1900, bid=3740), TOKEN, now=T0)
        self.assertTrue(any("⚠️ 加碼單停損不一致" in m and "3660" in m for m in self.sent()), self.sent())

    def test_add_point_notified_once_per_anchor(self):
        # 只有核心單(停損 3640)、淨值夠大，讓規則允許加碼；現價離 3447.56 早就超過 30 點
        # 第一筆加碼單的停損＝加碼價 − 加碼間距 30 ＝ 3642.09
        p1 = [pos(1, 0.04, 3447.56, 3640.0)]
        S.receive(snap(p1, balance=1788.77, equity=2676.27), TOKEN, now=T0)
        adds = [m for m in self.sent() if "到加碼點了" in m]
        self.assertEqual(len(adds), 1)
        self.assertIn("加碼單停損 3642.09", adds[0])
        self.assertIn("核心單停損照舊", adds[0])
        S.receive(snap(p1, balance=1788.77, equity=2680.0, bid=3673.0), TOKEN, now=T0 + 3)
        self.assertEqual(len([m for m in self.sent() if "到加碼點了" in m]), 1)
        # 加碼成交後最有利的進場價變了：還沒離 30 點就不通知
        p2 = p1 + [pos(2, 0.01, 3672.5, 3640.0)]
        S.receive(snap(p2, balance=1788.77, equity=2680.0, bid=3680.0), TOKEN, now=T0 + 6)
        self.assertEqual(len([m for m in self.sent() if "到加碼點了" in m]), 1)

    def test_lock_alert_when_profit_grows(self):
        # 核心單 0.04 @3447.56 停損 3582.28、加碼 0.02 @3676.83 停損 3646(使用者 11/27 的部位)，規則 15 倍、保住 30%
        rules = {**S.default_rules(), "max_leverage": 15, "lock_pct": 30}
        ps = [pos(1, 0.04, 3447.56, 3582.28), pos(2, 0.02, 3676.83, 3646.0)]

        def at(bid, now, p=ps):
            eq = 788.71 - 10.62 + sum(100 * q["volume"] * (bid - q["price"]) for q in p)
            S.receive(snap(p, balance=788.71, equity=round(eq, 2), bid=bid), TOKEN, now=now)

        with mock.patch.object(S, "get_rules", return_value=rules):
            at(3677.6, T0)
            self.assertNotIn("lock", S._state["alerts"])                     # 現在保住 466 ≥ 273，沒事
            at(3800.0, T0 + 3)                                              # 漲上去：要保住的變多
            locks = [m for m in self.sent() if "浮盈變大了" in m]
            self.assertEqual(len(locks), 1)
            self.assertIn("加碼單停損要上移到", locks[0])
            self.assertIn("目前 3646", locks[0])
            at(3805.0, T0 + 6)
            at(3810.0, T0 + 9)
            self.assertEqual(len([m for m in self.sent() if "浮盈變大了" in m]), 1)   # 價格在動不重發
            line = S.status(now=T0 + 9)["active_alerts"]["lock"]["text"]     # 網頁上的文字跟著最新價格更新
            self.assertIn("要上移到", line)
            p2 = [pos(1, 0.04, 3447.56, 3582.28), pos(2, 0.02, 3676.83, 3760.0)]
            at(3810.0, T0 + 12, p2)
            self.assertTrue(any("✅ 已處理" in m and "浮盈變大了" in m for m in self.sent()), self.sent())
            self.assertNotIn("lock", S._state["alerts"])

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
            for bad in ({"lock_pct": 150}, {"core_mode": "biggest"}, {"add_stop_points": -5}):
                with self.assertRaises(ValueError):
                    S.save_rules(bad)
            self.assertEqual(sv.call_count, 0)

    def test_core_rules_saved(self):
        with mock.patch("app.db.save_app_settings"):
            r = S.save_rules({"core_mode": "none", "add_stop_points": "20"})
        self.assertEqual(r["core_mode"], "none")
        self.assertEqual(r["add_stop_points"], 20.0)
        self.assertEqual(S.default_rules()["core_mode"], "earliest")


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
            self.assertEqual(r["inputs"]["core_stop"], 3600.0)

    def test_rules_endpoint_needs_password(self):
        with mock.patch.dict(os.environ, {"SETTINGS_PASSWORD": "pw-123"}), \
                mock.patch("app.db.save_app_settings"), mock.patch("app.db.insert_settings_audit"):
            r = self.c.post("/tools/roll-rules", json={"password": "x", "rules": {"max_leverage": 12}}).json()
            self.assertIs(r["success"], False)
            r = self.c.post("/tools/roll-rules", json={"password": "pw-123", "rules": {"max_leverage": 12}}).json()
            self.assertIs(r["success"], True)
            self.assertEqual(r["rules"]["max_leverage"], 12.0)


class EaFile(unittest.TestCase):
    def test_ea_is_read_only_by_default(self):
        with open(os.path.join(ROOT, "windows", "TzujenRollSync.mq5"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn('"/tools/roll-sync"', src)
        self.assertIn("X-Token", src)
        # r129 起 EA 能執行指令，但預設關著(AllowTrading=false)＝跟 r125 一樣只回報；細節見 test_roll_exec
        self.assertIn("input bool   AllowTrading = false;", src)
        for banned in ("OrderSend(", "PositionClose", "OrderModify"):
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
