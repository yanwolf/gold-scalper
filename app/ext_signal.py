"""
外部訊號回報(r123)：把 lab 模擬引擎「現在的部位」回報給 cfd-live，由它在 MT5(Pepperstone 模擬帳戶)照著下單，
量出換到 MT5 之後的真實差異——點差、滑價、延遲、幣安與券商的報價差(cfd-live 的 app/ext.py、儀表板「外部訊號」)。

背景：幣安每筆成本約 3.4 點，15 分K 纏論扣完是負的；MT5 券商約 0.4 點。lab 的 15 分K 引擎(窄中樞前瞻測試)
11/9 起 58 筆 PF 1.72、MT5 重算 PF 1.62，所以在等 100 筆的同時，先在模擬帳戶上真的下單，確認成本假設。

做法是回報「狀態」不是「事件」：
  · 背景執行緒每 POLL_SECONDS 秒看一次要回報的引擎；部位一有變化(開倉、平倉、換一筆)就馬上送，沒變化每 HEARTBEAT_SECONDS 秒送一次。
  · 內容：position = None 或 {key, direction, entry_time, entry_price, sl_price, sl_points}，加上最近幾筆已平倉的模擬單
    (key、出場價、點數、出場原因)，cfd-live 用來對照。key = engine_id + 進場時間，重啟後一樣。
  · 送失敗就等下一輪再送(狀態是最新的，不用補送舊的)；cfd-live 那邊做過的 key 不會重做，重送不會重複下單。
  · 不碰引擎本身：只讀 engine._position 與 engine._closed_trades_memory，回報失敗不影響模擬單。

設定(環境變數，只在 lab 服務設)：
  EXT_SIGNAL_URL      例如 https://cfd.tzujenlab.com/ext/gold-scalper
  EXT_SIGNAL_TOKEN    跟 cfd-live 的 EXT_TOKEN 一樣
  EXT_SIGNAL_ENGINES  要回報的引擎，逗號分隔，例如 chan_profile_900
只在 APP_ROLE=lab 跑：正式端也有同名的 chan_profile_900，兩邊都回報的話 cfd-live 會收到互相矛盾的部位、來回開平倉。
"""
import logging
import os
import threading
import time
import uuid

import requests

from app import role as role_module

logger = logging.getLogger("ext_signal")

POLL_SECONDS = 2
HEARTBEAT_SECONDS = 20
RECENT_CLOSED = 20
TIMEOUT = 5

URL = os.getenv("EXT_SIGNAL_URL", "").strip()
TOKEN = os.getenv("EXT_SIGNAL_TOKEN", "").strip()
ENGINES = [x.strip() for x in os.getenv("EXT_SIGNAL_ENGINES", "").split(",") if x.strip()]

BOOT = uuid.uuid4().hex[:12]
_state = {}            # engine_id -> {"seq", "last_sent", "last_key", "last_ok", "last_error", "sl_points": {key: 距離}}
_lock = threading.Lock()
_thread = None


def enabled_reason():
    """回傳 (開不開, 原因)"""
    if not (URL and TOKEN and ENGINES):
        return False, "沒有設定 EXT_SIGNAL_URL／EXT_SIGNAL_TOKEN／EXT_SIGNAL_ENGINES"
    if role_module.is_live():
        return False, "只在 lab 服務回報(正式端也有同名引擎，兩邊都回報會互相矛盾)"
    return True, None


def trade_key(engine_id, entry_time):
    return f"{engine_id}:{entry_time}"


