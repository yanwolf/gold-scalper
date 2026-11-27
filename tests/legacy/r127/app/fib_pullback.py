"""
斐波那契回檔掛單(strategy_type="fib_pullback"，1小時K，r120，只存在回測)

背景(2026-11-06)：使用者參考一個講師訊號的邏輯(趨勢 → 等回檔到斐波那契比例掛限價單 → 很近的停損、前高停利)。
跟我們之前的策略剛好相反：之前是「突破就市價追」，r115 診斷發現突破後回抽的單大虧；這裡是「等回抽到位才進」。
講師的完整規則沒有公開，以下是自己定的版本，跑之前定死，預設值就是要驗的那一組：

  1. 趨勢(多單；空單反過來)：1 小時K 收盤在 EMA50 之上，而且 EMA50 比 5 根前高。
  2. 推動段：最近一個「確認過的擺動低點」L(左右各 PIVOT 根都比它高；右邊那 PIVOT 根收完才算確認)，
     L 之後到現在的最高點 H，H−L ≥ impulse_atr 倍 ATR(推動夠大)。
  3. 掛單：買進限價 = H − entry_fib × (H−L)(預設 0.618)；停損 = H − stop_fib × (H−L)(預設 0.786)再往外 0.1 倍 ATR；
     停利 = H(回到前高)。H 之後價格已經碰過掛單價(機會過了)、或現價已經在掛單價以下，就不掛。
  4. 單子只掛到下一根 1 小時K 收盤：每根收盤重新算一次(H 更新就換價)，跟真的程式掛單、每小時改單一樣。
     成交用 1 分K：低點碰到限價就以限價成交(空單看高點)。
  5. 出場：停損、停利，或持有 max_hold_hours 小時(預設 48)到時間出場；不移動停損、反向訊號不提前出場。

介面跟 mean_reversion／pullback_resume／breakout_trend 一樣(build_cfg / precompute / evaluate_at，結果放在 "mr")，
多了 mr["limit_price"]／mr["sl_price"]：回測迴圈看到 limit_price 就掛單(不是馬上用收盤價進場)。跟即時系統完全無關。
"""
from app.mean_reversion import _ema_series, _atr_series

PIVOT = 3
EMA_PERIOD = 50
EMA_SLOPE_BARS = 5

DEFAULT_CFG = {
    "impulse_atr": 2.0,
    "entry_fib": 0.618,
    "stop_fib": 0.786,
    "sides": 3,
    "max_hold_hours": 48,
}

CFG_LIMITS = {
    "impulse_atr": (0.5, 10.0, float),
    "entry_fib": (0.2, 0.9, float),
    "stop_fib": (0.3, 1.0, float),
    "sides": (1, 3, int),
    "max_hold_hours": (1, 240, int),
}
STOP_BUFFER_ATR = 0.1


def build_cfg(overrides=None):
    cfg = dict(DEFAULT_CFG)
    for k, v in (overrides or {}).items():
        if v is None or v == "":
            continue
        if k not in CFG_LIMITS:
            return None, f"斐波那契回檔沒有這個參數：{k}"
        lo, hi, typ = CFG_LIMITS[k]
        try:
            fv = float(v)
        except (TypeError, ValueError):
            return None, f"斐波那契回檔參數 {k} 要是數字，收到 {v}"
        if typ is int and fv != int(fv):
            return None, f"斐波那契回檔參數 {k} 要是整數，收到 {v}"
        val = int(fv) if typ is int else fv
        if not lo <= val <= hi:
            return None, f"斐波那契回檔參數 {k} 要在 {lo}～{hi} 之間，收到 {v}"
        cfg[k] = val
    if cfg["stop_fib"] <= cfg["entry_fib"]:
        return None, "停損比例要比掛單比例深(stop_fib > entry_fib)"
    cfg["trail_atr"] = 0.0   # 回測迴圈共用欄位：不移動停損
    return cfg, None


def precompute(hourly):
    highs = [c["high"] for c in hourly]
    lows = [c["low"] for c in hourly]
    n = len(hourly)
    piv_hi = [False] * n
    piv_lo = [False] * n
    for j in range(PIVOT, n - PIVOT):
        left_h, right_h = highs[j - PIVOT:j], highs[j + 1:j + 1 + PIVOT]
        left_l, right_l = lows[j - PIVOT:j], lows[j + 1:j + 1 + PIVOT]
        # 左邊嚴格、右邊可以相等：K線開盤＝前一根收盤時，相鄰兩根的極值常常一樣，算前面那一根
        piv_hi[j] = highs[j] > max(left_h) and highs[j] >= max(right_h)
        piv_lo[j] = lows[j] < min(left_l) and lows[j] <= min(right_l)
    return {"hourly": hourly, "high": highs, "low": lows, "close": [c["close"] for c in hourly],
            "ema": _ema_series([c["close"] for c in hourly], EMA_PERIOD), "atr": _atr_series(hourly),
            "piv_hi": piv_hi, "piv_lo": piv_lo}


