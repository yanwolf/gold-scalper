"""
r129 的測試：滾倉自動執行(app/roll_exec.py、/tools/roll-sync 的 v2 純文字回應、/tools/roll-exec-mode、EA v2)。
執行：python -m unittest tests.test_roll_exec -v
"""
import logging
import os
import re
import unittest
from unittest import mock

logging.disable(logging.CRITICAL)

from app import basis as B  # noqa: E402
from app import roll_exec as E  # noqa: E402
from app import roll_sync as S  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN = "roll-test-token-123"
T0 = 1_800_000_000.0
RULES = {**S.default_rules(), "max_leverage": 15, "lock_pct": 30}


def pos(t, v, p, sl, typ=0, time=None):
    return {"ticket": t, "symbol": "XAUUSD+", "type": typ, "volume": v, "price": p, "sl": sl, "tp": 0,
            "time": 1_790_000_000 + t if time is None else time}


def order(t, price, v=0.01, sl=0.0, magic=E.ROLL_MAGIC, typ=4):
    return {"ticket": t, "symbol": "XAUUSD+", "type": typ, "volume": v, "price": price, "sl": sl, "magic": magic}


def snap(ps, bid, orders=(), allow=True, balance=788.71, adj=-10.62, results=None, stops_level=0):
    s = -1 if ps and ps[0]["type"] == 1 else 1
    eq = balance + adj + sum(100 * p["volume"] * s * ((bid if s == 1 else bid + 0.2) - p["price"]) for p in ps)
    return {"login": 1, "company": "Vantage", "version": "2.00", "balance": balance, "equity": round(eq, 2),
            "positions": ps, "orders": list(orders), "allow_trading": allow, "results": results or [],
            "ticks": {"XAUUSD+": {"bid": bid, "ask": bid + 0.2, "time": 1, "point": 0.01, "digits": 2,
                                  "stops_level": stops_level, "vol_min": 0.01, "vol_step": 0.01}}}


# 11/27 的實際部位＋3706.4 那張已成交：核心 0.04、加碼 0.02(停損還在 3646)、加碼 0.01(停損 3676)
REAL = [pos(1, 0.04, 3447.56, 3582.28), pos(2, 0.02, 3676.83, 3646.0), pos(3, 0.01, 3706.4, 3676.0)]


