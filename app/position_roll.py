"""
r124：倉位滾動（滾倉）計算器。純函式：不碰交易所、不碰引擎、不打網路。

滾倉的紀律(計算器照這幾條判斷)：
  1. 只用浮盈加碼：沒有浮盈就不加。
  2. 每次加碼後，整體停損打到時「至少保住目前浮盈的 lock_pct%」(lock_pct=0 ＝ 至少保住本金)。
  3. 實際槓桿(名目部位 ÷ 淨值)不超過 max_leverage。
  4. 加碼手數不越加越大(每次 ≤ 計畫手數、≤ 上一次)。

名詞：
  結餘 balance      ＝ 已實現的錢(MT5「結餘」)
  淨值 equity       ＝ 結餘 + 浮動損益(MT5「淨值」)；有填的話，跟價格算出來的差額(隔夜費、手續費)會帶進所有情境
  保住 kept         ＝ 打到某價位時的淨值 − 結餘；負的就是虧到本金
  1 手 = contract_size 盎司(XAUUSD 一般是 100)，所以每 1 點 = 手數 × contract_size 美元
"""
import math

DEFAULTS = {
    "contract_size": 100.0,
    "max_leverage": 10.0,
    "lock_pct": 50.0,
    "add_step": 30.0,
    "add_lots": None,       # None ＝ 用現有部位裡最小的那筆
    "ladder_steps": 3,
    "cost_points": 0.4,     # 每次加碼的點差＋滑價(點)，r111 MT5 成本假設
    "lot_step": 0.01,
    "trail_stop": True,     # 每往上一個加碼點，停損也跟著移同樣的距離
}

MAX_LADDER = 10


def _num(v, name, positive=False, allow_none=False):
    if v is None or (isinstance(v, str) and not v.strip()):
        if allow_none:
            return None
        raise ValueError(f"缺少{name}")
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{name}不是數字：{v}")
    if not math.isfinite(x):
        raise ValueError(f"{name}不是有效數字：{v}")
    if positive and x <= 0:
        raise ValueError(f"{name}要大於 0")
    return x


def _floor_lots(x, step):
    if x <= 0:
        return 0.0
    return round(math.floor(x / step + 1e-9) * step, 6)


def _ceil_lots(x, step):
    if x <= 0:
        return 0.0
    return round(math.ceil(x / step - 1e-9) * step, 6)


def parse(inp):
    """把網頁送來的欄位整理成計算用的參數；有問題丟 ValueError(訊息直接給網頁顯示)。"""
    inp = inp or {}
    d = str(inp.get("direction") or "long").lower()
    if d not in ("long", "short"):
        raise ValueError("方向只能是 long(多) 或 short(空)")
    s = 1 if d == "long" else -1
    cs = _num(inp.get("contract_size", DEFAULTS["contract_size"]), "合約大小", positive=True)
    lot_step = _num(inp.get("lot_step", DEFAULTS["lot_step"]), "最小手數單位", positive=True)
    raw = inp.get("positions") or []
    positions = []
    for i, p in enumerate(raw, 1):
        if not isinstance(p, dict):
            raise ValueError(f"第 {i} 筆部位格式不對")
        if (p.get("lots") in (None, "")) and (p.get("entry") in (None, "")):
            continue   # 網頁上的空白列
        positions.append({"lots": _num(p.get("lots"), f"第 {i} 筆手數", positive=True),
                          "entry": _num(p.get("entry"), f"第 {i} 筆進場價", positive=True)})
    if not positions:
        raise ValueError("至少要有一筆部位")
    price = _num(inp.get("price"), "現價", positive=True)
    balance = _num(inp.get("balance"), "結餘", positive=True)
    equity = _num(inp.get("equity"), "淨值", allow_none=True)
    stop = _num(inp.get("stop"), "停損價", positive=True, allow_none=True)
    if stop is not None and s * (price - stop) <= 0:
        raise ValueError(f"停損價 {stop:g} 要在現價{'下方(多單)' if s == 1 else '上方(空單)'}")
    max_lev = _num(inp.get("max_leverage", DEFAULTS["max_leverage"]), "槓桿上限", positive=True)
    if max_lev <= 1:
        raise ValueError("槓桿上限要大於 1 倍")
    lock_pct = _num(inp.get("lock_pct", DEFAULTS["lock_pct"]), "保住浮盈 %")
    if not 0 <= lock_pct <= 100:
        raise ValueError("保住浮盈 % 要在 0～100 之間")
    add_step = _num(inp.get("add_step", DEFAULTS["add_step"]), "加碼間距", positive=True)
    add_lots = _num(inp.get("add_lots"), "每次加碼手數", positive=True, allow_none=True)
    if add_lots is None:
        add_lots = min(p["lots"] for p in positions)
    steps = int(_num(inp.get("ladder_steps", DEFAULTS["ladder_steps"]), "加碼階數"))
    steps = max(1, min(MAX_LADDER, steps))
    cost = _num(inp.get("cost_points", DEFAULTS["cost_points"]), "加碼成本(點)")
    if cost < 0:
        raise ValueError("加碼成本不能是負的")
    trail = inp.get("trail_stop", DEFAULTS["trail_stop"])
    trail = trail if isinstance(trail, bool) else str(trail).lower() in ("1", "true", "yes", "on")
    return {"s": s, "direction": d, "cs": cs, "lot_step": lot_step, "positions": positions, "price": price,
            "balance": balance, "equity": equity, "stop": stop, "max_lev": max_lev, "lock_pct": lock_pct,
            "add_step": add_step, "add_lots": add_lots, "steps": steps, "cost": cost, "trail": trail}


