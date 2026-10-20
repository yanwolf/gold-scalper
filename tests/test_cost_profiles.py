"""
r111 的測試：MT5 券商成本重算(app/cost_profiles.py)——每筆點差＋佣金、隔夜庫存費(紐約 17:00 換日、週三 3 倍)。
執行：python -m unittest tests.test_cost_profiles -v
"""
import logging
import os
import unittest
from datetime import datetime
from unittest import mock

logging.disable(logging.CRITICAL)

from app import cost_profiles as C  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def ny(s):
    """紐約當地時間字串 → ISO(帶時區)。"""
    return datetime.fromisoformat(s).replace(tzinfo=C.NY).isoformat()


class Rollovers(unittest.TestCase):
    def test_same_day_before_rollover(self):
        self.assertEqual(C.rollovers(ny("2026-10-20T10:00"), ny("2026-10-20T16:59")), 0)
        self.assertEqual(C.rollovers(ny("2026-10-20T10:00"), ny("2026-10-20T17:01")), 1, "對照：過了 17:00 就算一次")

    def test_crosses_one(self):
        self.assertEqual(C.rollovers(ny("2026-10-20T16:00"), ny("2026-10-20T18:00")), 1)   # 週二

    def test_wednesday_triple(self):
        self.assertEqual(C.rollovers(ny("2026-10-21T16:00"), ny("2026-10-21T18:00")), 3)   # 週三

    def test_exit_exactly_at_rollover_does_not_count(self):
        self.assertEqual(C.rollovers(ny("2026-10-20T10:00"), ny("2026-10-20T17:00")), 0)
        self.assertEqual(C.rollovers(ny("2026-10-20T10:00"), ny("2026-10-20T17:00:01")), 1, "對照：過一秒就算")

    def test_weekend_only_friday_counts(self):
        self.assertEqual(C.rollovers(ny("2026-10-23T16:00"), ny("2026-10-26T10:00")), 1)   # 週五→週一

    def test_full_week_is_seven(self):
        self.assertEqual(C.rollovers(ny("2026-10-19T10:00"), ny("2026-10-26T10:00")), 7)

    def test_dst_uses_new_york_time(self):
        # 夏令(EDT, UTC-4)：換日是 21:00 UTC；冬令(EST, UTC-5)：22:00 UTC
        self.assertEqual(C.rollovers("2026-07-14T20:30:00+00:00", "2026-07-14T21:30:00+00:00"), 1)
        self.assertEqual(C.rollovers("2026-12-15T21:30:00+00:00", "2026-12-15T21:59:00+00:00"), 0)
        self.assertEqual(C.rollovers("2026-12-15T21:30:00+00:00", "2026-12-15T22:30:00+00:00"), 1)

    def test_bad_or_reversed_times(self):
        self.assertEqual(C.rollovers("x", ny("2026-10-20T18:00")), 0)
        self.assertEqual(C.rollovers(ny("2026-10-20T18:00"), ny("2026-10-20T10:00")), 0)
        self.assertEqual(C.rollovers("2026-10-20T20:00:00", "2026-10-20T22:30:00"), 1, "沒帶時區當成 UTC")


