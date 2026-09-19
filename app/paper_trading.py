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
from app.trading_stats import compute_stats, assess_readiness, compute_slippage_impact
from app.analysis import trend_filter_allows

logger = logging.getLogger("paper_trading")

PAPER_POLL_SECONDS = int(os.getenv("PAPER_POLL_SECONDS", "15"))

DEFAULT_BUCKET_SIZE = 1.0
DEFAULT_TRADE_LIMIT = 3000

MAX_MEMORY_TRADES = 500  # 沒有資料庫時，最多在記憶體保留這麼多筆已平倉紀錄


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
        self._fast_stop_registered = False

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
            with self._lock:
                self._position = db.get_open_paper_trade(engine_id=self.engine_id)
            self._seeded_from_db = True

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
        threading.Thread(
            target=self._close_position, args=(position, price, reason), kwargs={"bid": bid, "ask": ask}, daemon=True,
        ).start()

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

                    success, result = execution_module.open_position(
                        direction=position["direction"],
                        quantity=quantity,
                        symbol=self.execution_symbol,
                        account=self.execution_account,
                        hedge=hedge,
                    )
                    executed = success
                    # 把「這筆單有沒有真的開出真實部位、開了多少」記進部位跟資料庫，
                    # 平倉時只有真的開過才會送真實平倉單(修正記錄見README)
                    position["real_open_executed"] = bool(success)
                    position["real_open_quantity"] = quantity if success else None
                    db.update_paper_trade_real_open(position.get("id"), bool(success), quantity if success else None)
                    if success:
                        logger.info(f"同步下單成功({self.label}): {result}")
                        actual_fill_price = execution_module.extract_fill_price(result)
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
                        logger.error(f"同步下單失敗({self.label}): {result}")
                except Exception as e:
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
        return True, f"已以 {price} 平倉({reason})"

    def _close_position(self, position, exit_price, exit_reason, bid=None, ask=None, book_stale=None):
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
        s = settings_module.get_settings(engine_id=self.engine_id)
        is_execution_engine = self._is_execution_engine(s)

        # 平倉只有在「開倉當時真的送出真實下單」才送真實平倉單(修正記錄見README)。
        # 開倉被風控擋下/下單失敗的帳面部位，出場時絕不能去動帳戶上的真實部位——
        # 使用者實際遇到1分K的帳面多單「出場」時，把15分K的真實空單平掉了。
        # real_open_executed是None代表修正前的舊部位(不知道有沒有真開)，維持舊行為
        # 嘗試平倉，避免留下孤兒真實部位。
        real_open = position.get("real_open_executed")
        skip_close_reason = None
        if is_execution_engine and real_open is False:
            is_execution_engine = False
            skip_close_reason = "開倉當時未送出真實下單(被風控擋下或失敗)，此筆帳面部位出場不送真實平倉單"
            logger.info(f"平倉跳過真實下單({self.label}): {skip_close_reason}")

        exit_actual_price = None  # 真實平倉成交價(給USDT損益用)
        if is_execution_engine:
            try:
                success, result = execution_module.close_position(
                    direction=position["direction"],
                    symbol=self.execution_symbol,
                    account=self.execution_account,
                    quantity=position.get("real_open_quantity"),
                    hedge=execution_module.current_hedge_mode(
                        account=self.execution_account, default=bool(s.get("execution_hedge_mode", 1))
                    ),
                )
                executed = success
                if success:
                    logger.info(f"同步平倉成功({self.label}): {result}")
                    actual_fill_price = execution_module.extract_fill_price(result)
                    exit_actual_price = actual_fill_price
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
                executed = False
                execution_error = str(e)
                logger.error(f"同步平倉發生例外({self.label}): {e}")

        if is_execution_engine or skip_close_reason:
            try:
                # 真實下單引擎的出場通知附上USDT：優先用真實成交價(進出場都有actual_price時)，
                # 否則用模擬點數×張數估算。XAUUSDT永續1張=1盎司，1點=1 USDT/張。
                qty = float(s.get("execution_quantity", 0) or 0) if is_execution_engine else None
                real_pnl_usd = None
                ea, xa = position.get("entry_actual_price"), exit_actual_price
                if qty and ea and xa and executed:
                    real_qty = position.get("real_open_quantity") or qty
                    real_pnl_usd = ((xa - ea) if position["direction"] == "bullish" else (ea - xa)) * real_qty
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
