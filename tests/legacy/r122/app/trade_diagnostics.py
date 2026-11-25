"""
進出場診斷統計(r115，只在回測)：用數據決定「改進場點／改停損／維持原樣」，不靠幾筆單的印象。

背景(2026-11-06)：15 分K 纏論在震盪往下的盤連續被洗——跌破中樞就進場、價格回抽中樞就停損，事後看方向是對的。
可能的改法(改成第三類買賣點才進、改用結構停損、分批進場)各有代價：例如等回抽才進，會錯過「一次就走掉」的大行情
(11/4 那筆 +107.75)。所以先回答三個問題：

  1. 被洗出場有多常見：虧損出場後 AFTER_HOURS 小時內，價格有沒有朝原本方向走到「進場價 ± FAVORABLE_ATR 倍 ATR」。
  2. 停損是不是太緊：賺錢的單，持倉期間最多逆向走了多深(MAE)，佔初始停損距離的幾成。
  3. 中樞突破單「有回抽」和「直接走」各賺賠多少：突破後 RETEST_HOURS 小時內，價格有沒有回測被突破的中樞邊界
     (多單：低點碰到中樞上界 ZG；空單：高點碰到中樞下界 ZD)。直接走的那組就是改成第三類買賣點會錯過的單。

annotate() 在回測迴圈結束後，用回測用的K線(1 分K，長天數是 15 分K)替每筆交易補上診斷欄位(diag_*)；
summarize() 只看交易紀錄本身，所以分段回測的合計也能用同一支算。全部是事後統計，不影響回測的進出場。
"""
import bisect
from datetime import datetime

AFTER_HOURS = 4        # 虧損出場後看幾小時
FAVORABLE_ATR = 2.0    # 朝原方向走到「進場價 ± 幾倍 ATR」算「方向其實是對的」
RETEST_HOURS = 2       # 突破後幾小時內回測中樞邊界算「有回抽」
HOUR_MS = 3_600_000
MAE_BUCKETS = ((0.25, "<25%"), (0.5, "25~50%"), (0.75, "50~75%"), (1.0, "75~100%"), (float("inf"), "≥100%"))


def entry_meta(strategy_type, result, atr, sl_points, price=None, direction=None):
    """
    開倉當下要記的診斷資訊(放在部位的 diag 欄位)：ATR、初始停損距離、進場種類(中樞突破／背馳／其他)
    與被突破的中樞邊界。只有纏論+分價量表(chan_profile)分得出種類，其他策略記 "n/a"。
    """
    meta = {"atr": atr, "sl_points": sl_points, "kind": "n/a", "level": None}
    if strategy_type not in (None, "chan_profile"):
        return meta
    reason = str(((result or {}).get("chan") or {}).get("reason") or "")
    zs = ((result or {}).get("chan_detail") or {}).get("latest_zhongshu") or {}
    if "背馳" in reason:
        meta["kind"] = "beichi"
    elif "突破確立" in reason and zs.get("zg") is not None:
        meta["kind"], meta["level"] = "breakout", float(zs["zg"])
    elif "破壞確立" in reason and zs.get("zd") is not None:
        meta["kind"], meta["level"] = "breakout", float(zs["zd"])
    else:
        meta["kind"] = "other"
    # r116：進場當下就看得到的特徵(看能不能事前分出「有回抽」和「直接走」)
    if meta["kind"] == "breakout" and atr:
        sign = 1 if direction == "bullish" else -1
        if price is not None:
            meta["break_atr"] = round(sign * (float(price) - meta["level"]) / atr, 3)   # 進場價離被突破的邊界幾倍 ATR
        if zs.get("zg") is not None and zs.get("zd") is not None:
            meta["zs_height_atr"] = round((float(zs["zg"]) - float(zs["zd"])) / atr, 3)   # 中樞高度幾倍 ATR
    return meta


# r119 → r121 修正：消息面震盪(不靠經濟日曆，直接從價格認)。
# r119 用「前 24 小時 1 分K 高低差中位數的 5 倍」，結果每 30 天認出 3,000～6,000 次——亞洲盤的 1 分K 很小，
# 紐約開盤正常放量就超過 5 倍，量到的是「時段」不是「消息」。r121 起兩個條件都要成立：
#   1. 這根 1 分K 的高低差 ≥ SHOCK_MULT 倍「前 SHOCK_DAYS 天同一個 UTC 小時的 1 分K 高低差中位數」(跟同時段比)；
#   2. 而且 ≥ 最近 SHOCK_ATR_HOURS 根已收完 1 小時K 的平均高低差(一根 1 分K 走掉一整個小時的平均波動)。
# 同一個小時至少要有 SHOCK_MIN_DAYS 天的樣本、而且已經收完 SHOCK_ATR_HOURS 根小時K 才判斷；只用那根之前的K線(因果)。
SHOCK_MULT = 5.0
SHOCK_DAYS = 20
SHOCK_MIN_DAYS = 5
SHOCK_ATR_HOURS = 14
SHOCK_COOLDOWN_MIN = 60


