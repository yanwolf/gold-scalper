"""
交易場所的成本設定(r111)：同一批交易，用另一個場所的真實成本重算績效。

背景：幣安 XAUUSDT 每筆一進一出約 3.4 點(手續費按金額收)，15 分K 纏論與布林觸軌扣完都是負的。
MT5 的 CFD 券商(使用者有 Pepperstone、Vantage 帳戶，2026-10-20 截圖)：
  - Pepperstone XAUUSD：只收點差，截圖 22:00 時約 0.23 點；
  - Vantage XAUUSD+：點差約 0.11 ＋ 佣金每手每邊 3 USD(1 手 100 盎司 → 每盎司一進一出 0.06)，約 0.17 點；
  - 點差浮動：換日(紐約 17:00，台灣清晨 5～6 點)與重大數據時常放大好幾倍，所以預設保守用 0.4 點。
  - 隔夜庫存費(每手每晚、點模式)：Pepperstone 買 −81.22／賣 +28.42、Vantage 買 −77.54／賣 +32.15，
    換算每盎司約做多 −0.80、做空 +0.30 點；週三收 3 倍。
這裡只是**重算統計**(回測、引擎績效)，不改下單、不改任何正式設定。數字要換的話改這個檔、或回測時填欄位。
"""
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    NY = ZoneInfo("America/New_York")
except Exception:   # 沒有時區資料時退回固定 UTC-5(冬令)：換日時間會差一小時，只影響邊界上的單
    NY = timezone(timedelta(hours=-5))

MT5 = {
    "key": "mt5",
    "label": "MT5 券商(Pepperstone／Vantage 估)",
    "cost_points": 0.4,          # 每筆一進一出：點差＋佣金，保守估(截圖時 0.17～0.23)
    "swap_long": -0.80,          # 每盎司每晚(點)：做多付錢
    "swap_short": 0.30,          # 做空收錢
    "rollover_hour_ny": 17,      # 紐約時間 17:00 換日
    "triple_weekday": 2,         # 週三(星期一=0)收 3 倍
}


def rollovers(entry_iso, exit_iso, profile=MT5):
    """
    持倉期間跨過幾次「換日」(紐約 17:00)，週三那次算 3 次。進出場時間讀不懂就回 0(不猜)。
    在換日那一刻之前進場、之後出場才算跨過(剛好在換日那一刻出場不算)。
    """
    try:
        a = datetime.fromisoformat(str(entry_iso))
        b = datetime.fromisoformat(str(exit_iso))
    except (TypeError, ValueError):
        return 0
    if a.tzinfo is None:
        a = a.replace(tzinfo=timezone.utc)
    if b.tzinfo is None:
        b = b.replace(tzinfo=timezone.utc)
    if b <= a:
        return 0
    h = int(profile["rollover_hour_ny"])
    local = a.astimezone(NY)
    roll = local.replace(hour=h, minute=0, second=0, microsecond=0)
    if roll <= local:
        roll += timedelta(days=1)
    n = 0
    while roll < b.astimezone(NY):
        if roll.weekday() < 5:   # 週末不換日(週五 17:00 之後休市到週日)
            n += 3 if roll.weekday() == int(profile["triple_weekday"]) else 1
        roll = (roll + timedelta(days=1)).replace(hour=h)
    return n


def swap_points(trade, profile=MT5):
    """這筆交易的庫存費(點，每盎司)：做多是負的、做空是正的。"""
    n = rollovers(trade.get("entry_time"), trade.get("exit_time"), profile)
    per = profile["swap_long"] if trade.get("direction") == "bullish" else profile["swap_short"]
    return round(n * per, 4)


def adjust(trades, profile=MT5, cost_points=None):
    """
    回傳 (調整後的交易清單, 明細)：每筆 pnl_points 減掉成本、加上庫存費。損益未知(None)的照原樣留著。
    明細：{cost_points, trades, cost_total, swap_total, nights}。
    """
    cost = float(profile["cost_points"] if cost_points is None else cost_points)
    out, swap_total, nights, counted = [], 0.0, 0, 0
    for t in trades or []:
        if t.get("pnl_points") is None:
            out.append(t)
            continue
        sw = swap_points(t, profile)
        n = rollovers(t.get("entry_time"), t.get("exit_time"), profile)
        t2 = dict(t)
        t2["pnl_points"] = t["pnl_points"] - cost + sw
        out.append(t2)
        swap_total += sw
        nights += n
        counted += 1
    return out, {"cost_points": cost, "trades": counted, "cost_total": round(cost * counted, 2),
                 "swap_total": round(swap_total, 2), "nights": nights}


def view(trades, profile=MT5, cost_points=None):
    """用這個場所的成本重算的績效摘要(給網頁與回測結果)。"""
    from app.trading_stats import compute_stats
    adjusted, detail = adjust(trades, profile, cost_points)
    st = compute_stats(adjusted)
    return {"label": profile["label"], "key": profile["key"], **detail,
            "total_pnl_points": st["total_pnl_points"], "profit_factor": st["profit_factor"],
            "win_rate": st["win_rate"], "max_drawdown_points": st["max_drawdown_points"],
            "total_trades": st["total_trades"]}
