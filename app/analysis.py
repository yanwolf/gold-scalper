"""
黃金極短線分析核心模組。

三個主要功能，彼此獨立、可單獨呼叫：
1. build_candles()        — 把逐筆成交(trades)聚合成K線(預設5分鐘)
2. compute_volume_profile()— 把逐筆成交依價格分箱累加成交量，重現類似截圖裡的分價量表
3. analyze_chan()          — 纏論分析：分型(fenxing) -> 筆(bi) -> 中樞(zhongshu) -> 背馳(beichi)判斷

設計原則：
- 不依賴 pandas，純 Python 實作，減少 Zeabur 容器的套件負擔
- 輸入資料統一用 binance_streamer.get_recent_trades() 拿到的逐筆成交清單
  （每筆含 time, price, qty），之後如果要接其他資料源，只要轉成同樣格式即可套用同一套邏輯
- 這是第一版，纏論部分用簡化規則實作（標準教學版分型/筆/中樞定義），
  之後如果要更嚴謹（處理跳空、盤整背馳等細節），可以在這個檔案內逐步擴充，
  不影響 main.py 的 endpoint 介面
"""

import math
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# 1. K線聚合
# ---------------------------------------------------------------------------

def build_candles(trades, interval_seconds=300):
    """
    把逐筆成交(trades)依時間分桶聚合成OHLCV K線。

    trades: [{"time": epoch_ms, "price": float, "qty": float, ...}, ...]，
            必須是時間遞增排序（binance_streamer 的 deque 本來就是這個順序）
    interval_seconds: K棒週期，預設300秒(5分鐘)

    回傳: [{"bucket_start": epoch_ms, "open","high","low","close","volume"}, ...]
    """
    if not trades:
        return []

    interval_ms = interval_seconds * 1000
    candles = []
    current_bucket = None
    current = None

    for t in trades:
        price = t["price"]
        qty = t["qty"]
        ts = t["time"]
        bucket_start = (ts // interval_ms) * interval_ms

        if bucket_start != current_bucket:
            if current is not None:
                candles.append(current)
            current_bucket = bucket_start
            current = {
                "bucket_start": bucket_start,
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": qty,
            }
        else:
            current["high"] = max(current["high"], price)
            current["low"] = min(current["low"], price)
            current["close"] = price
            current["volume"] += qty

    if current is not None:
        candles.append(current)

    return candles


# ---------------------------------------------------------------------------
# 2. 分價量表 (Volume Profile)
# ---------------------------------------------------------------------------

def compute_volume_profile(trades, bucket_size=1.0):
    """
    把逐筆成交依價格分箱(bucket_size為每箱價格寬度)累加成交量。

    回傳依成交量由大到小排序的清單:
    [{"price_level": float, "volume": float, "pct_of_max": float}, ...]
    pct_of_max 是相對於最大量那個箱子的百分比，對應截圖裡看到的百分比呈現方式。
    """
    if not trades:
        return []

    buckets = {}
    for t in trades:
        level = round(t["price"] / bucket_size) * bucket_size
        buckets[level] = buckets.get(level, 0.0) + t["qty"]

    max_volume = max(buckets.values()) if buckets else 1.0

    result = [
        {
            "price_level": level,
            "volume": volume,
            "pct_of_max": round((volume / max_volume) * 100, 2),
        }
        for level, volume in buckets.items()
    ]
    result.sort(key=lambda x: x["price_level"], reverse=True)
    return result


def poc_and_value_area(volume_profile, value_area_pct=0.70):
    """
    從分價量表算出 POC(Point of Control，成交量最大的價位)
    和 Value Area(累積約70%成交量的價格區間)，這是Volume Profile的標準延伸指標。
    """
    if not volume_profile:
        return {"poc": None, "value_area_high": None, "value_area_low": None}

    by_volume = sorted(volume_profile, key=lambda x: x["volume"], reverse=True)
    poc = by_volume[0]["price_level"]

    total_volume = sum(x["volume"] for x in volume_profile)
    target = total_volume * value_area_pct

    accumulated = 0.0
    included_levels = set()
    for row in by_volume:
        accumulated += row["volume"]
        included_levels.add(row["price_level"])
        if accumulated >= target:
            break

    return {
        "poc": poc,
        "value_area_high": max(included_levels),
        "value_area_low": min(included_levels),
    }


# ---------------------------------------------------------------------------
# 3. 纏論：分型 -> 筆 -> 中樞 -> 背馳
# ---------------------------------------------------------------------------

def _merge_inclusion(candles):
    """
    處理K棒之間的「包含關係」：若相鄰K棒一根的高低點完全包住另一根，
    依照纏論規則合併成一根（依前一段趨勢方向決定取高點還是低點）。
    這是纏論分析的必要前處理，不做這步分型會判斷錯誤。
    """
    if len(candles) < 2:
        return list(candles)

    merged = [dict(candles[0])]
    direction = 0  # 1=上升, -1=下降, 0=尚未決定

    for c in candles[1:]:
        last = merged[-1]

        contains_forward = last["high"] >= c["high"] and last["low"] <= c["low"]
        contains_backward = c["high"] >= last["high"] and c["low"] <= last["low"]

        if contains_forward or contains_backward:
            if direction >= 0:
                last["high"] = max(last["high"], c["high"])
                last["low"] = max(last["low"], c["low"])
            else:
                last["high"] = min(last["high"], c["high"])
                last["low"] = min(last["low"], c["low"])
            last["close"] = c["close"]
            last["bucket_start"] = c["bucket_start"]
        else:
            if c["high"] > last["high"]:
                direction = 1
            elif c["low"] < last["low"]:
                direction = -1
            merged.append(dict(c))

    return merged


def _find_fenxing(merged_candles):
    """
    分型判斷：連續三根合併後的K棒，中間那根的高點比左右都高 -> 頂分型；
    中間那根的低點比左右都低 -> 底分型。
    """
    fenxing = []
    for i in range(1, len(merged_candles) - 1):
        left, mid, right = merged_candles[i - 1], merged_candles[i], merged_candles[i + 1]
        if mid["high"] > left["high"] and mid["high"] > right["high"]:
            fenxing.append({"index": i, "type": "top", "price": mid["high"], "time": mid["bucket_start"]})
        elif mid["low"] < left["low"] and mid["low"] < right["low"]:
            fenxing.append({"index": i, "type": "bottom", "price": mid["low"], "time": mid["bucket_start"]})
    return fenxing


def _build_bi(fenxing, min_gap=1):
    """
    筆的構成：相鄰、方向相反(頂->底或底->頂)的分型之間，且中間至少間隔 min_gap 根合併K棒，
    才能連成一筆。這是簡化版規則，先滿足「不同型、有間隔」這個核心條件。
    """
    bi_list = []
    if len(fenxing) < 2:
        return bi_list

    prev = fenxing[0]
    for cur in fenxing[1:]:
        if cur["type"] == prev["type"]:
            # 同型分型：保留極值較大的那個，濾掉次要的雜訊分型
            if cur["type"] == "top" and cur["price"] > prev["price"]:
                prev = cur
            elif cur["type"] == "bottom" and cur["price"] < prev["price"]:
                prev = cur
            continue

        if cur["index"] - prev["index"] >= min_gap:
            direction = "up" if prev["type"] == "bottom" else "down"
            bi_list.append({
                "start_index": prev["index"],
                "end_index": cur["index"],
                "start_price": prev["price"],
                "end_price": cur["price"],
                "start_time": prev["time"],
                "end_time": cur["time"],
                "direction": direction,
            })
            prev = cur

    return bi_list


def _find_zhongshu(bi_list):
    """
    中樞判斷：連續三筆(bi[i], bi[i+1], bi[i+2])的價格區間有重疊，
    重疊區間 = [max(低點), min(高點)]，此區間有效(下界<上界)則構成一個中樞。
    之後如果後續的筆還在這個區間內延伸，中樞會持續擴張(這裡先抓出基本的三筆中樞，
    擴張邏輯留待下一版加)。
    """
    zhongshu_list = []
    for i in range(len(bi_list) - 2):
        b1, b2, b3 = bi_list[i], bi_list[i + 1], bi_list[i + 2]

        highs = [max(b["start_price"], b["end_price"]) for b in (b1, b2, b3)]
        lows = [min(b["start_price"], b["end_price"]) for b in (b1, b2, b3)]

        zg = min(highs)  # 中樞上界
        zd = max(lows)   # 中樞下界

        if zg > zd:
            zhongshu_list.append({
                "start_time": b1["start_time"],
                "end_time": b3["end_time"],
                "zg": zg,
                "zd": zd,
                "bi_indices": [i, i + 1, i + 2],
            })

    return zhongshu_list


def _ema(values, period):
    if not values:
        return []
    k = 2 / (period + 1)
    result = [values[0]]
    for v in values[1:]:
        result.append(v * k + result[-1] * (1 - k))
    return result


def _macd_histogram(closes, fast=12, slow=26, signal=9):
    if len(closes) < slow + signal:
        return [0.0] * len(closes)
    ema_fast = _ema(closes, fast)
    ema_slow = _ema(closes, slow)
    dif = [f - s for f, s in zip(ema_fast, ema_slow)]
    dea = _ema(dif, signal)
    hist = [(d - e) * 2 for d, e in zip(dif, dea)]
    return hist


def _detect_beichi(bi_list, merged_candles):
    """
    背馳判斷(簡化版)：取同方向、且都跟同一個中樞相關的最後兩筆走勢，
    比較「價格是否創新高/新低」與「MACD柱狀圖面積是否縮小」。
    價格創新極值但動能(MACD面積)不如前一筆 -> 判定為背馳，是短線可能反轉的訊號。
    """
    if len(bi_list) < 4:
        return {"has_beichi": False, "detail": "筆的數量不足，還無法判斷背馳"}

    closes = [c["close"] for c in merged_candles]
    hist = _macd_histogram(closes)

    def bi_macd_area(bi):
        start, end = bi["start_index"], bi["end_index"]
        start, end = min(start, end), max(start, end)
        segment = hist[start:end + 1]
        return sum(abs(h) for h in segment)

    same_direction_bi = [b for b in bi_list if b["direction"] == bi_list[-1]["direction"]]
    if len(same_direction_bi) < 2:
        return {"has_beichi": False, "detail": "同方向筆數不足，還無法判斷背馳"}

    last_bi = same_direction_bi[-1]
    prev_bi = same_direction_bi[-2]

    last_area = bi_macd_area(last_bi)
    prev_area = bi_macd_area(prev_bi)

    if last_bi["direction"] == "up":
        price_new_extreme = last_bi["end_price"] > prev_bi["end_price"]
    else:
        price_new_extreme = last_bi["end_price"] < prev_bi["end_price"]

    momentum_weaker = last_area < prev_area

    has_beichi = price_new_extreme and momentum_weaker

    return {
        "has_beichi": has_beichi,
        "direction": last_bi["direction"],
        "last_bi_macd_area": round(last_area, 4),
        "prev_bi_macd_area": round(prev_area, 4),
        "price_made_new_extreme": price_new_extreme,
        "detail": (
            "價格創新極值但動能縮小，屬於背馳訊號" if has_beichi
            else "尚未同時滿足價格新極值+動能縮小兩個條件"
        ),
    }


def analyze_chan(candles):
    """
    纏論分析主入口：輸入K線清單(build_candles的輸出)，
    回傳分型/筆/中樞/背馳的完整分析結果。
    """
    if len(candles) < 5:
        return {
            "message": "K棒數量不足，至少需要5根才能開始分型判斷",
            "candle_count": len(candles),
        }

    merged = _merge_inclusion(candles)
    fenxing = _find_fenxing(merged)
    bi_list = _build_bi(fenxing)
    zhongshu_list = _find_zhongshu(bi_list)
    beichi = _detect_beichi(bi_list, merged) if bi_list else {"has_beichi": False, "detail": "尚無筆可供判斷"}

    return {
        "merged_candle_count": len(merged),
        "fenxing_count": len(fenxing),
        "bi_count": len(bi_list),
        "bi_list": bi_list[-10:],  # 只回傳最近10筆，避免資料量過大
        "zhongshu_list": zhongshu_list[-5:],
        "beichi": beichi,
        "latest_zhongshu": zhongshu_list[-1] if zhongshu_list else None,
    }
