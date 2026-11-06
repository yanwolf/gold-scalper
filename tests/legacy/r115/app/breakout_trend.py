"""
大週期突破(strategy_type="breakout_trend"，4小時K，r105，只存在回測)

背景(r103／r104)：幣安 XAUUSDT 每筆真實成本約 3.4 點(手續費一進一出＋價差)。15 分K 纏論每筆帳面約 0.9 點、
1 小時K 布林觸軌約 2 點，扣完都是負的。能扣得起成本的策略，平均每筆要賺遠大於 3.4 點——只做大行情、拉長週期。

規則(多單；空單整套反過來)，跑之前定死，參數都可調但預設值就是要驗的那一組：
  1. 突破：4小時K收盤高於前 channel 根(預設 20 根，約 3～4 個交易日)的最高價(唐奇安通道突破)。
  2. 大行情：突破那根K的真實波幅至少 big_bar_atr 倍 ATR(預設 1.0；ATR 用前一根的 14 期 ATR，不讓這根自己灌大)。
  3. 趨勢：收盤在 4 小時 EMA50 之上(trend=1；0＝不看)。
  4. 停損：進場價 ∓ sl_atr × ATR(預設 2 倍 4 小時 ATR)。
  5. 出場：浮盈到 trail_trigger_atr 倍 ATR(預設 2)後啟動移動停損，回吐 trail_atr 倍 ATR(預設 3)出場、最差保本；
     沒有固定停利(大行情要讓它跑)；max_hold_days 天到就出場(預設 0＝不限)。反向訊號不提前出場。

4 小時K 用 1 小時K 依 UTC 4 小時對齊組出來(00/04/08/…)。只在「這根 1 小時K 是某根 4 小時K 的最後一小時」時判斷那根 4 小時K，
所以第 i 根 1 小時K 的判斷只用到 <= i 的資料(因果)。某個 4 小時區間的最後一小時剛好沒有資料(休市)，那根就不判斷。
介面跟 mean_reversion.py／pullback_resume.py 一樣(build_cfg / precompute / evaluate_at，結果放在 "mr")，共用回測迴圈的1小時K路徑。
另外每次都回傳 hr_atr(最近一根已收盤 4 小時K 的 ATR)，回測迴圈用它算移動停損距離(不是 1 小時 ATR)。
跟即時系統完全無關：沒有引擎、不是 settings 欄位。
"""
from app.mean_reversion import _ema_series, _atr_series

TF_HOURS = 4
HOUR_MS = 3_600_000
TF_MS = TF_HOURS * HOUR_MS
EMA_PERIOD = 50

DEFAULT_CFG = {
    "channel": 20,
    "big_bar_atr": 1.0,
    "trend": 1,
    "sides": 3,
    "sl_atr": 2.0,
    "trail_trigger_atr": 2.0,
    "trail_atr": 3.0,
    "max_hold_days": 0,
}

CFG_LIMITS = {
    "channel": (5, 120, int),
    "big_bar_atr": (0.0, 5.0, float),
    "trend": (0, 1, int),
    "sides": (1, 3, int),
    "sl_atr": (0.5, 10.0, float),
    "trail_trigger_atr": (0.0, 20.0, float),
    "trail_atr": (0.5, 20.0, float),
    "max_hold_days": (0, 60, int),
}


def build_cfg(overrides=None):
    cfg = dict(DEFAULT_CFG)
    for k, v in (overrides or {}).items():
        if v is None or v == "":
            continue
        if k not in CFG_LIMITS:
            return None, f"大週期突破沒有這個參數：{k}"
        lo, hi, typ = CFG_LIMITS[k]
        try:
            fv = float(v)
        except (TypeError, ValueError):
            return None, f"大週期突破參數 {k} 要是數字，收到 {v}"
        if typ is int and fv != int(fv):
            return None, f"大週期突破參數 {k} 要是整數，收到 {v}"
        val = int(fv) if typ is int else fv
        if not lo <= val <= hi:
            return None, f"大週期突破參數 {k} 要在 {lo}～{hi} 之間，收到 {v}"
        cfg[k] = val
    # 給回測迴圈的欄位(跟其他1小時K策略共用)
    cfg["max_hold_hours"] = int(cfg["max_hold_days"]) * 24
    return cfg, None


