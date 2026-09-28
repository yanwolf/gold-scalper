"""
績效統計與「達標門檻」評估。

被 paper_trading.py(即時模擬單) 和 backtest.py(歷史回測) 共用，
確保兩邊看到的績效指標定義完全一致(勝率怎麼算、獲利因子怎麼算、
最大回撤怎麼算)，不會有一邊統計口徑跟另一邊對不上的問題。

達標門檻的數值來源是 app/settings.py(可透過dashboard線上調整、有密碼保護)，
不再是這裡固定讀一次環境變數的常數，改參數不用重新部署就會生效。
"""

from datetime import datetime, timezone



def real_usd_summary(trades):
    """
    真實下單的USDT損益加總(第8條r28「估算也不能把未知包裝成已知」)：只用真實成交價算出來的
    real_pnl_usd。缺成交價的真實單記「未知」另外計數；純模擬單不算(那不是真錢)。
    以前網頁的總損益USDT是「全部模擬點數×張數」，連沒真的下過單的交易都算進去。
    """
    # 使用者決定(2026-09-22)：真實成交價查不到時用推估值(點數×張數)照算，但分開計數、網頁標「估」
    num = lambda v: isinstance(v, (int, float))
    real = [t for t in trades if t.get("real_open_executed")]
    known = [t["real_pnl_usd"] for t in real if num(t.get("real_pnl_usd"))]
    est = [t["real_pnl_usd_est"] for t in real if not num(t.get("real_pnl_usd")) and num(t.get("real_pnl_usd_est"))]
    unknown = len(real) - len(known) - len(est)
    return {"total": round(sum(known) + sum(est), 2), "known": len(known), "estimated": len(est),
            "estimated_total": round(sum(est), 2), "unknown": unknown}


def compute_stats(trades, spread_cost_points=0.0):
    """
    輸入已平倉的模擬單清單(順序不拘，函式內部會自行按entry_time排序來算最大回撤)，
    回傳完整績效指標。trades裡每筆至少要有 entry_time, pnl_points, direction。

    spread_cost_points：每筆交易假設要扣掉的買賣價差成本(points，預設0代表
    不調整，維持原本行為)。市價單開倉+平倉各會吃到大約半個價差，兩者加
    起來大約等於一個完整價差，這個成本不管這筆單本身賺賠都一定會發生——
    這跟ATR停損距離是兩件完全不同的事：停損是「價格真的走錯方向時的防線」，
    價差成本是「不管對錯、每筆單都躲不掉的固定交易成本」，不該混在一起，
    也不該讓停損變寬去「補償」它(那只會改變風險，不會抵銷成本)。這裡讓
    呼叫端可以另外用這個參數重新算一次「扣掉真實交易成本後」的統計數字，
    才能誠實評估策略扣掉價差後還剩不剩得下獲利(修正記錄見README)。
    """
    if not trades:
        return {
            "total_trades": 0, "win_count": 0, "loss_count": 0, "win_rate": 0.0,
            "total_pnl_points": 0.0, "avg_win_points": 0.0, "avg_loss_points": 0.0,
            "profit_factor": None, "max_drawdown_points": 0.0,
            "drawdown_peak_time": None, "drawdown_trough_time": None,
        }

    if spread_cost_points:
        adjusted = []
        for t in trades:
            if t.get("pnl_points") is None:
                adjusted.append(t)
                continue
            t2 = dict(t)
            t2["pnl_points"] = t["pnl_points"] - spread_cost_points
            adjusted.append(t2)
        trades = adjusted

    total = len(trades)
    # 損益未知(r24起結帳遇缺欄位會記成None)不算勝負、另外計數(第8條r27)：以前勝率的分母包含它們，
    # 等於把未知算成虧損、勝率被拉低
    known_total = sum(1 for t in trades if t.get("pnl_points") is not None)
    unknown_pnl_trades = total - known_total
    wins = [t for t in trades if t.get("pnl_points") and t["pnl_points"] > 0]
    losses = [t for t in trades if t.get("pnl_points") is not None and t["pnl_points"] <= 0]

    total_pnl = sum(t["pnl_points"] for t in trades if t.get("pnl_points") is not None)
    gross_profit = sum(t["pnl_points"] for t in wins)
    gross_loss = abs(sum(t["pnl_points"] for t in losses))

    win_rate = (len(wins) / known_total * 100) if known_total > 0 else 0.0
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
        "unknown_pnl_trades": unknown_pnl_trades,
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


