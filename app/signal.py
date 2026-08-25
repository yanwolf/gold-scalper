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
