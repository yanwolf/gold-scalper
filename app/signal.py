"""
多空訊號引擎。

設計原則：纏論(中樞突破/背馳反轉)和分價量表(POC/Value Area位置)兩個獨立訊號源，
各自產出方向判斷，綜合後才決定輸出階段：
- 兩邊方向一致 + 至少一邊訊號夠強 -> "訊號"
- 兩邊方向一致但都偏弱，或只有單邊有方向判斷 -> "關注"
- 兩邊方向衝突，或雙方都判斷不出方向 -> "中性"

這是規則式(rule-based)的第一版，不是機器學習模型，每條規則都對應到
纏論或Volume Profile的標準概念，方便之後逐條檢視、調整權重或替換。
"""


def _evaluate_chan_bias(chan_data):
    """
    纏論那一側的方向判斷。背馳(反轉訊號)優先於中樞突破(趨勢延續訊號)判斷，
    因為背馳出現時代表當前趨勢動能已經減弱，比單純的突破訊號更早示警。
    """
    if not chan_data or chan_data.get("message"):
        return {"bias": "neutral", "strength": "none", "reason": "纏論資料尚不足，尚無法判斷"}

    beichi = chan_data.get("beichi") or {}
    if beichi.get("has_beichi"):
        direction = beichi.get("direction")
        beichi_type = beichi.get("beichi_type")
        # 趨勢背馳(至少兩個不重疊中樞構成的真正趨勢)給strong權重；
        # 盤整背馳(範圍局部，可能只是中樞內部暫停)降級成weak，
        # 這樣「訊號」階段只會在趨勢背馳、或盤整背馳+分價量表也強烈同向時才觸發，
        # 避免把可信度較低的局部背馳當成跟趨勢背馳同等級的訊號處理
        strength = "strong" if beichi_type == "趨勢背馳" else "weak"
        if direction == "up":
            return {
                "bias": "bearish", "strength": strength,
                "reason": f"上漲段出現{beichi_type}，動能較前一段減弱（{beichi.get('detail', '')}），留意反轉向下",
            }
        elif direction == "down":
            return {
                "bias": "bullish", "strength": strength,
                "reason": f"下跌段出現{beichi_type}，動能較前一段減弱（{beichi.get('detail', '')}），留意反彈向上",
            }

    zhongshu = chan_data.get("latest_zhongshu")
    bi_list = chan_data.get("bi_list") or []
    if not zhongshu or not bi_list:
        return {"bias": "neutral", "strength": "none", "reason": "尚未形成中樞，纏論暫無方向判斷"}

    latest_bi_end_price = bi_list[-1]["end_price"]
    zg, zd = zhongshu["zg"], zhongshu["zd"]

    if latest_bi_end_price > zg:
        return {
            "bias": "bullish", "strength": "strong",
            "reason": f"價格站上中樞上界 {zg:.2f}，突破確立",
        }
    elif latest_bi_end_price < zd:
        return {
            "bias": "bearish", "strength": "strong",
            "reason": f"價格跌破中樞下界 {zd:.2f}，破壞確立",
        }
    else:
        return {
            "bias": "neutral", "strength": "weak",
            "reason": f"價格仍在中樞區間 {zd:.2f}~{zg:.2f} 內整理，尚未突破",
        }


def _evaluate_profile_bias(profile_data, current_price):
    """分價量表那一側的方向判斷：脫離Value Area是強訊號，只是相對POC偏移是弱訊號。"""
    if not profile_data or current_price is None:
        return {"bias": "neutral", "strength": "none", "reason": "分價量表資料尚不足，尚無法判斷"}

    poc = profile_data.get("poc")
    vah = profile_data.get("value_area_high")
    val = profile_data.get("value_area_low")

    if vah is not None and current_price > vah:
        return {
            "bias": "bullish", "strength": "strong",
            "reason": f"價格 {current_price:.2f} 站上 Value Area 高點 {vah:.2f}，脫離主要成交區",
        }
    if val is not None and current_price < val:
        return {
            "bias": "bearish", "strength": "strong",
            "reason": f"價格 {current_price:.2f} 跌破 Value Area 低點 {val:.2f}，脫離主要成交區",
        }
    if poc is not None:
        if current_price > poc:
            return {
                "bias": "bullish", "strength": "weak",
                "reason": f"價格位於 POC {poc:.2f} 之上，區間內偏多",
            }
        elif current_price < poc:
            return {
                "bias": "bearish", "strength": "weak",
                "reason": f"價格位於 POC {poc:.2f} 之下，區間內偏空",
            }

    return {"bias": "neutral", "strength": "none", "reason": "價格接近POC，區間內無明顯偏向"}


