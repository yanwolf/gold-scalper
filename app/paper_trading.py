"""
模擬單(paper trading)追蹤引擎 —— 移動停損版。

用途：在訊號階段升級成「訊號」時虛擬開一筆倉位(不是真的下單，只是紀錄)，
之後每個檢查週期比對現價，用移動停損(trailing stop)或訊號反轉出場。

為什麼改用移動停損取代原本的固定停利：
固定停利在順勢行情下會提早出場、吃不到後面的延伸漲跌幅，出場後只能乾等。
移動停損的做法是：
1. 一開始用固定的保守停損(PAPER_SL_POINTS)保護，避免進場後立刻被小波動洗出去
2. 價格往有利方向前進到一定距離(PAPER_TRAIL_TRIGGER_POINTS)後，「啟動」移動停損
3. 啟動後，停損價位跟著最高(多單)/最低(空單)價持續往有利方向移動，
   永遠只距離峰值PAPER_TRAIL_DISTANCE_POINTS，讓利潤有機會隨趨勢延伸，
   直到價格回檔碰到移動停損才出場——強勢單邊行情理論上能吃到比固定停利更多的漲幅，
   代價是出場價位不會是最高點，會比峰值回吐一段距離。

出場規則(二選一，先到先出)：
1. 觸及停損(可能是初始固定停損，也可能是已經上移/下移過的移動停損)
2. 訊號反轉：出現方向相反的「訊號」時，視為原倉位理由不再成立，出場

同一時間只維護一筆模擬倉位，不做加倉/多筆並存，保持邏輯單純、
方便看懂每一筆的因果關係。

風控參數都是保守預設值，可以透過環境變數調整，不需要改程式碼。
"""

import os
import threading
import logging
from collections import deque
from datetime import datetime, timezone

from app.signal_engine import compute_full_signal
from app import db

logger = logging.getLogger("paper_trading")

PAPER_POLL_SECONDS = int(os.getenv("PAPER_POLL_SECONDS", "15"))
PAPER_SL_POINTS = float(os.getenv("PAPER_SL_POINTS", "3.0"))            # 進場時的初始保護停損
PAPER_TRAIL_TRIGGER_POINTS = float(os.getenv("PAPER_TRAIL_TRIGGER_POINTS", "3.0"))  # 獲利多少才開始移動停損
PAPER_TRAIL_DISTANCE_POINTS = float(os.getenv("PAPER_TRAIL_DISTANCE_POINTS", "3.0"))  # 移動停損跟峰值的距離

DEFAULT_INTERVAL_SECONDS = 60  # 跟notifier.py保持一致，用1分鐘K線判斷
DEFAULT_BUCKET_SIZE = 1.0
DEFAULT_TRADE_LIMIT = 3000

MAX_MEMORY_TRADES = 500  # 沒有資料庫時，最多在記憶體保留這麼多筆已平倉紀錄


