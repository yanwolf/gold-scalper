"""
回拉轉強(strategy_type="pullback_resume"，1小時K，r99，只存在回測)

來源：使用者自己的 XAUUSD 1小時K 做法(2026/10/15 截圖)——
「下跌回拉之後，等上漲的那根1小時K收完馬上進場，吃下一根1小時K會不會延續上漲」。

規則(多單；空單整套反過來)：
  1. 趨勢向上：收盤在趨勢線之上、趨勢線往上走(跟 SLOPE_BARS 根前比)。
     趨勢線預設 EMA20(MT5 圖上那條藍線)，可改布林中軌(SMA20)，或不看趨勢。
  2. 回拉：前面連續至少 min_pullback_bars 根陰線(收<開)；可選回拉幅度至少 min_pullback_atr 倍 ATR
     (從回拉前 PRE_BARS 根的最高點，量到回拉段的最低點)。
  3. 轉強：這一根是陽線(收>開)，而且收盤收回前一根陰線的開盤之上。
  4. 進場：這根收盤(回測迴圈在1小時K收盤那一步用當下價格進場)。
  5. 出場：持有 hold_bars 根(預設 1)後，在那根收盤出場，賺賠都走。可選保護停損＝回拉段的最低點。

布林觸軌轉強(mode=2，r101)：不看趨勢；回拉段(連續反色K)裡至少一根的低點碰到布林下軌(BOLL 20,2；空單：高點碰上軌)，
接著同樣的轉強K收回前一根開盤才進場。停損一律放在回拉段的極值(這個模式不管 sl_mode，規則事先定死)。
出場可選 exit_mode：0=持有 hold_bars 根；1=回到布林中軌(訊號那根的中軌，停利價固定)，最長持有 MID_MAX_HOLD 根。
r104 加兩種(都是最長 MID_MAX_HOLD 根)：2=對面的布林軌(做多停利在訊號那根的上軌，空單在下軌)；
3=移動停損(浮盈到 TRAIL_ATR 倍 ATR 啟動，最差保本，之後回吐 TRAIL_ATR 倍 ATR 出場，沒有停利價)。
中軌不在進場價的獲利方向時，沒有停利價，只靠時間出場。

基準組(mode=1)：同樣的趨勢條件，「任何一根陽線收盤就買、持有一樣久」(空單：任何陰線就賣)。
拿來判斷賺的是「回拉＋轉強」這個型態，還是只是趨勢本身的漂移。要看純漂移，把趨勢設成「不看」再跑一次基準組。

全部因果：第 i 根的判斷只用 <= i 的已收盤1小時K。跟即時系統完全無關(沒有引擎、不是 settings 欄位)。
介面跟 mean_reversion.py 一樣(build_cfg / precompute / evaluate_at，結果放在 "mr" 鍵)，共用回測迴圈的1小時K路徑。
"""
from app.mean_reversion import _ema_series, _atr_series

TREND_PERIOD = 20
BB_PERIOD, BB_K = 20, 2.0
MID_MAX_HOLD = 12     # exit_mode=1/2/3 時最長持有幾根
TRAIL_ATR = 1.0       # exit_mode=3：浮盈幾倍 ATR 啟動移動停損、回吐幾倍 ATR 出場
SLOPE_BARS = 3
PRE_BARS = 5           # 回拉幅度：從回拉開始前幾根的最高點量起
MAX_PULLBACK_BARS = 12  # 往回數陰線最多數幾根
WARMUP_BARS = TREND_PERIOD + SLOPE_BARS + PRE_BARS

DEFAULT_CFG = {
    "sides": 3,               # 1=只做多 2=只做空 3=多空都做
    "trend": 0,               # 0=EMA20 1=布林中軌(SMA20) 2=不看趨勢
    "mode": 0,                # 0=回拉轉強型態 1=基準組(趨勢裡任何一根同色K) 2=布林觸軌轉強(r101)
    "exit_mode": 0,           # 0=持有 hold_bars 根 1=回到布林中軌 2=對面布林軌 3=移動停損(1~3 最長 MID_MAX_HOLD 根)
    "min_pullback_bars": 1,
    "min_pullback_atr": 0.0,  # 0=不限回拉幅度
    "sl_mode": 0,             # 0=不設停損(持有到時間) 1=停損在回拉段低點(空單：反彈段高點)
    "hold_bars": 1,
    # 給回測迴圈的固定值(跟 mean_reversion 共用欄位名)
    "trail_atr": 0.0,
}

CFG_LIMITS = {
    "sides": (1, 3, int),
    "trend": (0, 2, int),
    "mode": (0, 2, int),
    "exit_mode": (0, 3, int),
    "min_pullback_bars": (1, 6, int),
    "min_pullback_atr": (0.0, 5.0, float),
    "sl_mode": (0, 1, int),
    "hold_bars": (1, 6, int),
}

