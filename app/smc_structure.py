"""
SMC 市場結構策略 (strategy_type="smc_structure") —— 1小時K長線結構引擎。

參考的是TradingView上常見的「Smart Money Concepts + Cipher B」看盤法：
  1. 市場結構：swing高低點 -> MSS(結構轉換) -> 連續BOS(結構突破)確認趨勢
  2. 供需區：OB(訂單塊，突破前最後一根反向K) + FVG(三根K的價格缺口)
  3. 觸發：WaveTrend(Cipher B的核心振盪器)在超買/超賣區交叉
  4. 濾網：EMA20/EMA50排列要跟趨勢同向

設計原則跟既有策略一致：
- 純函式，輸入一份K棒清單(專案標準格式 {bucket_start,open,high,low,close,volume})
  就回傳訊號，即時模擬單/回測共用同一份邏輯。
- 回傳格式沿用 generate_signal() 的 {stage, direction, chan, profile, current_price}，
  跟resonance_fvg一樣「借用」chan/profile欄位放判斷理由(chan=結構面、profile=觸發面)，
  讓trading_core/paper_trading/backtest/dashboard完全不用改就能吃。
- 這裡「不」自己管停損/停利，出場交給既有的trading_core(固定/ATR移動停損)，
  只額外提供 smc.suggested_sl_points(以OB/FVG外緣算的結構停損距離)，
  paper_trading/backtest在strategy_type=="smc_structure"時會優先用它當初始停損。
- 每次呼叫都用整段K棒重算結構(無狀態)，重啟不會遺失狀態；訊號只在
  「最後一根已收盤K棒」剛好發生WaveTrend交叉時才給，避免同一個區域反覆觸發。

資料長度需求：結構判定至少要 SMC_MIN_CANDLES 根1小時K(約12天)，
但binance_client的1分K快取預設只保留3天(72根1小時K)，所以這個模組自帶
一個「1小時K REST快取」(get_hourly_history)，signal_engine在即時路徑會把
REST歷史 + 本地重取樣的最新K棒接起來用，不用改MINUTE_BAR_HISTORY_DAYS。
"""

import logging
import threading
import time

logger = logging.getLogger("smc_structure")

SMC_INTERVAL_SECONDS = 3600
SMC_MIN_CANDLES = 250        # 少於這個數量就不判斷(回傳中性)
SMC_MAX_CANDLES = 600        # 每次判斷最多吃這麼多根(結構狀態只跟最近幾百根有關，zone_max_age=60)，控制回測每步的計算量
SMC_HISTORY_LIMIT = 1500     # REST單次最多1500根1小時K(約62天)，足夠warmup

DEFAULT_SMC_CFG = {
    "swing_n": 5,            # swing左右各N根；4小時可用3
    "ema_fast": 20,
    "ema_slow": 50,
    "wt_ch": 10,             # WaveTrend channel length
    "wt_avg": 21,            # WaveTrend average length
    "wt_level": 40,          # 超買/超賣門檻(交叉前兩根內要碰到 wt_level*0.6=24 才算「高檔/低檔」)；
                             # 原本53在1小時K黃金上太嚴(2588個在區域內的訊號步只有22個過關)
    "confirm_bos": 2,        # MSS後要幾次BOS才確認趨勢
    "zone_max_age": 60,      # OB/FVG幾根K後失效
    "sl_buffer_pct": 0.0015, # 結構停損放在區域外緣再多0.15%
    "require_ema": True,
    "touch_window": 4,       # 「碰到區域」跟「WaveTrend交叉」允許發生在最近幾根K內(不必同一根)
}

# 可用Zeabur環境變數覆寫(不用改程式重部署)：SMC_SWING_N / SMC_CONFIRM_BOS / SMC_WT_LEVEL /
# SMC_ZONE_MAX_AGE / SMC_REQUIRE_EMA(0或1)。訊號太稀疏時優先試 SMC_REQUIRE_EMA=0、SMC_CONFIRM_BOS=1。
import os as _os
for _k, _env, _cast in (("swing_n", "SMC_SWING_N", int), ("confirm_bos", "SMC_CONFIRM_BOS", int),
                        ("wt_level", "SMC_WT_LEVEL", float), ("zone_max_age", "SMC_ZONE_MAX_AGE", int),
                        ("touch_window", "SMC_TOUCH_WINDOW", int),
                        ("require_ema", "SMC_REQUIRE_EMA", lambda v: v.strip() not in ("0", "false", "False"))):
    _v = _os.getenv(_env)
    if _v:
        try:
            DEFAULT_SMC_CFG[_k] = _cast(_v)
        except (TypeError, ValueError):
            pass