class PaperTradingEngine:
    def __init__(self):
        self._lock = threading.Lock()
        self._position = None  # 目前開倉中的模擬單，None代表空手
        self._closed_trades_memory = deque(maxlen=MAX_MEMORY_TRADES)  # DB沒開時的備用儲存
        self._thread = None
        self._stop_flag = threading.Event()
        self._seeded_from_db = False

    def start(self):
        # 放在start()而不是__init__，確保main.py的db.init_schema()已經先執行過
        if not self._seeded_from_db:
            with self._lock:
                self._position = db.get_open_paper_trade()
            self._seeded_from_db = True

        if self._thread and self._thread.is_alive():
            return
        self._stop_flag.clear()
        self._thread = threading.Thread(target=self._run_forever, daemon=True)
        self._thread.start()
        logger.info("模擬單追蹤引擎已啟動(移動停損模式)")

    def stop(self):
        self._stop_flag.set()

    def _run_forever(self):
        while not self._stop_flag.is_set():
            try:
                self._tick()
            except Exception as e:
                logger.error(f"模擬單檢查失敗: {e}")
            self._stop_flag.wait(PAPER_POLL_SECONDS)

    def _tick(self):
        result = compute_full_signal(
            interval_seconds=DEFAULT_INTERVAL_SECONDS,
            bucket_size=DEFAULT_BUCKET_SIZE,
            trade_limit=DEFAULT_TRADE_LIMIT,
        )
        current_price = result.get("current_price")
        if current_price is None:
            return

        with self._lock:
            position = self._position

        if position:
            self._update_trailing_stop(position, current_price)
            exit_reason = self._check_exit(position, current_price, result)
            if exit_reason:
                self._close_position(position, current_price, exit_reason)
                position = None

        if position is None and result["stage"] == "訊號" and result["direction"]:
            self._open_position(result, current_price)

    def _update_trailing_stop(self, position, current_price):
        """
        更新峰值價格，判斷要不要啟動移動停損、以及停損要不要跟著往有利方向移動。
        停損只會往有利方向移動(對多單只會上移、對空單只會下移)，不會反向鬆綁。
        """
        direction = position["direction"]
        changed = False

        if direction == "bullish":
            if current_price > position["peak_price"]:
                position["peak_price"] = current_price
                changed = True

            profit = position["peak_price"] - position["entry_price"]
            if not position["trailing_active"] and profit >= PAPER_TRAIL_TRIGGER_POINTS:
                position["trailing_active"] = True
                changed = True

            if position["trailing_active"]:
                new_stop = position["peak_price"] - PAPER_TRAIL_DISTANCE_POINTS
                if new_stop > position["sl_price"]:
                    position["sl_price"] = new_stop
                    changed = True
        else:
            if current_price < position["peak_price"]:
                position["peak_price"] = current_price
                changed = True

            profit = position["entry_price"] - position["peak_price"]
            if not position["trailing_active"] and profit >= PAPER_TRAIL_TRIGGER_POINTS:
                position["trailing_active"] = True
                changed = True

            if position["trailing_active"]:
                new_stop = position["peak_price"] + PAPER_TRAIL_DISTANCE_POINTS
                if new_stop < position["sl_price"]:
                    position["sl_price"] = new_stop
                    changed = True

        if changed:
            db.update_paper_trade_stop(
                position.get("id"), position["sl_price"], position["peak_price"], position["trailing_active"]
            )

    def _check_exit(self, position, current_price, signal_result):
        direction = position["direction"]

        if direction == "bullish":
            if current_price <= position["sl_price"]:
                return "觸及移動停損" if position["trailing_active"] else "觸及停損"
        else:
            if current_price >= position["sl_price"]:
                return "觸及移動停損" if position["trailing_active"] else "觸及停損"

        if signal_result["stage"] == "訊號" and signal_result["direction"] and signal_result["direction"] != direction:
            return "訊號反轉"

        return None

    def _open_position(self, signal_result, current_price):
        direction = signal_result["direction"]
        sl_price = current_price - PAPER_SL_POINTS if direction == "bullish" else current_price + PAPER_SL_POINTS

        position = {
            "direction": direction,
            "entry_price": current_price,
            "entry_time": datetime.now(timezone.utc).isoformat(),
            "sl_price": sl_price,
            "peak_price": current_price,
            "trailing_active": False,
            "chan_reason": signal_result["chan"]["reason"],
            "profile_reason": signal_result["profile"]["reason"],
        }
        db_id = db.insert_open_paper_trade(position)
        position["id"] = db_id

        with self._lock:
            self._position = position

        logger.info(f"模擬單開倉: {direction} @ {current_price:.2f} (初始SL:{sl_price:.2f})")

    def _close_position(self, position, exit_price, exit_reason):
        direction = position["direction"]
        pnl_points = (
            (exit_price - position["entry_price"]) if direction == "bullish"
            else (position["entry_price"] - exit_price)
        )
        exit_time = datetime.now(timezone.utc).isoformat()

        db.close_paper_trade(position.get("id"), exit_price, exit_time, exit_reason, pnl_points)

        closed_record = {
            **position,
            "exit_price": exit_price,
            "exit_time": exit_time,
            "exit_reason": exit_reason,
            "pnl_points": pnl_points,
        }
        self._closed_trades_memory.append(closed_record)

        with self._lock:
            self._position = None

        logger.info(f"模擬單平倉: {direction} @ {exit_price:.2f} ({exit_reason}, 損益:{pnl_points:+.2f})")

    def get_summary(self, limit=50):
        """
        績效摘要：總筆數、勝率、總損益(points)、獲利因子、平均獲利/虧損，
        以及目前開倉狀態和最近N筆紀錄，供dashboard渲染。
        """
        if db.is_enabled():
            trades = db.get_closed_paper_trades(limit=max(limit, 500))
        else:
            trades = list(self._closed_trades_memory)[::-1]  # 記憶體版本由新到舊

        total = len(trades)
        wins = [t for t in trades if t["pnl_points"] and t["pnl_points"] > 0]
        losses = [t for t in trades if t["pnl_points"] and t["pnl_points"] <= 0]

        total_pnl = sum(t["pnl_points"] for t in trades if t["pnl_points"] is not None)
        gross_profit = sum(t["pnl_points"] for t in wins)
        gross_loss = abs(sum(t["pnl_points"] for t in losses))

        win_rate = (len(wins) / total * 100) if total > 0 else 0.0
        avg_win = (gross_profit / len(wins)) if wins else 0.0
        avg_loss = (gross_loss / len(losses)) if losses else 0.0
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)

        with self._lock:
            position = self._position

        return {
            "total_trades": total,
            "win_count": len(wins),
            "loss_count": len(losses),
            "win_rate": round(win_rate, 1),
            "total_pnl_points": round(total_pnl, 2),
            "avg_win_points": round(avg_win, 2),
            "avg_loss_points": round(avg_loss, 2),
            "profit_factor": round(profit_factor, 2) if profit_factor != float("inf") else None,
            "open_position": position,
            "recent_trades": trades[:limit],
            "sl_points": PAPER_SL_POINTS,
            "trail_trigger_points": PAPER_TRAIL_TRIGGER_POINTS,
            "trail_distance_points": PAPER_TRAIL_DISTANCE_POINTS,
        }


# 單例，供 main.py 匯入使用
paper_trading = PaperTradingEngine()