def _combine(chan_bias, profile_bias):
    """
    綜合兩側判斷，決定輸出階段。規則寫在函式最前面的表格方便之後調整：
    - 同方向 + 至少一邊strong -> 訊號
    - 同方向但都weak，或只有一邊有方向 -> 關注
    - 方向衝突，或雙方都neutral -> 中性
    """
    c_dir, p_dir = chan_bias["bias"], profile_bias["bias"]

    if c_dir != "neutral" and p_dir != "neutral":
        if c_dir == p_dir:
            has_strong = chan_bias["strength"] == "strong" or profile_bias["strength"] == "strong"
            stage = "訊號" if has_strong else "關注"
            return stage, c_dir
        else:
            return "中性", None

    if c_dir != "neutral" and p_dir == "neutral":
        return "關注", c_dir

    if p_dir != "neutral" and c_dir == "neutral":
        return "關注", p_dir

    return "中性", None


def generate_signal(chan_data, profile_data, current_price):
    """
    訊號引擎主入口。輸入纏論分析結果、分價量表結果、目前市價，
    回傳綜合判斷：階段(訊號/關注/中性)、方向(bullish/bearish/None)、
    以及兩側各自的判斷理由，方便在dashboard完整呈現「為什麼」給這個訊號。
    """
    chan_bias = _evaluate_chan_bias(chan_data)
    profile_bias = _evaluate_profile_bias(profile_data, current_price)
    stage, direction = _combine(chan_bias, profile_bias)

    return {
        "stage": stage,          # "訊號" / "關注" / "中性"
        "direction": direction,  # "bullish" / "bearish" / None
        "chan": chan_bias,
        "profile": profile_bias,
        "current_price": current_price,
    }


# ---------------------------------------------------------------------------
# 實驗性策略：多條件共振 + FVG (resonance_fvg)
#
# 目前只接回測(backtest.py可以指定strategy_type測試)，不接即時模擬單，
# 用意是先驗證「這麼多條件疊在一起，訊號量夠不夠、勝率好不好」，再決定
# 要不要真的接上即時追蹤(見signal_engine.py/README的STRATEGY_TYPE說明)。
#
# 設計上刻意要求「全部條件都符合」(AND邏輯)才給訊號，這是使用者參考的
# 交易筆記(RSI+MACD+多條EMA+FVG共振)明確要求的風格。要注意的取捨：
# 條件越多，符合的次數會斷崖式下降，訊號量可能很稀疏，這正是要先用
# 回測驗證「訊號量夠不夠」的原因，不能只看邏輯合理就直接假設會有效。
#
# 為了跟trading_core.open_position()等既有介面相容，回傳格式沿用
# generate_signal()同樣的{stage, direction, chan, profile, current_price}
# 結構，只是這裡"chan"欄位放的是動能面判斷(RSI+MACD)，"profile"欄位放的是
# 結構面判斷(EMA/FVG位置+價格行為)，跟纏論/分價量表的原意不同，是刻意
# 借用相同的欄位名稱維持介面相容，不是真的纏論或分價量表資料。
# ---------------------------------------------------------------------------

def _find_ema_support_or_resistance(current_price, emas, direction, tolerance_pct=0.15):
    """
    檢查價格是不是剛好回踩到長天期EMA(50/100/200)附近。tolerance_pct是容許的
    價格誤差百分比(預設0.15%，因為要求價格「剛好」壓在EMA上機率極低，需要
    給一個合理的容忍帶)。direction是"bullish"時找支撐(價格從上方接近)，
    "bearish"時找壓力(價格從下方接近)。
    """
    for period in (50, 100, 200):
        ema_value = emas.get(period)
        if ema_value is None:
            continue
        distance_pct = abs(current_price - ema_value) / ema_value * 100
        if distance_pct <= tolerance_pct:
            role = "支撐" if direction == "bullish" else "壓力"
            return f"價格回踩{period}EMA({ema_value:.2f})附近，視為{role}"
    return None


