"""
均值回歸策略(strategy_type="mean_reversion"，1小時K，r96，只存在回測)

來源：使用者朋友在 MT5 上的 XAUUSD 1小時K 對帳單(2026/10/7～10/8，8 筆全賺)。看不到他的規則，
從圖面反推出兩種進場，寫成可調參數的假設，用回測驗證「這種做法長期站不站得住」：

  A. 拉開過頭(逆勢)：急跌/急漲之後 RSI 到極端值、收盤離快線超過 N 倍 ATR → 反向進場，目標回到快線。
     (10/7：長下影線、RSI 27、離均線很遠 → 4,081／4,072 做多，回到 4,094 出場)
  B. 反彈碰慢線(順勢回檔)：慢線往下彎、快線在慢線下方，價格從慢線下方反彈碰到慢線、又收黑在慢線下 → 放空；多單反之。
     (10/8：反彈到下彎的慢線 4,136 附近放空，回到 4,113 出場)

刻意沒有模擬的：他的分批加碼(同方向第二筆在更差的價位)。回測引擎一次只有一個部位，
加碼會讓「賺的時候賺兩份、賠的時候賠兩份」，這裡的結果只代表單一部位的版本。

全部都是因果計算：第 i 根的指標只用 <= i 的已收盤1小時K；同一根K只會給一次訊號(由回測迴圈控管)。
跟即時系統完全無關：沒有引擎、不是 settings 欄位，paper_trading.py 不會 import 這個模組。
"""

FAST_EMA = 20
SLOW_EMA = 50
RSI_PERIOD = 14
ATR_PERIOD = 14
SLOPE_BARS = 5          # 慢線「往下彎/往上彎」：跟 5 根前比
WARMUP_BARS = SLOW_EMA + SLOPE_BARS
REBOUND_ATR = 1.0       # B：最近 SLOPE_BARS 根內曾經離慢線至少這麼多倍 ATR，才算「反彈」碰慢線

DEFAULT_CFG = {
    "setups": 3,          # 1=只做A(拉開過頭) 2=只做B(反彈碰慢線) 3=兩種都做
    "rsi_level": 30.0,    # A：多單 RSI <= 這個值、空單 RSI >= 100-這個值
    "stretch_atr": 1.5,   # A：收盤離快線至少幾倍 ATR
    "touch_atr": 0.2,     # B：高(低)點離慢線幾倍 ATR 以內算碰到
    "tp_mode": 0,         # 0=回到快線(太近時改用 tp_atr) 1=固定 tp_atr 倍 ATR
    "tp_atr": 1.5,
    "sl_atr": 1.5,        # 初始停損＝進場價 ∓ sl_atr × ATR
    "trail_atr": 1.0,     # 浮盈到 trail_atr × ATR 才啟動移動停損(最差保本)；0＝不移動
    "max_hold_hours": 12, # 持有超過幾小時就市價出場；0＝不限
}

# (最小值, 最大值, 型別)
CFG_LIMITS = {
    "setups": (1, 3, int),
    "rsi_level": (5.0, 45.0, float),
    "stretch_atr": (0.0, 6.0, float),
    "touch_atr": (0.0, 2.0, float),
    "tp_mode": (0, 1, int),
    "tp_atr": (0.2, 10.0, float),
    "sl_atr": (0.2, 10.0, float),
    "trail_atr": (0.0, 10.0, float),
    "max_hold_hours": (0, 240, int),
}

# 回到快線的目標太近(小於這個倍數的 ATR)時改用 tp_atr，免得賺不到價差
MIN_TP_ATR = 0.5


def build_cfg(overrides=None):
    """合併預設值與回測面板填的值；超出範圍或型別不對回 (None, 錯誤訊息)。None 的欄位沿用預設。"""
    cfg = dict(DEFAULT_CFG)
    for k, v in (overrides or {}).items():
        if v is None or v == "":
            continue
        if k not in CFG_LIMITS:
            return None, f"均值回歸沒有這個參數：{k}"
        lo, hi, typ = CFG_LIMITS[k]
        try:
            val = typ(float(v)) if typ is int else float(v)
        except (TypeError, ValueError):
            return None, f"均值回歸參數 {k} 要是數字，收到 {v}"
        if typ is int and float(v) != int(float(v)):
            return None, f"均值回歸參數 {k} 要是整數，收到 {v}"
        if not lo <= val <= hi:
            return None, f"均值回歸參數 {k} 要在 {lo}～{hi} 之間，收到 {v}"
        cfg[k] = val
    return cfg, None


# ---------------------------------------------------------------------------
# 指標序列(全部因果；資料不足處是 None)
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


def _rsi_series(closes, n=RSI_PERIOD):
    """Wilder 平滑，跟 analysis.compute_rsi 同一個算法(那邊只回最後一個值)。"""
    out = [None] * len(closes)
    if len(closes) < n + 1:
        return out
    gains = [max(closes[i] - closes[i - 1], 0.0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0.0) for i in range(1, len(closes))]
    ag = sum(gains[:n]) / n
    al = sum(losses[:n]) / n

    def _v(g, l):
        if l == 0:
            return 100.0   # 跟 analysis.compute_rsi 一樣(沒有下跌就是 100)
        return 100 - 100 / (1 + g / l)

    out[n] = _v(ag, al)
    for i in range(n, len(gains)):
        ag = (ag * (n - 1) + gains[i]) / n
        al = (al * (n - 1) + losses[i]) / n
        out[i + 1] = _v(ag, al)
    return out


