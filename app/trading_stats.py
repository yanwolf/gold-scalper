"""
績效統計與「達標門檻」評估。

被 paper_trading.py(即時模擬單) 和 backtest.py(歷史回測) 共用，
確保兩邊看到的績效指標定義完全一致(勝率怎麼算、獲利因子怎麼算、
最大回撤怎麼算)，不會有一邊統計口徑跟另一邊對不上的問題。

達標門檻的數值來源是 app/settings.py(可透過dashboard線上調整、有密碼保護)，
不再是這裡固定讀一次環境變數的常數，改參數不用重新部署就會生效。
"""


def compute_stats(trades):
    """
    輸入已平倉的模擬單清單(順序不拘，函式內部會自行按entry_time排序來算最大回撤)，
    回傳完整績效指標。trades裡每筆至少要有 entry_time, pnl_points, direction。
    """
    if not trades:
        return {
            "total_trades": 0, "win_count": 0, "loss_count": 0, "win_rate": 0.0,
            "total_pnl_points": 0.0, "avg_win_points": 0.0, "avg_loss_points": 0.0,
            "profit_factor": None, "max_drawdown_points": 0.0,
            "drawdown_peak_time": None, "drawdown_trough_time": None,
        }

    total = len(trades)
    wins = [t for t in trades if t.get("pnl_points") and t["pnl_points"] > 0]
    losses = [t for t in trades if t.get("pnl_points") is not None and t["pnl_points"] <= 0]

    total_pnl = sum(t["pnl_points"] for t in trades if t.get("pnl_points") is not None)
    gross_profit = sum(t["pnl_points"] for t in wins)
    gross_loss = abs(sum(t["pnl_points"] for t in losses))

    win_rate = (len(wins) / total * 100) if total > 0 else 0.0
    avg_win = (gross_profit / len(wins)) if wins else 0.0
    avg_loss = (gross_loss / len(losses)) if losses else 0.0
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)

    # 最大回撤：把交易依時間排序，算累積損益曲線，抓「從歷史高點回落最多」的那一段。
    # 這個數字代表的是曲線上「峰值到之後最低點」的落差，不是總虧損，兩者是完全不同的
    # 概念——如果策略曾經帳面大賺一段、後來又把獲利吐回去，最大回撤可以遠大於最終總損益
    # (例如帳面衝到+130又跌回-30，回撤就有160，但最終總損益可能只是-33)。
    # 額外記錄峰值/谷底發生在哪一筆、什麼時間，方便診斷這段回撤到底是哪個時期造成的，
    # 不然只看到一個數字，沒辦法判斷是「一直穩定小賠累積」還是「曾經賺很多後來吐回去」。
    sorted_trades = sorted(
        [t for t in trades if t.get("entry_time") and t.get("pnl_points") is not None],
        key=lambda t: t["entry_time"],
    )
    cumulative = 0.0
    peak = 0.0
    peak_time = None
    max_drawdown = 0.0
    drawdown_peak_time = None
    drawdown_trough_time = None
    for t in sorted_trades:
        cumulative += t["pnl_points"]
        if cumulative > peak:
            peak = cumulative
            peak_time = t.get("exit_time") or t.get("entry_time")
        drawdown = peak - cumulative
        if drawdown > max_drawdown:
            max_drawdown = drawdown
            drawdown_peak_time = peak_time
            drawdown_trough_time = t.get("exit_time") or t.get("entry_time")

    return {
        "total_trades": total,
        "win_count": len(wins),
        "loss_count": len(losses),
        "win_rate": round(win_rate, 1),
        "total_pnl_points": round(total_pnl, 2),
        "avg_win_points": round(avg_win, 2),
        "avg_loss_points": round(avg_loss, 2),
        "profit_factor": round(profit_factor, 2) if profit_factor != float("inf") else None,
        "max_drawdown_points": round(max_drawdown, 2),
        "drawdown_peak_time": drawdown_peak_time,     # 回撤起算的高點發生在什麼時候
        "drawdown_trough_time": drawdown_trough_time,  # 回撤最深的谷底發生在什麼時候
    }


def assess_readiness(stats):
    """
    對照「達標門檻」評估目前的績效統計，回傳是否達標、以及每一項門檻各自的通過狀況，
    給dashboard和決策參考用。這不是保證獲利的保證，只是避免樣本太少/績效太差就衝去接真錢。

    門檻值即時從 app/settings.py 讀取(而不是固定常數)，這樣使用者在dashboard調整過
    達標門檻設定後，這裡馬上就會用新的門檻評估，不用重新部署。
    """
    from app import settings
    s = settings.get_settings()

    checks = [
        {
            "label": "樣本數",
            "pass": stats["total_trades"] >= s["readiness_min_trades"],
            "actual": stats["total_trades"],
            "threshold": s["readiness_min_trades"],
        },
        {
            "label": "勝率",
            "pass": stats["win_rate"] >= s["readiness_min_win_rate"],
            "actual": stats["win_rate"],
            "threshold": s["readiness_min_win_rate"],
        },
        {
            "label": "獲利因子",
            "pass": (stats["profit_factor"] or 0) >= s["readiness_min_profit_factor"],
            "actual": stats["profit_factor"],
            "threshold": s["readiness_min_profit_factor"],
        },
        {
            "label": "最大回撤(points)",
            "pass": stats["max_drawdown_points"] <= s["readiness_max_drawdown_points"],
            "actual": stats["max_drawdown_points"],
            "threshold": s["readiness_max_drawdown_points"],
        },
    ]

    all_pass = all(c["pass"] for c in checks)

    return {
        "ready": all_pass,
        "checks": checks,
    }
