"""
r124 的測試：滾倉計算器(app/position_roll.py、/tools/roll*)。
執行：python -m unittest tests.test_position_roll -v
"""
import logging
import os
import unittest

logging.disable(logging.CRITICAL)

from app import position_roll as R  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 2026-11-27 使用者 MT5 截圖：兩筆 XAUUSD 多單各 0.05 手，結餘 300.01、淨值 1567.48
SHOT = {"direction": "long", "positions": [{"lots": 0.05, "entry": 3447.56}, {"lots": 0.05, "entry": 3621.87}],
        "price": 3662.79, "balance": 300.01, "equity": 1567.48}


def inp(**kw):
    d = {**SHOT, "positions": [dict(p) for p in SHOT["positions"]]}
    d.update(kw)
    return d


def levels(r, level):
    return [a["text"] for a in r["actions"] if a["level"] == level]


class Status(unittest.TestCase):
    def test_screenshot_numbers(self):
        r = R.calc(inp())
        s = r["status"]
        self.assertTrue(r["success"])
        self.assertEqual(s["lots"], 0.1)
        self.assertAlmostEqual(s["avg_price"], 3534.72, places=1)
        self.assertAlmostEqual(s["floating"], 1267.47, places=2)       # 淨值 − 結餘，含隔夜費差額
        self.assertAlmostEqual(s["adjustment"], -13.28, places=2)      # 1076.15 + 204.60 = 1280.75，差 13.28
        self.assertEqual(s["per_point"], 10.0)
        self.assertAlmostEqual(s["leverage"], 23.37, places=2)
        self.assertAlmostEqual(s["zero_price"], 3506.04, places=2)
        self.assertAlmostEqual(r["stop"]["lock_line"], 3599.42, places=2)
        self.assertAlmostEqual(r["stop"]["keep_required"], 633.74, places=2)

    def test_without_equity_uses_price(self):
        r = R.calc(inp(equity=None))
        self.assertAlmostEqual(r["status"]["floating"], 1280.75, places=2)
        self.assertEqual(r["status"]["adjustment"], 0)

    def test_leverage_cap_price_is_where_leverage_equals_cap(self):
        p = R.parse(inp())
        book = R.Book(p["s"], p["cs"], p["balance"], p["positions"])
        cp = book.leverage_cap_price(10)
        self.assertTrue(cp is not None and cp > p["price"])            # 多單：要漲上去槓桿才會降
        self.assertAlmostEqual(book.leverage_at(cp), 10, places=6)


class Warnings(unittest.TestCase):
    def test_no_stop_and_over_leverage_are_alarms(self):
        r = R.calc(inp())
        alarms = levels(r, "alarm")
        self.assertTrue(any("沒有停損" in t and "3506.04" in t for t in alarms), alarms)
        self.assertTrue(any("超過上限" in t for t in alarms), alarms)
        self.assertEqual(r["add_now"]["lots"], 0.0)
        self.assertIs(r["add_now"]["stop_is_lock_line"], True)

    def test_reduce_closes_worst_entry_first_and_fits_cap(self):
        r = R.calc(inp(stop=3620))
        rd = r["reduce"]
        self.assertEqual(rd["close_lots"], 0.06)
        self.assertEqual(len(rd["plan"]), 2)
        self.assertEqual(rd["plan"][0]["entry"], 3621.87)               # 多單：進場價最高(成本最差)的先平
        self.assertEqual(rd["plan"][0]["close_lots"], 0.05)
        self.assertLessEqual(rd["after"]["leverage"], 10)
        # 打到停損保住的錢跟原本結餘比：平掉那幾筆實現的獲利也算
        p = R.parse(inp(stop=3620))
        adj = r["status"]["adjustment"]
        expect = 300.01 + adj + 100 * 0.04 * (3620 - 3447.56) + sum(x["realized"] for x in rd["plan"]) - 300.01
        self.assertAlmostEqual(rd["after"]["kept_at_stop"], expect, places=1)
        self.assertEqual(p["stop"], 3620)

    def test_stop_below_lock_line_warns(self):
        r = R.calc(inp(stop=3560, max_leverage=30))
        self.assertTrue(any("只保住" in t and "3599.42" in t for t in levels(r, "warn")), r["actions"])
        self.assertIs(r["stop"]["locks_enough"], False)

    def test_stop_losing_principal_is_alarm(self):
        r = R.calc(inp(stop=3510, max_leverage=30))
        self.assertTrue(any("虧到本金" in t for t in levels(r, "alarm")), r["actions"])

    def test_stop_on_wrong_side_rejected(self):
        with self.assertRaises(ValueError):
            R.calc(inp(stop=3700))
        with self.assertRaises(ValueError):
            R.calc(inp(direction="short", stop=3600))

    def test_bad_inputs_rejected(self):
        for bad in (inp(positions=[]), inp(price="abc"), inp(balance=0), inp(lock_pct=120), inp(max_leverage=1)):
            with self.assertRaises(ValueError):
                R.calc(bad)

    def test_no_profit_means_no_add(self):
        r = R.calc(inp(price=3500, equity=None, stop=3480, max_leverage=100))
        self.assertEqual(r["add_now"]["lots"], 0.0)
        self.assertIn("沒有浮盈", r["add_now"]["why"])
        self.assertTrue(any("沒有浮盈" in t for t in levels(r, "info")))