def _find_fvg_zone(current_price, fvgs, zone_type):
    """檢查價格是否落在某個未回補的FVG區間內。zone_type是"bullish"或"bearish"。"""
    for f in fvgs:
        if f["type"] != zone_type or f["filled"]:
            continue
        if f["bottom"] <= current_price <= f["top"]:
            role = "支撐" if zone_type == "bullish" else "壓力"
            return f"價格落入未回補的FVG區間({f['bottom']:.2f}~{f['top']:.2f})，視為{role}"
    return None


def _has_stabilizing_price_action(latest_candle, prev_candle, direction, wick_ratio_threshold=0.4):
    """
    簡化版價格行為止跌/止漲判斷：當前K棒留有夠長的方向性影線，或收盤價
    突破前一根K棒的極值。這是簡化實作，沒有做更嚴謹的K線形態辨識(例如
    吞噬、槌子線等經典形態)，先用這個粗略但計算簡單的版本驗證概念可不可行。
    """
    if not latest_candle:
        return None

    high, low = latest_candle["high"], latest_candle["low"]
    open_, close = latest_candle["open"], latest_candle["close"]
    total_range = high - low
    if total_range <= 0:
        return None

    if direction == "bullish":
        lower_wick = min(open_, close) - low
        if lower_wick / total_range >= wick_ratio_threshold:
            return "當前K棒留有長下影線，短線賣壓可能已經釋放"
        if prev_candle and close > prev_candle["low"]:
            return "收盤價站上前一根K棒低點，止跌訊號"
    else:
        upper_wick = high - max(open_, close)
        if upper_wick / total_range >= wick_ratio_threshold:
            return "當前K棒留有長上影線，短線買盤可能已經衰竭"
        if prev_candle and close < prev_candle["high"]:
            return "收盤價跌破前一根K棒高點，止漲訊號"

    return None


def _has_volume_confirmation(candles, direction, lookback=10, shrink_ratio=0.7):
    """
    量縮止跌/止漲判斷：比較最新K棒的成交量跟前面近期(lookback根，不含最新那根)
    的平均成交量，如果明顯縮小(低於平均的shrink_ratio倍)，代表賣壓(多單情境)
    或買壓(空單情境)可能已經釋放完畢——這是手稿裡「價量的東西」的「量」那半部，
    原本第一版只做了「價」(K棒影線/收盤位置)，這裡補上「量」。

    這是簡化實作，只採用「量縮止跌」這個方向的價量邏輯(手稿明確描述的版本)，
    沒有涵蓋「反轉K棒爆量」這種另一種常見但意義不同的價量確認方式(兩者是
    不同的解讀，量縮代表賣壓衰竭、爆量代表積極承接，這裡先只做前者)。
    資料不足時回傳None(不擋單，避免資料不足就整個判斷卡住)。
    """
    if len(candles) < lookback + 1:
        return None

    recent = candles[-(lookback + 1):-1]  # 不含最新那根，取前面lookback根當基準
    avg_volume = sum(c.get("volume", 0) for c in recent) / len(recent)
    if avg_volume <= 0:
        return None

    latest_volume = candles[-1].get("volume", 0)
    if latest_volume <= avg_volume * shrink_ratio:
        role = "賣壓" if direction == "bullish" else "買盤"
        return f"成交量較近期平均萎縮({latest_volume:.1f} vs 均量{avg_volume:.1f})，{role}可能已經釋放"
    return None


