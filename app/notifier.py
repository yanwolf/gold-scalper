"""
Telegram 通知模組。

設計改版(修正記錄見README)：原本這裡自己背景執行緒每30秒獨立重算一次訊號，
只要訊號階段達到「訊號」就發通知——這跟「模擬單引擎實際有沒有開倉」是
兩條分開的邏輯，容易對不上(例如震盪濾網擋掉了進場，但通知端不知道濾網
存在，還是會發「訊號」通知，使用者收到通知卻在模擬單面板上找不到對應的
交易紀錄)。

改成事件驅動：模擬單引擎(paper_trading.py)實際「開倉」或「平倉」時，直接
呼叫這裡的notify_trade_event()發送通知，不再自己獨立計算訊號。這樣通知
內容永遠精確對應模擬單實際的操作，格式也改成類似「下單進場/平倉」的呈現
方式(價格、方向、損益)，而不是抽象的「偵測到訊號」——這也是未來接軌真正
的MT5自動下單後，通知格式基本上不用再改的原因，先在模擬階段就用同樣的
呈現邏輯。

沒有設定TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID時，這個模組會靜默停用，
不影響其他功能，設計原則跟db.py等模組一致。
"""

import os
import logging
from datetime import datetime, timezone

import requests

logger = logging.getLogger("notifier")

DIRECTION_LABELS = {"bullish": "多單 ▲", "bearish": "空單 ▼"}


class TelegramNotifier:
    def __init__(self):
        self._muted = False  # 暫停通知開關(記憶體狀態，服務重啟會重置回False)
        self._last_notified_at = None  # 最近一次成功發送通知的時間，給dashboard顯示用

    @property
    def is_enabled(self):
        return bool(os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"))

    @property
    def is_muted(self):
        return self._muted

    @property
    def status(self):
        return {
            "enabled": self.is_enabled,
            "muted": self._muted,
            "last_notified_at": self._last_notified_at,
            "mode": "事件驅動(模擬單實際進場/出場時才通知)",
        }

    def set_muted(self, muted: bool):
        self._muted = muted

    def start(self):
        """
        改成事件驅動後不再需要自己的背景執行緒(不用獨立輪詢訊號)，
        這個方法保留是為了main.py啟動流程的介面一致，不用特別改呼叫端。
        """
        if self.is_enabled:
            logger.info("Telegram 通知功能已啟用(事件驅動：模擬單進場/出場時通知)")
        else:
            logger.info("未設定 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID，通知功能停用")

    def stop(self):
        """同上，事件驅動模式下沒有背景執行緒需要停止，保留是為了介面一致。"""
        pass

    def notify_trade_event(self, action, label, direction, price, exit_reason=None, pnl_points=None,
                            executed=None, execution_error=None):
        """
        模擬單引擎實際開倉/平倉時呼叫這個方法發送通知。

        action: "open" 或 "close"
        label: K線週期標籤，例如"1分K"/"5分K"/"15分K"
        direction: "bullish" 或 "bearish"
        price: 進場價或出場價
        exit_reason/pnl_points: 只有action="close"時才需要提供
        executed: None代表這個週期沒有設定同步下單(純模擬)；True代表真的送出
                  下單成功；False代表有嘗試同步下單但失敗了。用來讓使用者從
                  Telegram訊息本身就能分辨「這是純模擬」還是「真的下單了」，
                  不用另外切回dashboard確認。
        execution_error: executed=False時，附上失敗的詳細原因(例如幣安API回傳的
                  錯誤代碼/訊息)，直接顯示在通知裡，不用另外查伺服器log才知道
                  發生什麼事(修正記錄見README)。
        """
        if self._muted:
            return

        direction_label = DIRECTION_LABELS.get(direction, direction)
        now_str = datetime.now(timezone.utc).astimezone().strftime("%H:%M:%S")

        if executed is True:
            from app import execution as execution_module
            env_label = "測試網" if execution_module.use_testnet() else "⚠️正式環境(真錢)"
            execution_note = f"（已同步在幣安{env_label}下單）"
        elif executed is False:
            error_snippet = str(execution_error)[:200] if execution_error else "未知原因"
            execution_note = f"（同步下單失敗，僅記錄模擬單）\n失敗原因：{error_snippet}"
        else:
            execution_note = "（目前僅模擬單，未接自動下單）"

        if action == "open":
            text = (
                f"🟢 黃金模擬單【{label}】進場\n"
                f"方向：{direction_label}\n"
                f"時間：{now_str}\n"
                f"價格：{price:.2f}\n\n"
                f"{execution_note}"
            )
        else:
            pnl_sign = "+" if (pnl_points or 0) >= 0 else ""
            pnl_emoji = "🟢" if (pnl_points or 0) >= 0 else "🔴"
            text = (
                f"{pnl_emoji} 黃金模擬單【{label}】出場\n"
                f"方向：{direction_label}\n"
                f"時間：{now_str}\n"
                f"價格：{price:.2f}\n"
                f"出場原因：{exit_reason}\n"
                f"損益：{pnl_sign}{pnl_points:.2f} points\n\n"
                f"{execution_note}"
            )

        success, _ = self._send_telegram_message(text)
        if success:
            self._last_notified_at = datetime.now(timezone.utc).isoformat()

    def _send_telegram_message(self, text):
        """回傳 (success: bool, error_message: str|None)，方便API endpoint把結果回報給前端。"""
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        chat_id = os.getenv("TELEGRAM_CHAT_ID")

        if not token or not chat_id:
            return False, "尚未設定 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID"

        url = f"https://api.telegram.org/bot{token}/sendMessage"
        try:
            resp = requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=10)
            resp.raise_for_status()
            return True, None
        except Exception as e:
            logger.error(f"Telegram 訊息發送失敗: {e}")
            return False, str(e)

    def send_test_message(self):
        text = "✅ 測試通知：如果你收到這則訊息，代表Telegram通知設定成功了。"
        return self._send_telegram_message(text)

    def send_raw_message(self, text):
        """
        給其他模組(例如health_monitor.py)重用同一個Telegram連線發送任意文字用，
        不會動到交易事件通知自己的狀態，兩者完全獨立。
        """
        return self._send_telegram_message(text)

    def detect_recent_chats(self):
        """
        呼叫Telegram的getUpdates，列出最近有跟這個bot說過話的對話(chat)，
        讓使用者能直接從清單裡找到自己的chat_id，不用手動組網址查JSON。
        只需要TELEGRAM_BOT_TOKEN就能用，不需要先設定TELEGRAM_CHAT_ID。
        """
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        if not token:
            return {"error": "尚未設定 TELEGRAM_BOT_TOKEN"}

        url = f"https://api.telegram.org/bot{token}/getUpdates"
        try:
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            return {"error": f"呼叫Telegram API失敗: {e}"}

        if not data.get("ok"):
            return {"error": f"Telegram API回傳錯誤: {data}"}

        seen = {}
        for update in data.get("result", []):
            message = update.get("message") or update.get("channel_post")
            if not message:
                continue
            chat = message.get("chat", {})
            chat_id = chat.get("id")
            if chat_id is None:
                continue
            seen[chat_id] = {
                "chat_id": chat_id,
                "name": chat.get("username") or chat.get("first_name") or chat.get("title") or "未知",
                "last_text": message.get("text", ""),
            }

        return {"chats": list(seen.values())}


# 單例，供 main.py 匯入使用
notifier = TelegramNotifier()
