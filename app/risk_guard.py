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


def _one_r(engine, trade):
    """一次完整停損的點數：設定的停損點數與那筆「進場到停損價」的距離取較大者(保守)。"""
    s = settings_module.get_settings(engine_id=engine.engine_id)
    base = float(s.get("paper_sl_points") or 0)
    try:
        dist = abs(float(trade.get("entry_price")) - float(trade.get("sl_price")))
    except (TypeError, ValueError):
        dist = 0.0
    return max(base, dist)


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
        if exit_dt.date() != today:
            continue
        if isinstance(t.get("pnl_points"), (int, float)):
            daily_pnl_points += t["pnl_points"]   # 推估的損益(r45 pnl_estimated)是數字，照樣算進來
        else:
            # 損益未知：斷路器當成一次完整停損(-1R，第8條r50)——只會更早停、不會更晚。
            # 以前是不加：成交價都查不到的那天，每日虧損上限永遠不會觸發
            daily_pnl_points -= _one_r(engine, t)

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
        pnl = t.get("pnl_points")
        if pnl is None:
            count += 1  # 損益未知：斷路器當成一次虧損(第8條r50)，只會更早停
            continue
        if pnl <= 0:
            count += 1
        else:
            break
    return count


def get_unknown_pnl_count(engine):
    """最近平倉裡損益未知的筆數：每日損益與連續虧損都沒算到它們，要在風控狀態上講明。"""
    if db.is_enabled():
        trades = db.get_closed_paper_trades(limit=50, engine_id=engine.engine_id)
    else:
        trades = list(engine._closed_trades_memory)
    return sum(1 for t in trades if t.get("pnl_points") is None)


def check_spread_edge(sl_points, spread_points, min_ratio):
    """
    檢查這筆單的停損距離，相對於假設的買賣價差成本，是不是有足夠的安全邊際。

    使用者實測發現，正式環境的買賣價差可能高達十幾點，如果剛好遇到ATR偏低
    的時段，價差甚至可能超過整個停損距離本身——代表就算訊號完全正確、只是
    正常觸及停損出場，實際虧損也會被價差放大將近一倍，獲利的單也要先跨過
    這道成本門檻才有得賺。這道閘門確保只有「停損距離夠大、價差成本佔比
    夠小」的訊號才會真的送出真實下單；ATR偏低、價差相對停損距離過高的
    時段自動跳過真實下單(模擬單照常記錄、不受影響，用來累積策略本身的
    訊號品質資料)，等波動度回升、安全邊際重新充足時才恢復真實下單
    (修正記錄見README)。

    這不是在幫ATR停損「補償」價差——停損距離本身完全不會被這道檢查修改，
    只是「這筆訊號的當下條件划不划算送真錢」的獨立判斷，不划算就先跳過，
    不會扭曲停損邏輯本身。

    回傳(allowed, reason)。spread_points或min_ratio任一個是0/None時，視為
    關閉這道檢查(使用者還沒設定/觀察到足夠的真實價差資料前，不該用0去
    卡死所有交易)。
    """
    if not spread_points or spread_points <= 0 or not min_ratio or min_ratio <= 0:
        return True, None

    required_sl = spread_points * min_ratio
    if sl_points < required_sl:
        return False, (
            f"停損距離{sl_points:.2f}points對假設價差{spread_points:.2f}points的"
            f"安全邊際不足(門檻要求至少{min_ratio}倍，即{required_sl:.2f}points)，"
            f"目前市場波動度可能偏低，暫停真實下單"
        )
    return True, None


