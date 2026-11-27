"""
r125：滾倉同步(只讀、不下單)。真實帳戶(Vantage MT5)上的唯讀 EA `windows/TzujenRollSync.mq5` 每幾秒把
帳戶(結餘、淨值、槓桿)與持倉(手數、進場價、停損)POST 到 /tools/roll-sync，這裡：
  1. 存最新一份，給滾倉計算器(/tools/roll)自動帶入，不用再手動 key。
  2. 用滾倉計算器(app/position_roll.py)＋「通知規則」檢查，該處理的事發 Telegram：
       · 有單沒停損、停損打到會虧到本金、實際槓桿超過上限          → 🚨 警示，沒處理每 30 分鐘提醒一次，處理好發 ✅
       · 各單停損不一致(加碼成交後舊單停損沒跟著移)                → ⚠️ 提醒，同上節奏
       · 價格離最有利那筆進場價 ≥ 加碼間距、而且規則允許加碼         → 📈 加碼點，同一個加碼點只通知一次
       · 有持倉時 EA 超過 3 分鐘沒回報                              → 🔌 斷線一次、恢復一次

這裡不下任何單：EA 本身也沒有下單的程式碼。跟 cfd-live 完全分開(cfd-live 的對帳／緊急停止假設一個帳戶只跑它的策略)。

設定：環境變數 ROLL_SYNC_TOKEN(跟 EA 參數 Token 一樣的長亂碼)。沒設就整個關閉(/tools/roll-sync 回 404)。
只在一個服務設(lab 或 live 擇一)，兩邊都設會發兩份通知。
ROLL_SYNC_SYMBOL：只看商品名稱含這段字的持倉，預設 XAU(Vantage 是 XAUUSD+)。
"""
import hmac
import json
import logging
import os
import threading
import time

from app import position_roll

logger = logging.getLogger(__name__)

STALE_SECONDS = 180          # 有持倉時 EA 超過這麼久沒回報 → 斷線
REPEAT_SECONDS = 1800        # 沒處理的警示多久再提醒一次
CHECK_SECONDS = 30           # 背景檢查斷線的間隔
RULES_DB_KEY = "roll_rules"  # 存在 app_settings(設定頁不認得這個 key，會略過)
RULE_FIELDS = ("max_leverage", "lock_pct", "add_step", "add_lots", "cost_points", "contract_size", "trail_stop",
               "core_mode", "add_stop_points")
CORE_MODES = ("earliest", "none")   # r126：核心單＝最早那筆／不分

_lock = threading.Lock()
_state = {"snapshot": None, "received_at": None, "rules": None, "alerts": {}, "disconnected": False,
          "had_positions": False, "add_keys": set(), "last_eval": None, "sent": []}
_thread = None


def token():
    return os.getenv("ROLL_SYNC_TOKEN", "").strip()


def symbol_hint():
    return (os.getenv("ROLL_SYNC_SYMBOL", "XAU").strip() or "XAU").upper()


def enabled():
    return len(token()) >= 8


def _send(text):
    """發 Telegram；沒設通知就只記 log。回傳有沒有送出(測試用)。"""
    _state["sent"].append(text)
    del _state["sent"][:-50]
    try:
        from app.notifier import notifier
        if notifier.is_enabled:
            notifier.send_raw_message(text)
            return True
    except Exception as e:
        logger.warning(f"滾倉同步：通知送不出去: {e}")
    logger.info(f"滾倉同步(未送出)：{text}")
    return False


# ---------------------------------------------------------------------------
# 通知規則：存資料庫，沒有就用計算器的預設值
# ---------------------------------------------------------------------------

def default_rules():
    rules = {k: position_roll.DEFAULTS[k] for k in RULE_FIELDS if k in position_roll.DEFAULTS}
    rules["core_mode"] = "earliest"
    return rules


def get_rules():
    with _lock:
        if _state["rules"] is not None:
            return dict(_state["rules"])
    rules = default_rules()
    try:
        from app import db
        raw = db.get_app_settings().get(RULES_DB_KEY)
        if raw:
            stored = json.loads(raw)
            rules.update({k: v for k, v in stored.items() if k in RULE_FIELDS})
    except Exception as e:
        logger.warning(f"滾倉同步：讀通知規則失敗，用預設值: {e}")
        return rules       # 讀失敗不快取，下次再讀
    with _lock:
        _state["rules"] = dict(rules)
    return rules