NO_STOP_POINTS = 1e6   # 不設停損：給一個碰不到的距離(持有時間到才出場)


def build_cfg(overrides=None):
    cfg = dict(DEFAULT_CFG)
    for k, v in (overrides or {}).items():
        if v is None or v == "":
            continue
        if k not in CFG_LIMITS:
            return None, f"回拉轉強沒有這個參數：{k}"
        lo, hi, typ = CFG_LIMITS[k]
        try:
            fv = float(v)
        except (TypeError, ValueError):
            return None, f"回拉轉強參數 {k} 要是數字，收到 {v}"
        if typ is int and fv != int(fv):
            return None, f"回拉轉強參數 {k} 要是整數，收到 {v}"
        val = int(fv) if typ is int else fv
        if not lo <= val <= hi:
            return None, f"回拉轉強參數 {k} 要在 {lo}～{hi} 之間，收到 {v}"
        cfg[k] = val
    # 回測迴圈用這個欄位做時間出場
    cfg["max_hold_hours"] = MID_MAX_HOLD if int(cfg["exit_mode"]) in (1, 2, 3) else int(cfg["hold_bars"])
    cfg["trail_atr"] = TRAIL_ATR if int(cfg["exit_mode"]) == 3 else 0.0   # 回測迴圈用這個欄位做移動停損
    return cfg, None


def _sma_series(vals, n):
    out = [None] * len(vals)
    for i in range(n - 1, len(vals)):
        out[i] = sum(vals[i - n + 1:i + 1]) / n
    return out


def _bollinger(closes, n=BB_PERIOD, k=BB_K):
    """布林通道(母體標準差，跟 MT5／TradingView 一樣)：(中軌, 上軌, 下軌)。"""
    mid, up, lo = [None] * len(closes), [None] * len(closes), [None] * len(closes)
    for i in range(n - 1, len(closes)):
        w = closes[i - n + 1:i + 1]
        m = sum(w) / n
        sd = (sum((x - m) ** 2 for x in w) / n) ** 0.5
        mid[i], up[i], lo[i] = m, m + k * sd, m - k * sd
    return mid, up, lo


def precompute(candles):
    closes = [c["close"] for c in candles]
    bb_mid, bb_up, bb_lo = _bollinger(closes)
    return {"candles": candles,
            "ema": _ema_series(closes, TREND_PERIOD),
            "sma": _sma_series(closes, TREND_PERIOD),
            "atr": _atr_series(candles),
            "bb_mid": bb_mid, "bb_up": bb_up, "bb_lo": bb_lo}


def _neutral(reason):
    return {"stage": "中性", "direction": None,
            "chan": {"reason": reason}, "profile": {"reason": ""}, "mr": None}


def _color(bar):
    if bar["close"] > bar["open"]:
        return 1
    if bar["close"] < bar["open"]:
        return -1
    return 0


def _trend_ok(pre, i, cfg, sign):
    t = int(cfg["trend"])
    if t == 2:
        return True, "不看趨勢"
    line = pre["ema"] if t == 0 else pre["sma"]
    cur, prev = line[i], line[i - SLOPE_BARS]
    if cur is None or prev is None:
        return False, "趨勢線還沒算出來"
    name = "EMA20" if t == 0 else "布林中軌"
    close = pre["candles"][i]["close"]
    if sign > 0:
        return (close > cur and cur > prev), f"收盤{'在' if close > cur else '不在'}{name}之上、{name}{'上彎' if cur > prev else '沒有上彎'}"
    return (close < cur and cur < prev), f"收盤{'在' if close < cur else '不在'}{name}之下、{name}{'下彎' if cur < prev else '沒有下彎'}"


