"""
風控斷路器：每日虧損上限 + 連續虧損上限。

用途：只影響「要不要送出新的真實開倉單」，不影響模擬單本身的追蹤——模擬單
繼續正常記錄每一筆交易(這是判斷策略好壞的主要依據)，斷路器只是在「已經
虧到設定的門檻」時，暫停真實下單去累積新的風險。已經開的真實部位該停損/
該出場照樣正常出場，不受斷路器影響(斷路器只擋「新增風險」的動作，不擋
「降低風險」的動作)。

這是業界最基本、最普遍的資金管理常識：再準的策略也會遇到連續不順的一段，
沒有這道煞車，運氣差的一天可能會把好幾天的獲利吐光。

判斷依據用「跟真實下單綁定的那個模擬單引擎」的損益紀錄(乘上目前設定的
execution_quantity換算成美元)，不是直接查幣安的已實現損益——這樣可以重用
既有的、已經驗證過的損益追蹤邏輯，不用額外對接幣安的損益查詢API，兩者
在執行成功的情況下應該非常接近。
"""

import logging
from datetime import datetime, timezone

from app import db
from app import settings as settings_module

logger = logging.getLogger("risk_guard")


def get_daily_pnl_usd(engine, quantity):
    """
    這個引擎今天(UTC日)已平倉損益，換算成美元(乘以quantity)。
    用UTC日界線判斷「今天」，跟系統裡其他時間戳記的時區慣例一致。
    """
    today = datetime.now(timezone.utc).date()

    if db.is_enabled():
        trades = db.get_closed_paper_trades(limit=500, engine_id=engine.engine_id)
    else:
        trades = list(engine._closed_trades_memory)

    daily_pnl_points = 0.0
    for t in trades:
        exit_time_str = t.get("exit_time")
        if not exit_time_str:
            continue
        try:
            exit_dt = datetime.fromisoformat(exit_time_str)
        except ValueError:
            continue
        if exit_dt.date() == today:
            daily_pnl_points += t.get("pnl_points") or 0.0

    return daily_pnl_points * quantity


def get_consecutive_losses(engine):
    """
    最近連續幾筆虧損(依平倉時間由新到舊看，遇到獲利就停止累計)。
    盈虧兩平(pnl_points恰好等於0)也算在虧損裡，保守判斷。
    """
    if db.is_enabled():
        trades = db.get_closed_paper_trades(limit=50, engine_id=engine.engine_id)
    else:
        trades = list(engine._closed_trades_memory)[::-1]  # 記憶體版本是舊到新存的，反過來才是新到舊

    count = 0
    for t in trades:
        pnl = t.get("pnl_points") or 0.0
        if pnl <= 0:
            count += 1
        else:
            break
    return count


def check(engine, quantity):
    """
    檢查這個引擎目前能不能送出新的真實開倉單。
    回傳 (allowed: bool, reason: str|None)。allowed=False代表已達每日虧損上限
    或連續虧損上限，這次不該送出真實下單(模擬單本身照常記錄，不受影響)。
    """
    s = settings_module.get_settings()

    daily_pnl_usd = get_daily_pnl_usd(engine, quantity)
    daily_limit = s["execution_daily_loss_limit_usd"]
    if daily_pnl_usd <= -daily_limit:
        return False, f"今日已實現虧損 ${abs(daily_pnl_usd):.2f}，達到每日虧損上限 ${daily_limit:.2f}"

    consecutive_losses = get_consecutive_losses(engine)
    max_consecutive = s["execution_max_consecutive_losses"]
    if consecutive_losses >= max_consecutive:
        return False, f"連續虧損 {consecutive_losses} 筆，達到連續虧損上限 {max_consecutive} 筆"

    return True, None


def status(engine, quantity):
    """給dashboard顯示目前風控斷路器狀態用：今日損益、連續虧損筆數、是否已觸發。"""
    allowed, reason = check(engine, quantity)
    return {
        "daily_pnl_usd": round(get_daily_pnl_usd(engine, quantity), 2),
        "consecutive_losses": get_consecutive_losses(engine),
        "tripped": not allowed,
        "reason": reason,
    }
