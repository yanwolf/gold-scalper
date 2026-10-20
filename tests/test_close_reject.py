"""
r107 的測試：BINANCE_LESSONS.md 第 16 條 r99——交易所一直拒絕平倉(-2022)時，重試要會退讓、要有「停止追蹤」的出口。
三個檢查項目：
  1. 平倉重試把「交易所明確拒絕」和「暫時性錯誤」分開；明確拒絕連續一段時間後退讓、只推一則要人工處理的通知；
     暫時性錯誤不退讓。
  2. 「停止追蹤此部位」：帳上移除但交易所不動、不算進斷路器、這檔不再進場、交易所清空後自動解除、重啟後還在。
  3. 測試用的模擬交易所能回 -2022(另有 -2019 與逾時當對照組)。
執行：python -m unittest tests.test_close_reject -v
"""
import json
import os
import time
import unittest
from unittest import mock

from tests.test_lessons import ExecHarness, _pos, _rows, ex, pt   # noqa: F401
from app import risk_guard  # noqa: E402
from app import trading_core  # noqa: E402
try:
    from app import abandoned  # r107 新模組：舊版程式沒有，測試要回「前提不成立」而不是整個檔案匯入失敗
except ImportError:
    abandoned = None
NEW_API = ("CLOSE_REJECT_STUCK_AFTER", "CLOSE_STUCK_RETRY_SECONDS", "ABANDON_EXIT_REASON", "ABANDONED_RECONCILE_EVERY_TICKS")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REJECT = (False, {"code": -2022, "msg": "ReduceOnly Order is rejected."})
MARGIN = (False, {"code": -2019, "msg": "Margin is insufficient."})     # 對照組：別種明確錯誤
TIMEOUT = (False, "Read timed out")                                    # 對照組：暫時性錯誤


def manual_alerts(alerts):
    return [a for a in alerts if "需要人工處理" in a]


class Base(ExecHarness):
    def setUp(self):
        missing = [n for n in NEW_API if not hasattr(pt, n)] + [n for n in ("is_reduce_only_rejected",) if not hasattr(ex, n)] \
            + (["app.abandoned"] if abandoned is None else []) + [n for n in ("abandon_position", "_reconcile_abandoned")
                                                               if not hasattr(pt.PaperTradingEngine, n)]
        if missing:
            self.fail(f"前提不成立：沒有 {missing}(第16條 r99 的新功能)")
        super().setUp()
        abandoned.reset_for_tests()
        self.saved = {}
        self.ps2 = [
            mock.patch.object(pt.db, "save_app_settings", side_effect=lambda d: self.saved.update(d)),
            mock.patch.object(pt.db, "load_app_settings", side_effect=lambda: (True, dict(self.saved))),
        ]
        for x in self.ps2:
            x.start()

    def tearDown(self):
        for x in getattr(self, "ps2", []):
            x.stop()
        if abandoned is not None:
            abandoned.reset_for_tests()
        if hasattr(self, "ps"):
            super().tearDown()

    def _pos(self, **kw):
        p = _pos(real_open_quantity=0.1, entry_actual_price=4391.0, **kw)
        self.eng._position = None
        return p

    def fail_close(self, pos, result, n=1, rows=None):
        rows = rows if rows is not None else _rows(0.1)
        with mock.patch.object(ex, "close_position", return_value=result) as cp, \
             mock.patch.object(ex, "get_position_info", return_value=(True, rows)), \
             mock.patch.object(self.eng, "_cancel_backstop", return_value=False):
            for _ in range(n):
                self.eng._position = None
                self.eng._close_position(pos, 4380.0, "觸及停損")
        return cp


