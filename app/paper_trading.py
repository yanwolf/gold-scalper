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
from app.trading_stats import compute_stats, assess_readiness

logger = logging.getLogger("paper_trading")

PAPER_POLL_SECONDS = int(os.getenv("PAPER_POLL_SECONDS", "15"))

DEFAULT_BUCKET_SIZE = 1.0
DEFAULT_TRADE_LIMIT = 3000

MAX_MEMORY_TRADES = 500  # 沒有資料庫時，最多在記憶體保留這麼多筆已平倉紀錄


class PaperTradingEngine:
    def __init__(self, interval_seconds=60, label=None):
        self.interval_seconds = interval_seconds
        self.label = label or f"{interval_seconds}秒K線"

        self._lock = threading.Lock()
        self._position = None
        self._closed_trades_memory = deque(maxlen=MAX_MEMORY_TRADES)
        self._thread = None
        self._stop_flag = threading.Event()
        self._seeded_from_db = False
        self._last_tick_at = None  # 給health_monitor.py檢查引擎是否還活著用

    @property
    def last_tick_at(self):
        return self._last_tick_at

    def start(self):
        if not self._seeded_from_db:
            with self._lock:
                self._position = db.get_open_paper_trade(interval_seconds=self.interval_seconds)
            self._seeded_from_db = True

        if self._thread and self._thread.is_alive():
            return
        self._stop_flag.clear()
        self._thread = threading.Thread(target=self._run_forever, daemon=True)
        self._thread.start()
        logger.info(f"模擬單追蹤引擎已啟動({self.label}，移動停損模式)")

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
                self._close_position(position, current_price, exit_reason)
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
        db_id = db.insert_open_paper_trade(position)
        position["id"] = db_id

        with self._lock:
            self._position = position

        logger.info(
            f"模擬單開倉({self.label}): {position['direction']} @ {current_price:.2f} "
            f"(初始SL:{position['sl_price']:.2f})"
        )

    def _close_position(self, position, exit_price, exit_reason):
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

    def get_summary(self, limit=50):
        """
        績效摘要：總筆數、勝率、總損益、獲利因子、最大回撤、目前開倉狀態、
        最近N筆紀錄、以及對照「達標門檻」的評估結果。只回傳這個引擎自己
        (自己的interval_seconds)的資料，不會混到其他週期的紀錄。

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
            trades = db.get_closed_paper_trades(limit=max(limit, 500), interval_seconds=self.interval_seconds)
        else:
            trades = list(self._closed_trades_memory)[::-1]

        settings_changed_at = settings_module.get_last_changed_at()
        if settings_changed_at:
            stats_trades = [t for t in trades if t.get("entry_time") and t["entry_time"] >= settings_changed_at]
        else:
            stats_trades = trades

        stats = compute_stats(stats_trades)
        readiness = assess_readiness(stats)

        with self._lock:
            position = self._position

        return {
            **stats,
            "interval_seconds": self.interval_seconds,
            "label": self.label,
            "open_position": position,
            "recent_trades": trades[:limit],
            "stats_excluded_old_trades": len(trades) - len(stats_trades),  # 給dashboard顯示排除了幾筆舊紀錄
            "active_settings": settings_module.get_settings(),
            "settings_changed_at": settings_changed_at,
            "readiness": readiness,
        }


# 三個平行運作的實例：1分K/5分K/15分K，各自獨立追蹤、可直接對照績效。
# PAPER_TRADING_ENGINES讓main.py能依interval_seconds查到對應的引擎，
# health_monitor.py也是直接遍歷這個字典做心跳監控，新增引擎不用改健康監控邏輯。
paper_trading_1m = PaperTradingEngine(interval_seconds=60, label="1分K")
paper_trading_5m = PaperTradingEngine(interval_seconds=300, label="5分K")
paper_trading_15m = PaperTradingEngine(interval_seconds=900, label="15分K")

PAPER_TRADING_ENGINES = {
    60: paper_trading_1m,
    300: paper_trading_5m,
    900: paper_trading_15m,
}

# 保留舊名稱指向1分K引擎，避免其他還沒更新的地方(例如health_monitor.py)
# import時直接壞掉；health_monitor.py之後會更新成明確檢查兩個引擎。
paper_trading = paper_trading_1m
