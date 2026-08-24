"""
統一的訊號計算邏輯。

/signal/latest API、Telegram通知(notifier.py)、模擬單績效追蹤(paper_trading.py)
原本各自重複寫了一份「抓trades -> 建K線 -> 纏論分析 -> 分價量表 -> 綜合訊號」的邏輯，
容易改一個地方忘了改另一個地方。統一抽成這個模組，三邊都呼叫同一份函式。
"""

from app.binance_client import binance_streamer
from app.analysis import build_candles, compute_volume_profile, poc_and_value_area, analyze_chan
from app.signal import generate_signal

CHAN_LOOKBACK_TRADES = 20000  # 纏論固定用較大回看範圍，確保K棒數量足夠，不受trade_limit影響


def compute_full_signal(interval_seconds=60, bucket_size=1.0, trade_limit=3000):
    """
    回傳完整訊號結果：stage、direction、chan/profile各自的判斷理由、
    current_price、以及完整的chan_detail/profile_detail(供dashboard渲染用)。

    trade_limit只影響分價量表的取樣範圍，纏論一律用CHAN_LOOKBACK_TRADES，
    兩者是同一份trades快照的不同切片，保證同步且各自有適合的資料量。
    """
    trades = binance_streamer.get_recent_trades(limit=CHAN_LOOKBACK_TRADES)
    candles = build_candles(trades, interval_seconds=interval_seconds)
    chan_data = analyze_chan(candles)

    profile_trades = trades[-trade_limit:] if trade_limit < len(trades) else trades
    profile = compute_volume_profile(profile_trades, bucket_size=bucket_size)
    poc_info = poc_and_value_area(profile)

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
    result["profile_detail"] = {
        "bucket_size": bucket_size,
        "trade_count": len(profile_trades),
        "profile": profile,
        **poc_info,
    }
    return result
