"""
BINANCE_LESSONS.md 對照測試(清單第5點的做法：先在修改前的程式上跑、確認會失敗，再修改到全部通過)。
涵蓋 r7→r28 第1、2、3、7、8、14條的每一個檢查項目。

斷言分類(用法第5點r19)：
  正向斷言(「有送單」「數量＝0.05」「有告警」)——程式什麼都沒做會失敗，不會空跑。
  否定句斷言(「沒送單」「帳上紀錄還在」「停損沒被撤」)——程式什麼都沒做也成立，
  所以每一個都要有「前提」斷言：證明走到被測那一步所需的每個動作真的發生了(測錯方式8、12)。
  斷言看「這一步之後新發生的事」(只算這一步建立的mock/清單)，不看被測前就存在的最終狀態(測錯方式13)。
  否定句的前提要檢查查詢的「結果」，不只是「有去查」(測錯方式16)：「沒掛停損」可能是「部位沒了」也可能是「查不到」，
  前提要寫出是哪一個。分類看斷言本身，不看測試描述(測錯方式15)。
  注入要綁在被測的那一步，不用「第幾次查詢」計數(測錯方式18)：多一次查詢就會錯位。執行：python -m unittest tests.test_lessons -v

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
import time  # noqa: E402
QTY = getattr(pt, "QTY_CHECK_EVERY_TICKS", 4)

ONE_WAY_ROWS = [{"symbol": "XAUUSDT", "positionSide": "BOTH", "positionAmt": "0.1"}]


def _engine():
    eng = next(iter(pt.PAPER_TRADING_ENGINES.values()))
    eng.alerts = []
    eng._backstop_alert = lambda t: eng.alerts.append(t)
    eng._orphan_cancels = []
    eng._step_errors = {}      # 前一個測試留下的出錯次數不能帶進來
    eng._qty_check_tick = 0
    eng._position = None
    ex._last_known_hedge.clear()   # 模組層級的「記住的持倉模式」也會被測試改到
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
        with mock.patch.object(ex, "ensure_position_mode", return_value=(False, {"code": -1001})) as en, \
             mock.patch.object(ex, "get_position_mode", return_value=(False, "timeout")) as gm:
            effective, _ = ex.resolve_position_mode(True, account="gold")
        self.assertTrue(en.called and gm.called, "前提：真的嘗試切模式、切不過去後真的重新偵測(而且失敗)")
        self.assertFalse(effective, "從沒偵測成功過：假設單向，不是設定想要的雙向")
        # 正向(靜態檢查抓到只有否定句)：有舊值時要回舊值——結果非得經過退路邏輯不可
        ex._last_known_hedge["gold"] = True
        with mock.patch.object(ex, "ensure_position_mode", return_value=(False, {"code": -1001})), \
             mock.patch.object(ex, "get_position_mode", return_value=(False, "timeout")):
            effective2, _ = ex.resolve_position_mode(False, account="gold")
        self.assertIs(effective2, True, "偵測失敗、有舊值(雙向)：用舊值，不是設定想要的單向")

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
                  mock.patch.object(ex, "current_hedge_mode", return_value=False),
                  # r19起掛停損前會先確認部位還在：模擬交易所上有自己的0.1張
                  mock.patch.object(ex, "get_position_info", return_value=(True, _rows(0.1)))]
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
        self.assertIn("-1001", eng.alerts[0])  # 告警裡的錯誤要是注入的那個(第14條)
        self.assertIn("補上", eng.alerts[1])

    def test_t8b_orphan_fail_at_close_then_next_round_ok_sends_recovery(self):
        eng = _engine()
        with mock.patch.object(ex, "cancel_algo_stop", return_value=(False, {"code": -1001}, False)):
            eng._cancel_backstop({"backstop_algo_id": "X1", "backstop_used_legacy": False})
        with mock.patch.object(ex, "cancel_algo_stop", return_value=(True, {}, False)):
            eng._retry_orphan_cancels()
        self.assertEqual(len(eng.alerts), 2)
        self.assertIn("第 1 次", eng.alerts[0])
        self.assertIn("-1001", eng.alerts[0])
        self.assertIn("撤掉", eng.alerts[1])

    def test_t8c1_exception_in_backstop_sync_is_counted(self):
        """掛停損過程丟例外也是一次失敗，要計數、第1次要告警。"""
        eng, pos = _engine(), _pos()
        with mock.patch.object(ex, "place_algo_stop", side_effect=RuntimeError("boom")):
            eng._sync_backstop(pos)
        self.assertEqual(pos.get("backstop_fail_count"), 1)
        self.assertEqual(len(eng.alerts), 1)
        self.assertIn("第 1 次", eng.alerts[0])
        self.assertIn("boom", eng.alerts[0])

    def test_t8c1_stale_cancel_failure_during_move_is_counted_at_failure(self):
        """搬移時舊單撤不掉：要在撤不掉的當下計數並告警第1次，不是等平倉才開始算。"""
        eng, pos = _engine(), _pos(backstop_algo_id="OLD", backstop_price=4370.0)
        with mock.patch.object(ex, "place_algo_stop", return_value=(True, "NEW", False)), \
             mock.patch.object(ex, "cancel_algo_stop", return_value=(False, {"code": -1001}, False)):
            eng._sync_backstop(pos)
        self.assertEqual([(i["algo_id"], i["fail_count"]) for i in eng._orphan_cancels], [("OLD", 1)])
        self.assertTrue(any("第 1 次" in a and "-1001" in a for a in eng.alerts), eng.alerts)

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
             mock.patch.object(ex, "open_position", return_value=(True, {"avgPrice": "4391.2"})) as op, \
             mock.patch.object(ex, "get_position_info", return_value=(True, _rows(0))), \
             mock.patch.object(ex, "analyze_execution_quality", side_effect=RuntimeError("boom")), \
             mock.patch.object(eng, "_sync_backstop"), \
             mock.patch.object(pt.db, "insert_paper_trade", return_value=None, create=True), \
             mock.patch.object(pt.notifier_module.notifier, "notify_trade_event",
                               side_effect=lambda **k: captured.update(k)):
            eng._position = None
            eng._open_position({"direction": "bullish", "bid": 4389.9, "ask": 4390.0,
                                "chan": {"reason": "t"}, "profile": {"reason": "t"}}, 4390.0, 10.0)
        self.assertEqual(op.call_count, 1, "前提：單真的送出去、成交(測錯方式8)")
        self.assertIs(captured.get("executed"), True, f"通知內容：{captured}")
        self.assertIn("boom", captured.get("slippage_note") or "")



# ------------------------------------------------------------------ r11 → r13
def _exec_settings(eng):
    st = dict(pt.settings_module.get_settings(engine_id=eng.engine_id))
    st.update(execution_quantity=0.2, execution_leverage=5, execution_margin_type=0, execution_hedge_mode=0)
    return st


def _rows(qty, entry=4391.5, side="BOTH", mark=4395.0):
    """模擬positionRisk：單向空單positionAmt是負數、雙向回LONG/SHORT列(用法第5點)。"""
    return [{"symbol": "XAUUSDT", "positionSide": side, "positionAmt": str(qty),
             "entryPrice": str(entry), "markPrice": str(mark)}]


class ExecHarness(unittest.TestCase):
    """真實下單引擎的共用環境：通知、資料庫、交易所都攔截，記錄呼叫。"""
    def setUp(self):
        self.eng = _engine()
        self.notes = []
        self.dbclose = []
        st = _exec_settings(self.eng)
        self.ps = [
            mock.patch.object(self.eng, "_is_execution_engine", return_value=True),
            mock.patch.object(pt.settings_module, "get_settings", return_value=st),
            mock.patch.object(pt.notifier_module.notifier, "notify_trade_event", side_effect=lambda **k: self.notes.append(k)),
            mock.patch.object(pt.db, "close_paper_trade", side_effect=lambda *a, **k: self.dbclose.append(a)),
            mock.patch.object(ex, "current_hedge_mode", return_value=False),
            mock.patch.object(ex, "round_price", lambda p, *a, **k: p),
        ]
        for x in self.ps:
            x.start()

    def tearDown(self):
        for x in self.ps:
            x.stop()


class Lesson1(unittest.TestCase):
    def test_t1a_algo_404_fallback_is_per_call_not_permanent(self):
        calls = []
        def fake(method, path, params=None, account=None, return_status=False):
            calls.append(path)
            if path == "/fapi/v1/algoOrder":
                return (False, {"code": -5000}, 404) if return_status else (False, {})
            return True, {"orderId": 7}
        with mock.patch.object(ex, "_signed_request", side_effect=fake):
            ex.place_algo_stop("bullish", 0.1, 4380.0, account="gold")
            ex.place_algo_stop("bullish", 0.1, 4381.0, account="gold")
        self.assertEqual(calls, ["/fapi/v1/algoOrder", "/fapi/v1/order", "/fapi/v1/algoOrder", "/fapi/v1/order"])

    def test_t1a_minus1000_and_minus1013_do_not_fall_back(self):
        for code, status in ((-1000, 500), (-1013, 400)):
            calls = []
            def fake(method, path, params=None, account=None, return_status=False):
                calls.append(path)
                return (False, {"code": code}, status) if return_status else (False, {"code": code})
            with mock.patch.object(ex, "_signed_request", side_effect=fake):
                ok, err, legacy = ex.place_algo_stop("bullish", 0.1, 4380.0, account="gold")
            self.assertEqual(calls, ["/fapi/v1/algoOrder"], code)
            self.assertFalse(ok)
            self.assertEqual(err.get("code"), code)


class Lesson3(ExecHarness):
    def _open(self, open_ret, before, after):
        """
        部位查詢綁在「開倉單送出」這一步(測錯方式18)：送出之前回before(基準)、之後回after。
        after=None代表這個情境送單後不該再查部位。self.inj記錄每次注入是在送單前還是後觸發。
        """
        eng = self.eng
        state = {"sent": False}
        self.inj = []
        def fake_open(*a, **k):
            state["sent"] = True
            return open_ret
        def fake_rows(*a, **k):
            self.inj.append("after" if state["sent"] else "before")
            return (after if state["sent"] else before) or (False, "不該在送單後查部位")
        with mock.patch.object(pt.risk_guard, "check", return_value=(True, None, None)), \
             mock.patch.object(ex, "resolve_position_mode", return_value=(False, None)), \
             mock.patch.object(ex, "set_margin_type", return_value=(True, {})), \
             mock.patch.object(ex, "set_leverage", return_value=(True, {})), \
             mock.patch.object(ex, "open_position", side_effect=fake_open) as self.op, \
             mock.patch.object(ex, "get_position_info", side_effect=fake_rows), \
             mock.patch.object(eng, "_sync_backstop") as sync:
            eng._position = None
            eng._open_position({"direction": "bullish", "bid": 4389.9, "ask": 4390.0,
                                "chan": {"reason": "t"}, "profile": {"reason": "t"}}, 4390.0, 10.0)
        return eng._position, sync

    def test_t3a_timeout_but_filled_is_claimed_with_exchange_qty_and_price(self):
        # 設定要0.2張、交易所實際0.1張；訊號價4390、交易所均價4391.5(測錯方式1：兩個來源不同值)
        pos, sync = self._open((False, "Read timed out"), before=(True, _rows(0)), after=(True, _rows(0.1)))
        self.assertEqual(self.inj, ["before", "after"], "前提：基準在送單前查、認領在送單後查(注入觸發時機)")
        self.assertIs(pos.get("real_open_executed"), True)
        self.assertEqual(pos.get("real_open_quantity"), 0.1)
        self.assertEqual(pos.get("entry_actual_price"), 4391.5)
        self.assertTrue(sync.called, "認領後要當場掛停損")
        self.assertIs(self.notes[-1].get("executed"), True)

    def test_t3b_timeout_not_yet_visible_is_kept_pending_with_deadline(self):
        pos, _ = self._open((False, "Read timed out"), before=(True, _rows(0)), after=(True, _rows(0)))
        self.assertEqual(self.inj, ["before", "after"], "前提：送單後真的查了、而且看到的是『還沒有』")
        self.assertIsNot(pos.get("real_open_executed"), False, "結果不明不能直接記成沒開倉")
        self.assertTrue(pos.get("real_open_pending_until"))
        eng = self.eng
        with mock.patch.object(ex, "get_position_info", return_value=(True, _rows(0))) as gp:
            eng._resolve_open_pending(pos)                       # 期限內：這一輪沒看到也不能清
        self.assertTrue(gp.called, "前提：這一輪真的查了")
        self.assertTrue(pos.get("real_open_pending_until"))
        pos["real_open_pending_until"] = 1.0                     # 期限已過(用非0的過去時間，測錯方式5)
        with mock.patch.object(ex, "get_position_info", return_value=(True, _rows(0))):
            eng._resolve_open_pending(pos)
        self.assertIs(pos.get("real_open_executed"), False)
        self.assertTrue(any("未成交" in a for a in eng.alerts), eng.alerts)

    def test_t3c_explicit_reject_is_not_pending(self):
        pos, _ = self._open((False, {"code": -2019, "msg": "Margin is insufficient."}), before=(True, _rows(0)), after=None)
        self.assertEqual(self.inj, ["before"], "明確拒絕不是結果不明，送單後不該再查部位")
        self.assertEqual(self.op.call_count, 1, "前提：單真的送出去、被交易所拒絕")
        self.assertIs(pos.get("real_open_executed"), False)
        self.assertFalse(pos.get("real_open_pending_until"))


class Lesson7Manual(unittest.TestCase):
    def test_t7b_manual_close_own_side_missing_sends_nothing(self):
        rows = [{"symbol": "XAUUSDT", "positionSide": "SHORT", "positionAmt": "-0.3"},
                {"symbol": "XAUUSDT", "positionSide": "LONG", "positionAmt": "0"}]
        with mock.patch.object(ex, "is_enabled", return_value=True), \
             mock.patch.object(ex, "get_position_info", return_value=(True, rows)) as gp, \
             mock.patch.object(ex, "place_market_order") as pm:
            ok, msg = ex.close_position("bullish", account="gold", quantity=None, hedge=True)
        self.assertTrue(gp.called, "前提：真的查了部位表")
        self.assertIn("LONG", str(msg), "前提：不送的原因是『自己那一側沒有部位』(測錯方式16)")
        self.assertFalse(ok)
        self.assertEqual(pm.call_count, 0, "不能退而平掉別的那一列(測錯方式4：要看有沒有送單)")


class Lesson8Close(ExecHarness):
    def _pos(self):
        p = _pos(real_open_quantity=0.1, entry_actual_price=4391.0, backstop_algo_id="B1", backstop_price=4380.0)
        self.eng._position = None  # 已被claim
        return p

    def test_t8a_rejected_close_keeps_record_backstop_and_marks_pending(self):
        pos = self._pos()
        with mock.patch.object(ex, "close_position", return_value=(False, {"code": -2019, "msg": "Margin is insufficient."})) as cp, \
             mock.patch.object(ex, "get_position_info", return_value=(True, _rows(0.1))) as gp, \
             mock.patch.object(self.eng, "_cancel_backstop") as cb:
            self.eng._close_position(pos, 4380.0, "觸及停損")
        self.assertEqual(cp.call_count, 1, "前提：平倉單真的送出、被拒")
        self.assertTrue(gp.called, "前提：被拒後真的去查了部位")
        self.assertEqual(self.dbclose, [], "平倉沒確認前不能把帳上紀錄結掉")
        self.assertFalse(cb.called, "平倉沒確認前不能撤交易所停損")
        self.assertIs(self.eng._position, pos)
        self.assertTrue(pos.get("pending_close"))
        self.assertTrue(any("第 1 次" in a and "-2019" in a for a in self.eng.alerts), self.eng.alerts)

    def test_t8a_timeout_but_gone_on_exchange_is_closed_without_failure_alert(self):
        pos = self._pos()
        with mock.patch.object(ex, "close_position", return_value=(False, "Read timed out")), \
             mock.patch.object(ex, "get_position_info", return_value=(True, _rows(0))), \
             mock.patch.object(self.eng, "_cancel_backstop", return_value=False):
            self.eng._close_position(pos, 4380.0, "觸及停損")
        self.assertEqual(len(self.dbclose), 1)
        self.assertIsNone(self.eng._position)
        self.assertFalse(self.notes[-1].get("execution_error"), f"不能告警平倉失敗：{self.notes[-1]}")

    def test_t8a_verify_query_failure_is_treated_as_not_closed(self):
        pos = self._pos()
        with mock.patch.object(ex, "close_position", return_value=(False, "Read timed out")) as cp, \
             mock.patch.object(ex, "get_position_info", return_value=(False, "Read timed out")) as gp, \
             mock.patch.object(self.eng, "_cancel_backstop") as cb:
            self.eng._close_position(pos, 4380.0, "觸及停損")
        self.assertEqual(cp.call_count, 1, "前提：平倉單真的送出")
        self.assertTrue(gp.called, "前提：真的去查了部位")
        self.assertEqual(self.dbclose, [])
        self.assertFalse(cb.called)
        self.assertTrue(pos.get("pending_close"))

    def test_t8a_pending_close_retried_every_round_then_recovers(self):
        pos = self._pos()
        with mock.patch.object(ex, "close_position", return_value=(False, {"code": -2019})), \
             mock.patch.object(ex, "get_position_info", return_value=(True, _rows(0.1))):
            self.eng._close_position(pos, 4380.0, "觸及停損")
        self.assertTrue(pos.get("pending_close"), "前提：第一次被拒後進入待平倉")
        self.assertIn("目前交易所部位：0.1", self.eng.alerts[0],
                      "前提：確認的結果是『部位還在0.1』，不是『查不到』(測錯方式16：兩者都會進待平倉)")
        with mock.patch.object(ex, "close_position", return_value=(True, {"avgPrice": "4379.4"})), \
             mock.patch.object(ex, "get_position_info", return_value=(True, _rows(0.1))), \
             mock.patch.object(self.eng, "_cancel_backstop", return_value=False), \
             mock.patch.object(pt, "_latest_price", return_value=4379.5, create=True):
            self.eng._retry_pending_close()   # 下一輪tick直接重試，不等下一根K棒的出場訊號
        self.assertEqual(len(self.dbclose), 1)
        self.assertIsNone(self.eng._position)
        self.assertTrue(any("恢復" in a or "已平倉" in a for a in self.eng.alerts[1:]), self.eng.alerts)

    def test_t8a_quantity_mismatch_resends_with_exchange_quantity(self):
        pos = self._pos()
        pos["real_open_quantity"] = 0.2
        calls = []
        def fake_close(**k):
            calls.append(k.get("quantity"))
            return (False, {"code": -2022, "msg": "ReduceOnly Order is rejected."}) if len(calls) == 1 else (True, {"avgPrice": "4379.4"})
        with mock.patch.object(ex, "close_position", side_effect=fake_close), \
             mock.patch.object(ex, "get_position_info", return_value=(True, _rows(0.05))), \
             mock.patch.object(self.eng, "_cancel_backstop", return_value=False):
            self.eng._close_position(pos, 4380.0, "觸及停損")
        self.assertEqual(calls, [0.2, 0.05])
        self.assertEqual(len(self.dbclose), 1)

    def test_t8b_exchange_quantity_decrease_is_detected(self):
        pos = self._pos()
        pos["real_open_quantity"] = 0.2
        self.eng._position = pos
        with mock.patch.object(ex, "get_position_info", return_value=(True, _rows(0.1, mark=4396.0))):
            self.eng._check_exchange_quantity(pos)
        self.assertEqual(pos["real_open_quantity"], 0.1)
        self.assertTrue(any("減少" in a and "0.1" in a for a in self.eng.alerts), self.eng.alerts)


# ------------------------------------------------------------------ r14 → r16
class Lesson16(ExecHarness):
    def _real_pos(self, **kw):
        p = _pos(real_open_quantity=0.1, entry_actual_price=4391.0, backstop_algo_id="B1",
                 backstop_price=4380.0, backstop_used_legacy=False)
        p.update(kw)
        self.eng._position = p
        return p

    # 1a/1b：停損守衛。每個情境各自建立攔截(測錯方式10)，不共用上一個情境的物件
    def _guard(self, pos, algo_resp, legacy_resp, rounds):
        calls = []
        def fake(method, path, params=None, account=None, return_status=False):
            calls.append(path)
            if path == "/fapi/v2/positionRisk":
                r = (True, _rows(0.1), 200)  # 補掛前的部位確認(r19)：自己的部位還在
            else:
                r = algo_resp if path == "/fapi/v1/openAlgoOrders" else legacy_resp
            return r if return_status else r[:2]
        with mock.patch.object(ex, "_signed_request", side_effect=fake), \
             mock.patch.object(ex, "place_algo_stop", return_value=(True, "NEW", False)) as pl:
            for _ in range(rounds):
                self.eng._check_backstop_present(pos)
        return calls, pl

    def test_t1a_guard_replaces_backstop_missing_three_rounds(self):
        pos = self._real_pos()
        calls, pl = self._guard(pos, (True, [], 200), (True, [], 200), 2)
        self.assertEqual(pl.call_count, 0, "連續3輪才動手(第2條)")
        self.assertIn("/fapi/v1/openAlgoOrders", calls, "前提：真的查了Algo掛單")
        calls, pl = self._guard(pos, (True, [], 200), (True, [], 200), 1)
        self.assertEqual(pl.call_count, 1)
        self.assertEqual(pos.get("backstop_algo_id"), "NEW")

    def test_t1a_guard_query_failure_does_not_count(self):
        pos = self._real_pos()
        calls, pl = self._guard(pos, (False, {"code": -1001}, 500), (False, {"code": -1001}, 500), 5)
        self.assertEqual(calls.count("/fapi/v1/openAlgoOrders"), 5, "前提：5輪都真的查了")
        self.assertIsNone(pos.get("backstop_missing"), "前提：5輪的結果都是『查不到』，沒有被算成『不見』")
        self.assertEqual(pl.call_count, 0, "查詢失敗不能當成停損不見(第2條)")

    def test_t1b_algo_404_falls_back_to_legacy_query_instead_of_skipping(self):
        """服務重啟後沒有任何退回旗標：Algo查詢404要改查舊端點，不能每輪都「查詢失敗、跳過」(r16)。"""
        pos = self._real_pos(backstop_algo_id="L7", backstop_used_legacy=True)
        calls, pl = self._guard(pos, (False, {"code": -5000}, 404), (True, [], 200), 3)
        self.assertIn("/fapi/v1/openOrders", calls)
        self.assertEqual(pl.call_count, 1, "舊端點的停損不見了要補掛")
        pos2 = self._real_pos(backstop_algo_id="A9", backstop_used_legacy=False)
        calls, pl = self._guard(pos2, (False, {"code": -5000}, 404), (True, [], 200), 3)
        self.assertIn("/fapi/v1/openOrders", calls, "Algo查詢404也要進入退回、改查舊端點")
        self.assertEqual(pl.call_count, 1)

    def test_t8b_guard_replace_hits_minus2021_exits(self):
        pos = self._real_pos()
        pos["backstop_missing"] = 2
        closed = []
        def fake(method, path, params=None, account=None, return_status=False):
            r = (True, _rows(0.1), 200) if path == "/fapi/v2/positionRisk" else (True, [], 200)
            return r if return_status else r[:2]
        with mock.patch.object(ex, "_signed_request", side_effect=fake), \
             mock.patch.object(ex, "place_algo_stop", return_value=(False, {"code": -2021, "msg": "Order would immediately trigger."}, False)), \
             mock.patch.object(self.eng, "_close_position", side_effect=lambda p, px, r, **k: closed.append(r)):
            self.eng._check_backstop_present(pos)
        self.assertEqual(len(closed), 1, "守衛補掛遇-2021要直接出場，不是只告警")

    # 2a：帶symbol查詢回200＋空清單，不能判成「沒有部位」
    def test_t2a_empty_list_is_not_no_position(self):
        pos = self._real_pos()
        with mock.patch.object(ex, "close_position", return_value=(False, "Read timed out")) as cp, \
             mock.patch.object(ex, "get_position_info", return_value=(True, [])) as gp, \
             mock.patch.object(self.eng, "_cancel_backstop") as cb:
            self.eng._close_position(pos, 4380.0, "觸及停損")
        self.assertEqual(cp.call_count, 1, "前提：平倉單真的送出")
        self.assertTrue(gp.called)
        self.assertTrue(pos.get("pending_close"), "前提：結果是『無法確認、待平倉』(測錯方式16)")
        self.assertEqual(self.dbclose, [], "空清單不能當成已平倉")
        self.assertFalse(cb.called)
        pend = self._real_pos(real_open_executed=None, real_open_pending_until=1.0, real_open_baseline=0.0)
        with mock.patch.object(ex, "get_position_info", return_value=(True, [])):
            self.eng._resolve_open_pending(pend)
        self.assertIsNot(pend.get("real_open_executed"), False, "空清單不能判定未成交")

    # 3a：基準查不到就不送單
    def test_t3a_baseline_query_failure_does_not_send(self):
        eng = self.eng
        with mock.patch.object(pt.risk_guard, "check", return_value=(True, None, None)), \
             mock.patch.object(ex, "resolve_position_mode", return_value=(False, None)), \
             mock.patch.object(ex, "set_margin_type", return_value=(True, {})), \
             mock.patch.object(ex, "set_leverage", return_value=(True, {})), \
             mock.patch.object(ex, "open_position", return_value=(True, {"avgPrice": "4391.2"})) as op, \
             mock.patch.object(ex, "get_position_info", return_value=(False, "Read timed out")) as gp, \
             mock.patch.object(eng, "_sync_backstop"):
            eng._position = None
            eng._open_position({"direction": "bullish", "bid": 4389.9, "ask": 4390.0,
                                "chan": {"reason": "t"}, "profile": {"reason": "t"}}, 4390.0, 10.0)
        self.assertTrue(gp.called, "前提：真的查了基準")
        self.assertEqual(op.call_count, 0, "基準查不到不能當成0繼續送")
        self.assertIs(eng._position.get("real_open_executed"), False)

    # 3b：基準跟著部位走完——數量比對、平倉確認、平倉數量都要扣
    def test_t3b_quantity_check_deducts_baseline(self):
        pos = self._real_pos(real_open_baseline=0.3)
        with mock.patch.object(ex, "get_position_info", return_value=(True, _rows(0.4))) as gp, \
             mock.patch.object(pt.db, "update_paper_trade_real_open") as upd:
            status = self.eng._check_exchange_quantity(pos)
        self.assertTrue(gp.called, "前提：真的比對了(測錯方式13：數量0.1是測試前就有的狀態，不能只看它)")
        self.assertEqual(status, "same", "前提：比對結果是『數量沒變』，不是『查不到』(測錯方式16)")
        self.assertFalse(upd.called, "0.4−基準0.3＝0.1，沒有減少，不能更新帳上數量")
        self.assertEqual(pos["real_open_quantity"], 0.1)
        self.assertFalse(any("減少" in a for a in self.eng.alerts), self.eng.alerts)
        with mock.patch.object(ex, "get_position_info", return_value=(True, _rows(0.35))):
            self.eng._check_exchange_quantity(pos)
        self.assertEqual(pos["real_open_quantity"], 0.05)

    def test_t3b_close_confirm_deducts_baseline(self):
        """自己的0.1已被停損、交易所只剩別人的0.3(=基準)：要判成已平倉，不能一直待平倉重送。"""
        pos = self._real_pos(real_open_baseline=0.3)
        with mock.patch.object(ex, "close_position", return_value=(False, "目前沒有未平倉部位可以平")), \
             mock.patch.object(ex, "get_position_info", return_value=(True, _rows(0.3))), \
             mock.patch.object(self.eng, "_cancel_backstop", return_value=False):
            self.eng._close_position(pos, 4380.0, "觸及停損")
        self.assertEqual(len(self.dbclose), 1)
        self.assertFalse(pos.get("pending_close"))

    def test_t7_close_order_deducts_baseline_and_never_touches_others(self):
        sent = []
        with mock.patch.object(ex, "is_enabled", return_value=True), \
             mock.patch.object(ex, "get_position_info", return_value=(True, _rows(0.3))), \
             mock.patch.object(ex, "place_market_order", side_effect=lambda side, q, **k: sent.append(q) or (True, {})):
            ok, msg = ex.close_position("bullish", account="gold", quantity=0.1, hedge=False, baseline=0.3)
        self.assertIn("基準", str(msg), "前提：不送的原因是『扣掉基準後沒有自己的』")
        self.assertFalse(ok)
        self.assertEqual(sent, [], "扣掉基準後沒有自己的部位，不能送單")
        with mock.patch.object(ex, "is_enabled", return_value=True), \
             mock.patch.object(ex, "get_position_info", return_value=(True, _rows(0.35))), \
             mock.patch.object(ex, "place_market_order", side_effect=lambda side, q, **k: sent.append(q) or (True, {})):
            ex.close_position("bullish", account="gold", quantity=0.1, hedge=False, baseline=0.3)
        self.assertEqual(sent, [0.05], "數量＝min(交易所−基準, 帳上)")

    # 3c：認領要在判斷還在場之前——走完整的每輪流程，不單獨呼叫認領(測錯方式9)
    def test_t3c_claim_then_same_round_checks_do_not_close(self):
        pos = self._real_pos(real_open_executed=None, real_open_quantity=0.1, real_open_pending_until=time.time() + 100,
                             real_open_baseline=0.0, backstop_algo_id=None, backstop_price=None)
        self.eng._qty_check_tick = QTY - 1
        def fake(method, path, params=None, account=None, return_status=False):
            return (True, [], 200) if return_status else (True, [])
        with mock.patch.object(ex, "get_position_info", return_value=(True, _rows(0.1))) as gp, \
             mock.patch.object(ex, "_signed_request", side_effect=fake), \
             mock.patch.object(ex, "place_algo_stop", return_value=(True, "C1", False)), \
             mock.patch.object(self.eng, "_close_position") as cp:
            self.eng._housekeeping(pos)
        self.assertTrue(gp.called)
        self.assertIs(pos.get("real_open_executed"), True, "前提：同一輪完成認領")
        self.assertEqual(pos.get("backstop_algo_id"), "C1", "認領後當場掛停損")
        self.assertFalse(cp.called, "剛認領的部位同一輪不能被判成已平倉")


# ------------------------------------------------------------------ r17 → r19
class Lesson19(ExecHarness):
    def _real_pos(self, **kw):
        p = _pos(real_open_quantity=0.1, entry_actual_price=4391.0, backstop_algo_id=None,
                 backstop_price=None, backstop_used_legacy=False, real_open_baseline=0.3)
        p.update(kw)
        self.eng._position = p
        return p

    # 2b：任何掛停損之前都要確認自己的部位還在(扣基準)
    def test_t2b_no_backstop_when_own_position_gone(self):
        pos = self._real_pos()
        with mock.patch.object(ex, "get_position_info", return_value=(True, _rows(0.3))) as gp, \
             mock.patch.object(ex, "place_algo_stop", return_value=(True, "X", False)) as pl:
            status = self.eng._sync_backstop(pos)
        self.assertTrue(gp.called, "前提：掛單前真的查了部位")
        self.assertEqual(status, "gone", "前提：確認的結果是『沒了』，不是『查不到』(測錯方式16)")
        self.assertEqual(pl.call_count, 0, "交易所只剩基準(別人的)0.3，不能掛孤兒reduce-only單")

    def test_t2b_query_failure_skips_round_without_counting_failure(self):
        pos = self._real_pos()
        with mock.patch.object(ex, "get_position_info", return_value=(False, "Read timed out")) as gp, \
             mock.patch.object(ex, "place_algo_stop") as pl:
            status = self.eng._sync_backstop(pos)
        self.assertTrue(gp.called, "前提：真的查了")
        self.assertEqual(status, "unknown", "前提：結果是『查不到』")
        self.assertEqual(pl.call_count, 0)
        self.assertEqual(self.eng.alerts, [], "查不到是這輪不動，不是掛單失敗")

    def test_t2b_quantity_is_min_of_exchange_minus_baseline_and_recorded(self):
        pos = self._real_pos()
        with mock.patch.object(ex, "get_position_info", return_value=(True, _rows(0.35))), \
             mock.patch.object(ex, "place_algo_stop", return_value=(True, "X", False)) as pl:
            self.eng._sync_backstop(pos)
        self.assertEqual(pl.call_args[0][1], 0.05)

    def test_t2b_moving_stop_also_confirms_position(self):
        """搬移停損也是掛一張新的reduce-only單，跟補掛一樣要先確認(r20)。"""
        pos = self._real_pos(backstop_algo_id="OLD", backstop_price=4370.0)
        with mock.patch.object(ex, "get_position_info", return_value=(True, _rows(0.3))) as gp, \
             mock.patch.object(ex, "place_algo_stop") as pl:
            status = self.eng._sync_backstop(pos)
        self.assertTrue(gp.called, "前提：搬移前真的查了部位")
        self.assertEqual(status, "gone", "前提：確認的結果是『沒了』")
        self.assertEqual(pl.call_count, 0)

    # 8a/8b：每一步各自try；出錯的步驟排在最前面(測錯方式11)
    def test_t8b_first_step_raises_later_steps_still_run(self):
        pos = self._real_pos(real_open_pending_until=time.time() + 100)
        self.eng._qty_check_tick = QTY - 1
        with mock.patch.object(self.eng, "_resolve_open_pending", side_effect=RuntimeError("inj-resolve")), \
             mock.patch.object(self.eng, "_check_exchange_quantity") as q, \
             mock.patch.object(self.eng, "_check_backstop_present") as g:
            self.eng._housekeeping(pos)
        self.assertTrue(q.called, "第一步出錯，數量比對仍要跑")
        self.assertTrue(g.called, "第一步出錯，停損守衛仍要跑")
        self.assertTrue(any("第 1 次" in a and "inj-resolve" in a for a in self.eng.alerts), self.eng.alerts)

    def test_t8a_housekeeping_error_does_not_stop_exit_judgement(self):
        """維護出錯不能讓整輪tick中止——後面的出場判斷(程式內停損)照樣要跑。"""
        class Reached(Exception):
            pass
        self._real_pos(real_open_executed=True)
        with mock.patch.object(self.eng, "_housekeeping", side_effect=RuntimeError("inj-hk")), \
             mock.patch.object(pt.settings_module, "get_settings", side_effect=Reached()):
            with self.assertRaises(Reached, msg="維護出錯後，流程要繼續走到下一步(讀設定、出場判斷)"):
                self.eng._tick()

    def test_t8a_orphan_retry_error_does_not_stop_tick(self):
        class Reached(Exception):
            pass
        self.eng._orphan_cancels = [{"algo_id": "Z", "used_legacy": False, "symbol": "XAUUSDT", "fail_count": 1, "last_error": ""}]
        self._real_pos()
        with mock.patch.object(self.eng, "_retry_orphan_cancels", side_effect=RuntimeError("inj-orphan")), \
             mock.patch.object(self.eng, "_housekeeping") as hk, \
             mock.patch.object(pt.settings_module, "get_settings", side_effect=Reached()):
            with self.assertRaises(Reached):
                self.eng._tick()
        self.assertTrue(hk.called)

    # 8c：出錯次數照節奏、恢復要通知、部位結束時清掉
    def test_t8c_step_error_recovers_and_is_cleared_at_position_end(self):
        pos = self._real_pos()
        self.eng._qty_check_tick = QTY - 1
        with mock.patch.object(self.eng, "_check_exchange_quantity", side_effect=RuntimeError("inj-qty")), \
             mock.patch.object(self.eng, "_check_backstop_present"):
            self.eng._housekeeping(pos)
        self.eng._qty_check_tick = QTY - 1
        with mock.patch.object(self.eng, "_check_exchange_quantity"), mock.patch.object(self.eng, "_check_backstop_present"):
            self.eng._housekeeping(pos)
        self.assertTrue(any("恢復" in a for a in self.eng.alerts[1:]), self.eng.alerts)
        self.eng._qty_check_tick = QTY - 1
        with mock.patch.object(self.eng, "_check_exchange_quantity", side_effect=RuntimeError("inj-qty2")), \
             mock.patch.object(self.eng, "_check_backstop_present"):
            self.eng._housekeeping(pos)
        before = len(self.eng.alerts)
        with mock.patch.object(ex, "close_position", return_value=(True, {"avgPrice": "4380"})), \
             mock.patch.object(self.eng, "_cancel_backstop", return_value=False):
            self.eng._close_position(pos, 4380.0, "觸及停損")
        self.assertEqual(self.eng._step_errors, {}, "部位結束，出錯次數要清掉(同幣下一筆不能接著數)")
        self.assertTrue(any("結束" in a for a in self.eng.alerts[before:]), self.eng.alerts[before:])

    # 2a：啟動對帳遇空清單，不能回報成「一致(空手)」
    def test_t2a_reconcile_empty_list_reports_unknown(self):
        import app.main as m
        class E:
            label = "15分K"; execution_index = 3; execution_account = "gold"; _position = None
        got = []
        with mock.patch.dict(m.PAPER_TRADING_ENGINES, {"x": E()}, clear=True), mock.patch("time.sleep"), \
             mock.patch.object(ex, "get_position_info", return_value=(True, [])) as gp, \
             mock.patch.object(ex, "usdt_balance_line", return_value=None), \
             mock.patch.object(m.logger, "info", side_effect=got.append), mock.patch.object(m.db, "insert_settings_audit"):
            m._reconcile_with_exchange_on_startup()
        self.assertTrue(gp.called)
        self.assertIn("查不到", got[-1].splitlines()[1])


# ------------------------------------------------------------------ r20 → r22
class Lesson22(ExecHarness):
    def _real_pos(self, **kw):
        p = _pos(real_open_quantity=0.1, entry_actual_price=4391.0, backstop_algo_id="B1",
                 backstop_price=4380.0, backstop_used_legacy=False, real_open_baseline=0.0)
        p.update(kw)
        self.eng._position = None  # 已被claim
        return p

    # 8c：結帳之後出錯——已經做完的不可逆動作不能被當成沒做
    def test_t8c_error_after_close_confirmed_is_not_restored(self):
        pos = self._real_pos()
        def close_then_fail(*a, **k):
            self.dbclose.append(a)            # 帳已經寫進去了(不可逆)
            raise RuntimeError("inj-after")   # 之後的步驟才出錯
        with mock.patch.object(ex, "close_position", return_value=(True, {"avgPrice": "4379.4"})) as cp, \
             mock.patch.object(self.eng, "_cancel_backstop", return_value=False) as cb, \
             mock.patch.object(pt.db, "close_paper_trade", side_effect=close_then_fail):
            self.eng._close_position(pos, 4380.0, "觸及停損")
        self.assertEqual(cp.call_count, 1, "前提：平倉單送出並成交")
        self.assertEqual(cb.call_count, 1, "前提：停損已撤")
        self.assertEqual(len(self.dbclose), 1, "前提：帳已經結了")
        self.assertIsNone(self.eng._position, "已結帳的部位不能被放回帳上")
        self.assertFalse(pos.get("pending_close"), "已結帳的部位不能被記成待平倉")
        self.assertTrue(any("inj-after" in a for a in self.eng.alerts), self.eng.alerts)

    # 8c：結帳之前出錯——部位不能被弄丟
    def test_t8c_error_before_confirmation_keeps_position(self):
        pos = self._real_pos()
        with mock.patch.object(ex, "close_position", return_value=(False, "Read timed out")) as cp, \
             mock.patch.object(self.eng, "_own_qty", side_effect=RuntimeError("inj-before")), \
             mock.patch.object(self.eng, "_cancel_backstop") as cb:
            self.eng._close_position(pos, 4380.0, "觸及停損")
        self.assertEqual(cp.call_count, 1, "前提：平倉單送出、沒成交")
        self.assertIs(self.eng._position, pos, "沒確認平掉就出錯，部位要放回帳上")
        self.assertTrue(pos.get("pending_close"), "並記成待平倉、每輪重試")
        self.assertFalse(cb.called, "沒確認平掉不能撤停損")
        self.assertEqual(self.dbclose, [])
        self.assertTrue(any("inj-before" in a and "第 1 次" in a for a in self.eng.alerts), self.eng.alerts)

    def test_t8c_exit_price_none_still_closes(self):
        pos = self._real_pos()
        with mock.patch.object(ex, "close_position", return_value=(True, {"avgPrice": "4379.4"})), \
             mock.patch.object(self.eng, "_cancel_backstop", return_value=False), \
             mock.patch.object(pt, "_latest_price", return_value=None):
            self.eng._close_position(pos, None, "交易所端部位已不在")
        self.assertEqual(len(self.dbclose), 1)

    # 8a：快速停損在背景執行緒送平倉，出錯要計數推播(不能沒人接)
    def test_t8a_fast_stop_thread_error_is_counted(self):
        pos = self._real_pos()
        self.eng._position = pos
        class SyncThread:
            def __init__(self, target=None, args=(), kwargs=None, daemon=None):
                self.t, self.a, self.k = target, args, kwargs or {}
            def start(self):
                self.t(*self.a, **self.k)
        with mock.patch.object(pt.threading, "Thread", SyncThread), \
             mock.patch.object(self.eng, "_close_position", side_effect=RuntimeError("inj-fast")) as cl:
            self.eng._fast_stop_check(4379.0, 4379.2)
        self.assertEqual(cl.call_count, 1, "前提：快速停損真的觸發了平倉")
        self.assertTrue(any("inj-fast" in a and "第 1 次" in a for a in self.eng.alerts), self.eng.alerts)

    # 8d：逐筆重試，壞掉的那筆排在前面(測錯方式11)
    def test_t8d_broken_orphan_item_does_not_stop_the_rest(self):
        self.eng._orphan_cancels = [
            {"algo_id": "BAD"},  # 缺欄位
            {"algo_id": "GOOD", "used_legacy": False, "symbol": "XAUUSDT", "fail_count": 1, "last_error": ""},
        ]
        with mock.patch.object(ex, "cancel_algo_stop", return_value=(True, {}, False)) as cc:
            self.eng._retry_orphan_cancels()
        self.assertIn("GOOD", [c.args[0] for c in cc.call_args_list], "壞掉那筆後面的要照樣處理")
        self.assertEqual([i.get("algo_id") for i in self.eng._orphan_cancels], ["BAD"], "壞掉那筆要留在清單，不能丟掉")
        self.assertTrue(any("BAD" in a and "資料有問題" in a for a in self.eng.alerts),
                        f"前提：壞資料真的讓那一筆走到例外處理(用法第5點r28)，不是走一般的撤單失敗：{self.eng.alerts}")

    # 2c：手上有正向證據時，直接用已知數量掛停損，不重查
    def test_t2c_open_fill_places_stop_with_known_qty_even_if_exchange_lags(self):
        pos = self._real_pos(backstop_algo_id=None, backstop_price=None)
        with mock.patch.object(ex, "get_position_info", return_value=(True, _rows(0))) as gp, \
             mock.patch.object(ex, "place_algo_stop", return_value=(True, "N1", False)) as pl:
            self.eng._place_backstop(pos, 0.1, False)
        self.assertEqual(pl.call_count, 1, "剛成交的回應就是部位在的證據，交易所還沒反映也要掛")
        self.assertEqual(pl.call_args[0][1], 0.1)
        self.assertFalse(gp.called, "有正向證據時不重查")

    def test_t2c_claim_places_stop_with_claimed_qty_without_requery(self):
        pos = self._real_pos(real_open_executed=None, real_open_pending_until=time.time() + 100,
                             backstop_algo_id=None, backstop_price=None)
        # 綁在被測那一步(測錯方式18)：認領前交易所看得到0.1；認領之後再查會拿到0(模擬還沒反映)
        rows = lambda *a, **k: (True, _rows(0.1 if pos.get("real_open_executed") is None else 0))
        with mock.patch.object(ex, "get_position_info", side_effect=rows) as gp, \
             mock.patch.object(ex, "place_algo_stop", return_value=(True, "N2", False)) as pl:
            self.eng._resolve_open_pending(pos)
        self.assertIs(pos.get("real_open_executed"), True, "前提：認領成功")
        self.assertEqual(gp.call_count, 1, "認領那一列就是證據，掛停損不重查")
        self.assertEqual(pl.call_count, 1)


# ------------------------------------------------------------------ r23 → r25
class Lesson25(ExecHarness):
    def _real_pos(self, **kw):
        p = _pos(real_open_quantity=0.1, entry_actual_price=4391.0, backstop_algo_id="B1",
                 backstop_price=4380.0, backstop_used_legacy=False, real_open_baseline=0.0)
        p.update(kw)
        return p

    # 第2條：停損守衛每個出口都回傳原因
    def test_t2_guard_every_exit_returns_reason(self):
        eng = self.eng
        def run(pos, algo_resp, rounds=1):
            def fake(method, path, params=None, account=None, return_status=False):
                r = (True, _rows(0.1), 200) if path == "/fapi/v2/positionRisk" else algo_resp
                return r if return_status else r[:2]
            out = []
            with mock.patch.object(ex, "_signed_request", side_effect=fake), \
                 mock.patch.object(ex, "place_algo_stop", return_value=(True, "R1", False)):
                for _ in range(rounds):
                    out.append(eng._check_backstop_present(pos))
            return out
        self.assertEqual(run(self._real_pos(backstop_algo_id=None), (True, [], 200)), ["skip"])
        self.assertEqual(run(self._real_pos(), (False, {"code": -1001}, 500)), ["unknown"])
        self.assertEqual(run(self._real_pos(), (True, [{"algoId": "B1"}], 200)), ["present"])
        self.run_guard = run

    def test_t2_guard_replace_exit_returns_placement_result(self):
        """獨立成一個情境(用法第5點r28)：只有這個出口會查部位，不跟上面三個不查部位的出口共用命中次數。"""
        self.test_t2_guard_every_exit_returns_reason()
        self.assertEqual(self.run_guard(self._real_pos(), (True, [], 200), 3), ["missing", "missing", "replaced:placed"])

    # 第8條r24：結帳排最前面；算損益不能丟例外
    def test_t8a_record_is_written_before_cleanup_and_pnl_error_does_not_block(self):
        pos = self._real_pos()
        pos.pop("entry_price")                        # 算損益會缺欄位
        order = []
        with mock.patch.object(ex, "close_position", return_value=(True, {"avgPrice": "4379.4"})) as cp, \
             mock.patch.object(pt.db, "close_paper_trade", side_effect=lambda *a, **k: order.append(("db", a))), \
             mock.patch.object(self.eng, "_cancel_backstop", side_effect=lambda p: order.append(("cancel",)) or False):
            self.eng._close_position(pos, 4380.0, "觸及停損")
        self.assertEqual(cp.call_count, 1, "前提：平倉單送出並成交")
        self.assertEqual([o[0] for o in order], ["db", "cancel"], "先寫紀錄、移出帳，撤停損在後")
        self.assertIsNone(order[0][1][4], "缺欄位時損益記為未知，不能丟例外")

    def test_t8a_cleanup_error_after_record_does_not_stop_other_cleanup(self):
        pos = self._real_pos()
        with mock.patch.object(ex, "close_position", return_value=(True, {"avgPrice": "4379.4"})), \
             mock.patch.object(self.eng, "_end_position_step_errors", side_effect=RuntimeError("inj-end")), \
             mock.patch.object(self.eng, "_cancel_backstop", return_value=False) as cb:
            self.eng._close_position(pos, 4380.0, "觸及停損")
        self.assertEqual(len(self.dbclose), 1, "前提：帳已經結了")
        self.assertEqual(cb.call_count, 1, "收尾的一步出錯，撤停損照樣要做")
        self.assertIsNone(self.eng._position)
        self.assertFalse(pos.get("pending_close"))
        self.assertTrue(any("inj-end" in a for a in self.eng.alerts), self.eng.alerts)
        self.assertTrue(any(n.get("action") == "close" for n in self.notes), "收尾出錯，平倉通知照樣要發")

    # 第8條r23/r24：背景迴圈本身出錯要推播
    def test_t8c_background_loop_error_is_pushed(self):
        with mock.patch.object(self.eng, "_tick", side_effect=RuntimeError("inj-loop")):
            self.eng._loop_once()
        self.assertTrue(any("inj-loop" in a and "第 1 次" in a for a in self.eng.alerts), self.eng.alerts)

    # 第8條r24：網頁交易入口出錯要回錯誤給網頁、並推播
    def test_t8b_web_trade_entry_error_returns_error_and_pushes(self):
        import asyncio
        import app.main as m
        pushed = []
        with mock.patch.object(m.settings_module, "verify_password", return_value=(True, None)), \
             mock.patch.object(ex, "is_enabled", return_value=True, create=True), \
             mock.patch.object(ex, "close_position", side_effect=RuntimeError("inj-web")) as cp, \
             mock.patch.object(m.notifier, "send_raw_message", side_effect=pushed.append):
            res = asyncio.run(m.execution_test_close({"password": "x", "direction": "bullish", "account": "gold"}))
        self.assertTrue(cp.called, "前提：真的走到了送平倉單那一步")
        self.assertIn("inj-web", str(res.get("error")), res)
        self.assertTrue(any("inj-web" in t for t in pushed), pushed)


class NotifierFormat(unittest.TestCase):
    """損益未知時通知不能格式化失敗(第8條r24)。其他測試都把通知mock掉，這一段要真的跑格式化。"""
    def test_close_notice_with_unknown_pnl_is_sent(self):
        from app.notifier import notifier as N, TelegramNotifier
        sent = []
        with mock.patch.object(TelegramNotifier, "is_enabled", new_callable=mock.PropertyMock, return_value=True), \
             mock.patch.object(TelegramNotifier, "is_muted", new_callable=mock.PropertyMock, return_value=False), \
             mock.patch.object(N, "_send_telegram_message", side_effect=lambda t: sent.append(t) or (True, None)):
            N.notify_trade_event(action="close", label="15分K", direction="bullish", price=4380.0,
                                 exit_reason="觸及停損", pnl_points=None, executed=True, quantity=0.1)
        self.assertEqual(len(sent), 1, f"通知要送出：{sent}")
        self.assertIn("損益：未知", sent[0])

    # 放在這個不mock通知的情境：ExecHarness在setUp裡把notify_trade_event整個mock掉，格式化根本不會執行
    # 8c：估算不能把未知包裝成已知(第8條r28)
    def test_t8c_close_notice_does_not_estimate_usd_from_paper_points(self):
        from app.notifier import notifier as N, TelegramNotifier
        sent = []
        with mock.patch.object(TelegramNotifier, "is_enabled", new_callable=mock.PropertyMock, return_value=True), \
             mock.patch.object(TelegramNotifier, "is_muted", new_callable=mock.PropertyMock, return_value=False), \
             mock.patch.object(N, "_send_telegram_message", side_effect=lambda t: sent.append(t) or (True, None)):
            N.notify_trade_event(action="close", label="15分K", direction="bullish", price=4380.0, exit_reason="觸及停損",
                                 pnl_points=-2.0, executed=True, quantity=0.1, real_pnl_usd=None)
        self.assertEqual(len(sent), 1, "前提：通知真的組出來、送出了")
        self.assertIn("-2.00 points", sent[0], "前提：模擬點數照常顯示")
        self.assertNotIn("-0.20 USDT", sent[0], "真實成交價查不到，不能用模擬點數×張數估出一個USDT")
        self.assertIn("USDT 損益未知", sent[0])



# ------------------------------------------------------------------ r26 → r28
class Lesson28(ExecHarness):
    # 8b：統計——損益未知不算勝負，另外計數
    def test_t8b_stats_unknown_pnl_is_neither_win_nor_loss(self):
        from app import trading_stats as TS
        trades = [{"entry_time": "2026-09-21T01:00:00+00:00", "pnl_points": 5.0, "direction": "bullish"},
                  {"entry_time": "2026-09-21T02:00:00+00:00", "pnl_points": -3.0, "direction": "bullish"},
                  {"entry_time": "2026-09-21T03:00:00+00:00", "pnl_points": None, "direction": "bullish"}]
        st = TS.compute_stats(trades)
        self.assertEqual(st["win_rate"], 50.0, "1勝1負；損益未知那筆不能算成虧損拉低勝率")
        self.assertEqual(st.get("unknown_pnl_trades"), 1, "損益未知的筆數要另外計")

    def test_t8b_risk_guard_unknown_pnl_not_counted_as_loss(self):
        from app import risk_guard as RG
        class Eng:
            _closed_trades_memory = [{"exit_time": "2026-09-21T01:00:00+00:00", "pnl_points": -1.0},
                                     {"exit_time": "2026-09-21T02:00:00+00:00", "pnl_points": None}]
            engine_id = "x"
        with mock.patch.object(RG.db, "is_enabled", return_value=False):
            n = RG.get_consecutive_losses(Eng())
            unknown = RG.get_unknown_pnl_count(Eng())
        self.assertEqual(n, 1, "最新那筆損益未知，不能算成一筆虧損")
        self.assertEqual(unknown, 1)

    def test_t8c_partial_reduction_pnl_is_unknown_not_mark_estimate(self):
        pos = _pos(real_open_quantity=0.2, entry_actual_price=4391.0, real_open_baseline=0.0)
        self.eng._position = pos
        with mock.patch.object(ex, "get_position_info", return_value=(True, _rows(0.1, mark=4396.0))):
            status = self.eng._check_exchange_quantity(pos)
        self.assertEqual(status, "reduced", "前提：偵測到數量減少")
        self.assertTrue(pos.get("partial_pnl_unknown"), "減少那部分的成交價不知道，損益要記未知")
        self.assertFalse(any("0.50" in a or "估算損益" in a for a in self.eng.alerts), f"不能用標記價估：{self.eng.alerts}")
        self.assertTrue(any("未知" in a for a in self.eng.alerts), self.eng.alerts)

    def test_t8c_real_usd_summary_uses_only_actual_fills(self):
        from app import trading_stats as TS
        trades = [{"real_open_executed": True, "real_pnl_usd": 1.5},
                  {"real_open_executed": True, "real_pnl_usd": None, "pnl_points": 30.0},  # 缺成交價：未知
                  {"real_open_executed": False, "real_pnl_usd": None, "pnl_points": 99.0}]  # 純模擬：不算
        s = TS.real_usd_summary(trades)
        self.assertEqual((s["total"], s["known"], s["unknown"]), (1.5, 1, 1))


class StaticChecks(unittest.TestCase):
    """全域／靜態檢查自成一個情境(用法第5點r25)：不放在別的情境最後，才不會繼承那個情境的突變命中次數。"""
    RETURN_FUNCS = ("_check_backstop_present", "_sync_backstop", "_check_exchange_quantity",
                    "_resolve_open_pending", "_retry_pending_close")

    @staticmethod
    def return_problems(fn_ast):
        """回傳原因檢查(第2條r25/r27/r28)：每個return帶值(明寫return None也算沒帶)；最後不能掉出函式。"""
        import ast
        out = []
        for n in ast.walk(fn_ast):
            if isinstance(n, ast.Return) and (n.value is None or (isinstance(n.value, ast.Constant) and n.value.value is None)):
                out.append(f"第{n.lineno}行不帶值")
        def ends(stmts):
            last = stmts[-1]
            if isinstance(last, (ast.Return, ast.Raise)):
                return True
            if isinstance(last, ast.If):
                return bool(last.orelse) and ends(last.body) and ends(last.orelse)
            if isinstance(last, ast.Try):
                return ends(last.body) and all(ends(h.body) for h in last.handlers)
            return False
        if not ends(fn_ast.body):
            out.append("最後會掉出函式")
        return out

    def test_return_checker_selftest(self):
        import ast
        cases = [("def f():\n    return 1", False), ("def f():\n    if x:\n        return\n    return 1", True),
                 ("def f():\n    return None", True), ("def f():\n    if x:\n        return 1", True),
                 ("def f():\n    if x:\n        return 1\n    else:\n        return 2", False),
                 ("def f():\n    try:\n        return 1\n    except E:\n        pass", True)]
        for src, bad in cases:
            self.assertEqual(bool(self.return_problems(ast.parse(src).body[0])), bad, src)

    def test_every_return_in_guard_functions_carries_a_value(self):
        import ast, inspect, textwrap
        for fn in self.RETURN_FUNCS:
            tree = ast.parse(textwrap.dedent(inspect.getsource(getattr(pt.PaperTradingEngine, fn)))).body[0]
            n_returns = sum(1 for n in ast.walk(tree) if isinstance(n, ast.Return))
            self.assertGreater(n_returns, 1, f"前提：{fn} 真的解析到了多個出口(第19種：什麼都沒掃到時也會通過)")
            self.assertEqual(self.return_problems(tree), [], f"{fn}(第2條r25/r27)")


class Lesson14(unittest.TestCase):
    def test_t14_pyflakes_no_undefined_names(self):
        import subprocess, sys, os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        # 金絲雀：一個一定有未定義名稱的檔，pyflakes必須報出來(第19種：工具沒真的在查時會空跑)
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as cf:
            cf.write("def f():\n    return undefined_canary_name\n")
        canary = subprocess.run([sys.executable, "-m", "pyflakes", cf.name], capture_output=True, text=True).stdout
        self.assertIn("undefined_canary_name", canary, "前提：pyflakes真的抓得到未定義名稱")
        r = subprocess.run([sys.executable, "-m", "pyflakes", "app", "tests", "scripts"], cwd=root, capture_output=True, text=True)
        self.assertNotIn("unable to detect", r.stdout, "有檔案用了import *，pyflakes在那些檔上完全失效(r28)")
        out = r.stdout
        # 前提(靜態檢查抓到只有否定句)：pyflakes真的跑了、真的掃到了程式。沒裝pyflakes時輸出是空的，
        # 「沒有undefined name」就會空跑通過。main.py那一條已知的誤報可以當證據。
        self.assertNotIn("No module named", r.stderr, "前提：pyflakes有安裝")
        self.assertIn("undefined name 'fastapi'", out, "前提：真的掃到了app/main.py")
        bad = [l for l in out.splitlines() if "undefined name" in l and "'fastapi'" not in l]
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()