class RejectVsTransient(Base):
    def test_fake_exchange_can_return_2022(self):
        self.assertTrue(ex.is_reduce_only_rejected(REJECT[1]))
        self.assertTrue(ex.is_reduce_only_rejected({"code": "-2022"}))
        self.assertTrue(ex.is_reduce_only_rejected("binance error -2022 ReduceOnly Order is rejected."))
        self.assertFalse(ex.is_reduce_only_rejected(MARGIN[1]))
        self.assertFalse(ex.is_reduce_only_rejected(TIMEOUT[1]))
        self.assertFalse(ex.is_reduce_only_rejected(None))

    def test_reject_counts_and_transient_does_not(self):
        pos = self._pos()
        cp = self.fail_close(pos, REJECT, n=3)
        self.assertGreaterEqual(cp.call_count, 3, "前提：平倉單真的送出、被拒")
        self.assertEqual(pos.get("close_reject_count"), 3)
        self.fail_close(pos, TIMEOUT)
        self.assertNotIn("close_reject_count", pos, "換成暫時性錯誤：歸零、回到每輪重試")
        self.assertTrue(pos.get("pending_close"))

    def test_stuck_after_threshold_with_single_manual_alert(self):
        pos = self._pos()
        self.fail_close(pos, REJECT, n=pt.CLOSE_REJECT_STUCK_AFTER - 1)
        self.assertFalse(pos.get("close_stuck"), "前提：還沒到門檻")
        self.assertEqual(manual_alerts(self.eng.alerts), [])
        before = len(self.eng.alerts)
        self.fail_close(pos, REJECT)
        self.assertTrue(pos.get("close_stuck"))
        self.assertAlmostEqual(float(pos["close_next_try"]), time.time() + pt.CLOSE_STUCK_RETRY_SECONDS, delta=5)
        self.assertEqual(len(manual_alerts(self.eng.alerts)), 1)
        self.assertIn("-2022", manual_alerts(self.eng.alerts)[0])
        self.fail_close(pos, REJECT, n=150 - pt.CLOSE_REJECT_STUCK_AFTER)   # 跨過一般告警節奏的第 150 次
        self.assertGreaterEqual(pos["close_fail_count"], 150, "前提：失敗次數到了一般節奏會再提醒的那一次")
        self.assertEqual(len(manual_alerts(self.eng.alerts)), 1, "卡住後只推一則")
        self.assertEqual(len(self.eng.alerts), before + 1, "卡住後被拒不再照次數告警(洗版)")

    def test_other_errors_never_back_off(self):
        pos = self._pos()
        self.fail_close(pos, MARGIN, n=pt.CLOSE_REJECT_STUCK_AFTER + 5)
        self.assertEqual(pos.get("close_fail_count"), pt.CLOSE_REJECT_STUCK_AFTER + 5, "前提：真的失敗了這麼多次")
        self.assertFalse(pos.get("close_stuck"), "別種錯誤不算明確拒絕，不退讓")
        self.assertEqual(manual_alerts(self.eng.alerts), [])

    def test_backoff_skips_retry_until_next_try_then_retries(self):
        pos = self._pos()
        self.fail_close(pos, REJECT, n=pt.CLOSE_REJECT_STUCK_AFTER)
        self.assertTrue(pos.get("close_stuck"), "前提：已卡住")
        self.eng._position = pos
        with mock.patch.object(self.eng, "_retry_pending_close") as rp, \
             mock.patch.object(self.eng, "_sync_backstop") as sb:
            self.eng._housekeeping(pos)
        self.assertFalse(rp.called, "退讓期間不重送平倉")
        self.assertFalse(sb.called, "退讓期間也不重掛停損(一樣會被拒)")
        pos["close_next_try"] = time.time() - 1
        with mock.patch.object(self.eng, "_retry_pending_close") as rp, \
             mock.patch.object(self.eng, "_sync_backstop"):
            self.eng._housekeeping(pos)
        self.assertTrue(rp.called, "時間到了照樣重試")

    def test_not_stuck_retries_every_round(self):
        pos = self._pos()
        self.fail_close(pos, TIMEOUT)
        self.eng._position = pos
        with mock.patch.object(self.eng, "_retry_pending_close") as rp, \
             mock.patch.object(self.eng, "_sync_backstop"):
            self.eng._housekeeping(pos)
        self.assertTrue(rp.called)

    def test_transient_after_stuck_resumes_every_round(self):
        pos = self._pos()
        self.fail_close(pos, REJECT, n=pt.CLOSE_REJECT_STUCK_AFTER)
        self.assertTrue(pos.get("close_stuck"), "前提：先卡住了")
        self.fail_close(pos, TIMEOUT)
        self.assertFalse(pos.get("close_stuck"))
        self.assertNotIn("close_next_try", pos)

    def test_success_after_stuck_settles_and_clears_flags(self):
        pos = self._pos()
        self.fail_close(pos, REJECT, n=pt.CLOSE_REJECT_STUCK_AFTER)
        with mock.patch.object(ex, "close_position", return_value=(True, {"avgPrice": "4379.4", "executedQty": "0.1"})), \
             mock.patch.object(ex, "get_position_info", return_value=(True, _rows(0.1))), \
             mock.patch.object(self.eng, "_cancel_backstop", return_value=False):
            self.eng._position = None
            self.eng._close_position(pos, 4380.0, "觸及停損")
        self.assertEqual(len(self.dbclose), 1, "時間到那次平掉了照常結帳")
        for k in ("close_reject_count", "close_stuck", "close_next_try"):
            self.assertNotIn(k, pos)

    def test_state_survives_restart(self):
        for k in ("close_reject_count", "close_stuck", "close_next_try"):
            self.assertIn(k, pt.RUNTIME_STATE_KEYS)


