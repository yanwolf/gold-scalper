"""
日線 SMC 結構參考 (r89) —— 只當「大方向參考」，不參與任何進出場判斷。

用途(使用者 2026-09-28 決定)：小時級 SMC 當進場訊號回測不好看，但想把日線的市場結構
(CHoCH/BOS、OB/FVG)拿來當趨勢參考。所以這個模組只做四件事：
  1. 抓幣安期貨日K(公開資料)，只用「已收盤」的日K判斷結構(最後一根未收盤的丟掉，清單第 10 條)
  2. 給 dashboard 顯示：目前結構方向、最近一次 CHoCH/BOS、上下最近的 OB/FVG 區
  3. 開倉時把當下的日線狀態標記在交易紀錄上(tag_for_trade)，之後分組統計「順／逆日線結構」的績效
  4. 給回測用的逐根因果序列(bias_series)：回測可以比較加不加日線濾網的差異(只存在回測，不是正式設定)

結構判斷直接重用 smc_structure.analyze_structure()(純函式、純因果)，這裡只換成日K、另外定義方向：
  - 方向(bias) ＝ 最近一次結構突破的方向(跟 LuxAlgo 一樣：CHoCH 一出現方向就翻)
  - CHoCH ＝ 方向改變後的第一次突破；之後同方向每突破一次算一次 BOS
  - 「已確認」＝ CHoCH 之後至少有一次同方向 BOS
  不用 analyze_structure 回傳的 trend/pending——那是 1 小時引擎的進場規則(要 N 次 BOS 才算趨勢)，
  而且 pending 在「假 MSS 後又往回突破」時不會跟著改，拿來當方向會錯。

即時路徑的原則：交易迴圈只讀記憶體快取(get_snapshot / tag_for_trade)，絕不在交易迴圈裡打網路；
快取由背景執行緒(start_refresher)每 30 分鐘刷新一次。抓不到時沿用上一次、標「過期」，不會拋例外。
日K的日期一律用 UTC(清單第 11 條)：幣安日K 00:00 UTC 開盤，台灣時間早上 8 點換日。
"""

import logging
import os
import threading
import time
from datetime import datetime, timezone

from app import smc_structure

logger = logging.getLogger("daily_smc")

DAILY_INTERVAL_SECONDS = 86400
DAILY_HISTORY_LIMIT = 500          # 即時快取抓幾根日K(約 1 年 4 個月；幣安單次上限 1500)
DAILY_MIN_CANDLES = 30             # 少於這個數量不判斷
REFRESH_SECONDS = 1800             # 背景刷新間隔(日K一天才收一根，30 分鐘足夠)
RETRY_SECONDS = 300                # 抓失敗時多久後重試
STALE_AFTER_SECONDS = 30 * 3600    # 最後一根已收盤日K的收盤時間超過這麼久 → 標過期(正常最多 24 小時多一點)
BACKTEST_WARMUP_DAYS = 400         # 回測時多抓回測視窗之前的日K當 warmup
BACKTEST_CACHE_SECONDS = 600       # 參數掃描十幾組回測共用同一份日K

# 日線用的結構參數：swing 左右各 N 根。TradingView LuxAlgo 的波段結構長度比較長，
# 標出來的位置不會完全一樣——可用環境變數調(DAILY_SMC_SWING_N)，對照圖上的 CHoCH/BOS 位置
DAILY_CFG = {
    "swing_n": max(1, int(os.getenv("DAILY_SMC_SWING_N", "3") or 3)),
    "confirm_bos": 1,
    "zone_max_age": max(10, int(os.getenv("DAILY_SMC_ZONE_MAX_AGE", "120") or 120)),
}

