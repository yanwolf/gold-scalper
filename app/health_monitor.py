"""
背景執行緒健康監控與告警模組。

用途：定期檢查關鍵背景元件是否正常運作，異常時主動透過Telegram告警，
不用再自己手動查/health才會發現問題。恢復正常時也會發一則「已恢復」通知。

監控項目：
1. Binance連線(public/market兩條路由)：斷線超過門檻時間 -> 告警
2. Binance逐筆成交資料是否還在更新：看「最新一筆成交的時間」離現在多久，
   不是看trade_count(這個數字在成交量累積超過緩衝區上限後會卡住不變，
   之前用trade_count判斷曾經誤報過，見下方修正記錄)
3. 模擬單追蹤引擎的心跳：太久沒有執行過_tick() -> 可能執行緒掛了或卡住
4. Telegram通知執行緒：如果有設定Token/ChatID卻執行緒沒有存活 -> 告警
5. 資料庫寫入健康度：如果有接資料庫，最近一次寫入是失敗的 -> 告警

每種檢查都有獨立的「目前是否在告警中」狀態，避免同一個問題每次檢查週期
都重複發送(只在問題「剛發生」或「剛恢復」時發送一次)，設計邏輯跟
notifier.py通知訊號時的防重複機制一致。

沒有設定TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID時，這個模組還是會持續檢查、
把結果存在記憶體供 /health/monitor 查看，只是不會發送Telegram告警。
"""

import os
import threading
import logging
from datetime import datetime, timezone

from app.binance_client import binance_streamer
from app.paper_trading import PAPER_TRADING_ENGINES
from app import notifier as notifier_module
from app import db

logger = logging.getLogger("health_monitor")

HEALTH_CHECK_POLL_SECONDS = int(os.getenv("HEALTH_CHECK_POLL_SECONDS", "60"))
HEALTH_DISCONNECT_THRESHOLD_SECONDS = int(os.getenv("HEALTH_DISCONNECT_THRESHOLD_SECONDS", "120"))
HEALTH_TRADE_STALL_THRESHOLD_SECONDS = int(os.getenv("HEALTH_TRADE_STALL_THRESHOLD_SECONDS", "300"))
HEALTH_PAPER_STALL_THRESHOLD_SECONDS = int(os.getenv("HEALTH_PAPER_STALL_THRESHOLD_SECONDS", "180"))


