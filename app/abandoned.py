"""
已放棄追蹤的部位(BINANCE_LESSONS.md 第 16 條 r99)

交易所一直拒絕平倉(-2022 ReduceOnly Order is rejected)、程式怎麼重送都不會好時，使用者可以「停止追蹤此部位」：
帳上移除、交易所上的部位與掛單不動(撤單也平不掉時，留著停損多一層保護)。被放棄的部位記在這裡：
  - 存進資料庫(app_settings 的 ABANDONED_DB_KEY，JSON)，重啟後還在；
  - 同一個實體帳戶＋同一個商品，清空前不再送真實開倉單(risk_guard 擋，模擬單照常)；
  - 引擎定期對帳(reconcile)：交易所那一側清空了就自動解除並通知；
  - 交易所自檢另外列出(preflight)。
讀不到資料庫時保守當成「有」(第8條r43：讀取失敗≠沒有)，跟緊急停止一樣。
"""
import json
import logging
import threading
from datetime import datetime, timezone

from app import db

logger = logging.getLogger("abandoned")

ABANDONED_DB_KEY = "abandoned_positions"
_lock = threading.Lock()
_cache = {"loaded": False, "items": {}}


def _key(account_key, symbol):
    return f"{account_key}|{symbol}"


def _load():
    """(ok, items)。已經讀過就用快取；讀不到時 ok=False(呼叫端保守處理)。"""
    with _lock:
        if _cache["loaded"]:
            return True, dict(_cache["items"])
    ok, values = db.load_app_settings()
    if not ok:
        return False, {}
    raw = values.get(ABANDONED_DB_KEY)
    try:
        items = json.loads(raw) if raw else {}
        if not isinstance(items, dict):
            items = {}
    except (TypeError, ValueError):
        logger.error(f"已放棄追蹤清單內容壞了，當成空的：{raw!r}")
        items = {}
    with _lock:
        _cache["items"] = items
        _cache["loaded"] = True
        return True, dict(items)


def _save(items):
    with _lock:
        _cache["items"] = dict(items)
        _cache["loaded"] = True
    db.save_app_settings({ABANDONED_DB_KEY: json.dumps(items, ensure_ascii=False)})


def reset_for_tests():
    with _lock:
        _cache["loaded"] = False
        _cache["items"] = {}


def add(account_key, symbol, **info):
    ok, items = _load()
    if not ok:
        items = dict(_cache["items"])
    entry = dict(info, account_key=account_key, symbol=symbol, at=datetime.now(timezone.utc).isoformat())
    items[_key(account_key, symbol)] = entry
    _save(items)
    return entry


def remove(account_key, symbol):
    ok, items = _load()
    if not ok:
        return False
    if items.pop(_key(account_key, symbol), None) is None:
        return False
    _save(items)
    return True


def blocked(account_key, symbol):
    """
    這個實體帳戶的這個商品有沒有已放棄追蹤的部位：回傳那一筆(dict)或 None。
    讀不到資料庫：回 {"unknown": True}(保守當成有，真實開倉照擋)。
    """
    ok, items = _load()
    if not ok:
        return {"unknown": True}
    return items.get(_key(account_key, symbol))


def list_all():
    """(ok, [entries])"""
    ok, items = _load()
    return ok, list(items.values())