# r112：大級別流動性(還沒被掃過的波段高低點)。用比結構判斷更長的 swing，抓的是「圖上一眼看得到」的那種高低點
MAJOR_SWING_N = max(3, int(os.getenv("DAILY_SMC_MAJOR_SWING_N", "10") or 10))
EQUAL_LEVEL_PCT = 0.3        # 兩個波段低點(高點)相差在 0.3% 以內 → 視為等低點(等高點)，停損單堆在同一區
LEVELS_SHOWN = 3
# XAUUSDT 合約歷史只有一年左右；更早的結構(例如跌破後「下一個」在哪)改用 PAXGUSDT 現貨長歷史補，
# 一天抓一次就夠(舊歷史不會變)。現貨K線只能走 batch(binance_weight 規定)，這裡是背景執行緒，可以排隊等
LONG_HISTORY_SYMBOL = "PAXGUSDT"
LONG_HISTORY_LIMIT = 1000
LONG_HISTORY_REFRESH_SECONDS = 86400

_lock = threading.Lock()
_state = {"snapshot": None, "refreshed_at": None, "last_attempt": None, "last_error": None,
          "long_candles": None, "long_fetched_at": None, "long_error": None}
_refresher_started = False
_bt_cache = {}


# ---------------------------------------------------------------------------
# 資料
# ---------------------------------------------------------------------------
def klines_to_closed_candles(raw, now_ms=None):
    """幣安K線原始格式 → 專案K棒格式(多帶 close_time)，只留已收盤的(close_time < now)。時間遞增。"""
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    out = []
    for k in raw or []:
        try:
            c = {"bucket_start": int(k[0]), "open": float(k[1]), "high": float(k[2]), "low": float(k[3]),
                 "close": float(k[4]), "volume": float(k[5]), "close_time": int(k[6])}
        except (TypeError, ValueError, IndexError):
            continue
        if c["close_time"] < now_ms:
            out.append(c)
    out.sort(key=lambda c: c["bucket_start"])
    return out


def fetch_daily_klines(symbol="XAUUSDT", limit=DAILY_HISTORY_LIMIT):
    """抓幣安期貨日K(公開資料，不用 API key)。回傳原始格式，失敗拋例外(呼叫端處理)。"""
    from app import binance_weight   # r98：即時路徑走 live(不排隊、不等；冷卻/封鎖中這輪不抓，沿用上次快照)
    data = binance_weight.get_klines({"symbol": symbol, "interval": "1d", "limit": int(limit)},
                                     mode="live", source="klines-daily")
    if not isinstance(data, list):
        raise ValueError(f"日K回傳格式不對: {str(data)[:120]}")
    return data


