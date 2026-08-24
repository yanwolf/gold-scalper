"""
歷史回測模組。

用途：抓Binance期貨XAUUSDT的歷史K線資料，還原成逐筆成交格式，
按時間順序重播，套用跟即時模擬單完全相同的訊號邏輯(signal_engine)
和交易規則(trading_core)，快速驗證這套策略在過去一段時間表現如何，
不用像即時模擬單一樣乾等好幾天才能累積到有意義的樣本數。

重要設計：Walk-forward重播，避免look-ahead bias(用到未來資料)。
每一步只用「當下時間點為止」的歷史資料去計算訊號，不會偷看後面的價格
才回頭決定進場，這樣回測結果才有參考價值。

資料還原的限制：Binance K線本身沒有逐筆明細，這裡用開高低收四個價位
各自帶1/4成交量，還原成4筆「合成成交」塞回trades清單，讓後續能沿用
既有的build_candles/compute_volume_profile邏輯，不用另外寫一套。
這是近似值，分價量表的精細度會比即時模式(用真實逐筆成交)粗糙一些，
但足夠用來抓策略的大方向表現。
"""

import logging
from datetime import datetime, timezone

import requests

from app.signal_engine import compute_signal_from_trades
from app import trading_core
from app.trading_stats import compute_stats, assess_readiness

logger = logging.getLogger("backtest")

BINANCE_KLINES_URL = "https://fapi.binance.com/fapi/v1/klines"
MAX_KLINES_PER_REQUEST = 1500

DEFAULT_INTERVAL_SECONDS = 60
DEFAULT_BUCKET_SIZE = 1.0
DEFAULT_TRADE_LIMIT = 3000
DEFAULT_SL_POINTS = 3.0
DEFAULT_TRAIL_TRIGGER_POINTS = 3.0
DEFAULT_TRAIL_DISTANCE_POINTS = 3.0

MAX_BACKTEST_DAYS = 7  # 天數上限，避免單次回測跑太久(纏論分析在大量K棒上會變慢)


def fetch_historical_klines(symbol="XAUUSDT", interval="1m", days=2):
    """
    分頁抓取Binance期貨歷史K線，回傳由舊到新排序的原始K線資料。
    公開市場資料，不需要API Key。
    """
    days = min(days, MAX_BACKTEST_DAYS)
    end_time = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_time = end_time - days * 24 * 60 * 60 * 1000

    all_klines = []
    cursor = start_time

    while cursor < end_time:
        params = {
            "symbol": symbol,
            "interval": interval,
            "startTime": cursor,
            "endTime": end_time,
            "limit": MAX_KLINES_PER_REQUEST,
        }
        resp = requests.get(BINANCE_KLINES_URL, params=params, timeout=15)
        resp.raise_for_status()
        batch = resp.json()

        if not batch:
            break

        all_klines.extend(batch)

        last_open_time = batch[-1][0]
        if last_open_time <= cursor:
            break  # 保險：避免因為API回傳異常造成無窮迴圈
        cursor = last_open_time + 1

        if len(batch) < MAX_KLINES_PER_REQUEST:
            break  # 這批資料不滿，代表已經抓到最新的了

    return all_klines


def klines_to_synthetic_trades(klines):
    """
    把K線(open_time, open, high, low, close, volume, ...)還原成逐筆成交近似值。
    每根K線拆成開/高/低/收四個時間點的合成成交，時間平均分配在該根K線的區間內，
    確保還原後的trades清單仍然是時間遞增排序。
    """
    trades = []
    for k in klines:
        open_time = int(k[0])
        close_time = int(k[6])
        o, h, l, c, v = float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])

        span = max(close_time - open_time, 4)
        qty_each = v / 4 if v > 0 else 0.0001  # 完全沒成交量的K線給極小值，避免分價量表除零

        trades.append({"time": open_time, "price": o, "qty": qty_each})
        trades.append({"time": open_time + span // 3, "price": h, "qty": qty_each})
        trades.append({"time": open_time + span * 2 // 3, "price": l, "qty": qty_each})
        trades.append({"time": close_time, "price": c, "qty": qty_each})

    trades.sort(key=lambda t: t["time"])
    return trades


def run_backtest(
    days=2,
    interval_seconds=DEFAULT_INTERVAL_SECONDS,
    bucket_size=DEFAULT_BUCKET_SIZE,
    trade_limit=DEFAULT_TRADE_LIMIT,
    sl_points=DEFAULT_SL_POINTS,
    trail_trigger_points=DEFAULT_TRAIL_TRIGGER_POINTS,
    trail_distance_points=DEFAULT_TRAIL_DISTANCE_POINTS,
):
    """
    執行完整回測流程：抓歷史資料 -> 還原成成交 -> 逐根K線重播 -> 套用交易規則 -> 統計績效。
    回傳格式跟 /paper-trading/summary 一致，方便dashboard共用同一套渲染邏輯。
    """
    klines = fetch_historical_klines(days=days)
    if not klines:
        return {"error": "抓不到歷史K線資料，請稍後再試"}

    trades = klines_to_synthetic_trades(klines)

    # 以每根K線的收盤時間為一個重播步驟，跟即時模式「每次檢查訊號」的頻率概念一致
    step_times = sorted({int(k[6]) for k in klines})

    position = None
    closed_trades = []

    for step_time in step_times:
        # 只用「這個時間點為止」的資料，避免look-ahead bias
        trades_so_far = [t for t in trades if t["time"] <= step_time]
        if len(trades_so_far) < 20:  # 資料太少，跳過這一步(通常是回測最一開始的幾步)
            continue

        current_price = trades_so_far[-1]["price"]

        result = compute_signal_from_trades(
            trades_so_far,
            interval_seconds=interval_seconds,
            bucket_size=bucket_size,
            trade_limit=trade_limit,
            current_price=current_price,
        )

        if position:
            trading_core.update_trailing_stop(position, current_price, trail_trigger_points, trail_distance_points)
            exit_reason = trading_core.check_exit(position, current_price, result["stage"], result["direction"])
            if exit_reason:
                exit_time_iso = datetime.fromtimestamp(step_time / 1000, tz=timezone.utc).isoformat()
                closed = trading_core.close_position(position, current_price, exit_reason, exit_time_iso)
                closed_trades.append(closed)
                position = None

        if position is None and result["stage"] == "訊號" and result["direction"]:
            entry_time_iso = datetime.fromtimestamp(step_time / 1000, tz=timezone.utc).isoformat()
            position = trading_core.open_position(
                direction=result["direction"],
                current_price=current_price,
                entry_time=entry_time_iso,
                sl_points=sl_points,
                chan_reason=result["chan"]["reason"],
                profile_reason=result["profile"]["reason"],
            )

    stats = compute_stats(closed_trades)
    readiness = assess_readiness(stats)

    return {
        **stats,
        "open_position_at_end": position,  # 回測結束時如果還有未平倉部位，僅供參考，不計入統計
        "recent_trades": sorted(closed_trades, key=lambda t: t["exit_time"], reverse=True)[:100],
        "readiness": readiness,
        "backtest_days": days,
        "kline_count": len(klines),
        "synthetic_trade_count": len(trades),
        "sl_points": sl_points,
        "trail_trigger_points": trail_trigger_points,
        "trail_distance_points": trail_distance_points,
    }