def build_payload(engine, st):
    """把引擎現在的部位整理成要送的內容。st 是這個引擎的回報狀態(記住每筆的初始停損距離)"""
    pos = getattr(engine, "_position", None)
    position = None
    if pos and pos.get("entry_time") and pos.get("direction") in ("bullish", "bearish") and not pos.get("pending_close"):
        key = trade_key(engine.engine_id, pos["entry_time"])
        entry = float(pos["entry_price"])
        sl = pos.get("sl_price")
        slp = st.setdefault("sl_points", {}).get(key)
        if slp is None and sl is not None:
            # 第一次看到這筆時記下「初始停損距離」(之後移動停損會改 sl_price，cfd-live 的安全停損用初始距離算)
            slp = round(abs(entry - float(sl)), 4)
            st["sl_points"][key] = slp
            for k in list(st["sl_points"])[:-50]:
                st["sl_points"].pop(k, None)
        position = {"key": key, "direction": pos["direction"], "entry_time": pos["entry_time"], "entry_price": entry,
                    "sl_price": float(sl) if sl is not None else None, "sl_points": slp,
                    "trailing_active": bool(pos.get("trailing_active")),
                    "reason": (pos.get("chan_reason") or "")[:120]}
    closed = []
    for t in list(getattr(engine, "_closed_trades_memory", []) or [])[-RECENT_CLOSED:]:
        if not t.get("entry_time") or t.get("pnl_points") is None:
            continue
        closed.append({"key": trade_key(engine.engine_id, t["entry_time"]), "direction": t.get("direction"),
                       "entry_price": t.get("entry_price"), "exit_price": t.get("exit_price"),
                       "exit_time": t.get("exit_time"), "pnl_points": t.get("pnl_points"),
                       "exit_reason": t.get("exit_reason")})
    return {"source": "gold-scalper", "engine_id": engine.engine_id, "boot": BOOT, "position": position,
            "recent_closed": closed, "sent_at": int(time.time())}


def maybe_send(engine, now=None, post=None):
    """看一個引擎要不要送；回傳 True＝送了而且成功、False＝送了但失敗、None＝這輪不用送"""
    now = now or time.time()
    post = post or requests.post
    with _lock:
        st = _state.setdefault(engine.engine_id, {"seq": 0, "last_sent": 0, "last_key": "∅", "last_ok": None,
                                                  "last_error": None, "fails": 0})
        payload = build_payload(engine, st)
        key = (payload["position"] or {}).get("key")
        changed = key != st["last_key"]
        if not changed and now - st["last_sent"] < HEARTBEAT_SECONDS and st["fails"] == 0:
            return None
        st["seq"] += 1
        payload["seq"] = st["seq"]
    try:
        r = post(URL, json=payload, headers={"x-token": TOKEN}, timeout=TIMEOUT)
        ok = 200 <= int(getattr(r, "status_code", 0)) < 300
        err = None if ok else f"HTTP {getattr(r, 'status_code', '?')} {str(getattr(r, 'text', ''))[:200]}"
    except Exception as e:                     # 網路錯誤：下一輪(2 秒後)再送
        ok, err = False, f"{type(e).__name__}: {e}"[:200]
    with _lock:
        st["last_sent"] = now
        if ok:
            st.update({"last_key": key, "last_ok": now, "last_error": None, "fails": 0})
        else:
            st["fails"] += 1
            st["last_error"] = err
            if st["fails"] in (3, 30, 300):
                logger.warning(f"外部訊號回報失敗 {st['fails']} 次({engine.engine_id})：{err}")
    return ok


def status(engine_id):
    """給引擎績效摘要(網頁)看：有沒有在回報、上次成功多久前、錯誤"""
    if engine_id not in ENGINES:
        return None
    on, why = enabled_reason()
    if not on:
        return {"enabled": False, "reason": why}
    with _lock:
        st = dict(_state.get(engine_id) or {})
    last_ok = st.get("last_ok")
    return {"enabled": True, "url": URL, "last_ok_ago": int(time.time() - last_ok) if last_ok else None,
            "last_error": st.get("last_error"), "fails": st.get("fails", 0)}


def _loop(get_engines):
    while True:
        try:
            engines = get_engines()
            for eid in ENGINES:
                eng = engines.get(eid)
                if eng is not None:
                    maybe_send(eng)
        except Exception as e:                 # 回報出錯不能影響其他東西
            logger.error(f"外部訊號回報迴圈錯誤：{e}")
        time.sleep(POLL_SECONDS)


def start(get_engines):
    """服務啟動時呼叫；沒設定或不是 lab 就什麼都不做"""
    global _thread
    on, why = enabled_reason()
    if not on:
        if URL or ENGINES:
            logger.warning(f"外部訊號回報沒有啟動：{why}")
        return False
    missing = [e for e in ENGINES if e not in get_engines()]
    if missing:
        logger.warning(f"外部訊號回報：找不到引擎 {missing}(這個服務沒有載入)")
    if _thread is None or not _thread.is_alive():
        _thread = threading.Thread(target=_loop, args=(get_engines,), daemon=True, name="ext-signal")
        _thread.start()
        logger.info(f"外部訊號回報啟動：{ENGINES} → {URL}")
    return True