class Abandon(Base):
    def _stuck(self):
        pos = self._pos(id=77)
        self.fail_close(pos, REJECT, n=pt.CLOSE_REJECT_STUCK_AFTER)
        self.eng._position = pos
        return pos

    def test_refused_unless_stuck(self):
        pos = self._pos()
        self.fail_close(pos, REJECT, n=2)
        self.eng._position = pos
        ok, msg = self.eng.abandon_position()
        self.assertFalse(ok)
        self.assertIs(self.eng._position, pos)

    def test_abandon_removes_from_books_but_leaves_exchange(self):
        pos = self._stuck()
        with mock.patch.object(ex, "close_position") as cp, \
             mock.patch.object(ex, "cancel_all_open_orders", create=True) as ca, \
             mock.patch.object(self.eng, "_cancel_backstop") as cb, \
             mock.patch.object(pt, "_latest_price", return_value=4385.0, create=True):
            ok, msg = self.eng.abandon_position()
        self.assertTrue(ok, msg)
        self.assertIsNone(self.eng._position)
        self.assertFalse(cp.called or ca.called or cb.called, "交易所上的部位與掛單不動")
        self.assertEqual(len(self.dbclose), 1)
        trade_id, _px, _t, reason, pnl = self.dbclose[0][:5]
        self.assertEqual((trade_id, reason, pnl), (77, pt.ABANDON_EXIT_REASON, None), "損益記未知")
        entry = abandoned.blocked(ex.physical_account_key(self.eng.execution_account),
                                  ex._resolve_symbol(self.eng.execution_symbol))
        self.assertEqual((entry["direction"], entry["quantity"]), ("bullish", 0.1))
        self.assertTrue(any("已停止追蹤" in a for a in self.eng.alerts))
        self.assertTrue(self.eng._closed_trades_memory, "前提：記憶體裡有那筆")
        self.assertIsNone(self.eng._closed_trades_memory[-1]["pnl_points"], "記憶體裡那筆也是損益未知")

    def test_record_excluded_from_stats_and_breaker(self):
        rec = {"exit_reason": pt.ABANDON_EXIT_REASON, "pnl_points": None, "exit_time": "2099-01-01T00:00:00+00:00",
               "entry_price": 4390.0, "sl_price": 4300.0, "entry_time": "2099-01-01T00:00:00+00:00"}
        self.assertTrue(trading_core.is_abandoned_record(rec))
        self.eng._closed_trades_memory.clear()
        self.eng._closed_trades_memory.append(rec)
        with mock.patch.object(pt.db, "is_enabled", return_value=False), \
             mock.patch.object(risk_guard.db, "is_enabled", return_value=False):
            self.assertEqual(risk_guard.get_consecutive_losses(self.eng), 0, "不算連續虧損")
            self.assertEqual(risk_guard.get_unknown_pnl_count(self.eng), 0)
            today = dict(rec, exit_time=pt.datetime.now(pt.timezone.utc).isoformat())
            self.eng._closed_trades_memory.clear()
            self.eng._closed_trades_memory.append(today)
            self.assertEqual(risk_guard.get_daily_pnl_usd(self.eng, 1.0), 0.0, "不算進當日斷路器(不是 -1R)")
            summary = self.eng.get_summary()
        self.assertEqual(summary["total_trades"], 0, "不計入績效")
        self.assertEqual(len(summary["recent_trades"]), 1, "成交紀錄照樣列出")

    def test_blocks_real_entry_until_cleared(self):
        self._stuck()
        with mock.patch.object(pt, "_latest_price", return_value=4385.0, create=True):
            done, _ = self.eng.abandon_position()
            self.assertTrue(done)
        allowed, reason, kind = risk_guard._check_inner(self.eng, 0.1)
        self.assertEqual((allowed, kind), (False, "abandoned"))
        self.assertIn("已放棄追蹤", reason)

    def test_unreadable_list_blocks_conservatively(self):
        abandoned.reset_for_tests()
        with mock.patch.object(pt.db, "load_app_settings", return_value=(False, "db down")):
            allowed, reason, kind = risk_guard._check_inner(self.eng, 0.1)
        self.assertEqual((allowed, kind), (False, "abandoned"))
        self.assertIn("讀不到", reason)

    def test_persisted_and_reloaded_after_restart(self):
        self._stuck()
        with mock.patch.object(pt, "_latest_price", return_value=4385.0, create=True):
            self.eng.abandon_position()
        self.assertIn(abandoned.ABANDONED_DB_KEY, self.saved)
        abandoned.reset_for_tests()   # 重啟：記憶體清空，從資料庫讀回
        key = ex.physical_account_key(self.eng.execution_account)
        self.assertIsNotNone(abandoned.blocked(key, ex._resolve_symbol(self.eng.execution_symbol)))

    def test_reconcile_clears_when_exchange_flat(self):
        self._stuck()
        with mock.patch.object(pt, "_latest_price", return_value=4385.0, create=True):
            self.eng.abandon_position()
        with mock.patch.object(ex, "get_position_info", return_value=(True, _rows(0.1))):
            self.assertEqual(self.eng._reconcile_abandoned(), "still")
        with mock.patch.object(ex, "get_position_info", return_value=(False, "timeout")):
            self.assertEqual(self.eng._reconcile_abandoned(), "unknown", "查不到≠沒有")
        with mock.patch.object(ex, "get_position_info", return_value=(True, _rows(-0.3))):
            self.assertEqual(self.eng._reconcile_abandoned(), "cleared",
                             "只有反方向的部位：放棄的那一側已經清空")
        self.assertIsNone(abandoned.blocked(ex.physical_account_key(self.eng.execution_account),
                                            ex._resolve_symbol(self.eng.execution_symbol)))
        self.assertTrue(any("自動解除" in a for a in self.eng.alerts))
        self.assertEqual(json.loads(self.saved[abandoned.ABANDONED_DB_KEY]), {}, "解除也存回資料庫")

    def test_reconcile_runs_from_tick_when_flat(self):
        self.eng._position = None
        with mock.patch.object(self.eng, "_reconcile_abandoned") as rc:
            for _ in range(pt.ABANDONED_RECONCILE_EVERY_TICKS):
                self.eng._maybe_reconcile_abandoned()
        self.assertEqual(rc.call_count, 1, "約 5 分鐘一次")


