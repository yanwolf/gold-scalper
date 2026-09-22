"""
模擬單(paper trading)追蹤引擎 —— 即時版本，支援多週期平行追蹤。

實際的開倉/移動停損/出場判斷規則都在 app/trading_core.py (純函式，
跟資料庫、背景執行緒無關)，這個檔案只負責：
1. 背景執行緒定期呼叫 app/signal_engine.py 取得即時訊號
2. 呼叫 trading_core 的純函式決定要不要開倉/更新停損/出場
3. 把結果寫進資料庫(有接的話)，並且維護記憶體中的目前倉位狀態

回測(app/backtest.py)呼叫的是同一套 trading_core 純函式，
確保「即時模擬單」和「歷史回測」用的是完全一樣的交易規則。

多週期平行追蹤：PaperTradingEngine用interval_seconds參數化，可以同時
建立多個實例(例如1分K跟5分K)各自獨立追蹤、各自累積績效，彼此不會互相
干擾，資料庫裡用interval_seconds欄位區分每筆紀錄屬於哪個週期。
"""

import os
import functools
import threading
import time
import logging
from collections import deque
from datetime import datetime, timezone

from app.signal_engine import compute_full_signal
from app import db
from app import trading_core
from app import settings as settings_module
from app import notifier as notifier_module
from app import alert_cadence
from app import execution as execution_module
from app import risk_guard
from app.trading_stats import compute_stats, assess_readiness, compute_slippage_impact
from app.analysis import trend_filter_allows

logger = logging.getLogger("paper_trading")

PAPER_POLL_SECONDS = int(os.getenv("PAPER_POLL_SECONDS", "15"))

DEFAULT_BUCKET_SIZE = 1.0
DEFAULT_TRADE_LIMIT = 3000

MAX_MEMORY_TRADES = 500  # 沒有資料庫時，最多在記憶體保留這麼多筆已平倉紀錄



OPEN_PENDING_SECONDS = 180  # 開倉回應不明時，保留待確認的期限(第3條，r13)
OP_LOCK_WAIT = 10.0   # 網頁操作等引擎操作鎖的上限(秒)，等不到就回「背景正在處理」(第8條r48)


def _engine_op(web=False):
    """
    引擎操作鎖(第8條r48)：會動部位、又會查交易所的步驟(對帳、數量比對、停損守衛、掛停損、認領、重試平倉、平倉、開倉、
    手動平倉)共用一把可重入鎖。背景流程查交易所的幾秒之間，網頁手動平倉不能同時進來改部位。
    鎖加在函式本身(不是加在背景迴圈的某一個呼叫端，否則直接呼叫這些函式時照樣交錯)。
    web=True：網頁那邊等鎖有上限，拿不到就回「背景正在處理」，不讓請求一直掛著。
    """
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(self, *args, **kwargs):
            if web:
                if not self._op_lock.acquire(timeout=OP_LOCK_WAIT):
                    return False, f"背景正在處理這個部位(查交易所中)，{OP_LOCK_WAIT:.0f} 秒內等不到，請稍後再試"
            else:
                self._op_lock.acquire()
            try:
                return fn(self, *args, **kwargs)
            finally:
                self._op_lock.release()
        wrapper._engine_op = True
        return wrapper
    return deco


QTY_CHECK_EVERY_TICKS = 4
FILL_PAGE_SIZE = 1000      # 成交明細每頁筆數(幣安上限1000)
FILL_MAX_PAGES = 10        # 界線之後超過這麼多頁就當成沒拿完、記未知(第8條r37)   # 每4輪(約60秒)比對一次交易所數量(第8條減碼偵測；第6條限流)


def _latest_price():
    """最新成交價，給「每輪重試平倉」用(不等下一根K棒的出場訊號)。"""
    try:
        from app.binance_client import binance_streamer
        trades = binance_streamer.get_recent_trades(limit=1)
        return trades[-1]["price"] if trades else None
    except Exception:
        return None

