"""
r129：滾倉第 2 步——照滾倉規則自動移停損、自動掛加碼單。

兩把鑰匙(都打開才會真的動單)：
  1. 伺服器模式(資料庫 app_settings: roll_exec_mode)：off(不算)／dry(試跑：只算、只發通知，預設)／live(執行)
  2. EA 參數 AllowTrading(預設 false)：關著時 EA 收到指令也不做，回報「EA 端未開啟」

會自動做的事(都只往有利方向收緊，絕不放寬)：
  A. 加碼單停損對齊：加碼單之間停損不一樣時(新加碼單成交後)，把落後的改成最新(最緊)那個。
  B. 保住浮盈：停損全部打到保住的錢 < 要保住的金額 → 把加碼單停損(只有核心單時改核心單)上移到停損線。
  C. 加碼單掛單：以最有利那筆進場價為基準、每隔加碼間距一格，取現價外面最近的一格，掛 Buy Stop(空單 Sell Stop)，
     手數照計算器算(全部停損打到仍保住、槓桿不超過、不越加越大、不超過 ROLL_EXEC_MAX_LOTS)，
     停損＝加碼點 ∓ 加碼停損距。成交後由 A 把舊加碼單停損對齊上來。只管 EA 自己掛的單(magic)。

不自動做的事：減碼平倉(槓桿超標只通知)、核心單的結構停損(只有「保住浮盈」那條規則會動到只有核心單時的停損)。
有手動掛單、同時有多空單、其他商品部位、報價有問題、停損在現價另一側 → 整個暫停，只通知。

協定：EA(v2，header X-Proto: 2)回報後，回應是純文字：
  第一行  OK|<mode>
  之後每行一個指令(只有 live 才給)：
    MOD|<id>|<ticket>|<sl>
    PEND|<id>|BUY_STOP 或 SELL_STOP|<price>|<volume>|<sl>
    DEL|<id>|<ticket>
EA 下一次回報帶 results：[{"id", "ok", "retcode", "msg"}]。
"""
import hashlib
import logging
import math
import os
import threading
import time

from app import position_roll

logger = logging.getLogger(__name__)

MODES = ("off", "dry", "live")
MODE_DB_KEY = "roll_exec_mode"
ROLL_MAGIC = 26112801            # EA 自己掛的加碼單用這個 magic
INFLIGHT_SECONDS = 20            # 同一個指令送出後這麼久內不重送(等 EA 回報結果)
RETRY_SECONDS = 600              # 執行失敗的指令隔這麼久才重送
MIN_TICK = 0.01

_lock = threading.Lock()
_state = {"mode": None, "inflight": {}, "dry_seen": set(), "last_plan": None, "results": []}


def reset():
    with _lock:
        _state.update({"mode": None, "inflight": {}, "dry_seen": set(), "last_plan": None, "results": []})


def max_auto_lots():
    try:
        return max(0.0, float(os.getenv("ROLL_EXEC_MAX_LOTS", "0.05")))
    except ValueError:
        return 0.05


# ---------------------------------------------------------------------------
# 模式
# ---------------------------------------------------------------------------

def get_mode():
    with _lock:
        if _state["mode"] is not None:
            return _state["mode"]
    mode = "dry"
    try:
        from app import db
        v = db.get_app_settings().get(MODE_DB_KEY)
        if v in MODES:
            mode = v
    except Exception as e:
        logger.warning(f"滾倉執行：讀模式失敗，用 dry: {e}")
        return mode
    with _lock:
        _state["mode"] = mode
    return mode


def set_mode(mode):
    if mode not in MODES:
        raise ValueError("模式只能是 off／dry／live")
    from app import db
    db.save_app_settings({MODE_DB_KEY: mode})
    with _lock:
        _state["mode"] = mode
        _state["inflight"].clear()
        _state["dry_seen"].clear()
    return mode


# ---------------------------------------------------------------------------
# 計算要做的事(純函式：輸入同步畫面＋原始回報＋規則，輸出動作與暫停原因)
# ---------------------------------------------------------------------------

def _ceil_tick(x, tick):
    return round(math.ceil(x / tick - 1e-9) * tick, 6)