def _utc_date(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# 結構判斷(純函式)
# ---------------------------------------------------------------------------
def _tail_streak(events):
    """最近一段同方向的突破有幾次、從哪一個開始。回傳(bias, streak, 第一個事件)。"""
    if not events:
        return None, 0, None
    bias = events[-1]["direction"]
    k = len(events) - 1
    while k > 0 and events[k - 1]["direction"] == bias:
        k -= 1
    return bias, len(events) - k, events[k]


def _event_out(e, candles):
    c = candles[e["index"]] if 0 <= e["index"] < len(candles) else None
    return {"direction": e["direction"], "price": round(float(e["price"]), 2),
            "date": _utc_date(c["bucket_start"]) if c else None}


def summarize(candles, cfg=None):
    """
    已收盤日K → 目前的日線結構摘要。純函式，不打網路、不看時間。
    回傳 {bias, confirmed, bos_after_choch, choch, last_break, recent_events, zones,
          break_up_level, break_down_level, as_of, last_close, last_close_time, candle_count, cfg}
    """
    cfg = {**DAILY_CFG, **(cfg or {})}
    base = {"bias": None, "confirmed": False, "bos_after_choch": 0, "choch": None, "last_break": None,
            "recent_events": [], "zones": [], "break_up_level": None, "break_down_level": None,
            "as_of": _utc_date(candles[-1]["bucket_start"]) if candles else None,
            "last_close": round(candles[-1]["close"], 2) if candles else None,
            "last_close_time": candles[-1].get("close_time") if candles else None,
            "candle_count": len(candles or []), "cfg": cfg}
    if not candles or len(candles) < DAILY_MIN_CANDLES:
        return {**base, "reason": f"已收盤日K不足({len(candles or [])}根，至少要{DAILY_MIN_CANDLES}根)"}
    st = smc_structure.analyze_structure(candles, cfg)
    events = st.get("events") or []
    bias, streak, first = _tail_streak(events)
    recent = []
    if events:
        # 重新標名稱：方向改變後的第一次＝CHoCH，同方向接著的＝BOS(不沿用1小時引擎的MSS/BOS標籤)
        prev_dir = None
        for e in events:
            label = "CHoCH" if e["direction"] != prev_dir else "BOS"
            prev_dir = e["direction"]
            recent.append({"type": label, **_event_out(e, candles)})
        recent = recent[-6:]
    zones = [{"kind": z["kind"], "side": z["side"], "top": round(z["top"], 2), "bot": round(z["bot"], 2),
              "date": _utc_date(candles[z["index"]]["bucket_start"]) if 0 <= z["index"] < len(candles) else None}
             for z in (st.get("zones") or [])]
    return {
        **base,
        "bias": bias,
        "confirmed": streak >= 2,
        "bos_after_choch": max(0, streak - 1),
        "choch": _event_out(first, candles) if first else None,
        "last_break": {"type": recent[-1]["type"], **_event_out(events[-1], candles)} if events else None,
        "recent_events": recent,
        "zones": zones,
        # 下一次結構突破的參考價：收盤站上 break_up_level＝往上突破、跌破 break_down_level＝往下突破
        "break_up_level": round(st["last_hh"], 2) if st.get("last_hh") is not None else None,
        "break_down_level": round(st["last_ll"], 2) if st.get("last_ll") is not None else None,
        "reason": None if bias else "還沒有出現結構突破",
    }


def bias_series(candles, cfg=None):
    """
    回測用：每一根已收盤日K收盤當下的(方向, 是否已確認)，純因果(第 i 根只用到 <=i 的資料)。
    回傳跟 candles 等長的清單 [{"bias","confirmed"}, ...]。
    """
    cfg = {**DAILY_CFG, **(cfg or {})}
    n = len(candles or [])
    if n < DAILY_MIN_CANDLES:
        return [{"bias": None, "confirmed": False} for _ in range(n)]
    _snaps, events = smc_structure.analyze_structure(candles, cfg, snapshots=True)
    out, ptr, bias, streak = [], 0, None, 0
    for i in range(n):
        while ptr < len(events) and events[ptr]["index"] <= i:
            d = events[ptr]["direction"]
            streak = streak + 1 if d == bias else 1
            bias = d
            ptr += 1
        # 前 DAILY_MIN_CANDLES 根當 warmup，跟即時路徑「日K不足不判斷」一致
        out.append({"bias": bias if i + 1 >= DAILY_MIN_CANDLES else None,
                    "confirmed": streak >= 2 if i + 1 >= DAILY_MIN_CANDLES else False})
    return out


def locate_zones(zones, price):
    """把區域依現價分成上方(壓力)、下方(支撐)、現價所在，各自依距離排序。"""
    above, below, inside = [], [], []
    if price is None:
        return above, below, inside
    for z in zones or []:
        if z["bot"] <= price <= z["top"]:
            inside.append(z)
        elif z["bot"] > price:
            above.append(z)
        else:
            below.append(z)
    above.sort(key=lambda z: z["bot"] - price)
    below.sort(key=lambda z: price - z["top"])
    return above[:3], below[:3], inside


def _untaken_swings(candles, n, source):
    """
    r112：還沒被掃過的波段高低點。swing low 之後只要有任何一根日K的最低價跌破它，就算被掃過(流動性已經被拿走)；
    swing high 同理看最高價。純函式。回傳 [{kind:"low"/"high", price, index, date, source}, ...]。
    """
    out = []
    if not candles:
        return out
    m = len(candles)
    # 從後往前算「之後的最低/最高」，整段只掃一次
    after_low, after_high = [None] * m, [None] * m
    lo = hi = None
    for i in range(m - 1, -1, -1):
        after_low[i], after_high[i] = lo, hi
        lo = candles[i]["low"] if lo is None else min(lo, candles[i]["low"])
        hi = candles[i]["high"] if hi is None else max(hi, candles[i]["high"])
    for _confirm, kind, price, p in smc_structure._find_swings(candles, n):
        if kind == "L" and (after_low[p] is None or after_low[p] >= price):
            out.append({"kind": "low", "price": round(price, 2), "index": p,
                        "date": _utc_date(candles[p]["bucket_start"]), "source": source})
        elif kind == "H" and (after_high[p] is None or after_high[p] <= price):
            out.append({"kind": "high", "price": round(price, 2), "index": p,
                        "date": _utc_date(candles[p]["bucket_start"]), "source": source})
    return out


def _merge_equal(levels, pct=EQUAL_LEVEL_PCT):
    """價位相差 pct% 以內的同種高低點合成一個(取最極端的那個價，記有幾個)。levels 依價格排序後合併。"""
    out = []
    for lv in sorted(levels, key=lambda x: x["price"]):
        last = out[-1] if out else None
        if last and last["kind"] == lv["kind"] and abs(lv["price"] - last["price"]) <= last["price"] * pct / 100:
            last["equal_count"] += 1
            last["dates"].append(lv["date"])
            if lv["kind"] == "low" and lv["price"] < last["price"]:
                last["price"], last["date"], last["source"] = lv["price"], lv["date"], lv["source"]
            if lv["kind"] == "high" and lv["price"] > last["price"]:
                last["price"], last["date"], last["source"] = lv["price"], lv["date"], lv["source"]
        else:
            out.append({**{k: lv[k] for k in ("kind", "price", "date", "source")},
                        "equal_count": 1, "dates": [lv["date"]]})
    return out


def liquidity_levels(candles, ref_price, long_candles=None, major_n=None):
    """
    r112：大級別流動性與防守點(純參考)。
      - below：現價下方還沒被掃過的大級別波段低點(賣方流動性，多單停損堆在下面)，近→遠
      - above：現價上方還沒被掃過的大級別波段高點(買方流動性)，近→遠
      - 相差 0.3% 以內的合成一個並標 equal_count(等低點／等高點)
      - long_candles(PAXGUSDT 現貨長歷史)只補「XAUUSDT 第一根日K之前」的波段點；之後一律以 XAUUSDT 為準。
        判斷有沒有被掃過時，PAXG 的點要同時看 PAXG 之後的價格與 XAUUSDT 全部的價格
      - void_below：最近的下方防守點被收盤跌破後，到下一個點之間的距離(這段沒有大級別結構，跌破常走得快)
    回傳 {below, above, void_below, major_swing_n, xau_first_date, long_used}。
    """
    n = major_n or MAJOR_SWING_N
    base = {"below": [], "above": [], "void_below": None, "major_swing_n": n,
            "xau_first_date": _utc_date(candles[0]["bucket_start"]) if candles else None, "long_used": False}
    if not candles or ref_price is None:
        return base
    levels = _untaken_swings(candles, n, "XAUUSDT")
    if long_candles:
        first_ms = candles[0]["bucket_start"]
        older = [c for c in long_candles if c["bucket_start"] < first_ms]
        if len(older) > 2 * n:
            xau_low = min(c["low"] for c in candles)
            xau_high = max(c["high"] for c in candles)
            for lv in _untaken_swings(older, n, LONG_HISTORY_SYMBOL):
                if lv["kind"] == "low" and xau_low >= lv["price"]:
                    levels.append(lv)
                elif lv["kind"] == "high" and xau_high <= lv["price"]:
                    levels.append(lv)
            base["long_used"] = True
    merged = _merge_equal(levels)
    below = sorted([lv for lv in merged if lv["kind"] == "low" and lv["price"] < ref_price],
                   key=lambda x: ref_price - x["price"])
    above = sorted([lv for lv in merged if lv["kind"] == "high" and lv["price"] > ref_price],
                   key=lambda x: x["price"] - ref_price)
    void = None
    if below:
        nxt = below[1] if len(below) > 1 else None
        void = {"from": below[0]["price"], "to": nxt["price"] if nxt else None,
                "points": round(below[0]["price"] - nxt["price"], 2) if nxt else None,
                "to_source": nxt["source"] if nxt else None}
    return {**base, "below": below[:LEVELS_SHOWN], "above": above[:LEVELS_SHOWN], "void_below": void}


# ---------------------------------------------------------------------------
# 即時快取(交易迴圈只讀這裡)
# ---------------------------------------------------------------------------
def refresh(symbol="XAUUSDT", fetcher=None):
    """抓一次日K並更新快取。不拋例外；失敗時保留上一次的快照、記下錯誤。回傳目前快照(可能是 None)。"""
    now = time.time()
    with _lock:
        _state["last_attempt"] = now
    try:
        raw = (fetcher or fetch_daily_klines)(symbol=symbol, limit=DAILY_HISTORY_LIMIT)
        candles = klines_to_closed_candles(raw, now_ms=int(now * 1000))
        snap = summarize(candles)
        snap["symbol"] = symbol
        # r112：大級別流動性要用到整段日K(依現價分上下，在 get_snapshot 算)；只留高低價與時間，不放進 API 回傳
        snap["_levels_candles"] = [{"bucket_start": c["bucket_start"], "high": c["high"], "low": c["low"]} for c in candles]
        with _lock:
            _state["snapshot"] = snap
            _state["refreshed_at"] = now
            _state["last_error"] = None
        return snap
    except Exception as e:
        logger.warning(f"日線SMC刷新失敗，沿用上一次的結果: {type(e).__name__}: {e}")
        with _lock:
            _state["last_error"] = f"{type(e).__name__}: {e}"
            return _state["snapshot"]


def _fetch_long_history():
    """PAXGUSDT 現貨日K(長歷史)。現貨只能走 batch：會排隊、讓回測先跑，所以只在背景執行緒呼叫。"""
    from app import binance_weight
    data = binance_weight.get_klines({"symbol": LONG_HISTORY_SYMBOL, "interval": "1d", "limit": LONG_HISTORY_LIMIT},
                                     mode="batch", source="klines-daily-long", market="spot")
    if not isinstance(data, list):
        raise ValueError(f"長歷史日K回傳格式不對: {str(data)[:120]}")
    return data


def refresh_long_history(fetcher=None, now=None):
    """一天最多抓一次 PAXGUSDT 長歷史。不拋例外；失敗保留上一次、記錯誤(只影響「更早的結構點」顯示)。"""
    now = time.time() if now is None else now
    with _lock:
        fetched = _state["long_fetched_at"]
        if fetched and now - fetched < LONG_HISTORY_REFRESH_SECONDS and _state["long_candles"]:
            return _state["long_candles"]
    try:
        raw = (fetcher or _fetch_long_history)()
        candles = [{"bucket_start": c["bucket_start"], "high": c["high"], "low": c["low"]}
                   for c in klines_to_closed_candles(raw, now_ms=int(now * 1000))]
        with _lock:
            _state["long_candles"] = candles
            _state["long_fetched_at"] = now
            _state["long_error"] = None
        return candles
    except Exception as e:
        logger.warning(f"PAXGUSDT 長歷史日K抓取失敗(只影響更早的結構點): {type(e).__name__}: {e}")
        with _lock:
            _state["long_error"] = f"{type(e).__name__}: {e}"
            _state["long_fetched_at"] = now   # 失敗也等一輪(避免每 30 分鐘一直打)；之前抓到的保留
            return _state["long_candles"]


def _is_stale(snap, now=None):
    if not snap or not snap.get("last_close_time"):
        return True
    now = time.time() if now is None else now
    return (now * 1000 - snap["last_close_time"]) > STALE_AFTER_SECONDS * 1000


def get_snapshot(current_price=None, now=None):
    """給網頁/API：快取的日線結構 + 依現價分好的上下區域 + 盤中提示。不打網路。"""
    with _lock:
        snap = dict(_state["snapshot"]) if _state["snapshot"] else None
        refreshed_at, last_error = _state["refreshed_at"], _state["last_error"]
    meta = {"refreshed_at": (datetime.fromtimestamp(refreshed_at, tz=timezone.utc).isoformat() if refreshed_at else None),
            "last_error": last_error, "reference_only": True}
    if not snap:
        return {"available": False, "bias": None, "stale": True,
                "reason": "日線資料還沒抓到" + (f"（{last_error}）" if last_error else ""), **meta}
    ref = current_price if current_price is not None else snap.get("last_close")
    above, below, inside = locate_zones(snap.get("zones"), ref)
    with _lock:
        long_candles, long_error = _state["long_candles"], _state["long_error"]
    lv_candles = snap.pop("_levels_candles", None)
    try:
        liquidity = liquidity_levels(lv_candles, ref, long_candles=long_candles)
    except Exception as e:   # pragma: no cover - 防禦：純參考的顯示不能讓整張卡片出錯
        logger.warning(f"大級別流動性計算失敗: {e}")
        liquidity = None
    if liquidity is not None:
        liquidity["long_error"] = long_error
    note = None
    if current_price is not None and snap.get("bias"):
        # 只用已收盤日K判斷；今天盤中已經越過突破價時提示「待收盤確認」，不改方向
        if snap.get("break_down_level") is not None and current_price < snap["break_down_level"]:
            note = f"盤中已跌破 {snap['break_down_level']:.2f}（今天日K收盤才算數）"
        elif snap.get("break_up_level") is not None and current_price > snap["break_up_level"]:
            note = f"盤中已站上 {snap['break_up_level']:.2f}（今天日K收盤才算數）"
    return {**snap, "available": True, "stale": _is_stale(snap, now), "zones_above": above, "zones_below": below,
            "zones_inside": inside, "reference_price": ref, "intraday_note": note, "liquidity": liquidity, **meta}


def tag_for_trade(now=None):
    """
    開倉時記在交易紀錄上的精簡標記(只讀快取，不拋例外)。
    {bias, confirmed, as_of, stale} —— bias 為 None 或 stale 為 True 時，統計會歸到「未知」。
    """
    try:
        with _lock:
            snap = _state["snapshot"]
        if not snap:
            return {"bias": None, "confirmed": False, "as_of": None, "stale": True}
        return {"bias": snap.get("bias"), "confirmed": bool(snap.get("confirmed")), "as_of": snap.get("as_of"),
                "stale": _is_stale(snap, now)}
    except Exception as e:   # pragma: no cover - 防禦：標記失敗不能擋開倉
        logger.warning(f"日線SMC標記失敗: {e}")
        return {"bias": None, "confirmed": False, "as_of": None, "stale": True}


def _refresh_loop(symbol):
    while True:
        refresh_long_history()   # r112：一天最多抓一次；失敗不影響下面的日線結構
        snap = refresh(symbol)
        with _lock:
            ok = _state["last_error"] is None
        time.sleep(REFRESH_SECONDS if ok and snap else RETRY_SECONDS)


def start_refresher(symbol="XAUUSDT"):
    """啟動背景刷新(重複呼叫只會啟動一次)。"""
    global _refresher_started
    with _lock:
        if _refresher_started:
            return False
        _refresher_started = True
    threading.Thread(target=_refresh_loop, args=(symbol,), name="daily-smc-refresh", daemon=True).start()
    return True


# ---------------------------------------------------------------------------
# 回測用
# ---------------------------------------------------------------------------
def backtest_series(symbol, days, fetcher, now_ms=None, end_key=None):
    """
    回測用：抓「回測視窗 + warmup」的日K，回傳(已收盤日K收盤時間清單, 每根的方向序列, 日期清單)。
    fetcher(symbol, days) 回傳幣安原始日K。同一組(symbol, days)10 分鐘內共用快取(參數掃描用)。
    抓不到時拋例外，由呼叫端決定怎麼處理。
    """
    key = (symbol, int(days), end_key)   # r90：回測結束時間不同就是不同的資料
    now = time.time()
    hit = _bt_cache.get(key)
    if hit and now - hit[0] < BACKTEST_CACHE_SECONDS:
        return hit[1]
    raw = fetcher(symbol, int(days) + BACKTEST_WARMUP_DAYS)
    candles = klines_to_closed_candles(raw, now_ms=now_ms)
    series = bias_series(candles)
    res = ([c["close_time"] for c in candles], series, [_utc_date(c["bucket_start"]) for c in candles])
    _bt_cache[key] = (now, res)
    return res