def check_current_spread(bid, ask, max_spread_points):
    """
    檢查決策當下的真實買賣價差(不是假設值，是即時盤口bid/ask)是不是異常放大。

    跟check_spread_edge()的差異：那個是拿「使用者假設的固定價差值」去跟ATR
    停損距離比，是一個間接的代理指標(用波動度去猜價差划不划算)；這個是
    直接查當下的真實盤口，抓「這一刻價差真的變寬了」的異常時刻——例如
    低流動性時段、大單剛好吃掉盤口深度、重大消息公布前後，這種瞬間價差
    可能跳高，光看ATR/假設值判斷不出來，只有查當下真實報價才抓得到
    (修正記錄見README，使用者實際觀察到平時價差穩定在0.01points左右，
    但某幾筆單出現+6.68points的滑點，想在下單前直接擋掉這種異常時刻)。

    這道檢查用的bid/ask，跟後續算「真正執行滑點」用的是同一份資料(決策
    當下從即時報價流拿到的)，不需要額外呼叫API，幾乎沒有額外成本。

    回傳(allowed, reason)。max_spread_points<=0時視為關閉這道檢查；沒有
    bid/ask資料時(例如報價還沒抓到)不會因此擋單，避免資料缺失就卡死交易。
    """
    if not max_spread_points or max_spread_points <= 0:
        return True, None
    if bid is None or ask is None:
        return True, None

    spread = ask - bid
    if spread > max_spread_points:
        return False, (
            f"當下買賣價差{spread:.2f}points，超過上限{max_spread_points:.2f}points，"
            f"可能是流動性瞬間變薄或即將有重大波動，暫停真實下單"
        )
    return True, None


def get_account_daily_pnl_usd(execution_account, quantity):
    """同一個幣安帳戶底下、所有綁定真實下單的引擎，今日已實現損益加總(USD)。"""
    from app.paper_trading import PAPER_TRADING_ENGINES  # 延遲import避免循環引用
    s = settings_module.get_settings()
    total = 0.0
    from app import execution as execution_module
    target = execution_module.physical_account_key(execution_account)
    for eng in PAPER_TRADING_ENGINES.values():
        if execution_module.physical_account_key(eng.execution_account) != target or not eng._is_execution_engine(s):
            continue
        total += get_daily_pnl_usd(eng, quantity)
    return total


# ---------------------------------------------------------------------------
# 手動緊急停止(修正記錄見README)：正式端dashboard的紅色按鈕。啟動後所有引擎
# 都不再送出新的真實開倉單(模擬單照常記錄)，既有部位的出場單不受影響——
# 停止「開新倉」而不是停止「管理部位」，才不會把已經在場上的單子晾著。
# ---------------------------------------------------------------------------
_manual_halt = {"active": False, "reason": None, "at": None, "by": None}


def set_manual_halt(active, reason=None):
    from datetime import datetime, timezone
    _manual_halt["active"] = bool(active)
    _manual_halt["reason"] = reason if active else None
    _manual_halt["at"] = datetime.now(timezone.utc).isoformat()
    return dict(_manual_halt)


def get_manual_halt():
    return dict(_manual_halt)


_history_fail = {"n": 0}


def _history_read_failed(err):
    _history_fail["n"] += 1
    n = _history_fail["n"]
    try:
        from app import alert_cadence
        from app.notifier import notifier
        if alert_cadence.should_alert(n):
            notifier.send_raw_message(f"⚠️ 風控讀不到平倉紀錄(資料庫)(第 {n} 次)\n錯誤：{err}\n暫停真實下單(模擬單照常)，每次要下單時重試")
    except Exception:
        pass


def _history_read_ok():
    n = _history_fail["n"]
    _history_fail["n"] = 0
    if n:
        try:
            from app.notifier import notifier
            notifier.send_raw_message(f"✅ 風控讀得到平倉紀錄了(失敗 {n} 次後)，恢復真實下單")
        except Exception:
            pass


def check(engine, quantity, sl_points=None, bid=None, ask=None):
    """風控檢查(見 _check_inner)。擋下的是每日虧損／連續虧損、而裡面含損益未知的筆數時，講明(第8條r50)。"""
    allowed, reason, kind = _check_inner(engine, quantity, sl_points=sl_points, bid=bid, ask=ask)
    if not allowed and kind in ("daily_loss", "consecutive_loss"):
        n = get_unknown_pnl_count(engine)
        if n:
            reason = f"{reason}(含 {n} 筆損益未知，各以一次完整停損計)"
    return allowed, reason, kind