def _floor_tick(x, tick):
    return round(math.floor(x / tick + 1e-9) * tick, 6)


def _tighten(s, price, tick):
    """往有利方向取整：多單停損往上、空單往下(取整後一定不會比要求的鬆)。"""
    return _ceil_tick(price, tick) if s == 1 else _floor_tick(price, tick)


def _cid(*parts):
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:10]


def plan(view, snap, rules):
    """
    回傳 {"actions": [...], "blocked": [原因...], "notes": [...]}。
    action：{"op": "MOD", "ticket", "from", "to", "why"} / {"op": "PEND", "type", "price", "volume", "sl", "why"} /
            {"op": "DEL", "ticket", "why"}；每個都有 "id"(內容決定，重送會一樣)。
    """
    out = {"actions": [], "blocked": [], "notes": []}
    inputs = view.get("inputs")
    if not view.get("connected"):
        out["blocked"].append("EA 沒有連線")
        return out
    bad = [i for i in view.get("issues", []) if i["key"] in ("mixed", "noprice", "sl_cross", "others")]
    if bad:
        out["blocked"].extend(i["text"] for i in bad)
        return out
    if not inputs:
        return out          # 沒有黃金持倉：沒事做
    s = 1 if inputs["direction"] == "long" else -1
    sym = inputs["symbol"]
    info = (snap.get("ticks") or {}).get(sym) or {}
    tick = float(info.get("point") or 0) or MIN_TICK
    digits = int(info.get("digits") or 2)
    stops_dist = float(info.get("stops_level") or 0) * tick
    vol_min = float(info.get("vol_min") or 0.01)
    vol_step = float(info.get("vol_step") or 0.01)
    bid, ask = float(info.get("bid") or 0), float(info.get("ask") or 0)
    close_px = bid if s == 1 else ask     # 停損要離「平倉價」夠遠
    open_px = ask if s == 1 else bid
    buffer = max(stops_dist, 2 * tick)

    def sl_ok(sl):
        return s * (close_px - sl) > buffer

    positions = view["positions"]
    core_ticket = view.get("core_ticket")
    adds = [p for p in positions if p.get("ticket") != core_ticket]
    core = [p for p in positions if p.get("ticket") == core_ticket]
    if any(not float(p.get("sl") or 0) for p in positions):
        out["blocked"].append("有單沒有停損：先自己設好，程式只會收緊、不會替你設第一個停損")
        return out

    # 目前每張單的停損(之後一路只收緊)
    new_sl = {p["ticket"]: float(p["sl"]) for p in positions}

    # A. 加碼單停損對齊到最緊的那個
    if len(adds) > 1:
        target = max(new_sl[p["ticket"]] for p in adds) if s == 1 else min(new_sl[p["ticket"]] for p in adds)
        for p in adds:
            if s * (target - new_sl[p["ticket"]]) > 1e-9:
                new_sl[p["ticket"]] = target

    # B. 保住浮盈
    def calc_with(sl_map):
        add_sls = [sl_map[p["ticket"]] for p in adds]
        stop = (min(add_sls) if s == 1 else max(add_sls)) if add_sls else None
        q = {**inputs, **rules, "ladder_steps": 1, "stop": stop,
             "core_stop": sl_map[core[0]["ticket"]] if core else None}
        if not core:
            q["stop"] = min(sl_map.values()) if s == 1 else max(sl_map.values())
        return position_roll.calc(q)

    try:
        r = calc_with(new_sl)
    except ValueError as e:
        out["blocked"].append(f"計算失敗：{e}")
        return out
    st = r["stop"]
    if st["kept"] is not None and st["lock_line"] is not None and r["status"]["floating"] > 0 \
            and st["kept"] < st["keep_required"] - 1e-6:
        line = _tighten(s, st["lock_line"], tick)
        targets = {"all": positions, "add": adds, "core": core}[st["lock_target"]]
        if sl_ok(line):
            for p in targets:
                if s * (line - new_sl[p["ticket"]]) > 1e-9:
                    new_sl[p["ticket"]] = line
        else:
            out["notes"].append(f"停損線 {line:.{digits}f} 已經太靠近現價，沒辦法自動上移，請自己決定要不要先出場")

    for p in positions:
        t = p["ticket"]
        old, nv = float(p["sl"]), round(new_sl[t], digits)
        if s * (nv - old) > 1e-9:
            if not sl_ok(nv):
                out["notes"].append(f"#{t} 要改的停損 {nv} 太靠近現價，跳過")
                continue
            why = "保住浮盈" if (st["lock_line"] is not None and abs(nv - _tighten(s, st["lock_line"], tick)) < 1e-6) \
                else "加碼單停損對齊"
            out["actions"].append({"op": "MOD", "ticket": t, "from": old, "to": nv, "why": why,
                                   "id": _cid("MOD", t, nv)})

    # C. 加碼單掛單
    orders = [o for o in (snap.get("orders") or []) if str(o.get("symbol")) == sym]
    mine = [o for o in orders if int(o.get("magic") or 0) == ROLL_MAGIC]
    manual = [o for o in orders if int(o.get("magic") or 0) != ROLL_MAGIC]
    want = None
    if manual:
        out["notes"].append(f"有 {len(manual)} 張手動掛單，自動加碼單先暫停(避免重複加碼)")
    elif r["status"]["leverage"] is None or r["status"]["leverage"] > float(rules["max_leverage"]) + 1e-9:
        out["notes"].append("槓桿超過上限，不掛加碼單")
    else:
        entries = [float(p["price"]) for p in positions]
        anchor = max(entries) if s == 1 else min(entries)
        step = float(rules["add_step"])
        # 加碼點是以最有利那筆為基準、每隔加碼間距一格；取現價外面最近的那一格(已經走過的格子不追)
        k = max(1, math.floor(s * (open_px - anchor) / step) + 1)
        trig = _tighten(s, anchor + s * k * step, tick)
        if s * (trig - open_px) <= buffer:
            k += 1
            trig = _tighten(s, anchor + s * k * step, tick)
        if s * (trig - open_px) <= buffer:
            out["notes"].append(f"下一個加碼點 {trig:.{digits}f} 離現價太近，這次不掛")
        else:
            stop_dist = float(rules.get("add_stop_points") or step)
            add_stop = _tighten(s, trig - s * stop_dist, tick)
            # 在加碼點上試算：成交後所有加碼單停損對齊到 add_stop
            # 價格走到加碼點時的淨值＝現在淨值 ± 每點金額 × 距離
            at_trig = {**inputs, "price": trig,
                       "equity": float(inputs["equity"]) + s * r["status"]["per_point"] * (trig - inputs["price"])}
            q = {**at_trig, **rules, "ladder_steps": 1, "stop": add_stop,
                 "core_stop": new_sl[core[0]["ticket"]] if core else None}
            try:
                rt = position_roll.calc(q)
                lots = rt["add_now"]["lots"]
            except ValueError:
                lots = 0.0
            last_add = min([float(p["volume"]) for p in adds] or [float(rules.get("add_lots") or 0) or 9e9])
            cap = min(max_auto_lots(), last_add, float(rules.get("add_lots") or 9e9))
            lots = min(lots, cap)
            lots = round(math.floor(lots / vol_step + 1e-9) * vol_step, 2)
            if lots >= vol_min:
                want = {"type": "BUY_STOP" if s == 1 else "SELL_STOP", "price": round(trig, digits),
                        "volume": lots, "sl": round(add_stop, digits)}
            else:
                out["notes"].append(f"加碼點 {trig:.{digits}f} 照規則加不進去({rt['add_now']['why'] if lots == 0 else '手數太小'})，不掛")
    for o in mine:
        same = want and abs(float(o["price"]) - want["price"]) < tick / 2 and abs(float(o["volume"]) - want["volume"]) < 1e-9 \
            and abs(float(o.get("sl") or 0) - want["sl"]) < tick / 2
        if same:
            want = None
        else:
            out["actions"].append({"op": "DEL", "ticket": o["ticket"], "why": "加碼單要更新或取消",
                                   "id": _cid("DEL", o["ticket"])})
    if want and not mine:
        out["actions"].append({"op": "PEND", **want, "why": "下一個加碼點",
                               "id": _cid("PEND", want["type"], want["price"], want["volume"], want["sl"])})
    return out


