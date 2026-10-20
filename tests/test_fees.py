"""
r103 的測試：真實下單的實際手續費(成交明細 commission)記錄、扣手續費後的淨損益。
執行：python -m unittest tests.test_fees -v
"""
import logging
import os
import unittest
from unittest import mock

logging.disable(logging.CRITICAL)

from app import db  # noqa: E402
from app import execution as ex  # noqa: E402
from app import notifier as notifier_module  # noqa: E402
from app import paper_trading as pt  # noqa: E402
from app import trading_stats as ts  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def row(order_id, qty, fee, asset="USDT", price=4170.0, tid=1, side="BUY"):
    return {"id": tid, "orderId": order_id, "qty": str(qty), "price": str(price), "side": side,
            "commission": str(fee), "commissionAsset": asset, "positionSide": "BOTH"}


class Commission(unittest.TestCase):
    def test_sums_usdt(self):
        self.assertAlmostEqual(ex.commission_usdt([row(1, 0.5, "0.8"), row(1, 0.5, "-0.87")]), 1.67)

    def test_unknown_cases(self):
        self.assertEqual(ex.commission_usdt([row(1, 1, "0.5")]), 0.5, "對照：全用 USDT 付就算得出來")
        self.assertIsNone(ex.commission_usdt([]))
        self.assertIsNone(ex.commission_usdt([row(1, 1, "0.5", asset="BNB")]), "BNB 抵扣：不能當成 0")
        self.assertIsNone(ex.commission_usdt([row(1, 1, "0.5"), row(1, 1, "0.1", asset="BNB")]), "一部分不知道就是不知道")
        self.assertIsNone(ex.commission_usdt([{"commissionAsset": "USDT", "commission": None}]))


class DbRows(unittest.TestCase):
    def test_with_fees(self):
        t = db._with_fees({"real_pnl_usd": 8.37, "entry_fee_usd": 1.67, "exit_fee_usd": 1.67})
        self.assertEqual((t["fee_usd"], t["real_net_usd"]), (3.34, 5.03))
        t2 = db._with_fees({"real_pnl_usd": 8.37, "entry_fee_usd": 1.67, "exit_fee_usd": None})
        self.assertEqual((t2["fee_usd"], t2["real_net_usd"]), (None, None))
        t3 = db._with_fees({"real_pnl_usd": None, "entry_fee_usd": 1.67, "exit_fee_usd": 1.67})
        self.assertEqual((t3["fee_usd"], t3["real_net_usd"]), (3.34, None), "估算的 USDT 不跟真實手續費混")

    def test_sql_roundtrip(self):
        from tests.test_lessons import _sqlite_pool
        pool, conn = _sqlite_pool()
        conn.execute("INSERT INTO paper_trades (id, status, engine_id, direction, entry_price, entry_time, exit_price, exit_time, "
                     "pnl_points, entry_actual_price, exit_actual_price, real_open_executed, real_open_quantity) VALUES "
                     "(9,'closed','e','bullish',4175.15,'2026-10-16T03:40:29+00:00',4183.53,'2026-10-16T04:36:25+00:00',"
                     "8.38,4175.16,4183.53,1,1.0)")
        with mock.patch.object(db, "_enabled", True), mock.patch.object(db, "_pool", pool, create=True):
            db.update_paper_trade_fees(9, exit_fee=1.67)
            db.update_paper_trade_fees(9, entry_fee=1.67)                       # 只寫進場：出場那欄不能被清掉
            db.update_paper_trade_fees(9, entry_fee=None, exit_fee=None)       # 不動
            rows = db.get_closed_paper_trades(limit=5, engine_id="e")
        self.assertEqual(len(rows), 1, "前提：讀得到")
        r = rows[0]
        self.assertEqual((r["entry_fee_usd"], r["exit_fee_usd"], r["fee_usd"]), (1.67, 1.67, 3.34))
        self.assertEqual((r["real_pnl_usd"], r["real_net_usd"]), (8.37, 5.03), "截圖那筆：+8.37 實、扣手續費 +5.03")