def save_rules(payload):
    """驗證後存起來。回傳存好的規則；有問題丟 ValueError。"""
    rules = default_rules()
    for k in RULE_FIELDS:
        if k in (payload or {}) and payload[k] not in (None, ""):
            rules[k] = payload[k]
    # 用計算器的檢查驗一次(拿一組假部位)，欄位有問題會丟 ValueError
    position_roll.parse({"positions": [{"lots": 0.01, "entry": 100}], "price": 101, "balance": 100, **rules})
    if str(rules.get("core_mode")) not in CORE_MODES:
        raise ValueError("核心單設定只能是 earliest(最早那筆)或 none(不分)")
    clean = {k: rules[k] for k in RULE_FIELDS}
    for k in ("max_leverage", "lock_pct", "add_step", "cost_points", "contract_size"):
        clean[k] = float(clean[k])
    clean["add_lots"] = float(clean["add_lots"]) if clean["add_lots"] not in (None, "") else None
    clean["add_stop_points"] = float(clean["add_stop_points"]) if clean["add_stop_points"] not in (None, "") else None
    tr = clean["trail_stop"]
    clean["trail_stop"] = tr if isinstance(tr, bool) else str(tr).lower() in ("1", "true", "yes", "on")
    from app import db
    db.save_app_settings({RULES_DB_KEY: json.dumps(clean)})
    with _lock:
        _state["rules"] = dict(clean)
    return clean


# ---------------------------------------------------------------------------
# 收 EA 的回報
# ---------------------------------------------------------------------------

def receive(payload, given_token, now=None):
    """回傳 (ok, error)。token 不對、格式不對都不存。存好之後馬上檢查一次要不要通知。"""
    if not enabled():
        return False, "沒有啟用(ROLL_SYNC_TOKEN 未設定)"
    if not hmac.compare_digest(str(given_token or ""), token()):
        return False, "token 不對"
    if not isinstance(payload, dict) or not isinstance(payload.get("positions"), list):
        return False, "格式不對：缺 positions"
    for k in ("balance", "equity"):
        try:
            float(payload.get(k))
        except (TypeError, ValueError):
            return False, f"格式不對：{k}"
    now = time.time() if now is None else now
    with _lock:
        _state["snapshot"] = payload
        _state["received_at"] = now
    try:
        evaluate(now)
    except Exception as e:      # 檢查出錯不能讓 EA 以為回報失敗
        logger.exception(f"滾倉同步：檢查失敗: {e}")
    return True, None


def _gold_positions(snap):
    hint = symbol_hint()
    return [p for p in (snap.get("positions") or []) if hint in str(p.get("symbol", "")).upper()]