class PaperTradingEngine:
    def __init__(self, interval_seconds=60, label=None, strategy_type="chan_profile",
                 resonance_min_conditions=4, execution_index=None, engine_id=None,
                 execution_account="gold", execution_symbol=None):
        self.interval_seconds = interval_seconds
        self.label = label or f"{interval_seconds}秒K線"
        self.strategy_type = strategy_type
        self.resonance_min_conditions = resonance_min_conditions
        # execution_index是這個引擎在「真實下單」設定裡的固定編號(見settings.py的
        # execution_engine_index說明)，取代原本直接比對interval_seconds的做法——
        # 現在同一個K線週期可能有多個策略的引擎平行運作(例如1分K纏論 vs 1分K共振)，
        # 光用interval_seconds已經無法唯一決定「哪一個」引擎該負責真實下單。
        self.execution_index = execution_index
        # execution_account/execution_symbol：這個引擎真實下單時要用哪個幣安帳戶、
        # 哪個商品(見execution.py的多帳戶設計說明)。預設帳戶"gold"對應現有的黃金
        # 交易設定，之後新增BTC等其他商品時，讓對應的模擬單引擎指定不同的帳戶
        # 名稱(例如"btc")，各自用獨立的子帳戶下單，避免共用帳戶造成部位互相
        # 抵銷(修正記錄見README)。execution_symbol不指定時，execution.py會依
        # 帳戶名稱決定要用哪個預設商品。
        self.execution_account = execution_account
        self.execution_symbol = execution_symbol
        # engine_id是資料庫查詢用的真正唯一鍵，預設用"策略_週期"組合，
        # 不指定的話自動產生(例如"chan_profile_60")
        self.engine_id = engine_id or f"{strategy_type}_{interval_seconds}"

        # 可重入(第8條r37)：存檔/讀檔失敗的推播路徑若回頭拿同一把鎖，不可重入的Lock會讓整支程式卡住
        self._lock = threading.RLock()
        self._op_lock = threading.RLock()  # 引擎操作鎖(第8條r48)，見 _engine_op
        self._position = None
        self._closed_trades_memory = deque(maxlen=MAX_MEMORY_TRADES)
        self._thread = None
        self._stop_flag = threading.Event()
        self._seeded_from_db = False
        self._last_tick_at = None  # 給health_monitor.py檢查引擎是否還活著用
        self._circuit_breaker_alerted = False  # 避免風控斷路器每次被觸發都重複發送警示
        self._fast_stop_registered = False
        self._orphan_cancels = []  # 平倉後撤不掉的殘留條件單(第13條)，每輪tick重試
        self._qty_check_tick = 0
        # 每輪各步驟的出錯次數(第8條r18/r19)。以步驟名為鍵、存在引擎上；跟部位有關的步驟
        # 在部位結束時清掉，同一引擎下一筆部位才不會接著數
        self._step_errors = {}

    @property
    def last_tick_at(self):
        return self._last_tick_at

    def _is_execution_engine(self, s):
        """
        這個引擎目前是不是被綁定真實下單。支援兩個槽位(execution_engine_index /
        execution_engine_index_2)，讓兩個引擎能同時真實下單——用途是加快累積滑價
        統計資料(例如15分K跟1分K同時跑)。前提是兩個引擎各自綁不同的幣安帳戶
        (execution_account)，同一個帳戶被兩個引擎同時下單會發生部位互相抵銷
        (修正記錄見README)。
        """
        if self.execution_index is None:
            return False
        return self.execution_index in (s.get("execution_engine_index"), s.get("execution_engine_index_2"))

    def start(self):
        if not self._seeded_from_db:
            self._ensure_state_loaded()

        if not self._fast_stop_registered:
            # 快速停損監控：掛在幣安bookTicker(買一/賣一)串流上，sub-second更新，
            # 遠快於15秒一次的模擬單tick。只做「現價有沒有穿過停損位」這件輕量比較，
            # 不重算指標；找到觸發就直接平倉，把「價格已觸發但還沒偵測到」的視窗從
            # 最多15秒縮短到接近即時，藉此減少非策略造成的滑價損失(修正記錄見README)。
            from app.binance_client import binance_streamer
            binance_streamer.add_price_listener(self._fast_stop_check)
            self._fast_stop_registered = True

        if self._thread and self._thread.is_alive():
            return
        self._stop_flag.clear()
        self._thread = threading.Thread(target=self._run_forever, daemon=True)
        self._thread.start()
        logger.info(f"模擬單追蹤引擎已啟動({self.label}，{self.strategy_type}策略，移動停損模式)")

    def _place_backstop(self, position, quantity, hedge):
        """開倉成功後第一次掛backstop：記下想要的停損價，交給_sync_backstop去掛。"""
        position["backstop_hedge"] = bool(hedge)
        self._sync_backstop(position, known_qty=quantity)  # 剛成交的回應就是部位在的證據

    def _move_backstop(self, position):
        """移動停損更新時：一樣交給_sync_backstop(它會比對想要的價位跟實際掛著的)。"""
        self._sync_backstop(position)

    @_engine_op()
    def _sync_backstop(self, position, known_qty=None):
        """
        讓交易所的backstop停損單對齊「目前想要的停損價」(程式內sl_price)。
        BINANCE_LESSONS.md第8條：掛不上不能只告警一次——下一格tick停損不一定再移動，
        錯過就永遠停在舊價位。所以這支每輪tick都會被呼叫，只要實際掛著的價位跟想要的
        不一樣(包括根本還沒掛上)，就再試一次，直到成功為止：
          - 還沒有backstop → 掛新的
          - 價位不同 → 先掛新的、成功才撤舊的(撤舊失敗記進stale，平倉時再撤，第13條)
          - 交易所回-2021(觸發價已經被穿過，會立刻觸發) → 代表價格已經穿過停損，
            直接走正常出場流程，不再掛單
        失敗照第8條告警節奏推播(_backstop_failed)，恢復時通知一次。
        known_qty：呼叫端手上有「部位在」的正向證據(剛成交的回應、交易所那一列)時直接傳數量，
        不重查——交易所剛成交時偶爾還沒反映，重查拿到0會誤判成部位不在而不掛停損(第2條r21/r22)。
        回傳這一輪的結果：skip / aligned / unknown(查不到) / gone(確認沒了) / placed / failed / exit
        """
        if not position.get("real_open_executed") or not position.get("real_open_quantity"):
            return "skip"
        if position.get("_closing"):
            return "skip"
        try:
            symbol = self.execution_symbol or execution_module.DEFAULT_SYMBOL
            desired = execution_module.round_price(position["sl_price"], symbol, account=self.execution_account)
            if position.get("backstop_algo_id") and position.get("backstop_price") == desired:
                return "aligned"
            # 掛任何一張停損單之前(第一次掛、搬移、守衛補掛都走這裡)，先逐幣確認自己的部位還在
            # (第2條r19)：部位若已被平掉、只是還沒偵測到，補上去的就是孤兒reduce-only單。
            # 查不到→這輪不動(不算掛單失敗)；扣基準後沒了→不掛，交給數量比對/對帳；
            # 還在→數量取「交易所−基準」與帳上的小者
            if known_qty is not None:
                ok_q, own = True, float(known_qty)
            else:
                ok_q, own = self._own_qty(position)
            if not ok_q:
                logger.info(f"掛停損前查不到部位({self.label})，這輪不動")
                return "unknown"
            if own <= 1e-9:
                logger.warning(f"掛停損前確認自己的部位已不在({self.label})，不掛，交給數量比對確認")
                return "gone"
            stop_qty = round(min(own, position["real_open_quantity"]), 6)
            s = settings_module.get_settings(engine_id=self.engine_id)
            hedge = position.get("backstop_hedge")
            if hedge is None or position.get("backstop_algo_id"):
                # 搬移時用交易所實際模式(第7條)；第一次掛用開倉當下resolve出來的模式
                hedge = execution_module.current_hedge_mode(
                    account=self.execution_account, default=bool(s.get("execution_hedge_mode", 1))
                )
            position_side = ("LONG" if position["direction"] == "bullish" else "SHORT") if hedge else None
            ok, new_id, used_legacy = execution_module.place_algo_stop(
                position["direction"], stop_qty, desired,
                symbol=symbol, account=self.execution_account, position_side=position_side,
            )
            if not ok:
                code = new_id.get("code") if isinstance(new_id, dict) else None
                if code == -2021:
                    # 觸發價已經被穿過：價格已經到停損了，直接出場(第8條)
                    logger.warning(f"backstop觸發價{desired}已被穿過({self.label})，直接出場")
                    if self._try_claim_close(position):
                        self._close_position(position, desired, "觸及移動停損" if position.get("trailing_active") else "觸及停損")
                    return "exit"
                self._backstop_failed(position, symbol, desired, new_id)
                return "failed"

            old_id, old_legacy = position.get("backstop_algo_id"), position.get("backstop_used_legacy", False)
            position["backstop_algo_id"] = new_id
            position["backstop_used_legacy"] = used_legacy
            position["backstop_price"] = desired
            db.update_paper_trade_backstop(position.get("id"), new_id, used_legacy)
            if old_id:
                cancel_ok, cancel_res, _ = execution_module.cancel_algo_stop(
                    old_id, symbol=symbol, account=self.execution_account, used_legacy=old_legacy,
                )
                if not cancel_ok:
                    # 帶quantity的reduceOnly單部位歸零後不會自動消失(第13條)。撤不掉的當下就是
                    # 失敗發生點：就在這裡進待撤清單(計數1、告警)，之後每輪重試，不等平倉(r10第8條)
                    self._queue_orphan_cancel(old_id, old_legacy, symbol, cancel_res)
                    logger.warning(f"舊backstop撤不掉({self.label}, algoId={old_id})，已進待撤清單")
            failed = position.pop("backstop_fail_count", 0)
            if failed:
                self._backstop_alert(
                    f"✅ {self.label} 交易所停損單已補上(失敗 {failed} 次後恢復)\n"
                    f"幣別：{symbol}\n目前交易所停損價：{desired}"
                )
            logger.info(f"backstop已對齊({self.label}): algoId={new_id}, 停損價{desired}")
            return "placed"
        except Exception as e:
            # 例外也是一次失敗，要計數、照節奏告警(r10第8條：失敗不能發生在計數之外)
            logger.error(f"同步backstop停損單發生例外({self.label}): {e}")
            self._backstop_failed(position, self.execution_symbol or execution_module.DEFAULT_SYMBOL,
                                  position.get("sl_price"), f"程式例外：{e}")
            return "failed"

    def _backstop_failed(self, position, symbol, desired, error):
        """
        backstop掛單/搬移失敗的唯一計數點(r10第8條「計數和恢復放錯位置」)：
        交易所拒絕、程式例外、第幾次呼叫都走這裡，計數與告警在同一處。
        沒有backstop且補掛失敗(第2條)也是這條。
        """
        n = position.get("backstop_fail_count", 0) + 1
        position["backstop_fail_count"] = n
        if alert_cadence.should_alert(n):
            self._backstop_alert(
                f"⚠️ {self.label} 交易所停損單{'搬移' if position.get('backstop_algo_id') else '掛單'}失敗(第 {n} 次)\n"
                f"幣別：{symbol}\n想要的停損價：{desired}\n"
                f"目前交易所停損價：{self._current_backstop_text(position)}\n"
                f"錯誤：{error}\n"
                f"每輪自動重試直到成功；程式內停損仍正常運作"
            )
        logger.warning(f"backstop掛單失敗({self.label}, 第{n}次)，下一輪重試: {error}")

    def _current_backstop_text(self, position):
        if not position.get("backstop_algo_id"):
            return "未掛(交易所端沒有停損)"
        if position.get("backstop_price") is None:
            return f"已掛(algoId={position['backstop_algo_id']}，服務重啟後價位未知)"
        return f"{position['backstop_price']}"

    def _queue_orphan_cancel(self, algo_id, used_legacy, symbol, error):
        """
        平倉後撤不掉的單(第13條殘留單)：放進引擎層級的待撤清單，之後每輪tick重試，
        照第8條的告警節奏提醒直到撤掉。部位已經不在了，所以不能掛在position上。
        (只存記憶體；服務重啟後由preflight的「孤兒條件單」自檢兜底)
        """
        for item in self._orphan_cancels:
            if item["algo_id"] == algo_id:
                return
        self._orphan_cancels.append({
            "algo_id": algo_id, "used_legacy": used_legacy, "symbol": symbol,
            "fail_count": 0, "last_error": error,
        })
        self._retry_orphan_cancels(first_error=error)

    def _retry_orphan_cancels(self, first_error=None):
        remaining = []
        for item in self._orphan_cancels:
            try:
                self._retry_one_orphan(item, first_error, remaining)
            except Exception as e:
                # 這一筆本身壞掉(欄位缺漏等)：留在清單、照節奏推播，不能讓後面的都不處理(第8條r22)。
                # 告警只用.get()組——進到這裡往往就是某個欄位壞了
                n = (item.get("fail_count") or 0) + 1 if isinstance(item, dict) else 1
                if isinstance(item, dict):
                    item["fail_count"] = n
                remaining.append(item)
                if alert_cadence.should_alert(n):
                    self._backstop_alert(
                        f"⚠️ {self.label} 待撤清單有一筆資料有問題(第 {n} 次)\n"
                        f"algoId：{item.get('algo_id') if isinstance(item, dict) else item}\n"
                        f"錯誤：{type(e).__name__}: {e}\n請到幣安確認這張單，其他筆照常處理"
                    )
        self._orphan_cancels = remaining

    def _retry_one_orphan(self, item, first_error, remaining):
        """待撤清單的一筆(原本迴圈的主體原封不動搬來，外層逐筆try)。還要重試就放進remaining。"""
        if True:  # 保留原本的縮排層級，主體內容一行不改(第14條r22：避免重排縮排時漏行)
            if first_error is not None and item["fail_count"] == 0:
                ok, result = False, first_error  # 剛才平倉時已經撤過一次，算第1次失敗
            else:
                ok, result, _ = execution_module.cancel_algo_stop(
                    item["algo_id"], symbol=item["symbol"], account=self.execution_account,
                    used_legacy=item["used_legacy"],
                )
            if ok:
                if item["fail_count"]:
                    self._backstop_alert(
                        f"✅ {self.label} 多餘的停損單已撤掉(失敗 {item.get('fail_count')} 次後恢復)\n"
                        f"幣別：{item.get('symbol')}\nalgoId：{item.get('algo_id')}"
                    )
                return
            item["fail_count"] += 1
            item["last_error"] = result
            if alert_cadence.should_alert(item["fail_count"]):
                self._backstop_alert(
                    f"⚠️ {self.label} 多餘的停損單撤不掉(第 {item.get('fail_count')} 次)\n"
                    f"幣別：{item.get('symbol')}\nalgoId：{item.get('algo_id')}\n"
                    f"想要的停損價：無(這張是被換掉的舊單或平倉後殘留，應該撤掉)\n目前交易所停損：這張仍掛著\n"
                    f"錯誤：{result}\n每輪自動重試；也可以到幣安手動撤單"
                )
            remaining.append(item)

    def _side_qty(self, direction):
        """
        查交易所上「這筆單那一側」的部位：(ok, 數量, 均價, 標記價)。依幣＋方向判斷
        (第7條)：雙向看LONG/SHORT列，單向看BOTH列的正負號。查詢失敗回ok=False，
        呼叫端不能當成「沒有部位」(第2條)。
        """
        ok, rows = execution_module.get_position_info(symbol=self.execution_symbol, account=self.execution_account)
        if not ok or not isinstance(rows, list):
            return False, 0.0, None, None
        sym = execution_module._resolve_symbol(self.execution_symbol)
        want_long = direction == "bullish"
        qty, entry, mark = 0.0, None, None
        if not any(r.get("symbol") == sym for r in rows):
            # 帶symbol查詢時交易所一定會回這個幣的列(單向1列、雙向2列，數量0也會回)。
            # 回200＋空清單是維護或閘門異常，不能當成「沒有部位」(第2條r15)
            return False, 0.0, None, None
        for r in rows:
            if r.get("symbol") != sym:
                continue
            # 標記價是整個幣的，數量0的列也有：部位已經平掉時推估出場價要用它，不能退到停損價
            mark = mark or (float(r.get("markPrice") or 0) or None)
            try:
                amt = float(r.get("positionAmt", 0) or 0)
            except (TypeError, ValueError):
                continue
            side = r.get("positionSide", "BOTH")
            mine = (side == "LONG" and want_long) or (side == "SHORT" and not want_long) or \
                   (side == "BOTH" and amt != 0 and (amt > 0) == want_long)
            if mine and amt != 0:
                qty += abs(amt)
                entry = float(r.get("entryPrice") or 0) or entry
                mark = float(r.get("markPrice") or 0) or mark
        return True, qty, entry, mark

    def _own_qty(self, position):
        """交易所這一側扣掉基準後，屬於這筆單的數量：(ok, 數量)。第3條r16：基準要跟著部位走完。"""
        ok, qty, _, _ = self._side_qty(position["direction"])
        if not ok:
            return False, 0.0
        return True, max(0.0, qty - (position.get("real_open_baseline", 0.0) or 0.0))

    POSITION_STEPS = ("開倉確認", "平倉重試", "停損對齊", "數量比對", "停損守衛", "每輪維護", "快速停損出場", "平倉收尾")

    def _run_step(self, name, fn, *args):
        """
        每輪流程的一步(第8條r18/r19)：各自try，一步出錯不影響後面的步驟與出場判斷。
        出錯不能只寫日誌被吞掉(第14條)：照第8條節奏推播(帶錯誤內容)，恢復時通知一次。
        """
        try:
            fn(*args)
        except Exception as e:
            n = self._step_errors.get(name, 0) + 1
            self._step_errors[name] = n
            logger.error(f"每輪步驟「{name}」出錯({self.label}, 第{n}次): {e}")
            if alert_cadence.should_alert(n):
                self._backstop_alert(
                    f"⚠️ {self.label} 每輪步驟「{name}」出錯(第 {n} 次)\n"
                    f"幣別：{execution_module._resolve_symbol(self.execution_symbol)}\n"
                    f"錯誤：{type(e).__name__}: {e}\n其他步驟與出場判斷照常執行，下一輪重試"
                )
            return False
        n = self._step_errors.pop(name, 0)
        if n:
            self._backstop_alert(f"✅ {self.label} 每輪步驟「{name}」已恢復(出錯 {n} 次後)")
        return True

    def _end_position_step_errors(self):
        """部位結束：跟部位有關的出錯次數清掉；還在出錯中的發一則收尾通知(第8條r10/r19)。"""
        ended = {k: v for k, v in self._step_errors.items() if k in self.POSITION_STEPS}
        for k in ended:
            self._step_errors.pop(k, None)
        if ended:
            detail = "、".join(f"{k} {v} 次" for k, v in ended.items())
            self._backstop_alert(f"ℹ️ {self.label} 部位已結束，每輪步驟的出錯狀態結束({detail})")

    def _housekeeping(self, pos):
        """
        每輪tick開頭的維護，順序有意義(第3條r15「認領要在判斷還在場之前」)：
          1. pending開倉確認(認領)
          2. 待平倉就只做重試
          3. 每4輪：比對交易所數量(判斷部位還在不在)，還在才檢查停損還在不在
        """
        # 每一步各自try(第8條r19)：前一步出錯不能讓這個部位的停損守衛跳過
        if pos.get("real_open_pending_until"):
            self._run_step("開倉確認", self._resolve_open_pending, pos)
        elif pos.get("real_open_executed") is None and \
                self._is_execution_engine(settings_module.get_settings(engine_id=self.engine_id)):
            # 結果不明、而且沒有確認期限(服務重啟後記憶體裡的期限不見了、或修正前的舊部位)：
            # 以前這裡什麼都不做——不確認、不掛停損、不比對數量，永遠卡著也沒有訊息(第8條r33)。
            # 缺期限當成已經逾時：逐幣確認，有就認領、沒有就判定未成交
            self._run_step("開倉確認", self._resolve_open_pending, pos)
        if pos.get("pending_close"):
            # 平倉單沒確認成交：每輪直接重試(第8條r13)，不等下一根K棒再觸發出場。
            # 等待期間交易所停損照常保留並對齊。
            self._run_step("停損對齊", self._sync_backstop, pos)
            self._run_step("平倉重試", self._retry_pending_close)
            return
        self._qty_check_tick = (self._qty_check_tick + 1) % QTY_CHECK_EVERY_TICKS
        if self._qty_check_tick == 0 and pos.get("real_open_executed"):
            self._run_step("數量比對", self._check_exchange_quantity, pos)
            if self._position is pos and not pos.get("_closing"):
                self._run_step("停損守衛", self._check_backstop_present, pos)

    @_engine_op()
    def _check_backstop_present(self, pos):
        """
        停損守衛(第2條、第1條r15/r16)：交易所停損單還在嗎？App手動撤掉、或其他原因消失時，
        原本程式完全不會發現。
          - 查詢失敗 → 這輪不動(查不到≠不見了)
          - 連續3輪確認不在 → 清掉紀錄，交給_sync_backstop當場補掛
            (補掛遇-2021價格已穿過停損 → 直接出場，第8條r16)
        """
        algo_id = pos.get("backstop_algo_id")
        if not algo_id or not pos.get("real_open_executed"):
            return "skip"
        ok, present = execution_module.find_open_stop(
            algo_id, used_legacy=pos.get("backstop_used_legacy", False),
            symbol=self.execution_symbol, account=self.execution_account,
        )
        if not ok:
            return "unknown"
        if present:
            pos.pop("backstop_missing", None)
            return "present"
        n = pos.get("backstop_missing", 0) + 1
        pos["backstop_missing"] = n
        if n < 3:
            return "missing"
        pos.pop("backstop_missing", None)
        logger.warning(f"交易所停損單連續3輪查不到({self.label}, id={algo_id})，重新掛單")
        self._backstop_alert(
            f"⚠️ {self.label} 交易所停損單不見了(連續3輪查不到，id={algo_id})，立刻重新掛單\n"
            f"幣別：{execution_module._resolve_symbol(self.execution_symbol)}\n想要的停損價：{pos.get('sl_price')}"
        )
        pos.pop("backstop_algo_id", None)
        pos.pop("backstop_price", None)
        return "replaced:" + str(self._sync_backstop(pos))

    def _record_fill_boundary(self, position, latest=False):
        """
        開倉成交後、認領當下，記下成交明細的起始界線(第8條r34)並存進資料庫：之後的平倉成交只看這個id之後的。
        以前是平倉時才去最近100筆裡找開倉那筆——持倉期間成交一多就找不到，重啟後也沒有。
          - 一般開倉：開倉那張單最後一筆成交的id；那張單的成交還沒出現時，用當下最後一筆的id
            (開倉成交是同方向，不會被當成平倉，所以界線早一點沒關係)
          - 認領(latest=True)：當下最後一筆的id——開倉單號不知道，認領之前的都不算
        查不到就不記，之後平倉時記未知(不會退成0，第8條r34)。不能丟例外：這是成交之後的步驟。
        """
        try:
            ok, trades = execution_module.get_user_trades(symbol=self.execution_symbol, account=self.execution_account)
            if not ok or not isinstance(trades, list) or not trades:
                return
            ids = [int(t["id"]) for t in trades if isinstance(t.get("id"), (int, float)) or str(t.get("id", "")).isdigit()]
            oid = position.get("real_open_order_id")
            own_rows = [t for t in trades if not latest and oid is not None and str(t.get("orderId")) == str(oid)]
            own = [int(t["id"]) for t in own_rows]
            if own_rows and not isinstance(position.get("entry_actual_price"), (int, float)):
                # 回應沒有均價(ACK或回填慢)：用這張開倉單的成交明細算加權均價(實際成交價，不是估算)
                q = sum(float(t["qty"]) for t in own_rows)
                if q > 0:
                    position["entry_actual_price"] = round(sum(float(t["price"]) * float(t["qty"]) for t in own_rows) / q, 6)
            boundary = max(own) if own else (max(ids) if ids else None)
            if boundary is None:
                return
            position["fill_boundary_id"] = boundary
            db.update_paper_trade_fills(position.get("id"), position.get("real_open_order_id"), boundary)
        except Exception as e:
            logger.error(f"記錄成交明細界線失敗({self.label})，平倉時出場價會記未知: {e}")

    def _fill_vwap_by_order(self, order_result):
        """回應裡沒有均價時，用這張單號在成交明細裡的成交算加權均價；查不到回None(記未知，不估算)。"""
        try:
            oid = order_result.get("orderId") if isinstance(order_result, dict) else None
            if oid is None:
                return None
            ok, trades = execution_module.get_user_trades(symbol=self.execution_symbol, account=self.execution_account)
            rows = [t for t in trades if str(t.get("orderId")) == str(oid)] if ok and isinstance(trades, list) else []
            q = sum(float(t["qty"]) for t in rows)
            return round(sum(float(t["price"]) * float(t["qty"]) for t in rows) / q, 6) if q > 0 else None
        except Exception as e:
            logger.error(f"用成交明細算均價失敗({self.label}): {e}")
            return None

    def _trades_after(self, boundary):
        """
        界線之後的全部成交：帶fromId(界線＋1)往後分頁查到拿完(第8條r37)。以前只查最近100筆——
        界線之後的平倉成交掉了幾筆時，照樣用剩下的算出一個「確定」的出場價，比未知更糟。
        頁數到上限還沒拿完 → 回失敗(記未知)，不用拿到的部分算。
        """
        out, nxt = [], int(boundary) + 1
        for _ in range(FILL_MAX_PAGES):
            ok, page = execution_module.get_user_trades(symbol=self.execution_symbol, account=self.execution_account,
                                                        limit=FILL_PAGE_SIZE, from_id=nxt)
            if not ok or not isinstance(page, list):
                return False, f"成交明細查不到：{page}"
            out.extend(page)
            if len(page) < FILL_PAGE_SIZE:
                return True, out
            nxt = max(int(t["id"]) for t in page) + 1
        return False, f"界線之後的成交超過 {FILL_MAX_PAGES} 頁，沒拿完"

    def _closing_fills(self, position, want_qty):
        """
        從成交明細挑出這筆部位的平倉成交(第8條r30/r31)，回傳(status, 均價, 數量, 最後採用的id, 說明)：
          - 界線用成交id(不用時間：同一毫秒可能有好幾筆)：第一次從開倉那張單的最後一筆成交id起算，
            之後用「已採用的最後一筆」——部分出場採用過的成交，最後出場不能再算一次
          - 挑平倉成交看方向(多單的平倉是SELL)，不看realizedPnl≠0：打平出場那筆的realizedPnl剛好是0
          - 同側有基準部位(別人的倉)時分不出哪幾筆是自己的 → unknown
        status：ok / none(界線之後沒有平倉成交) / unknown(查不到或分不出來)
        """
        if (position.get("real_open_baseline") or 0) > 1e-9:
            return "unknown", None, 0.0, None, "同側有基準部位，成交明細分不出哪幾筆是自己的"
        boundary = position.get("fill_boundary_id")
        if boundary is not None and not isinstance(boundary, int):
            # 缺值不能退成0(第8條r34)：界線是0時，這個幣歷史上所有平倉成交都會被算成這筆的出場——不是未知，是算錯
            if isinstance(boundary, float) and boundary.is_integer():
                boundary = int(boundary)
            else:
                return "unknown", None, 0.0, None, f"界線不是數字({boundary!r})"
        if boundary is None:
            # 沒記到界線(修正前的舊部位)：從最近的成交裡找開倉那張單
            oid = position.get("real_open_order_id")
            ok, recent = execution_module.get_user_trades(symbol=self.execution_symbol, account=self.execution_account)
            if not ok or not isinstance(recent, list):
                return "unknown", None, 0.0, None, f"成交明細查不到：{recent}"
            opens = [int(t["id"]) for t in recent if oid is not None and str(t.get("orderId")) == str(oid)]
            if not opens:
                return "unknown", None, 0.0, None, "找不到開倉那筆成交，定不出界線"
            boundary = max(opens)
        ok, trades = self._trades_after(boundary)
        if not ok:
            return "unknown", None, 0.0, None, trades
        bullish = position["direction"] == "bullish"
        close_side, pos_side = ("SELL", "LONG") if bullish else ("BUY", "SHORT")
        cands = sorted((t for t in trades if int(t.get("id", 0)) > boundary and t.get("side") == close_side
                        and t.get("positionSide", "BOTH") in ("BOTH", pos_side)), key=lambda t: int(t["id"]))
        take, q = [], 0.0
        for t in cands:
            if q >= want_qty - 1e-9:
                break
            take.append(t)
            q += float(t["qty"])
        if not take:
            return "none", None, 0.0, None, "界線之後沒有平倉成交"
        vwap = sum(float(t["price"]) * float(t["qty"]) for t in take) / q
        return "ok", round(vwap, 6), round(q, 6), int(take[-1]["id"]), ""

    @_engine_op()
    def _resolve_open_pending(self, position):
        """
        開倉回應不明(逾時/5xx)時保留的pending(第3條r12/r13)。每輪查一次：
          - 交易所這一側比送單前多出部位 → 認領：數量、均價一律取交易所那一列，並當場掛停損
          - 期限(180秒)內沒看到 → 繼續等，交易所可能還沒反映，不能這一輪沒看到就清掉
          - 期限過了還沒有 → 判定未成交並通知
          - 查詢失敗 → 不下結論(查不到≠不存在，第2條)
        pending期間這個引擎已經有帳面部位，不會再開新倉(擋同幣進場)。
        """
        ok, qty, entry, _ = self._side_qty(position["direction"])
        if not ok:
            return "unknown"
        base = position.get("real_open_baseline", 0.0) or 0.0
        if qty - base > 1e-9:
            claimed = round(qty - base, 6)
            position["real_open_executed"] = True
            position["real_open_quantity"] = claimed
            position.pop("real_open_pending_until", None)
            if base <= 1e-9 and entry:
                position["entry_actual_price"] = entry
            db.update_paper_trade_real_open(position.get("id"), True, claimed)
            self._backstop_alert(
                f"✅ {self.label} 送單結果不明的開倉已確認成交，已認領\n"
                f"幣別：{execution_module._resolve_symbol(self.execution_symbol)}\n"
                f"數量：{claimed}(取自交易所)　均價：{entry if base <= 1e-9 else '與既有部位合併，無法單獨取得'}"
            )
            self._record_fill_boundary(position, latest=True)
            # 認領回來的部位一定還沒掛停損，當場掛(第3條r13)；交易所那一列就是證據，不重查(第2條r22)
            return "claimed:" + str(self._sync_backstop(position, known_qty=claimed))
        elif not isinstance(position.get("real_open_pending_until"), (int, float)) or \
                time.time() > position["real_open_pending_until"]:
            had_deadline = isinstance(position.get("real_open_pending_until"), (int, float))
            position["real_open_executed"] = False
            position.pop("real_open_pending_until", None)
            db.update_paper_trade_real_open(position.get("id"), False, None)
            self._backstop_alert(
                (f"ℹ️ {self.label} 送單結果不明的開倉，{OPEN_PENDING_SECONDS}秒內交易所都沒有對應部位，判定未成交\n"
                 if had_deadline else
                 f"ℹ️ {self.label} 送單結果不明、而且沒有確認期限(例如服務重啟)的部位，交易所這一側沒有，判定未成交\n") +
                f"此筆之後只記帳面，出場不送真實平倉單"
            )
            return "expired"
        return "waiting"

    def _keep_after_failed_close(self, position, exit_price, exit_reason, error, exchange_qty):
        """
        平倉單沒確認成交(第8條r13)：帳上紀錄與交易所停損都保留，記「待平倉」，每輪重試，
        照告警節奏提醒直到平掉。
        """
        n = position.get("close_fail_count", 0) + 1
        position["close_fail_count"] = n
        position["pending_close"] = {"reason": exit_reason, "price": exit_price}
        position.pop("_closing", None)
        with self._lock:
            self._position = position
        if alert_cadence.should_alert(n):
            self._backstop_alert(
                f"⚠️ {self.label} 平倉單沒有成交(第 {n} 次)\n"
                f"幣別：{execution_module._resolve_symbol(self.execution_symbol)}\n"
                f"想要的動作：平倉({exit_reason})\n"
                f"目前交易所部位：{exchange_qty if exchange_qty is not None else '查詢失敗，無法確認'}\n"
                f"交易所停損：{self._current_backstop_text(position)}(保留)\n"
                f"錯誤：{error}\n每輪自動重試直到平掉"
            )
        logger.error(f"平倉未確認成交({self.label}, 第{n}次)，保留部位每輪重試: {error}")

    @_engine_op()
    def _retry_pending_close(self):
        position = self._position
        if not position or not position.get("pending_close"):
            return "none"
        if not self._try_claim_close(position):
            return "busy"
        pc = position["pending_close"]
        self._close_position(position, _latest_price() or pc.get("price"), pc.get("reason", "待平倉重試"))
        return "closed" if self._position is not position else "still_pending"

    @_engine_op()
    def _check_exchange_quantity(self, position):
        """
        比對交易所數量與帳上數量(第8條r12/r13)：沒有交易所停利單也要做——App手動減碼、
        ADL自動減倉都會讓帳實不符，之後的停損、平倉就會用舊數量。
          - 變少 → 更新帳上數量、通知；減少那部分的成交價不知道，損益記未知(第8條r28，不用標記價估)
          - 連續3輪都是0 → 部位已不在交易所(手動平倉或停損觸發)，走平倉流程確認
        """
        if position.get("pending_close") or position.get("_closing") or position.get("real_open_pending_until"):
            return "skip"
        ok, qty, _, mark = self._side_qty(position["direction"])
        if not ok:
            return "unknown"
        qty = max(0.0, qty - (position.get("real_open_baseline", 0.0) or 0.0))  # 扣基準(第3條r16)
        recorded = position.get("real_open_quantity") or 0.0
        if qty <= 1e-9:
            n = position.get("gone_checks", 0) + 1
            position["gone_checks"] = n
            if n >= 3 and self._try_claim_close(position):
                self._close_position(position, mark or _latest_price() or position["sl_price"], "交易所端部位已不在")
                return "gone"
            return "gone_pending"
        position.pop("gone_checks", None)
        if qty < recorded - 1e-9:
            reduced = round(recorded - qty, 6)
            # 減少那部分(App手動減碼/ADL)：查得到成交明細就用實際成交價、推進界線；查不到記未知，
            # 不用標記價估(第8條r28/r30)
            st, px, fq, last_id, why = self._closing_fills(position, reduced)
            entry_ref = position.get("entry_actual_price")
            if st == "ok" and abs(fq - reduced) < 1e-6 and isinstance(entry_ref, (int, float)):
                pnl = ((px - entry_ref) if position["direction"] == "bullish" else (entry_ref - px)) * fq
                position["partial_realized_usd"] = (position.get("partial_realized_usd") or 0.0) + pnl
                position["fill_boundary_id"] = last_id
                db.update_paper_trade_fills(position.get("id"), position.get("real_open_order_id"), last_id)
                partial_txt = f"這部分依成交明細 {fq}@{px}，損益 {pnl:+.2f} U"
            else:
                # 使用者決定(2026-09-22)：查不到成交明細時用標記價推估，標記為估算
                ref = entry_ref if isinstance(entry_ref, (int, float)) else position["entry_price"]
                px_est = mark or _latest_price() or ref
                pnl = ((px_est - ref) if position["direction"] == "bullish" else (ref - px_est)) * reduced
                position["partial_realized_usd"] = (position.get("partial_realized_usd") or 0.0) + pnl
                position["usd_estimated"] = True
                partial_txt = (f"這部分成交明細查不到({why or '數量對不上'})，用標記價 {px_est} 推估損益 {pnl:+.2f} U(估算)")
            position["real_open_quantity"] = round(qty, 6)
            db.update_paper_trade_real_open(position.get("id"), True, round(qty, 6))
            self._backstop_alert(
                f"ℹ️ {self.label} 交易所部位數量減少 {recorded} → {round(qty, 6)}(可能是App手動減碼或ADL)\n"
                f"幣別：{execution_module._resolve_symbol(self.execution_symbol)}\n"
                f"已更新帳上數量；{partial_txt}"
            )
            return "reduced"
        return "same"

    def _backstop_alert(self, text):
        try:
            notifier_module.notifier.send_raw_message(text)
        except Exception:
            pass

    def _cancel_backstop(self, position):
        """
        平倉時一併撤掉backstop停損單(這次修正的重點)：不管是程式判斷出場、
        快速停損監控、結構停利、還是手動force_close，只要部位要關掉，這張單
        都要撤，否則會留下一個沒有對應部位的孤兒掛單。

        回傳already_triggered：True代表這張backstop單已經不在了(可能是它自己
        先觸發把部位平掉了)，呼叫端要用這個線索決定要不要再送一次真實平倉單。
        """
        symbol = self.execution_symbol or execution_module.DEFAULT_SYMBOL
        # backstop失敗中、部位從別的路徑平掉(程式出場、-2021、手動平倉)：失敗狀態在這裡
        # 結束，要發收尾通知，不然使用者收到「失敗」卻永遠等不到結果(r10第8條)
        failed = position.pop("backstop_fail_count", 0)
        if failed:
            self._backstop_alert(
                f"ℹ️ {self.label} 部位已平倉，交易所停損單失敗狀態結束(期間失敗 {failed} 次)\n幣別：{symbol}"
            )
        # 先清掉移動時沒撤成功的舊backstop(第13條：reduceOnly+quantity單不會自動消失)，
        # 還是撤不掉就進待撤清單，每輪重試、照節奏提醒
        for stale_id, stale_legacy in position.pop("backstop_stale_ids", []) or []:
            ok, res, _ = execution_module.cancel_algo_stop(
                stale_id, symbol=symbol, account=self.execution_account, used_legacy=stale_legacy,
            )
            if not ok:
                self._queue_orphan_cancel(stale_id, stale_legacy, symbol, res)
        algo_id = position.get("backstop_algo_id")
        if not algo_id:
            return False
        ok, result, already_gone = execution_module.cancel_algo_stop(
            algo_id, symbol=symbol, account=self.execution_account,
            used_legacy=position.get("backstop_used_legacy", False),
        )
        if not ok:
            logger.error(f"撤銷backstop停損單失敗({self.label}, algoId={algo_id}): {result}，進待撤清單每輪重試")
            self._queue_orphan_cancel(algo_id, position.get("backstop_used_legacy", False), symbol, result)
            return False
        if already_gone:
            logger.warning(f"backstop停損單已經不在({self.label}, algoId={algo_id})，可能已經自動觸發把部位平掉了")
        return already_gone

    def _try_claim_close(self, position):
        """
        原子性地「認領」關掉這筆倉位的權利：只有self._position現在確實還是傳入的
        這個物件時才成功、並立刻清空self._position，回傳True。用來避免快速停損
        監控(每次報價都可能觸發)跟正常15秒tick同時判斷「該出場了」，對同一筆倉位
        重複呼叫_close_position、送出兩次真實平倉單(修正記錄見README)。
        """
        with self._lock:
            if self._position is not position:
                return False
            self._position = None
            return True

    def _fast_stop_check(self, bid, ask):
        """
        掛在binance_streamer報價串流上的快速停損檢查(見start()裡的說明)。只判斷
        「現價有沒有穿過停損位」，不判斷訊號反轉/9EMA動態防守/結構停利等需要重算
        指標的出場條件，那些仍交給正常的15秒tick處理。
        """
        position = self._position  # 讀取不需要鎖：dict物件參照，最壞情況只是比對到一瞬間前的狀態
        if not position:
            return
        if position.get("pending_close") or position.get("_closing"):
            return  # 平倉中或待重試：交給每輪tick處理，不要每筆報價都送一次平倉單
        direction = position["direction"]
        sl_price = position.get("sl_price")
        if sl_price is None:
            return
        if direction == "bullish":
            price = bid  # 多單出場是賣出，用買一(bid)判斷跟成交
            if price is None or price > sl_price:
                return
        else:
            price = ask  # 空單出場是買回，用賣一(ask)判斷跟成交
            if price is None or price < sl_price:
                return
        if not self._try_claim_close(position):
            return
        reason = "觸及移動停損" if position.get("trailing_active") else "觸及停損"
        logger.info(f"快速停損觸發({self.label}): 現價{price:.2f} 穿過停損位{sl_price:.2f}")
        # 平倉牽涉真實下單/資料庫寫入/Telegram，不能佔用bookTicker串流的處理thread
        # (那個thread要一直空著去接下一筆報價)，丟到背景thread執行(修正記錄見README)
        # 背景執行緒丟的例外沒人接(不計數也不推播，第14條)：包進_run_step
        threading.Thread(
            target=self._run_step, args=("快速停損出場", lambda: self._close_position(position, price, reason, bid=bid, ask=ask)),
            daemon=True,
        ).start()

    def stop(self):
        self._stop_flag.set()

    def _loop_once(self):
        """背景迴圈的一輪(第8條r23/r24)：tick本身出錯也要照節奏推播、恢復時通知，不能只寫日誌。"""
        self._run_step("每輪判斷", self._tick)

    def _run_forever(self):
        while not self._stop_flag.is_set():
            try:
                self._loop_once()
            except Exception as e:  # 最後一道：連推播都出錯時，至少讓執行緒活著
                logger.error(f"模擬單檢查失敗({self.label}): {e}")
            self._stop_flag.wait(PAPER_POLL_SECONDS)

    def _tick(self):
        self._last_tick_at = datetime.now(timezone.utc)
        if not self._ensure_state_loaded():
            return  # 持倉紀錄讀不到：什麼都不判斷(不知道有沒有部位)，已照節奏推播、下一輪重試
        # 開頭的兩件事各自try(第8條r18)：出錯不能讓整輪tick中止——後面的出場判斷
        # (程式內停損)是最重要的保護，不能因為維護步驟出錯就不跑
        if self._orphan_cancels:
            self._run_step("殘留單重試", self._retry_orphan_cancels)
        pos = self._position
        if pos:
            self._run_step("每輪維護", self._housekeeping, pos)
            if pos.get("pending_close"):
                return

        # 風控參數即時從settings.py讀取(而不是啟動時就固定的常數)，
        # 這樣使用者在dashboard調整過設定後，下一次tick馬上就會用新的參數，
        # 不用重新部署。已開倉的部位維持原本的移動停損進度，只有「新的判斷」
        # 才會套用最新參數(例如新開倉的初始停損、觸發距離)。
        s = settings_module.get_settings(engine_id=self.engine_id)

        trend_mode = int(s.get("paper_trend_filter_mode", 0) or 0)
        result = compute_full_signal(
            interval_seconds=self.interval_seconds,
            strategy_type=self.strategy_type,
            resonance_min_conditions=self.resonance_min_conditions,
            bucket_size=DEFAULT_BUCKET_SIZE,
            trade_limit=DEFAULT_TRADE_LIMIT,
            # 趨勢濾網開啟時才多算一份大週期K棒的雙SuperTrend
            trend_interval_seconds=int(s.get("paper_trend_interval_seconds", 3600)) if trend_mode else None,
            trend_slow_multiplier=float(s.get("paper_trend_slow_multiplier", 3.0)),
        )
        current_price = result.get("current_price")
        if current_price is None:
            return

        # ATR動態停損模式：開啟時用「ATR x 倍數」取代下面的固定點數，
        # 讓停損距離跟著市場當下實際波動度調整。ATR資料不足(剛啟動、K棒不夠)
        # 時會是None，這種情況先退回固定點數，避免整個判斷卡住。
        atr = result.get("atr")
        if s["paper_use_atr_stops"] and atr:
            sl_points = atr * s["paper_atr_sl_multiplier"]
            trail_trigger_points = atr * s["paper_atr_trigger_multiplier"]
            trail_distance_points = atr * s["paper_atr_trail_multiplier"]
        else:
            sl_points = s["paper_sl_points"]
            trail_trigger_points = s["paper_trail_trigger_points"]
            trail_distance_points = s["paper_trail_distance_points"]

        # SMC結構策略：初始停損優先用「OB/FVG區域外緣」算出來的結構停損距離
        # (smc_structure.py的suggested_sl_points)，比固定點數/ATR更貼近這套方法的本意；
        # 算不出來(沒碰到區域)時退回上面的設定值。移動停損仍照設定跑。
        if self.strategy_type == "smc_structure":
            suggested = (result.get("smc") or {}).get("suggested_sl_points")
            if suggested and suggested > 0:
                sl_points = suggested

        with self._lock:
            position = self._position

        if position:
            # SMC結構停利：進場時記在position["tp_price"](純記憶體，重啟後這筆單退回只用移動停損)
            tp = position.get("tp_price")
            if tp and ((position["direction"] == "bearish" and current_price <= tp)
                       or (position["direction"] == "bullish" and current_price >= tp)):
                if self._try_claim_close(position):
                    self._close_position(position, current_price, "觸及結構停利", bid=result.get("bid"), ask=result.get("ask"), book_stale=result.get("book_stale"))
                position = None
        if position:
            changed = trading_core.update_trailing_stop(
                position, current_price, trail_trigger_points, trail_distance_points
            )
            if changed:
                db.update_paper_trade_stop(
                    position.get("id"), position["sl_price"], position["peak_price"], position["trailing_active"]
                )

            # 第8條：不管這輪停損有沒有移動，都檢查一次backstop有沒有對齊，
            # 上一輪掛不上的會在這裡重試
            if self._is_execution_engine(s):
                self._sync_backstop(position)

            exit_reason = trading_core.check_exit(
                position, current_price, result["stage"], result["direction"],
                reversal_confirm_count=s["paper_reversal_confirm_count"],
            )
            if exit_reason:
                # 這裡才第一次呼叫claim：如果快速停損監控已經在這之前搶先關掉這筆倉位，
                # _try_claim_close會失敗，這裡就不會重複平倉(修正記錄見README)
                if self._try_claim_close(position):
                    self._close_position(position, current_price, exit_reason, bid=result.get("bid"), ask=result.get("ask"), book_stale=result.get("book_stale"))
                position = None

        if position is None and result["stage"] == "訊號" and result["direction"]:
            # 震盪濾網：開啟時，偵測到目前是震盪盤就暫停開新倉(現有部位不受影響，
            # 出場規則照常運作)。choppiness_index資料不足時是None，這種情況
            # 不擋單(寧可正常運作，不要因為資料不足就整個卡住)。
            choppiness_index = result.get("choppiness_index")
            is_choppy = (
                s["paper_use_chop_filter"]
                and choppiness_index is not None
                and choppiness_index >= s["paper_chop_threshold"]
            )
            # 休市濾網：底層黃金市場休市時不開新倉(週末、CME每日維護)，見trading_core說明
            market_closed = False
            if s.get("paper_block_market_closed", 1):
                market_closed, _ = trading_core.is_gold_market_closed(datetime.now(timezone.utc))
            # 最小ATR門檻：波動太小沒行情可做，不開新倉
            atr_too_low = False
            min_atr = float(s.get("paper_min_atr_points", 0) or 0)
            if min_atr > 0 and result.get("atr") is not None and result["atr"] < min_atr:
                atr_too_low = True
            # 趨勢濾網：大週期雙SuperTrend方向跟訊號方向比對(見analysis.trend_filter_allows)
            trend_dir = (result.get("trend_filter") or {}).get("direction")
            trend_ok, trend_note = trend_filter_allows(trend_mode, trend_dir, result["direction"])
            if not trend_ok:
                logger.info(f"{self.label}: {trend_note}")
            if not is_choppy and not market_closed and not atr_too_low and trend_ok:
                self._open_position(result, current_price, sl_points)

    def _load_state(self):
        """從資料庫載入持倉紀錄。讀取失敗就拋出去(由_run_step照節奏推播)，不能當成「沒有持倉」(第8條r37)。"""
        ok, pos = db.load_open_paper_trade(engine_id=self.engine_id)
        if not ok:
            raise RuntimeError(f"讀不到持倉紀錄(資料庫)：{pos}；這個引擎暫停開新倉、每輪重試，不會當成沒有持倉")
        with self._lock:
            self._position = pos
        self._seeded_from_db = True

    def _ensure_state_loaded(self):
        """持倉紀錄載入了沒？沒有就試一次。資料庫讀取在鎖外面做，鎖只包住指定部位那一下。"""
        if self._seeded_from_db:
            return True
        self._run_step("讀取持倉紀錄", self._load_state)
        return self._seeded_from_db

    @_engine_op()
    def _open_position(self, signal_result, current_price, sl_points):
        if not self._seeded_from_db:
            # 持倉紀錄還沒載入：不知道有沒有部位，不能開新倉(第8條r37)
            return "state_not_loaded"
        if not settings_module.settings_loaded():
            # 交易設定還沒載入：會用預設值交易(引擎專屬覆寫不見)，不能開新倉(第8條r43)
            return "settings_not_loaded"
        if self._position is not None:
            # 拿到引擎鎖之後再檢查一次(第8條r50/r51)：鎖只讓兩次開倉排隊，第二次等到鎖之後照樣會送單、
            # 記帳時把原本那筆蓋掉(停損、停利從此沒人管)。呼叫端事先看過沒部位不夠
            return "already_has_position"
        position = trading_core.open_position(
            direction=signal_result["direction"],
            current_price=current_price,
            entry_time=datetime.now(timezone.utc).isoformat(),
            sl_points=sl_points,
            chan_reason=signal_result["chan"]["reason"],
            profile_reason=signal_result["profile"]["reason"],
        )
        position["interval_seconds"] = self.interval_seconds
        position["engine_id"] = self.engine_id
        if self.strategy_type == "smc_structure":
            smc = signal_result.get("smc") or {}
            s_ = settings_module.get_settings(engine_id=self.engine_id)
            if int(s_.get("smc_exit_mode", 0) or 0) == 1 and smc.get("suggested_tp_price"):
                position["tp_price"] = float(smc["suggested_tp_price"])
        db_id = db.insert_open_paper_trade(position)
        position["id"] = db_id

        with self._lock:
            self._position = position

        logger.info(
            f"模擬單開倉({self.label}): {position['direction']} @ {current_price:.2f} "
            f"(初始SL:{position['sl_price']:.2f})"
        )

        # 只有「指定的那個引擎」才會同步送出真實(測試網/正式環境依BINANCE_USE_TESTNET
        # 決定)下單，其他引擎繼續純模擬。改用execution_index(每個引擎固定的編號)判斷，
        # 不再直接比對interval_seconds——因為現在同一個K線週期可能有多個策略的引擎
        # 平行運作(例如1分K纏論 vs 1分K共振)，光用interval_seconds已經無法唯一決定
        #「哪一個」引擎該負責真實下單，兩個引擎會同時誤判自己該出手(修正記錄見README)。
        executed = None  # None=沒有嘗試下單(純模擬)，True=下單成功，False=下單失敗
        execution_error = None  # 下單失敗時的詳細原因，會一起放進Telegram通知裡
        skip_reason = None  # 風控斷路器擋下這次下單的原因(修正記錄見README)
        slippage_note = None  # 買賣價差+真正執行滑點的說明(修正記錄見README)
        mode_warning = None  # 持倉模式/槓桿被交易所限制而自動調整時的說明

        # 市價買單實際會成交在賣一(ask)、市價賣單會成交在買一(bid)，不是
        # 中間價(current_price)——使用者實測發現，直接拿中間價當基準會把
        # 「買賣價差」誤判成「執行滑點」，兩者性質不同：價差是每筆單都會有
        # 的固定成本(不管執行多快都躲不掉)，真正的滑點才是執行過程中價格
        # 又跑掉的部分。這裡改用decision當下實際的bid/ask當基準，backtest
        # 模式或即時報價還沒抓到時沒有bid/ask，execution.analyze_execution_quality()
        # 會安全回傳None，不會顯示滑價資訊。
        bid, ask = signal_result.get("bid"), signal_result.get("ask")

        s = settings_module.get_settings(engine_id=self.engine_id)
        is_execution_engine = self._is_execution_engine(s)

        if is_execution_engine:
            quantity = s["execution_quantity"]
            allowed, block_reason, block_type = risk_guard.check(self, quantity, sl_points=sl_points, bid=bid, ask=ask)

            if not allowed:
                skip_reason = block_reason
                position["real_open_executed"] = False
                db.update_paper_trade_real_open(position.get("id"), False, None)
                logger.warning(f"真實下單被擋下({self.label}, 原因類型:{block_type}): {block_reason}")
                # 只有「真正的風控斷路器」(每日/連續虧損)觸發時才發獨立警示，
                # 且「剛觸發」的那一刻才發、避免之後每次被擋都重複騷擾。
                # spread_edge(價差安全邊際不足)是市場條件判斷，可能在低波動
                # 時段頻繁觸發，不該跟風控斷路器共用同一套「只提醒一次」的
                # 旗標——不然可能會互相干擾，讓真正的虧損警示被誤判成
                # 「已經提醒過」而被壓下(修正記錄見README)。
                if block_type in ("daily_loss", "consecutive_loss") and not self._circuit_breaker_alerted:
                    self._circuit_breaker_alerted = True
                    try:
                        notifier_module.notifier.notify_circuit_breaker(self.label, block_reason)
                    except Exception as e:
                        logger.error(f"風控斷路器警示發送失敗({self.label}): {e}")
            else:
                self._circuit_breaker_alerted = False  # 恢復正常了，下次再觸發要重新警示
                try:
                    # 每次真實開倉前先確認/設定保證金模式(逐倉/全倉)跟槓桿，不再只靠
                    # dashboard手動按鈕。保證金模式的幣安API不是冪等的(已經是目標模式
                    # 時會回傳特定錯誤碼-4046)，execution.py的set_margin_type()已經把
                    # 這個情況處理成「視同成功」，這裡不用額外判斷。任一項設定失敗就
                    # 直接放棄這筆下單，不會用不確定的保證金模式/槓桿去冒險
                    # (修正記錄見README)。
                    # 持倉模式(單向/雙向)先確認，再確認保證金模式與槓桿(修正記錄見README)
                    hedge = bool(s.get("execution_hedge_mode", 1))
                    # 先查再切：已經是目標模式就不會打切換API；被殘留掛單(-4067)擋下時
                    # 會自動取消掛單再重試一次，避免每次進場都被交易所擋下(修正記錄見README)
                    # 切不過去也不放棄下單：退回帳戶目前的模式，warning會寫進通知
                    hedge, mode_warning = execution_module.resolve_position_mode(hedge, account=self.execution_account)
                    if mode_warning:
                        logger.warning(f"{self.label}: {mode_warning}")

                    margin_type = "ISOLATED" if s["execution_margin_type"] == 0 else "CROSSED"
                    margin_ok, margin_result = execution_module.set_margin_type(
                        margin_type,
                        symbol=self.execution_symbol,
                        account=self.execution_account,
                    )
                    if not margin_ok:
                        raise RuntimeError(f"保證金模式設定失敗，放棄下單: {margin_result}")

                    leverage_ok, leverage_result = execution_module.set_leverage(
                        int(s["execution_leverage"]),
                        symbol=self.execution_symbol,
                        account=self.execution_account,
                    )
                    if not leverage_ok:
                        # 幣安對新開的子帳戶有槓桿上限(錯誤碼-4421，訊息會寫允許的最大倍數，
                        # 例如"restricted from using leverage greater than 5x")。槓桿只影響
                        # 保證金占用、不影響單筆風險，所以碰到這個限制時自動降到允許的上限
                        # 再下單，不要因為設定寫10x就整筆放棄(修正記錄見README)
                        import re as _re
                        code = leverage_result.get("code") if isinstance(leverage_result, dict) else None
                        msg = str(leverage_result.get("msg", "")) if isinstance(leverage_result, dict) else str(leverage_result)
                        m = _re.search(r"greater than\s*(\d+)x", msg)
                        if code == -4421 and m:
                            capped = int(m.group(1))
                            leverage_ok, leverage_result = execution_module.set_leverage(
                                capped, symbol=self.execution_symbol, account=self.execution_account,
                            )
                            if leverage_ok:
                                logger.warning(f"{self.label}: 交易所限制槓桿上限{capped}x，已自動改用{capped}x下單(設定值{s['execution_leverage']}x)")
                                mode_warning = (mode_warning + "；" if mode_warning else "") + f"交易所限制槓桿上限{capped}x，已自動改用{capped}x"
                    if not leverage_ok:
                        raise RuntimeError(f"槓桿設定失敗，放棄下單: {leverage_result}")

                    filled = False
                    # 送單前記下這一側原有的數量，回應不明時才分得出哪些是這張單成交的(第3條)
                    baseline_ok, baseline, _, _ = self._side_qty(position["direction"])
                    if not baseline_ok:
                        # 基準查不到不能當成0繼續送(第3條r16)：之後認領、平倉都會把別人的部位算成
                        # 自己的。這次不送真實單、記錄原因，下一個訊號再說
                        success, result = False, {"code": "NO_BASELINE", "msg": "送單前查不到交易所這一側的部位(基準)，這次不送真實單"}
                    else:
                        success, result = execution_module.open_position(
                            direction=position["direction"],
                            quantity=quantity,
                            symbol=self.execution_symbol,
                            account=self.execution_account,
                            hedge=hedge,
                        )
                    # 回應逾時/5xx：結果不明，單可能已經成交(第3條r12)。先查交易所：
                    # 看到了就認領(數量、均價取交易所那一列)；還沒看到就保留pending 180秒
                    ambiguous_pending = False
                    claim_entry = None
                    if not success and execution_module.is_ambiguous_result(result):
                        ok2, qty2, entry2, _ = self._side_qty(position["direction"])
                        base = baseline if baseline_ok else 0.0
                        if ok2 and qty2 - base > 1e-9:
                            success = True
                            quantity = round(qty2 - base, 6)
                            claim_entry = entry2 if base <= 1e-9 else None
                            result = {"avgPrice": str(claim_entry)} if claim_entry else {}
                            logger.warning(f"開倉回應不明但交易所已有部位({self.label})，認領數量{quantity}")
                        else:
                            ambiguous_pending = True
                    if success:
                        fq = execution_module.extract_filled_qty(result)
                        if fq is not None and fq < quantity - 1e-9:
                            logger.warning(f"開倉只成交 {fq}/{quantity}({self.label})，真實數量以成交量為準")
                            quantity = round(fq, 6)  # 真實數量用交易所確認的成交量，不是送出的數量
                    executed = success
                    filled = bool(success)  # r10第8條「已成交的動作先通知」：之後出錯不能改寫這個事實
                    # 把「這筆單有沒有真的開出真實部位、開了多少」記進部位跟資料庫，
                    # 平倉時只有真的開過才會送真實平倉單(修正記錄見README)
                    position["real_open_executed"] = bool(success)
                    position["real_open_quantity"] = quantity if success else None
                    # 開倉單號：之後查成交明細時，界線從這張單的最後一筆成交id開始(第8條r30/r31)
                    position["real_open_order_id"] = result.get("orderId") if (success and isinstance(result, dict)) else None
                    if success:
                        self._record_fill_boundary(position)
                    db.update_paper_trade_real_open(position.get("id"), bool(success), quantity if success else None)
                    if claim_entry:
                        position["entry_actual_price"] = claim_entry
                    if success:
                        position["real_open_baseline"] = baseline
                        db.update_paper_trade_baseline(position.get("id"), baseline)
                    if ambiguous_pending:
                        # 不能記成「沒開倉」：記成不明(None)＋期限，每輪確認
                        position["real_open_executed"] = None
                        position["real_open_quantity"] = quantity
                        position["real_open_pending_until"] = time.time() + OPEN_PENDING_SECONDS
                        position["real_open_baseline"] = baseline
                        db.update_paper_trade_real_open(position.get("id"), None, quantity)
                        db.update_paper_trade_baseline(position.get("id"), baseline)
                    if success:
                        # 真實開倉成功才掛backstop停損單：交易所端的最後防線，服務掛掉/
                        # 斷線時至少不會裸奔(修正記錄見README)。掛不上不影響這筆交易繼續
                        # 進行——程式內的停損判斷本來就是主要防線，backstop只是保險，
                        # 掛不上就記警告、繼續走原本流程，不會因此讓這筆單卡住或撤銷。
                        self._place_backstop(position, quantity, hedge)
                    if success:
                        logger.info(f"同步下單成功({self.label}): {result}")
                        actual_fill_price = execution_module.extract_fill_price(result) or position.get("entry_actual_price")
                        quality = execution_module.analyze_execution_quality(
                            position["direction"], bid, ask, actual_fill_price, is_close=False,
                        ) if actual_fill_price else None
                        if quality:
                            slippage_note = (
                                f"預期成交價{quality['expected_fill_price']:.2f}(依決策當下ask/bid) vs "
                                f"實際成交價{actual_fill_price:.2f}，真正執行滑點{quality['slippage_points']:+.2f}points"
                                f"，當下價差{quality['spread']:.2f}points"
                            )
                            if signal_result.get("book_stale"):
                                bs = signal_result["book_stale"]
                                lag = bs.get("lag_seconds")
                                slippage_note += (
                                    f"\n⚠️ 盤口報價過期(落後成交流{lag:.0f}秒)，基準改用最後成交價，價差不可信"
                                    if lag is not None else "\n⚠️ 盤口報價缺失，基準改用最後成交價，價差不可信"
                                )
                            logger.info(f"開倉滑點({self.label}): {slippage_note}")
                            # 把這筆的執行品質資料補寫回資料庫(insert當下還沒有這些
                            # 資料，因為要先真的送出下單、拿到成交價才能算出來)，
                            # 之後才能回頭做「哪個時段特別容易滑價」的統計分析，不然
                            # 這些數字原本只是曇花一現顯示在Telegram通知裡，沒有真正
                            # 留存(修正記錄見README)
                            db.update_paper_trade_entry_execution(
                                position.get("id"), quality["expected_fill_price"], actual_fill_price,
                                quality["slippage_points"], quality["spread"],
                                book_stale=bool(signal_result.get("book_stale")),
                            )
                            position["entry_actual_price"] = actual_fill_price  # 平倉時算真實USDT損益用
                        else:
                            # 不要靜默略過——明確講出是「成交價拿不到」還是「盤口
                            # bid/ask拿不到」，不然使用者只會看到完全沒有滑價資訊，
                            # 猜不出是哪個環節出問題(修正記錄見README)
                            if not actual_fill_price:
                                slippage_note = f"(無法計算執行品質：幣安訂單回應裡沒有avgPrice，原始回應：{result})"
                            elif not (bid and ask):
                                slippage_note = f"(無法計算執行品質：決策當下沒有取得bid/ask報價，成交價是{actual_fill_price:.2f})"
                            logger.warning(f"開倉執行品質無法計算({self.label}): fill={actual_fill_price}, bid={bid}, ask={ask}")
                    else:
                        execution_error = result
                        slippage_note = None
                        if ambiguous_pending:
                            execution_error = (f"送單結果不明({result})，單可能已經成交；"
                                               f"{OPEN_PENDING_SECONDS}秒內每輪查交易所確認，期間不開新倉")
                        logger.error(f"同步下單失敗({self.label}): {result}")
                except Exception as e:
                    if filled:
                        # 單已經成交：部位是真的，通知照成交發、紀錄照開倉。以前這裡會把
                        # real_open_executed改成False，出場時不送真實平倉單→交易所留孤兒倉(第3條)
                        slippage_note = (slippage_note + "\n" if slippage_note else "") + f"(成交後記錄步驟出錯：{e}，不影響部位)"
                        logger.error(f"開倉已成交但後續步驟出錯({self.label}): {e}")
                    else:
                        executed = False
                        execution_error = str(e)
                        slippage_note = None
                        position["real_open_executed"] = False
                        db.update_paper_trade_real_open(position.get("id"), False, None)
                        logger.error(f"同步下單發生例外({self.label}): {e}")

        # 事件驅動通知：只有「這個引擎目前綁定真實下單」才會發送Telegram通知，
        # 純模擬的引擎完全不通知——原本是每個引擎開倉/平倉都會發，四個引擎
        # 全部訊息量太大、變成垃圾訊息，使用者只在意「真的下單了沒有」，
        # 純模擬的部分繼續在dashboard上看就好，不需要即時推播(修正記錄見README)。
        if is_execution_engine:
            try:
                # 持倉模式/槓桿自動調整的warning一併附在通知裡，讓使用者知道實際用了什麼設定
                open_note = slippage_note
                if mode_warning:
                    open_note = (open_note + "\n" if open_note else "") + f"⚠️ {mode_warning}"
                notifier_module.notifier.notify_trade_event(
                    action="open", label=self.label,
                    direction=position["direction"], price=current_price,
                    executed=executed, execution_error=execution_error, skip_reason=skip_reason,
                    account=self.execution_account, slippage_note=open_note,
                )
            except Exception as e:
                logger.error(f"開倉通知發送失敗({self.label}): {e}")

    @_engine_op(web=True)
    def force_close(self, reason="手動緊急平倉"):
        """
        正式端控制面板用：不等訊號、不等停損，立刻以最新成交價把這個引擎的部位平掉
        (走跟正常出場同一條_close_position，所以真實下單/通知/統計全部一致)。
        回傳(closed: bool, message)。
        """
        with self._lock:
            position = self._position
        if not position:
            return False, "目前沒有部位"
        from app.binance_client import binance_streamer
        trades = binance_streamer.get_recent_trades(limit=1)
        if not trades:
            return False, "拿不到最新價格，無法平倉"
        price = trades[-1]["price"]
        if not self._try_claim_close(position):
            return False, "這筆部位剛好被快速停損監控同時關閉，未重複下單"
        self._close_position(position, price, reason)
        if self._position is position:
            # 平倉單沒確認成交：部位與交易所停損都保留，每輪重試(第8條r13)
            return False, "平倉單沒有確認成交，已保留部位與交易所停損，系統每輪自動重試並會發Telegram"
        return True, f"已以 {price} 平倉({reason})"

    @_engine_op()
    def _close_position(self, position, exit_price, exit_reason, bid=None, ask=None, book_stale=None):
        """
        平倉的外層保護(第8條r22「except 不能把已經做完的動作當成沒做」)。以「帳上紀錄結掉」為界：
          - 結帳之前出錯：平倉可能沒成交、停損也還在 → 放回帳上、記待平倉、每輪重試、照節奏告警。
            呼叫端已經把部位從記憶體取走(claim)，這裡不放回的話程式就忘了這個部位。
          - 結帳之後出錯(紀錄、通知)：帳已結、停損已撤，是不可逆的 → 不能放回，只推播出錯。
        exit_price缺值時用最新價補，避免算損益時出錯。
        """
        if exit_price is None:
            exit_price = _latest_price() or position.get("sl_price") or position.get("entry_price")
        try:
            return self._close_position_inner(position, exit_price, exit_reason, bid=bid, ask=ask, book_stale=book_stale)
        except Exception as e:
            if position.pop("_close_confirmed", False):
                logger.error(f"平倉已結帳但收尾出錯({self.label}): {e}")
                with self._lock:
                    if self._position is position:
                        self._position = None
                self._run_step("平倉收尾", self._raise, e)
            else:
                logger.error(f"平倉確認前出錯({self.label})，保留部位待平倉: {e}")
                position.pop("_closing", None)
                self._keep_after_failed_close(position, exit_price, exit_reason, f"程式例外：{type(e).__name__}: {e}", None)

    @staticmethod
    def _raise(e):
        raise e

    def _safe(self, label, fn, default=None):
        """
        結帳之後的收尾步驟(第8條r24)：各自try，一步出錯不影響其他收尾；出錯推播(不能被吞掉，第14條)。
        這些步驟一筆平倉只跑一次，不用節奏計數。
        """
        try:
            return fn()
        except Exception as e:
            logger.error(f"平倉收尾「{label}」出錯({self.label}): {e}")
            try:
                self._backstop_alert(f"⚠️ {self.label} 平倉收尾「{label}」出錯(帳已結、不影響部位)\n錯誤：{type(e).__name__}: {e}")
            except Exception:
                pass
            return default

    def _safe_closed_record(self, position, exit_price, exit_reason, exit_time):
        """算平倉紀錄不能丟例外(第8條r24)：缺欄位時損益記為未知(None)，照樣結帳。
        交易所端平掉、成交明細又查不到時，出場價不知道，損益也記未知(第8條r30)。"""
        try:
            return trading_core.close_position(position, exit_price, exit_reason, exit_time)
        except Exception as e:
            logger.error(f"算平倉損益出錯({self.label})，損益記為未知: {e}")
            return {**position, "exit_price": exit_price, "exit_time": exit_time,
                    "exit_reason": exit_reason, "pnl_points": None}

    def _close_position_inner(self, position, exit_price, exit_reason, bid=None, ask=None, book_stale=None):
        """
        平倉流程(BINANCE_LESSONS.md第8條r12/r13「平倉單送出後一定要看結果」)，順序是：
          1. 送真實平倉單
          2. 沒成交 → 再查一次部位：這一側確實沒了(逾時但其實成交、或被交易所停損觸發)才算平掉；
             數量比帳上少(被拒)→ 用交易所實際數量重送一次；還在或查不到 → 保留部位與交易所停損、
             記「待平倉」每輪重試、照節奏告警，然後返回
          3. 確認平掉之後才撤交易所停損
          4. 最後才把帳上紀錄結掉
        以前是1、2、3倒過來：先結帳、先撤停損、最後送單，被拒時部位從程式眼中消失、而且沒有停損。
        """
        executed = None
        execution_error = None
        slippage_note = None
        s = settings_module.get_settings(engine_id=self.engine_id)
        is_execution_engine = self._is_execution_engine(s)
        position["_closing"] = True
        if position.get("real_open_pending_until"):
            self._resolve_open_pending(position)

        # 平倉只有在「開倉當時真的送出真實下單」才送真實平倉單。開倉被風控擋下/下單失敗的
        # 帳面部位，出場時不能去動帳戶上的真實部位。None=不明(舊部位或回應不明的pending)，
        # 照樣嘗試，送單後會再查部位確認。
        real_open = position.get("real_open_executed")
        skip_close_reason = None
        exit_actual_price = None
        closed_externally = False
        close_filled_qty = None
        if is_execution_engine and real_open is False:
            is_execution_engine = False
            skip_close_reason = "開倉當時未送出真實下單(被風控擋下或失敗)，此筆帳面部位出場不送真實平倉單"
            logger.info(f"平倉跳過真實下單({self.label}): {skip_close_reason}")

        if is_execution_engine:
            filled = False
            try:
                success, result = execution_module.close_position(
                    direction=position["direction"],
                    symbol=self.execution_symbol,
                    account=self.execution_account,
                    quantity=position.get("real_open_quantity"),
                    baseline=position.get("real_open_baseline", 0.0) or 0.0,
                    hedge=execution_module.current_hedge_mode(
                        account=self.execution_account, default=bool(s.get("execution_hedge_mode", 1))
                    ),
                )
                executed = success
                filled = bool(success)
                if success:
                    logger.info(f"同步平倉成功({self.label}): {result}")
                    actual_fill_price = execution_module.extract_fill_price(result) or self._fill_vwap_by_order(result)
                    exit_actual_price = actual_fill_price
                    close_filled_qty = execution_module.extract_filled_qty(result)
                    quality = execution_module.analyze_execution_quality(
                        position["direction"], bid, ask, actual_fill_price, is_close=True,
                    ) if actual_fill_price else None
                    if quality:
                        slippage_note = (
                            f"預期成交價{quality['expected_fill_price']:.2f}(依決策當下ask/bid) vs "
                            f"實際成交價{actual_fill_price:.2f}，真正執行滑點{quality['slippage_points']:+.2f}points"
                            f"，當下價差{quality['spread']:.2f}points"
                        )
                        if book_stale:
                            lag = book_stale.get("lag_seconds")
                            slippage_note += (
                                f"\n⚠️ 盤口報價過期(落後成交流{lag:.0f}秒)，基準改用最後成交價，價差不可信"
                                if lag is not None else "\n⚠️ 盤口報價缺失，基準改用最後成交價，價差不可信"
                            )
                        logger.info(f"平倉滑點({self.label}): {slippage_note}")
                        db.update_paper_trade_exit_execution(
                            position.get("id"), quality["expected_fill_price"], actual_fill_price,
                            quality["slippage_points"], quality["spread"],
                            book_stale=bool(book_stale),
                        )
                    else:
                        if not actual_fill_price:
                            slippage_note = f"(無法計算執行品質：幣安訂單回應裡沒有avgPrice，原始回應：{result})"
                        elif not (bid and ask):
                            slippage_note = f"(無法計算執行品質：決策當下沒有取得bid/ask報價，成交價是{actual_fill_price:.2f})"
                        logger.warning(f"平倉執行品質無法計算({self.label}): fill={actual_fill_price}, bid={bid}, ask={ask}")
                else:
                    execution_error = result
                    logger.error(f"同步平倉失敗({self.label}): {result}")
            except Exception as e:
                if filled:
                    slippage_note = (slippage_note + "\n" if slippage_note else "") + f"(成交後記錄步驟出錯：{e}，不影響平倉)"
                    logger.error(f"平倉已成交但後續步驟出錯({self.label}): {e}")
                else:
                    executed = False
                    execution_error = str(e)
                    logger.error(f"同步平倉發生例外({self.label}): {e}")

        # ---- 平倉只成交一部分：不能當成平掉(第15條r43/r44) ----
        want_close = position.get("real_open_quantity") or 0.0
        if is_execution_engine and executed and close_filled_qty is not None and close_filled_qty < want_close - 1e-9:
            remaining = round(want_close - close_filled_qty, 6)
            position.setdefault("close_orig_qty", want_close)
            position["partial_close_filled"] = True   # 出場價之後用成交明細把各段加權，不能只用最後一張單
            position["real_open_quantity"] = remaining
            db.update_paper_trade_real_open(position.get("id"), True, remaining)
            self._keep_after_failed_close(position, exit_price, exit_reason,
                                          f"平倉只成交 {close_filled_qty}/{want_close}，剩 {remaining} 待平倉", remaining)
            return
        if position.get("partial_close_filled") and is_execution_engine and executed:
            st, px, _, last_id, _ = self._closing_fills(position, position.get("close_orig_qty") or want_close)
            if st == "ok":
                exit_actual_price = px
                position["real_qty_for_pnl"] = position.get("close_orig_qty")

        # ---- 平倉單送出後一定要看結果(第8條r12/r13) ----
        if is_execution_engine and not executed:
            ok_q, ex_qty = self._own_qty(position)
            recorded = position.get("real_open_quantity") or 0.0
            if ok_q and 1e-9 < ex_qty < recorded - 1e-9:
                # 數量不符被拒(帳上比交易所多)：改用交易所實際數量重送一次
                try:
                    ok_r, res_r = execution_module.close_position(
                        direction=position["direction"], symbol=self.execution_symbol,
                        account=self.execution_account, quantity=round(ex_qty, 6),
                        baseline=position.get("real_open_baseline", 0.0) or 0.0,
                        hedge=execution_module.current_hedge_mode(account=self.execution_account),
                    )
                except Exception as e:
                    ok_r, res_r = False, str(e)
                if ok_r:
                    executed, execution_error = True, None
                    exit_actual_price = execution_module.extract_fill_price(res_r)
                    position["real_open_quantity"] = round(ex_qty, 6)
                else:
                    execution_error = res_r
                    ok_q, ex_qty = self._own_qty(position)
            if not executed:
                if ok_q and ex_qty <= 1e-9:
                    # 這一側已經沒有部位：回應逾時但其實成交，或被交易所停損觸發
                    closed_externally = True
                    executed, execution_error = None, None
                    skip_close_reason = ("平倉單回應失敗或逾時，但交易所這一側已經沒有部位"
                                         "(可能其實已成交，或被交易所停損觸發)，視為已平倉")
                    # 出場價只用實際成交價(第8條r30)：查成交明細；查不到就記未知，不用偵測當下的標記價
                    st, px, fq, last_id, why = self._closing_fills(position, position.get("real_open_quantity") or 0.0)
                    if st == "ok":
                        exit_actual_price = px
                        exit_price = px
                        position["fill_boundary_id"] = last_id
                    else:
                        # 使用者決定(2026-09-22)：查不到成交價時用推估值(偵測當下的價格)照算，標記為估算
                        position["_pnl_estimated"] = True
                        skip_close_reason += f"；出場成交價查不到({why})，損益用偵測當下的價格推估(估算)"
                else:
                    # 還在、或查不到(查不到≠已經沒了，第2條)：保留部位，每輪重試
                    self._keep_after_failed_close(position, exit_price, exit_reason, execution_error,
                                                  ex_qty if ok_q else None)
                    return

        # ---- 平倉確認：交易所這一側已經沒了(或本來就沒送真實單)。從這裡開始不可逆 ----
        # 順序(第8條r24)：先算紀錄(不丟例外) → 寫紀錄、移出帳(界線) → 撤停損、通知、統計各自try。
        # 以前是先撤停損、再算損益寫紀錄：算損益一丟例外，停損撤了、紀錄沒寫。
        position["_close_confirmed"] = True  # 外層包裝據此判斷：之後出錯不能把部位放回帳上
        failed_closes = position.pop("close_fail_count", 0)
        position.pop("pending_close", None)
        position.pop("_closing", None)
        exit_time = datetime.now(timezone.utc).isoformat()
        closed_record = self._safe_closed_record(position, exit_price, exit_reason, exit_time)
        pnl_estimated = bool(position.pop("_pnl_estimated", False))
        self._safe("寫平倉紀錄", lambda: db.close_paper_trade(
            position.get("id"), exit_price, exit_time, exit_reason, closed_record.get("pnl_points"),
            pnl_estimated=pnl_estimated))
        self._closed_trades_memory.append(closed_record)
        with self._lock:
            if self._position is position:
                self._position = None

        # ---- 界線之後：撤交易所停損、收尾，每一步各自try ----
        backstop_triggered = False
        if position.get("backstop_algo_id") or position.get("backstop_stale_ids") or position.get("backstop_fail_count"):
            backstop_triggered = self._safe("撤交易所停損", lambda: self._cancel_backstop(position), default=False)
        if closed_externally and backstop_triggered and position.get("backstop_algo_id"):
            skip_close_reason = "交易所backstop停損單已先觸發平倉，程式判斷出場時部位已不存在，不重複送單"
            try:
                status_ok, status_data = execution_module.get_algo_stop_status(
                    position["backstop_algo_id"], symbol=self.execution_symbol, account=self.execution_account,
                    used_legacy=position.get("backstop_used_legacy", False),
                )
                if status_ok:
                    exit_actual_price = execution_module.extract_fill_price(status_data)
                    if exit_actual_price:
                        db.update_paper_trade_exit_execution(position.get("id"), None, exit_actual_price, None, None)
            except Exception as e:
                logger.error(f"查詢backstop成交價失敗({self.label}): {e}")

        self._safe("出錯次數收尾", self._end_position_step_errors)
        pnl_txt = f"{closed_record['pnl_points']:+.2f}" if closed_record.get("pnl_points") is not None else "未知"
        logger.info(f"模擬單平倉({self.label}): {position.get('direction')} @ {exit_price} ({exit_reason}, 損益:{pnl_txt})")
        if failed_closes:
            self._safe("平倉恢復通知", lambda: self._backstop_alert(
                f"✅ {self.label} 平倉已完成(失敗 {failed_closes} 次後恢復)\n"
                f"幣別：{execution_module._resolve_symbol(self.execution_symbol)}"
            ))

        if is_execution_engine or skip_close_reason:
            try:
                # 真實下單引擎的出場通知附上USDT：優先用真實成交價(進出場都有actual_price時)，
                # 否則用模擬點數×張數估算。XAUUSDT永續1張=1盎司，1點=1 USDT/張。
                # backstop觸發的情況is_execution_engine已經被設回False(不會再送真實平倉單)，
                # 但那仍然是一筆真實成交，qty/real_pnl_usd照樣要算，不能因為這樣就漏算。
                qty = float(s.get("execution_quantity", 0) or 0) if (is_execution_engine or backstop_triggered) else None
                real_pnl_usd = None
                ea, xa = position.get("entry_actual_price"), exit_actual_price
                if qty and ea and xa and (executed or backstop_triggered or closed_externally):
                    real_qty = position.get("real_qty_for_pnl") or position.get("real_open_quantity") or qty
                    real_pnl_usd = ((xa - ea) if position["direction"] == "bullish" else (ea - xa)) * real_qty
                    # 期間有部分出場(App手動減碼/ADL)而那部分成交價不知道：整筆USDT損益記未知(第8條r28)
                    if position.get("partial_pnl_unknown"):
                        real_pnl_usd = None
                    else:
                        real_pnl_usd += position.get("partial_realized_usd") or 0.0  # 各段加總(任一段未知就整筆未知)
                # 停損出場時附上「當時的停損位/峰值/出場價與停損位的差」，才分得出是
                # 停損位本身設在那裡，還是價格跳空穿過停損位(15秒輪詢一次會有落差)
                stop_note = None
                if "停損" in (exit_reason or ""):
                    gap = (position["sl_price"] - exit_price) if position["direction"] == "bullish" else (exit_price - position["sl_price"])
                    stop_note = (f"停損位 {position['sl_price']:.2f}（峰值 {position['peak_price']:.2f}，"
                                 f"移動停損{'已' if position.get('trailing_active') else '未'}啟動）"
                                 f"，出場價穿過停損位 {gap:.2f} 點")
                notifier_module.notifier.notify_trade_event(
                    action="close", label=self.label,
                    direction=position["direction"], price=exit_price,
                    exit_reason=exit_reason, pnl_points=closed_record["pnl_points"],
                    executed=executed, execution_error=execution_error, skip_reason=skip_close_reason,
                    account=self.execution_account, slippage_note=slippage_note,
                    quantity=qty, real_pnl_usd=real_pnl_usd, stop_note=stop_note,
                    real_pnl_estimated=bool(position.get("usd_estimated")) and real_pnl_usd is not None,
                )
            except Exception as e:
                logger.error(f"平倉通知發送失敗({self.label}): {e}")

    def get_summary(self, limit=50):
        """
        績效摘要：總筆數、勝率、總損益、獲利因子、最大回撤、目前開倉狀態、
        最近N筆紀錄、以及對照「達標門檻」的評估結果。只回傳這個引擎自己
        (自己的engine_id)的資料，不會混到其他引擎的紀錄——即使interval_seconds
        相同(例如1分K纏論跟1分K共振都是60秒週期)，engine_id不同就不會互相混雜。

        active_settings回傳目前生效中的「完整」設定快照(不是只挑幾個固定
        點數欄位)，讓dashboard能準確顯示「現在到底在跑什麼策略」——包含
        是固定點數模式還是ATR動態模式、震盪濾網開沒開、反轉確認次數等，
        不會像舊版只回傳固定點數欄位、卻沒說明ATR模式其實已經覆蓋掉這些值
        的情況(修正記錄見README)。

        績效統計(總筆數/勝率/獲利因子/最大回撤/達標門檻)只用「目前設定生效後」
        的交易來算，不會把舊設定底下的歷史交易混進來稀釋或扭曲數字——這是
        使用者明確要求的行為：改過參數之後，就該用新參數底下的實際表現來
        評估，混入舊參數的交易會讓「現在這組設定到底行不行」的判斷失真。
        設定從來沒被手動改過(settings_changed_at是None)的話，就照常用全部
        歷史交易計算，沒有這個篩選的必要。
        `recent_trades`清單本身仍然回傳完整歷史(含分隔線標示新舊分界)，
        方便對照細節，只有上方的統計數字會排除舊設定的交易。
        """
        if db.is_enabled():
            trades = db.get_closed_paper_trades(limit=max(limit, 500), engine_id=self.engine_id)
        else:
            trades = list(self._closed_trades_memory)[::-1]

        settings_changed_at = settings_module.get_last_changed_at(engine_id=self.engine_id)
        if settings_changed_at:
            stats_trades = [t for t in trades if t.get("entry_time") and t["entry_time"] >= settings_changed_at]
        else:
            stats_trades = trades

        s = settings_module.get_settings(engine_id=self.engine_id)

        stats = compute_stats(stats_trades)
        readiness = assess_readiness(stats)

        # 價差成本調整版統計：拿一樣的交易清單，但每筆先扣掉假設的買賣價差
        # 成本(execution_assumed_spread_points，預設0代表不調整)，讓使用者
        # 能同時看到「原始訊號表現」和「扣掉真實交易成本後」兩組數字，才能
        # 誠實評估策略扣掉價差後還剩不剩得下獲利(修正記錄見README)。
        # 預設值0時，這組數字會跟raw stats完全一樣，不影響任何既有行為。
        spread_points = s.get("execution_assumed_spread_points", 0.0)
        stats_spread_adjusted = compute_stats(stats_trades, spread_cost_points=spread_points)
        from app.trading_stats import real_usd_summary
        real_usd = real_usd_summary(stats_trades)
        readiness_spread_adjusted = assess_readiness(stats_spread_adjusted)

        # 用每筆「實際存下來」的真正執行滑點算真實影響(含肥尾佔比與調整後PF)，
        # 跟上面用假設固定值的版本並列，讓使用者用真實數字決定該擋極端值
        # 還是壓平均(修正記錄見README)。同一批stats_trades，口徑一致。
        slippage_impact = compute_slippage_impact(stats_trades)

        with self._lock:
            position = self._position

        circuit_breaker = None
        if self._is_execution_engine(s):
            circuit_breaker = risk_guard.status(self, s["execution_quantity"])

        return {
            **stats,
            "interval_seconds": self.interval_seconds,
            "engine_id": self.engine_id,
            "strategy_type": self.strategy_type,
            "label": self.label,
            "open_position": position,
            "recent_trades": trades[:limit],
            "stats_excluded_old_trades": len(trades) - len(stats_trades),  # 給dashboard顯示排除了幾筆舊紀錄
            "active_settings": s,
            "engine_overrides": settings_module.get_engine_overrides(self.engine_id),
            "settings_changed_at": settings_changed_at,
            "readiness": readiness,
            "circuit_breaker": circuit_breaker,  # None代表這個引擎沒有接真實下單，不適用風控斷路器
            # 給dashboard標題用：這個引擎現在有沒有接真實下單、打的是測試網還是正式環境、數量多少
            "real_usd": real_usd,
            "execution": {
                "enabled": circuit_breaker is not None,
                "quantity": s.get("execution_quantity"),
                # 1張XAUUSDT永續=1盎司，1點=1 USDT/張，dashboard用點數×張數換算USDT
                "testnet": execution_module.status(account=self.execution_account).get("testnet") if circuit_breaker is not None else None,
            },
            "stats_spread_adjusted": stats_spread_adjusted,
            "readiness_spread_adjusted": readiness_spread_adjusted,
            "assumed_spread_points": spread_points,
            "slippage_impact": slippage_impact,
        }