def build_4h(hourly):
    """1小時K依 UTC 4 小時對齊組成 4 小時K。回傳 (four_h, close_idx, last_closed)：
    close_idx[i]＝第 i 根1小時K若是某根4小時K的最後一小時，就是那根4小時K的索引，否則 None；
    last_closed[i]＝到第 i 根1小時K為止，最近一根已收盤4小時K的索引(沒有就是 None)。"""
    four_h, close_idx, last_closed = [], [], []
    cur_bucket, last = None, None
    for c in hourly:
        b = c["bucket_start"] - c["bucket_start"] % TF_MS
        if b != cur_bucket:
            four_h.append({"bucket_start": b, "open": c["open"], "high": c["high"], "low": c["low"],
                           "close": c["close"], "volume": c.get("volume", 0.0)})
            cur_bucket = b
        else:
            k = four_h[-1]
            k["high"] = max(k["high"], c["high"])
            k["low"] = min(k["low"], c["low"])
            k["close"] = c["close"]
            k["volume"] = k.get("volume", 0.0) + c.get("volume", 0.0)
        if (c["bucket_start"] + HOUR_MS) % TF_MS == 0:
            close_idx.append(len(four_h) - 1)
            last = len(four_h) - 1
        else:
            close_idx.append(None)
        last_closed.append(last)
    return four_h, close_idx, last_closed


def precompute(hourly):
    four_h, close_idx, last_closed = build_4h(hourly)
    closes = [k["close"] for k in four_h]
    return {"hourly": hourly, "four_h": four_h, "close_idx": close_idx, "last_closed": last_closed,
            "ema": _ema_series(closes, EMA_PERIOD), "atr": _atr_series(four_h)}


def _neutral(reason, hr_atr=None):
    return {"stage": "中性", "direction": None, "chan": {"reason": reason}, "profile": {"reason": ""},
            "mr": None, "hr_atr": hr_atr}


def _tr(bars, k):
    h, l, pc = bars[k]["high"], bars[k]["low"], bars[k - 1]["close"]
    return max(h - l, abs(h - pc), abs(l - pc))


def evaluate_at(pre, i, cfg, current_price=None):
    if i < 0 or i >= len(pre["close_idx"]):
        return _neutral("超出範圍")
    lc = pre["last_closed"][i]
    hr_atr = pre["atr"][lc] if lc is not None else None
    k = pre["close_idx"][i]
    if k is None:
        return _neutral("等這根4小時K收盤", hr_atr)
    n = int(cfg["channel"])
    bars = pre["four_h"]
    if k < max(n, EMA_PERIOD) + 1:
        return _neutral(f"資料不足(4小時K要至少{max(n, EMA_PERIOD) + 2}根)", hr_atr)
    atr, atr_prev, ema = pre["atr"][k], pre["atr"][k - 1], pre["ema"][k]
    if None in (atr, atr_prev, ema) or atr <= 0 or atr_prev <= 0:
        return _neutral("資料不足(指標還沒算出來)", hr_atr)
    bar = bars[k]
    hi = max(b["high"] for b in bars[k - n:k])
    lo = min(b["low"] for b in bars[k - n:k])
    tr = _tr(bars, k)
    sides = int(cfg["sides"])
    reasons = []
    for sign in (1, -1):
        if (sign > 0 and sides == 2) or (sign < 0 and sides == 1):
            continue
        side = "多" if sign > 0 else "空"
        level = hi if sign > 0 else lo
        if not sign * (bar["close"] - level) > 0:
            reasons.append(f"{side}：沒有突破前{n}根{'高' if sign > 0 else '低'}點 {level:.2f}")
            continue
        need = float(cfg["big_bar_atr"])
        if need > 0 and tr < need * atr_prev:
            reasons.append(f"{side}：突破K波幅 {tr:.2f} 不到 {need:g} 倍ATR({need * atr_prev:.2f})，不算大行情")
            continue
        if int(cfg["trend"]) == 1 and not sign * (bar["close"] - ema) > 0:
            reasons.append(f"{side}：收盤不在 EMA50({ema:.2f}) {'之上' if sign > 0 else '之下'}")
            continue
        entry = float(current_price) if current_price is not None else bar["close"]
        sl_points = float(cfg["sl_atr"]) * atr
        why = (f"4小時K{'向上' if sign > 0 else '向下'}突破前{n}根{'高' if sign > 0 else '低'}點 {level:.2f}，"
               f"波幅 {tr:.2f}＝{tr / atr_prev:.2f} 倍ATR" + ("，順 EMA50" if int(cfg["trend"]) == 1 else ""))
        trig = float(cfg["trail_trigger_atr"])
        exit_note = (f"停損 {float(cfg['sl_atr']):g} 倍4小時ATR({sl_points:.2f}點，{entry - sign * sl_points:.2f})；"
                     f"浮盈 {trig:g} 倍ATR 後移動停損、回吐 {float(cfg['trail_atr']):g} 倍ATR 出場(最差保本)；沒有固定停利"
                     + (f"；最長 {int(cfg['max_hold_days'])} 天" if int(cfg["max_hold_days"]) > 0 else ""))
        return {"stage": "訊號", "direction": "bullish" if sign > 0 else "bearish",
                "chan": {"reason": why}, "profile": {"reason": exit_note},
                "mr": {"setup": f"突破{side}", "sl_points": sl_points, "tp_price": None, "atr": atr},
                "hr_atr": atr}
    return _neutral("；".join(reasons) or "這個方向不做", hr_atr)