class Adding(unittest.TestCase):
    def _check_limits(self, book, price, stop, keep, cap, cost, lots):
        """手數 lots 守住三條；再多一個最小單位就會破其中一條(槓桿／保住浮盈)或超過計畫。"""
        def ok(x):
            b2 = R.Book(book.s, book.cs, book.balance, book.positions + [{"lots": x, "entry": price + book.s * cost}],
                        book.adj)
            return b2.leverage_at(price) <= cap + 1e-9 and b2.kept_at(stop) >= keep - 1e-6
        return ok(lots), ok(lots + 0.01)

    def test_add_now_is_max_lots_under_rules(self):
        q = inp(stop=3640, max_leverage=50, add_lots=1.0)
        r = R.calc(q)
        a = r["add_now"]
        self.assertGreater(a["lots"], 0)
        self.assertTrue(any("現在可以加" in t for t in levels(r, "ok")), r["actions"])
        p = R.parse(q)
        book = R.Book(p["s"], p["cs"], p["balance"], p["positions"], r["status"]["adjustment"])
        fits, one_more = self._check_limits(book, p["price"], 3640, r["stop"]["keep_required"], 50, p["cost"], a["lots"])
        self.assertIs(fits, True)
        self.assertIs(one_more, False)
        self.assertGreaterEqual(a["kept_at_stop"], r["stop"]["keep_required"] - 0.01)

    def test_plan_lots_caps_add(self):
        r = R.calc(inp(stop=3640, max_leverage=50, add_lots=0.02))
        self.assertEqual(r["add_now"]["lots"], 0.02)
        self.assertEqual(r["add_now"]["why"], "計畫手數")

    def test_ladder_trails_stop_and_never_grows(self):
        r = R.calc(inp(stop=3620, max_leverage=25, add_step=30, ladder_steps=4))
        lad = r["ladder"]
        self.assertEqual(len(lad), 4)
        self.assertEqual([x["price"] for x in lad], [3692.79, 3722.79, 3752.79, 3782.79])
        self.assertEqual([x["stop"] for x in lad], [3650.0, 3680.0, 3710.0, 3740.0])
        adds = [x["add_lots"] for x in lad if x["add_lots"] > 0]
        self.assertTrue(len(adds) >= 2, lad)
        self.assertTrue(all(b <= a + 1e-9 for a, b in zip(adds, adds[1:])), adds)
        for x in lad:
            if x["add_lots"] > 0:
                self.assertLessEqual(x["leverage"], 25 + 1e-6)
                self.assertGreaterEqual(x["kept_at_stop"], x["keep_required"] - 0.01)

    def test_ladder_without_trail_keeps_stop(self):
        r = R.calc(inp(stop=3620, max_leverage=25, trail_stop=False))
        self.assertEqual({x["stop"] for x in r["ladder"]}, {3620.0})

    def test_short_mirrors_long(self):
        q = {"direction": "short", "positions": [{"lots": 0.05, "entry": 3700}, {"lots": 0.05, "entry": 3650}],
             "price": 3600, "balance": 500, "stop": 3630, "max_leverage": 40, "add_lots": 1}
        r = R.calc(q)
        s = r["status"]
        self.assertEqual(len(r["ladder"]), 3)
        self.assertAlmostEqual(s["floating"], 750.0, places=2)
        self.assertAlmostEqual(s["zero_price"], 3675 + 50, places=2)     # 空單：漲到均價 + 結餘/每點
        self.assertGreater(s["zero_price"], 3600)
        self.assertLess(r["ladder"][0]["price"], 3600)                    # 空單往下加碼
        self.assertLess(r["ladder"][0]["stop"], 3630)                     # 停損往下移
        self.assertGreater(r["add_now"]["lots"], 0)
        self.assertGreaterEqual(r["add_now"]["kept_at_stop"], r["stop"]["keep_required"] - 0.01)


