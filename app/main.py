"""
黃金極短線分析工具 - 後端主程式 (FastAPI)

架構參考自 crypto-screener（均線三刀流）：Python/FastAPI 後端 + 前端 PWA 分離。
本檔案僅負責：
1. 啟動時開始背景執行緒，持續從 OANDA 拉 XAU_USD 即時報價
2. 提供 REST endpoint 給前端輪詢最新價格 / 健康檢查
3. 提供 WebSocket endpoint 給前端做即時推播（比輪詢更適合極短線）

後續要加的分析模組（分價量表、纏論中樞/背馳等）建議獨立成
app/analysis.py，在這裡 import 進來、加新的 endpoint 即可，
不用動到 streaming 這一層。
"""

import asyncio
import os

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse

from app.oanda_client import streamer
from app.binance_client import binance_streamer
from app.analysis import build_candles, compute_volume_profile, poc_and_value_area, analyze_chan
from app.signal import generate_signal
from app.notifier import notifier
from app import db

app = FastAPI(title="Gold Scalping Analyzer", version="0.1.0")

# 開發階段先全開，正式上線建議改成前端網域白名單
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup_event():
    db.init_schema()  # 要在 binance_streamer.start() 之前，回填歷史資料時才讀得到
    streamer.start()
    binance_streamer.start()
    notifier.start()


@app.on_event("shutdown")
async def shutdown_event():
    streamer.stop()
    binance_streamer.stop()
    notifier.stop()


@app.get("/dashboard")
async def dashboard():
    """
    分價量表視覺化頁面。直接跟API同源(same-origin)提供，避免瀏覽器/App沙盒
    環境擋掉跨網域fetch的問題(手機瀏覽器對第三方頁面打外部API常常會被擋)。
    """
    return FileResponse(os.path.join(os.path.dirname(__file__), "static", "dashboard.html"))


@app.get("/")
async def root():
    return RedirectResponse(url="/dashboard")


@app.get("/health")
async def health():
    """
    Zeabur 或任何平台的健康檢查都可以打這個 endpoint。
    同時回報資料源的連線狀態，方便快速判斷是哪一邊出問題。
    """
    return {
        "service": "ok",
        "database_persistence_enabled": db.is_enabled(),
        "telegram_notifier_enabled": notifier.is_enabled,
        "oanda_stream": streamer.status,
        "binance_stream": binance_streamer.status,
    }


# ---------------------------------------------------------------------------
# Telegram 通知設定 endpoint：給dashboard的通知設定面板用
# ---------------------------------------------------------------------------

@app.get("/notify/status")
async def notify_status():
    return notifier.status


@app.post("/notify/test")
async def notify_test():
    """從dashboard按「傳送測試通知」時打這支，回傳是否成功、失敗原因是什麼。"""
    success, error = notifier.send_test_message()
    return {"success": success, "error": error}


@app.post("/notify/toggle")
async def notify_toggle(muted: bool):
    """暫停/恢復通知。這是記憶體狀態，服務重啟會重置回「未暫停」，不是永久設定。"""
    notifier.set_muted(muted)
    return notifier.status


@app.get("/notify/detect-chat-id")
async def notify_detect_chat_id():
    """
    列出最近有跟這個bot說過話的對話，方便使用者在dashboard上直接找到自己的chat_id，
    不用手動組Telegram API網址去看JSON。只需要TELEGRAM_BOT_TOKEN就能用。
    """
    return notifier.detect_recent_chats()


@app.get("/price/latest")
async def latest_price():
    """
    主要訊號源（OANDA XAU_USD）。未來實際下單走 CFD 經紀商時，
    這裡的價格基準應該跟執行端保持一致。
    """
    return streamer.get_latest() or {"message": "尚未收到任何報價，請稍後再試"}


@app.get("/price/recent")
async def recent_ticks(limit: int = 200):
    return streamer.get_recent_ticks(limit=limit)


@app.get("/price/latest/binance")
async def latest_price_binance():
    """
    輔助confirmation訊號源（Binance XAUUSDT 永續合約）。
    24/7 交易，可用來觀察 CFD 黃金收盤期間（週末）的價格動向、或跟主訊號源做交叉驗證。
    """
    return binance_streamer.get_latest() or {"message": "尚未收到任何報價，請稍後再試"}


@app.get("/price/recent/binance")
async def recent_ticks_binance(limit: int = 200):
    return binance_streamer.get_recent_ticks(limit=limit)


# ---------------------------------------------------------------------------
# 分析模組 endpoint：K線 / 分價量表 / 纏論分型-筆-中樞-背馳
# 資料源固定用 Binance 的逐筆成交（真實成交量）。
# ---------------------------------------------------------------------------

@app.get("/analysis/candles")
async def analysis_candles(interval_seconds: int = 300, trade_limit: int = 20000):
    """
    K線聚合。預設5分鐘一根，資料源是 Binance 的逐筆成交(aggTrade)。
    interval_seconds 可調整週期，例如 60 就是1分鐘K線，方便之後往下切。
    """
    trades = binance_streamer.get_recent_trades(limit=trade_limit)
    candles = build_candles(trades, interval_seconds=interval_seconds)
    return {
        "interval_seconds": interval_seconds,
        "candle_count": len(candles),
        "candles": candles,
    }


