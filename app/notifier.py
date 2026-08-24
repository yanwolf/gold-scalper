"""
Telegram 通知模組（純通知/觀察階段，不涉及下單）。

用途：背景執行緒定期(預設30秒)在後端直接計算訊號(重用analysis/signal的邏輯，
不透過HTTP呼叫自己的/signal/latest，避免多一層網路開銷)，當訊號階段變成
「訊號」時透過Telegram Bot發送通知。避免同一個訊號重複狂發，只在「階段或
方向有變化」時才通知。

這是接軌未來MT5自動下單前的中間步驟：先驗證訊號品質，觀察一陣子確認
判斷邏輯夠準之後，再把這裡的通知邏輯換成/追加真正的下單邏輯(EA執行)。

沒有設定TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID時，這個模組會靜默停用，
不影響其他功能，設計原則跟db.py、goldapi等模組一致。
"""

import os
import threading
import logging
from datetime import datetime, timezone

import requests

from app.binance_client import binance_streamer
from app.analysis import build_candles, compute_volume_profile, poc_and_value_area, analyze_chan
from app.signal import generate_signal

logger = logging.getLogger("notifier")

CHAN_LOOKBACK_TRADES = 20000  # 跟main.py的/signal/latest保持一致，纏論需要較大回看範圍
DEFAULT_INTERVAL_SECONDS = 300  # 5分鐘K線，跟dashboard預設一致
DEFAULT_BUCKET_SIZE = 1.0
DEFAULT_TRADE_LIMIT = 3000

NOTIFY_POLL_SECONDS = int(os.getenv("NOTIFY_POLL_SECONDS", "30"))


class TelegramNotifier:
    def __init__(self):
        self._thread = None
        self._stop_flag = threading.Event()
        self._last_notified_key = None  # 記錄上次通知的(stage, direction)，避免重複發送
        self._muted = False  # 暫停通知開關(記憶體狀態，服務重啟會重置回False)
        self._last_notified_at = None

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
            "poll_seconds": NOTIFY_POLL_SECONDS,
        }

    def set_muted(self, muted: bool):
        self._muted = muted

    def start(self):
        if not self.is_enabled:
            logger.info("未設定 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID，通知功能停用")
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop_flag.clear()
        self._thread = threading.Thread(target=self._run_forever, daemon=True)
        self._thread.start()
        logger.info("Telegram 通知功能已啟動")

    def stop(self):
        self._stop_flag.set()

    def _run_forever(self):
        while not self._stop_flag.is_set():
            try:
                if not self._muted:
                    self._check_and_notify()
            except Exception as e:
                logger.error(f"訊號檢查/通知失敗: {e}")
            self._stop_flag.wait(NOTIFY_POLL_SECONDS)

    def _check_and_notify(self):
        trades = binance_streamer.get_recent_trades(limit=CHAN_LOOKBACK_TRADES)
        candles = build_candles(trades, interval_seconds=DEFAULT_INTERVAL_SECONDS)
        chan_data = analyze_chan(candles)

        profile_trades = trades[-DEFAULT_TRADE_LIMIT:] if DEFAULT_TRADE_LIMIT < len(trades) else trades
        profile = compute_volume_profile(profile_trades, bucket_size=DEFAULT_BUCKET_SIZE)
        poc_info = poc_and_value_area(profile)

        latest_tick = binance_streamer.get_latest()
        current_price = None
        if latest_tick and latest_tick.get("bid") and latest_tick.get("ask"):
            current_price = (float(latest_tick["bid"]) + float(latest_tick["ask"])) / 2

        result = generate_signal(chan_data, poc_info, current_price)

        stage = result["stage"]
        direction = result["direction"]
        key = f"{stage}_{direction}"

        # 只有階段升級成「訊號」、且跟上次通知的內容不同(階段或方向有變化)才發送，
        # 避免同一個訊號每30秒重複狂發
        if stage == "訊號" and key != self._last_notified_key:
            success, _ = self._send_telegram_message(self._format_signal_message(result))
            if success:
                self._last_notified_key = key
                self._last_notified_at = datetime.now(timezone.utc).isoformat()
        elif stage != "訊號":
            # 訊號降級了，重置記錄，下次再升級成訊號時才會是「新的」通知
            self._last_notified_key = None

    def _format_signal_message(self, result):
        direction_label = {"bullish": "看多 ▲", "bearish": "看空 ▼"}.get(result["direction"], "")
        now_str = datetime.now(timezone.utc).astimezone().strftime("%H:%M:%S")
        return (
            f"🟡 黃金訊號：{direction_label}\n"
            f"時間：{now_str}\n"
            f"現價：{result['current_price']:.2f}\n\n"
            f"纏論：{result['chan']['reason']}\n"
            f"分價量表：{result['profile']['reason']}\n\n"
            f"（目前僅通知，未自動下單）"
        )

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