class Summary(unittest.TestCase):
    def test_fee_totals_and_net(self):
        trades = [{"real_open_executed": True, "real_open_quantity": 1.0, "real_pnl_usd": 17.29, "fee_usd": 3.34},
                  {"real_open_executed": True, "real_open_quantity": 1.0, "real_pnl_usd": 8.37, "fee_usd": 3.34},
                  {"real_open_executed": False, "pnl_points": 6.23}]
        s = ts.real_usd_summary(trades)
        self.assertEqual((s["fee_total"], s["fee_known"], s["fee_unknown"], s["net_total"]), (6.68, 2, 0, 18.98))
        self.assertEqual(s["fee_per_unit_round_trip"], 3.34)

    def test_net_hidden_when_any_fee_unknown(self):
        trades = [{"real_open_executed": True, "real_open_quantity": 1.0, "real_pnl_usd": 17.29, "fee_usd": 3.34},
                  {"real_open_executed": True, "real_open_quantity": 1.0, "real_pnl_usd": 8.37, "fee_usd": None}]
        s = ts.real_usd_summary(trades)
        self.assertEqual((s["fee_total"], s["fee_unknown"], s["net_total"]), (3.34, 1, None))


class EngineFees(unittest.TestCase):
    def setUp(self):
        self.eng = pt.PaperTradingEngine(interval_seconds=900, label="測試", engine_id="fee_test", execution_index=98)
        self.threads = []
        self.writes = []
        self.p = [mock.patch.object(pt, "_start_fee_thread", side_effect=self.threads.append),
                  mock.patch.object(pt.db, "update_paper_trade_fees",
                                    side_effect=lambda tid, entry_fee=None, exit_fee=None: self.writes.append((tid, entry_fee, exit_fee)))]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()

    def _pos(self, **kw):
        p = {"id": 7, "direction": "bullish", "real_open_executed": True, "real_open_quantity": 1.0,
             "real_open_order_id": 100}
        p.update(kw)
        return p

    def test_both_fees_found(self):
        trades = [row(100, 1.0, "1.67", tid=1), row(200, 1.0, "1.67", tid=2, side="SELL")]
        with mock.patch.object(ex, "get_user_trades", return_value=(True, trades)):
            out = self.eng._close_fees(self._pos(), True, {"orderId": 200}, 1.0)
        self.assertEqual((out["entry"], out["exit"], out["total"], out["pending"]), (1.67, 1.67, 3.34, False))
        self.assertEqual(self.writes, [(7, 1.67, 1.67)])
        self.assertEqual(self.threads, [])

    def test_entry_fee_from_position_is_reused(self):
        with mock.patch.object(ex, "get_user_trades", return_value=(True, [row(200, 1.0, "1.70", side="SELL")])):
            out = self.eng._close_fees(self._pos(entry_fee_usd=1.65), True, {"orderId": 200}, 1.0)
        self.assertEqual(out["total"], 3.35)

    def test_missing_exit_rows_schedules_backfill(self):
        with mock.patch.object(ex, "get_user_trades", return_value=(True, [row(100, 1.0, "1.67")])):
            out = self.eng._close_fees(self._pos(), True, {"orderId": 200}, 1.0)
        self.assertEqual((out["entry"], out["exit"], out["total"], out["pending"]), (1.67, None, None, True))
        self.assertEqual(len(self.threads), 1, "排了背景補查")

    def test_incomplete_rows_are_not_used(self):
        trades = [row(100, 1.0, "1.67"), row(200, 0.4, "0.67", side="SELL")]
        with mock.patch.object(ex, "get_user_trades", return_value=(True, trades)):
            out = self.eng._close_fees(self._pos(), True, {"orderId": 200}, 1.0)
        self.assertEqual(out["entry"], 1.67, "前提：進場那張湊滿了照算")
        self.assertIsNone(out["exit"], "只出現 0.4/1.0 張：不能當成完整的手續費")

    def test_partial_close_and_external_close_are_unknown_without_backfill(self):
        with mock.patch.object(ex, "get_user_trades", return_value=(True, [row(100, 1.0, "1.67")])):
            a = self.eng._close_fees(self._pos(partial_close_filled=True), True, {"orderId": 200}, 1.0)
            b = self.eng._close_fees(self._pos(), None, None, None)
        for out in (a, b):
            self.assertEqual((out["exit"], out["total"], out["pending"]), (None, None, False))
        self.assertEqual(self.threads, [])

    def test_sim_only_does_nothing(self):
        with mock.patch.object(ex, "get_user_trades") as g:
            out = self.eng._close_fees(self._pos(real_open_executed=False), None, None, None)
        self.assertIs(out["real"], False, "標成不是真實單")
        self.assertFalse(g.called)
        self.assertEqual(self.writes, [])

    def test_backfill_finds_later(self):
        seq = iter([(True, []), (True, [row(200, 1.0, "1.67", side="SELL")])])
        ctx = {"trade_id": 7, "open_oid": 100, "open_qty": 1.0, "close_oid": 200, "close_qty": 1.0,
               "entry_fee": 1.67, "exit_fee": None}
        with mock.patch.object(ex, "get_user_trades", side_effect=lambda **k: next(seq)), \
             mock.patch.object(pt.time, "sleep"):
            self.eng._run_fee_backfill(ctx)
        self.assertEqual(self.writes, [(7, None, 1.67)])

    def test_open_records_entry_fee_when_complete(self):
        pos = self._pos(entry_actual_price=4175.16)
        with mock.patch.object(ex, "get_user_trades", return_value=(True, [row(100, 1.0, "1.67")])), \
             mock.patch.object(pt.db, "update_paper_trade_fills"):
            self.eng._record_fill_boundary(pos)
        self.assertEqual(pos["entry_fee_usd"], 1.67)
        pos2 = self._pos(entry_actual_price=4175.16)
        with mock.patch.object(ex, "get_user_trades", return_value=(True, [row(100, 0.3, "0.5")])), \
             mock.patch.object(pt.db, "update_paper_trade_fills"):
            self.eng._record_fill_boundary(pos2)
        self.assertNotIn("entry_fee_usd", pos2, "成交明細還沒湊滿：先不記")

    def test_entry_fee_survives_restart(self):
        self.assertIn("entry_fee_usd", pt.RUNTIME_STATE_KEYS)


