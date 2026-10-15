"""
幣安 IP 權重協調(r98，BINANCE_LESSONS.md 第 6 條 r97／r98)

幣安的請求權重是「每個出口 IP、每分鐘」算總數(上限 2400)。lab 與 live 是兩個 Zeabur 服務，
可能共用同一個出口 IP；程序內的鎖管不到另一個服務，跨程序唯一的協調是每次回應帶回來的
`X-MBX-USED-WEIGHT-1M`(這個 IP 這一分鐘的總數，包含別的服務打的)。所以：

- 所有幣安回應都要 record()：K 線、下單、查部位、盤口、exchangeInfo。任何一個被 429／418，K 線都要停。
- K 線分兩種：
  - batch(回測、參數掃描)：整個程序排一條隊；這一分鐘已用到 PAUSE_AT(上限的一半)就等到下一分鐘，
    讓出來的一半留給實盤；429 冷卻中就等完再試(最多 MAX_RETRIES 次、單次最多 MAX_WAIT 秒)；418 封鎖中立刻停。
  - live(即時引擎回填、1小時K/日K快取、自檢)：不排 batch 的隊、不因權重過半等待——不能讓研究端的等待
    拖住實盤漏棒。冷卻中(429)或封鎖中(418)直接不送，當成這輪沒抓到，下一輪再來；被 429／418 也不重試。
- 下單、查部位不在這裡擋(停損在交易所上，下單路徑不因為這個模組改變行為)，只負責記錄。
"""
import math
import threading
import time

import requests

KLINES_URL = "https://fapi.binance.com/fapi/v1/klines"
WEIGHT_LIMIT_1M = 2400
PAUSE_AT = 1200               # batch：這一分鐘已用到這裡就讓路到下一分鐘
MAX_RETRIES = 3               # batch：429／冷卻最多等幾次
DEFAULT_429_WAIT = 60         # 沒有 Retry-After 時
DEFAULT_418_WAIT = 600
MAX_WAIT = 120                # batch：單次等待上限

_now = time.time              # 測試換掉
_sleep = time.sleep           # 測試換掉
_state_lock = threading.Lock()
_batch_lock = threading.Lock()   # 只有 batch 排隊；live 不碰這把鎖


class BinanceRateLimited(RuntimeError):
    """幣安限流(429)或封 IP(418)：暫時性，跟「這段期間沒有資料」不同。"""


def _fresh_state():
    return {"used": 0, "used_minute": None, "used_source": None,
            "count_429": 0, "count_418": 0, "cooldown_until": 0.0, "banned_until": 0.0,
            "last_limit_source": None, "waits": 0, "wait_seconds": 0.0, "skipped_live": 0}


_state = _fresh_state()


def reset_for_tests():
    with _state_lock:
        _state.clear()
        _state.update(_fresh_state())


def _retry_after(headers, default):
    try:
        v = float(headers.get("Retry-After"))
        return v if v > 0 else default
    except (TypeError, ValueError, AttributeError):
        return default


def record(resp, source=""):
    """記下任何一個幣安回應的權重標頭與 429／418。回傳狀態碼。resp 是 None 或格式不對時什麼都不做。"""
    if resp is None:
        return None
    status = getattr(resp, "status_code", None)
    headers = getattr(resp, "headers", None) or {}
    now = _now()
    with _state_lock:
        try:
            used = headers.get("X-MBX-USED-WEIGHT-1M") or headers.get("x-mbx-used-weight-1m")
        except AttributeError:
            used = None
        if used is not None:
            try:
                _state["used"] = int(used)
                _state["used_minute"] = int(now // 60)
                _state["used_source"] = source
            except (TypeError, ValueError):
                pass
        if status == 429:
            _state["count_429"] += 1
            _state["cooldown_until"] = max(_state["cooldown_until"], now + _retry_after(headers, DEFAULT_429_WAIT))
            _state["last_limit_source"] = source
        elif status == 418:
            _state["count_418"] += 1
            _state["banned_until"] = max(_state["banned_until"], now + _retry_after(headers, DEFAULT_418_WAIT))
            _state["last_limit_source"] = source
    return status


def used_this_minute():
    """這一分鐘(依最後一次看到的標頭)已用的權重；上一分鐘的數字已經歸零，回 0。"""
    with _state_lock:
        if _state["used_minute"] == int(_now() // 60):
            return _state["used"]
        return 0


def blocked():
    """(\"banned\"|\"cooldown\"|None, 剩幾秒)。封鎖優先。"""
    now = _now()
    with _state_lock:
        if _state["banned_until"] > now:
            return "banned", _state["banned_until"] - now
        if _state["cooldown_until"] > now:
            return "cooldown", _state["cooldown_until"] - now
    return None, 0.0


def _note_wait(seconds):
    with _state_lock:
        _state["waits"] += 1
        _state["wait_seconds"] += seconds


def snapshot():
    kind, left = blocked()
    with _state_lock:
        out = {k: _state[k] for k in ("count_429", "count_418", "waits", "wait_seconds", "skipped_live",
                                       "last_limit_source", "used_source")}
    out.update({"used_this_minute": used_this_minute(), "limit": WEIGHT_LIMIT_1M, "pause_at": PAUSE_AT,
                "blocked": kind, "blocked_seconds_left": round(left, 1)})
    return out


def _banned_error(left):
    return BinanceRateLimited(f"幣安已暫時封鎖這個 IP(418)，還要約 {math.ceil(left)} 秒：先前請求太密集。"
                              "封鎖期間不送任何K線請求；同 IP 的正式端也可能受影響，請檢查正式端")


def get_klines(params, mode="batch", source=None):
    """抓一頁K線(回傳 JSON list)。mode="batch" 排隊且會等待；mode="live" 不排隊、不等待、被擋就直接拋。"""
    source = source or f"klines-{mode}"
    if mode == "live":
        kind, left = blocked()
        if kind:
            with _state_lock:
                _state["skipped_live"] += 1
            if kind == "banned":
                raise _banned_error(left)
            raise BinanceRateLimited(f"幣安限流冷卻中(429，還要約 {math.ceil(left)} 秒)：這輪不抓，下一輪再試")
        resp = requests.get(KLINES_URL, params=params, timeout=15)
        status = record(resp, source)
        if status == 418:
            raise _banned_error(blocked()[1])
        if status == 429:
            raise BinanceRateLimited("幣安限流(429)：這輪不抓，下一輪再試")
        resp.raise_for_status()
        return resp.json()

    with _batch_lock:
        tries = 0
        while True:
            kind, left = blocked()
            if kind == "banned":
                raise _banned_error(left)
            if kind == "cooldown":
                if tries >= MAX_RETRIES:
                    raise BinanceRateLimited(f"幣安限流(429)：等了 {MAX_RETRIES} 次仍被擋。"
                                             "這是暫時性的，等 1～2 分鐘再跑(跟「沒有資料」不同)")
                tries += 1
                wait = float(min(max(math.ceil(left), 1), MAX_WAIT))
                _note_wait(wait)
                _sleep(wait)
            elif used_this_minute() >= PAUSE_AT:
                wait = float(61 - int(_now()) % 60)   # 權重以分鐘重算，等到下一分鐘
                _note_wait(wait)
                _sleep(wait)
            resp = requests.get(KLINES_URL, params=params, timeout=15)
            status = record(resp, source)
            if status == 418:
                raise _banned_error(blocked()[1])
            if status == 429:
                continue      # 冷卻已記下，迴圈開頭照 Retry-After 等
            resp.raise_for_status()
            return resp.json()