class Book:
    """一組同方向的部位 + 帳戶結餘。所有價位情境都從這裡算。"""

    def __init__(self, s, cs, balance, positions, adj=0.0):
        self.s, self.cs, self.balance, self.adj = s, cs, balance, adj
        self.positions = [dict(p) for p in positions]

    @property
    def lots(self):
        return sum(p["lots"] for p in self.positions)

    @property
    def avg(self):
        return sum(p["lots"] * p["entry"] for p in self.positions) / self.lots

    def pnl(self, price):
        return self.s * self.cs * sum(p["lots"] * (price - p["entry"]) for p in self.positions)

    def equity_at(self, price):
        return self.balance + self.adj + self.pnl(price)

    def kept_at(self, price):
        """打到這個價位時，相對結餘還剩多少(正＝還有賺、負＝虧到本金)。"""
        return self.equity_at(price) - self.balance

    def leverage_at(self, price):
        eq = self.equity_at(price)
        return (self.lots * self.cs * price / eq) if eq > 0 else None

    def zero_price(self):
        """淨值歸零的價位(不算券商強平線，強平會更早)。"""
        q = self.lots * self.cs
        return self.avg - self.s * (self.balance + self.adj) / q

    def lock_price(self, keep):
        """停損至少要放在哪裡，打到時才會保住 keep 美元(相對結餘)。"""
        q = self.lots * self.cs
        return self.avg + self.s * (keep - self.adj) / q

    def leverage_cap_price(self, cap):
        """槓桿剛好等於 cap 的價位。多單：價格高於它槓桿就低於 cap；空單相反。"""
        q = self.lots * self.cs
        b = self.balance + self.adj
        den = q * (1 - cap * self.s)
        if den == 0:
            return None
        p = cap * (b - self.s * q * self.avg) / den
        if p <= 0 or self.equity_at(p) <= 0:
            return None
        return p


def max_add(book, price, stop, keep_req, cap, cost, plan_lots, lot_step):
    """
    在 price 加碼、整體停損放 stop 時最多能加幾手。回傳 (手數, 卡住的條件, 進場價)。
    三個上限取最小：槓桿上限、停損時至少保住 keep_req、計畫手數。
    """
    s, cs = book.s, book.cs
    entry = price + s * cost
    eq = book.equity_at(price)
    if eq <= 0:
        return 0.0, "淨值已經小於等於 0", entry
    # 槓桿：(L+x)·cs·P ≤ cap·(E − x·cs·cost)
    by_lev = (cap * eq - book.lots * cs * price) / (cs * (price + cap * cost))
    # 停損：kept(stop) + s·cs·x·(stop − entry) ≥ keep_req，(stop − entry) 跟方向相反所以每加一手就少保住一點
    per_lot_loss = cs * s * (entry - stop)
    by_stop = (book.kept_at(stop) - keep_req) / per_lot_loss if per_lot_loss > 0 else 0.0
    limits = [(by_lev, "槓桿上限"), (by_stop, "停損時要保住的浮盈"), (plan_lots, "計畫手數")]
    x, why = min(limits, key=lambda t: t[0])
    lots = _floor_lots(x, lot_step)
    if lots < lot_step:
        return 0.0, why, entry
    return lots, why, entry


