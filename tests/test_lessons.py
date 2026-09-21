"""
BINANCE_LESSONS.md 對照測試(清單第5點的做法：先在修改前的程式上跑、確認會失敗，再修改到全部通過)。
涵蓋 r7→r10 第7、8條的每一個檢查項目。執行：python -m unittest tests.test_lessons -v

每個測試名稱前綴對應清單段落：
  t7a 反轉依被拒的單   t7b 送單前偵測失敗   t7c 記住/清掉假設
  t8a 節奏落點         t8b 恢復條件≥1       t8c1 失敗在計數之外   t8c2 恢復放錯位置
  t8d 已成交先通知
"""
import logging
import unittest
from unittest import mock

logging.disable(logging.CRITICAL)

from app import execution as ex, alert_cadence as ac  # noqa: E402
from app import paper_trading as pt  # noqa: E402

ONE_WAY_ROWS = [{"symbol": "XAUUSDT", "positionSide": "BOTH", "positionAmt": "0.1"}]


def _engine():
    eng = next(iter(pt.PAPER_TRADING_ENGINES.values()))
    eng.alerts = []
    eng._backstop_alert = lambda t: eng.alerts.append(t)
    eng._orphan_cancels = []
    return eng


def _pos(**kw):
    p = dict(direction="bullish", sl_price=4380.0, entry_price=4390.0, peak_price=4390.0,
             real_open_executed=True, real_open_quantity=0.1, id=None, trailing_active=False)
    p.update(kw)
    return p


class Lesson7(unittest.TestCase):
    def setUp(self):
        if hasattr(ex, "_last_known_hedge"):
            ex._last_known_hedge.clear()

    def test_t7a_flip_uses_rejected_order_not_remembered_mode(self):
        """被拒的單帶positionSide(當雙向送)→重送一定是單向寫法，不能拿記住的模式來反轉。"""
        if hasattr(ex, "_last_known_hedge"):
            ex._last_known_hedge["gold"] = True  # 記住的是雙向，照它反轉就會錯
        sent = []
        with mock.patch.object(ex, "get_position_mode", return_value=(False, "timeout")), \
             mock.patch.object(ex, "_signed_request", side_effect=lambda m, p, params, **k: sent.append(params) or (True, {})):
            ex._resend_on_mode_mismatch("POST", "/fapi/v1/order", {"side": "SELL", "positionSide": "LONG"},
                                        "SELL", True, {"code": -4061}, "gold")
        self.assertNotIn("positionSide", sent[0])
        self.assertEqual(sent[0].get("reduceOnly"), "true")

    def test_t7b_close_is_sent_when_mode_detection_fails_on_one_way_account(self):
        """偵測失敗時平倉單一定要送出去。單向帳戶部位在BOTH列，不能因為猜成雙向就回「沒有部位」。"""
        with mock.patch.object(ex, "is_enabled", return_value=True), \
             mock.patch.object(ex, "get_position_mode", return_value=(False, "timeout")), \
             mock.patch.object(ex, "get_position_info", return_value=(True, ONE_WAY_ROWS)), \
             mock.patch.object(ex, "place_market_order", return_value=(True, {"avgPrice": "4380"})) as pm:
            hedge = ex.current_hedge_mode(account="gold", default=True)
            ok, _ = ex.close_position("bullish", account="gold", quantity=0.1, hedge=hedge)
        self.assertTrue(ok)
        self.assertEqual(pm.call_count, 1)

    def test_t7b_detection_failure_uses_last_known_then_one_way(self):
        """偵測失敗：有舊值用舊值，從沒偵測成功過就先假設單向(不是用呼叫端的default猜雙向)。"""
        with mock.patch.object(ex, "get_position_mode", return_value=(False, "timeout")):
            self.assertFalse(ex.current_hedge_mode(account="gold", default=True))
        # 偵測成功要走真的get_position_mode(記住模式的程式在它裡面)，所以mock底層API回應
        with mock.patch.object(ex, "_signed_request", return_value=(True, {"dualSidePosition": True})):
            self.assertTrue(ex.current_hedge_mode(account="gold"))
        with mock.patch.object(ex, "get_position_mode", return_value=(False, "timeout")):
            self.assertTrue(ex.current_hedge_mode(account="gold", default=False))

    def test_t7b_open_detection_failure_does_not_assume_wanted_mode(self):
        """開倉：切模式失敗、再偵測也失敗時，不能直接假設「設定想要的模式」。"""
        with mock.patch.object(ex, "ensure_position_mode", return_value=(False, {"code": -1001})), \
             mock.patch.object(ex, "get_position_mode", return_value=(False, "timeout")):
            effective, _ = ex.resolve_position_mode(True, account="gold")
        self.assertFalse(effective)

    def test_t7c_resend_success_remembers_resend_failure_clears(self):
        """重送成功才記住成功的那個假設；重送也失敗就清掉，不留沒驗證過的值。"""
        self.assertTrue(hasattr(ex, "_last_known_hedge"), "沒有記住模式的機制")
        with mock.patch.object(ex, "get_position_mode", return_value=(False, "timeout")), \
             mock.patch.object(ex, "_signed_request", return_value=(True, {})):
            ex._resend_on_mode_mismatch("POST", "/x", {"side": "SELL", "positionSide": "LONG"}, "SELL", True, {"code": -4061}, "gold")
        self.assertIs(ex._last_known_hedge.get("gold"), False)
        with mock.patch.object(ex, "get_position_mode", return_value=(False, "timeout")), \
             mock.patch.object(ex, "_signed_request", return_value=(False, {"code": -4061})):
            ex._resend_on_mode_mismatch("POST", "/x", {"side": "SELL", "reduceOnly": "true"}, "SELL", True, {"code": -4061}, "gold")
        self.assertNotIn("gold", ex._last_known_hedge)