def _neutral(reason):
    return {"stage": "中性", "direction": None, "chan": {"reason": reason}, "profile": {"reason": ""}, "mr": None}


def _last_pivot(flags, i):
    """到第 i 根為止已經確認的最近一個擺動點(右邊 PIVOT 根已收完：j <= i - PIVOT)。"""
    for j in range(i - PIVOT, PIVOT - 1, -1):
        if flags[j]:
            return j
    return None


def _setup(pre, i, cfg, sign):
    ema, atr = pre["ema"][i], pre["atr"][i]
    if i < EMA_SLOPE_BARS or ema is None or atr is None or atr <= 0 or pre["ema"][i - EMA_SLOPE_BARS] is None:
        return None, "資料不足"
    close = pre["close"][i]
    side = "多" if sign > 0 else "空"
    if not (sign * (close - ema) > 0 and sign * (ema - pre["ema"][i - EMA_SLOPE_BARS]) > 0):
        return None, f"{side}：不在 EMA50 {'之上且向上' if sign > 0 else '之下且向下'}"
    j0 = _last_pivot(pre["piv_lo"] if sign > 0 else pre["piv_hi"], i)
    if j0 is None:
        return None, f"{side}：還沒有確認的擺動{'低' if sign > 0 else '高'}點"
    start = pre["low"][j0] if sign > 0 else pre["high"][j0]
    seg = range(j0 + 1, i + 1)
    if not seg:
        return None, f"{side}：擺動點之後還沒有K線"
    if sign > 0:
        j1 = max(seg, key=lambda k: pre["high"][k])
        end = pre["high"][j1]
    else:
        j1 = min(seg, key=lambda k: pre["low"][k])
        end = pre["low"][j1]
    rng = sign * (end - start)
    if rng < float(cfg["impulse_atr"]) * atr:
        return None, f"{side}：推動段 {rng:.2f} 不到 {float(cfg['impulse_atr']):g} 倍ATR"
    limit = end - sign * float(cfg["entry_fib"]) * rng
    stop = end - sign * float(cfg["stop_fib"]) * rng - sign * STOP_BUFFER_ATR * atr
    if not sign * (close - limit) > 0:
        return None, f"{side}：現價已經過了掛單價 {limit:.2f}"
    after = range(j1 + 1, i + 1)
    if after:
        touched = (min(pre["low"][k] for k in after) <= limit) if sign > 0 else (max(pre["high"][k] for k in after) >= limit)
        if touched:
            return None, f"{side}：高低點之後已經碰過掛單價，機會過了"
    return {"limit": limit, "stop": stop, "tp": end, "start": start, "end": end, "atr": atr}, None


def evaluate_at(pre, i, cfg, current_price=None):
    if i < 0 or i >= len(pre["close"]):
        return _neutral("超出範圍")
    sides = int(cfg["sides"])
    reasons = []
    for sign in (1, -1):
        if (sign > 0 and sides == 2) or (sign < 0 and sides == 1):
            continue
        st, why = _setup(pre, i, cfg, sign)
        if st is None:
            reasons.append(why)
            continue
        side = "多" if sign > 0 else "空"
        sl_points = abs(st["limit"] - st["stop"])
        reason = (f"{side}：推動段 {st['start']:.2f}→{st['end']:.2f}，掛{'買' if sign > 0 else '賣'}限價 {st['limit']:.2f}"
                  f"(回檔 {float(cfg['entry_fib']):g})，停損 {st['stop']:.2f}，停利 {st['tp']:.2f}(前{'高' if sign > 0 else '低'})")
        return {"stage": "訊號", "direction": "bullish" if sign > 0 else "bearish",
                "chan": {"reason": reason},
                "profile": {"reason": f"停損 {st['stop']:.2f}／停利 {st['tp']:.2f}／最長 {int(cfg['max_hold_hours'])} 小時"},
                "mr": {"setup": f"回檔{side}", "sl_points": sl_points, "tp_price": st["tp"], "atr": st["atr"],
                       "limit_price": st["limit"], "sl_price": st["stop"]}}
    return _neutral("；".join(reasons) or "這個方向不做")
