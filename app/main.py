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

from app.oanda_client import streamer
from app.binance_client import binance_streamer
from app.goldapi_client import goldapi_streamer
from app.analysis import build_candles, compute_volume_profile, poc_and_value_area, analyze_chan

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
    streamer.start()
    binance_streamer.start()
    goldapi_streamer.start()


@app.on_event("shutdown")
async def shutdown_event():
    streamer.stop()
    binance_streamer.stop()
    goldapi_streamer.stop()


@app.get("/health")
async def health():
    """
    Zeabur 或任何平台的健康檢查都可以打這個 endpoint。
    同時回報三個資料源的連線狀態，方便快速判斷是哪一邊出問題。
    """
    return {
        "service": "ok",
        "oanda_stream": streamer.status,
        "binance_stream": binance_streamer.status,
        "goldapi_stream": goldapi_streamer.status,
    }


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


@app.get("/price/latest/goldapi")
async def latest_price_goldapi():
    """
    低頻confirmation訊號源（GoldAPI.io，REST輪詢，非即時streaming）。
    跟 Binance 的 tick 級資料交叉比對用，不當作極短線主要判斷依據。
    """
    return goldapi_streamer.get_latest() or {"message": "尚未收到任何報價，請稍後再試"}


@app.get("/price/recent/goldapi")
async def recent_ticks_goldapi(limit: int = 200):
    return goldapi_streamer.get_recent_ticks(limit=limit)


# ---------------------------------------------------------------------------
# 分析模組 endpoint：K線 / 分價量表 / 纏論分型-筆-中樞-背馳
# 資料源固定用 Binance 的逐筆成交（真實成交量），GoldAPI 只用來對齊校正，
# 對齊校正邏輯本身還沒實作，先留 TODO，目前 GoldAPI 資料走 /price/latest/goldapi 單獨查看。
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