def _atr_series(candles, n=ATR_PERIOD):
    """True Range 的 N 期簡單平均，跟 analysis.compute_atr 同定義。"""
    out = [None] * len(candles)
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        if len(trs) >= n:
            out[i] = sum(trs[-n:]) / n
    return out


def precompute(candles):
    """整段已收盤1小時K算一次指標，之後用 evaluate_at() 逐根查。"""
    closes = [c["close"] for c in candles]
    return {
        "candles": candles,
        "fast": _ema_series(closes, FAST_EMA),
        "slow": _ema_series(closes, SLOW_EMA),
        "rsi": _rsi_series(closes),
        "atr": _atr_series(candles),
    }


def _neutral(reason):
    return {"stage": "中性", "direction": None,
            "chan": {"reason": reason}, "profile": {"reason": ""}, "mr": None}


def evaluate_at(pre, i, cfg, current_price=None):
    """
    第 i 根(已收盤)1小時K的訊號。回傳格式跟其他策略一樣 {stage, direction, chan, profile}，
    多一個 mr：{setup, sl_points, tp_price, atr, rsi, fast, slow}(只有「訊號」時才有)。
    tp_price 以 current_price(實際進場價)為基準算距離；沒給就用這根收盤價。
    """
    c = pre["candles"]
    if i < WARMUP_BARS or i >= len(c):
        return _neutral("資料不足(1小時K要至少55根)")
    fast, slow, rsi, atr = pre["fast"][i], pre["slow"][i], pre["rsi"][i], pre["atr"][i]
    slow_prev = pre["slow"][i - SLOPE_BARS]
    if None in (fast, slow, rsi, atr, slow_prev) or atr <= 0:
        return _neutral("資料不足(指標還沒算出來)")
    bar = c[i]
    close, high, low, open_ = bar["close"], bar["high"], bar["low"], bar["open"]
    setups = int(cfg["setups"])
    hit = None   # (direction, setup, reason)

    if setups in (1, 3):
        lv = float(cfg["rsi_level"])
        stretch = float(cfg["stretch_atr"]) * atr
        if rsi <= lv and fast - close >= stretch:
            hit = ("bullish", "A", f"拉開過頭做多：RSI {rsi:.1f}≤{lv:g}，收盤在快線下 {(fast - close) / atr:.2f} 倍ATR")
        elif rsi >= 100 - lv and close - fast >= stretch:
            hit = ("bearish", "A", f"拉開過頭放空：RSI {rsi:.1f}≥{100 - lv:g}，收盤在快線上 {(close - fast) / atr:.2f} 倍ATR")

    if hit is None and setups in (2, 3):
        tol = float(cfg["touch_atr"]) * atr
        # 「反彈」碰慢線：上一根收盤還在慢線的同一側(從下面彈上來／從上面拉回來)，急跌穿過慢線的那根不算
        # 而且最近幾根曾經離慢線至少 1 倍 ATR(真的是從遠處彈回來，不是貼著慢線小幅來回)
        prev_close, prev_slow = c[i - 1]["close"], pre["slow"][i - 1]
        recent = [x["close"] for x in c[i - SLOPE_BARS:i]]
        from_below = (prev_slow is not None and prev_close < prev_slow
                      and min(recent) <= slow - REBOUND_ATR * atr)
        from_above = (prev_slow is not None and prev_close > prev_slow
                      and max(recent) >= slow + REBOUND_ATR * atr)
        if (from_below and slow < slow_prev and fast < slow and high >= slow - tol
                and close < slow and close < open_):
            hit = ("bearish", "B", f"反彈碰下彎慢線放空：高點 {high:.2f} 碰到慢線 {slow:.2f}，收黑在慢線下")
        elif (from_above and slow > slow_prev and fast > slow and low <= slow + tol
                and close > slow and close > open_):
            hit = ("bullish", "B", f"回檔碰上彎慢線做多：低點 {low:.2f} 碰到慢線 {slow:.2f}，收紅在慢線上")

    if hit is None:
        return _neutral(f"沒有進場條件(RSI {rsi:.1f}，離快線 {(close - fast) / atr:+.2f} 倍ATR)")

    direction, setup, reason = hit
    entry = float(current_price) if current_price is not None else close
    sl_points = float(cfg["sl_atr"]) * atr
    sign = 1 if direction == "bullish" else -1
    tp_note = f"固定 {float(cfg['tp_atr']):g} 倍ATR"
    tp_price = entry + sign * float(cfg["tp_atr"]) * atr
    if int(cfg["tp_mode"]) == 0:
        if sign * (fast - entry) >= MIN_TP_ATR * atr:
            tp_price, tp_note = fast, "回到快線"
        else:
            tp_note = f"快線太近，改用 {float(cfg['tp_atr']):g} 倍ATR"
    return {
        "stage": "訊號", "direction": direction,
        "chan": {"reason": reason},
        "profile": {"reason": f"停利{tp_note} {tp_price:.2f}，停損 {float(cfg['sl_atr']):g} 倍ATR({sl_points:.2f}點)"},
        "mr": {"setup": setup, "sl_points": sl_points, "tp_price": tp_price, "atr": atr,
               "rsi": rsi, "fast": fast, "slow": slow},
    }