def account_view(now=None):
    """給網頁與檢查用：把最新回報整理成計算器的輸入。沒有回報時 inputs 是 None。"""
    now = time.time() if now is None else now
    with _lock:
        snap, at = _state["snapshot"], _state["received_at"]
    out = {"enabled": enabled(), "connected": False, "age_seconds": None, "account": None, "inputs": None,
           "issues": [], "positions": []}
    if not snap:
        return out
    age = now - at
    out["age_seconds"] = round(age, 1)
    out["connected"] = age <= STALE_SECONDS
    out["account"] = {k: snap.get(k) for k in ("login", "server", "company", "trade_mode", "leverage", "currency")}
    gold = _gold_positions(snap)
    others = [p for p in (snap.get("positions") or []) if p not in gold]
    out["positions"] = [{"ticket": p.get("ticket"), "symbol": p.get("symbol"), "type": p.get("type"),
                         "volume": p.get("volume"), "price": p.get("price"), "sl": p.get("sl")} for p in gold]
    issues = out["issues"]
    if others:
        issues.append({"level": "warn", "key": "others",
                       "text": f"帳戶裡還有 {len(others)} 筆其他商品的部位，淨值包含它們，計算會不準"})
    if not gold:
        return out
    types = {int(p.get("type", 0)) for p in gold}
    if len(types) > 1:
        issues.append({"level": "alarm", "key": "mixed", "text": "同時有多單和空單，計算器只能算單一方向"})
        return out
    s = 1 if types == {0} else -1
    direction = "long" if s == 1 else "short"
    sym = gold[0].get("symbol")
    tick = (snap.get("ticks") or {}).get(sym) or {}
    price = tick.get("bid") if s == 1 else tick.get("ask")     # 平倉要用的那一邊
    if not price:
        issues.append({"level": "alarm", "key": "noprice", "text": f"沒有收到 {sym} 的報價"})
        return out
    missing = [p for p in gold if not float(p.get("sl") or 0)]
    if missing:
        issues.append({"level": "alarm", "key": "no_sl",
                       "text": "沒有停損：" + "、".join(f"{p.get('volume')} 手 @ {float(p.get('price')):g}" for p in missing)})
    # r126：核心單＝最早開的那一筆(規則 core_mode=earliest)，它的停損另外算，不跟加碼單比一不一致
    gold = sorted(gold, key=lambda p: (int(p.get("time") or 0), int(p.get("ticket") or 0)))
    core_ticket = gold[0].get("ticket") if get_rules().get("core_mode", "earliest") == "earliest" else None
    core = [p for p in gold if core_ticket is not None and p.get("ticket") == core_ticket]
    adds = [p for p in gold if p not in core]

    def _sl(p):
        return float(p.get("sl") or 0)

    core_stop = None
    if core and _sl(core[0]) > 0:
        core_stop = _sl(core[0])
        if s * (price - core_stop) <= 0:
            issues.append({"level": "alarm", "key": "sl_cross", "text": f"核心單停損 {core_stop:g} 已經在現價另一側"})
            core_stop = None
    set_sls = [_sl(p) for p in adds if _sl(p) > 0]
    stop = None
    if set_sls:
        # 計算用最保守的那個(多單取最低)；加碼單之間不一致另外提醒
        stop = min(set_sls) if s == 1 else max(set_sls)
        if len(set(round(x, 2) for x in set_sls)) > 1 and len(set_sls) == len(adds):
            target = max(set_sls) if s == 1 else min(set_sls)
            lagging = [p for p in adds if abs(_sl(p) - target) > 1e-6]
            who = "加碼單" if core else "各單"
            issues.append({"level": "warn", "key": "sl_mismatch", "target": target,
                           "text": f"{who}停損不一致：最新的是 {target:g}，" + "、".join(
                               f"{float(p.get('price')):g} 那筆還在 {_sl(p):g}" for p in lagging)
                           + f"——如果是加碼後還沒移，記得改成 {target:g}"})
        if s * (price - stop) <= 0:
            issues.append({"level": "alarm", "key": "sl_cross",
                           "text": f"{'加碼單' if core else ''}停損 {stop:g} 已經在現價另一側"})
            stop = None
    out["core_ticket"] = core_ticket
    out["inputs"] = {"direction": direction, "price": round(float(price), 2),
                     "balance": round(float(snap["balance"]), 2), "equity": round(float(snap["equity"]), 2),
                     "stop": stop, "core_stop": core_stop, "symbol": sym,
                     "positions": [{"lots": float(p["volume"]), "entry": float(p["price"]), "core": p in core}
                                   for p in gold]}
    return out


# ---------------------------------------------------------------------------
# 檢查與通知
# ---------------------------------------------------------------------------

def _label(view):
    acc = view.get("account") or {}
    return f"滾倉｜{acc.get('company') or 'MT5'} {acc.get('login') or ''}".strip()


def _raise(key, sig, text, now, label):
    """
    持續中的問題：第一次發，之後每 REPEAT_SECONDS 再發；sig(問題本身，例如哪幾張單、停損價)變了也馬上發。
    文字裡的即時數字(槓桿幾倍)每幾秒都在變，不能拿來判斷「變了」，不然會每次回報都發。
    """
    a = _state["alerts"].get(key)
    if a is None or now - a["last_sent"] >= REPEAT_SECONDS or a["sig"] != sig:
        times = (a["times"] + 1) if a else 1
        _send(f"{label}\n{text}" + (f"\n（第 {times} 次提醒）" if times > 1 else ""))
        _state["alerts"][key] = {"sig": sig, "text": text, "last_sent": now, "since": a["since"] if a else now,
                                 "times": times}
    else:
        a["text"] = text


def _resolve_missing(active_keys, label):
    for key in [k for k in _state["alerts"] if k not in active_keys]:
        a = _state["alerts"].pop(key)
        _send(f"{label}\n✅ 已處理：{a['text'].splitlines()[0]}")


