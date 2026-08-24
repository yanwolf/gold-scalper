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


def check_exit(position, current_price, signal_stage, signal_direction):
    """
    出場規則：觸及停損(初始或移動後) 或 訊號反轉，先到先出。
    回傳出場原因字串，沒觸發任何條件則回傳None。
    """
    direction = position["direction"]

    if direction == "bullish":
        if current_price <= position["sl_price"]:
            return "觸及移動停損" if position["trailing_active"] else "觸及停損"
    else:
        if current_price >= position["sl_price"]:
            return "觸及移動停損" if position["trailing_active"] else "觸及停損"

    if signal_stage == "訊號" and signal_direction and signal_direction != direction:
        return "訊號反轉"

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