def _r(x, n=2):
    return None if x is None else round(x, n)


def calc(inp):
    p = parse(inp)
    s, cs, step = p["s"], p["cs"], p["lot_step"]
    price, cap = p["price"], p["max_lev"]
    up = "上" if s == 1 else "下"
    adverse = "跌到" if s == 1 else "漲到"
    favor = "漲到" if s == 1 else "跌到"

    book0 = Book(s, cs, p["balance"], p["positions"])
    adj = (p["equity"] - book0.equity_at(price)) if p["equity"] is not None else 0.0
    book = Book(s, cs, p["balance"], p["positions"], adj)

    eq = book.equity_at(price)
    floating = eq - p["balance"]
    lev = book.leverage_at(price)
    keep_req = max(floating, 0.0) * p["lock_pct"] / 100.0
    lock_line = book.lock_price(keep_req)
    stop = p["stop"]
    stop_for_calc = stop if stop is not None else lock_line
    stop_kept = book.kept_at(stop) if stop is not None else None

    status = {
        "lots": _r(book.lots), "avg_price": _r(book.avg), "floating": _r(floating), "equity": _r(eq),
        "adjustment": _r(adj), "per_point": _r(book.lots * cs), "notional": _r(book.lots * cs * price),
        "leverage": _r(lev), "zero_price": _r(book.zero_price()),
        "points_to_zero": _r(abs(price - book.zero_price())),
        "leverage_cap_price": _r(book.leverage_cap_price(cap)),
    }
    stop_info = {
        "stop": stop, "kept": _r(stop_kept),
        "equity_at_stop": _r(stop_kept + p["balance"]) if stop_kept is not None else None,
        "lock_pct": p["lock_pct"], "keep_required": _r(keep_req), "lock_line": _r(lock_line),
        "locks_enough": (stop_kept is not None and floating > 0 and stop_kept >= keep_req - 1e-6),
    }

    # ---- 減碼：槓桿超過上限時，要平掉幾手、先平哪一筆 ----
    reduce = None
    if lev is None or lev > cap + 1e-9:
        target = _floor_lots(cap * eq / (cs * price), step) if eq > 0 else 0.0
        need = _ceil_lots(book.lots - target, step)
        order = sorted(range(len(book.positions)), key=lambda i: -s * book.positions[i]["entry"])  # 成本最差的先平
        plan, left = [], need
        for i in order:
            if left <= 1e-9:
                break
            pos = book.positions[i]
            take = round(min(pos["lots"], left), 6)
            plan.append({"index": i, "entry": pos["entry"], "lots_held": pos["lots"], "close_lots": _r(take),
                         "realized": _r(s * cs * take * (price - pos["entry"]))})
            left = round(left - take, 6)
        remaining = []
        for i, pos in enumerate(book.positions):
            closed = sum(x["close_lots"] for x in plan if x["index"] == i)
            if pos["lots"] - closed > 1e-9:
                remaining.append({"lots": round(pos["lots"] - closed, 6), "entry": pos["entry"]})
        after = None
        if remaining:
            realized = sum(x["realized"] for x in plan)
            b2 = Book(s, cs, p["balance"] + realized, remaining, adj)
            after = {"lots": _r(b2.lots), "avg_price": _r(b2.avg), "leverage": _r(b2.leverage_at(price)),
                     "balance": _r(b2.balance), "zero_price": _r(b2.zero_price()),
                     # 跟原本的結餘比(平掉那幾筆實現的獲利也算保住)
                     "kept_at_stop": _r(b2.equity_at(stop) - p["balance"]) if stop is not None else None}
        reduce = {"close_lots": _r(need), "target_lots": _r(target), "plan": plan, "after": after}

    # ---- 現在加碼 ----
    if floating <= 0:
        add_now = {"lots": 0.0, "why": "沒有浮盈(滾倉只用浮盈加碼)", "entry": _r(price + s * p["cost"]),
                   "stop": _r(stop_for_calc)}
    else:
        x, why, entry = max_add(book, price, stop_for_calc, keep_req, cap, p["cost"], p["add_lots"], step)
        add_now = {"lots": x, "why": why, "entry": _r(entry), "stop": _r(stop_for_calc)}
        if x > 0:
            b2 = Book(s, cs, p["balance"], book.positions + [{"lots": x, "entry": entry}], adj)
            add_now.update({"total_lots": _r(b2.lots), "avg_price": _r(b2.avg), "leverage": _r(b2.leverage_at(price)),
                            "kept_at_stop": _r(b2.kept_at(stop_for_calc))})
    add_now["stop_is_lock_line"] = stop is None

    # ---- 往後的加碼點(假設前一階有照可加的手數加) ----
    ladder = []
    lb = Book(s, cs, p["balance"], book.positions + ([{"lots": add_now["lots"], "entry": add_now["entry"]}]
                                                      if add_now["lots"] > 0 else []), adj)
    last_add = add_now["lots"] if add_now["lots"] > 0 else p["add_lots"]
    for k in range(1, p["steps"] + 1):
        t = price + s * k * p["add_step"]
        st = stop_for_calc + s * k * p["add_step"] if p["trail"] else stop_for_calc
        fl = lb.kept_at(t)
        kr = max(fl, 0.0) * p["lock_pct"] / 100.0
        x, why, entry = max_add(lb, t, st, kr, cap, p["cost"], min(p["add_lots"], last_add), step) if fl > 0 \
            else (0.0, "沒有浮盈", t + s * p["cost"])
        if x > 0:
            lb = Book(s, cs, p["balance"], lb.positions + [{"lots": x, "entry": entry}], adj)
            last_add = x
        ladder.append({"step": k, "price": _r(t), "stop": _r(st), "add_lots": x, "why": why,
                       "total_lots": _r(lb.lots), "avg_price": _r(lb.avg), "equity": _r(lb.equity_at(t)),
                       "leverage": _r(lb.leverage_at(t)), "kept_at_stop": _r(lb.kept_at(st)),
                       "keep_required": _r(kr)})

    # ---- 建議(由重到輕) ----
    actions = []
    if eq <= 0:
        actions.append({"level": "alarm", "text": "淨值已經小於等於 0，先處理部位"})
    if stop is None:
        actions.append({"level": "alarm", "text": f"沒有停損：{adverse} {status['zero_price']:.2f}"
                        f"（距現價 {status['points_to_zero']:.2f} 點）帳戶歸零，強平還會更早"})
    elif stop_kept < 0:
        actions.append({"level": "alarm", "text": f"停損 {stop:g} 打到會虧到本金 {abs(stop_kept):.2f}"})
    if lev is None or lev > cap + 1e-9:
        first = reduce["plan"][0] if reduce and reduce["plan"] else None
        actions.append({"level": "alarm", "text": (
            f"實際槓桿 {lev:.1f} 倍超過上限 {cap:g} 倍：建議減碼 {reduce['close_lots']:g} 手"
            + (f"（先平進場 {first['entry']:g} 那筆）" if first else "")
            + (f"，或等{favor} {status['leverage_cap_price']:.2f} 槓桿自然回到 {cap:g} 倍" if status["leverage_cap_price"]
               and s * (status["leverage_cap_price"] - price) > 0 else "")) if lev is not None
            else "淨值小於等於 0，無法計算槓桿"})
    if floating > 0:
        if stop is None:
            actions.append({"level": "warn", "text": f"停損至少要移到 {lock_line:.2f}，打到時才保住 {p['lock_pct']:g}% 浮盈"
                            f"（{keep_req:.2f}）；要有加碼空間，停損得比這條更{('高' if s == 1 else '低')}"})
        elif stop_kept is not None and stop_kept < keep_req - 1e-6:
            actions.append({"level": "warn", "text": f"停損 {stop:g} 只保住 {stop_kept:.2f}，"
                            f"要保住 {p['lock_pct']:g}% 浮盈需移到 {lock_line:.2f} 以{up}"})
    else:
        actions.append({"level": "info", "text": "目前沒有浮盈，不適合滾倉加碼"})
    if not any(a["level"] == "alarm" for a in actions) and floating > 0:
        if add_now["lots"] > 0:
            actions.append({"level": "ok", "text": f"現在可以加 {add_now['lots']:g} 手，停損 {add_now['stop']:.2f}；"
                            f"打到停損仍保住 {add_now['kept_at_stop']:.2f}，槓桿 {add_now['leverage']:.1f} 倍"})
        else:
            nxt = next((r for r in ladder if r["add_lots"] > 0), None)
            actions.append({"level": "info", "text": (
                f"現在先不加（卡在{add_now['why']}）；" + (
                    f"下一個加碼點 {nxt['price']:.2f}，可加 {nxt['add_lots']:g} 手，停損移到 {nxt['stop']:.2f}"
                    if nxt else "往後幾個加碼點也加不進去，先調整停損或部位"))})
    elif any(a["level"] == "alarm" for a in actions):
        actions.append({"level": "info", "text": "先處理上面的警示，再考慮加碼"})

    return {"success": True, "direction": p["direction"], "price": price, "status": status, "stop": stop_info,
            "reduce": reduce, "add_now": add_now, "ladder": ladder, "actions": actions,
            "params": {"max_leverage": cap, "lock_pct": p["lock_pct"], "add_step": p["add_step"],
                       "add_lots": p["add_lots"], "cost_points": p["cost"], "contract_size": cs,
                       "trail_stop": p["trail"], "ladder_steps": p["steps"]}}


