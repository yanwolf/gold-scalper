"""
r128：幣安 XAUUSDT 與券商 XAUUSD(CFD)的價差(基差)紀錄。只看資料，不碰下單。

為什麼要量：gold-scalper 的訊號用幣安算(週末也有成交、週一跳空會提早反應)，下單在 MT5 券商。
平日兩邊走勢一樣，但價差不是定值——要知道它晃多大、新聞時跳多大、持倉期間會漂多少，
才能決定「繼續用幣安當訊號源、價位用即時價差換算」夠不夠，還是要改資料源。

資料來源：r125 的唯讀 EA 每 3 秒回報 Vantage 的 XAUUSD+ 報價(/tools/roll-sync)，同一時間取幣安的即時盤口，
兩邊都新鮮才記一筆：價差 = 券商中價 − 幣安中價。

  · 每分鐘彙總一筆(筆數、總和、平方和、最高、最低)，存資料庫 basis_minutes，重啟後讀回最近 14 天。
  · 券商報價時間超過 STALE_CFD_SECONDS 沒變 ＝ 休市(週末、每天的休息時段)：不記價差，記下「收盤前的價差」。
  · 休市超過 REOPEN_MIN_CLOSED 後恢復報價 ＝ 一次開盤：記錄收盤前／開盤後的價差、券商跳空、休市期間幣安走了多少(basis_events)。
  · 休市期間：幣安價 + 收盤前價差 ＝ 券商開盤的預估價(週末預警用，見 roll_sync)。
"""
import json
import logging
import math
import threading
import time
from collections import deque
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

STALE_CFD_SECONDS = 120        # 券商報價這麼久沒變 → 視為休市
BINANCE_FRESH_SECONDS = 10     # 幣安盤口時間離現在超過這麼久 → 不拿來算
REOPEN_MIN_CLOSED = 1800       # 休市超過 30 分鐘再開才算一次「開盤」
KEEP_DAYS = 14
DRIFT_HORIZONS = (15, 60)      # 分鐘：持倉期間價差會漂多少

_lock = threading.Lock()


def _fresh_state():
    return {
        "minutes": deque(maxlen=KEEP_DAYS * 1440),   # 已收完的分鐘：{"m", "n", "sum", "sumsq", "min", "max", "cfd", "bn"}
        "cur": None,                                  # 正在累積的那一分鐘
        "loaded": False,
        "last_cfd_time": None, "last_cfd_change": None, "last_cfd_mid": None,
        "closed": False, "closed_since": None,
        "close_basis": None, "close_cfd": None, "close_bn": None,
        "last_basis": None, "last_basis_at": None,
        "events": deque(maxlen=30),
    }


_state = _fresh_state()


def reset():
    """測試用。"""
    global _state
    with _lock:
        _state = _fresh_state()


# ---------------------------------------------------------------------------
# 資料庫(盡力而為：沒有資料庫或寫入失敗只記 log)
# ---------------------------------------------------------------------------

def _db():
    from app import db
    return db


def _save_minute(rec):
    try:
        _db().save_basis_minute(rec)
    except Exception as e:
        logger.warning(f"價差紀錄寫入失敗: {e}")


def _save_event(ev):
    try:
        _db().insert_basis_event(ev)
    except Exception as e:
        logger.warning(f"開盤事件寫入失敗: {e}")


def _ensure_loaded():
    if _state["loaded"]:
        return
    _state["loaded"] = True
    try:
        rows = _db().load_basis_minutes(days=KEEP_DAYS)
        evs = _db().load_basis_events(limit=30)
    except Exception as e:
        logger.warning(f"讀取價差紀錄失敗: {e}")
        return
    for r in rows or []:
        _state["minutes"].append(r)
    for ev in evs or []:
        _state["events"].append(ev)


# ---------------------------------------------------------------------------
# 收資料
# ---------------------------------------------------------------------------