class Base(unittest.TestCase):
    def setUp(self):
        B.reset()
        B._state["loaded"] = True
        E.reset()
        S._state.update({"snapshot": None, "received_at": None, "rules": None, "alerts": {}, "disconnected": False,
                         "had_positions": False, "add_keys": set(), "last_eval": None, "sent": []})
        self.patches = [mock.patch.dict(os.environ, {"ROLL_SYNC_TOKEN": TOKEN}),
                        mock.patch.object(S, "get_rules", return_value=RULES)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        E.reset()

    def view(self, sn, now=T0):
        S.receive(sn, TOKEN, now=now)
        return S.account_view(now)

    def plan(self, sn, rules=RULES):
        return E.plan(self.view(sn), sn, rules)


class Plan(Base):
    def test_align_add_stops_and_place_next_add(self):
        sn = snap(REAL, 3712.0)
        p = self.plan(sn)
        self.assertEqual(p["blocked"], [])
        mods = [a for a in p["actions"] if a["op"] == "MOD"]
        self.assertEqual(len(mods), 1)
        self.assertEqual((mods[0]["ticket"], mods[0]["from"], mods[0]["to"]), (2, 3646.0, 3676.0))
        self.assertEqual(mods[0]["why"], "加碼單停損對齊")
        pend = [a for a in p["actions"] if a["op"] == "PEND"]
        self.assertEqual(len(pend), 1)
        self.assertEqual((pend[0]["type"], pend[0]["price"], pend[0]["sl"]), ("BUY_STOP", 3736.4, 3706.4))
        self.assertEqual(pend[0]["volume"], 0.01)                          # 不越加越大：≤ 最小那張加碼單

    def test_never_loosen_core_or_adds(self):
        sn = snap(REAL, 3712.0)
        p = self.plan(sn)
        for a in p["actions"]:
            if a["op"] == "MOD":
                self.assertGreater(a["to"], a["from"])
        self.assertNotIn(1, [a["ticket"] for a in p["actions"] if a["op"] == "MOD"])   # 核心單不動

    def test_lock_line_raises_add_stops(self):
        ps = [pos(1, 0.04, 3447.56, 3582.28), pos(2, 0.02, 3676.83, 3646.0)]
        p = self.plan(snap(ps, 3800.0))
        mods = [a for a in p["actions"] if a["op"] == "MOD"]
        self.assertEqual(len(mods), 1)
        self.assertEqual(mods[0]["ticket"], 2)
        self.assertEqual(mods[0]["why"], "保住浮盈")
        # 改完之後再算一次：保住的錢夠了
        ps2 = [pos(1, 0.04, 3447.56, 3582.28), pos(2, 0.02, 3676.83, mods[0]["to"])]
        p2 = self.plan(snap(ps2, 3800.0))
        self.assertEqual([a for a in p2["actions"] if a["op"] == "MOD"], [])
        self.assertTrue(any(a["op"] == "PEND" for a in p2["actions"]) or p2["notes"], p2)

    def test_lock_line_too_close_only_notes(self):
        ps = [pos(1, 0.04, 3447.56, 3582.28), pos(2, 0.02, 3676.83, 3646.0)]
        p = self.plan(snap(ps, 3800.0, stops_level=15000))                 # 券商要求停損離現價 150 點(停損線約 3659.5)
        self.assertEqual([a for a in p["actions"] if a["op"] == "MOD"], [])
        self.assertTrue(any("太靠近現價" in n for n in p["notes"]), p["notes"])

    def test_manual_pending_pauses_auto_add(self):
        p = self.plan(snap(REAL, 3712.0, orders=[order(90, 3740.0, magic=0)]))
        self.assertEqual([a for a in p["actions"] if a["op"] in ("PEND", "DEL")], [])
        self.assertTrue(any("手動掛單" in n for n in p["notes"]))
        self.assertTrue(any(a["op"] == "MOD" for a in p["actions"]))       # 停損照樣收緊

    def test_existing_own_order_same_kept_different_replaced(self):
        same = order(91, 3736.4, v=0.01, sl=3706.4)
        p = self.plan(snap(REAL, 3712.0, orders=[same]))
        self.assertEqual([a for a in p["actions"] if a["op"] in ("PEND", "DEL")], [])
        old = order(92, 3720.0, v=0.01, sl=3690.0)
        p = self.plan(snap(REAL, 3712.0, orders=[old]))
        ops = [(a["op"], a.get("ticket")) for a in p["actions"] if a["op"] in ("PEND", "DEL")]
        self.assertEqual(ops, [("DEL", 92)])                                # 先刪，下一輪才掛新的

    def test_over_leverage_no_pending(self):
        ps = [pos(1, 0.05, 3447.56, 3600.0), pos(2, 0.05, 3621.87, 3640.0)]
        p = self.plan(snap(ps, 3662.79, balance=300.01, adj=-13.28))
        self.assertEqual([a for a in p["actions"] if a["op"] == "PEND"], [])
        self.assertTrue(any("槓桿超過上限" in n for n in p["notes"]), p["notes"])

    def test_missing_sl_or_mixed_blocks_everything(self):
        p = self.plan(snap([pos(1, 0.04, 3447.56, 0)], 3712.0))
        self.assertEqual(p["actions"], [])
        self.assertTrue(any("沒有停損" in b for b in p["blocked"]))
        p = self.plan(snap([pos(1, 0.04, 3447.56, 3600.0), pos(2, 0.01, 3750.0, 3770.0, typ=1)], 3712.0))
        self.assertEqual(p["actions"], [])
        self.assertTrue(len(p["blocked"]) > 0)

    def test_passed_grid_levels_are_skipped(self):
        ps = [pos(1, 0.04, 3447.56, 3582.28), pos(2, 0.01, 3676.83, 3650.0)]
        p = self.plan(snap(ps, 3712.0))                                     # 3706.83 那格已經走過
        pend = [a for a in p["actions"] if a["op"] == "PEND"]
        self.assertEqual(len(pend), 1)
        self.assertEqual(pend[0]["price"], 3736.83)                         # 取現價外面最近的一格
        self.assertEqual(pend[0]["sl"], 3706.83)

    def test_grid_level_too_close_goes_one_further(self):
        ps = [pos(1, 0.04, 3447.56, 3582.28), pos(2, 0.01, 3676.83, 3650.0)]
        p = self.plan(snap(ps, 3736.62))                                    # ask 3736.82，離 3736.83 只差 0.01
        pend = [a for a in p["actions"] if a["op"] == "PEND"]
        self.assertEqual(len(pend), 1)
        self.assertEqual(pend[0]["price"], 3766.83)

    def test_max_auto_lots_caps(self):
        rules = {**RULES, "max_leverage": 50, "lock_pct": 0, "add_lots": 1}
        ps = [pos(1, 0.04, 3447.56, 3640.0)]                                # 只有核心單、停損很緊、規則很寬
        with mock.patch.dict(os.environ, {"ROLL_EXEC_MAX_LOTS": "0.02"}):
            p = self.plan(snap(ps, 3712.0, balance=5000), rules)
        pend = [a for a in p["actions"] if a["op"] == "PEND"]
        self.assertEqual(len(pend), 1)
        self.assertEqual(pend[0]["volume"], 0.02)

    def test_short_mirror(self):
        ps = [pos(1, 0.04, 3900.0, 3800.0, typ=1), pos(2, 0.02, 3700.0, 3740.0, typ=1),
              pos(3, 0.01, 3670.0, 3700.0, typ=1)]
        p = self.plan(snap(ps, 3660.0))
        mods = [a for a in p["actions"] if a["op"] == "MOD"]
        self.assertEqual(len(mods), 1)
        self.assertEqual((mods[0]["ticket"], mods[0]["to"]), (2, 3700.0))
        pend = [a for a in p["actions"] if a["op"] == "PEND"]
        self.assertEqual(len(pend), 1)
        self.assertEqual((pend[0]["type"], pend[0]["price"], pend[0]["sl"]), ("SELL_STOP", 3640.0, 3670.0))


class Handle(Base):
    def run_handle(self, sn, mode, now=T0):
        sent = []
        with mock.patch.object(E, "get_mode", return_value=mode):
            v = self.view(sn, now)
            body = E.handle(v, sn, RULES, sent.append, "L", now=now)
        return body, sent

    def test_off(self):
        body, sent = self.run_handle(snap(REAL, 3712.0), "off")
        self.assertEqual(body, "OK|off")
        self.assertEqual(sent, [])

    def test_dry_alerts_once_no_commands(self):
        body, sent = self.run_handle(snap(REAL, 3712.0), "dry")
        self.assertEqual(body, "OK|dry")
        self.assertEqual(len([m for m in sent if "試跑" in m]), 2)
        body, sent2 = self.run_handle(snap(REAL, 3712.0), "dry", now=T0 + 3)
        self.assertEqual(body, "OK|dry")
        self.assertEqual(sent2, [])                                        # 同樣的事不重發

    def test_live_needs_ea_key(self):
        body, sent = self.run_handle(snap(REAL, 3712.0, allow=False), "live")
        self.assertEqual(body, "OK|live")
        self.assertEqual(len([m for m in sent if "AllowTrading" in m]), 1)

    def test_live_commands_inflight_and_results(self):
        body, _ = self.run_handle(snap(REAL, 3712.0), "live")
        lines = body.split("\n")
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0], "OK|live")
        self.assertTrue(re.match(r"^MOD\|\w+\|2\|3676\.0$", lines[1]), lines)
        self.assertTrue(re.match(r"^PEND\|\w+\|BUY_STOP\|3736\.4\|0\.01\|3706\.4$", lines[2]), lines)
        body2, _ = self.run_handle(snap(REAL, 3712.0), "live", now=T0 + 3)
        self.assertEqual(body2, "OK|live")                                 # 送出中，不重送
        body3, _ = self.run_handle(snap(REAL, 3712.0), "live", now=T0 + E.INFLIGHT_SECONDS + 5)
        self.assertEqual(len(body3.split("\n")), 3)                        # 逾時沒回報才重送
        mod_id = lines[1].split("|")[1]
        pend_id = lines[2].split("|")[1]
        res = [{"id": mod_id, "ok": True, "retcode": 10009, "msg": "#2 停損 3646→3676 done"},
               {"id": pend_id, "ok": False, "retcode": 10027, "msg": "autotrading disabled"}]
        now = T0 + E.INFLIGHT_SECONDS + 8
        body4, sent = self.run_handle(snap(REAL, 3712.0, results=res), "live", now=now)
        self.assertTrue(any("✅ 已執行" in m and "#2 停損" in m for m in sent), sent)
        self.assertTrue(any("🚨 執行失敗" in m and "10027" in m for m in sent), sent)
        self.assertNotIn("PEND", body4)                                     # 失敗的先等 10 分鐘
        body5, _ = self.run_handle(snap(REAL, 3712.0), "live", now=now + E.RETRY_SECONDS + 1)
        self.assertIn("PEND", body5)
        self.assertEqual(len(E.status()["results"]), 2)