def _check_side(pre, i, cfg, sign):
    """sign=+1 多單、-1 空單。回傳 (命中?, 理由, 停損參考價)。"""
    c = pre["candles"]
    bar = c[i]
    if _color(bar) != sign:
        return False, "這根不是" + ("陽線" if sign > 0 else "陰線"), None
    band_mode = int(cfg["mode"]) == 2
    if band_mode:
        why = "不看趨勢(布林觸軌)"
    else:
        ok, why = _trend_ok(pre, i, cfg, sign)
        if not ok:
            return False, why, None
    if int(cfg["mode"]) == 1:   # 基準組：趨勢裡任何一根同色K
        ref = bar["low"] if sign > 0 else bar["high"]
        return True, f"基準組：{why}，任何{'陽' if sign > 0 else '陰'}線收盤進場", ref

    # 回拉段：往回數連續的反色K
    k = 0
    while k < MAX_PULLBACK_BARS and i - 1 - k >= 0 and _color(c[i - 1 - k]) == -sign:
        k += 1
    if k < int(cfg["min_pullback_bars"]):
        return False, f"前面沒有連續 {int(cfg['min_pullback_bars'])} 根{'陰' if sign > 0 else '陽'}線回拉", None
    last_pb = c[i - 1]
    if not (sign * (bar["close"] - last_pb["open"]) > 0):
        return False, "沒有收回前一根的開盤", None
    pb = c[i - k:i]
    if band_mode:
        band = pre["bb_lo"] if sign > 0 else pre["bb_up"]
        touched = any(band[j] is not None and sign * (c[j]["low" if sign > 0 else "high"] - band[j]) <= 0
                      for j in range(i - k, i))
        if not touched:
            return False, f"回拉段沒有碰到布林{'下' if sign > 0 else '上'}軌", None
        why = f"碰布林{'下' if sign > 0 else '上'}軌後轉{'強' if sign > 0 else '弱'}"
    pre_seg = c[max(0, i - k - PRE_BARS):i - k]
    atr = pre["atr"][i]
    if sign > 0:
        extreme = min(b["low"] for b in pb)
        start = max([b["high"] for b in pre_seg] + [pb[0]["high"]])
        depth = start - extreme
    else:
        extreme = max(b["high"] for b in pb)
        start = min([b["low"] for b in pre_seg] + [pb[0]["low"]])
        depth = extreme - start
    need = float(cfg["min_pullback_atr"])
    if need > 0 and (not atr or depth < need * atr):
        return False, f"回拉幅度 {depth:.2f} 不到 {need:g} 倍ATR", None
    return True, (f"{'回拉轉強做多' if sign > 0 else '反彈轉弱放空'}：{why}，{k} 根{'陰' if sign > 0 else '陽'}線"
                  f"{'回拉' if sign > 0 else '反彈'} {depth:.2f} 點後收回前一根開盤"), extreme


def evaluate_at(pre, i, cfg, current_price=None):
    c = pre["candles"]
    if i < WARMUP_BARS or i >= len(c):
        return _neutral(f"資料不足(1小時K要至少{WARMUP_BARS + 1}根)")
    atr = pre["atr"][i]
    sides = int(cfg["sides"])
    hit, reasons = None, []
    for sign in (1, -1):
        if (sign > 0 and sides == 2) or (sign < 0 and sides == 1):
            continue
        ok, why, ref = _check_side(pre, i, cfg, sign)
        if ok:
            hit = (sign, why, ref)
            break
        reasons.append(why)
    if hit is None:
        return _neutral("；".join(reasons) or "這個方向不做")
    sign, why, ref = hit
    entry = float(current_price) if current_price is not None else c[i]["close"]
    band_mode = int(cfg["mode"]) == 2
    if (int(cfg["sl_mode"]) == 1 or band_mode) and ref is not None and sign * (entry - ref) > 0:
        sl_points, sl_note = sign * (entry - ref), f"停損在{'回拉低點' if sign > 0 else '反彈高點'} {ref:.2f}"
    else:
        sl_points, sl_note = NO_STOP_POINTS, "不設停損"
    setup = {0: "型態", 1: "基準", 2: "觸軌"}[int(cfg["mode"])] + ("多" if sign > 0 else "空")
    tp_price, exit_note = None, f"持有 {int(cfg['hold_bars'])} 根1小時K後收盤出場"
    if int(cfg["exit_mode"]) == 1:
        mid = pre["bb_mid"][i]
        if mid is not None and sign * (mid - entry) > 0:
            tp_price, exit_note = mid, f"回到布林中軌 {mid:.2f} 停利，最長 {MID_MAX_HOLD} 根"
        else:
            exit_note = f"中軌不在獲利方向，沒有停利價，最長 {MID_MAX_HOLD} 根"
    elif int(cfg["exit_mode"]) == 2:
        far = (pre["bb_up"] if sign > 0 else pre["bb_lo"])[i]
        if far is not None and sign * (far - entry) > 0:
            tp_price, exit_note = far, f"碰到對面布林{'上' if sign > 0 else '下'}軌 {far:.2f} 停利，最長 {MID_MAX_HOLD} 根"
        else:
            exit_note = f"對面布林軌不在獲利方向，沒有停利價，最長 {MID_MAX_HOLD} 根"
    elif int(cfg["exit_mode"]) == 3:
        exit_note = f"浮盈 {TRAIL_ATR:g} 倍ATR 後啟動移動停損(最差保本)、回吐 {TRAIL_ATR:g} 倍ATR 出場，最長 {MID_MAX_HOLD} 根"
    return {
        "stage": "訊號", "direction": "bullish" if sign > 0 else "bearish",
        "chan": {"reason": why},
        "profile": {"reason": f"{exit_note}；{sl_note}"},
        "mr": {"setup": setup, "sl_points": sl_points, "tp_price": tp_price, "atr": atr},
    }
