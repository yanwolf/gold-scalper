"""
統一的訊號計算邏輯。

拆成兩層：
- compute_signal_from_trades()：純計算，輸入一份逐筆成交清單，輸出完整訊號結果。
  不碰即時資料源，可以餵歷史資料進去，這是回測(backtest.py)能重用同一套訊號
  邏輯的關鍵。
- compute_full_signal()：即時版本，從binance_streamer抓最新資料，
  再呼叫上面那個純函式。/signal/latest API、Telegram通知、即時模擬單
  三邊都呼叫這個。

這樣「即時判斷」和「回測重播」永遠共用同一套訊號規則，不會有回測邏輯
跟正式運作邏輯兜不起來的風險。
"""

from app.binance_client import binance_streamer
from app.analysis import (
    build_candles, compute_volume_profile, poc_and_value_area, analyze_chan,
    compute_atr, compute_choppiness_index,
)
from app.signal import generate_signal

CHAN_LOOKBACK_TRADES = 60000  # 纏論固定用較大回看範圍，確保K棒數量足夠，不受trade_limit影響
                              # (跟binance_client.py的MAX_TRADE_HISTORY保持一致，這裡切太少
                              # 也沒用，實際能用的資料量是兩者取較小值)


def compute_signal_from_trades(trades, interval_seconds=60, bucket_size=1.0, trade_limit=3000, current_price=None):
    """
    純計算版本：輸入任意來源的逐筆成交清單(即時的或歷史重播的都可以)，
    回傳跟compute_full_signal()一樣格式的完整訊號結果。

    trades必須是時間遞增排序、格式為[{"time","price","qty",...}, ...]。
    trade_limit只影響分價量表的取樣範圍，纏論一律用CHAN_LOOKBACK_TRADES內的資料
    (如果傳進來的trades本身就比較短，就整份都用)。

    current_price可以外部指定(例如即時模式想用bid/ask中價而不是最後一筆成交價)，
    不指定的話預設用trades最後一筆的成交價。這個值必須在呼叫generate_signal()
    之前就決定好，否則訊號判斷理由裡引用的價格會跟回傳的current_price對不上。
    """
    chan_trades = trades[-CHAN_LOOKBACK_TRADES:] if len(trades) > CHAN_LOOKBACK_TRADES else trades
    candles = build_candles(chan_trades, interval_seconds=interval_seconds)
    chan_data = analyze_chan(candles)
    atr = compute_atr(candles)  # 給ATR動態停損模式用，資料不足時是None(呼叫端要處理)
    choppiness_index = compute_choppiness_index(candles)  # 給震盪濾網用，資料不足時是None

    profile_trades = trades[-trade_limit:] if len(trades) > trade_limit else trades
    profile = compute_volume_profile(profile_trades, bucket_size=bucket_size)
    poc_info = poc_and_value_area(profile)

    if current_price is None:
        current_price = trades[-1]["price"] if trades else None

    result = generate_signal(chan_data, poc_info, current_price)
    result["atr"] = atr
    result["choppiness_index"] = choppiness_index
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


def compute_full_signal(interval_seconds=60, bucket_size=1.0, trade_limit=3000):
    """
    即時版本：從binance_streamer抓最新的逐筆成交，current_price優先用bid/ask中價
    (比用最後一筆成交價更貼近實際可成交價格)，沒有報價時才退回用最後一筆成交價。
    """
    trades = binance_streamer.get_recent_trades(limit=CHAN_LOOKBACK_TRADES)

    current_price = None
    latest_tick = binance_streamer.get_latest()
    if latest_tick and latest_tick.get("bid") and latest_tick.get("ask"):
        current_price = (float(latest_tick["bid"]) + float(latest_tick["ask"])) / 2

    return compute_signal_from_trades(
        trades,
        interval_seconds=interval_seconds,
        bucket_size=bucket_size,
        trade_limit=trade_limit,
        current_price=current_price,
    )