class Notification(unittest.TestCase):
    def _text(self, **kw):
        sent = []
        n = notifier_module.notifier
        with mock.patch.object(n, "_send_telegram_message", side_effect=lambda t: sent.append(t) or (True, None)), \
             mock.patch.object(n, "_muted", False), \
             mock.patch.object(ex, "use_testnet", return_value=False), \
             mock.patch.object(ex, "usdt_balance_line", return_value=None, create=True):
            n.notify_trade_event(action="close", label="15分K", direction="bullish", price=4183.53, exit_reason="訊號反轉",
                                 pnl_points=8.38, executed=True, quantity=1.0, real_pnl_usd=8.37, **kw)
        return sent[0]

    def test_net_after_fee(self):
        t = self._text(fee_usd=3.34)
        self.assertIn("手續費：−3.34 USDT，扣除後淨 +5.03 USDT", t)

    def test_pending(self):
        self.assertIn("背景補查中", self._text(fee_usd=None, fee_pending=True))
        self.assertNotIn("手續費", self._text(fee_usd=None, fee_pending=False))


class Wiring(unittest.TestCase):
    def _src(self, rel):
        with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
            return f.read()

    def test_close_passes_fees_to_notification(self):
        s = self._src("app/paper_trading.py")
        self.assertIn('fee_usd=fees["total"], fee_pending=fees["pending"]', s)
        self.assertIn("close_order = result", s)
        self.assertIn("close_order = res_r", s)

    def test_dashboard(self):
        h = self._src("app/static/dashboard.html")
        self.assertIn("t.real_net_usd", h)
        self.assertIn("ru.fee_per_unit_round_trip", h)
        self.assertIn("ru.net_total", h)


if __name__ == "__main__":
    unittest.main()
