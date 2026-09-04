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


def resample_candles(candles, interval_seconds):
    """
    把細週期K棒(通常是1分鐘)重新取樣成粗週期K棒。輸入必須時間遞增。
    interval_seconds=60時直接回傳原資料(複製)。最後一根是進行中的部分K棒，
    跟build_candles從逐筆成交聚合出來的行為一致(修正記錄見README)。
    """
    if not candles:
        return []
    interval_ms = interval_seconds * 1000
    out = []
    cur = None
    cur_bucket = None
    for c in candles:
        b = (c["bucket_start"] // interval_ms) * interval_ms
        if b != cur_bucket:
            if cur is not None:
                out.append(cur)
            cur_bucket = b
            cur = {"bucket_start": b, "open": c["open"], "high": c["high"], "low": c["low"],
                   "close": c["close"], "volume": c["volume"]}
        else:
            cur["high"] = max(cur["high"], c["high"])
            cur["low"] = min(cur["low"], c["low"])
            cur["close"] = c["close"]
            cur["volume"] += c["volume"]
    if cur is not None:
        out.append(cur)
    return out


# ---------------------------------------------------------------------------
# 1b. ATR (Average True Range) — 讓停損距離跟著市場實際波動度動態調整
# ---------------------------------------------------------------------------

def compute_atr(candles, period=14):
    """
    計算ATR：衡量近期價格實際波動幅度的指標，用來讓停損/移動停損距離
    自動跟著市場波動調整(震盪加大時停損跟著放寬，市場平靜時跟著收緊)，
    取代原本用固定點數猜一個距離的做法。

    True Range每根K棒取三者最大值：(high-low)、|high-前一根close|、|low-前一根close|，
    ATR是這個值的N期簡單移動平均(不是EMA，維持一致性且好理解)。

    candles是build_candles()的輸出(未經merge_inclusion處理的原始K線)，
    回傳ATR數值(跟價格同單位，例如黃金就是美元points)，資料不足時回傳None。
    """
    if len(candles) < 2:
        return None

    true_ranges = []
    for i in range(1, len(candles)):
        high = candles[i]["high"]
        low = candles[i]["low"]
        prev_close = candles[i - 1]["close"]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        true_ranges.append(tr)

    if not true_ranges:
        return None

    window = true_ranges[-period:] if len(true_ranges) >= period else true_ranges
    return sum(window) / len(window)


def compute_choppiness_index(candles, period=14):
    """
    計算Choppiness Index(震盪指標，業界常見的濾網指標)：不判斷方向，只判斷
    「市場現在有沒有在走出明確方向」。用來當進場濾網——偵測到目前是震盪盤時
    自動暫停開新倉，不管這個震盪發生在哪個時段，都能即時反應。

    公式：CI = 100 x log10( 過去N根K棒的True Range總和 / (N根K棒的最高點-最低點) ) / log10(N)

    數值介於0~100：
    - 數值越高(通常>61.8)代表越震盪／盤整：True Range總和相對於整體價格區間
      很大，代表價格在原地反覆折返，沒有走出淨移動
    - 數值越低(通常<38.2)代表趨勢越明確：True Range總和跟整體價格區間相近，
      代表價格是有效率地朝同一個方向移動，沒有太多來回

    candles是build_candles()的輸出，資料不足或分母為0(例如完全沒有波動)時回傳None。
    """
    if len(candles) < period + 1:
        return None

    window = candles[-(period + 1):]
    true_ranges = []
    for i in range(1, len(window)):
        high = window[i]["high"]
        low = window[i]["low"]
        prev_close = window[i - 1]["close"]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        true_ranges.append(tr)

    if len(true_ranges) < period:
        return None

    tr_sum = sum(true_ranges[-period:])
    range_window = window[-period:]
    highest_high = max(c["high"] for c in range_window)
    lowest_low = min(c["low"] for c in range_window)
    price_range = highest_high - lowest_low

    if price_range <= 0 or tr_sum <= 0:
        return None

    return 100 * math.log10(tr_sum / price_range) / math.log10(period)


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
    中樞判斷 + 延伸：連續三筆(bi[i], bi[i+1], bi[i+2])的價格區間有重疊，
    重疊區間 = [max(低點), min(高點)]，此區間有效(下界<上界)則構成一個中樞，
    核心邊界(ZG/ZD)就固定在這三筆算出來的值，不會再變動。

    之後檢查後續的筆有沒有延伸這個中樞：只要該筆的價格區間還跟[ZD, ZG]有重疊，
    就算「延伸」，中樞的時間範圍往後拉長、納入更多筆，但ZG/ZD不變；一旦某一筆
    完全不跟[ZD, ZG]重疊了，代表真正突破，這個中樞到此結束。

    掃描完一個延伸完的中樞後，從突破那一筆重新開始找下一個中樞，避免產生
    大量重疊、邊界互相矛盾的中樞紀錄(這是舊版「每3筆重新算一次」的問題，
    同一段真實的中樞會被拆成好幾個ZG/ZD不一致的片段)。
    """
    zhongshu_list = []
    i = 0
    n = len(bi_list)

    while i <= n - 3:
        b1, b2, b3 = bi_list[i], bi_list[i + 1], bi_list[i + 2]

        highs = [max(b["start_price"], b["end_price"]) for b in (b1, b2, b3)]
        lows = [min(b["start_price"], b["end_price"]) for b in (b1, b2, b3)]

        zg = min(highs)
        zd = max(lows)

        if zg <= zd:
            # 這三筆不構成中樞，往後移一筆繼續嘗試(維持原本逐筆掃描的行為)
            i += 1
            continue

        # 找到一個有效中樞，接著嘗試延伸：檢查後面的筆是否還跟[zd, zg]重疊
        end_index = i + 2
        bi_indices = [i, i + 1, i + 2]

        j = i + 3
        while j < n:
            bj = bi_list[j]
            bj_high = max(bj["start_price"], bj["end_price"])
            bj_low = min(bj["start_price"], bj["end_price"])

            still_overlaps = bj_high >= zd and bj_low <= zg
            if not still_overlaps:
                break  # 真正突破，延伸到此為止

            end_index = j
            bi_indices.append(j)
            j += 1

        zhongshu_list.append({
            "start_time": b1["start_time"],
            "end_time": bi_list[end_index]["end_time"],
            "zg": zg,
            "zd": zd,
            "bi_indices": bi_indices,
            "bi_count": len(bi_indices),
            "is_extended": len(bi_indices) > 3,
        })

        # 從突破那一筆(j)開始找下一個中樞，不重疊回顧已經納入這個中樞的筆
        i = j if j > i + 2 else i + 1

    return zhongshu_list


def _find_trend_zhongshu_pair(zhongshu_list):
    """
    在最新的中樞之前，往回找一個「不重疊」的中樞，兩者合起來才構成真正的趨勢
    (纏論定義：至少兩個中樞、且價格區間不重疊，才算走出一個趨勢，而不只是
    同一個大區間裡的盤整)。回傳(前一個趨勢中樞, 最新中樞)，找不到就回傳(None, 最新中樞)。
    """
    if not zhongshu_list:
        return None, None

    latest = zhongshu_list[-1]
    for candidate in reversed(zhongshu_list[:-1]):
        overlaps = candidate["zg"] >= latest["zd"] and candidate["zd"] <= latest["zg"]
        if not overlaps:
            return candidate, latest

    return None, latest


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


# ---------------------------------------------------------------------------
# 「多條件共振 + FVG」實驗性策略(resonance_fvg)專用指標 —— 目前只接回測，
# 不影響即時模擬單(見signal_engine.py的STRATEGY_TYPE切換說明)。
#
# 重要：這裡刻意重用上面的_ema()，跟纏論背馳判斷用的是同一套EMA計算方式。
# 原本考慮另外寫一份標準SMA種子的EMA實作，但那樣會讓系統裡同時存在兩種
# EMA/MACD計算方式，容易在不同地方得出「看起來像但實際上不一致」的數字，
# 增加除錯難度，所以統一共用同一套。
# ---------------------------------------------------------------------------

def compute_ema(candles, periods=None):
    """
    計算EMA(指數移動平均)，periods預設[9,21,50,100,200]。
    回傳 {period: 最新EMA值}，資料不足特定週期時該週期的值是None。
    """
    if periods is None:
        periods = [9, 21, 50, 100, 200]
    closes = [c["close"] for c in candles]
    result = {}
    for period in periods:
        if len(closes) < period:
            result[period] = None
            continue
        result[period] = _ema(closes, period)[-1]
    return result


def compute_rsi(candles, period=14):
    """
    計算RSI(相對強弱指標，0~100)，用Wilder's平滑法(業界標準做法)。
    資料不足時回傳None。
    """
    closes = [c["close"] for c in candles]
    if len(closes) < period + 1:
        return None

    gains, losses = [], []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def compute_macd(candles, fast=12, slow=26, signal=9):
    """
    計算MACD，回傳最新的{macd(快慢線差/DIF), signal(訊號線/DEA), histogram(柱狀圖)}。
    共用_macd_histogram()同一套_ema()實作(見上方說明)，資料不足時三個值都是None。
    """
    closes = [c["close"] for c in candles]
    if len(closes) < slow + signal:
        return {"macd": None, "signal": None, "histogram": None}

    ema_fast = _ema(closes, fast)
    ema_slow = _ema(closes, slow)
    dif = [f - s for f, s in zip(ema_fast, ema_slow)]
    dea = _ema(dif, signal)
    histogram = (dif[-1] - dea[-1]) * 2
    return {"macd": dif[-1], "signal": dea[-1], "histogram": histogram}


def find_fvg(candles, lookback=50):
    """
    尋找FVG(Fair Value Gap，公平價值缺口)：連續三根K棒，如果第1根的高點
    低於第3根的低點(看多缺口，當支撐參考)，或第1根的低點高於第3根的高點
    (看空缺口，當壓力參考)，中間留下一個「價格跳空、還沒被完全回補」的
    區間。只掃最近lookback根K棒，避免K棒一多、回傳的缺口列表過長。

    每個缺口會標示filled(是否已經被後續價格完全回補穿越過)，未回補的
    缺口才有支撐/壓力參考價值，已回補的通常視為失效。

    回傳由新到舊排序的缺口列表，每個是
    {type: "bullish"/"bearish", top, bottom, filled, candle_index}。
    """
    if len(candles) < 3:
        return []

    recent = candles[-lookback:] if len(candles) > lookback else candles
    fvgs = []
    for i in range(2, len(recent)):
        c1, c3 = recent[i - 2], recent[i]
        if c1["high"] < c3["low"]:
            fvgs.append({"type": "bullish", "top": c3["low"], "bottom": c1["high"], "candle_index": i})
        elif c1["low"] > c3["high"]:
            fvgs.append({"type": "bearish", "top": c1["low"], "bottom": c3["high"], "candle_index": i})

    for f in fvgs:
        filled = False
        for later in recent[f["candle_index"] + 1:]:
            if f["type"] == "bullish" and later["low"] <= f["bottom"]:
                filled = True
                break
            if f["type"] == "bearish" and later["high"] >= f["top"]:
                filled = True
                break
        f["filled"] = filled

    return list(reversed(fvgs))


def _detect_beichi(bi_list, merged_candles, zhongshu_list):
    """
    背馳判斷，區分「趨勢背馳」跟「盤整背馳」兩種可信度不同的類型：

    - 趨勢背馳：需要至少兩個「不重疊」的中樞(纏論定義的真正趨勢結構)，
      比較「連接兩個中樞之間的那一段走勢」跟「離開最新中樞的這一段走勢」，
      這是代表整個趨勢動能衰竭的訊號，可信度較高。
    - 盤整背馳：找不到兩個不重疊中樞時，退回比較最近兩筆同方向走勢，
      這種背馳範圍比較局部(可能只是同一個中樞內部的暫停)，可信度較低。

    兩種類型都用同一套「價格創新極值 + MACD柱狀圖面積縮小」的判斷方式，
    差別只在於「拿什麼去比較」，這個差異會讓signal.py給不同的訊號強度。
    """
    if len(bi_list) < 4:
        return {"has_beichi": False, "beichi_type": None, "detail": "筆的數量不足，還無法判斷背馳"}

    closes = [c["close"] for c in merged_candles]
    hist = _macd_histogram(closes)

    def bi_macd_area(bi):
        start, end = bi["start_index"], bi["end_index"]
        start, end = min(start, end), max(start, end)
        segment = hist[start:end + 1]
        return sum(abs(h) for h in segment)

    last_bi = bi_list[-1]
    same_direction_bi = [b for b in bi_list if b["direction"] == last_bi["direction"]]
    if len(same_direction_bi) < 2:
        return {"has_beichi": False, "beichi_type": None, "detail": "同方向筆數不足，還無法判斷背馳"}

    # 預設用「盤整背馳」的比較方式：最近兩筆同方向走勢
    compare_bi = same_direction_bi[-2]
    beichi_type = "盤整背馳" if zhongshu_list else "背馳(無中樞參考)"

    # 嘗試升級成「趨勢背馳」：需要兩個不重疊的中樞，且中間連接的那一筆方向
    # 要跟目前這筆一致，才代表這真的是同一個趨勢方向上的動能比較
    prev_zhongshu, latest_zhongshu = _find_trend_zhongshu_pair(zhongshu_list)
    if prev_zhongshu is not None and latest_zhongshu is not None:
        connecting_index = latest_zhongshu["bi_indices"][0] - 1
        if 0 <= connecting_index < len(bi_list):
            connecting_bi = bi_list[connecting_index]
            if connecting_bi["direction"] == last_bi["direction"]:
                compare_bi = connecting_bi
                beichi_type = "趨勢背馳"

    last_area = bi_macd_area(last_bi)
    compare_area = bi_macd_area(compare_bi)

    if last_bi["direction"] == "up":
        price_new_extreme = last_bi["end_price"] > compare_bi["end_price"]
    else:
        price_new_extreme = last_bi["end_price"] < compare_bi["end_price"]

    momentum_weaker = last_area < compare_area
    has_beichi = price_new_extreme and momentum_weaker

    return {
        "has_beichi": has_beichi,
        "beichi_type": beichi_type,
        "direction": last_bi["direction"],
        "last_bi_macd_area": round(last_area, 4),
        "compare_bi_macd_area": round(compare_area, 4),
        "price_made_new_extreme": price_new_extreme,
        "detail": (
            f"【{beichi_type}】價格創新極值但動能縮小，屬於背馳訊號" if has_beichi
            else f"【{beichi_type}】尚未同時滿足價格新極值+動能縮小兩個條件"
        ),
    }


def analyze_chan(candles):
    """
    纏論分析主入口：輸入K線清單(build_candles的輸出)，
    回傳分型/筆/中樞(含延伸)/背馳(區分趨勢/盤整)的完整分析結果。
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
    beichi = (
        _detect_beichi(bi_list, merged, zhongshu_list) if bi_list
        else {"has_beichi": False, "beichi_type": None, "detail": "尚無筆可供判斷"}
    )

    return {
        "merged_candle_count": len(merged),
        "fenxing_count": len(fenxing),
        "bi_count": len(bi_list),
        "bi_list": bi_list[-10:],  # 只回傳最近10筆，避免資料量過大
        "zhongshu_list": zhongshu_list[-5:],
        "beichi": beichi,
        "latest_zhongshu": zhongshu_list[-1] if zhongshu_list else None,
    }
