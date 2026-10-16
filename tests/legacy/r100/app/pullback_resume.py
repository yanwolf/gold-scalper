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

基準組(mode=1)：同樣的趨勢條件，「任何一根陽線收盤就買、持有一樣久」(空單：任何陰線就賣)。
拿來判斷賺的是「回拉＋轉強」這個型態，還是只是趨勢本身的漂移。要看純漂移，把趨勢設成「不看」再跑一次基準組。

全部因果：第 i 根的判斷只用 <= i 的已收盤1小時K。跟即時系統完全無關(沒有引擎、不是 settings 欄位)。
介面跟 mean_reversion.py 一樣(build_cfg / precompute / evaluate_at，結果放在 "mr" 鍵)，共用回測迴圈的1小時K路徑。
"""
from app.mean_reversion import _ema_series, _atr_series

TREND_PERIOD = 20
SLOPE_BARS = 3
PRE_BARS = 5           # 回拉幅度：從回拉開始前幾根的最高點量起
MAX_PULLBACK_BARS = 12  # 往回數陰線最多數幾根
WARMUP_BARS = TREND_PERIOD + SLOPE_BARS + PRE_BARS

DEFAULT_CFG = {
    "sides": 3,               # 1=只做多 2=只做空 3=多空都做
    "trend": 0,               # 0=EMA20 1=布林中軌(SMA20) 2=不看趨勢
    "mode": 0,                # 0=回拉轉強型態 1=基準組(趨勢裡任何一根同色K)
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
    "mode": (0, 1, int),
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
    cfg["max_hold_hours"] = int(cfg["hold_bars"])   # 回測迴圈用這個欄位做時間出場
    return cfg, None


def _sma_series(vals, n):
    out = [None] * len(vals)
    for i in range(n - 1, len(vals)):
        out[i] = sum(vals[i - n + 1:i + 1]) / n
    return out


def precompute(candles):
    closes = [c["close"] for c in candles]
    return {"candles": candles,
            "ema": _ema_series(closes, TREND_PERIOD),
            "sma": _sma_series(closes, TREND_PERIOD),
            "atr": _atr_series(candles)}


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
    if int(cfg["sl_mode"]) == 1 and ref is not None and sign * (entry - ref) > 0:
        sl_points, sl_note = sign * (entry - ref), f"停損在{'回拉低點' if sign > 0 else '反彈高點'} {ref:.2f}"
    else:
        sl_points, sl_note = NO_STOP_POINTS, "不設停損"
    setup = ("基準" if int(cfg["mode"]) == 1 else "型態") + ("多" if sign > 0 else "空")
    return {
        "stage": "訊號", "direction": "bullish" if sign > 0 else "bearish",
        "chan": {"reason": why},
        "profile": {"reason": f"持有 {int(cfg['hold_bars'])} 根1小時K後收盤出場；{sl_note}"},
        "mr": {"setup": setup, "sl_points": sl_points, "tp_price": None, "atr": atr},
    }