def generate_signal_resonance_fvg(candles, emas, rsi, macd, fvgs, choppiness_index, current_price,
                                   chop_threshold=61.8, rsi_oversold=30, rsi_overbought=70,
                                   min_conditions_met=4):
    """
    多條件共振策略主入口。四個子條件(RSI極端值、EMA/FVG支撐壓力、價格行為止跌、
    成交量萎縮)各自獨立檢查，符合min_conditions_met個(含)以上才給"訊號"。

    min_conditions_met=4(預設)等於原本的嚴格AND邏輯(全部4項都要符合)，
    調低到3或2可以放寬門檻——這是使用者用真實歷史資料回測後的決定：嚴格
    AND邏輯訊號量太少(30天只有25筆)，但獲利因子/勝率的數字看起來不錯，
    值得試試看放寬門檻、用回測比較「訊號量 vs 品質」的取捨，而不是直接
    改成OR(任一條件就觸發，會太鬆、容易混入雜訊訊號)。

    重要行為差異：min_conditions_met=4時，RSI仍然是事實上的必要條件(4項
    全部要符合，RSI當然也要符合)，跟改版前行為完全一致；但min_conditions_met
    調低後，RSI只是4票中的1票，不再是唯一決定「要不要評估這個方向」的
    硬性前提——例如門檻設3時，即使RSI沒有達到超買超賣，只要EMA/FVG+價格
    行為+成交量這3項都符合，一樣可以觸發訊號。這是刻意放寬的設計，用
    min_conditions_met控制放寬的程度。

    濾網防禦(choppiness_index)不算在這四個條件裡，是獨立的硬性前提——判定
    為震盪盤時直接拒絕，不管其他條件符合幾個都不給訊號。

    MACD目前只當參考資訊放進理由文字裡，不算在四個計分條件裡(使用者明確
    決定先不補這個缺口，見README修正記錄)。
    """
    if choppiness_index is not None and choppiness_index >= chop_threshold:
        return {
            "stage": "中性", "direction": None,
            "chan": {"bias": "neutral", "strength": "none", "reason": f"Choppiness Index({choppiness_index:.1f})過高，判定為震盪盤，濾網暫停判斷"},
            "profile": {"bias": "neutral", "strength": "none", "reason": "震盪盤濾網已阻擋，不評估其他條件"},
            "current_price": current_price,
        }

    latest_candle = candles[-1] if candles else None
    prev_candle = candles[-2] if len(candles) >= 2 else None

    for direction in ("bullish", "bearish"):
        reasons = []

        if direction == "bullish" and rsi is not None and rsi < rsi_oversold:
            reasons.append(("rsi", f"RSI({rsi:.1f})處於超賣區"))
        elif direction == "bearish" and rsi is not None and rsi > rsi_overbought:
            reasons.append(("rsi", f"RSI({rsi:.1f})處於超買區"))

        level_reason = (
            _find_ema_support_or_resistance(current_price, emas, direction)
            or _find_fvg_zone(current_price, fvgs, direction)
        )
        if level_reason:
            reasons.append(("level", level_reason))

        price_action_reason = _has_stabilizing_price_action(latest_candle, prev_candle, direction)
        if price_action_reason:
            reasons.append(("price_action", price_action_reason))

        volume_reason = _has_volume_confirmation(candles, direction)
        if volume_reason:
            reasons.append(("volume", volume_reason))

        if len(reasons) >= min_conditions_met:
            macd_note = (
                f"；MACD柱狀圖{macd.get('histogram'):.4f}(參考資訊，非計分條件)"
                if macd and macd.get("histogram") is not None else ""
            )
            rsi_text = next((r for k, r in reasons if k == "rsi"), "RSI未達極端值(此項未計分)")
            other_reasons = "；".join(r for k, r in reasons if k != "rsi") or "無其他條件符合"
            return {
                "stage": "訊號", "direction": direction,
                "chan": {"bias": direction, "strength": "strong", "reason": f"{rsi_text}{macd_note}"},
                "profile": {"bias": direction, "strength": "strong", "reason": f"符合{len(reasons)}/4項條件(門檻{min_conditions_met})：{other_reasons}"},
                "current_price": current_price,
                "conditions_met": len(reasons),
            }

    return {
        "stage": "中性", "direction": None,
        "chan": {"bias": "neutral", "strength": "none", "reason": f"兩個方向都未達到{min_conditions_met}/4項條件門檻"},
        "profile": {"bias": "neutral", "strength": "none", "reason": "共振策略要求至少符合設定的條件數才給訊號"},
        "current_price": current_price,
    }
