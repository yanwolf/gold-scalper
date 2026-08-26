"""
純交易邏輯核心：開倉/移動停損/出場判斷。

刻意設計成「純函式」(不碰資料庫、不碰背景執行緒、不呼叫外部API)，
理由：這套規則要同時給「即時模擬單追蹤」(paper_trading.py)和
「歷史回測」(backtest.py)使用，如果兩邊各自寫一份邏輯，未來改規則
很容易改一邊忘了改另一邊，導致回測結果跟即時模擬單的行為對不上。
統一抽成這個模組後，兩邊永遠保證用同一套規則。

這裡的函式都是「輸入狀態 -> 輸出新狀態」，不修改任何外部狀態，
方便單元測試、也方便在回測的迴圈裡快速重複呼叫。
"""


def open_position(direction, current_price, entry_time, sl_points, chan_reason=None, profile_reason=None):
    """
    開一筆新倉位。sl_points是初始保護停損的距離(多單=進場價-sl_points，空單反向)。
    peak_price一開始等於進場價，trailing_active一開始是False(移動停損尚未啟動)。
    pending_reversal_direction/count是給訊號反轉「連續確認」機制用的狀態，
    一開始都是空的(還沒看過任何反向訊號)。
    """
    sl_price = current_price - sl_points if direction == "bullish" else current_price + sl_points
    return {
        "direction": direction,
        "entry_price": current_price,
        "entry_time": entry_time,
        "sl_price": sl_price,
        "peak_price": current_price,
        "trailing_active": False,
        "chan_reason": chan_reason,
        "profile_reason": profile_reason,
        "pending_reversal_direction": None,
        "pending_reversal_count": 0,
    }


def update_trailing_stop(position, current_price, trail_trigger_points, trail_distance_points):
    """
    更新峰值價格，判斷要不要啟動移動停損、以及停損要不要往有利方向移動。
    直接修改傳入的position字典(呼叫端要自己決定要不要複製)，回傳是否有變化。
    停損只會往有利方向移動，不會因為價格反彈而放寬(單向棘輪機制)。
    """
    direction = position["direction"]
    changed = False

    if direction == "bullish":
        if current_price > position["peak_price"]:
            position["peak_price"] = current_price
            changed = True

        profit = position["peak_price"] - position["entry_price"]
        if not position["trailing_active"] and profit >= trail_trigger_points:
            position["trailing_active"] = True
            changed = True

        if position["trailing_active"]:
            new_stop = position["peak_price"] - trail_distance_points
            if new_stop > position["sl_price"]:
                position["sl_price"] = new_stop
                changed = True
    else:
        if current_price < position["peak_price"]:
            position["peak_price"] = current_price
            changed = True

        profit = position["entry_price"] - position["peak_price"]
        if not position["trailing_active"] and profit >= trail_trigger_points:
            position["trailing_active"] = True
            changed = True

        if position["trailing_active"]:
            new_stop = position["peak_price"] + trail_distance_points
            if new_stop < position["sl_price"]:
                position["sl_price"] = new_stop
                changed = True

    return changed


def check_exit(position, current_price, signal_stage, signal_direction, reversal_confirm_count=2, current_ema9=None):
    """
    出場規則：觸及停損(初始或移動後，價格觸發，不需要確認、立刻出場)、
    9EMA動態防守(見下方說明，立刻出場)、或 訊號反轉(需要連續
    reversal_confirm_count次檢查都看到同一個反向訊號才算數，避免訊號瞬間
    閃爍一次就把倉位洗出場)，先到先出。

    9EMA動態防守：current_ema9有提供、且移動停損已經啟動(trailing_active)時，
    如果價格有效跌破(多單)/突破(空單)9EMA，代表短線動能可能急轉，立刻出場
    鎖住獲利，不用等移動停損追上。只在trailing_active後才檢查，避免剛進場、
    還沒累積足夠獲利緩衝時就被9EMA的正常雜訊洗出場。這是resonance_fvg實驗性
    策略專用的出場加強，current_ema9預設None時完全不影響原本的行為，
    現有的chan_profile策略(呼叫端不傳這個參數)不受任何影響。

    「連續確認」用position裡的pending_reversal_direction/pending_reversal_count
    追蹤跨檢查週期的狀態：
    - 看到反向訊號且跟上次記錄的方向一樣 -> 計數+1，達標才真的出場
    - 看到反向訊號但跟上次記錄的方向不同(訊號又換了方向) -> 重新從1開始算
    - 沒看到反向訊號(訊號消失、變中性、或轉回同方向) -> 計數歸零，反轉訊號streak中斷

    這個函式會直接修改傳入的position字典(維護pending_reversal狀態)，
    回傳出場原因字串，沒觸發任何條件則回傳None。
    """
    direction = position["direction"]

    if direction == "bullish":
        if current_price <= position["sl_price"]:
            return "觸及移動停損" if position["trailing_active"] else "觸及停損"
    else:
        if current_price >= position["sl_price"]:
            return "觸及移動停損" if position["trailing_active"] else "觸及停損"

    if position["trailing_active"] and current_ema9 is not None:
        if direction == "bullish" and current_price < current_ema9:
            return "跌破9EMA動態防守"
        elif direction == "bearish" and current_price > current_ema9:
            return "突破9EMA動態防守"

    is_opposite_signal = signal_stage == "訊號" and signal_direction and signal_direction != direction

    if is_opposite_signal:
        if position.get("pending_reversal_direction") == signal_direction:
            position["pending_reversal_count"] = position.get("pending_reversal_count", 0) + 1
        else:
            position["pending_reversal_direction"] = signal_direction
            position["pending_reversal_count"] = 1

        if position["pending_reversal_count"] >= reversal_confirm_count:
            return "訊號反轉"
    else:
        # 反向訊號streak中斷(訊號消失、變中性、或轉回原方向)，重新歸零
        position["pending_reversal_direction"] = None
        position["pending_reversal_count"] = 0

    return None


def close_position(position, exit_price, exit_reason, exit_time):
    """計算這筆倉位的損益(points)，回傳完整的已平倉紀錄(不修改傳入的position)。"""
    direction = position["direction"]
    pnl_points = (
        (exit_price - position["entry_price"]) if direction == "bullish"
        else (position["entry_price"] - exit_price)
    )
    return {
        **position,
        "exit_price": exit_price,
        "exit_time": exit_time,
        "exit_reason": exit_reason,
        "pnl_points": pnl_points,
    }