# 平行運作的模擬單引擎：1分K/5分K/15分K纏論(既有)，加上1分K多條件共振(新增，
# 實驗性策略，見README)。PAPER_TRADING_ENGINES改用engine_id字串當key，不再用
# interval_seconds——因為現在1分K纏論跟1分K共振都是60秒週期，光用interval_seconds
# 已經無法唯一區分，會互相打架(修正記錄見README)。
# health_monitor.py是直接遍歷這個字典做心跳監控，新增引擎不用改健康監控邏輯。
# 1分K纏論引擎綁定獨立的幣安帳戶「gold_1m」(讀BINANCE_API_KEY_GOLD_1M / BINANCE_API_SECRET_GOLD_1M)，
# 讓它能跟15分K(帳戶gold)同時真實下單而不互相干擾——用途是加快累積滑價統計資料。
# execution_symbol要明確給，因為execution.py的symbol預設值只會為預設帳戶"gold"自動補
# DEFAULT_SYMBOL，其他帳戶名稱不會自動補(那是為未來BTC帳戶預留的設計)(修正記錄見README)。
paper_trading_1m = PaperTradingEngine(
    interval_seconds=60, label="1分K", strategy_type="chan_profile", execution_index=1,
    execution_account="gold_1m", execution_symbol=execution_module.DEFAULT_SYMBOL,
)
paper_trading_5m = PaperTradingEngine(interval_seconds=300, label="5分K", strategy_type="chan_profile", execution_index=2)
paper_trading_15m = PaperTradingEngine(interval_seconds=900, label="15分K", strategy_type="chan_profile", execution_index=3)
# resonance_min_conditions=3：使用者用真實歷史資料回測後，4/4門檻樣本數太少(25筆)、
# 2/4門檻獲利因子/回撤都不理想，3/4門檻是三者裡樣本數(245筆)、勝率、獲利因子最平衡
# 的一組，但務必留意最大回撤86.19遠超過30的門檻，這是已知的風險特徵，不是bug，
# 純模擬階段先觀察，還沒有要接真實下單(見README)。
paper_trading_1m_resonance = PaperTradingEngine(
    interval_seconds=60, label="1分K共振", strategy_type="resonance_fvg",
    resonance_min_conditions=3, execution_index=4,
)