# ---------------------------------------------------------------------------
# 1小時K REST 快取(給即時路徑補足歷史長度用)
# ---------------------------------------------------------------------------
_hist_lock = threading.Lock()
_hist_cache = {"candles": [], "fetched_at": 0.0}
HIST_REFRESH_SECONDS = 600  # 最多10分鐘重抓一次(每根1小時K收盤後很快就會補上)


def fetch_hourly_klines(symbol="XAUUSDT", limit=SMC_HISTORY_LIMIT):
    """抓幣安期貨1小時K(公開資料，不用API key)，轉成專案標準K棒格式，時間遞增。"""
    import requests
    url = "https://fapi.binance.com/fapi/v1/klines"
    resp = requests.get(url, params={"symbol": symbol, "interval": "1h", "limit": limit}, timeout=15)
    resp.raise_for_status()
    out = []
    for k in resp.json():
        try:
            out.append({"bucket_start": int(k[0]), "open": float(k[1]), "high": float(k[2]),
                        "low": float(k[3]), "close": float(k[4]), "volume": float(k[5])})
        except (TypeError, ValueError, IndexError):
            continue
    return out


def get_hourly_history(symbol="XAUUSDT", force=False):
    """帶快取的1小時K歷史。抓失敗時回傳上一次成功的快取(可能是空的)，不會拋例外。"""
    now = time.time()
    with _hist_lock:
        fresh = (now - _hist_cache["fetched_at"]) < HIST_REFRESH_SECONDS
        if _hist_cache["candles"] and fresh and not force:
            return list(_hist_cache["candles"])
    try:
        candles = fetch_hourly_klines(symbol=symbol)
        if candles:
            with _hist_lock:
                _hist_cache["candles"] = candles
                _hist_cache["fetched_at"] = now
            return list(candles)
    except Exception as e:
        logger.warning(f"1小時K歷史抓取失敗，沿用快取: {e}")
    with _hist_lock:
        return list(_hist_cache["candles"])


def merge_hourly_candles(history, recent):
    """
    把REST歷史(較舊、已收盤)跟本地重取樣的最新K棒(含進行中最後一根)接起來，
    同一個bucket_start以本地資料為準。回傳時間遞增。
    """
    merged = {c["bucket_start"]: c for c in (history or [])}
    merged.update({c["bucket_start"]: c for c in (recent or [])})
    return [merged[k] for k in sorted(merged)]


# ---------------------------------------------------------------------------
# 指標
# ---------------------------------------------------------------------------
def _ema_series(vals, n):
    out = [None] * len(vals)
    if len(vals) < n:
        return out
    k = 2 / (n + 1)
    s = sum(vals[:n]) / n
    out[n - 1] = s
    for i in range(n, len(vals)):
        s = vals[i] * k + s * (1 - k)
        out[i] = s
    return out


def compute_wavetrend(candles, ch=10, avg=21):
    """Cipher B的WaveTrend：回傳(wt1, wt2)兩條序列，資料不足處是None。"""
    n = len(candles)
    hlc3 = [(c["high"] + c["low"] + c["close"]) / 3 for c in candles]
    esa = _ema_series(hlc3, ch)
    dev_src = [abs(hlc3[i] - esa[i]) if esa[i] is not None else 0.0 for i in range(n)]
    d = _ema_series(dev_src, ch)
    ci = []
    for i in range(n):
        if esa[i] is None or not d[i]:
            ci.append(0.0)
        else:
            ci.append((hlc3[i] - esa[i]) / (0.015 * d[i]))
    wt1 = _ema_series(ci, avg)
    wt2 = [None] * n
    for i in range(3, n):
        if None not in (wt1[i], wt1[i - 1], wt1[i - 2], wt1[i - 3]):
            wt2[i] = (wt1[i] + wt1[i - 1] + wt1[i - 2] + wt1[i - 3]) / 4
    return wt1, wt2