class Lesson8(unittest.TestCase):
    def setUp(self):
        self.p = [mock.patch.object(ex, "round_price", lambda p, *a, **k: p),
                  mock.patch.object(ex, "current_hedge_mode", return_value=False)]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()

    def test_t8a_cadence_points(self):
        hits = [n for n in range(1, 400) if ac.should_alert(n)]
        self.assertEqual(hits, [1, 5, 30, 150, 270, 390])

    def test_t8b_backstop_fail_once_then_recover_sends_recovery(self):
        eng, pos = _engine(), _pos()
        with mock.patch.object(ex, "place_algo_stop", side_effect=[(False, {"code": -1001}, False), (True, "A1", False)]):
            eng._sync_backstop(pos)
            eng._sync_backstop(pos)
        self.assertEqual(len(eng.alerts), 2)
        self.assertIn("第 1 次", eng.alerts[0])
        self.assertIn("補上", eng.alerts[1])

    def test_t8b_orphan_fail_at_close_then_next_round_ok_sends_recovery(self):
        eng = _engine()
        with mock.patch.object(ex, "cancel_algo_stop", return_value=(False, {"code": -1001}, False)):
            eng._cancel_backstop({"backstop_algo_id": "X1", "backstop_used_legacy": False})
        with mock.patch.object(ex, "cancel_algo_stop", return_value=(True, {}, False)):
            eng._retry_orphan_cancels()
        self.assertEqual(len(eng.alerts), 2)
        self.assertIn("第 1 次", eng.alerts[0])
        self.assertIn("撤掉", eng.alerts[1])

    def test_t8c1_exception_in_backstop_sync_is_counted(self):
        """掛停損過程丟例外也是一次失敗，要計數、第1次要告警。"""
        eng, pos = _engine(), _pos()
        with mock.patch.object(ex, "place_algo_stop", side_effect=RuntimeError("boom")):
            eng._sync_backstop(pos)
        self.assertEqual(pos.get("backstop_fail_count"), 1)
        self.assertEqual(len(eng.alerts), 1)
        self.assertIn("第 1 次", eng.alerts[0])

    def test_t8c1_stale_cancel_failure_during_move_is_counted_at_failure(self):
        """搬移時舊單撤不掉：要在撤不掉的當下計數並告警第1次，不是等平倉才開始算。"""
        eng, pos = _engine(), _pos(backstop_algo_id="OLD", backstop_price=4370.0)
        with mock.patch.object(ex, "place_algo_stop", return_value=(True, "NEW", False)), \
             mock.patch.object(ex, "cancel_algo_stop", return_value=(False, {"code": -1001}, False)):
            eng._sync_backstop(pos)
        self.assertEqual([(i["algo_id"], i["fail_count"]) for i in eng._orphan_cancels], [("OLD", 1)])
        self.assertTrue(any("第 1 次" in a for a in eng.alerts))

    def test_t8c2_failure_state_cleared_by_close_sends_notice(self):
        """backstop失敗中、部位從別的路徑平掉(失敗狀態消失)：要發通知收尾，不能讓使用者等不到結果。"""
        eng, pos = _engine(), _pos()
        with mock.patch.object(ex, "place_algo_stop", return_value=(False, {"code": -1001}, False)):
            eng._sync_backstop(pos)
        eng.alerts.clear()
        eng._cancel_backstop(pos)  # 平倉流程一定會經過這裡
        self.assertEqual(len(eng.alerts), 1)
        self.assertIn("已平倉", eng.alerts[0])

    def test_t8d_open_filled_then_later_step_raises_still_reported_as_filled(self):
        """真實開倉已成交、後續記錄步驟丟例外：通知仍要是成交，不能報成下單失敗。"""
        eng = _engine()
        captured = {}
        settings = dict(pt.settings_module.get_settings(engine_id=eng.engine_id))
        settings.update(execution_quantity=0.1, execution_leverage=5, execution_margin_type=0, execution_hedge_mode=0)
        with mock.patch.object(eng, "_is_execution_engine", return_value=True), \
             mock.patch.object(pt.settings_module, "get_settings", return_value=settings), \
             mock.patch.object(pt.risk_guard, "check", return_value=(True, None, None)), \
             mock.patch.object(ex, "resolve_position_mode", return_value=(False, None)), \
             mock.patch.object(ex, "set_margin_type", return_value=(True, {})), \
             mock.patch.object(ex, "set_leverage", return_value=(True, {})), \
             mock.patch.object(ex, "open_position", return_value=(True, {"avgPrice": "4390"})), \
             mock.patch.object(ex, "analyze_execution_quality", side_effect=RuntimeError("boom")), \
             mock.patch.object(eng, "_sync_backstop"), \
             mock.patch.object(pt.db, "insert_paper_trade", return_value=None, create=True), \
             mock.patch.object(pt.notifier_module.notifier, "notify_trade_event",
                               side_effect=lambda **k: captured.update(k)):
            eng._position = None
            eng._open_position({"direction": "bullish", "bid": 4389.9, "ask": 4390.0,
                                "chan": {"reason": "t"}, "profile": {"reason": "t"}}, 4390.0, 10.0)
        self.assertIs(captured.get("executed"), True, f"通知內容：{captured}")


if __name__ == "__main__":
    unittest.main()