def shock_times(klines):
    """回傳消息震盪那幾根 1 分K 的收盤時間(遞增)。K線不是 1 分K 時回 None(不判斷)。"""
    if len(klines) < 2 or int(klines[1][0]) - int(klines[0][0]) != 60_000:
        return None
    per_hour_window = {h: [] for h in range(24)}   # 每個 UTC 小時：最近 SHOCK_DAYS 天的 1 分K 高低差(依時間)
    per_hour_sorted = {h: [] for h in range(24)}
    cap = SHOCK_DAYS * 60
    hour_ranges = []                                # 已收完的 1 小時K 高低差(最近 SHOCK_ATR_HOURS 根)
    cur_bucket, cur_hi, cur_lo = None, None, None
    out = []
    for k in klines:
        t = int(k[0])
        hi, lo = float(k[2]), float(k[3])
        rng = hi - lo
        bucket = t // 3_600_000
        if bucket != cur_bucket:
            if cur_bucket is not None:
                hour_ranges.append(cur_hi - cur_lo)
                if len(hour_ranges) > SHOCK_ATR_HOURS:
                    hour_ranges.pop(0)
            cur_bucket, cur_hi, cur_lo = bucket, hi, lo
        h = bucket % 24
        ordered = per_hour_sorted[h]
        if len(ordered) >= SHOCK_MIN_DAYS * 60 and len(hour_ranges) >= SHOCK_ATR_HOURS:
            med = ordered[len(ordered) // 2]
            hour_avg = sum(hour_ranges) / len(hour_ranges)
            if med > 0 and rng >= SHOCK_MULT * med and rng >= hour_avg:
                out.append(int(k[6]))
        win = per_hour_window[h]
        win.append(rng)
        bisect.insort(ordered, rng)
        if len(win) > cap:
            old = win.pop(0)
            del ordered[bisect.bisect_left(ordered, old)]
        cur_hi, cur_lo = max(cur_hi, hi), min(cur_lo, lo)
    return out


def recent_shock(shocks, now_ms, minutes=SHOCK_COOLDOWN_MIN):
    """now_ms 往前 minutes 分鐘內(含 now)有沒有消息震盪。shocks=None(不判斷)時回 None。"""
    if shocks is None:
        return None
    i = bisect.bisect_right(shocks, now_ms)
    return i > 0 and shocks[i - 1] > now_ms - minutes * 60_000


def narrow_zs_allows(strategy_type, result, max_zs_height_atr, price=None):
    """
    r117 回測、r118 起即時引擎共用：中樞突破只做「中樞高度 < N 倍 ATR」(窄中樞＝壓縮後的突破)。
    回傳 (允許, 說明)。N <= 0＝關閉；不是纏論中樞突破(背馳／其他／別的策略)一律允許；
    中樞突破但算不出高度(沒有 ATR 或中樞)＝不當成窄中樞，不允許。
    """
    n = float(max_zs_height_atr or 0)
    if n <= 0:
        return True, None
    meta = entry_meta(strategy_type, result, (result or {}).get("atr"), None,
                      price=price, direction=(result or {}).get("direction"))
    if meta["kind"] != "breakout":
        return True, None
    h = meta.get("zs_height_atr")
    if h is None:
        return False, "中樞突破但算不出中樞高度(沒有 ATR)，不當成窄中樞"
    if h >= n:
        return False, f"中樞高度 {h:.2f} 倍 ATR，不小於 {n:g}(只做窄中樞突破)"
    return True, None


def _ms(iso):
    try:
        return int(datetime.fromisoformat(str(iso)).timestamp() * 1000)
    except (TypeError, ValueError):
        return None


class Bars:
    """K線(幣安格式 list)轉成可以用時間區間查最高/最低的陣列。"""

    def __init__(self, klines):
        self.open_t = [int(k[0]) for k in klines]
        self.close_t = [int(k[6]) for k in klines]
        self.high = [float(k[2]) for k in klines]
        self.low = [float(k[3]) for k in klines]

    def span(self, start_ms, end_ms):
        """開盤時間落在 [start_ms, end_ms) 的K線索引範圍。"""
        return bisect.bisect_left(self.open_t, start_ms), bisect.bisect_left(self.open_t, end_ms)

    def extremes(self, start_ms, end_ms):
        a, b = self.span(start_ms, end_ms)
        if a >= b:
            return None, None
        return max(self.high[a:b]), min(self.low[a:b])

    def last_ms(self):
        return self.close_t[-1] if self.close_t else None


def annotate(trades, klines):
    """替每筆有 diag 的交易補上：diag_after_favorable、diag_mae_ratio、diag_mfe_ratio、diag_retest。資料不夠的記 None。"""
    if not klines:
        return trades
    bars = Bars(klines)
    end_of_data = bars.last_ms()
    for t in trades:
        d = t.get("diag") or {}
        ent, ext = _ms(t.get("entry_time")), _ms(t.get("exit_time"))
        price, sign = t.get("entry_price"), (1 if t.get("direction") == "bullish" else -1)
        atr, slp = d.get("atr"), d.get("sl_points")
        if ent is None or ext is None or price is None:
            continue
        # 持倉期間的最大逆向／順向幅度(MAE／MFE)，以初始停損距離為單位
        hi, lo = bars.extremes(ent, ext + 1)
        if hi is not None and slp:
            adverse = (price - lo) if sign > 0 else (hi - price)
            favorable = (hi - price) if sign > 0 else (price - lo)
            t["diag_mae_ratio"] = round(max(0.0, adverse) / slp, 3)
            t["diag_mfe_ratio"] = round(max(0.0, favorable) / slp, 3)
        # 虧損出場後，方向其實是對的嗎(出場後 AFTER_HOURS 小時內走到進場價 ± FAVORABLE_ATR 倍 ATR)
        if t.get("pnl_points") is not None and t["pnl_points"] < 0 and atr:
            if end_of_data is not None and ext + AFTER_HOURS * HOUR_MS <= end_of_data:
                hi2, lo2 = bars.extremes(ext + 1, ext + AFTER_HOURS * HOUR_MS)
                target = price + sign * FAVORABLE_ATR * atr
                t["diag_after_favorable"] = (hi2 is not None and
                                             ((hi2 >= target) if sign > 0 else (lo2 <= target)))
            else:
                t["diag_after_favorable"] = None   # 資料不夠看完 N 小時，不算
        # r116：進場前 1 小時往突破方向走了幾倍 ATR(進場當下就知道，只用進場前的K線)
        if d.get("kind") == "breakout" and atr and "momentum_atr" not in d:
            a, b = bars.span(ent - HOUR_MS, ent + 1)
            if b - a >= 2:
                first_open = float(klines[a][1])
                last_close = float(klines[b - 1][4])
                d["momentum_atr"] = round(sign * (last_close - first_open) / atr, 3)
        # 中樞突破單：RETEST_HOURS 小時內有沒有回測被突破的邊界
        if d.get("kind") == "breakout" and d.get("level") is not None:
            if end_of_data is not None and ent + RETEST_HOURS * HOUR_MS <= end_of_data:
                hi3, lo3 = bars.extremes(ent + 1, ent + RETEST_HOURS * HOUR_MS)
                lvl = d["level"]
                t["diag_retest"] = (hi3 is not None and ((lo3 <= lvl) if sign > 0 else (hi3 >= lvl)))
            else:
                t["diag_retest"] = None
    return trades


def _group(trades):
    pnls = [t["pnl_points"] for t in trades if t.get("pnl_points") is not None]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_loss = -sum(losses)
    return {"trades": len(pnls), "win_rate": round(100 * len(wins) / len(pnls), 1) if pnls else None,
            "total_pnl_points": round(sum(pnls), 2), "avg_pnl_points": round(sum(pnls) / len(pnls), 2) if pnls else None,
            "profit_factor": round(sum(wins) / gross_loss, 2) if gross_loss > 0 else None,
            "biggest_win": round(max(wins), 2) if wins else None}


def summarize(trades):
    """只看交易紀錄(含 diag_* 欄位)算出三組診斷數字；沒有 diag 的交易不算。"""
    ts = [t for t in (trades or []) if t.get("diag") and t.get("pnl_points") is not None]
    if not ts:
        return None
    # 1. 被洗出場
    losers = [t for t in ts if t["pnl_points"] < 0]
    judged = [t for t in losers if t.get("diag_after_favorable") is not None]
    washed = [t for t in judged if t["diag_after_favorable"]]
    by_reason = {}
    for t in judged:
        key = "停損" if "停損" in str(t.get("exit_reason") or "") else "其他(訊號反轉等)"
        g = by_reason.setdefault(key, {"judged": 0, "washed": 0})
        g["judged"] += 1
        g["washed"] += 1 if t["diag_after_favorable"] else 0
    washed_part = {"losers": len(losers), "judged": len(judged), "washed": len(washed),
                   "washed_pct": round(100 * len(washed) / len(judged), 1) if judged else None,
                   "washed_loss_points": round(sum(t["pnl_points"] for t in washed), 2),
                   "by_exit": by_reason, "after_hours": AFTER_HOURS, "favorable_atr": FAVORABLE_ATR}
    # 2. 贏單的最大逆向(以初始停損距離為單位)
    winners = [t for t in ts if t["pnl_points"] > 0 and t.get("diag_mae_ratio") is not None]
    buckets = {label: 0 for _, label in MAE_BUCKETS}
    for t in winners:
        for limit, label in MAE_BUCKETS:
            if t["diag_mae_ratio"] < limit:
                buckets[label] += 1
                break
    near = buckets["75~100%"] + buckets["≥100%"]
    mae_part = {"winners": len(winners), "buckets": buckets,
                "near_stop_pct": round(100 * near / len(winners), 1) if winners else None,
                "median_mae_ratio": (sorted(t["diag_mae_ratio"] for t in winners)[len(winners) // 2] if winners else None)}
    # 3. 進場種類，以及中樞突破單「有回抽／直接走」
    kinds = {}
    for k in ("breakout", "beichi", "other", "n/a"):
        sub = [t for t in ts if (t.get("diag") or {}).get("kind") == k]
        if sub:
            kinds[k] = _group(sub)
    bo = [t for t in ts if (t.get("diag") or {}).get("kind") == "breakout"]
    retest = [t for t in bo if t.get("diag_retest") is True]
    runaway = [t for t in bo if t.get("diag_retest") is False]
    breakout_part = {"retest": _group(retest), "runaway": _group(runaway),
                     "unknown": len([t for t in bo if t.get("diag_retest") is None]), "retest_hours": RETEST_HOURS}
    breakout_part["features"] = feature_buckets(bo)
    # r121：多單／空單分開(看近半年的虧損是不是主要來自某一邊)
    by_direction = {"bullish": _group([t for t in ts if t.get("direction") == "bullish"]),
                    "bearish": _group([t for t in ts if t.get("direction") == "bearish"])}
    # r119：消息震盪後 SHOCK_COOLDOWN_MIN 分鐘內進場 vs 其他(全部進場種類)
    shock_part = None
    flagged = [t for t in ts if (t.get("diag") or {}).get("after_shock") is not None]
    if flagged:
        shock_part = {"after": _group([t for t in flagged if t["diag"]["after_shock"]]),
                      "normal": _group([t for t in flagged if not t["diag"]["after_shock"]]),
                      "cooldown_min": SHOCK_COOLDOWN_MIN, "mult": SHOCK_MULT, "days": SHOCK_DAYS}
    return {"washed": washed_part, "winner_mae": mae_part, "kinds": kinds, "breakout": breakout_part, "news_shock": shock_part,
            "by_direction": by_direction}


FEATURES = (
    ("break_atr", "突破距離(進場價離中樞邊界，ATR)", (0.25, 0.5, 1.0, 2.0)),
    ("zs_height_atr", "中樞高度(ATR)", (1.0, 2.0, 4.0)),
    ("momentum_atr", "進場前 1 小時往突破方向的漲跌(ATR)", (1.0, 2.0, 3.0)),
)


def _bucket_label(edges, i):
    if i == 0:
        return f"<{edges[0]:g}"
    if i == len(edges):
        return f"≥{edges[-1]:g}"
    return f"{edges[i - 1]:g}~{edges[i]:g}"


def feature_buckets(breakouts):
    """
    r116：中樞突破單依進場當下的特徵分組，每組列「有回抽」的比例與損益。某一組的回抽比例明顯低、損益明顯好，
    才代表這個特徵事前就分得出直接走的突破(之後才值得當進場門檻測試)。
    """
    out = []
    for key, label, edges in FEATURES:
        rows = []
        for i in range(len(edges) + 1):
            lo = edges[i - 1] if i > 0 else float("-inf")
            hi = edges[i] if i < len(edges) else float("inf")
            sub = [t for t in breakouts if (t.get("diag") or {}).get(key) is not None and lo <= t["diag"][key] < hi]
            judged = [t for t in sub if t.get("diag_retest") is not None]
            g = _group(sub)
            g["label"] = _bucket_label(edges, i)
            g["retest_pct"] = round(100 * sum(1 for t in judged if t["diag_retest"]) / len(judged), 1) if judged else None
            rows.append(g)
        out.append({"key": key, "label": label, "rows": rows})
    return out