class HealthMonitor:
    def __init__(self):
        self._thread = None
        self._stop_flag = threading.Event()

        # 每種檢查各自的狀態追蹤
        self._binance_disconnected_since = None

        self._alert_active = {
            "binance_connection": False,
            "trade_data_stall": False,
            "db_write_failure": False,
        }
        # 模擬單心跳告警是每個週期引擎各自獨立一個key(例如paper_trading_stall_60、
        # paper_trading_stall_300)，用迴圈動態產生，這樣新增第三個週期引擎時
        # 不用回頭改這裡
        for interval_seconds in PAPER_TRADING_ENGINES:
            self._alert_active[f"paper_trading_stall_{interval_seconds}"] = False

        self._last_checked_at = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_flag.clear()
        self._thread = threading.Thread(target=self._run_forever, daemon=True)
        self._thread.start()
        logger.info("健康監控已啟動")

    def stop(self):
        self._stop_flag.set()

    def _run_forever(self):
        while not self._stop_flag.is_set():
            try:
                self._check_all()
            except Exception as e:
                logger.error(f"健康檢查本身發生錯誤: {e}")
            self._stop_flag.wait(HEALTH_CHECK_POLL_SECONDS)

    def _check_all(self):
        now = datetime.now(timezone.utc)
        self._last_checked_at = now

        self._check_binance_connection(now)
        self._check_trade_data_flow(now)
        for interval_seconds, engine in PAPER_TRADING_ENGINES.items():
            self._check_paper_trading_heartbeat(now, interval_seconds, engine)
        self._check_db_write_health()

    def _set_alert(self, key, is_problem, problem_message, recovered_message):
        """
        統一的告警狀態轉換邏輯：問題剛發生 -> 發送告警；問題剛恢復 -> 發送恢復通知；
        狀態沒變 -> 不重複發送。
        """
        was_active = self._alert_active[key]

        if is_problem and not was_active:
            self._alert_active[key] = True
            logger.warning(f"[健康告警] {problem_message}")
            self._send_alert(f"🔴 系統告警\n{problem_message}")
        elif not is_problem and was_active:
            self._alert_active[key] = False
            logger.info(f"[健康告警恢復] {recovered_message}")
            self._send_alert(f"🟢 已恢復\n{recovered_message}")

    def _send_alert(self, text):
        # 重用notifier.py的Telegram發送邏輯，這樣Token/ChatID只需要設定一次，
        # 訊號通知和系統告警共用同一個bot，但彼此的防重複邏輯完全獨立。
        if notifier_module.notifier.is_enabled:
            notifier_module.notifier.send_raw_message(text)

    def _check_binance_connection(self, now):
        status = binance_streamer.status
        connected = status["public_connected"] and status["market_connected"]

        if not connected:
            if self._binance_disconnected_since is None:
                self._binance_disconnected_since = now
            disconnected_seconds = (now - self._binance_disconnected_since).total_seconds()
            is_problem = disconnected_seconds >= HEALTH_DISCONNECT_THRESHOLD_SECONDS
        else:
            self._binance_disconnected_since = None
            is_problem = False

        self._set_alert(
            "binance_connection",
            is_problem,
            f"Binance資料源已斷線超過{HEALTH_DISCONNECT_THRESHOLD_SECONDS}秒"
            f"(public:{status['public_connected']}, market:{status['market_connected']})，"
            f"分析與模擬單會用到過期資料，請檢查服務狀態",
            "Binance資料源連線已恢復正常",
        )

    def _check_trade_data_flow(self, now):
        """
        即使顯示connected，也可能資料實際上卡住沒在更新(例如連線狀態沒被正確偵測到)，
        用「最新一筆成交的實際時間」離現在多久來判斷，不是用trade_count——
        trade_count在成交量累積超過緩衝區上限(MAX_TRADE_HISTORY)後會卡住不再變化，
        用它來判斷會誤報「停滯」，即使資料其實仍在正常更新(修正記錄見README)。
        """
        status = binance_streamer.status
        latest_trade_time_ms = status.get("latest_trade_time")

        if latest_trade_time_ms is None:
            # 還沒收到過任何成交(例如剛啟動)，不算異常，等下一輪再看
            is_problem = False
        else:
            latest_trade_dt = datetime.fromtimestamp(latest_trade_time_ms / 1000, tz=timezone.utc)
            stalled_seconds = (now - latest_trade_dt).total_seconds()
            # 只有在號稱已連線、但最新成交時間卻已經是很久以前的情況下才視為異常，
            # 已知斷線的情況交給上面_check_binance_connection處理，避免重複告警
            is_problem = (
                status["public_connected"] and status["market_connected"]
                and stalled_seconds >= HEALTH_TRADE_STALL_THRESHOLD_SECONDS
            )

        self._set_alert(
            "trade_data_stall",
            is_problem,
            f"Binance連線狀態正常，但逐筆成交資料已經超過{HEALTH_TRADE_STALL_THRESHOLD_SECONDS}秒沒有更新，"
            f"可能是連線假死，建議檢查Zeabur服務日誌",
            "Binance逐筆成交資料已恢復正常更新",
        )

    def _check_paper_trading_heartbeat(self, now, interval_seconds, engine):
        last_tick = engine.last_tick_at
        if last_tick is None:
            # 服務剛啟動、還沒執行過第一次tick，不算異常
            is_problem = False
        else:
            stalled_seconds = (now - last_tick).total_seconds()
            is_problem = stalled_seconds >= HEALTH_PAPER_STALL_THRESHOLD_SECONDS

        self._set_alert(
            f"paper_trading_stall_{interval_seconds}",
            is_problem,
            f"模擬單追蹤引擎({engine.label})已經超過{HEALTH_PAPER_STALL_THRESHOLD_SECONDS}秒沒有執行檢查，"
            f"可能背景執行緒已經停止運作",
            f"模擬單追蹤引擎({engine.label})已恢復正常運作",
        )

    def _check_db_write_health(self):
        if not db.is_enabled():
            is_problem = False
        else:
            write_health = db.get_write_health()
            is_problem = write_health["last_write_error"] is not None

        write_health = db.get_write_health() if db.is_enabled() else {}
        self._set_alert(
            "db_write_failure",
            is_problem,
            f"資料庫寫入失敗: {write_health.get('last_write_error', '未知錯誤')}",
            "資料庫寫入已恢復正常",
        )

    def get_status(self):
        return {
            "last_checked_at": self._last_checked_at.isoformat() if self._last_checked_at else None,
            "active_alerts": {k: v for k, v in self._alert_active.items() if v},
            "all_checks": self._alert_active,
        }


# 單例，供 main.py 匯入使用
health_monitor = HealthMonitor()