def _binance_mid(now):
    try:
        from app.binance_client import binance_streamer
        tick = binance_streamer.get_latest()
    except Exception:
        return None
    if not isinstance(tick, dict):
        return None
    try:
        bid, ask = float(tick["bid"]), float(tick["ask"])
        t = float(tick.get("time") or 0) / 1000.0
    except (TypeError, ValueError, KeyError):
        return None
    if bid <= 0 or ask <= 0 or abs(now - t) > BINANCE_FRESH_SECONDS:
        return None
    return (bid + ask) / 2


def _flush_minute():
    cur = _state["cur"]
    if cur and cur["n"] > 0:
        rec = dict(cur)
        _state["minutes"].append(rec)
        _save_minute(rec)
    _state["cur"] = None


def record(cfd_tick, now=None, bn_mid=None):
    """
    EA 每次回報呼叫一次。cfd_tick = {"bid", "ask", "time"}(time 是券商伺服器時間，只拿來看有沒有在變)。
    bn_mid 測試用；平常從幣安串流取。回傳這次的價差(沒記就 None)。
    """
    now = time.time() if now is None else now
    try:
        bid, ask = float(cfd_tick["bid"]), float(cfd_tick["ask"])
    except (TypeError, ValueError, KeyError):
        return None
    if bid <= 0 or ask <= 0:
        return None
    cfd_mid = (bid + ask) / 2
    tick_time = cfd_tick.get("time")
    bn = bn_mid if bn_mid is not None else _binance_mid(now)
    with _lock:
        _ensure_loaded()
        st = _state
        changed = tick_time != st["last_cfd_time"] or cfd_mid != st["last_cfd_mid"]
        if changed or st["last_cfd_change"] is None:
            # 休市後恢復報價
            if st["closed"] and changed:
                closed_for = now - (st["closed_since"] or now)
                if closed_for >= REOPEN_MIN_CLOSED and st["close_cfd"] is not None:
                    ev = {"closed_at": _iso(st["closed_since"]), "reopened_at": _iso(now),
                          "closed_minutes": round(closed_for / 60),
                          "close_basis": _r(st["close_basis"]), "close_cfd": _r(st["close_cfd"]),
                          "open_cfd": _r(cfd_mid), "cfd_gap": _r(cfd_mid - st["close_cfd"]),
                          "close_bn": _r(st["close_bn"]), "open_bn": _r(bn),
                          "bn_move": _r(bn - st["close_bn"]) if (bn is not None and st["close_bn"] is not None) else None,
                          "open_basis": _r(cfd_mid - bn) if bn is not None else None,
                          "projected_open": _r(bn + st["close_basis"]) if (bn is not None and st["close_basis"] is not None)
                          else None}
                    if ev["projected_open"] is not None:
                        ev["projection_error"] = _r(cfd_mid - ev["projected_open"])
                    st["events"].append(ev)
                    _save_event(ev)
                st["closed"], st["closed_since"] = False, None
            st["last_cfd_time"], st["last_cfd_mid"], st["last_cfd_change"] = tick_time, cfd_mid, now
        elif not st["closed"] and now - st["last_cfd_change"] > STALE_CFD_SECONDS:
            st["closed"], st["closed_since"] = True, st["last_cfd_change"]
            st["close_basis"], st["close_cfd"] = st["last_basis"], st["last_cfd_mid"]
            st["close_bn"] = (st["last_cfd_mid"] - st["last_basis"]) if st["last_basis"] is not None else None
            _flush_minute()
        if not changed or bn is None:      # 休市中報價不會變，也會在這裡擋掉
            return None
        basis = cfd_mid - bn
        st["last_basis"], st["last_basis_at"] = basis, now
        m = int(now // 60)
        if st["cur"] and st["cur"]["m"] != m:
            _flush_minute()
        if st["cur"] is None:
            st["cur"] = {"m": m, "n": 0, "sum": 0.0, "sumsq": 0.0, "min": basis, "max": basis, "cfd": cfd_mid, "bn": bn}
        c = st["cur"]
        c["n"] += 1
        c["sum"] += basis
        c["sumsq"] += basis * basis
        c["min"], c["max"] = min(c["min"], basis), max(c["max"], basis)
        c["cfd"], c["bn"] = cfd_mid, bn
        return basis


# ---------------------------------------------------------------------------
# 給網頁與其他模組
# ---------------------------------------------------------------------------

def _r(x, n=2):
    return None if x is None else round(x, n)


def _iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else None


def _pct(sorted_vals, q):
    if not sorted_vals:
        return None
    k = (len(sorted_vals) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def stats(window_seconds, now=None):
    """window 內每分鐘的價差：平均、標準差、範圍、分鐘平均的 5%／95%，以及 15／60 分鐘內價差漂移(絕對值)的分布。"""
    now = time.time() if now is None else now
    lo_m = int((now - window_seconds) // 60)
    with _lock:
        _ensure_loaded()
        recs = [r for r in _state["minutes"] if r["m"] >= lo_m]
        if _state["cur"] and _state["cur"]["m"] >= lo_m and _state["cur"]["n"]:
            recs.append(dict(_state["cur"]))
    n = sum(r["n"] for r in recs)
    if not n:
        return {"minutes": 0, "samples": 0}
    mean = sum(r["sum"] for r in recs) / n
    var = max(sum(r["sumsq"] for r in recs) / n - mean * mean, 0.0)
    means = {r["m"]: r["sum"] / r["n"] for r in recs if r["n"]}
    srt = sorted(means.values())
    drift = {}
    for h in DRIFT_HORIZONS:
        d = sorted(abs(v - means[m - h]) for m, v in means.items() if (m - h) in means)
        drift[str(h)] = {"count": len(d), "p50": _r(_pct(d, 0.5)), "p95": _r(_pct(d, 0.95)), "max": _r(d[-1] if d else None)}
    return {"minutes": len(recs), "samples": n, "mean": _r(mean), "std": _r(math.sqrt(var)),
            "min": _r(min(r["min"] for r in recs)), "max": _r(max(r["max"] for r in recs)),
            "p5": _r(_pct(srt, 0.05)), "p95": _r(_pct(srt, 0.95)), "drift": drift}


def current(now=None):
    """現在的價差(券商開盤中)，或休市中的收盤前價差。"""
    now = time.time() if now is None else now
    with _lock:
        st = dict(_state)
    out = {"closed": st["closed"], "basis": None, "age_seconds": None, "source": None,
           "closed_since": _iso(st["closed_since"]), "close_basis": _r(st["close_basis"])}
    if st["closed"] and st["close_basis"] is not None:
        out.update({"basis": _r(st["close_basis"]), "source": "close"})
    elif st["last_basis"] is not None and now - st["last_basis_at"] <= 300:
        out.update({"basis": _r(st["last_basis"]), "age_seconds": round(now - st["last_basis_at"]), "source": "live"})
    return out


def projected_cfd(now=None, bn_mid=None):
    """休市中：幣安中價 + 收盤前價差 ＝ 券商開盤的預估價。沒休市或資料不夠回 None。"""
    now = time.time() if now is None else now
    with _lock:
        closed, cb = _state["closed"], _state["close_basis"]
    if not closed or cb is None:
        return None
    bn = bn_mid if bn_mid is not None else _binance_mid(now)
    if bn is None:
        return None
    return {"projected": bn + cb, "binance": bn, "close_basis": cb}


def is_closed():
    with _lock:
        return _state["closed"]


def report(now=None):
    """網頁用：現在的價差、24 小時／7 天統計、最近幾次開盤。"""
    now = time.time() if now is None else now
    with _lock:
        _ensure_loaded()
        events = list(_state["events"])[-8:]
    return {"current": current(now), "day": stats(86400, now), "week": stats(7 * 86400, now),
            "events": list(reversed(events))}


def parse_event(raw):
    try:
        return json.loads(raw) if isinstance(raw, str) else dict(raw)
    except (TypeError, ValueError):
        return None