def evaluate(now=None):
    """依最新回報檢查一次。回傳這次在意的問題 key(測試用)。"""
    now = time.time() if now is None else now
    view = account_view(now)
    label = _label(view)
    _state["last_eval"] = now
    # 斷線：只在有持倉時在意
    if view["age_seconds"] is not None and not view["connected"]:
        if _state["had_positions"] and not _state["disconnected"]:
            _send(f"{label}\n🔌 EA 已經 {int(view['age_seconds'] // 60)} 分鐘沒回報(電腦或 MT5 可能關了)。"
                  f"停損在券商那邊，不受影響；但加碼／停損提醒會停。")
            _state["disconnected"] = True
        return set()          # 舊資料不拿來判斷其他事
    if _state["disconnected"]:
        _send(f"{label}\n🔌 EA 恢復回報")
        _state["disconnected"] = False

    active = {}      # key -> (sig, 文字)
    for iss in view["issues"]:
        if iss["key"] in ("no_sl", "mixed", "noprice", "sl_cross"):
            active[iss["key"]] = (iss["text"], "🚨 " + iss["text"])
        elif iss["key"] == "sl_mismatch":
            active[iss["key"]] = (iss["text"], "⚠️ " + iss["text"])
    inputs = view["inputs"]
    _state["had_positions"] = bool(view["positions"])
    if inputs:
        rules = get_rules()
        try:
            r = position_roll.calc({**inputs, **rules, "ladder_steps": 1})
        except ValueError as e:
            r = None
            active["calc"] = (str(e), f"🚨 計算失敗：{e}")
        if r:
            st, cap = r["status"], rules["max_leverage"]
            if st["leverage"] is None or st["leverage"] > float(cap) + 1e-9:
                rd = r["reduce"] or {}
                plan = "、".join(f"{p['entry']:g} 那筆平 {p['close_lots']:g} 手" for p in rd.get("plan") or [])
                active["over_leverage"] = ("over", f"🚨 實際槓桿 {st['leverage'] or 0:.1f} 倍超過上限 {float(cap):g} 倍"
                                           + (f"\n建議減碼 {rd.get('close_lots'):g} 手：{plan}" if plan else ""))
            if r["stop"]["kept"] is not None and r["stop"]["kept"] < 0:
                active["principal"] = ((inputs["stop"], inputs.get("core_stop")),
                                       f"🚨 停損全部打到會虧到本金 {abs(r['stop']['kept']):.2f}")
            _check_add(r, inputs, rules, label)
    for key, (sig, text) in active.items():
        _raise(key, sig, text, now, label)
    _resolve_missing(set(active), label)
    if not view["positions"]:
        _state["add_keys"].clear()
    return set(active)


def _check_add(r, inputs, rules, label):
    """價格離最有利那筆進場價 ≥ 加碼間距、規則允許加 → 通知一次(同一個最有利進場價＋手數只通知一次)。"""
    s = 1 if inputs["direction"] == "long" else -1
    entries = [p["entry"] for p in inputs["positions"]]
    anchor = max(entries) if s == 1 else min(entries)
    step = float(rules["add_step"])
    a = r["add_now"]
    if s * (inputs["price"] - anchor) < step or a["lots"] <= 0:
        return
    key = (round(anchor, 2), a["lots"])
    if key in _state["add_keys"]:
        return
    _state["add_keys"].add(key)
    side = "Buy" if s == 1 else "Sell"
    _send(f"{label}\n📈 到加碼點了：現價 {inputs['price']:g}，離上次進場 {anchor:g} 已 {abs(inputs['price'] - anchor):.1f} 點\n"
          f"可加 {a['lots']:g} 手（{side}），{'加碼單' if inputs.get('core_stop') is not None else ''}停損 {a['stop']:g}\n"
          f"加完 {a['total_lots']:g} 手、槓桿 {a['leverage']:.1f} 倍，停損全部打到仍保住 {a['kept_at_stop']:.2f}\n"
          + ("成交後記得把其他加碼單的停損也設成同一個價位；核心單停損照舊" if inputs.get("core_stop") is not None
             else "成交後記得把舊單停損也設成同一個價位"))


def _loop():
    while True:
        time.sleep(CHECK_SECONDS)
        try:
            with _lock:
                has = _state["snapshot"] is not None
            if has:
                evaluate()
        except Exception as e:
            logger.exception(f"滾倉同步：背景檢查失敗: {e}")


def start():
    """沒設 ROLL_SYNC_TOKEN 就不啟動。"""
    global _thread
    if not enabled() or _thread is not None:
        return False
    _thread = threading.Thread(target=_loop, daemon=True, name="roll-sync")
    _thread.start()
    logger.info(f"滾倉同步已啟動(商品含 {symbol_hint()})")
    return True


def status(now=None):
    v = account_view(now)
    with _lock:
        alerts = {k: {"text": a["text"], "times": a["times"]} for k, a in _state["alerts"].items()}
    return {**v, "rules": get_rules() if enabled() else None, "active_alerts": alerts,
            "symbol_hint": symbol_hint()}