# ---------------------------------------------------------------------------
# 市場結構
# ---------------------------------------------------------------------------
def _find_swings(candles, n):
    """swing點：第p根的高點是左右各n根裡最高 -> swing high；低點最低 -> swing low。
    回傳 [(index, "H"/"L", price), ...] 時間遞增。swing在p+n那根才「確認」，
    這裡用確認時間排序，避免look-ahead。"""
    swings = []
    for p in range(n, len(candles) - n):
        win = candles[p - n:p + n + 1]
        if candles[p]["high"] >= max(c["high"] for c in win):
            swings.append((p + n, "H", candles[p]["high"], p))
        if candles[p]["low"] <= min(c["low"] for c in win):
            swings.append((p + n, "L", candles[p]["low"], p))
    swings.sort()
    return swings


def _last_opposite_candle(candles, i, side, lookback=12):
    """OB：突破那根K之前最後一根反向K(做空找最後一根收紅K，做多找最後一根收黑K)。"""
    j = i - 1
    while j > 0 and j > i - lookback:
        c = candles[j]
        if side == "bearish" and c["close"] > c["open"]:
            return c
        if side == "bullish" and c["close"] < c["open"]:
            return c
        j -= 1
    return None


def analyze_structure(candles, cfg=None, snapshots=False):
    """
    整段K棒掃一遍(純因果：第i根的狀態只用到<=i的資料)，回傳目前的結構狀態：
    {trend, pending, bos_count, events[], zones[], last_hh, last_ll, last_break}
    snapshots=True時改回傳「每一根K收盤時的狀態」清單(回測用：整段算一次，
    每個訊號步直接查表，不用每步重算，365天回測從幾十秒降到幾秒)。
    """
    cfg = {**DEFAULT_SMC_CFG, **(cfg or {})}
    n = cfg["swing_n"]
    swings = _find_swings(candles, n)
    swing_ptr = 0

    trend = None
    pending = None
    bos_count = 0
    last_hh = last_ll = None
    events = []
    zones = []
    last_break = {"bearish": None, "bullish": None}  # 各方向最近一次MSS/BOS的突破價
    snaps = [] if snapshots else None

    for i in range(len(candles)):
        while swing_ptr < len(swings) and swings[swing_ptr][0] <= i:
            _, kind, price, _ = swings[swing_ptr]
            if kind == "H":
                last_hh = price
            else:
                last_ll = price
            swing_ptr += 1

        if last_hh is not None and last_ll is not None:
            c = candles[i]
            broke_dn = c["close"] < last_ll
            broke_up = c["close"] > last_hh

            if broke_dn:
                if trend != "bearish" and pending != "bearish":
                    pending, bos_count = "bearish", 0
                    events.append({"index": i, "time": c["bucket_start"], "type": "MSS", "direction": "bearish", "price": last_ll})
                else:
                    bos_count += 1
                    events.append({"index": i, "time": c["bucket_start"], "type": "BOS", "direction": "bearish", "price": last_ll})
                    if bos_count >= cfg["confirm_bos"]:
                        trend = "bearish"
                last_break["bearish"] = last_ll
                ob = _last_opposite_candle(candles, i, "bearish")
                if ob:
                    zones.append({"kind": "OB", "side": "bearish", "top": ob["high"], "bot": ob["low"], "index": i})
                last_ll = c["low"]
            elif broke_up:
                if trend != "bullish" and pending != "bullish":
                    pending, bos_count = "bullish", 0
                    events.append({"index": i, "time": c["bucket_start"], "type": "MSS", "direction": "bullish", "price": last_hh})
                else:
                    bos_count += 1
                    events.append({"index": i, "time": c["bucket_start"], "type": "BOS", "direction": "bullish", "price": last_hh})
                    if bos_count >= cfg["confirm_bos"]:
                        trend = "bullish"
                last_break["bullish"] = last_hh
                ob = _last_opposite_candle(candles, i, "bullish")
                if ob:
                    zones.append({"kind": "OB", "side": "bullish", "top": ob["high"], "bot": ob["low"], "index": i})
                last_hh = c["high"]

            if i >= 2:
                a, b = candles[i - 2], candles[i]
                if a["low"] > b["high"]:
                    zones.append({"kind": "FVG", "side": "bearish", "top": a["low"], "bot": b["high"], "index": i})
                elif a["high"] < b["low"]:
                    zones.append({"kind": "FVG", "side": "bullish", "top": b["low"], "bot": a["high"], "index": i})

            keep = []
            for z in zones:
                if i - z["index"] > cfg["zone_max_age"]:
                    continue
                if z["side"] == "bearish" and c["close"] > z["top"]:
                    continue
                if z["side"] == "bullish" and c["close"] < z["bot"]:
                    continue
                keep.append(z)
            zones = keep

        if snapshots:
            snaps.append({"trend": trend, "pending": pending, "bos_count": bos_count,
                          "zones": list(zones), "last_hh": last_hh, "last_ll": last_ll,
                          "last_break": dict(last_break)})

    if snapshots:
        return snaps, events
    return {
        "trend": trend,
        "pending": pending,
        "bos_count": bos_count,
        "events": events[-20:],
        "zones": zones,
        "last_hh": last_hh,
        "last_ll": last_ll,
        "last_break": last_break,
    }