class WebAndPreflight(Base):
    def test_endpoint_requires_password_and_engine(self):
        from app import main
        import asyncio
        with mock.patch.object(main.settings_module, "verify_password", return_value=(False, "密碼錯誤")):
            r = asyncio.run(main.control_abandon({"password": "x", "engine_id": self.eng.engine_id}))
        self.assertFalse(r["success"])
        self.assertIn("密碼", r["error"])
        with mock.patch.object(main.settings_module, "verify_password", return_value=(True, None)):
            r = asyncio.run(main.control_abandon({"password": "x", "engine_id": "nope"}))
            self.assertFalse(r["success"])
            self.assertIn("沒有這個引擎", r["error"])
            self.eng._position = None
            r = asyncio.run(main.control_abandon({"password": "x", "engine_id": self.eng.engine_id}))
        self.assertFalse(r["success"], "沒有卡住的部位就不能停止追蹤")

    def test_preflight_lists_abandoned_separately(self):
        from app import preflight
        self.assertEqual(preflight.abandoned_check()["status"], "ok")
        abandoned.add("gold", "XAUUSDT", direction="bearish", quantity=1.0, label="15分K")
        r = preflight.abandoned_check()
        self.assertEqual((r["item"], r["status"]), ("已放棄追蹤的部位", "warn"))
        self.assertIn("XAUUSDT 空 1.0", r["msg"])
        self.assertRegex(preflight.VERSION, r"r(99|\d{3,})$", "r99 起(之後的版本也行)；跟清單一致由下一行檢查")
        with open(os.path.join(ROOT, "BINANCE_LESSONS.md"), encoding="utf-8") as f:
            self.assertIn(f"版本 {preflight.VERSION}", f.read(300))

    def test_preflight_check_includes_item(self):
        from app import preflight
        with mock.patch.object(preflight, "ip_weight_check", return_value={"item": "ip", "status": "ok", "msg": ""}), \
             mock.patch.object(preflight, "entry_price_check", return_value={"item": "x", "status": "ok", "msg": ""}), \
             mock.patch.object(preflight.execution_module, "get_symbol_filters", return_value={}), \
             mock.patch.object(preflight.execution_module, "_get_credentials", return_value=(None, None)):
            items = [x["item"] for x in preflight.check()]
        self.assertIn("已放棄追蹤的部位", items)

    def test_dashboard(self):
        with open(os.path.join(ROOT, "app/static/dashboard.html"), encoding="utf-8") as f:
            h = f.read()
        self.assertIn("pendingCloseHtml(p, data.engine_id, t.apiBase || '')", h)
        self.assertIn("/control/abandon", h)
        self.assertIn("apiBase: '/live-proxy'", h)
        self.assertIn("data.abandoned_position", h)
        from app import role
        self.assertTrue(any("/control/abandon".startswith(p) for p in role.LIVE_PROXY_ALLOWED_PREFIXES))


if __name__ == "__main__":
    unittest.main()