class ModeAndEndpoints(Base):
    def test_set_mode(self):
        saved = {}
        with mock.patch("app.db.save_app_settings", side_effect=lambda d: saved.update(d)):
            self.assertEqual(E.set_mode("live"), "live")
            with self.assertRaises(ValueError):
                E.set_mode("yolo")
        self.assertEqual(saved[E.MODE_DB_KEY], "live")
        self.assertEqual(E.get_mode(), "live")
        E.reset()
        with mock.patch("app.db.get_app_settings", return_value={}):
            self.assertEqual(E.get_mode(), "dry")                          # 沒設過＝試跑

    def test_endpoints(self):
        from fastapi.testclient import TestClient
        from app.main import app
        c = TestClient(app)
        with mock.patch.object(E, "get_mode", return_value="live"):
            r = c.post("/tools/roll-sync", json=snap(REAL, 3712.0), headers={"X-Token": TOKEN, "X-Proto": "2"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.text.startswith("OK|live\nMOD|"), r.text)
        r = c.post("/tools/roll-sync", json=snap(REAL, 3712.0), headers={"X-Token": TOKEN})
        self.assertEqual(r.json(), {"ok": True})                           # v1 EA 照舊
        with mock.patch.dict(os.environ, {"SETTINGS_PASSWORD": "pw-123"}), \
                mock.patch("app.db.save_app_settings"), mock.patch("app.db.insert_settings_audit"):
            self.assertIs(c.post("/tools/roll-exec-mode", json={"password": "x", "mode": "live"}).json()["success"], False)
            r = c.post("/tools/roll-exec-mode", json={"password": "pw-123", "mode": "dry"}).json()
            self.assertEqual(r["mode"], "dry")


class EaFile(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(ROOT, "windows", "TzujenRollSync.mq5"), encoding="utf-8") as f:
            self.src = f.read()

    def test_safety_defaults_and_checks(self):
        self.assertIn("input bool   AllowTrading = false;", self.src)
        self.assertIn("X-Proto: 2", self.src)
        self.assertIn("if(!AllowTrading)", self.src)
        self.assertIn("(type == POSITION_TYPE_BUY) ? (sl > cur)", self.src)     # 停損只准收緊
        self.assertIn("OrderGetInteger(ORDER_MAGIC) != RollMagic", self.src)    # 只刪自己的單
        self.assertIn("vol > MaxLots", self.src)
        self.assertIn(f"RollMagic    = {E.ROLL_MAGIC};", self.src)
        for banned in ("PositionClose", "Buy(", "Sell(", "OrderSend("):
            self.assertNotIn(banned, self.src)                                # 不會市價開平倉


if __name__ == "__main__":
    unittest.main()