# 1小時K SMC市場結構策略(新增，實驗性，見smc_structure.py/README)：長線結構引擎，
# 訊號頻率低(1小時K一個月可能只有幾筆)，純模擬觀察績效，沒有綁真實下單。
# 停損：初始用結構停損(OB/FVG外緣)，移動停損用這個engine_id的專屬參數
# (建議在dashboard「此引擎專屬參數」開ATR模式，因為ATR是用1小時K算的，單位比15分K大)。
paper_trading_1h_smc = PaperTradingEngine(
    interval_seconds=3600, label="1小時K SMC結構", strategy_type="smc_structure", execution_index=5,
)

ALL_PAPER_TRADING_ENGINES = {
    paper_trading_1m.engine_id: paper_trading_1m,
    paper_trading_5m.engine_id: paper_trading_5m,
    paper_trading_15m.engine_id: paper_trading_15m,
    paper_trading_1m_resonance.engine_id: paper_trading_1m_resonance,
    paper_trading_1h_smc.engine_id: paper_trading_1h_smc,
}

# 依服務角色決定實際載入的引擎(修正記錄見README)：lab全開；live只開LIVE_ENGINE_IDS，
# 其他引擎連物件都不放進這個dict，dashboard/健康監控/風控通通看不到它們
from app.role import active_engine_ids as _active_engine_ids
PAPER_TRADING_ENGINES = {
    eid: eng for eid, eng in ALL_PAPER_TRADING_ENGINES.items()
    if eid in _active_engine_ids(ALL_PAPER_TRADING_ENGINES.keys())
}

# 保留舊名稱指向1分K引擎，避免其他還沒更新的地方(例如health_monitor.py)
# import時直接壞掉；health_monitor.py之後會更新成明確檢查兩個引擎。
paper_trading = paper_trading_1m
