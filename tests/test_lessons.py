"""
BINANCE_LESSONS.md 對照測試(清單第5點的做法：先在修改前的程式上跑、確認會失敗，再修改到全部通過)。
涵蓋 r7→r54 第1、2、3、7、8、14、15條的每一個檢查項目。

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
import json
import unittest
from unittest import mock

logging.disable(logging.CRITICAL)

from app import execution as ex, alert_cadence as ac  # noqa: E402
from app import paper_trading as pt  # noqa: E402
import time  # noqa: E402
QTY = getattr(pt, "QTY_CHECK_EVERY_TICKS", 4)

# r74：背景補登的執行緒，框架預設一律不真的開——收下來，要跑的測試自己拿出來跑。不靠每個測試類別記得換掉
# (pump-dump-hunter 對照 r73：舊測試裡補登執行緒真的在背景跑、打網路，把「未知」補成已知)。
# 真的那個存起來，只給驗證正式路徑的那一項用(tests.error_scan 會數有沒有別的測試真的開了)
_REAL_BACKFILL_START = getattr(pt, "_start_backfill_thread", None)
_BACKFILL_DEFAULT = []
if _REAL_BACKFILL_START is not None:
    pt._start_backfill_thread = _BACKFILL_DEFAULT.append

ONE_WAY_ROWS = [{"symbol": "XAUUSDT", "positionSide": "BOTH", "positionAmt": "0.1"}]


def pick(alerts, title):
    """第21種：只看第一行(標題)含title的那幾則，不在全部通知裡找字——別的通知剛好有同樣的字就會空跑。"""
    return [a for a in alerts if title in a.splitlines()[0]]


def one(alerts, title):
    """恰好一則標題含title的通知，回傳它(0則或多則都算失敗)。"""
    got = pick(alerts, title)
    if len(got) != 1:
        raise AssertionError(f"預期恰好1則「{title}」，實際{len(got)}則：{[x.splitlines()[0] for x in alerts]}")
    return got[0]


import copy as _copy
import importlib as _importlib
import pkgutil as _pkgutil
import app as _app_pkg
# app/ 底下所有模組都納入(r48：沒列進清單的模組就不會被還原——不靠列舉)
_STATE_MODULES = tuple(f"app.{m.name}" for m in _pkgutil.iter_modules(_app_pkg.__path__))
# 匯入時把每個模組層級(底線開頭)的 dict／list／set 存一份初始值，_reset_module_state 一律還原(用法第5點r44)：
# 不用記得「新增的狀態要加進重設」——r38、r41 都漏過
_STATE_SNAPSHOT = []
for _mn in _STATE_MODULES:
    _mod = _importlib.import_module(_mn)
    for _k, _v in list(vars(_mod).items()):
        if _k.startswith("_") and not _k.startswith("__") and isinstance(_v, (dict, list, set)):
            _STATE_SNAPSHOT.append((_mod, _k, _copy.deepcopy(_v)))


def _reset_module_state():
    for _mod, _k, _init in _STATE_SNAPSHOT:
        cur = getattr(_mod, _k)
        cur.clear()
        (cur.update(_copy.deepcopy(_init)) if isinstance(cur, (dict, set)) else cur.extend(_copy.deepcopy(_init)))
    _reset_named_state()


def _reset_named_state():
    """
    模組層級、會被測試改到的狀態(第14種)。gold-scalper 沒有狀態檔(狀態都在資料庫，測試時資料庫是關的)，
    所以沒有「前一個情境留在磁碟上」的問題(r40)；要重設的是記憶體裡的這些。在舊版程式上重跑時不存在的就略過。
    """
    import app.main as m
    from app.notifier import notifier as N
    for obj, attr in ((pt.db, "_db_fail_counts"), (m, "_web_trade_errors"), (N, "errors")):
        if hasattr(obj, attr):
            getattr(obj, attr).clear()
    if hasattr(N, "_unconfigured_recorded"):
        N._unconfigured_recorded = False
    S = pt.settings_module
    for k, v in (("_load_fail_count", 0), ("_last_load_attempt", 0.0)):
        if hasattr(S, k):
            setattr(S, k, v)


def _engine():
    eng = next(iter(pt.PAPER_TRADING_ENGINES.values()))
    eng.alerts = []
    eng._backstop_alert = lambda t: eng.alerts.append(t)
    eng._orphan_cancels = []
    eng._step_errors = {}      # 前一個測試留下的出錯次數不能帶進來
    eng._qty_check_tick = 0
    eng._position = None
    eng._backfill_rescanned = False   # r76：「每個行程只重新排一次」是實例狀態，引擎在測試間共用，前一個測試留下的True不能帶進來
    _reset_module_state()
    _BACKFILL_DEFAULT.clear()
    eng._seeded_from_db = True   # r37起「持倉紀錄沒載入就不開倉」：前一個測試模擬讀不到時留下的False不能帶進來
    # 模組層級的「記住的持倉模式」也會被測試改到。在舊版程式上重跑時它可能還不存在(r10才加)，
    # 不存在就略過，不要讓框架本身崩掉(用法第5點r34)
    if hasattr(ex, "_last_known_hedge"):
        ex._last_known_hedge.clear()
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
        self.assertEqual(len(sent), 1, "前提：真的重送了一次")  # 先確認有東西才索引(用法第5點r34：突變下要明確失敗、不是測試本身崩掉)
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
        eng._position = pos   # 部位要在帳上：r53 起會送單的函式先確認傳進來的是帳上那一筆
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
        eng._position = pos   # 部位要在帳上：r53 起會送單的函式先確認傳進來的是帳上那一筆
        with mock.patch.object(ex, "place_algo_stop", side_effect=RuntimeError("boom")):
            eng._sync_backstop(pos)
        self.assertEqual(pos.get("backstop_fail_count"), 1)
        self.assertEqual(len(eng.alerts), 1)
        self.assertIn("第 1 次", eng.alerts[0])
        self.assertIn("boom", eng.alerts[0])

    def test_t8c1_stale_cancel_failure_during_move_is_counted_at_failure(self):
        """搬移時舊單撤不掉：要在撤不掉的當下計數並告警第1次，不是等平倉才開始算。"""
        eng, pos = _engine(), _pos(backstop_algo_id="OLD", backstop_price=4370.0)
        eng._position = pos   # 部位要在帳上：r53 起會送單的函式先確認傳進來的是帳上那一筆
        with mock.patch.object(ex, "place_algo_stop", return_value=(True, "NEW", False)), \
             mock.patch.object(ex, "cancel_algo_stop", return_value=(False, {"code": -1001}, False)):
            eng._sync_backstop(pos)
        self.assertEqual([(i["algo_id"], i["fail_count"]) for i in eng._orphan_cancels], [("OLD", 1)])
        a = one(eng.alerts, "多餘的停損單撤不掉"); self.assertIn("第 1 次", a); self.assertIn("-1001", a)

    def test_t8c2_failure_state_cleared_by_close_sends_notice(self):
        """backstop失敗中、部位從別的路徑平掉(失敗狀態消失)：要發通知收尾，不能讓使用者等不到結果。"""
        eng, pos = _engine(), _pos()
        eng._position = pos   # 部位要在帳上：r53 起會送單的函式先確認傳進來的是帳上那一筆
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


def _rows(qty, entry=4391.0, side="BOTH", mark=4395.0):
    """模擬positionRisk：單向空單positionAmt是負數、雙向回LONG/SHORT列(用法第5點)。"""
    return [{"symbol": "XAUUSDT", "positionSide": side, "positionAmt": str(qty),
             "entryPrice": str(entry), "markPrice": str(mark)}]


class ExecHarness(unittest.TestCase):
    """真實下單引擎的共用環境：通知、資料庫、交易所都攔截，記錄呼叫。"""
    def setUp(self):
        self.eng = _engine()
        self.notes = []
        self.dbclose = []
        self.dbclose_kw = []
        st = _exec_settings(self.eng)
        self.ps = [
            mock.patch.object(self.eng, "_is_execution_engine", return_value=True),
            mock.patch.object(pt.settings_module, "get_settings", return_value=st),
            mock.patch.object(pt.notifier_module.notifier, "notify_trade_event", side_effect=lambda **k: self.notes.append(k)),
            mock.patch.object(pt.db, "close_paper_trade", side_effect=lambda *a, **k: (self.dbclose.append(a), self.dbclose_kw.append(k))),
            mock.patch.object(ex, "current_hedge_mode", return_value=False),
            mock.patch.object(ex, "round_price", lambda p, *a, **k: p),
            # 成交明細預設「查不到」，要用的情境自己換(不打網路)
            mock.patch.object(ex, "get_user_trades", return_value=(False, "未模擬"), create=True),
            # 成交價背景補登(r71)：不真的開執行緒，收下來由測試自己跑(不會在測試結束後還去打網路)
            mock.patch.object(pt, "_start_backfill_thread", side_effect=lambda fn: self.backfills.append(fn), create=True),
        ]
        self.backfills = []
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
        # 設定要0.2張、交易所實際0.1張；訊號價4390、交易所均價4391.0(測錯方式1：兩個來源不同值)
        pos, sync = self._open((False, "Read timed out"), before=(True, _rows(0)), after=(True, _rows(0.1)))
        self.assertEqual(self.inj, ["before", "after"], "前提：基準在送單前查、認領在送單後查(注入觸發時機)")
        self.assertIs(pos.get("real_open_executed"), True)
        self.assertEqual(pos.get("real_open_quantity"), 0.1)
        self.assertEqual(pos.get("entry_actual_price"), 4391.0)
        self.assertTrue(sync.called, "認領後要當場掛停損")
        self.assertGreater(len(self.notes), 0, "前提：開倉通知有送出")  # 先確認有東西才索引(用法第5點r34：突變下要明確失敗、不是測試本身崩掉)
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
        one(eng.alerts, "判定未成交")

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
        a = one(self.eng.alerts, "平倉單沒有成交"); self.assertIn("第 1 次", a); self.assertIn("-2019", a)

    def test_t8a_timeout_but_gone_on_exchange_is_closed_without_failure_alert(self):
        pos = self._pos()
        with mock.patch.object(ex, "close_position", return_value=(False, "Read timed out")), \
             mock.patch.object(ex, "get_position_info", return_value=(True, _rows(0))), \
             mock.patch.object(self.eng, "_cancel_backstop", return_value=False):
            self.eng._close_position(pos, 4380.0, "觸及停損")
        self.assertEqual(len(self.dbclose), 1)
        self.assertIsNone(self.eng._position)
        self.assertGreater(len(self.notes), 0, "前提：平倉通知有送出")  # 先確認有東西才索引(用法第5點r34：突變下要明確失敗、不是測試本身崩掉)
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
        self.assertIn("目前交易所部位：0.1", one(self.eng.alerts, "平倉單沒有成交"),
                      "前提：確認的結果是『部位還在0.1』，不是『查不到』(測錯方式16：兩者都會進待平倉)")
        with mock.patch.object(ex, "close_position", return_value=(True, {"avgPrice": "4379.4"})), \
             mock.patch.object(ex, "get_position_info", return_value=(True, _rows(0.1))), \
             mock.patch.object(self.eng, "_cancel_backstop", return_value=False), \
             mock.patch.object(pt, "_latest_price", return_value=4379.5, create=True):
            self.eng._retry_pending_close()   # 下一輪tick直接重試，不等下一根K棒的出場訊號
        self.assertEqual(len(self.dbclose), 1)
        self.assertIsNone(self.eng._position)
        one(self.eng.alerts, "平倉已完成(失敗 1 次後恢復)")

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
        self.assertIn("0.2 → 0.1", one(self.eng.alerts, "交易所部位數量減少"))


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
        self.assertIsNotNone(eng._position, "前提：帳面部位有開(沒開成時要是斷言失敗、不是測試崩掉)")
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
        self.assertEqual(pick(self.eng.alerts, "交易所部位數量減少"), [])
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
        self.assertEqual(pl.call_count, 1, "前提：真的掛了停損")  # 先確認有東西才索引(用法第5點r34：突變下要明確失敗、不是測試本身崩掉)
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
        a = one(self.eng.alerts, "每輪步驟「開倉確認」出錯"); self.assertIn("第 1 次", a); self.assertIn("inj-resolve", a)

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
        one(self.eng.alerts, "每輪步驟「數量比對」已恢復")
        self.eng._qty_check_tick = QTY - 1
        with mock.patch.object(self.eng, "_check_exchange_quantity", side_effect=RuntimeError("inj-qty2")), \
             mock.patch.object(self.eng, "_check_backstop_present"):
            self.eng._housekeeping(pos)
        before = len(self.eng.alerts)
        with mock.patch.object(ex, "close_position", return_value=(True, {"avgPrice": "4380"})), \
             mock.patch.object(self.eng, "_cancel_backstop", return_value=False):
            self.eng._close_position(pos, 4380.0, "觸及停損")
        self.assertEqual(self.eng._step_errors, {}, "部位結束，出錯次數要清掉(同幣下一筆不能接著數)")
        self.assertIn("數量比對", one(self.eng.alerts[before:], "部位已結束，每輪步驟的出錯狀態結束"))

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
        self.assertGreater(len(got), 0, "前提：對帳訊息有寫出來")
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
        self.assertIn("inj-after", one(self.eng.alerts, "平倉收尾「寫平倉紀錄」出錯"))

    # 8c：結帳之前出錯——部位不能被弄丟
    def test_t8c_error_before_confirmation_keeps_position(self):
        pos = self._real_pos()
        sent = []
        def own_qty(position):
            if not sent:
                return False, 0.0          # 送單前的確認：查不到，照樣送(r54)
            raise RuntimeError("inj-before")  # 平倉單送出、沒成交之後的確認才出錯(測錯方式18：綁在被測那一步)
        def close_(*a, **k):
            sent.append(1)
            return False, "Read timed out"
        with mock.patch.object(ex, "close_position", side_effect=close_) as cp, \
             mock.patch.object(self.eng, "_own_qty", side_effect=own_qty), \
             mock.patch.object(self.eng, "_cancel_backstop") as cb:
            self.eng._close_position(pos, 4380.0, "觸及停損")
        self.assertEqual(cp.call_count, 1, "前提：平倉單送出、沒成交")
        self.assertIs(self.eng._position, pos, "沒確認平掉就出錯，部位要放回帳上")
        self.assertTrue(pos.get("pending_close"), "並記成待平倉、每輪重試")
        self.assertFalse(cb.called, "沒確認平掉不能撤停損")
        self.assertEqual(self.dbclose, [])
        a = one(self.eng.alerts, "平倉單沒有成交"); self.assertIn("第 1 次", a); self.assertIn("inj-before", a)

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
        a = one(self.eng.alerts, "每輪步驟「快速停損出場」出錯"); self.assertIn("第 1 次", a); self.assertIn("inj-fast", a)

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
        # 前提：壞資料真的讓那一筆走到例外處理(用法第5點r28)，不是走一般的撤單失敗
        self.assertIn("BAD", one(self.eng.alerts, "待撤清單有一筆資料有問題"))

    # 2c：手上有正向證據時，直接用已知數量掛停損，不重查
    def test_t2c_open_fill_places_stop_with_known_qty_even_if_exchange_lags(self):
        pos = self._real_pos(backstop_algo_id=None, backstop_price=None)
        self.eng._position = pos   # 部位要在帳上：r53 起會送單的函式先確認傳進來的是帳上那一筆
        with mock.patch.object(ex, "get_position_info", return_value=(True, _rows(0))) as gp, \
             mock.patch.object(ex, "place_algo_stop", return_value=(True, "N1", False)) as pl:
            self.eng._place_backstop(pos, 0.1, False)
        self.assertEqual(pl.call_count, 1, "剛成交的回應就是部位在的證據，交易所還沒反映也要掛")
        self.assertEqual(pl.call_args[0][1], 0.1)
        self.assertFalse(gp.called, "有正向證據時不重查")

    def test_t2c_claim_places_stop_with_claimed_qty_without_requery(self):
        pos = self._real_pos(real_open_executed=None, real_open_pending_until=time.time() + 100,
                             backstop_algo_id=None, backstop_price=None)
        self.eng._position = pos   # 部位要在帳上：r53 起會送單的函式先確認傳進來的是帳上那一筆
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
        self.eng._position = p   # 部位要在帳上：r53 起會送單的函式先確認傳進來的是帳上那一筆
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
        self.assertEqual(len(order), 2, "前提：寫紀錄與撤停損都有發生")
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
        self.assertIn("inj-end", one(self.eng.alerts, "平倉收尾「出錯次數收尾」出錯"))
        self.assertTrue(any(n.get("action") == "close" for n in self.notes), "收尾出錯，平倉通知照樣要發")

    # 第8條r23/r24：背景迴圈本身出錯要推播
    def test_t8c_background_loop_error_is_pushed(self):
        with mock.patch.object(self.eng, "_tick", side_effect=RuntimeError("inj-loop")):
            self.eng._loop_once()
        a = one(self.eng.alerts, "每輪步驟「每輪判斷」出錯"); self.assertIn("第 1 次", a); self.assertIn("inj-loop", a)

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
        self.assertIn("inj-web", one(pushed, "網頁操作「手動測試平倉」出錯"))


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
    # 使用者決定(2026-09-22)：成交價查不到時用推估值，但一定要標示「估算」(不能混成已知)
    def test_t8c_close_notice_estimates_usd_and_labels_it(self):
        from app.notifier import notifier as N, TelegramNotifier
        sent = []
        with mock.patch.object(TelegramNotifier, "is_enabled", new_callable=mock.PropertyMock, return_value=True), \
             mock.patch.object(TelegramNotifier, "is_muted", new_callable=mock.PropertyMock, return_value=False), \
             mock.patch.object(N, "_send_telegram_message", side_effect=lambda t: sent.append(t) or (True, None)):
            N.notify_trade_event(action="close", label="15分K", direction="bullish", price=4380.0, exit_reason="觸及停損",
                                 pnl_points=-2.0, executed=True, quantity=0.1, real_pnl_usd=None)
        self.assertEqual(len(sent), 1, "前提：通知真的組出來、送出了")
        self.assertIn("-2.00 points", sent[0], "前提：模擬點數照常顯示")
        self.assertIn("≈ -0.20 USDT", sent[0], "查不到成交價：用模擬點數×張數推估")
        self.assertIn("估算", sent[0], "一定要標示是估算，不能混成依真實成交價")
        self.assertNotIn("依真實成交價", sent[0])



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

    def test_t8b_risk_guard_unknown_pnl_counted_as_loss_and_reported(self):
        """r29 原本是「未知不算虧損、另外計數」；r50 起斷路器把未知當成一次虧損(只會更早停)，筆數照樣回報。"""
        from app import risk_guard as RG
        class Eng:
            _closed_trades_memory = [{"exit_time": "2026-09-21T01:00:00+00:00", "pnl_points": -1.0},
                                     {"exit_time": "2026-09-21T02:00:00+00:00", "pnl_points": None}]
            engine_id = "x"
        with mock.patch.object(RG.db, "is_enabled", return_value=False):
            n = RG.get_consecutive_losses(Eng())
            unknown = RG.get_unknown_pnl_count(Eng())
        self.assertEqual(n, 2, "最新那筆損益未知：斷路器當成一次虧損(r50)，連續虧損2筆")
        self.assertEqual(unknown, 1)

    def test_t8c_partial_reduction_without_fills_is_estimated_and_labeled(self):
        pos = _pos(real_open_quantity=0.2, entry_actual_price=4391.0, real_open_baseline=0.0)
        self.eng._position = pos
        # 明寫「成交明細查不到」(不依賴共用框架的預設值：預設改了這項就會莫名失敗或空跑)
        with mock.patch.object(ex, "get_position_info", return_value=(True, _rows(0.1, mark=4396.0))), \
             mock.patch.object(ex, "get_user_trades", return_value=(False, "Read timed out"), create=True):
            status = self.eng._check_exchange_quantity(pos)
        self.assertEqual(status, "reduced", "前提：偵測到數量減少")
        self.assertIn("成交明細查不到", one(self.eng.alerts, "交易所部位數量減少"), "前提：未知的原因是查不到成交明細(第16種)")
        self.assertTrue(pos.get("usd_estimated"), "減少那部分的成交價不知道：用標記價推估，標記為估算")
        self.assertAlmostEqual(pos.get("partial_realized_usd") or 0, (4396.0 - 4391.0) * 0.1, places=4)
        self.assertIn("估算", one(self.eng.alerts, "交易所部位數量減少"))

    def test_t8c_real_usd_summary_includes_labeled_estimates(self):
        from app import trading_stats as TS
        trades = [{"real_open_executed": True, "real_pnl_usd": 1.5},
                  {"real_open_executed": True, "real_pnl_usd": None, "real_pnl_usd_est": 3.0, "pnl_points": 30.0},  # 缺成交價：推估
                  {"real_open_executed": True, "real_pnl_usd": None, "real_pnl_usd_est": None, "pnl_points": None},  # 連點數都沒有：未知
                  {"real_open_executed": False, "real_pnl_usd": None, "pnl_points": 99.0}]  # 純模擬：不算
        s = TS.real_usd_summary(trades)
        self.assertIn("estimated", s, "前提：統計有「估算」這個分類(在舊版上是斷言失敗、不是崩掉)")
        self.assertEqual((s.get("total"), s.get("known"), s.get("estimated"), s.get("unknown")), (4.5, 1, 1, 1), s)


# ------------------------------------------------------------------ r29 → r31
def _fills(*rows):
    """
    模擬成交明細(GET /fapi/v1/userTrades)，刻意避開退化值(用法第5點r31)：成交價≠標記價、有手續費、
    id遞增、同一毫秒可有多筆、打平那筆的realizedPnl真的是0。rows: (id, orderId, side, qty, price, realizedPnl, time)
    """
    return [{"id": i, "orderId": o, "side": s, "positionSide": "BOTH", "qty": str(q), "price": str(p),
             "realizedPnl": str(r), "commission": "0.0172", "time": tm} for i, o, s, q, p, r, tm in rows]


class Lesson31(ExecHarness):
    OPEN = (101, 9001, "BUY", 0.1, 4391.2, 0.0, 1000)   # 開倉那筆(realizedPnl本來就是0)

    def _pos(self, **kw):
        p = _pos(real_open_quantity=0.1, entry_actual_price=4391.2, real_open_baseline=0.0,
                 real_open_order_id=9001, backstop_algo_id=None, backstop_price=None)
        p.update(kw)
        self.eng._position = p
        return p

    def _gone(self, pos, fills):
        """交易所端已經平掉(App手動、或backstop在服務中斷時觸發)：走數量比對連3輪為0的那條路。"""
        pos["gone_checks"] = 2
        with mock.patch.object(ex, "get_position_info", return_value=(True, _rows(0, mark=4395.0))), \
             mock.patch.object(ex, "close_position", return_value=(False, "目前沒有未平倉部位可以平")), \
             mock.patch.object(ex, "get_user_trades", return_value=fills, create=True) as ut, \
             mock.patch.object(self.eng, "_cancel_backstop", return_value=False):
            status = self.eng._check_exchange_quantity(pos)
        return status, ut

    def test_t8a_external_close_uses_fills_not_mark_price(self):
        pos = self._pos()
        fills = (True, _fills(self.OPEN, (99, 8000, "SELL", 0.5, 4300.0, 1.0, 900),        # 開倉之前的舊成交：不能算
                                   (105, 9100, "SELL", 0.06, 4380.4, -0.648, 2000),
                                   (106, 9100, "SELL", 0.04, 4380.0, -0.448, 2000)))
        status, ut = self._gone(pos, fills)
        self.assertEqual(status, "gone", "前提：判定交易所端已平掉")
        self.assertTrue(ut.called, "前提：真的查了成交明細")
        self.assertEqual(len(self.dbclose), 1)
        exit_px, pnl = self.dbclose[0][1], self.dbclose[0][4]
        self.assertAlmostEqual(exit_px, 4380.24, places=2, msg="出場價＝平倉成交的加權均價(不是標記價4395)")
        self.assertAlmostEqual(pnl, 4380.24 - 4390.0, places=2, msg="點數損益用實際成交價算")

    def test_t8e_breakeven_close_fill_is_not_filtered_by_realized_pnl(self):
        """打平出場那筆的realizedPnl是0：挑平倉成交要看方向，不能用realizedPnl≠0篩(r31)。"""
        pos = self._pos()
        fills = (True, _fills(self.OPEN, (105, 9100, "SELL", 0.1, 4391.2, 0.0, 2000)))
        status, _ = self._gone(pos, fills)
        self.assertEqual(status, "gone")
        self.assertEqual((len(self.dbclose), len(self.notes) > 0), (1, True), "前提：結帳了、通知有送出")  # 先確認有東西才索引(用法第5點r34：突變下要明確失敗、不是測試本身崩掉)
        self.assertEqual(self.dbclose[0][1], 4391.2, "打平那筆要被採用，出場價是它的成交價")
        self.assertEqual(self.notes[-1].get("real_pnl_usd"), 0.0, "真實損益是已知的0，不是未知")

    def test_t8a_no_fills_means_unknown_not_mark(self):
        pos = self._pos()
        status, ut = self._gone(pos, (False, {"code": -1001}))
        self.assertEqual(status, "gone")
        self.assertTrue(ut.called, "前提：真的去查了成交明細、查不到")
        self.assertEqual((len(self.dbclose), len(self.notes) > 0), (1, True), "前提：結帳了、通知有送出")  # 先確認有東西才索引(用法第5點r34：突變下要明確失敗、不是測試本身崩掉)
        self.assertAlmostEqual(self.dbclose[0][4], 4395.0 - 4390.0, places=4, msg="查不到成交明細：用偵測當下的標記價推估")
        self.assertEqual(len(self.dbclose_kw), 1, "前提：結帳的參數有記到")
        self.assertTrue(self.dbclose_kw[0].get("pnl_estimated"), "並標記為估算")
        self.assertIsNone(self.notes[-1].get("real_pnl_usd"), "真實損益不是已知的")

    def test_t8a_baseline_means_unknown_even_with_fills(self):
        pos = self._pos(real_open_baseline=0.3)
        fills = (True, _fills(self.OPEN, (105, 9100, "SELL", 0.1, 4380.0, -1.12, 2000)))
        pos["gone_checks"] = 2
        with mock.patch.object(ex, "get_position_info", return_value=(True, _rows(0.3, mark=4395.0))), \
             mock.patch.object(ex, "close_position", return_value=(False, "扣掉基準後沒有自己的部位")), \
             mock.patch.object(ex, "get_user_trades", return_value=fills, create=True), \
             mock.patch.object(self.eng, "_cancel_backstop", return_value=False):
            status = self.eng._check_exchange_quantity(pos)
        self.assertEqual(status, "gone", "前提：扣基準後判定自己的已平掉")
        self.assertEqual(len(self.dbclose), 1, "前提：結帳了")  # 先確認有東西才索引(用法第5點r34：突變下要明確失敗、不是測試本身崩掉)
        self.assertEqual(len(self.dbclose_kw), 1, "前提：結帳的參數有記到")
        self.assertTrue(self.dbclose_kw[0].get("pnl_estimated"), "同側有別人的部位：成交明細分不出來，損益是推估的")

    def test_t8b_partial_then_close_boundary_by_id(self):
        """部分出場採用了id105；最後出場只看105之後的(界線用id，不用時間——106與105同一毫秒)。"""
        pos = self._pos(real_open_quantity=0.2)
        fills1 = (True, _fills((101, 9001, "BUY", 0.2, 4391.2, 0.0, 1000), (105, 9100, "SELL", 0.1, 4396.5, 0.53, 2000)))
        with mock.patch.object(ex, "get_position_info", return_value=(True, _rows(0.1, entry=4391.2, mark=4395.0))), \
             mock.patch.object(ex, "get_user_trades", return_value=fills1, create=True):
            self.assertEqual(self.eng._check_exchange_quantity(pos), "reduced", "前提：偵測到減少")
        self.assertFalse(pos.get("partial_pnl_unknown"), "減少那段查得到成交明細，損益已知")
        self.assertEqual(pos.get("fill_boundary_id"), 105)
        fills2 = (True, _fills((101, 9001, "BUY", 0.2, 4391.2, 0.0, 1000), (105, 9100, "SELL", 0.1, 4396.5, 0.53, 2000),
                                (106, 9200, "SELL", 0.1, 4380.0, -1.12, 2000)))
        status, _ = self._gone(pos, fills2)
        self.assertEqual(status, "gone")
        self.assertEqual((len(self.dbclose), len(self.notes) > 0), (1, True), "前提：結帳了、通知有送出")  # 先確認有東西才索引(用法第5點r34：突變下要明確失敗、不是測試本身崩掉)
        self.assertEqual(self.dbclose[0][1], 4380.0, "最後出場只採用id>105的那筆，不被前一段拉偏")
        self.assertAlmostEqual(self.notes[-1].get("real_pnl_usd"), (4396.5 - 4391.2) * 0.1 + (4380.0 - 4391.2) * 0.1, places=4)


# ------------------------------------------------------------------ r32 → r34
class Lesson34(ExecHarness):
    def _unknown_pos(self, **kw):
        # 服務重啟後還原的「結果不明」部位：資料庫記的是不明(None)，記憶體裡的確認期限已經不見了
        p = _pos(real_open_executed=None, real_open_quantity=0.1, real_open_baseline=0.0,
                 backstop_algo_id=None, backstop_price=None)
        p.update(kw)
        self.eng._position = p
        return p

    # 8a：缺期限的結果不明部位不能靜靜卡住——當成已逾時，逐幣確認後認領或判定未成交
    def test_t8a_unknown_without_deadline_is_claimed_when_on_exchange(self):
        pos = self._unknown_pos()
        with mock.patch.object(ex, "get_position_info", return_value=(True, _rows(0.1))) as gp, \
             mock.patch.object(ex, "place_algo_stop", return_value=(True, "S1", False)) as pl:
            self.eng._housekeeping(pos)
        self.assertTrue(gp.called, "前提：真的去交易所確認了")
        self.assertIs(pos.get("real_open_executed"), True, "交易所有這筆：認領")
        self.assertEqual(pl.call_count, 1, "認領後當場掛交易所停損")

    def test_t8a_unknown_without_deadline_is_resolved_not_filled_when_absent(self):
        pos = self._unknown_pos()
        with mock.patch.object(ex, "get_position_info", return_value=(True, _rows(0))) as gp:
            self.eng._housekeeping(pos)
        self.assertTrue(gp.called, "前提：真的去交易所確認了、看到沒有")
        self.assertIs(pos.get("real_open_executed"), False, "缺期限＝已逾時，交易所沒有就判定未成交")
        self.assertIn("沒有確認期限", one(self.eng.alerts, "判定未成交"))

    def test_t8a_paper_only_engine_does_not_query_exchange(self):
        pos = self._unknown_pos()
        with mock.patch.object(self.eng, "_is_execution_engine", return_value=False) as ie, \
             mock.patch.object(ex, "get_position_info", return_value=(True, _rows(0.1))) as gp:
            self.eng._housekeeping(pos)
        self.assertTrue(ie.called, "前提：真的先判斷了是不是實盤引擎")
        self.assertEqual(gp.call_count, 0, "純模擬引擎沒有真實部位，不能去查交易所")
        self.assertIsNone(pos.get("real_open_executed"), "前提：純模擬的部位維持原狀")
        self.assertEqual(self.eng.alerts, [])

    # 8d：起始界線在開倉成交後記下(不是平倉時才算、也不是開倉時間往前幾秒)，並存進資料庫
    def test_t8d_boundary_recorded_at_open_and_persisted(self):
        eng = self.eng
        fills = (True, _fills((190, 7000, "SELL", 0.3, 4370.0, -2.0, 900),       # 開倉之前的舊成交
                               (201, 9001, "BUY", 0.06, 4391.0, 0.0, 1000),
                               (202, 9001, "BUY", 0.04, 4391.5, 0.0, 1000)))
        saved = []
        with mock.patch.object(pt.risk_guard, "check", return_value=(True, None, None)), \
             mock.patch.object(ex, "resolve_position_mode", return_value=(False, None)), \
             mock.patch.object(ex, "set_margin_type", return_value=(True, {})), \
             mock.patch.object(ex, "set_leverage", return_value=(True, {})), \
             mock.patch.object(ex, "open_position", return_value=(True, {"avgPrice": "4391.2", "orderId": 9001})) as op, \
             mock.patch.object(ex, "get_position_info", return_value=(True, _rows(0))), \
             mock.patch.object(ex, "get_user_trades", return_value=fills, create=True), \
             mock.patch.object(pt.db, "update_paper_trade_fills", side_effect=lambda *a: saved.append(a), create=True), \
             mock.patch.object(eng, "_sync_backstop"):
            eng._position = None
            eng._open_position({"direction": "bullish", "bid": 4389.9, "ask": 4390.0,
                                "chan": {"reason": "t"}, "profile": {"reason": "t"}}, 4390.0, 10.0)
        self.assertEqual(op.call_count, 1, "前提：開倉單送出並成交")
        self.assertIsNotNone(eng._position, "前提：部位真的開起來了(沒開成時要是斷言失敗、不是測試崩掉)")
        self.assertEqual(eng._position.get("fill_boundary_id"), 202, "界線＝開倉那張單最後一筆成交的id")
        self.assertEqual(saved and saved[-1][1:], (9001, 202), "開倉單號與界線要存進資料庫(重啟後還在)")

    def test_t8d_claim_records_boundary_as_latest_id(self):
        pos = self._unknown_pos(real_open_pending_until=time.time() + 100)
        fills = (True, _fills((301, 9999, "BUY", 0.1, 4391.0, 0.0, 1000), (302, 8888, "SELL", 0.2, 4300.0, 1.0, 1000)))
        with mock.patch.object(ex, "get_position_info", return_value=(True, _rows(0.1))), \
             mock.patch.object(ex, "get_user_trades", return_value=fills, create=True), \
             mock.patch.object(ex, "place_algo_stop", return_value=(True, "S2", False)):
            self.eng._resolve_open_pending(pos)
        self.assertIs(pos.get("real_open_executed"), True, "前提：認領成功")
        self.assertEqual(pos.get("fill_boundary_id"), 302, "認領當下成交明細最後一筆的id，之後的才算平倉")

    def test_t8c_non_numeric_boundary_is_unknown_not_zero(self):
        """缺值不能退成0：界線不是數字就記未知，不能把這個幣歷史上所有平倉成交算成這筆的出場(r34)。"""
        pos = _pos(real_open_quantity=0.1, real_open_baseline=0.0, fill_boundary_id="", real_open_order_id=None)
        fills = (True, _fills((5, 1, "SELL", 0.1, 4300.0, -9.0, 100)))
        with mock.patch.object(ex, "get_user_trades", return_value=fills, create=True):
            st = self.eng._closing_fills(pos, 0.1)  # 固定長度
        # 前提看判定的原因(第16種)：界線不是數字時在查成交明細之前就判定，不能用「有沒有查」當前提
        self.assertIn("界線不是數字", st[4], st)
        self.assertEqual(st[0], "unknown", st)

    # 8e：雙向模式看 positionSide
    def test_t8e_hedge_other_open_short_is_not_our_close(self):
        pos = _pos(real_open_quantity=0.1, real_open_baseline=0.0, fill_boundary_id=100)
        rows = _fills((101, 5000, "SELL", 0.1, 4385.0, 0.0, 2000), (102, 5100, "SELL", 0.1, 4380.0, -1.1, 2000))  # 固定長度
        rows[0]["positionSide"] = "SHORT"   # 別的專案同幣開空：也是SELL
        rows[1]["positionSide"] = "LONG"    # 自己多單的平倉
        with mock.patch.object(ex, "get_user_trades", return_value=(True, rows), create=True):
            st = self.eng._closing_fills(pos, 0.1)
        self.assertEqual(st[:2], ("ok", 4380.0), "只採用LONG那筆，不能把別人的開空算成自己的平倉")

    # 8d：重啟後從資料庫還原開倉單號與界線
    def test_t8d_restore_includes_order_id_and_boundary(self):
        row = (7, "bullish", 4390.0, None, 4380.0, 4395.0, True, "c", "p", 900, "chan_profile_900",
               True, 0.1, "B1", False, 0.0, 9001, 202, 4391.2, None)   # r74：SELECT 多了進場成交價、部分出場狀態
        class Cur:
            def __enter__(s): return s
            def __exit__(s, *a): return False
            def execute(s, *a): pass
            def fetchone(s): return row
        class Conn:
            def cursor(s): return Cur()
        class Pool:
            def getconn(s): return Conn()
            def putconn(s, c): pass
        with mock.patch.object(pt.db, "_enabled", True), mock.patch.object(pt.db, "_pool", Pool(), create=True):
            got = pt.db.get_open_paper_trade(engine_id="chan_profile_900")
        self.assertIsNotNone(got, "前提：讀得出這一列(欄位數跟 SELECT 對不齊時會讀取失敗、回None，不能讓測試本身崩掉)")
        self.assertEqual(got.get("id"), 7, "前提：真的解析了這一列")
        self.assertEqual((got.get("real_open_order_id"), got.get("fill_boundary_id")), (9001, 202))


# ------------------------------------------------------------------ r35 → r37
class Lesson37(ExecHarness):
    # 8d：開機讀持倉紀錄失敗，不能當成「沒有持倉」
    def test_t8d_startup_read_failure_is_not_flat(self):
        eng = self.eng
        self.assertTrue(hasattr(eng, "_ensure_state_loaded"), "前提：程式有「載入持倉紀錄」這一步(在舊版程式上重跑時是斷言失敗、不是崩掉)")
        eng._seeded_from_db = False
        with mock.patch.object(pt.db, "load_open_paper_trade", return_value=(False, "connection refused"), create=True) as ld, \
             mock.patch.object(pt.db, "get_open_paper_trade", return_value=None):
            eng._ensure_state_loaded()
        self.assertTrue(ld.called, "前提：真的去讀了")
        a = one(eng.alerts, "每輪步驟「讀取持倉紀錄」出錯")
        self.assertIn("connection refused", a)
        self.assertIn("暫停開新倉", a)
        self.assertIs(eng._seeded_from_db, False, "讀不到就還沒載入，不能當成已載入的空手")
        with mock.patch.object(eng, "_run_step", wraps=eng._run_step) as rs, \
             mock.patch.object(pt.db, "load_open_paper_trade", return_value=(False, "connection refused"), create=True):
            opened = eng._open_position({"direction": "bullish", "bid": 1, "ask": 1,
                                         "chan": {"reason": "t"}, "profile": {"reason": "t"}}, 4390.0, 10.0)
        self.assertEqual(opened, "state_not_loaded", "持倉紀錄沒載入前不能開新倉")

    def test_t8d_startup_read_recovers_and_restores_position(self):
        eng = self.eng
        self.assertTrue(hasattr(eng, "_ensure_state_loaded"), "前提：程式有「載入持倉紀錄」這一步(在舊版程式上重跑時是斷言失敗、不是崩掉)")
        eng._seeded_from_db = False
        row = _pos(id=11, real_open_executed=True)
        with mock.patch.object(pt.db, "load_open_paper_trade", side_effect=[(False, "timeout"), (True, row)], create=True):
            eng._ensure_state_loaded()
            eng._ensure_state_loaded()
        self.assertIs(eng._position, row, "第二次讀到了：還原持倉")
        self.assertIs(eng._seeded_from_db, True)
        one(eng.alerts, "每輪步驟「讀取持倉紀錄」已恢復")

    # 8b：資料庫寫入失敗要推播、照節奏、恢復時通知
    def test_t8b_db_write_failure_is_pushed_and_recovery_notified(self):
        pushed = []
        class Boom:
            def getconn(self): raise RuntimeError("inj-db-write")
            def putconn(self, c): pass
        class Ok:
            def getconn(self):
                class C:
                    def cursor(s):
                        class Cur:
                            def __enter__(c): return c
                            def __exit__(c, *a): return False
                            def execute(c, *a): pass
                        return Cur()
                    def commit(s): pass
                return C()
            def putconn(self, c): pass
        pt.db._db_fail_counts.clear() if hasattr(pt.db, "_db_fail_counts") else None
        with mock.patch.object(pt.db, "_enabled", True), \
             mock.patch.object(pt.notifier_module.notifier, "send_raw_message", side_effect=pushed.append):
            with mock.patch.object(pt.db, "_pool", Boom(), create=True):
                pt.db.update_paper_trade_fills(7, 9001, 202)
            with mock.patch.object(pt.db, "_pool", Ok(), create=True):
                pt.db.update_paper_trade_fills(7, 9001, 202)
        a = one(pushed, "資料庫寫入失敗")
        self.assertIn("inj-db-write", a)
        self.assertIn("第 1 次", a)
        one(pushed, "資料庫寫入已恢復")

    # 8f：在鎖裡面觸發「會回頭拿同一把鎖」的推播，不能死鎖
    def test_t8f_push_path_reacquiring_engine_lock_does_not_deadlock(self):
        import threading
        eng = self.eng
        self.assertTrue(hasattr(eng, "_ensure_state_loaded"), "前提：程式有「載入持倉紀錄」這一步(在舊版程式上重跑時是斷言失敗、不是崩掉)")
        reached = []
        def push_that_takes_lock(text):
            with eng._lock:                  # 模擬r37：推播路徑回頭拿同一把鎖
                reached.append(text)
        def body():
            with eng._lock:
                with mock.patch.object(pt.db, "load_open_paper_trade", return_value=(False, "inj-lock"), create=True):
                    eng._seeded_from_db = False
                    eng._ensure_state_loaded()
        with mock.patch.object(eng, "_backstop_alert", side_effect=push_that_takes_lock):
            th = threading.Thread(target=body, daemon=True)
            th.start()
            th.join(timeout=3)
        self.assertFalse(th.is_alive(), "死鎖：3秒內沒結束")
        self.assertTrue(any("讀取持倉紀錄" in r.splitlines()[0] and "inj-lock" in r for r in reached),
                        f"前提：推播路徑真的有走到：{reached}")

    # 8h：成交明細帶fromId往後分頁查完
    def test_t8h_fills_paged_from_boundary_not_last_n(self):
        pos = _pos(real_open_quantity=0.1, real_open_baseline=0.0, fill_boundary_id=150, entry_actual_price=4391.2)
        all_rows = _fills((151, 9001, "BUY", 0.1, 4391.2, 0.0, 1000),
                          (152, 9100, "SELL", 0.05, 4386.0, -0.26, 2000),
                          (153, 9100, "SELL", 0.05, 4380.0, -0.56, 2000))
        calls = []
        def fake(symbol=None, account=None, limit=100, from_id=None):
            calls.append(from_id)
            if from_id is None:
                return True, all_rows[-limit:] if limit < len(all_rows) else all_rows[-1:]   # 只給最近的(最後一頁)
            return True, [r for r in all_rows if r["id"] >= from_id][:limit]
        with mock.patch.object(ex, "get_user_trades", side_effect=fake, create=True), \
             mock.patch.object(pt, "FILL_PAGE_SIZE", 2, create=True):
            st = self.eng._closing_fills(pos, 0.1)
        self.assertEqual(calls[:1], [151], "從界線＋1往後查")
        self.assertEqual(st[:3], ("ok", 4383.0, 0.1), "兩頁的平倉成交都要採用(只拿最後一頁會算出4380這個確定的錯數字)")

    def test_t8h_too_many_pages_is_unknown_not_partial(self):
        pos = _pos(real_open_quantity=0.1, real_open_baseline=0.0, fill_boundary_id=150)
        def fake(symbol=None, account=None, limit=100, from_id=None):
            base = from_id or 151
            return True, _fills(*[(base + k, 1, "BUY", 0.01, 4391.0, 0.0, 1000) for k in range(limit)])
        with mock.patch.object(ex, "get_user_trades", side_effect=fake, create=True) as ut, \
             mock.patch.object(pt, "FILL_PAGE_SIZE", 2, create=True), mock.patch.object(pt, "FILL_MAX_PAGES", 3, create=True):
            st = self.eng._closing_fills(pos, 0.1)  # 固定長度
        self.assertEqual(ut.call_count, 3, "前提：查到頁數上限")
        self.assertEqual(st[0], "unknown", "沒拿完就是不完整，記未知，不能用拿到的部分算")


# ------------------------------------------------------------------ r38 → r40
class Lesson40(ExecHarness):
    # 8f：推播沒設定不能靜靜return——記一次、自檢列出來
    def test_t8f_unconfigured_push_is_recorded_once_and_listed_by_preflight(self):
        from app.notifier import notifier as N
        from app import preflight as pf
        self.assertTrue(hasattr(N, "errors"), "前提：推播有記錄錯誤的地方")
        N.errors.clear()
        N._unconfigured_recorded = False
        with mock.patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""}):
            N.send_raw_message("a")
            N.send_raw_message("b")
            item = pf.notify_check()
        unconf = [e for e in N.errors if e["kind"] == "未設定"]
        self.assertEqual(len(unconf), 1, "沒設定：記一次，不是每則都記、也不是完全不記")
        self.assertEqual(item["status"], "warn", item)
        self.assertIn("TELEGRAM_BOT_TOKEN", item["msg"])

    # 8d：推播送出失敗要記下來，而且記錄本身不再推播(不遞迴)
    def test_t8d_push_send_failure_is_recorded_without_recursion(self):
        from app.notifier import notifier as N
        N.errors.clear() if hasattr(N, "errors") else None
        with mock.patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "c"}), \
             mock.patch("app.notifier.requests.post", side_effect=RuntimeError("inj-push")) as post:
            ok, err = N.send_raw_message("hello")
        self.assertEqual(post.call_count, 1, "前提：真的嘗試送出一次，而且記錄錯誤時沒有再送(不遞迴)")
        self.assertFalse(ok)
        self.assertTrue(hasattr(N, "errors"), "前提：推播有記錄錯誤的地方")
        fails = [e for e in N.errors if e["kind"] == "送出失敗"]
        self.assertEqual(len(fails), 1)
        self.assertIn("inj-push", fails[0]["msg"])

    # 8c：手動下單也要擋——持倉紀錄沒載入時不能送
    def test_t8c_manual_test_order_blocked_when_state_not_loaded(self):
        import asyncio
        import app.main as m
        eng = self.eng
        eng._seeded_from_db = False
        with mock.patch.object(m.settings_module, "verify_password", return_value=(True, None)) as vp, \
             mock.patch.object(ex, "open_position", return_value=(True, {"avgPrice": "4391"})) as op, \
             mock.patch.object(ex, "place_market_order", return_value=(True, {"avgPrice": "4391"})) as pm:
            res = asyncio.run(m.execution_test_order({"password": "x", "direction": "bullish", "quantity": 0.1,
                                                     "account": eng.execution_account, "confirm_live": True}))
        self.assertTrue(vp.called, "前提：通過了密碼檢查，擋下的是持倉紀錄這一關")
        self.assertIn("持倉紀錄", str(res.get("error")), res)
        self.assertEqual(op.call_count + pm.call_count, 0, "沒送出任何單")

    # 8e：設定一次套用——有一個值不對，全部都不套用
    def test_t8e_update_settings_is_all_or_nothing(self):
        S = pt.settings_module
        self.assertTrue(hasattr(S, "SettingsValidationError"), "前提：程式有「整批驗證」這個機制(在舊版上重跑時是斷言失敗、不是崩掉)")
        before = dict(S.get_settings())
        key_ok, key_bad = "paper_sl_points", "paper_trail_trigger_points"
        new_ok = before[key_ok] + 1.0 if before[key_ok] < 50 else before[key_ok] - 1.0
        with mock.patch.object(pt.db, "is_enabled", return_value=False):
            with self.assertRaises(S.SettingsValidationError) as cm:
                S.update_settings({key_ok: new_ok, key_bad: "not-a-number"})
        self.assertIn(key_bad, str(cm.exception), "錯誤要講是哪個欄位")
        self.assertEqual(S.get_settings()[key_ok], before[key_ok], "有一個值不對：其他欄位也不能先套用(不能停在半套)")

    def test_t8e_param_set_import_rejects_whole_set_on_bad_value(self):
        import asyncio
        import app.main as m
        S = pt.settings_module
        eid = self.eng.engine_id
        before = dict(S.get_engine_overrides(eid) or {})
        ps = {"format": "gold-scalper-param-set/1", "engine_id": eid,
              "params": {"paper_sl_points": 7.5, "paper_trail_trigger_points": "abc"}}
        with mock.patch.object(m.settings_module, "verify_password", return_value=(True, None)), \
             mock.patch.object(pt.db, "is_enabled", return_value=False), \
             mock.patch.object(m.notifier, "send_raw_message"):
            res = asyncio.run(m.settings_import({"password": "x", "param_set": ps, "force": True}))
        self.assertIs(res.get("success"), False, res)
        self.assertIn("paper_trail_trigger_points", str(res.get("error")))
        self.assertEqual(dict(S.get_engine_overrides(eid) or {}), before, "整份拒絕，一個欄位都沒套用")

    # 5f：全量部位表跟真的一樣列出數量0的列(所有交易過的幣、雙向兩側)
    def test_t5f_orphan_check_with_realistic_full_position_table(self):
        from app import preflight as pf
        full = [{"symbol": "XAUUSDT", "positionSide": "BOTH", "positionAmt": "0.1"},
                {"symbol": "KASUSDT", "positionSide": "BOTH", "positionAmt": "0"},       # 交易過、現在是0
                {"symbol": "BTCUSDT", "positionSide": "LONG", "positionAmt": "0"},
                {"symbol": "BTCUSDT", "positionSide": "SHORT", "positionAmt": "0"}]
        orders = [{"symbol": "XAUUSDT", "side": "SELL", "algoId": 1, "positionSide": "BOTH"},
                  {"symbol": "KASUSDT", "side": "BUY", "algoId": 2, "positionSide": "BOTH"},
                  {"symbol": "BTCUSDT", "side": "SELL", "algoId": 3, "positionSide": "LONG"}]
        with mock.patch.object(ex, "_get_credentials", return_value=("k", "s")), \
             mock.patch.object(ex, "get_symbol_filters", return_value={"step_size": .001, "tick_size": .01, "min_notional": 5}), \
             mock.patch.object(ex, "current_hedge_mode", return_value=False), \
             mock.patch.object(ex, "get_algo_stop_status", return_value=(False, {"code": -2013, "msg": "Order does not exist."})), \
             mock.patch.object(ex, "usdt_balance_line", return_value="x"), mock.patch.object(ex, "max_leverage", return_value=5), \
             mock.patch.object(ex, "get_open_algo_orders", return_value=(True, orders)), \
             mock.patch.object(pf, "_signed_positions", return_value=(True, full)), \
             mock.patch.object(ex, "get_position_info", return_value=(True, [])):
            res = {r["item"]: r for r in pf.check(account="gold")}
        msg = res["孤兒條件單"]["msg"]
        self.assertEqual(res["孤兒條件單"]["status"], "fail", msg)
        self.assertIn("algoId=2", msg, "數量0的列不算有部位")
        self.assertIn("algoId=3", msg, "雙向兩側都是0也不算")
        self.assertNotIn("algoId=1", msg, "真的有部位的不算孤兒")


# ------------------------------------------------------------------ 實盤：市價單回應沒有成交資訊(2026-09-22)
ACK = {"orderId": 16598305424, "symbol": "XAUUSDT", "status": "NEW", "price": "0.00", "origQty": "0.100",
       "executedQty": "0.000", "cumQty": "0.000", "type": "MARKET", "side": "BUY", "positionSide": "LONG"}


class LiveAckResponse(ExecHarness):
    """實盤 2026-09-22 00:00:06 的開倉：回應是ACK(status NEW、沒有avgPrice)，程式當成已成交但沒有成交價。"""

    def _send(self, first, polls):
        sent, polled = [], []
        def fake_req(method, path, params=None, account=None, return_status=False):
            if method == "POST":
                sent.append(dict(params))
                return (True, first, 200) if return_status else (True, first)
            polled.append(params.get("orderId"))
            r = polls[min(len(polled) - 1, len(polls) - 1)]
            return (True, r, 200) if return_status else (True, r)
        with mock.patch.object(ex, "_signed_request", side_effect=fake_req), \
             mock.patch.object(ex, "round_quantity", return_value=(0.1, None)), mock.patch("time.sleep"):
            ok, res = ex.place_market_order("BUY", 0.1, account="gold", position_side="LONG")
        return ok, res, sent, polled

    def test_order_requests_full_result_response(self):
        filled = dict(ACK, status="FILLED", executedQty="0.100", avgPrice="4373.52")
        ok, res, sent, _ = self._send(filled, [filled])
        self.assertEqual(len(sent), 1, "前提：送出一張單")
        self.assertEqual(sent[0].get("newOrderRespType"), "RESULT", "要求回應包含成交資訊，不用預設的ACK")

    def test_ack_then_filled_on_poll_returns_fill(self):
        filled = dict(ACK, status="FILLED", executedQty="0.100", avgPrice="4373.52")
        ok, res, _, polled = self._send(ACK, [ACK, ACK, filled])
        self.assertEqual(len(polled), 3, "前提：沒確認成交時多查幾次，不是只查一次")
        self.assertTrue(ok)
        self.assertEqual(res.get("avgPrice"), "4373.52")

    def test_never_confirmed_is_ambiguous_not_success(self):
        ok, res, _, polled = self._send(ACK, [ACK])
        self.assertGreater(len(polled), 1, "前提：真的查了訂單")
        self.assertFalse(ok, "沒確認成交不能回報成功(平倉時會直接撤停損、結帳)")
        self.assertTrue(ex.is_ambiguous_result(res), "要當成結果不明，走查部位確認的路")

    def test_open_uses_executed_qty_and_fill_price_from_trades(self):
        eng = self.eng
        partial = dict(ACK, status="FILLED", executedQty="0.060", avgPrice="0.00")   # 成交0.06、回應沒均價
        fills = (True, _fills((201, 16598305424, "BUY", 0.02, 4373.4, 0.0, 1000),
                               (202, 16598305424, "BUY", 0.04, 4373.7, 0.0, 1000)))
        with mock.patch.object(pt.risk_guard, "check", return_value=(True, None, None)), \
             mock.patch.object(ex, "resolve_position_mode", return_value=(True, None)), \
             mock.patch.object(ex, "set_margin_type", return_value=(True, {})), \
             mock.patch.object(ex, "set_leverage", return_value=(True, {})), \
             mock.patch.object(ex, "open_position", return_value=(True, partial)) as op, \
             mock.patch.object(ex, "get_position_info", return_value=(True, _rows(0))), \
             mock.patch.object(ex, "get_user_trades", return_value=fills, create=True), \
             mock.patch.object(eng, "_sync_backstop"):
            eng._position = None
            eng._open_position({"direction": "bullish", "bid": 4373.4, "ask": 4373.5,
                                "chan": {"reason": "t"}, "profile": {"reason": "t"}}, 4373.49, 10.0)
        self.assertEqual(op.call_count, 1, "前提：開倉單送出")
        pos = eng._position
        self.assertIsNotNone(pos, "前提：開倉了")
        self.assertEqual(pos.get("real_open_quantity"), 0.06, "真實數量用交易所回報的成交量，不是送出的0.1")
        self.assertAlmostEqual(pos.get("entry_actual_price") or 0, (4373.4 * 0.02 + 4373.7 * 0.04) / 0.06, places=4,
                               msg="回應沒有均價：用這張單號的成交明細算")

    def test_close_fill_price_from_trades_when_response_lacks_it(self):
        pos = _pos(real_open_quantity=0.1, entry_actual_price=4373.5, real_open_baseline=0.0, backstop_algo_id="B1")
        filled = {"orderId": 777, "status": "FILLED", "executedQty": "0.100", "avgPrice": "0.00"}
        fills = (True, _fills((301, 777, "SELL", 0.1, 4380.2, 0.67, 2000)))
        with mock.patch.object(ex, "close_position", return_value=(True, filled)) as cp, \
             mock.patch.object(ex, "get_user_trades", return_value=fills, create=True), \
             mock.patch.object(self.eng, "_cancel_backstop", return_value=False):
            self.eng._close_position(pos, 4380.0, "觸及停損")
        self.assertEqual(cp.call_count, 1, "前提：平倉單送出、成交")
        self.assertGreater(len(self.notes), 0, "前提：平倉通知有送出")
        self.assertAlmostEqual(self.notes[-1].get("real_pnl_usd") or 0, (4380.2 - 4373.5) * 0.1, places=4,
                               msg="平倉回應沒有均價：用這張單號的成交明細算出真實損益")


# ------------------------------------------------------------------ r70 → r71 實盤 2026-09-25 22:46
# 平倉市價單帶了 RESULT、回應是 FILLED 成交 1 張，但 avgPrice、cumQuote 兩個欄位整個不存在(原始回應照抄)；
# 0.5 秒後查一次訂單、立刻查一次成交明細都沒拿到 → 通知寫「估算」並把整包原始回應貼進 Telegram。
LIVE_NO_AVG = {"orderId": 17233421715, "symbol": "XAUUSDT", "status": "FILLED", "clientOrderId": "DinxfWcCnuek57MpLPBhF8",
               "price": "0.00", "origQty": "1.000", "executedQty": "1.000", "cumQty": "1.000", "timeInForce": "GTC",
               "type": "MARKET", "reduceOnly": True, "closePosition": False, "side": "BUY", "positionSide": "SHORT",
               "stopPrice": "0.00", "workingType": "CONTRACT_PRICE", "priceProtect": False, "origType": "MARKET",
               "priceMatch": "NONE", "selfTradePreventionMode": "EXPIRE_MAKER", "goodTillDate": 0, "updateTime": 1790347587245}
OID71 = 17233421715


class LiveFilledNoAvgPrice(ExecHarness):
    """
    r71：成交了、但回應沒有均價。當下查不到不能就此放棄——背景延後再查，查到就補寫資料庫(執行品質、真實損益)並補發一則通知；
    一直查不到要明確說「補登失敗、維持估算」。注入綁在時間(背景等待過後成交明細才出現)，不用第幾次查詢計數(測錯方式18)。
    """

    def _clock(self, rows_by_time):
        """rows_by_time: [(第幾秒起看得到, 成交列)]。回傳(clock, 假sleep, 假userTrades)。"""
        clock = {"t": 0.0}
        def sleep(s):
            clock["t"] += s
        def trades(*a, **k):
            return (True, _fills(*[r for when, r in rows_by_time if clock["t"] >= when]))
        return clock, sleep, trades

    def _close(self, rows_by_time, order_status=(True, LIVE_NO_AVG)):
        pos = _pos(direction="bearish", entry_price=4312.89, entry_actual_price=4312.88, sl_price=4277.44, peak_price=4260.41,
                   trailing_active=True, real_open_quantity=1.0, real_open_baseline=0.0, backstop_algo_id="B71", id=71)
        self.eng._position = pos
        clock, sleep, trades = self._clock(rows_by_time)
        self.bnotes = []
        with mock.patch.object(ex, "close_position", return_value=(True, dict(LIVE_NO_AVG))) as cp, \
             mock.patch.object(ex, "get_position_info", return_value=(True, _rows(-1.0, entry=4312.88))), \
             mock.patch.object(ex, "get_user_trades", side_effect=trades, create=True), \
             mock.patch.object(ex, "get_order_status", return_value=order_status), \
             mock.patch.object(pt.time, "sleep", side_effect=sleep), \
             mock.patch.object(pt.db, "update_paper_trade_exit_execution") as ux, \
             mock.patch.object(pt.notifier_module.notifier, "notify_fill_backfill", create=True,
                               side_effect=lambda **k: self.bnotes.append(k)), \
             mock.patch.object(self.eng, "_cancel_backstop", return_value=True):
            self.eng._close_position(pos, 4277.45, "觸及移動停損", bid=4277.40, ask=4277.41)
            sync_calls = list(ux.call_args_list)
            for fn in list(self.backfills):
                fn()
        return cp, ux, sync_calls

    @staticmethod
    def _prices(calls):
        """update_paper_trade_exit_execution(trade_id, expected, actual, slip, spread) 各次呼叫的 actual。"""
        return [c.args[2] for c in calls if len(c.args) > 2 and c.args[2] is not None]

    def test_r71_order_polls_more_than_once_for_avg_price(self):
        sent, polled = [], []
        later = dict(LIVE_NO_AVG, avgPrice="4277.41")
        polls = [LIVE_NO_AVG, LIVE_NO_AVG, later]
        def fake_req(method, path, params=None, account=None, return_status=False):
            if method == "POST":
                sent.append(dict(params))
                return (True, dict(LIVE_NO_AVG))
            polled.append(params.get("orderId"))
            return (True, polls[min(len(polled) - 1, len(polls) - 1)])
        with mock.patch.object(ex, "_signed_request", side_effect=fake_req), \
             mock.patch.object(ex, "round_quantity", return_value=(1.0, None)), mock.patch("time.sleep"):
            ok, res = ex.place_market_order("BUY", 1.0, account="gold", position_side="SHORT")
        self.assertEqual(len(sent), 1, "前提：送出一張單")
        self.assertTrue(ok, "前提：FILLED 成交 1 張，是成功")
        self.assertEqual(res.get("avgPrice"), "4277.41", "FILLED 但沒均價：不能只查一次訂單就放棄")

    def test_r71_cum_quote_is_a_real_fill_price(self):
        self.assertIsNone(ex.extract_fill_price(LIVE_NO_AVG), "前提：這份回應真的沒有均價")
        self.assertAlmostEqual(ex.extract_fill_price(dict(LIVE_NO_AVG, cumQuote="8554.82", executedQty="2.000")) or 0, 4277.41,
                               places=6, msg="沒有 avgPrice 但有 cumQuote：成交額÷成交量就是實際均價")

    def test_r71_close_backfills_exit_price_and_notifies(self):
        cp, ux, sync_calls = self._close([(2.0, (901, OID71, "BUY", 1.0, 4277.41, 35.47, 3000))])
        self.assertEqual(cp.call_count, 1, "前提：平倉單送出、成交")
        self.assertGreater(len(self.notes), 0, "前提：平倉通知有送出")
        self.assertIsNone(self.notes[-1].get("real_pnl_usd"), "前提：當下(成交明細還沒出現)確實拿不到成交價，通知是估算")
        self.assertEqual(self._prices(sync_calls), [], "前提：當下沒有寫進任何出場成交價")
        self.assertEqual(self._prices(ux.call_args_list), [4277.41], "背景稍後查到：出場成交價補寫進資料庫(網頁、統計改用真實值)")
        self.assertEqual(len(self.bnotes), 1, "補登成功要補發一則通知")
        self.assertAlmostEqual(self.bnotes[0].get("real_pnl_usd") or 0, (4312.88 - 4277.41) * 1.0, places=4,
                               msg="補發的通知用真實成交價算 USDT 損益")
        self.assertAlmostEqual(self.bnotes[0].get("fill_price") or 0, 4277.41, places=6)

    def test_r71_close_notice_does_not_dump_raw_response(self):
        self._close([(2.0, (901, OID71, "BUY", 1.0, 4277.41, 35.47, 3000))])
        self.assertGreater(len(self.notes), 0, "前提：平倉通知有送出")
        note = self.notes[-1].get("slippage_note") or ""
        self.assertIn("補登", note, "要說明稍後會補登，讓人知道不是就此算估算")
        self.assertIn(str(OID71), note, "附單號就好")
        self.assertNotIn("'selfTradePreventionMode'", note, "原始回應整包貼進 Telegram：寫到日誌就好")

    def test_r71_backfill_gives_up_with_one_alert(self):
        cp, ux, _ = self._close([])   # 成交明細一直查不到、訂單查詢也一直沒有均價
        self.assertEqual(cp.call_count, 1, "前提：平倉單送出、成交")
        self.assertEqual(len(self.backfills), 1, "前提：當下查不到時排了一次背景補登")
        one(self.eng.alerts, "補登失敗")
        self.assertEqual(self._prices(ux.call_args_list), [], "查不到就不能寫一個成交價進去")
        self.assertEqual(self.bnotes, [], "沒補登成功，不發「補登」通知(發的是上面那則失敗告警)")

    def test_r71_partial_trade_rows_are_not_a_fill_price(self):
        # 成交明細先只出現 0.4 張(4270.0)，稍後才出現另外 0.6 張(4282.0)：只用前半段算均價是錯的
        cp, ux, sync_calls = self._close([(0.0, (911, OID71, "BUY", 0.4, 4270.0, 17.15, 3000)),
                                          (2.0, (912, OID71, "BUY", 0.6, 4282.0, 18.53, 3000))])
        self.assertEqual(cp.call_count, 1, "前提：平倉單送出、成交")
        self.assertGreater(len(self.notes), 0, "前提：平倉通知有送出")
        self.assertIsNone(self.notes[-1].get("real_pnl_usd"), "成交明細只湊到 0.4/1.0 張：當下不能拿來當成交價")
        self.assertEqual(len(self._prices(ux.call_args_list)), 1, "前提：背景補登有寫進成交價")
        self.assertAlmostEqual(self._prices(ux.call_args_list)[0], (0.4 * 4270.0 + 0.6 * 4282.0) / 1.0, places=6,
                               msg="湊滿 1 張之後才用兩段加權")

    def test_r71_open_backfills_entry_price(self):
        eng = self.eng
        OPEN_OID = 17233000001
        raw = dict(LIVE_NO_AVG, orderId=OPEN_OID, side="BUY", positionSide="LONG", reduceOnly=False,
                   origQty="0.100", executedQty="0.100", cumQty="0.100")
        clock, sleep, trades = self._clock([(2.0, (951, OPEN_OID, "BUY", 0.1, 4373.6, 0.0, 1000))])
        self.bnotes = []
        with mock.patch.object(pt.risk_guard, "check", return_value=(True, None, None)), \
             mock.patch.object(ex, "resolve_position_mode", return_value=(True, None)), \
             mock.patch.object(ex, "set_margin_type", return_value=(True, {})), \
             mock.patch.object(ex, "set_leverage", return_value=(True, {})), \
             mock.patch.object(ex, "open_position", return_value=(True, raw)) as op, \
             mock.patch.object(ex, "get_position_info", return_value=(True, _rows(0))), \
             mock.patch.object(ex, "get_user_trades", side_effect=trades, create=True), \
             mock.patch.object(ex, "get_order_status", return_value=(True, raw)), \
             mock.patch.object(pt.time, "sleep", side_effect=sleep), \
             mock.patch.object(pt.db, "update_paper_trade_entry_execution") as ue, \
             mock.patch.object(pt.notifier_module.notifier, "notify_fill_backfill", create=True,
                               side_effect=lambda **k: self.bnotes.append(k)), \
             mock.patch.object(eng, "_sync_backstop"):
            eng._position = None
            eng._open_position({"direction": "bullish", "bid": 4373.4, "ask": 4373.5,
                                "chan": {"reason": "t"}, "profile": {"reason": "t"}}, 4373.49, 10.0)
            pos = eng._position
            self.assertEqual(op.call_count, 1, "前提：開倉單送出")
            self.assertIsNotNone(pos, "前提：開倉了")
            self.assertIsNone(pos.get("entry_actual_price"), "前提：當下確實拿不到進場成交價")
            for fn in list(self.backfills):
                fn()
        self.assertAlmostEqual(pos.get("entry_actual_price") or 0, 4373.6, places=6,
                               msg="背景補登到的進場成交價要回填到部位(平倉時算真實 USDT 損益用)")
        got = [c.args[2] for c in ue.call_args_list if len(c.args) > 2 and c.args[2] is not None]
        self.assertEqual(got, [4373.6], "進場成交價補寫進資料庫(重啟後也還在)")
        self.assertEqual(len(self.bnotes), 1, "補發一則進場補登通知")


def _lagging(rows_by_time):
    """成交明細稍後才出現(r71/r72)：rows_by_time=[(第幾秒起看得到, 成交列)]，時間由假sleep推進。回傳(clock, sleep, userTrades)。"""
    clock = {"t": 0.0}
    def sleep(s):
        clock["t"] += s
    def trades(*a, **k):
        return (True, _fills(*[r for when, r in rows_by_time if clock["t"] >= when]))
    return clock, sleep, trades


class Lesson72(ExecHarness):
    """
    r72(pump-dump-hunter 對照 r71)：用「界線之後的平倉成交」算出場價時也要湊滿數量。gold-scalper 有三個地方用它：
    交易所端平倉的結帳、平倉分段成交後的加權、App 減碼的部分出場(這處本來就要求剛好等於減少量)。
    湊不滿 → 當成還查不到、估算、排背景補登；補登查到就更正資料庫與部位。
    另外：部分出場估算時界線沒推進，那段成交之後才出現，最後出場會把它混進去——要先跳過那一段。
    """
    OPEN = (101, 9001, "BUY", 1.0, 4391.2, 0.0, 1000)
    SEG1 = (150, 9100, "SELL", 0.4, 4380.0, -4.48, 2000)
    SEG2 = (151, 9200, "SELL", 0.6, 4384.0, -4.32, 2100)

    def _pos(self, **kw):
        p = _pos(real_open_quantity=1.0, entry_actual_price=4391.2, entry_price=4390.0, real_open_baseline=0.0,
                 real_open_order_id=9001, fill_boundary_id=101, backstop_algo_id=None, backstop_price=None, id=72)
        p.update(kw)
        self.eng._position = p
        return p

    def _run(self, rows_by_time, step):
        """在同一組(會隨時間出現的)成交明細下，先跑被測的那一步，再把排進來的背景補登跑完。"""
        clock, sleep, trades = _lagging(rows_by_time)
        self.bnotes = []
        with mock.patch.object(ex, "get_user_trades", side_effect=trades, create=True) as ut, \
             mock.patch.object(pt.time, "sleep", side_effect=sleep), \
             mock.patch.object(pt.db, "update_paper_trade_exit_execution") as ux, \
             mock.patch.object(pt.db, "update_paper_trade_fills") as uf, \
             mock.patch.object(pt.notifier_module.notifier, "notify_fill_backfill", create=True,
                               side_effect=lambda **k: self.bnotes.append(k)), \
             mock.patch.object(self.eng, "_cancel_backstop", return_value=False):
            out = step()
            n_sync = len(self.dbclose)
            for fn in list(self.backfills):
                fn()
        return out, ut, ux, uf, n_sync

    def _gone_step(self, pos):
        def step():
            pos["gone_checks"] = 2
            with mock.patch.object(ex, "get_position_info", return_value=(True, _rows(0, mark=4395.0))), \
                 mock.patch.object(ex, "close_position", return_value=(False, "目前沒有未平倉部位可以平")):
                return self.eng._check_exchange_quantity(pos)
        return step

    def _reduce_step(self, pos):
        def step():
            with mock.patch.object(ex, "get_position_info", return_value=(True, _rows(0.6, entry=4391.2, mark=4386.0))):
                return self.eng._check_exchange_quantity(pos)
        return step

    def test_r72_external_close_incomplete_fills_not_used_then_backfilled(self):
        pos = self._pos()
        st, ut, ux, _, n_sync = self._run([(0.0, self.OPEN), (0.0, self.SEG1), (2.0, self.SEG2)], self._gone_step(pos))
        self.assertEqual(st, "gone", "前提：判定交易所端已平掉")
        self.assertTrue(ut.called, "前提：真的查了成交明細")
        self.assertEqual((n_sync, len(self.dbclose_kw) >= 1, len(self.notes) > 0), (1, True, True), "前提：當下結帳了、通知有送出")
        self.assertTrue(self.dbclose_kw[0].get("pnl_estimated"), "平倉成交只先出現 0.4/1 張：當下不能拿來當出場價，要標估算")
        self.assertIsNone(self.notes[-1].get("real_pnl_usd"), "當下的 USDT 損益不能標成依真實成交價")
        self.assertEqual(len(self.dbclose), 2, "背景補登查到後要把平倉紀錄改寫一次(出場價、點數損益、取消估算)")
        vwap = 0.4 * 4380.0 + 0.6 * 4384.0
        self.assertAlmostEqual(self.dbclose[1][1], vwap, places=6, msg="改寫的出場價＝湊滿 1 張後的加權均價")
        self.assertIs(self.dbclose_kw[1].get("pnl_estimated"), False, "改寫後不再是估算")
        self.assertEqual(len(self.bnotes), 1, "補發一則補登通知")
        self.assertAlmostEqual(self.bnotes[0].get("real_pnl_usd") or 0, (vwap - 4391.2) * 1.0, places=6)

    def test_r72_external_close_fills_not_yet_visible_then_backfilled(self):
        pos = self._pos()
        st, ut, _, _, n_sync = self._run([(0.0, self.OPEN), (2.0, self.SEG1), (2.0, self.SEG2)], self._gone_step(pos))
        self.assertEqual(st, "gone", "前提：判定交易所端已平掉")
        self.assertEqual((n_sync, len(self.dbclose_kw) >= 1), (1, True), "前提：當下結帳了")
        self.assertTrue(self.dbclose_kw[0].get("pnl_estimated"), "前提：當下成交明細還沒出現，是估算")
        self.assertEqual(len(self.dbclose), 2, "界線之後的平倉成交稍後才出現：背景補登要改寫平倉紀錄")
        self.assertAlmostEqual(self.dbclose[1][1], 0.4 * 4380.0 + 0.6 * 4384.0, places=6)

    def test_r72_partial_close_weighting_waits_for_all_segments(self):
        # 平倉第一張只成交 0.4(r45 的待平倉)，第二張平掉剩下 0.6、回應有均價；出場價要兩段加權，但第一段稍後才出現在成交明細
        pos = self._pos(real_open_quantity=0.6, partial_close_filled=True, close_orig_qty=1.0)
        second = {"orderId": 9200, "status": "FILLED", "executedQty": "0.600", "avgPrice": "4384.0"}
        cp_box = []
        def step():
            with mock.patch.object(ex, "get_position_info", return_value=(True, _rows(0.6, entry=4391.2))), \
                 mock.patch.object(ex, "close_position", return_value=(True, second)) as cp:
                self.eng._close_position(pos, 4384.0, "觸及停損")
                cp_box.append(cp.call_count)
        self._run([(0.0, self.OPEN), (0.0, self.SEG2), (2.0, self.SEG1)], step)
        self.assertEqual(cp_box, [1], "前提：第二張平倉單送出、成交")
        self.assertGreater(len(self.notes), 0, "前提：平倉通知有送出")
        self.assertIsNone(self.notes[-1].get("real_pnl_usd"),
                          "兩段只查到一段：不能用一段(或只用最後一張單的均價)算整筆 1 張的真實損益")
        self.assertEqual(len(self.bnotes), 1, "背景補登查到兩段後補發通知")
        vwap = 0.4 * 4380.0 + 0.6 * 4384.0
        self.assertAlmostEqual(self.bnotes[0].get("real_pnl_usd") or 0, (vwap - 4391.2) * 1.0, places=6,
                               msg="兩段加權、數量用原本的 1 張")

    def test_r72_estimated_reduce_is_backfilled_and_boundary_advanced(self):
        pos = self._pos()
        st, _, _, uf, _ = self._run([(0.0, self.OPEN), (2.0, self.SEG1)], self._reduce_step(pos))
        self.assertEqual(st, "reduced", "前提：偵測到減少 0.4")
        self.assertEqual(pos.get("real_open_quantity"), 0.6, "前提：帳上數量更新成 0.6")
        self.assertEqual(pos.get("fill_boundary_id"), 150, "減碼那段成交稍後出現：背景補登後界線推進到那一筆")
        self.assertAlmostEqual(pos.get("partial_realized_usd") or 0, (4380.0 - 4391.2) * 0.4, places=6,
                               msg="部分出場的損益換成實際成交價算的(不是標記價估的)")
        self.assertFalse(pos.get("usd_estimated"), "補登後這筆不再含估算")
        self.assertTrue(uf.called, "界線要寫進資料庫(重啟後也在)")
        one(self.eng.alerts, "部分出場成交價補登")

    def test_r72_final_close_after_estimated_reduce_skips_that_segment(self):
        pos = self._pos()
        st1, _, _, _, _ = self._run([(0.0, self.OPEN)], self._reduce_step(pos))   # 減碼當下成交明細還沒出現：估算
        self.assertEqual(st1, "reduced", "前提：偵測到減少")
        self.assertTrue(pos.get("usd_estimated"), "前提：減碼那段是估算的")
        self.eng._position = pos
        self.backfills.clear()   # 那次補登還沒跑到，部位就被交易所端平掉了
        st2, _, _, _, n_sync = self._run([(0.0, self.OPEN), (0.0, self.SEG1), (0.0, self.SEG2)], self._gone_step(pos))
        self.assertEqual(st2, "gone", "前提：判定交易所端已平掉")
        self.assertEqual((n_sync, len(self.dbclose), len(self.notes) > 0), (1, 1, True), "前提：結帳了、通知有送出")
        self.assertEqual(self.dbclose[0][1], 4384.0, "最後出場只算剩下那 0.6 張(id151)，不能把減碼那段(id150)混進來")


class BackfillNoticeFormat(unittest.TestCase):
    """補登通知要真的跑格式化(ExecHarness 底下通知被 mock 掉，第20種)。"""
    def test_r71_backfill_notice_format(self):
        from app.notifier import notifier as N, TelegramNotifier
        self.assertTrue(hasattr(N, "notify_fill_backfill"), "前提：有補登通知(在舊版上重跑時是斷言失敗、不是崩掉)")
        sent = []
        with mock.patch.object(TelegramNotifier, "is_enabled", new_callable=mock.PropertyMock, return_value=True), \
             mock.patch.object(TelegramNotifier, "is_muted", new_callable=mock.PropertyMock, return_value=False), \
             mock.patch.object(N, "_send_telegram_message", side_effect=lambda t: sent.append(t) or (True, None)):
            N.notify_fill_backfill(action="close", label="15分K", account="gold", order_id=OID71, fill_price=4277.41,
                                   slippage_note="預期成交價4277.41 vs 實際成交價4277.41", real_pnl_usd=35.47, estimated_usd=35.44)
        self.assertEqual(len(sent), 1, "前提：通知真的組出來、送出了")
        one(sent, "成交價補登")   # 標題行(第21種：只看第一行)
        self.assertIn("4277.41", sent[0])
        self.assertIn(str(OID71), sent[0])
        self.assertIn("+35.47 USDT", sent[0])
        self.assertIn("依真實成交價", sent[0])
        self.assertIn("+35.44", sent[0], "附上原本的估算值，才看得出差多少")


# ------------------------------------------------------------------ r42 → r44 ＋ 實盤 2026-09-22
def _sqlite_pool():
    """用sqlite真的執行SQL(%s換成?)：驗證的是SQL本身，不是mock的回傳值。"""
    import sqlite3
    from datetime import datetime as _dt
    # 時間欄位要跟PostgreSQL一樣回datetime(不是字串)：程式會呼叫.isoformat()，模擬環境不能比真的寬鬆(r31退化值)
    sqlite3.register_converter("TIMESTAMP", lambda b: _dt.fromisoformat(b.decode()))
    conn = sqlite3.connect(":memory:", detect_types=sqlite3.PARSE_DECLTYPES)
    cols = ["id INTEGER PRIMARY KEY", "status TEXT", "engine_id TEXT", "direction TEXT", "entry_price REAL", "entry_time TIMESTAMP",
            "exit_price REAL", "exit_time TIMESTAMP", "exit_reason TEXT", "pnl_points REAL", "pnl_estimated INTEGER",
            "entry_expected_price REAL", "entry_actual_price REAL", "entry_slippage_points REAL", "entry_spread_points REAL",
            "exit_expected_price REAL", "exit_actual_price REAL", "exit_slippage_points REAL", "exit_spread_points REAL",
            "entry_book_stale INTEGER", "exit_book_stale INTEGER", "real_open_executed INTEGER", "real_open_quantity REAL",
            "sl_price REAL", "peak_price REAL", "trailing_active INTEGER", "chan_reason TEXT", "profile_reason TEXT",
            # 重啟還原(load_open_paper_trade)讀的欄位，r74 前這張模擬表沒有，讀持倉的 SQL 在這裡根本跑不了
            "interval_seconds INTEGER", "backstop_algo_id TEXT", "backstop_used_legacy INTEGER", "real_open_baseline REAL",
            "real_open_order_id INTEGER", "fill_boundary_id INTEGER", "partial_state TEXT",
            # r76：背景補登還沒完成的記號(進場／出場各一欄)
            "open_backfill TEXT", "exit_backfill TEXT"]
    conn.execute(f"CREATE TABLE paper_trades ({', '.join(cols)})")
    class Cur:
        def __init__(s): s.c = conn.cursor()
        def __enter__(s): return s
        def __exit__(s, *a): return False
        def execute(s, sql, params=()): s.c.execute(sql.replace("%s", "?"), params)
        def fetchall(s): return s.c.fetchall()
        def fetchone(s): return s.c.fetchone()
    class C:
        def cursor(s): return Cur()
        def commit(s): conn.commit()
    class Pool:
        def getconn(s): return C()
        def putconn(s, c): pass
    return Pool(), conn


class Lesson44(ExecHarness):
    # r43：設定讀不到不能靜靜回到預設(正式端的專屬覆寫會全部不見)
    def test_r43_settings_read_failure_is_not_defaults(self):
        S = pt.settings_module
        self.assertTrue(hasattr(S, "settings_loaded"), "前提：程式有「設定載入了沒」這個狀態")
        class Down:
            def getconn(s): raise RuntimeError("inj-settings-db")
            def putconn(s, c): pass
        pushed = []
        with mock.patch.object(S, "_loaded_from_db", False), mock.patch.object(S, "_last_load_attempt", 0.0, create=True), \
             mock.patch.object(pt.db, "_enabled", True), mock.patch.object(pt.db, "_pool", Down(), create=True), \
             mock.patch.object(pt.db, "save_app_settings") as save, \
             mock.patch.object(pt.notifier_module.notifier, "send_raw_message", side_effect=pushed.append):
            S.get_settings()
            loaded = S.settings_loaded()
            opened = self.eng._open_position({"direction": "bullish", "bid": 1, "ask": 1, "chan": {"reason": "t"},
                                              "profile": {"reason": "t"}}, 4390.0, 10.0)
        self.assertIs(loaded, False, "讀不到就是還沒載入，不能當成讀到了空的")
        self.assertEqual(save.call_count, 0, "讀取失敗期間不能把遷移用的時間戳寫回資料庫(會蓋掉原本的)")
        self.assertEqual(opened, "settings_not_loaded", "設定沒載入前不能開新倉(會用預設值交易)")
        self.assertIn("inj-settings-db", one(pushed, "讀不到交易設定"))

    # r44：網頁送來的表單格式錯，不能當成空表單
    def test_r44_bad_form_is_rejected_not_treated_as_empty(self):
        import asyncio
        import app.main as m
        with mock.patch.object(m.settings_module, "verify_password", return_value=(True, None)) as vp:
            r1 = asyncio.run(m.update_settings({"password": "x", "values": "paper_sl_points=8"}))
            r2 = asyncio.run(m.update_settings({"password": "x", "values": {}}))
            r3 = asyncio.run(m.settings_import({"password": "x", "param_set": {"format": "gold-scalper-param-set/1",
                                                "engine_id": self.eng.engine_id, "params": {}}, "force": True}))
        self.assertEqual(vp.call_count, 3, "前提：三次都通過了密碼檢查")
        for r in (r1, r2, r3):
            self.assertIs(r.get("success"), False, r)

    # r43/r44 第15條：平倉部分成交不能當成平掉
    def test_r44_partial_close_fill_is_not_closed(self):
        pos = _pos(real_open_quantity=0.1, entry_actual_price=4391.0, real_open_baseline=0.0, backstop_algo_id="B1")
        with mock.patch.object(ex, "close_position", return_value=(True, {"orderId": 5, "status": "FILLED", "executedQty": "0.040", "avgPrice": "4380.0"})) as cp, \
             mock.patch.object(ex, "get_position_info", return_value=(True, _rows(0.06))), \
             mock.patch.object(self.eng, "_cancel_backstop") as cb:
            self.eng._close_position(pos, 4380.0, "觸及停損")
        self.assertEqual(cp.call_count, 1, "前提：平倉單送出、成交了一部分")
        self.assertEqual(self.dbclose, [], "只成交0.04/0.1：不能結帳")
        self.assertFalse(cb.called, "不能撤交易所停損")
        self.assertTrue(pos.get("pending_close"), "剩下的記待平倉、每輪重試")
        self.assertAlmostEqual(pos.get("real_open_quantity"), 0.06, places=6)

    # r43/r44 第15條：交易所明確說沒成交(EXPIRED 0)就是沒成交，不是結果不明
    def test_r44_expired_zero_is_not_filled(self):
        expired = {"orderId": 9, "status": "EXPIRED", "executedQty": "0.000", "avgPrice": "0.00"}
        with mock.patch.object(ex, "_signed_request", return_value=(True, expired)) as sr, \
             mock.patch.object(ex, "round_quantity", return_value=(0.1, None)), mock.patch("time.sleep"):
            ok, res = ex.place_market_order("BUY", 0.1, account="gold", position_side="LONG")
        self.assertEqual(sr.call_count, 1, "前提：送出一張單；已經是最終狀態就不用再查")
        self.assertFalse(ok)
        self.assertFalse(ex.is_ambiguous_result(res), "交易所明確說沒成交，不用再等3分鐘確認")

    # r43/r44 第15條：卡在NEW的單要撤掉，再查一次拿最終成交量
    def test_r44_stuck_new_is_cancelled_then_final_state_used(self):
        new = {"orderId": 11, "status": "NEW", "executedQty": "0.000"}
        final = {"orderId": 11, "status": "CANCELED", "executedQty": "0.040", "avgPrice": "4373.6"}
        calls = []
        def fake(method, path, params=None, account=None, return_status=False):
            calls.append(method)
            r = new if method in ("POST", "GET") and calls.count("DELETE") == 0 else final
            return (True, r, 200) if return_status else (True, r)
        with mock.patch.object(ex, "_signed_request", side_effect=fake), \
             mock.patch.object(ex, "round_quantity", return_value=(0.1, None)), mock.patch("time.sleep"):
            ok, res = ex.place_market_order("BUY", 0.1, account="gold", position_side="LONG")
        self.assertIn("DELETE", calls, "查幾次仍是NEW：撤掉那張單")
        self.assertTrue(ok, "撤單後查到成交了0.04：算成交")
        self.assertEqual(ex.extract_filled_qty(res), 0.04, "數量用最終成交量")


class RealSqlDb(unittest.TestCase):
    """不mock資料庫、用sqlite真的執行SQL：ExecHarness把close_paper_trade整個mock掉，放在那裡測不到SQL(第20種)。"""
    def setUp(self):
        self.eng = _engine()

    # 實盤：r25起「先寫出場成交價、最後才結帳」，結帳把成交價覆寫成空的——網頁因此寫「成交價查不到」
    def test_live_close_record_does_not_wipe_exit_fill(self):
        pool, conn = _sqlite_pool()
        conn.execute("INSERT INTO paper_trades (id, status, engine_id, direction, entry_price, entry_time, entry_actual_price, "
                     "real_open_executed, real_open_quantity) VALUES (1,'open','e','bullish',4354.51,'2026-09-22T02:01:18+00:00',4354.51,1,0.1)")
        with mock.patch.object(pt.db, "_enabled", True), mock.patch.object(pt.db, "_pool", pool, create=True):
            pt.db.update_paper_trade_exit_execution(1, 4350.64, 4350.64, 0.0, 0.01)   # 平倉單成交(先)
            pt.db.close_paper_trade(1, 4350.65, "2026-09-22T02:45:56+00:00", "訊號反轉", -3.86)  # 結帳(後)
            rows = pt.db.get_closed_paper_trades(limit=5, engine_id="e")
        row = conn.execute("SELECT exit_actual_price, status FROM paper_trades WHERE id=1").fetchone()
        self.assertTrue(row, "前提：這一列還在")
        self.assertEqual(row[1], "closed", "前提：結帳真的執行了(不是被框架mock掉)")
        self.assertEqual(row[0], 4350.64, "結帳不能把已經寫進去的出場成交價清掉")
        self.assertEqual(len(rows), 1, "前提：讀得到這筆")
        self.assertEqual(rows[0].get("real_pnl_usd"), -0.39, "網頁跟Telegram一樣是-0.39(依真實成交價)")

    def test_web_list_gets_labeled_estimate_when_fill_missing(self):
        pool, conn = _sqlite_pool()
        conn.execute("INSERT INTO paper_trades (id, status, engine_id, direction, entry_price, entry_time, exit_price, exit_time, "
                     "pnl_points, real_open_executed, real_open_quantity) VALUES (2,'closed','e','bullish',4354.60,'2026-09-22T00:40:00+00:00',4362.74,'2026-09-22T00:54:55+00:00',8.15,1,0.1)")
        with mock.patch.object(pt.db, "_enabled", True), mock.patch.object(pt.db, "_pool", pool, create=True):
            rows = pt.db.get_closed_paper_trades(limit=5, engine_id="e")
        self.assertEqual(len(rows), 1, "前提：讀得到這筆")
        self.assertIsNone(rows[0].get("real_pnl_usd"), "前提：沒有真實成交價")
        self.assertEqual(rows[0].get("real_pnl_usd_est"), 0.82, "推估值＝點數×張數(8.15×0.1，跟真實損益一樣四捨五入到2位)")


    # r74：「還沒認領」的部分出場記號、部分出場損益，重啟後要還在；還原後要重新排補登
    def test_r74_unclaimed_reduce_survives_restart_and_is_backfilled(self):
        self.assertTrue(hasattr(pt.db, "update_paper_trade_partial_state"),
                        "前提：有把部分出場狀態寫進資料庫的函式(在舊版上重跑時是斷言失敗、不是崩掉)")
        eng = self.eng
        pool, conn = _sqlite_pool()
        conn.execute("INSERT INTO paper_trades (id, status, engine_id, direction, entry_price, entry_time, entry_actual_price, "
                     "sl_price, peak_price, trailing_active, real_open_executed, real_open_quantity, real_open_baseline, "
                     "real_open_order_id, fill_boundary_id) VALUES (74,'open',?,'bullish',4390.0,'2026-09-25T10:00:00+00:00',"
                     "4391.2,4370.0,4390.0,0,1,1.0,0.0,9001,101)", (eng.engine_id,))
        OPEN = (101, 9001, "BUY", 1.0, 4391.2, 0.0, 1000)
        SEG1 = (150, 9100, "SELL", 0.4, 4380.0, -4.48, 2000)
        clock, sleep, trades = _lagging([(0.0, OPEN), (2.0, SEG1)])
        sched = []
        with mock.patch.object(pt.db, "_enabled", True), mock.patch.object(pt.db, "_pool", pool, create=True), \
             mock.patch.object(pt, "_start_backfill_thread", side_effect=sched.append, create=True), \
             mock.patch.object(ex, "get_user_trades", side_effect=trades, create=True), \
             mock.patch.object(pt.time, "sleep", side_effect=sleep):
            eng._load_state()
            pos = eng._position
            self.assertIsNotNone(pos, "前提：從資料庫還原了開倉中的部位")
            with mock.patch.object(ex, "get_position_info", return_value=(True, _rows(0.6, entry=4391.2, mark=4386.0))):
                st = eng._check_exchange_quantity(pos)
            self.assertEqual(st, "reduced", "前提：偵測到 App 減碼 0.4")
            self.assertTrue(pos.get("usd_estimated"), "前提：那段成交還沒出現，損益是估算的")
            sched.clear()                               # 服務在補登跑之前重啟：排好的執行緒跟記憶體一起沒了
            eng._position, eng._seeded_from_db = None, False
            eng._load_state()
            pos2 = eng._position
            self.assertIsNotNone(pos2, "前提：重啟後還原了部位")
            self.assertAlmostEqual(pos2.get("unanchored_reduce_qty") or 0, 0.4, places=6,
                                   msg="「還沒認領」的減少量要存進資料庫，重啟後還在(否則最後出場會把那段混進去)")
            self.assertAlmostEqual(pos2.get("partial_realized_usd") or 0, (4386.0 - 4391.2) * 0.4, places=6,
                                   msg="部分出場的損益(估算值)也要還在")
            self.assertTrue(pos2.get("usd_estimated"), "估算標記也要還在")
            self.assertEqual(pos2.get("entry_actual_price"), 4391.2, "進場成交價要還原(算部分出場的實際損益要用)")
            self.assertEqual(len(sched), 1, "還原後要重新排補登(重啟前排的那個已經沒了)")
            for fn in list(sched):
                fn()
        self.assertEqual(pos2.get("fill_boundary_id"), 150, "那段出現後補登：界線推進")
        self.assertAlmostEqual(pos2.get("partial_realized_usd") or 0, (4380.0 - 4391.2) * 0.4, places=6)
        row = conn.execute("SELECT fill_boundary_id, partial_state FROM paper_trades WHERE id=74").fetchone()
        self.assertTrue(row, "前提：這一列還在")
        self.assertEqual(row[0], 150, "界線寫進資料庫")
        self.assertNotIn("unanchored_reduce_qty", str(row[1] or ""), "補登後資料庫裡的「還沒認領」記號要清掉")
        self.assertIn("partial_realized_usd", str(row[1] or ""), "前提：部分出場狀態真的寫進資料庫了")

    # r74：進場成交價的補登也只在記憶體裡排——補登跑之前重啟，還原時沒有進場成交價、有開倉單號就重新排
    def test_r74_missing_entry_price_is_backfilled_after_restart(self):
        self.assertTrue(hasattr(pt.db, "update_paper_trade_partial_state"), "前提：r74 的還原欄位存在(在舊版上重跑時是斷言失敗)")
        eng = self.eng
        pool, conn = _sqlite_pool()
        conn.execute("INSERT INTO paper_trades (id, status, engine_id, direction, entry_price, entry_time, sl_price, peak_price, "
                     "trailing_active, real_open_executed, real_open_quantity, real_open_baseline, real_open_order_id, fill_boundary_id) "
                     "VALUES (75,'open',?,'bullish',4373.49,'2026-09-25T10:00:00+00:00',4360.0,4373.49,0,1,0.1,0.0,17233000001,951)",
                     (eng.engine_id,))
        filled = {"orderId": 17233000001, "status": "FILLED", "executedQty": "0.100", "avgPrice": "4373.6"}
        sched = []
        with mock.patch.object(pt.db, "_enabled", True), mock.patch.object(pt.db, "_pool", pool, create=True), \
             mock.patch.object(pt, "_start_backfill_thread", side_effect=sched.append, create=True), \
             mock.patch.object(ex, "get_order_status", return_value=(True, filled)), \
             mock.patch.object(pt.notifier_module.notifier, "notify_fill_backfill", create=True), \
             mock.patch.object(pt.time, "sleep"):
            eng._load_state()
            pos = eng._position
            self.assertIsNotNone(pos, "前提：還原了部位")
            self.assertIsNone(pos.get("entry_actual_price"), "前提：資料庫裡沒有進場成交價")
            self.assertEqual(len(sched), 1, "還原時沒有進場成交價、有開倉單號：重新排補登")
            for fn in list(sched):
                fn()
        self.assertEqual(pos.get("entry_actual_price"), 4373.6, "補登到的進場成交價回填部位")
        row = conn.execute("SELECT entry_actual_price FROM paper_trades WHERE id=75").fetchone()
        self.assertTrue(row, "前提：這一列還在")
        self.assertEqual(row[0], 4373.6, "也寫進資料庫")

    # ---- r76：出場成交價的補登，重啟後靠平倉紀錄上的「補登還沒完成」記號重新排 ----
    NO_AVG_CLOSE = {"orderId": 9200, "symbol": "XAUUSDT", "status": "FILLED", "executedQty": "1.000", "cumQty": "1.000",
                    "side": "SELL", "positionSide": "LONG", "type": "MARKET", "updateTime": 1790400000000}

    def _open_row(self, conn, tid, **kw):
        f = dict(id=tid, status="open", engine_id=self.eng.engine_id, direction="bullish", entry_price=4390.0,
                 entry_time="2026-09-25T10:00:00+00:00", entry_actual_price=4391.2, sl_price=4370.0, peak_price=4390.0,
                 trailing_active=0, real_open_executed=1, real_open_quantity=1.0, real_open_baseline=0.0,
                 real_open_order_id=9001, fill_boundary_id=101)
        f.update(kw)
        conn.execute(f"INSERT INTO paper_trades ({', '.join(f)}) VALUES ({', '.join('?' * len(f))})", tuple(f.values()))

    def _close_with_missing_fill(self, pool, conn, trades, on_close_write=None):
        """從資料庫還原部位 → 平倉(回應沒均價、成交明細還沒出現) → 回傳(排進來的補登, 推播)。"""
        eng, sched, pushed = self.eng, [], []
        real_close = pt.db.close_paper_trade
        def close_write(*a, **k):
            if on_close_write:
                on_close_write()
            return real_close(*a, **k)
        with mock.patch.object(pt.db, "_enabled", True), mock.patch.object(pt.db, "_pool", pool, create=True), \
             mock.patch.object(eng, "_is_execution_engine", return_value=True), \
             mock.patch.object(pt.settings_module, "get_settings", return_value=_exec_settings(eng)), \
             mock.patch.object(ex, "current_hedge_mode", return_value=False), \
             mock.patch.object(pt, "_start_backfill_thread", side_effect=sched.append, create=True), \
             mock.patch.object(pt.db, "close_paper_trade", side_effect=close_write), \
             mock.patch.object(ex, "close_position", return_value=(True, dict(self.NO_AVG_CLOSE))), \
             mock.patch.object(ex, "get_order_status", return_value=(True, dict(self.NO_AVG_CLOSE))), \
             mock.patch.object(ex, "get_position_info", return_value=(True, _rows(1.0, entry=4391.2))), \
             mock.patch.object(ex, "get_user_trades", side_effect=trades, create=True), \
             mock.patch.object(pt.notifier_module.notifier, "notify_trade_event"), \
             mock.patch.object(pt.notifier_module.notifier, "send_raw_message", side_effect=pushed.append), \
             mock.patch.object(eng, "_cancel_backstop", return_value=True):
            eng._load_state()
            pos = eng._position
            self.assertIsNotNone(pos, "前提：從資料庫還原了部位")
            eng._close_position(pos, 4384.0, "觸及停損", bid=4384.0, ask=4384.1)
        return sched, pushed

    def _restart_and_run(self, pool, trades, sleep):
        eng, sched, pushed, bnotes = self.eng, [], [], []
        eng._position, eng._seeded_from_db = None, False
        for k in [k for k in vars(eng) if "rescan" in k]:
            setattr(eng, k, False)   # 新的行程：「每個行程只重新排一次」的旗標也跟著重來
        with mock.patch.object(pt.db, "_enabled", True), mock.patch.object(pt.db, "_pool", pool, create=True), \
             mock.patch.object(pt, "_start_backfill_thread", side_effect=sched.append, create=True), \
             mock.patch.object(ex, "get_order_status", return_value=(True, dict(self.NO_AVG_CLOSE))), \
             mock.patch.object(ex, "get_user_trades", side_effect=trades, create=True), \
             mock.patch.object(pt.time, "sleep", side_effect=sleep), \
             mock.patch.object(pt.notifier_module.notifier, "notify_fill_backfill", create=True,
                               side_effect=lambda **k: bnotes.append(k)), \
             mock.patch.object(pt.notifier_module.notifier, "send_raw_message", side_effect=pushed.append):
            eng._load_state()
            n = len(sched)
            for fn in list(sched):
                fn()
        return n, bnotes, pushed

    def test_r76_exit_backfill_is_rescheduled_after_restart(self):
        self.assertTrue(hasattr(pt.db, "set_paper_trade_backfill"), "前提：有補登記號(在舊版上重跑時是斷言失敗、不是崩掉)")
        pool, conn = _sqlite_pool()
        self._open_row(conn, 76)
        OPEN = (101, 9001, "BUY", 1.0, 4391.2, 0.0, 1000)
        FILL = (160, 9200, "SELL", 1.0, 4384.1, -7.1, 3000)
        clock, sleep, trades = _lagging([(0.0, OPEN), (2.0, FILL)])
        sched, _ = self._close_with_missing_fill(pool, conn, trades)
        self.assertEqual(len(sched), 1, "前提：當下查不到出場成交價，排了背景補登")
        row = conn.execute("SELECT status, exit_backfill, exit_actual_price FROM paper_trades WHERE id=76").fetchone()
        self.assertTrue(row, "前提：這一列還在")
        self.assertEqual((row[0], row[2]), ("closed", None), "前提：已結帳、還沒有出場成交價")
        self.assertTrue(row[1], "已平倉紀錄上要有「出場補登還沒完成」的記號")
        # 服務在補登跑之前重啟：排好的執行緒跟記憶體一起沒了
        n, bnotes, _ = self._restart_and_run(pool, trades, sleep)
        self.assertEqual(n, 1, "重啟後照記號重新排出場補登")
        row = conn.execute("SELECT exit_actual_price, exit_backfill FROM paper_trades WHERE id=76").fetchone()
        self.assertTrue(row, "前提：這一列還在")
        self.assertEqual(row[0], 4384.1, "重新排的補登查到出場成交價，寫進資料庫")
        self.assertIsNone(row[1], "補登完成：記號清掉")
        self.assertEqual(len(bnotes), 1, "補發補登通知")
        n2, _, _ = self._restart_and_run(pool, trades, sleep)
        self.assertEqual(n2, 0, "記號清掉之後，再重啟不會又排一次")

    def test_r76_marker_cleared_when_backfill_gives_up(self):
        self.assertTrue(hasattr(pt.db, "set_paper_trade_backfill"), "前提：有補登記號")
        pool, conn = _sqlite_pool()
        self._open_row(conn, 77)
        clock, sleep, trades = _lagging([(0.0, (101, 9001, "BUY", 1.0, 4391.2, 0.0, 1000))])   # 出場成交一直沒出現
        sched, _ = self._close_with_missing_fill(pool, conn, trades)
        self.assertEqual(len(sched), 1, "前提：排了背景補登")
        before = conn.execute("SELECT exit_backfill FROM paper_trades WHERE id=77").fetchone()
        self.assertTrue(before and before[0], "前提：放棄之前記號是在的(不然「清掉」是空的通過)")
        with mock.patch.object(pt.db, "_enabled", True), mock.patch.object(pt.db, "_pool", pool, create=True), \
             mock.patch.object(ex, "get_order_status", return_value=(True, dict(self.NO_AVG_CLOSE))), \
             mock.patch.object(ex, "get_user_trades", side_effect=trades, create=True), \
             mock.patch.object(pt.time, "sleep", side_effect=sleep):
            for fn in list(sched):
                fn()
        one(self.eng.alerts, "出場成交價補登失敗")
        row = conn.execute("SELECT exit_backfill FROM paper_trades WHERE id=77").fetchone()
        self.assertTrue(row, "前提：這一列還在")
        self.assertIsNone(row[0], "查不到(放棄)也要清掉記號，否則每次重啟都再查一輪")

    def test_r76_marker_is_written_before_the_close_record(self):
        self.assertTrue(hasattr(pt.db, "set_paper_trade_backfill"), "前提：有補登記號")
        pool, conn = _sqlite_pool()
        self._open_row(conn, 78)
        clock, sleep, trades = _lagging([(0.0, (101, 9001, "BUY", 1.0, 4391.2, 0.0, 1000))])
        seen = []
        self._close_with_missing_fill(pool, conn, trades, on_close_write=lambda: seen.append(
            conn.execute("SELECT exit_backfill FROM paper_trades WHERE id=78").fetchone()))
        self.assertEqual(len(seen), 1, "前提：寫了平倉紀錄")
        self.assertTrue(seen[0], "前提：寫平倉紀錄時這一列在")
        self.assertTrue(seen[0][0], "記號要在寫平倉紀錄(界線)之前就寫好：結帳後、排補登前當掉，重啟後才知道要補")

    def test_r76_entry_backfill_marker_on_closed_trade(self):
        self.assertTrue(hasattr(pt.db, "set_paper_trade_backfill"), "前提：有補登記號")
        pool, conn = _sqlite_pool()
        # 進場成交價還在補登，部位就平掉了，接著重啟：這筆已平倉、沒有進場成交價，靠記號重新排
        self._open_row(conn, 79, status="closed", entry_actual_price=None, exit_price=4384.0,
                       exit_time="2026-09-25T11:00:00+00:00", exit_actual_price=4384.1)
        filled = dict(self.NO_AVG_CLOSE, orderId=9001, side="BUY", avgPrice="4391.3")
        eng, sched = self.eng, []
        with mock.patch.object(pt.db, "_enabled", True), mock.patch.object(pt.db, "_pool", pool, create=True):
            pt.db.set_paper_trade_backfill(79, "open", json.dumps({"action": "open", "order_id": 9001, "want_qty": 1.0,
                                                                     "trade_id": 79, "direction": "bullish"}))
        with mock.patch.object(pt.db, "_enabled", True), mock.patch.object(pt.db, "_pool", pool, create=True), \
             mock.patch.object(pt, "_start_backfill_thread", side_effect=sched.append, create=True), \
             mock.patch.object(ex, "get_order_status", return_value=(True, filled)), \
             mock.patch.object(pt.notifier_module.notifier, "notify_fill_backfill", create=True), \
             mock.patch.object(pt.time, "sleep"):
            eng._load_state()
            self.assertEqual(len(sched), 1, "已平倉紀錄上有進場補登記號：重新排")
            for fn in list(sched):
                fn()
        row = conn.execute("SELECT entry_actual_price, open_backfill FROM paper_trades WHERE id=79").fetchone()
        self.assertTrue(row, "前提：這一列還在")
        self.assertEqual(row[0], 4391.3, "補登到的進場成交價寫進資料庫(網頁的真實損益跟著更正)")
        self.assertIsNone(row[1], "記號清掉")


class FrameworkState(unittest.TestCase):
    # r74：新增會開背景執行緒的函式，框架的預設也要一起改(第 14 種的另一個樣子)
    def test_r74_backfill_thread_is_not_really_started_by_default(self):
        self.assertTrue(hasattr(pt, "_start_backfill_thread"), "前提：程式有背景補登(在舊版上重跑時是斷言失敗、不是崩掉)")
        eng = _engine()
        started = []
        import threading as _th
        real = _th.Thread.start
        def spy(t):
            started.append(t.name)
            return real(t)
        before = len(_BACKFILL_DEFAULT)
        with mock.patch.object(_th.Thread, "start", spy):
            eng._schedule_fill_backfill("close", _pos(id=1), {"orderId": 1, "executedQty": "0.1"})
        self.assertEqual(len(_BACKFILL_DEFAULT), before + 1, "前提：真的排了一次補登，被框架的預設收下來")
        self.assertNotIn("fill-backfill", started, "沒有特別換掉的測試裡，補登執行緒也不能真的開(會在背景打網路)")

    def test_r74_real_backfill_start_runs_in_daemon_thread(self):
        """正式路徑：真的那個會開一條daemon執行緒去跑(框架預設換掉了，這項確認換掉的東西本身是對的)。"""
        self.assertIsNotNone(_REAL_BACKFILL_START, "前提：程式有背景補登")
        import threading as _th
        done = _th.Event()
        fn = lambda: done.set()
        fn._backfill_selftest = True   # tests.error_scan 據此排除這一項
        _REAL_BACKFILL_START(fn)
        self.assertTrue(done.wait(2), "背景執行緒真的跑了")
        ts = [t for t in _th.enumerate() if t.name == "fill-backfill"]
        self.assertTrue(all(t.daemon for t in ts), "daemon：不能擋住服務關閉")

    """用法第5點r44：框架的重設改成自動比對，不靠記得。"""
    def test_reset_restores_every_module_level_container(self):
        import importlib
        names = []
        for mod_name in _STATE_MODULES:
            mod = importlib.import_module(mod_name)
            for k, v in vars(mod).items():
                if k.startswith("_") and not k.startswith("__") and isinstance(v, (dict, list, set)):
                    names.append((mod, k))
        self.assertGreater(len(names), 5, "前提：真的掃到了模組層級的狀態")
        for mod, k in names:
            v = getattr(mod, k)
            v["__probe__"] = 1 if isinstance(v, dict) else None
            if isinstance(v, list): v.append("__probe__")
            if isinstance(v, set): v.add("__probe__")
        _reset_module_state()
        dirty = [f"{mod.__name__}.{k}" for mod, k in names if "__probe__" in (getattr(mod, k) if not isinstance(getattr(mod, k), dict) else getattr(mod, k).keys())]
        self.assertEqual(dirty, [], "框架重設沒有還原這些模組層級狀態")


# ------------------------------------------------------------------ r46 → r48
class Lesson48(ExecHarness):
    # 8b：風控讀不到平倉紀錄，不能當成「沒有交易」(斷路器永遠不會觸發)
    def test_r47_risk_guard_read_failure_blocks_real_orders(self):
        from app import risk_guard as RG
        class Down:
            def getconn(s): raise RuntimeError("inj-risk-read")
            def putconn(s, c): pass
        pushed = []
        with mock.patch.object(pt.db, "_enabled", True), mock.patch.object(pt.db, "_pool", Down(), create=True), \
             mock.patch.object(pt.notifier_module.notifier, "send_raw_message", side_effect=pushed.append):
            allowed, reason, kind = RG.check(self.eng, 0.1, sl_points=10.0, bid=4390.0, ask=4390.1)
        self.assertIs(allowed, False, "讀不到平倉紀錄：不知道今天虧多少、連虧幾筆，不能放行真實下單")
        self.assertIn("讀不到", str(reason))
        self.assertIn("inj-risk-read", one(pushed, "風控讀不到平倉紀錄"))

    # 8c：背景流程查交易所的幾秒之間，網頁手動平倉不能同時進來
    def test_r48_manual_close_waits_for_background_step(self):
        import threading
        eng = self.eng
        self.assertTrue(hasattr(pt, "OP_LOCK_WAIT"), "前提：程式有「引擎操作鎖」(在舊版上是斷言失敗、不是崩掉)")
        pos = _pos(real_open_quantity=0.1, entry_actual_price=4391.0, real_open_baseline=0.0, backstop_algo_id="B1")
        eng._position = pos
        in_query, release, order = threading.Event(), threading.Event(), []
        def slow_rows(*a, **k):
            order.append("背景查交易所")
            in_query.set()
            release.wait(3)
            return True, _rows(0.1)
        def background():
            with mock.patch.object(ex, "get_position_info", side_effect=slow_rows):
                eng._check_exchange_quantity(pos)
            order.append("背景完成")
        bg = threading.Thread(target=background, daemon=True)
        with mock.patch.object(pt, "OP_LOCK_WAIT", 0.3, create=True), \
             mock.patch.object(pt, "_latest_price", return_value=4380.0), \
             mock.patch.object(ex, "close_position", return_value=(True, {"executedQty": "0.100", "avgPrice": "4380"})) as cp, \
             mock.patch.object(eng, "_cancel_backstop", return_value=False):
            bg.start()
            self.assertTrue(in_query.wait(2), "前提：背景那條真的停在查交易所")
            ok, msg = eng.force_close()
            order.append("手動平倉回應")
            release.set()
            bg.join(3)
        self.assertEqual(cp.call_count, 0, "背景還在處理時，手動平倉不能同時送單")
        self.assertIs(ok, False)
        self.assertIn("背景正在處理", msg, "等不到鎖要回講明，不能讓請求一直掛著")
        self.assertEqual(order, ["背景查交易所", "手動平倉回應", "背景完成"], order)
        self.assertIs(eng._position, pos, "部位還在，沒被動到")

    def test_r48_lock_is_on_the_functions_not_a_caller(self):
        """鎖加在函式本身(裝飾器)，不是只加在背景迴圈的某一個呼叫端(r48 pump-dump-hunter 第一次就只加在呼叫端)。"""
        names = ["_check_exchange_quantity", "_check_backstop_present", "_sync_backstop", "_resolve_open_pending",
                 "_retry_pending_close", "force_close", "_close_position", "_open_position"]
        unlocked = [n for n in names if not getattr(getattr(pt.PaperTradingEngine, n), "_engine_op", False)]
        self.assertTrue(hasattr(pt, "OP_LOCK_WAIT"), "前提：程式有「引擎操作鎖」")
        self.assertEqual(unlocked, [], "這些會動部位、又會查交易所的函式沒有套引擎鎖")


class NoLock:
    """什麼都不擋的假鎖(對照組用，r50/r51)：同樣的情境換上它必須出事，才證明原本不出事是鎖擋下的。"""
    def acquire(self, *a, **k): return True
    def release(self): pass


class Lesson51(ExecHarness):
    def _signal(self):
        return {"direction": "bullish", "bid": 4389.9, "ask": 4390.0, "chan": {"reason": "t"}, "profile": {"reason": "t"}}

    def _open_patches(self, open_side_effect, rows_side_effect):
        return [mock.patch.object(pt.risk_guard, "check", return_value=(True, None, None)),
                mock.patch.object(ex, "resolve_position_mode", return_value=(False, None)),
                mock.patch.object(ex, "set_margin_type", return_value=(True, {})),
                mock.patch.object(ex, "set_leverage", return_value=(True, {})),
                mock.patch.object(ex, "open_position", side_effect=open_side_effect),
                mock.patch.object(ex, "get_position_info", side_effect=rows_side_effect),
                mock.patch.object(self.eng, "_sync_backstop")]

    # 8a：開倉函式本身、拿到鎖之後，再檢查一次「已經有部位」
    def test_r51_open_rechecks_existing_position_inside(self):
        eng = self.eng
        existing = _pos(real_open_quantity=0.1, real_open_executed=True)
        eng._position = existing
        ps = self._open_patches(lambda **k: (True, {"avgPrice": "4390", "executedQty": "0.100", "orderId": 1}),
                                lambda *a, **k: (True, _rows(0)))
        for p in ps: p.start()
        try:
            res = eng._open_position(self._signal(), 4390.0, 10.0)
            sent = ex.open_position.call_count
        finally:
            for p in ps: p.stop()
        self.assertIs(eng._position, existing, "原本那筆不能被蓋掉(停損、停利會沒人管)")
        self.assertEqual(sent, 0, "已經有部位：不能再送進場單")
        self.assertEqual(res, "already_has_position")

    def _race_two_opens(self):
        """
        兩條執行緒同時開倉：第一條停在「寫資料庫」(再檢查之後、記上部位之前的那一步 I/O)，第二條同時進來。
        回傳送出的進場單數。停在查交易所沒有用——那時部位已經記上了，再檢查自己就擋得住，證明不了鎖。
        """
        import threading
        eng = self.eng
        eng._position = None
        in_query, release, sent = threading.Event(), threading.Event(), []
        def slow_insert(position):
            if not in_query.is_set():
                in_query.set()
                release.wait(2)
            return None
        def rows(*a, **k):
            return True, _rows(0)
        def fake_open(**k):
            sent.append(1)
            return True, {"avgPrice": "4390", "executedQty": "0.100", "orderId": len(sent)}
        ps = self._open_patches(fake_open, rows) + [mock.patch.object(pt.db, "insert_open_paper_trade", side_effect=slow_insert)]
        for p in ps: p.start()
        try:
            t1 = threading.Thread(target=lambda: eng._open_position(self._signal(), 4390.0, 10.0), daemon=True)
            t1.start()
            self.assertTrue(in_query.wait(2), "前提：第一條真的停在寫資料庫(再檢查之後、記上部位之前)")
            t2 = threading.Thread(target=lambda: eng._open_position(self._signal(), 4390.0, 10.0), daemon=True)
            t2.start()
            t2.join(0.5)
            release.set()
            t1.join(3); t2.join(3)
        finally:
            for p in ps: p.stop()
        return len(sent)

    def test_r51_two_concurrent_opens_send_one_order(self):
        self.assertTrue(hasattr(self.eng, "_op_lock"), "前提：程式有引擎操作鎖(在舊版上是斷言失敗、不是崩掉)")
        self.assertEqual(self._race_two_opens(), 1, "同時開同一個引擎：只能送一張進場單")

    def test_r51_control_without_lock_two_orders_are_sent(self):
        """對照組：換上什麼都不擋的假鎖，同樣的情境必須送出兩張——證明上一項是鎖擋下的，不是剛好沒交錯。"""
        self.assertTrue(hasattr(self.eng, "_op_lock"), "前提：程式有引擎操作鎖")
        with mock.patch.object(self.eng, "_op_lock", NoLock()):
            self.assertEqual(self._race_two_opens(), 2, "沒有鎖時兩條都送了單：這個情境真的會交錯")

    def test_r51_control_manual_close_without_lock_sends_during_background(self):
        """r48 手動平倉那項的對照組：沒有鎖時，背景查交易所期間手動平倉會同時送單。"""
        import threading
        eng = self.eng
        self.assertTrue(hasattr(eng, "_op_lock"), "前提：程式有引擎操作鎖")
        pos = _pos(real_open_quantity=0.1, entry_actual_price=4391.0, real_open_baseline=0.0, backstop_algo_id="B1")
        eng._position = pos
        in_query, release = threading.Event(), threading.Event()
        def slow_rows(*a, **k):
            in_query.set(); release.wait(2)
            return True, _rows(0.1)
        def background():
            with mock.patch.object(ex, "get_position_info", side_effect=slow_rows):
                eng._check_exchange_quantity(pos)
        from app.binance_client import binance_streamer
        with mock.patch.object(eng, "_op_lock", NoLock()), \
             mock.patch.object(pt, "_latest_price", return_value=4380.0), \
             mock.patch.object(binance_streamer, "get_recent_trades", return_value=[{"price": 4380.0}]), \
             mock.patch.object(ex, "close_position", return_value=(True, {"executedQty": "0.100", "avgPrice": "4380"})) as cp, \
             mock.patch.object(eng, "_cancel_backstop", return_value=False):
            bg = threading.Thread(target=background, daemon=True)
            bg.start()
            self.assertTrue(in_query.wait(2), "前提：背景那條真的停在查交易所")
            eng.force_close()
            sent_during = cp.call_count
            release.set(); bg.join(3)
        self.assertEqual(sent_during, 1, "沒有鎖時，背景還在查交易所、手動平倉就送出了：r48那項確實是鎖擋下的")

    # 8a：網頁手動測試下單也要看引擎有沒有部位(拿到引擎鎖之後)
    def test_r51_manual_test_order_blocked_when_engine_has_position(self):
        import asyncio
        import app.main as m
        eng = self.eng
        eng._position = _pos(real_open_quantity=0.1, real_open_executed=True)
        with mock.patch.object(m.settings_module, "verify_password", return_value=(True, None)) as vp, \
             mock.patch.object(ex, "open_position", return_value=(True, {"avgPrice": "4391", "executedQty": "0.100"})) as op:
            res = asyncio.run(m.execution_test_order({"password": "x", "direction": "bullish", "quantity": 0.1,
                                                     "account": eng.execution_account, "confirm_live": True}))
        self.assertTrue(vp.called, "前提：通過了密碼檢查，擋下的是部位這一關")
        self.assertEqual(op.call_count, 0, "引擎有部位時手動下單會多一張程式不知道的部位")
        self.assertIn("部位", str(res.get("error")), res)

    # 8b：斷路器把每筆損益未知當成一次完整停損(-1R)，只會更早停
    def test_r50_breaker_counts_unknown_pnl_as_full_stop(self):
        from app import risk_guard as RG
        today = datetime_now_iso()
        eng = self.eng
        eng._closed_trades_memory.clear()
        eng._closed_trades_memory.extend([
            {"exit_time": today, "pnl_points": -1.0, "entry_price": 4390.0, "sl_price": 4380.0},
            {"exit_time": today, "pnl_points": None, "entry_price": 4390.0, "sl_price": 4360.0},   # 未知：停損距離30
        ])
        st = dict(pt.settings_module.get_settings(engine_id=eng.engine_id))
        st.update(paper_sl_points=8.0)
        with mock.patch.object(RG.db, "is_enabled", return_value=False), \
             mock.patch.object(RG.settings_module, "get_settings", return_value=st):
            daily = RG.get_daily_pnl_usd(eng, 0.1)
            streak = RG.get_consecutive_losses(eng)
        self.assertAlmostEqual(daily, (-1.0 - 30.0) * 0.1, places=6, msg="未知那筆以一次停損(停損距離30)計")
        self.assertEqual(streak, 2, "未知那筆算一次虧損，連續虧損2筆")


def datetime_now_iso():
    from datetime import datetime as _d, timezone as _tz
    return _d.now(_tz.utc).isoformat()


# ------------------------------------------------------------------ r52 → r54
class Lesson54(ExecHarness):
    """
    交易所端平掉後，App／別的專案在同一檔同方向開了新部位(均價 4400)，帳上還記著原本那筆(成交價 4391.2)。
    每一項都有對照組：帳上沒有成交價(均價比對失效)時，同樣的情境必須出事——證明擋下的是均價比對。
    gold-scalper 只有背景迴圈一個開倉入口，這組不牽涉引擎鎖，對照組只需要讓均價比對失效。
    """
    OTHER = 4400.0

    def _pos(self, entry_actual=4391.2, **kw):
        p = _pos(real_open_quantity=0.1, entry_actual_price=entry_actual, real_open_baseline=0.0,
                 backstop_algo_id=None, backstop_price=None, real_open_order_id=9001, fill_boundary_id=101)
        p.update(kw)
        self.eng._position = p
        return p

    def _other_rows(self):
        return (True, _rows(0.1, entry=self.OTHER))   # 交易所這一側：別人的0.1張，均價4400

    # 原本那筆的平倉成交(第二個證據，r56)：開倉那筆101、界線101之後有一筆SELL 0.1
    CLOSED = (True, _fills((101, 9001, "BUY", 0.1, 4391.2, 0.0, 1000), (150, 9100, "SELL", 0.1, 4380.0, -1.12, 2000)))
    NOT_CLOSED = (True, _fills((101, 9001, "BUY", 0.1, 4391.2, 0.0, 1000)))

    # 守衛補掛／掛停損
    def _sync(self, pos, fills=None):
        with mock.patch.object(ex, "get_position_info", return_value=self._other_rows()) as gp, \
             mock.patch.object(ex, "get_user_trades", return_value=fills or self.CLOSED, create=True), \
             mock.patch.object(ex, "place_algo_stop", return_value=(True, "S", False)) as pl:
            st = self.eng._sync_backstop(pos)
        return st, gp, pl

    def test_r54_backstop_not_placed_on_other_position(self):
        st, gp, pl = self._sync(self._pos())
        self.assertTrue(gp.called, "前提：掛停損前真的查了交易所")
        self.assertEqual(pl.call_count, 0, "交易所上是別人的部位(均價不同)：不能把我們的停損掛上去")
        self.assertEqual(st, "gone")

    def test_r54_control_backstop_placed_when_price_unknown(self):
        st, gp, pl = self._sync(self._pos(entry_actual=None))
        self.assertEqual(pl.call_count, 1, "對照組：沒有成交價可比對時，停損就掛到別人的部位上了")

    # 出場(背景判斷)、待平倉重試、網頁手動平倉：都走平倉
    def _close(self, pos, via, fills=None):
        fills = fills or self.CLOSED
        from app.binance_client import binance_streamer
        with mock.patch.object(ex, "get_position_info", return_value=self._other_rows()), \
             mock.patch.object(ex, "get_user_trades", return_value=fills, create=True) as self.fills_mock, \
             mock.patch.object(binance_streamer, "get_recent_trades", return_value=[{"price": 4401.0}]), \
             mock.patch.object(pt, "_latest_price", return_value=4401.0), \
             mock.patch.object(ex, "close_position", return_value=(True, {"executedQty": "0.100", "avgPrice": "4401"})) as cp, \
             mock.patch.object(self.eng, "_cancel_backstop", return_value=False):
            if via == "exit":
                self.assertTrue(self.eng._try_claim_close(pos), "前提：出場判斷認領了部位")
                self.eng._close_position(pos, 4401.0, "訊號反轉")
            elif via == "retry":
                pos["pending_close"] = {"price": 4401.0, "reason": "觸及停損"}
                self.eng._retry_pending_close()
            else:
                self.manual_result = self.eng.force_close()
        return cp

    def test_r54_exit_does_not_close_other_position(self):
        for via in ("exit", "retry", "manual"):
            with self.subTest(via=via):
                self.dbclose.clear()
                cp = self._close(self._pos(), via)
                self.assertEqual(cp.call_count, 0, f"{via}：交易所上是別人的部位，不能送平倉單把它平掉")
                self.assertEqual(len(self.dbclose), 1, f"{via}：原本那筆當成交易所端已平掉、照成交明細結帳")
                self.assertEqual(self.dbclose[0][1], 4380.0, f"{via}：出場價是原本那筆的平倉成交，不是別人的價格")

    def test_r54_control_exit_closes_other_position_when_price_unknown(self):
        for via in ("exit", "retry", "manual"):
            with self.subTest(via=via):
                cp = self._close(self._pos(entry_actual=None), via)
                self.assertEqual(cp.call_count, 1, f"對照組({via})：比對不了均價時，平倉單就送出、把別人的部位平掉了")

    # 數量比對(對帳)
    def test_r54_quantity_check_treats_other_position_as_gone(self):
        pos = self._pos()
        with mock.patch.object(ex, "get_position_info", return_value=self._other_rows()) as gp, \
             mock.patch.object(ex, "get_user_trades", return_value=self.CLOSED, create=True):
            st = self.eng._check_exchange_quantity(pos)
        self.assertTrue(gp.called, "前提：真的查了交易所")
        self.assertEqual(st, "gone_pending", "均價不同、查到平倉成交：原本那筆已經沒了(連續確認後結帳)，不是「數量沒變」")

    def test_r54_control_quantity_check_same_when_price_unknown(self):
        pos = self._pos(entry_actual=None)
        with mock.patch.object(ex, "get_position_info", return_value=self._other_rows()):
            st = self.eng._check_exchange_quantity(pos)
        self.assertEqual(st, "same", "對照組：比對不了均價時，別人的部位被當成原本那筆")

    # 啟動對帳
    def test_r54_startup_reconcile_reports_different_position(self):
        import app.main as m
        eng = self.eng
        eng._position = self._pos()
        got = []
        with mock.patch.object(ex, "get_position_info", return_value=self._other_rows()) as gp, \
             mock.patch.object(ex, "get_user_trades", return_value=self.CLOSED, create=True), \
             mock.patch.object(ex, "usdt_balance_line", return_value=None), mock.patch("time.sleep"), \
             mock.patch.dict(m.PAPER_TRADING_ENGINES, {"x": eng}, clear=True), \
             mock.patch.object(eng, "_is_execution_engine", return_value=True), \
             mock.patch.object(m.logger, "info", side_effect=got.append), mock.patch.object(m.db, "insert_settings_audit"):
            m._reconcile_with_exchange_on_startup()
        self.assertTrue(gp.called, "前提：真的查了交易所")
        self.assertGreater(len(got), 0, "前提：對帳訊息有寫出來")
        self.assertIn("平倉成交", got[-1], got[-1])
        self.assertNotIn("對帳一致", got[-1])

    # ---- r56/r57：均價有出入但「沒有平倉成交」＝同一筆，照常管理；成交明細查不到＝判斷不了 ----
    def test_r56_price_differs_but_never_closed_is_same_position(self):
        st, _gp, pl = self._sync(self._pos(), fills=self.NOT_CLOSED)
        self.assertEqual((st, pl.call_count), ("placed", 1), "沒有平倉成交：同一筆(記法不同)，停損照掛——不能撤掉還在場部位的保護")
        pos = self._pos()
        with mock.patch.object(ex, "get_position_info", return_value=self._other_rows()), \
             mock.patch.object(ex, "get_user_trades", return_value=self.NOT_CLOSED, create=True):
            self.assertEqual(self.eng._check_exchange_quantity(pos), "same", "數量比對：同一筆、數量沒變，不結帳")

    def test_r57_manual_close_sends_order_when_never_closed(self):
        self.dbclose.clear()
        cp = self._close(self._pos(), "manual", fills=self.NOT_CLOSED)
        self.assertTrue(self.fills_mock.called,
                        "前提：送單前真的比對了均價、查了成交明細(判定是同一筆)——不是因為部位查不到才照送(第16種)")
        self.assertEqual(cp.call_count, 1, "均價有出入但沒有平倉成交：手動平倉要真的送單(r57：不能回「已平倉」卻沒送)")
        self.assertEqual(len(self.manual_result), 2, "前提：手動平倉真的有回傳(成功與否, 訊息)")
        self.assertIs(self.manual_result[0], True, self.manual_result)
        self.assertIn("已以", self.manual_result[1])

    def test_r57_manual_close_says_no_order_when_already_closed(self):
        cp = self._close(self._pos(), "manual")
        self.assertEqual(cp.call_count, 0, "前提：交易所上是別人的部位、沒有送單")
        self.assertEqual(len(self.manual_result), 2, "前提：手動平倉真的有回傳(成功與否, 訊息)")
        self.assertIn("這次沒有送平倉單", self.manual_result[1], "回應要講明沒有送單，不能讓人以為是這次平掉的")
        self.assertIn("別的部位", self.manual_result[1], "平掉後被別人重開：要講交易所上現在那張沒有動它(r59)")

    # r61：對帳(數量比對連3輪判定部位不在)替原本那筆結帳時，結帳原因與通知照實際情況寫
    def _reconcile_close(self, rows, fills):
        pos = self._pos()
        pos["gone_checks"] = 2
        with mock.patch.object(ex, "get_position_info", return_value=rows) as gp, \
             mock.patch.object(ex, "get_user_trades", return_value=fills, create=True), \
             mock.patch.object(pt, "_latest_price", return_value=4401.0), \
             mock.patch.object(ex, "close_position") as cp, \
             mock.patch.object(self.eng, "_cancel_backstop", return_value=False):
            st = self.eng._check_exchange_quantity(pos)
        return st, gp, cp

    def test_r61_reconcile_reason_says_other_position_untouched(self):
        st, gp, cp = self._reconcile_close(self._other_rows(), self.CLOSED)
        self.assertEqual(st, "gone", "前提：對帳判定原本那筆已經沒了、結帳")
        self.assertEqual((cp.call_count, len(self.dbclose), len(self.notes) > 0), (0, 1, True), "前提：沒送單、結了帳、有發通知")
        reason = self.dbclose[0][3]
        self.assertIn("別的部位", reason, "結帳原因要寫明交易所上現在那張是別的部位")
        self.assertIn("沒有動它", reason)
        note = str(self.notes[-1].get("exchange_note") or "")
        self.assertIn("這次沒有送平倉單", note, "通知要講明沒有送單")
        self.assertIsNone(self.notes[-1].get("skip_reason"), "不能放進風控的欄位(通知會印成「已暫停真實下單」)")

    def test_r61_reconcile_reason_says_estimated_when_no_fills(self):
        st, gp, cp = self._reconcile_close((True, _rows(0)), (False, "Read timed out"))
        self.assertEqual(st, "gone", "前提：交易所這一側沒有部位、對帳結帳")
        self.assertEqual(len(self.dbclose), 1, "前提：結了帳")
        self.assertIn("推估", self.dbclose[0][3], "出場價查不到：結帳原因寫明是推估的")
        self.assertNotIn("別的部位", self.dbclose[0][3], "沒有重開：不能說有別的部位")

    # r59：手動平倉遇到「停損觸發、沒有重開」——交易所這一側已經沒有部位
    def _manual_close_flat(self, fills):
        pos = self._pos()
        from app.binance_client import binance_streamer
        with mock.patch.object(ex, "get_position_info", return_value=(True, _rows(0))), \
             mock.patch.object(ex, "get_user_trades", return_value=fills, create=True), \
             mock.patch.object(binance_streamer, "get_recent_trades", return_value=[{"price": 4379.0}]), \
             mock.patch.object(pt, "_latest_price", return_value=4379.0), \
             mock.patch.object(ex, "close_position", return_value=(False, "目前沒有未平倉部位可以平")) as cp, \
             mock.patch.object(self.eng, "_cancel_backstop", return_value=False):
            res = self.eng.force_close()
        return cp, res

    def test_r59_manual_close_after_stop_triggered_without_reopen(self):
        cp, res = self._manual_close_flat(self.CLOSED)
        self.assertEqual(cp.call_count, 0, "前提：交易所這一側已經沒有部位，送單前確認就知道，不送單")
        self.assertEqual(len(res), 2, "前提：手動平倉真的有回傳")
        self.assertIn("這次沒有送平倉單", res[1])
        self.assertIn("查到平倉成交", res[1])
        self.assertNotIn("別的部位", res[1], "沒有重開：不能說交易所上有別的部位")

    def test_r59_manual_close_message_does_not_claim_fills_when_unknown(self):
        cp, res = self._manual_close_flat((False, "Read timed out"))
        self.assertEqual(cp.call_count, 0, "前提：沒有送單")
        self.assertEqual(len(res), 2, "前提：手動平倉真的有回傳")
        self.assertNotIn("查到平倉成交", res[1], "成交明細查不到：不能寫查到了")
        self.assertIn("推估", res[1])

    # r59：「查不到」要直接驗證——部位正常在帳上，只在那一步讓查詢查不到，照 gold-scalper 自己的設計
    def test_r59_close_presend_query_failure_still_sends(self):
        """gold-scalper 平倉送單前「部位查不到」是照送(r55，跟以前一樣)；「均價不同、成交明細查不到」才不送(r56，另有測試)。"""
        pos = self._pos()
        self.assertIs(self.eng._position, pos, "前提：部位正常在帳上")
        self.assertTrue(self.eng._try_claim_close(pos), "前提：出場判斷認領了部位")
        with mock.patch.object(ex, "get_position_info", return_value=(False, "Read timed out")) as gp, \
             mock.patch.object(ex, "close_position", return_value=(True, {"executedQty": "0.100", "avgPrice": "4380"})) as cp, \
             mock.patch.object(self.eng, "_cancel_backstop", return_value=False):
            self.eng._close_position(pos, 4380.0, "觸及停損")
        self.assertEqual(gp.call_count, 1, "前提：送單前真的查了、查不到(只在平倉這一步)")
        self.assertEqual(cp.call_count, 1, "查不到：照送(不能因為查詢失敗就不平，停損要執行)")
        self.assertEqual(len(self.dbclose), 1, "平倉成交後結帳")

    def test_r59_quantity_check_query_failure_does_nothing(self):
        pos = self._pos()
        self.assertIs(self.eng._position, pos, "前提：部位正常在帳上")
        with mock.patch.object(ex, "get_position_info", return_value=(False, "Read timed out")) as gp, \
             mock.patch.object(pt.db, "update_paper_trade_real_open") as upd:
            st = self.eng._check_exchange_quantity(pos)
        self.assertEqual(gp.call_count, 1, "前提：數量比對這一步真的查了、查不到")
        self.assertEqual(st, "unknown")
        self.assertFalse(upd.called, "查不到：不改帳上數量")
        self.assertIsNone(pos.get("gone_checks"), "也不算成一次「部位不在」")
        self.assertIs(self.eng._position, pos, "部位留在帳上")

    def test_r56_fills_unknown_means_no_order_no_settle(self):
        unknown = (False, "Read timed out")
        st, _gp, pl = self._sync(self._pos(), fills=unknown)
        self.assertEqual((st, pl.call_count), ("unknown", 0), "判斷不了：不掛")
        for via in ("exit", "manual"):
            with self.subTest(via=via):
                self.dbclose.clear()
                pos = self._pos()
                cp = self._close(pos, via, fills=unknown)
                self.assertEqual(cp.call_count, 0, f"{via}：判斷不了，這輪不送任何單")
                self.assertEqual(self.dbclose, [], f"{via}：也不結帳")
                self.assertIs(self.eng._position, pos, f"{via}：部位留在帳上、記待平倉")

    def test_r57_preflight_lists_mismatched_positions(self):
        from app import preflight as pf
        self._pos()
        with mock.patch.dict(pt.PAPER_TRADING_ENGINES, {"x": self.eng}, clear=True), \
             mock.patch.object(ex, "get_position_info", return_value=self._other_rows()) as gp, \
             mock.patch.object(ex, "get_user_trades", return_value=self.NOT_CLOSED, create=True):
            self.assertTrue(hasattr(pf, "entry_price_check"), "前提：自檢有這一項")
            item = pf.entry_price_check(self.eng.execution_account)
        self.assertTrue(gp.called, "前提：真的查了交易所")
        self.assertEqual(item["status"], "warn", item)
        self.assertIn("沒有平倉成交 → 視為同一筆", item["msg"])

    # r53：傳進來的部位不是帳上那一筆就不動
    def test_r53_stale_position_object_is_not_acted_on(self):
        stale = self._pos()
        self.eng._position = _pos(real_open_quantity=0.1, entry_actual_price=4400.0)   # 帳上已經是另一筆
        with mock.patch.object(ex, "get_position_info", return_value=self._other_rows()) as gp, \
             mock.patch.object(ex, "place_algo_stop", return_value=(True, "S", False)) as pl:
            st = self.eng._sync_backstop(stale)
        self.assertEqual((st, pl.call_count), ("stale", 0), "傳進來的是舊的那筆：不查、不掛")
        self.assertFalse(gp.called)


class LiveReadiness(unittest.TestCase):
    """正式端可以直接改達標門檻(只有這四個)；其他全域設定照樣擋。真的打端點(role=live)。"""
    def setUp(self):
        _reset_module_state()

    def _client(self):
        from fastapi.testclient import TestClient
        import app.main as m
        return m, TestClient(m.app)

    def test_live_can_update_readiness_only(self):
        from app import role as R
        m, c = self._client()
        S = pt.settings_module
        self.assertTrue(hasattr(S, "READINESS_KEYS"), "前提：程式有「達標門檻」這組欄位的清單(在舊版上是斷言失敗、不是崩掉)")
        before = S.get_settings()["readiness_max_drawdown_points"]
        new = 31.0 if before != 31.0 else 32.0
        with mock.patch.object(R, "APP_ROLE", "live"), mock.patch.object(pt.db, "is_enabled", return_value=False), \
             mock.patch.object(m.settings_module, "verify_password", return_value=(True, None)) as vp:
            r_ok = c.post("/settings/readiness", json={"password": "x", "values": {"readiness_max_drawdown_points": new}}).json()
            r_bad = c.post("/settings/readiness", json={"password": "x", "values": {"readiness_min_trades": 20, "paper_sl_points": 99}}).json()
            blocked = c.post("/settings", json={"password": "x", "values": {"paper_sl_points": 99}}).status_code
            after = dict(S.get_settings())
        try:
            self.assertEqual(vp.call_count, 2, "前提：兩次都通過了密碼檢查(/settings 在正式端的最外層就擋掉)")
            self.assertIs(r_ok.get("success"), True, r_ok)
            self.assertEqual(after["readiness_max_drawdown_points"], new, "正式端改得了達標門檻")
            self.assertIs(r_bad.get("success"), False, "多帶交易參數：整批拒絕")
            self.assertIn("paper_sl_points", r_bad.get("error", ""))
            self.assertNotEqual(after["paper_sl_points"], 99, "交易參數沒有被改到")
            self.assertNotEqual(after["readiness_min_trades"], 20, "整批拒絕：同一批的門檻欄位也沒有套用")
            self.assertGreaterEqual(blocked, 400, "正式端的 POST /settings 照樣擋掉")
        finally:
            S.update_settings({"readiness_max_drawdown_points": before}) if not pt.db.is_enabled() else None


class WebBodies(unittest.TestCase):
    """8a：真的打端點，確認不是物件的請求內容進不到程式(不只看型別宣告)。"""
    def test_r47_non_object_bodies_are_rejected_before_code(self):
        from fastapi.testclient import TestClient
        import app.main as m
        c = TestClient(m.app)
        with mock.patch.object(m.settings_module, "update_settings") as us, \
             mock.patch.object(m.settings_module, "update_engine_overrides") as ueo, \
             mock.patch.object(m.settings_module, "verify_password", return_value=(True, None)) as vp:
            codes = {}
            for path in ("/settings", "/settings/import", "/control/flatten", "/execution/test-order"):
                for body in ("null", "[]", '"x"'):
                    r = c.post(path, content=body, headers={"content-type": "application/json"})
                    codes[(path, body)] = r.status_code
                codes[(path, "(沒帶)")] = c.post(path).status_code
        self.assertGreater(len(codes), 10, "前提：真的打了這些端點")
        self.assertEqual({k: v for k, v in codes.items() if v != 422}, {}, "不是物件、或沒帶內容：要在進程式之前就被擋下")
        self.assertEqual((us.call_count, ueo.call_count, vp.call_count), (0, 0, 0), "一次都沒有進到程式")


class NotifierExchangeClose(unittest.TestCase):
    """放在不 mock 通知的情境：ExecHarness 把 notify_trade_event 整個 mock 掉(第20種，這是第四次)。"""
    def test_r61_notice_for_exchange_close_is_not_breaker_wording(self):
        from app.notifier import notifier as N, TelegramNotifier
        sent = []
        with mock.patch.object(TelegramNotifier, "is_enabled", new_callable=mock.PropertyMock, return_value=True), \
             mock.patch.object(TelegramNotifier, "is_muted", new_callable=mock.PropertyMock, return_value=False), \
             mock.patch.object(N, "_send_telegram_message", side_effect=lambda t: sent.append(t) or (True, None)):
            self.assertIn("exchange_note", N.notify_trade_event.__code__.co_varnames, "前提：通知有「交易所端已平倉」這個欄位")
            N.notify_trade_event(action="close", label="15分K", direction="bullish", price=4380.0, exit_reason="交易所端部位已不在",
                                 pnl_points=-10.0, executed=None, quantity=0.1,
                                 exchange_note="送單前確認：交易所這一側已經沒有這一筆，這次沒有送平倉單")
        self.assertEqual(len(sent), 1, "前提：通知真的組出來、送出了")
        self.assertIn("這次沒有送平倉單", sent[0])
        self.assertNotIn("已暫停真實下單", sent[0], "交易所端已平倉不是風控暫停")


class StaticChecks(unittest.TestCase):
    """全域／靜態檢查自成一個情境(用法第5點r25)：不放在別的情境最後，才不會繼承那個情境的突變命中次數。"""
    RETURN_FUNCS = ("_check_backstop_present", "_sync_backstop", "_check_exchange_quantity",
                    "_resolve_open_pending", "_retry_pending_close", "force_close")

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

    def test_web_trade_entries_return_on_every_exit(self):
        """網頁交易入口(第2條r31)：每個出口都要回傳給網頁，不能掉出函式回None(網頁會拿到null)。"""
        import ast, inspect, textwrap
        import app.main as m
        for fn in ("execution_test_order", "execution_test_close", "execution_set_leverage",
                   "execution_cancel_open_orders", "control_flatten"):
            f = getattr(m, fn)
            f = getattr(f, "__wrapped__", f)  # guard_trade 包過一層
            tree = ast.parse(textwrap.dedent(inspect.getsource(f))).body[0]
            n_returns = sum(1 for n in ast.walk(tree) if isinstance(n, ast.Return))
            self.assertGreater(n_returns, 1, f"前提：{fn} 真的解析到了多個出口")
            self.assertEqual(self.return_problems(tree), [], fn)

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