# ---------------------------------------------------------------------------
# 訊號
# ---------------------------------------------------------------------------
def _neutral(current_price, reason, extra=None):
    return {
        "stage": "中性",
        "direction": None,
        "chan": {"bias": None, "strength": 0, "reason": reason},
        "profile": {"bias": None, "strength": 0, "reason": "—"},
        "current_price": current_price,
        "smc": extra or {},
    }


def precompute(closed, cfg=None):
    """對一整段「已收盤」K棒算一次結構快照 + EMA + WaveTrend，之後用evaluate_at()逐根查。"""
    cfg = {**DEFAULT_SMC_CFG, **(cfg or {})}
    snaps, events = analyze_structure(closed, cfg, snapshots=True)
    closes = [c["close"] for c in closed]
    return {
        "cfg": cfg,
        "candles": closed,
        "snaps": snaps,
        "events": events,
        "ef": _ema_series(closes, cfg["ema_fast"]),
        "es": _ema_series(closes, cfg["ema_slow"]),
        "wt": compute_wavetrend(closed, cfg["wt_ch"], cfg["wt_avg"]),
    }


def evaluate_at(pre, i, current_price=None):
    """
    在precompute()的結果上評估「第i根已收盤K收盤那一刻」的訊號(只看<=i的資料)。
    stage: "訊號"(趨勢確認+最近touch_window根內碰過同向OB/FVG+WaveTrend交叉+EMA排列)、
           "關注"(趨勢確認且碰過區域，等交叉)、"中性"。
    """
    cfg = pre["cfg"]
    closed = pre["candles"]
    if current_price is None:
        current_price = closed[i]["close"]
    if i + 1 < SMC_MIN_CANDLES:
        return _neutral(current_price, f"1小時K資料不足({i + 1}/{SMC_MIN_CANDLES}根)")

    st = pre["snaps"][i]
    ef, es = pre["ef"], pre["es"]
    wt1, wt2 = pre["wt"]
    c = closed[i]
    trend = st["trend"]

    extra = {
        "interval_seconds": SMC_INTERVAL_SECONDS,
        "candle_count": i + 1,
        "trend": trend,
        "pending": st["pending"],
        "bos_count": st["bos_count"],
        "last_hh": st["last_hh"],
        "last_ll": st["last_ll"],
        "zones": st["zones"],
        "ema_fast": ef[i],
        "ema_slow": es[i],
        "wt1": wt1[i],
        "wt2": wt2[i],
        "suggested_sl_points": None,
    }

    if trend is None:
        return _neutral(current_price, "結構未確認(還沒有MSS+足夠BOS)", extra)
    if None in (ef[i], es[i], wt1[i], wt2[i]):
        return _neutral(current_price, "指標資料不足", extra)

    if st["pending"] == trend:
        struct_reason = f"結構{'看空' if trend == 'bearish' else '看多'}：MSS後已{st['bos_count']}次BOS"
    else:
        struct_reason = f"結構{'看空' if trend == 'bearish' else '看多'}(已出現反向MSS，尚未確認翻轉)"
    last_break = st["last_break"].get(trend)
    if last_break:
        struct_reason += f"，最近突破位{last_break:.2f}"

    # 只看EMA20/50相對排列(收盤價回測區域時本來就會在EMA20另一側，不能拿收盤價當條件)
    if cfg["require_ema"]:
        if trend == "bearish" and not (ef[i] < es[i]):
            return _neutral(current_price, struct_reason + "；但EMA20/50未呈空頭排列", extra)
        if trend == "bullish" and not (ef[i] > es[i]):
            return _neutral(current_price, struct_reason + "；但EMA20/50未呈多頭排列", extra)

    # 最近touch_window根內碰過同向、且目前仍有效的OB/FVG
    w = max(1, int(cfg["touch_window"]))
    hit = None
    for z in st["zones"]:
        if z["side"] != trend:
            continue
        for k in range(i, max(i - w, z["index"]), -1):
            ck = closed[k]
            if trend == "bearish" and z["bot"] <= ck["high"] and ck["close"] < z["top"]:
                hit = z; break
            if trend == "bullish" and z["top"] >= ck["low"] and ck["close"] > z["bot"]:
                hit = z; break
        if hit:
            break
    if hit is None:
        return {
            "stage": "中性",
            "direction": None,
            "chan": {"bias": trend, "strength": 1, "reason": struct_reason},
            "profile": {"bias": None, "strength": 0, "reason": "價格不在OB/FVG區域內"},
            "current_price": current_price,
            "smc": extra,
        }

    zone_reason = f"回測{hit['kind']}區 {hit['bot']:.2f}~{hit['top']:.2f}"
    level = cfg["wt_level"] * 0.6

    def _cross_at(k):
        if k < 2 or None in (wt1[k], wt2[k], wt1[k - 1], wt2[k - 1], wt1[k - 2]):
            return False
        if trend == "bearish":
            return wt1[k - 1] >= wt2[k - 1] and wt1[k] < wt2[k] and max(wt1[k - 1], wt1[k - 2]) > level
        return wt1[k - 1] <= wt2[k - 1] and wt1[k] > wt2[k] and min(wt1[k - 1], wt1[k - 2]) < -level

    crossed = False
    for k in range(i, max(i - w, 1), -1):
        if _cross_at(k):
            crossed = (wt1[i] < wt2[i]) if trend == "bearish" else (wt1[i] > wt2[i])
            break
    if trend == "bearish":
        sl = hit["top"] * (1 + cfg["sl_buffer_pct"])
        sl_points = max(sl - current_price, 0.0) if current_price else None
    else:
        sl = hit["bot"] * (1 - cfg["sl_buffer_pct"])
        sl_points = max(current_price - sl, 0.0) if current_price else None

    extra["hit_zone"] = hit
    extra["suggested_sl_points"] = sl_points

    if crossed:
        return {
            "stage": "訊號",
            "direction": trend,
            "chan": {"bias": trend, "strength": 2, "reason": struct_reason},
            "profile": {"bias": trend, "strength": 2,
                        "reason": zone_reason + f"，WaveTrend{'高檔死叉' if trend == 'bearish' else '低檔金叉'}(wt1={wt1[i]:.1f})"},
            "current_price": current_price,
            "smc": extra,
        }
    return {
        "stage": "關注",
        "direction": trend,
        "chan": {"bias": trend, "strength": 2, "reason": struct_reason},
        "profile": {"bias": trend, "strength": 1,
                    "reason": zone_reason + f"，等WaveTrend交叉(wt1={wt1[i]:.1f}, wt2={wt2[i]:.1f})"},
        "current_price": current_price,
        "smc": extra,
    }


def generate_signal_smc(candles, current_price=None, cfg=None):
    """
    即時路徑入口：輸入1小時K棒清單(最後一根視為「進行中」、不參與判斷)，回傳標準訊號格式。
    內部就是precompute()+evaluate_at(最後一根已收盤)，跟回測走同一套函式。
    """
    closed = candles[:-1] if len(candles) > 1 else []
    if current_price is None and candles:
        current_price = candles[-1]["close"]
    if len(closed) < SMC_MIN_CANDLES:
        return _neutral(current_price, f"1小時K資料不足({len(closed)}/{SMC_MIN_CANDLES}根)")
    pre = precompute(closed, cfg)
    result = evaluate_at(pre, len(closed) - 1, current_price=current_price)
    result["smc"]["events"] = pre["events"][-20:]
    return result
