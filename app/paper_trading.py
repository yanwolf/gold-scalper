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
import threading
import logging
from collections import deque
from datetime import datetime, timezone

from app.signal_engine import compute_full_signal
from app import db
from app import trading_core
from app import settings as settings_module
from app import notifier as notifier_module
from app import execution as execution_module
from app import risk_guard
from app.trading_stats import compute_stats, assess_readiness

logger = logging.getLogger("paper_trading")

PAPER_POLL_SECONDS = int(os.getenv("PAPER_POLL_SECONDS", "15"))

DEFAULT_BUCKET_SIZE = 1.0
DEFAULT_TRADE_LIMIT = 3000

MAX_MEMORY_TRADES = 500  # 沒有資料庫時，最多在記憶體保留這麼多筆已平倉紀錄


def _extract_fill_price(order_result):
    """
    從幣安下單API的回傳結果裡取出實際平均成交價(avgPrice)，用來跟訊號當下
    的價格比較、算出真實滑價(修正記錄見README)。市價單成交時幣安會回傳
    avgPrice欄位，如果因為任何原因缺失或格式不對(例如"0.00"代表還沒完全
    成交、或某些回應格式差異)，安全回傳None，呼叫端就不會顯示滑價資訊，
    不會因為這個附加功能本身出錯而影響下單流程。
    """
    if not isinstance(order_result, dict):
        return None
    try:
        avg_price = float(order_result.get("avgPrice", 0))
        return avg_price if avg_price > 0 else None
    except (TypeError, ValueError):
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

        self._lock = threading.Lock()
        self._position = None
        self._closed_trades_memory = deque(maxlen=MAX_MEMORY_TRADES)
        self._thread = None
        self._stop_flag = threading.Event()
        self._seeded_from_db = False
        self._last_tick_at = None  # 給health_monitor.py檢查引擎是否還活著用
        self._circuit_breaker_alerted = False  # 避免風控斷路器每次被觸發都重複發送警示

    @property
    def last_tick_at(self):
        return self._last_tick_at

    def start(self):
        if not self._seeded_from_db:
            with self._lock:
                self._position = db.get_open_paper_trade(engine_id=self.engine_id)
            self._seeded_from_db = True

        if self._thread and self._thread.is_alive():
            return
        self._stop_flag.clear()
        self._thread = threading.Thread(target=self._run_forever, daemon=True)
        self._thread.start()
        logger.info(f"模擬單追蹤引擎已啟動({self.label}，{self.strategy_type}策略，移動停損模式)")

    def stop(self):
        self._stop_flag.set()

    def _run_forever(self):
        while not self._stop_flag.is_set():
            try:
                self._tick()
            except Exception as e:
                logger.error(f"模擬單檢查失敗({self.label}): {e}")
            self._stop_flag.wait(PAPER_POLL_SECONDS)

    def _tick(self):
        self._last_tick_at = datetime.now(timezone.utc)

        # 風控參數即時從settings.py讀取(而不是啟動時就固定的常數)，
        # 這樣使用者在dashboard調整過設定後，下一次tick馬上就會用新的參數，
        # 不用重新部署。已開倉的部位維持原本的移動停損進度，只有「新的判斷」
        # 才會套用最新參數(例如新開倉的初始停損、觸發距離)。
        s = settings_module.get_settings()

        result = compute_full_signal(
            interval_seconds=self.interval_seconds,
            strategy_type=self.strategy_type,
            resonance_min_conditions=self.resonance_min_conditions,
            bucket_size=DEFAULT_BUCKET_SIZE,
            trade_limit=DEFAULT_TRADE_LIMIT,
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

        with self._lock:
            position = self._position

        if position:
            changed = trading_core.update_trailing_stop(
                position, current_price, trail_trigger_points, trail_distance_points
            )
            if changed:
                db.update_paper_trade_stop(
                    position.get("id"), position["sl_price"], position["peak_price"], position["trailing_active"]
                )

            exit_reason = trading_core.check_exit(
                position, current_price, result["stage"], result["direction"],
                reversal_confirm_count=s["paper_reversal_confirm_count"],
            )
            if exit_reason:
                self._close_position(position, current_price, exit_reason, bid=result.get("bid"), ask=result.get("ask"))
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
            if not is_choppy:
                self._open_position(result, current_price, sl_points)

    def _open_position(self, signal_result, current_price, sl_points):
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

        # 市價買單實際會成交在賣一(ask)、市價賣單會成交在買一(bid)，不是
        # 中間價(current_price)——使用者實測發現，直接拿中間價當基準會把
        # 「買賣價差」誤判成「執行滑點」，兩者性質不同：價差是每筆單都會有
        # 的固定成本(不管執行多快都躲不掉)，真正的滑點才是執行過程中價格
        # 又跑掉的部分。這裡改用decision當下實際的bid/ask當基準，backtest
        # 模式或即時報價還沒抓到時沒有bid/ask，安全退回用中間價。
        bid, ask = signal_result.get("bid"), signal_result.get("ask")
        if bid and ask:
            expected_fill_price = ask if position["direction"] == "bullish" else bid
            spread = ask - bid
        else:
            expected_fill_price = current_price
            spread = None

        s = settings_module.get_settings()
        is_execution_engine = self.execution_index is not None and s["execution_engine_index"] == self.execution_index

        if is_execution_engine:
            quantity = s["execution_quantity"]
            allowed, block_reason, block_type = risk_guard.check(self, quantity, sl_points=sl_points)

            if not allowed:
                skip_reason = block_reason
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
                        raise RuntimeError(f"槓桿設定失敗，放棄下單: {leverage_result}")

                    success, result = execution_module.open_position(
                        direction=position["direction"],
                        quantity=quantity,
                        symbol=self.execution_symbol,
                        account=self.execution_account,
                    )
                    executed = success
                    if success:
                        logger.info(f"同步下單成功({self.label}): {result}")
                        actual_fill_price = _extract_fill_price(result)
                        if actual_fill_price:
                            # 正值=比預期更不利，負值=比預期更有利，用「決策當下
                            # 的ask/bid」當基準，不是中間價，這樣算出來的才是真正
                            # 的執行滑點，不包含買賣價差本身(修正記錄見README)
                            if position["direction"] == "bullish":
                                slippage_points = actual_fill_price - expected_fill_price
                            else:
                                slippage_points = expected_fill_price - actual_fill_price
                            spread_note = f"，當下價差{spread:.2f}points" if spread is not None else ""
                            slippage_note = (
                                f"預期成交價{expected_fill_price:.2f}(依決策當下ask/bid) vs "
                                f"實際成交價{actual_fill_price:.2f}，真正執行滑點{slippage_points:+.2f}points"
                                f"{spread_note}"
                            )
                            logger.info(f"開倉滑點({self.label}): {slippage_note}")
                        else:
                            slippage_note = None
                    else:
                        execution_error = result
                        slippage_note = None
                        logger.error(f"同步下單失敗({self.label}): {result}")
                except Exception as e:
                    executed = False
                    execution_error = str(e)
                    slippage_note = None
                    logger.error(f"同步下單發生例外({self.label}): {e}")

        # 事件驅動通知：只有「這個引擎目前綁定真實下單」才會發送Telegram通知，
        # 純模擬的引擎完全不通知——原本是每個引擎開倉/平倉都會發，四個引擎
        # 全部訊息量太大、變成垃圾訊息，使用者只在意「真的下單了沒有」，
        # 純模擬的部分繼續在dashboard上看就好，不需要即時推播(修正記錄見README)。
        if is_execution_engine:
            try:
                notifier_module.notifier.notify_trade_event(
                    action="open", label=self.label,
                    direction=position["direction"], price=current_price,
                    executed=executed, execution_error=execution_error, skip_reason=skip_reason,
                    account=self.execution_account, slippage_note=slippage_note,
                )
            except Exception as e:
                logger.error(f"開倉通知發送失敗({self.label}): {e}")

    def _close_position(self, position, exit_price, exit_reason, bid=None, ask=None):
        exit_time = datetime.now(timezone.utc).isoformat()
        closed_record = trading_core.close_position(position, exit_price, exit_reason, exit_time)

        db.close_paper_trade(position.get("id"), exit_price, exit_time, exit_reason, closed_record["pnl_points"])
        self._closed_trades_memory.append(closed_record)

        with self._lock:
            self._position = None

        logger.info(
            f"模擬單平倉({self.label}): {position['direction']} @ {exit_price:.2f} "
            f"({exit_reason}, 損益:{closed_record['pnl_points']:+.2f})"
        )

        executed = None
        execution_error = None
        slippage_note = None

        # 平倉方向要反過來：多單出場是賣出(市價賣單成交在買一bid)，空單出場
        # 是買回(市價買單成交在賣一ask)——跟開倉時的方向剛好相反(修正記錄見README)
        if bid and ask:
            expected_fill_price = bid if position["direction"] == "bullish" else ask
            spread = ask - bid
        else:
            expected_fill_price = exit_price
            spread = None

        s = settings_module.get_settings()
        is_execution_engine = self.execution_index is not None and s["execution_engine_index"] == self.execution_index

        if is_execution_engine:
            try:
                success, result = execution_module.close_position(
                    direction=position["direction"],
                    symbol=self.execution_symbol,
                    account=self.execution_account,
                )
                executed = success
                if success:
                    logger.info(f"同步平倉成功({self.label}): {result}")
                    actual_fill_price = _extract_fill_price(result)
                    if actual_fill_price:
                        if position["direction"] == "bullish":
                            slippage_points = expected_fill_price - actual_fill_price
                        else:
                            slippage_points = actual_fill_price - expected_fill_price
                        spread_note = f"，當下價差{spread:.2f}points" if spread is not None else ""
                        slippage_note = (
                            f"預期成交價{expected_fill_price:.2f}(依決策當下ask/bid) vs "
                            f"實際成交價{actual_fill_price:.2f}，真正執行滑點{slippage_points:+.2f}points"
                            f"{spread_note}"
                        )
                        logger.info(f"平倉滑點({self.label}): {slippage_note}")
                else:
                    execution_error = result
                    logger.error(f"同步平倉失敗({self.label}): {result}")
            except Exception as e:
                executed = False
                execution_error = str(e)
                logger.error(f"同步平倉發生例外({self.label}): {e}")

            try:
                notifier_module.notifier.notify_trade_event(
                    action="close", label=self.label,
                    direction=position["direction"], price=exit_price,
                    exit_reason=exit_reason, pnl_points=closed_record["pnl_points"],
                    executed=executed, execution_error=execution_error,
                    account=self.execution_account, slippage_note=slippage_note,
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

        settings_changed_at = settings_module.get_last_changed_at()
        if settings_changed_at:
            stats_trades = [t for t in trades if t.get("entry_time") and t["entry_time"] >= settings_changed_at]
        else:
            stats_trades = trades

        s = settings_module.get_settings()

        stats = compute_stats(stats_trades)
        readiness = assess_readiness(stats)

        # 價差成本調整版統計：拿一樣的交易清單，但每筆先扣掉假設的買賣價差
        # 成本(execution_assumed_spread_points，預設0代表不調整)，讓使用者
        # 能同時看到「原始訊號表現」和「扣掉真實交易成本後」兩組數字，才能
        # 誠實評估策略扣掉價差後還剩不剩得下獲利(修正記錄見README)。
        # 預設值0時，這組數字會跟raw stats完全一樣，不影響任何既有行為。
        spread_points = s.get("execution_assumed_spread_points", 0.0)
        stats_spread_adjusted = compute_stats(stats_trades, spread_cost_points=spread_points)
        readiness_spread_adjusted = assess_readiness(stats_spread_adjusted)

        with self._lock:
            position = self._position

        circuit_breaker = None
        if self.execution_index is not None and s["execution_engine_index"] == self.execution_index:
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
            "settings_changed_at": settings_changed_at,
            "readiness": readiness,
            "circuit_breaker": circuit_breaker,  # None代表這個引擎沒有接真實下單，不適用風控斷路器
            "stats_spread_adjusted": stats_spread_adjusted,
            "readiness_spread_adjusted": readiness_spread_adjusted,
            "assumed_spread_points": spread_points,
        }


# 平行運作的模擬單引擎：1分K/5分K/15分K纏論(既有)，加上1分K多條件共振(新增，
# 實驗性策略，見README)。PAPER_TRADING_ENGINES改用engine_id字串當key，不再用
# interval_seconds——因為現在1分K纏論跟1分K共振都是60秒週期，光用interval_seconds
# 已經無法唯一區分，會互相打架(修正記錄見README)。
# health_monitor.py是直接遍歷這個字典做心跳監控，新增引擎不用改健康監控邏輯。
paper_trading_1m = PaperTradingEngine(interval_seconds=60, label="1分K", strategy_type="chan_profile", execution_index=1)
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

PAPER_TRADING_ENGINES = {
    paper_trading_1m.engine_id: paper_trading_1m,
    paper_trading_5m.engine_id: paper_trading_5m,
    paper_trading_15m.engine_id: paper_trading_15m,
    paper_trading_1m_resonance.engine_id: paper_trading_1m_resonance,
}

# 保留舊名稱指向1分K引擎，避免其他還沒更新的地方(例如health_monitor.py)
# import時直接壞掉；health_monitor.py之後會更新成明確檢查兩個引擎。
paper_trading = paper_trading_1m