def compute_slippage_impact(trades, tail_threshold=3.0):
    """
    用每筆交易「實際存下來」的開倉+平倉真正執行滑點，算滑點對績效的真實影響
    (修正記錄見README)。「扣掉假設價差成本」那組統計是把成本攤成固定值，但
    15分K的實際損失結構是肥尾——大多數交易滑點在±1點內，少數幾筆(+12、+38)
    一筆吃掉好幾筆利潤。用固定假設值會失真，所以這裡直接用真實資料：

    - total_cost / avg_cost / max_cost：累積、平均、最大單筆滑點成本(正=不利)
    - tail_count / tail_cost / tail_share：滑點超過tail_threshold的筆數、合計、
      佔總成本比例——這個數字直接回答「問題是常態成本還是肥尾」：若少數幾筆
      佔了大半成本，該做的是擋極端值(時段過濾/限價封頂)，不是壓平均
    - adjusted_stats：把每筆pnl扣掉「該筆實際滑點」後重算的compute_stats，
      這才是真實執行下的獲利因子
    只計入有真實下單滑點資料的交易(純模擬的沒有這些欄位、視為0、不計入筆數)。
    """
    with_data = 0
    guarded = 0        # 修正後(有盤口檢查)的交易：entry_book_stale/exit_book_stale不是NULL
    stale_flagged = 0  # 修正後、且開倉或平倉當下盤口被判定過期(基準改用最後成交價)的交易
    per_trade_cost = []
    adjusted = []
    for t in trades:
        e = t.get("entry_slippage_points")
        x = t.get("exit_slippage_points")
        cost = (e or 0.0) + (x or 0.0)
        has = e is not None or x is not None
        if has:
            with_data += 1
            per_trade_cost.append(cost)
            ebs, xbs = t.get("entry_book_stale"), t.get("exit_book_stale")
            if ebs is not None or xbs is not None:
                guarded += 1
                if ebs or xbs:
                    stale_flagged += 1
        t2 = dict(t)
        if t2.get("pnl_points") is not None:
            t2["pnl_points"] = t2["pnl_points"] - cost
        adjusted.append(t2)

    total_cost = sum(per_trade_cost)
    adverse_cost = sum(c for c in per_trade_cost if c > 0)      # 所有不利滑點的總額(毛額)
    favorable_gain = -sum(c for c in per_trade_cost if c < 0)   # 所有有利滑點幫你多賺的總額
    tail = [c for c in per_trade_cost if c > tail_threshold]
    tail_cost = sum(tail)
    # 肥尾佔比的分母用「不利滑點毛額」而不是淨額——使用者實際資料出現過
    # 「5筆極端值61pt、其他18筆淨有利15pt、淨成本46pt」的情況，若用淨額
    # 當分母會顯示132.7%這種超過100%的數字，語意上正確(極端值比淨成本還多)
    # 但很難讀。改成「極端值佔所有不利滑點的比例」，並另外回傳有利抵銷額
    # (修正記錄見README)。
    return {
        "trades_with_data": with_data,
        "trades_total": len(trades),
        "guarded_count": guarded,
        "stale_flagged_count": stale_flagged,
        "total_cost": round(total_cost, 2),
        "adverse_cost": round(adverse_cost, 2),
        "favorable_gain": round(favorable_gain, 2),
        "avg_cost": round(total_cost / with_data, 3) if with_data else 0.0,
        "max_cost": round(max(per_trade_cost), 2) if per_trade_cost else 0.0,
        "favorable_count": sum(1 for c in per_trade_cost if c < 0),
        "tail_threshold": tail_threshold,
        "tail_count": len(tail),
        "tail_cost": round(tail_cost, 2),
        "tail_share": round(tail_cost / adverse_cost * 100, 1) if adverse_cost > 0 else 0.0,
        "adjusted_stats": compute_stats(adjusted),
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


# ---------------------------------------------------------------------------
# 日線SMC結構分組(r89)：開倉時記下的日線方向(daily_smc.tag_for_trade)跟這筆單的方向比對
# ---------------------------------------------------------------------------
DAILY_SMC_GROUPS = (
    ("aligned", "順日線結構"),
    ("against", "逆日線結構"),
    ("unknown", "日線未知"),
)


def daily_smc_alignment(tag, direction):
    """'aligned'／'against'／'unknown'。沒有標記、方向未定、資料過期都算 unknown(不硬猜)。"""
    if not isinstance(tag, dict) or not tag.get("bias") or tag.get("stale") or direction not in ("bullish", "bearish"):
        return "unknown"
    return "aligned" if tag["bias"] == direction else "against"


def compute_daily_smc_breakdown(trades):
    """
    依開倉時的日線結構把交易分成 順／逆／未知 三組，各算一次績效(跟 compute_stats 同一套口徑)。
    即時模擬單與回測共用。r89 以前的交易沒有標記，一律在「未知」那組。
    """
    groups = {key: [] for key, _ in DAILY_SMC_GROUPS}
    for t in trades or []:
        groups[daily_smc_alignment(t.get("daily_smc"), t.get("direction"))].append(t)
    out = []
    for key, label in DAILY_SMC_GROUPS:
        st = compute_stats(groups[key])
        out.append({
            "key": key, "label": label,
            "total_trades": st["total_trades"], "win_rate": st["win_rate"],
            "total_pnl_points": st["total_pnl_points"], "profit_factor": st["profit_factor"],
            "profit_factor_infinite": st["profit_factor"] is None and st["total_trades"] > 0,   # 全勝時 compute_stats 回 None
            "max_drawdown_points": st["max_drawdown_points"],
            "confirmed_count": sum(1 for t in groups[key] if isinstance(t.get("daily_smc"), dict)
                                   and t["daily_smc"].get("confirmed")),
        })
    return out


# ---------------------------------------------------------------------------
# 出場後多快再進場(r93)：6 月回撤裡看到「大賺出場後立刻在更低處再追、隨即被反彈掃掉」，
# 這裡把所有交易依「上一筆的結果＋間隔多久」分組，看這個模式在整段資料裡是不是普遍成立
# ---------------------------------------------------------------------------
REENTRY_WINDOWS_HOURS = (1, 3, 6)


def _to_dt(v):
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if isinstance(v, str) and v:
        try:
            d = datetime.fromisoformat(v.replace("Z", "+00:00"))
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def compute_reentry_breakdown(trades, windows_hours=REENTRY_WINDOWS_HOURS):
    """
    依開倉時間排序，每一筆看「上一筆」：上一筆賺還是賠、上一筆出場到這一筆進場隔了多久。
    回傳的分組(同一筆可能同時在 ≤1h、≤3h、≤6h 裡，是「以內」的累計)：
      after_win_{h}h  ：上一筆獲利出場後 h 小時內再進場
      after_loss_{h}h ：上一筆虧損出場後 h 小時內再進場
      rest            ：上一筆出場超過最長視窗才進場，或是第一筆
    另外 same_dir_ratio：該組裡跟上一筆同方向的比例(追同一波的程度)。
    時間讀不懂的交易略過(不猜)，略過幾筆放在 skipped。
    """
    rows = []
    for t in trades or []:
        et, xt = _to_dt(t.get("entry_time")), _to_dt(t.get("exit_time"))
        if et is None or xt is None:
            continue
        rows.append((et, xt, t))
    skipped = len(trades or []) - len(rows)
    rows.sort(key=lambda r: r[0])
    longest = max(windows_hours) if windows_hours else 0
    groups = {}
    for h in windows_hours:
        groups[f"after_win_{h}h"] = []
        groups[f"after_loss_{h}h"] = []
    groups["rest"] = []
    same_dir = {k: 0 for k in groups}
    for i, (et, _xt, t) in enumerate(rows):
        prev = rows[i - 1] if i > 0 else None
        gap_h = (et - prev[1]).total_seconds() / 3600 if prev else None
        if prev is None or gap_h is None or gap_h < 0 or gap_h > longest:
            groups["rest"].append(t)
            continue
        prev_pnl = prev[2].get("pnl_points")
        kind = "after_win" if (prev_pnl or 0) > 0 else "after_loss"
        for h in windows_hours:
            if gap_h <= h:
                key = f"{kind}_{h}h"
                groups[key].append(t)
                if prev[2].get("direction") == t.get("direction"):
                    same_dir[key] += 1
    out = []
    # 顯示順序：賺後各視窗 → 賠後各視窗 → 其他
    for kind, word in (("after_win", "獲利"), ("after_loss", "虧損")):
        for h in windows_hours:
            out.append((f"{kind}_{h}h", f"{word}出場後 {h} 小時內再進場"))
    out.append(("rest", f"其他(間隔超過 {longest} 小時或第一筆)"))
    result = []
    for key, label in out:
        st = compute_stats(groups[key])
        n = st["total_trades"]
        result.append({
            "key": key, "label": label, "total_trades": n, "win_rate": st["win_rate"],
            "total_pnl_points": st["total_pnl_points"], "profit_factor": st["profit_factor"],
            "profit_factor_infinite": st["profit_factor"] is None and n > 0,
            "avg_pnl_points": round(st["total_pnl_points"] / n, 2) if n else None,
            "same_dir_ratio": round(same_dir.get(key, 0) / n * 100, 1) if n and key != "rest" else None,
        })
    return {"groups": result, "skipped": skipped, "windows_hours": list(windows_hours)}