@app.get("/analysis/volume-profile")
async def analysis_volume_profile(bucket_size: float = 1.0, trade_limit: int = 20000):
    """
    分價量表。bucket_size 是每個價格箱的寬度(單位:USD)，例如0.5會分得更細。
    trade_limit 控制回看多少筆逐筆成交，數字越大涵蓋的時間範圍越長。
    """
    trades = binance_streamer.get_recent_trades(limit=trade_limit)
    profile = compute_volume_profile(trades, bucket_size=bucket_size)
    poc_info = poc_and_value_area(profile)
    return {
        "bucket_size": bucket_size,
        "trade_count": len(trades),
        "profile": profile,
        **poc_info,
    }


@app.get("/analysis/chan")
async def analysis_chan(interval_seconds: int = 300, trade_limit: int = 20000):
    """
    纏論分析：分型 -> 筆 -> 中樞 -> 背馳判斷。
    預設用5分鐘K線(interval_seconds=300)，之後要往下切1分鐘只要改參數即可，
    不用動到分析邏輯本身。
    """
    trades = binance_streamer.get_recent_trades(limit=trade_limit)
    candles = build_candles(trades, interval_seconds=interval_seconds)
    result = analyze_chan(candles)
    return {
        "interval_seconds": interval_seconds,
        "source_candle_count": len(candles),
        **result,
    }


CHAN_LOOKBACK_TRADES = 20000  # 纏論固定用較大的回看範圍，確保K棒數量足夠做分型/筆/中樞判斷，
                              # 不能被使用者在分價量表選的「近N筆」(可能只有1000~3000筆)拖累


@app.get("/signal/latest")
async def signal_latest(interval_seconds: int = 60, bucket_size: float = 1.0, trade_limit: int = 3000):
    """
    綜合訊號：纏論(中樞突破/背馳) + 分價量表(POC/Value Area)，
    兩者方向一致且至少一邊夠強才會是「訊號」，否則是「關注」或「中性」。
    這是未來要接給MT5 EA輪詢的endpoint，先在這裡驗證邏輯，之後格式穩定了
    可以直接給EA用WebRequest()定期打這支API。

    重要設計：這個endpoint只呼叫一次 get_recent_trades()(取固定的大範圍
    CHAN_LOOKBACK_TRADES)，chan_data用完整這份資料算，profile_data則從裡面
    切出使用者指定的trade_limit(較小範圍，符合分價量表想看「近期」熱區的需求)。
    兩者都是同一份trades快照的子集，保證同步，不會因為分開呼叫API、
    Binance報價持續在動而導致兩邊「最近N筆」範圍對不上。

    trade_limit在這裡只影響分價量表的取樣範圍，不影響纏論；纏論一律用
    CHAN_LOOKBACK_TRADES，避免K棒數量不足導致分型/筆/中樞判斷不出來。
    """
    trades = binance_streamer.get_recent_trades(limit=CHAN_LOOKBACK_TRADES)
    candles = build_candles(trades, interval_seconds=interval_seconds)
    chan_data = analyze_chan(candles)

    profile_trades = trades[-trade_limit:] if trade_limit < len(trades) else trades
    profile = compute_volume_profile(profile_trades, bucket_size=bucket_size)
    poc_info = poc_and_value_area(profile)
    profile_data = {
        "bucket_size": bucket_size,
        "trade_count": len(profile_trades),
        "profile": profile,
        **poc_info,
    }

    latest_tick = binance_streamer.get_latest()
    current_price = None
    if latest_tick and latest_tick.get("bid") and latest_tick.get("ask"):
        current_price = (float(latest_tick["bid"]) + float(latest_tick["ask"])) / 2

    result = generate_signal(chan_data, poc_info, current_price)
    result["chan_detail"] = {
        "interval_seconds": interval_seconds,
        "source_candle_count": len(candles),
        **chan_data,
    }
    result["profile_detail"] = profile_data
    return result


@app.websocket("/ws/price")
async def ws_price(websocket: WebSocket):
    """
    OANDA 即時推播：每 0.5 秒檢查一次共享狀態，若價格有變化就推給前端。
    """
    await websocket.accept()
    last_sent_time = None
    try:
        while True:
            latest = streamer.get_latest()
            if latest and latest.get("time") != last_sent_time:
                await websocket.send_json(latest)
                last_sent_time = latest.get("time")
            await asyncio.sleep(0.5)
    except WebSocketDisconnect:
        pass


@app.websocket("/ws/price/binance")
async def ws_price_binance(websocket: WebSocket):
    """Binance XAUUSDT 即時推播，跟 /ws/price 是獨立連線，前端可以同時訂閱兩條。"""
    await websocket.accept()
    last_sent_time = None
    try:
        while True:
            latest = binance_streamer.get_latest()
            if latest and latest.get("time") != last_sent_time:
                await websocket.send_json(latest)
                last_sent_time = latest.get("time")
            await asyncio.sleep(0.5)
    except WebSocketDisconnect:
        pass


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("app.main:app", host="0.0.0.0", port=port)