def structure_levels(direction, chan_by_interval, daily_snapshot, binance_price):
    """
    給網頁挑停損用的結構價位(幣安 XAUUSDT 價)：各週期最近的筆端點、最新中樞上下緣，日線 SMC 區。
    只列在「不利方向」那一側(多單列現價下方、空單列上方)，由近到遠。網頁再換算成券商價。
    """
    s = 1 if direction == "long" else -1
    out = []
    if binance_price is None:
        return out
    for label, chan in (chan_by_interval or {}).items():
        if not isinstance(chan, dict):
            continue
        for b in (chan.get("bi_list") or [])[-6:]:
            ends = [b.get(k) for k in ("start_price", "end_price") if b.get(k) is not None]
            if not ends:
                continue
            v = min(ends) if s == 1 else max(ends)   # 多單看筆的低端、空單看高端
            if s * (binance_price - v) > 0:
                out.append({"price": round(float(v), 2), "source": f"{label} 筆{'低點' if s == 1 else '高點'}"})
        zs = chan.get("latest_zhongshu")
        if isinstance(zs, dict):
            for key, name in (("zd", "中樞下緣"), ("zg", "中樞上緣")):
                v = zs.get(key)
                if v is not None and s * (binance_price - v) > 0:
                    out.append({"price": round(float(v), 2), "source": f"{label} {name}"})
    if isinstance(daily_snapshot, dict):
        zones = daily_snapshot.get("zones_below" if s == 1 else "zones_above") or []
        for z in zones[:3]:
            v = z.get("bot") if s == 1 else z.get("top")
            if v is not None and s * (binance_price - v) > 0:
                out.append({"price": round(float(v), 2), "source": f"日線 {z.get('kind', '')}".strip()})
    # 同價合併、由近到遠
    merged = {}
    for lv in out:
        key = lv["price"]
        if key in merged:
            if lv["source"] not in merged[key]["source"]:
                merged[key]["source"] += "、" + lv["source"]
        else:
            merged[key] = dict(lv)
    levels = sorted(merged.values(), key=lambda lv: abs(binance_price - lv["price"]))
    return levels[:8]