# ---------------------------------------------------------------------------
# 跟 EA 的協定
# ---------------------------------------------------------------------------

def _line(a):
    if a["op"] == "MOD":
        return f"MOD|{a['id']}|{a['ticket']}|{a['to']}"
    if a["op"] == "PEND":
        return f"PEND|{a['id']}|{a['type']}|{a['price']}|{a['volume']}|{a['sl']}"
    return f"DEL|{a['id']}|{a['ticket']}"


def describe(a):
    if a["op"] == "MOD":
        return f"#{a['ticket']} 停損 {a['from']:g} → {a['to']:g}（{a['why']}）"
    if a["op"] == "PEND":
        return f"掛 {a['type']} {a['volume']:g} 手 @ {a['price']:g}，停損 {a['sl']:g}（{a['why']}）"
    return f"刪除自動加碼單 #{a['ticket']}（{a['why']}）"


def handle(view, snap, rules, send, label, now=None):
    """
    每次 EA 回報呼叫。回傳要給 EA 的純文字回應(第一行 OK|mode)。send(text) 發 Telegram。
    """
    now = time.time() if now is None else now
    mode = get_mode()
    # 先處理 EA 帶回來的上一批結果
    for res in snap.get("results") or []:
        rid = str(res.get("id"))
        with _lock:
            info = _state["inflight"].pop(rid, None)
            _state["results"] = (_state["results"] + [{**res, "at": now}])[-20:]
        what = describe(info["action"]) if info else rid
        if res.get("ok"):
            send(f"{label}\n✅ 已執行：{what}")
        else:
            # 失敗：同一個指令 RETRY_SECONDS 後才再送(等於每 10 分鐘重試＋提醒一次，不會每 3 秒狂送)
            if info:
                with _lock:
                    _state["inflight"][rid] = {"at": now + RETRY_SECONDS - INFLIGHT_SECONDS, "action": info["action"]}
            send(f"{label}\n🚨 執行失敗：{what}\n{res.get('retcode', '')} {res.get('msg', '')}".rstrip())
    if mode == "off":
        with _lock:
            _state["last_plan"] = None
        return "OK|off"
    p = plan(view, snap, rules)
    with _lock:
        _state["last_plan"] = {**p, "at": now, "mode": mode}
    lines = [f"OK|{mode}"]
    if mode == "dry":
        keys = set()
        for a in p["actions"]:
            keys.add(a["id"])
            with _lock:
                seen = a["id"] in _state["dry_seen"]
                _state["dry_seen"].add(a["id"])
            if not seen:
                send(f"{label}\n🧪 試跑（沒有真的動單）：會{describe(a)}")
        with _lock:
            _state["dry_seen"] &= keys
        return "\n".join(lines)
    # live
    if not snap.get("allow_trading"):
        if p["actions"]:
            with _lock:
                seen = "ea_off" in _state["dry_seen"]
                _state["dry_seen"].add("ea_off")
            if not seen:
                send(f"{label}\n⚠️ 伺服器是執行模式，但 EA 參數 AllowTrading 沒開，{len(p['actions'])} 個動作沒做")
        return "\n".join(lines)
    with _lock:
        _state["dry_seen"].discard("ea_off")
        for k in [k for k, v in _state["inflight"].items() if now - v["at"] > INFLIGHT_SECONDS]:
            _state["inflight"].pop(k)
        for a in p["actions"]:
            if a["id"] in _state["inflight"]:
                continue
            _state["inflight"][a["id"]] = {"at": now, "action": a}
            lines.append(_line(a))
    return "\n".join(lines)


def status():
    mode = get_mode()          # 不能在 _lock 裡面呼叫(get_mode 自己會拿鎖)
    with _lock:
        return {"mode": mode, "plan": _state["last_plan"], "results": list(_state["results"])[-10:],
                "inflight": len(_state["inflight"]), "max_auto_lots": max_auto_lots(), "magic": ROLL_MAGIC}