def _check_inner(engine, quantity, sl_points=None, bid=None, ask=None):
    """
    檢查這個引擎目前能不能送出新的真實開倉單。
    回傳 (allowed: bool, reason: str|None, reason_type: str|None)。allowed=False
    代表已達每日虧損上限、連續虧損上限、(提供sl_points時)價差安全邊際不足、
    或(提供bid/ask時)當下真實價差過大，這次不該送出真實下單(模擬單本身
    照常記錄，不受影響)。

    reason_type區分是哪一種原因擋下的("daily_loss"/"consecutive_loss"/
    "spread_edge"/"current_spread")，讓呼叫端可以分開處理：daily_loss/
    consecutive_loss是真正的風控斷路器，觸發時值得發一則獨立警示提醒
    使用者；spread_edge跟current_spread都是「這個時刻划不划算真的下單」的
    市場條件判斷，可能頻繁觸發，不該跟風控斷路器共用同一套「只提醒一次」
    的機制，不然可能會互相干擾(修正記錄見README)。

    sl_points：這筆單目前算出來的停損距離(points)，用來檢查是否有足夠的
    安全邊際覆蓋「假設的」買賣價差成本(execution_min_edge_ratio設定)。
    bid/ask：決策當下的即時真實盤口，用來檢查「當下實際」價差是否異常
    放大(execution_max_spread_points設定)，比sl_points那組更直接、即時。
    兩者都不提供的話都不會做對應的檢查(向後相容既有呼叫方式)。
    """
    # 讀不到平倉紀錄就不知道今天虧多少、連虧幾筆(第8條r47)：以前讀取失敗被當成「沒有交易」，
    # 斷路器永遠不會觸發。讀不到就暫停真實下單、照節奏推播、恢復通知
    if db.is_enabled():
        ok_hist, hist_err = db.load_closed_paper_trades(limit=1, engine_id=engine.engine_id)
        if not ok_hist:
            _history_read_failed(hist_err)
            return False, f"讀不到平倉紀錄(資料庫)，不知道今天虧多少、連虧幾筆，暫停真實下單：{hist_err}", "history_unreadable"
        _history_read_ok()
    if _manual_halt["active"]:
        return False, f"手動緊急停止中({_manual_halt['reason'] or '未填原因'})，暫停所有新的真實開倉", "manual_halt"

    s = settings_module.get_settings()

    daily_pnl_usd = get_daily_pnl_usd(engine, quantity)
    daily_limit = s["execution_daily_loss_limit_usd"]
    if daily_pnl_usd <= -daily_limit:
        return False, f"今日已實現虧損 ${abs(daily_pnl_usd):.2f}，達到每日虧損上限 ${daily_limit:.2f}", "daily_loss"

    consecutive_losses = get_consecutive_losses(engine)
    max_consecutive = s["execution_max_consecutive_losses"]
    if consecutive_losses >= max_consecutive:
        return False, f"連續虧損 {consecutive_losses} 筆，達到連續虧損上限 {max_consecutive} 筆", "consecutive_loss"

    # 帳戶層級每日虧損(修正記錄見README)：兩個引擎共用同一個幣安帳戶時，帳戶
    # 實際承受的是兩邊加總。把「同一個execution_account」底下所有綁定真實下單
    # 的引擎今日已實現損益加總，達到上限就擋。0=不啟用。
    account_limit = s.get("execution_account_daily_loss_limit_usd") or 0
    if account_limit > 0:
        from app import execution as execution_module
        account_pnl = get_account_daily_pnl_usd(engine.execution_account, quantity)
        if account_pnl <= -account_limit:
            return False, (
                f"實體帳戶「{execution_module.physical_account_key(engine.execution_account)}」今日所有引擎合計已實現虧損 ${abs(account_pnl):.2f}，"
                f"達到帳戶層級每日虧損上限 ${account_limit:.2f}"
            ), "daily_loss"

    if sl_points is not None:
        edge_allowed, edge_reason = check_spread_edge(
            sl_points,
            s.get("execution_assumed_spread_points"),
            s.get("execution_min_edge_ratio"),
        )
        if not edge_allowed:
            return False, edge_reason, "spread_edge"

    if bid is not None and ask is not None:
        spread_allowed, spread_reason = check_current_spread(
            bid, ask, s.get("execution_max_spread_points"),
        )
        if not spread_allowed:
            return False, spread_reason, "current_spread"

    return True, None, None


def status(engine, quantity):
    """給dashboard顯示目前風控斷路器狀態用：今日損益、連續虧損筆數、是否已觸發。"""
    allowed, reason, _reason_type = check(engine, quantity)
    return {
        "daily_pnl_usd": round(get_daily_pnl_usd(engine, quantity), 2),
        "consecutive_losses": get_consecutive_losses(engine),
        "unknown_pnl_trades": get_unknown_pnl_count(engine),  # 上面兩個數字都沒算到這幾筆
        "tripped": not allowed,
        "reason": reason,
    }