class Adjust(unittest.TestCase):
    T = [{"direction": "bullish", "pnl_points": 5.0, "entry_time": ny("2026-10-20T16:00"), "exit_time": ny("2026-10-20T18:00")},
         {"direction": "bearish", "pnl_points": -2.0, "entry_time": ny("2026-10-21T16:00"), "exit_time": ny("2026-10-21T18:00")},
         {"direction": "bullish", "pnl_points": None, "entry_time": ny("2026-10-20T10:00"), "exit_time": ny("2026-10-20T11:00")}]

    def test_swap_signs(self):
        self.assertEqual(len(self.T), 3, "前提：三筆樣本")
        self.assertAlmostEqual(C.swap_points(self.T[0]), C.MT5["swap_long"])
        self.assertAlmostEqual(C.swap_points(self.T[1]), 3 * C.MT5["swap_short"])
        self.assertLess(C.MT5["swap_long"], 0)
        self.assertGreater(C.MT5["swap_short"], 0)

    def test_adjust_subtracts_cost_and_adds_swap(self):
        self.assertEqual(len(self.T), 3, "前提：三筆樣本")
        out, d = C.adjust(self.T)
        self.assertEqual(len(out), 3)
        self.assertAlmostEqual(out[0]["pnl_points"], 5.0 - 0.4 + C.MT5["swap_long"])
        self.assertAlmostEqual(out[1]["pnl_points"], -2.0 - 0.4 + 3 * C.MT5["swap_short"])
        self.assertIsNone(out[2]["pnl_points"], "損益未知的照原樣")
        self.assertEqual((d["trades"], d["nights"], d["cost_total"]), (2, 4, 0.8))
        self.assertAlmostEqual(self.T[0]["pnl_points"], 5.0, msg="不改原本的交易")

    def test_cost_override(self):
        out, d = C.adjust(self.T, cost_points=0.6)
        self.assertEqual(d["cost_points"], 0.6)
        self.assertEqual(len(out), 3, "前提：三筆都在")
        self.assertAlmostEqual(out[0]["pnl_points"], 5.0 - 0.6 + C.MT5["swap_long"])

    def test_view(self):
        v = C.view(self.T)
        self.assertEqual(v["key"], "mt5")
        self.assertEqual(v["total_trades"], 3)
        expected = (5.0 - 0.4 + C.MT5["swap_long"]) + (-2.0 - 0.4 + 3 * C.MT5["swap_short"])
        self.assertAlmostEqual(v["total_pnl_points"], round(expected, 2), places=2)


class Wiring(unittest.TestCase):
    def test_backtest_result_has_mt5_view(self):
        from tests.test_pullback_resume import _run, BAND_PATH
        res = _run(path=BAND_PATH, pr_mode=2, pr_exit_mode=1, pr_sides=1)
        self.assertGreater(res["total_trades"], 0, "前提：有交易")
        v = res["mt5_view"]
        self.assertEqual((v["cost_points"], v["trades"]), (0.4, res["total_trades"]))
        res2 = _run(path=BAND_PATH, pr_mode=2, pr_exit_mode=1, pr_sides=1, mt5_cost_points=0.6)
        self.assertEqual(res2["mt5_view"]["cost_points"], 0.6)
        self.assertAlmostEqual(res["mt5_view"]["total_pnl_points"] - res2["mt5_view"]["total_pnl_points"],
                               0.2 * res["total_trades"], places=2)

    def test_job_rows_and_aggregate(self):
        from app import backtest_jobs as J
        trades = [{"entry_time": ny("2026-10-20T16:00"), "exit_time": ny("2026-10-20T18:00"),
                   "pnl_points": 5.0, "direction": "bullish"}]
        res = {"total_trades": 1, "recent_trades": trades, "mt5_view": C.view(trades)}
        row = J._window_row(0, res, 0, 1)
        self.assertEqual((row["mt5_cost_points"], row["mt5_net_pnl_points"]), (0.4, res["mt5_view"]["total_pnl_points"]))
        ag = J._aggregate([res])
        self.assertAlmostEqual(ag["mt5_net_pnl_points"], 5.0 - 0.4 + C.MT5["swap_long"], places=2)
        self.assertIn("mt5_cost_points", J.LABEL_PARAM_KEYS)
        from app import main
        self.assertIn("mt5_cost_points", main._backtest_job_params())

    def test_engine_summary_has_mt5_view(self):
        from app import paper_trading as pt
        eng = pt.PaperTradingEngine(interval_seconds=900, label="t", engine_id="mt5_view_test", execution_index=97)
        eng._closed_trades_memory.append({"direction": "bullish", "pnl_points": 3.0, "entry_price": 4000.0,
                                          "entry_time": ny("2026-10-20T10:00"), "exit_time": ny("2026-10-20T11:00"),
                                          "exit_price": 4003.0, "exit_reason": "x"})
        with mock.patch.object(pt.db, "is_enabled", return_value=False), \
             mock.patch.object(pt.settings_module, "get_last_changed_at", return_value=None):
            s = eng.get_summary()
        self.assertEqual(s["total_trades"], 1, "前提：讀到那一筆")
        self.assertAlmostEqual(s["mt5_view"]["total_pnl_points"], 2.6)

    def test_dashboard(self):
        with open(os.path.join(ROOT, "app/static/dashboard.html"), encoding="utf-8") as f:
            h = f.read()
        self.assertIn("mt5ViewHtml(data.mt5_view)", h)
        self.assertIn("addIfPresent('mt5_cost_points'", h)
        self.assertIn("r.mt5_net_pnl_points", h)


if __name__ == "__main__":
    unittest.main()