class Levels(unittest.TestCase):
    CHAN = {"15分K": {"bi_list": [{"start_price": 3650, "end_price": 3630}, {"start_price": 3630, "end_price": 3670},
                                  {"start_price": 3670, "end_price": 3645}],
                      "latest_zhongshu": {"zd": 3632, "zg": 3655}},
            "1小時K": {"bi_list": [{"start_price": 3600, "end_price": 3680}], "latest_zhongshu": None}}

    def test_long_levels_below_price_nearest_first(self):
        out = R.structure_levels("long", self.CHAN, {"zones_below": [{"kind": "OB", "bot": 3580, "top": 3590}]}, 3660)
        self.assertEqual(len(out), 6)
        prices = [x["price"] for x in out]
        self.assertEqual(prices, [3655.0, 3645.0, 3632.0, 3630.0, 3600.0, 3580.0])  # 中樞上緣 3655 也在現價下方
        self.assertIn("中樞上緣", out[0]["source"])
        self.assertIn("筆低點", out[1]["source"])                         # 筆取低端(3670→3645 取 3645)
        self.assertIn("日線", out[-1]["source"])

    def test_short_levels_above_price(self):
        out = R.structure_levels("short", self.CHAN, None, 3640)
        prices = [x["price"] for x in out]
        self.assertTrue(len(prices) > 0 and all(p > 3640 for p in prices), prices)
        self.assertIn(3650.0, prices)

    def test_same_price_merged(self):
        chan = {"15分K": {"bi_list": [{"start_price": 3630, "end_price": 3650}], "latest_zhongshu": {"zd": 3630, "zg": 3640}}}
        out = R.structure_levels("long", chan, None, 3660)
        self.assertEqual(len(out), 2)
        self.assertIn("中樞下緣", next(x["source"] for x in out if x["price"] == 3630.0))


class Endpoints(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        from app.main import app
        self.c = TestClient(app)

    def test_calc_endpoint_and_error(self):
        body = inp(price="3662.79", positions=[{"lots": "0.05", "entry": "3447.56"}, {"lots": "0.05", "entry": "3621.87"},
                                               {"lots": "", "entry": ""}])
        r = self.c.post("/tools/roll-calc", json=body).json()
        self.assertIs(r["success"], True)
        self.assertEqual(r["status"]["lots"], 0.1)
        r = self.c.post("/tools/roll-calc", json=inp(stop=3700)).json()
        self.assertIs(r["success"], False)
        self.assertIn("停損", r["error"])

    def test_page_and_dashboard_link(self):
        r = self.c.get("/tools/roll")
        self.assertEqual(r.status_code, 200)
        self.assertIn("/tools/roll-calc", r.text)
        with open(os.path.join(ROOT, "app", "static", "dashboard.html"), encoding="utf-8") as f:
            self.assertIn('href="/tools/roll"', f.read())

    def test_levels_endpoint(self):
        from unittest import mock
        from app import main as M
        with mock.patch.object(M.binance_streamer, "get_latest", return_value=None):
            r = self.c.get("/tools/roll-levels?direction=long").json()
        self.assertIn("幣安報價還沒到", r["error"])
        snap = {"zones_below": [{"kind": "FVG", "bot": 3600.0, "top": 3610.0}], "zones_above": []}
        with mock.patch.object(M.binance_streamer, "get_latest", return_value={"bid": 3659.9, "ask": 3660.1}), \
                mock.patch.object(M.binance_streamer, "get_recent_candles", return_value=[]), \
                mock.patch.object(M.daily_smc, "get_snapshot", return_value=snap):
            r = self.c.get("/tools/roll-levels?direction=long").json()
        self.assertEqual(r["binance_price"], 3660.0)
        self.assertEqual(r["levels"], [{"price": 3600.0, "source": "日線 FVG"}])


if __name__ == "__main__":
    unittest.main()
