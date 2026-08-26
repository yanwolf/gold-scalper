"""
幣安期貨(USDⓈ-M)下單執行模組 —— 測試網優先。

用途：讓黃金模擬單系統能實際在幣安期貨(目前鎖定XAUUSDT)送出市價單，
驗證「訊號 -> 換算部位大小 -> 下單 -> 查詢部位」這條流程能不能跑通。
現階段刻意設計成獨立的手動測試模組，不會自動接上paper_trading.py的
開倉/平倉事件——因為現在同時有1分K/5分K/15分K三個模擬單引擎平行運作，
如果三個都自動對同一個真實帳戶的同一個部位下單，會互相打架，這需要
另外設計「哪個引擎負責真實下單」才能安全接上，先確保這個模組本身能
正常運作、驗證認證/簽章/精度換算都對，再談自動接軌的事。

安全設計：
- 沒有設定BINANCE_API_KEY/BINANCE_API_SECRET時整個模組靜默停用，
  不影響其他功能(跟db.py/notifier.py同樣的設計原則)
- BINANCE_USE_TESTNET預設值是"1"(測試網)，要故意設成"0"才會打正式環境，
  避免不小心不小心接到真錢帳戶去
- 所有函式都回傳(success, data_or_error)，不會讓例外往外亂噴

參考資料：官方文件 https://developers.binance.com/docs/derivatives/usds-margined-futures/general-info
(內容更新於2026/8/25)，測試網(Demo Trading) REST base是 https://demo-fapi.binance.com，
簽章方式是HMAC SHA256，把所有參數(含timestamp)組成query string後用secret key簽章。
"""

import os
import time
import hmac
import hashlib
import logging
from urllib.parse import urlencode

import requests

logger = logging.getLogger("execution")

MAINNET_BASE_URL = "https://fapi.binance.com"
TESTNET_BASE_URL = "https://demo-fapi.binance.com"

DEFAULT_SYMBOL = os.getenv("BINANCE_GOLD_SYMBOL", "xauusdt").upper()

_symbol_precision_cache = {}


def use_testnet():
    return os.getenv("BINANCE_USE_TESTNET", "1") != "0"


def _base_url():
    return TESTNET_BASE_URL if use_testnet() else MAINNET_BASE_URL


def is_enabled():
    return bool(os.getenv("BINANCE_API_KEY") and os.getenv("BINANCE_API_SECRET"))


def status():
    """給dashboard/API endpoint顯示目前執行模組的狀態用。"""
    return {
        "enabled": is_enabled(),
        "testnet": use_testnet(),
        "base_url": _base_url(),
        "symbol": DEFAULT_SYMBOL,
    }


def _sign(params: dict) -> dict:
    """依官方文件的HMAC SHA256簽章方式，把params組成query string、算出簽章，回傳含簽章的dict。"""
    secret = os.getenv("BINANCE_API_SECRET", "")
    query_string = urlencode(params)
    signature = hmac.new(secret.encode("utf-8"), query_string.encode("utf-8"), hashlib.sha256).hexdigest()
    params = dict(params)
    params["signature"] = signature
    return params


def _signed_request(method, path, params=None):
    """呼叫需要簽章的私有端點(帳戶、下單、部位查詢等)。回傳(success, data_or_error)。"""
    if not is_enabled():
        return False, "尚未設定 BINANCE_API_KEY / BINANCE_API_SECRET"

    params = dict(params or {})
    params["timestamp"] = int(time.time() * 1000)
    params.setdefault("recvWindow", 5000)
    signed_params = _sign(params)

    url = f"{_base_url()}{path}"
    headers = {"X-MBX-APIKEY": os.getenv("BINANCE_API_KEY")}

    try:
        resp = requests.request(method, url, headers=headers, params=signed_params, timeout=10)
        data = resp.json()
        if resp.status_code >= 400:
            logger.error(f"幣安API錯誤({resp.status_code}): {data}")
            return False, data
        return True, data
    except Exception as e:
        logger.error(f"幣安API請求失敗: {e}")
        return False, str(e)


def get_symbol_precision(symbol=None):
    """
    查合約的數量精度(quantityPrecision)，下單數量要依這個精度四捨五入，
    不然幣安會直接拒絕訂單。結果快取起來，不用每次下單都重查一次。
    這是公開端點，不需要簽章。
    """
    symbol = symbol or DEFAULT_SYMBOL
    if symbol in _symbol_precision_cache:
        return _symbol_precision_cache[symbol]

    try:
        resp = requests.get(f"{_base_url()}/fapi/v1/exchangeInfo", timeout=10)
        data = resp.json()
        for s in data.get("symbols", []):
            if s["symbol"] == symbol:
                precision = s.get("quantityPrecision", 3)
                _symbol_precision_cache[symbol] = precision
                return precision
    except Exception as e:
        logger.error(f"查詢合約精度失敗: {e}")

    return 3  # 查不到的話用一個保守預設值(0.001精度，符合先前查到的XAUUSDT最小交易單位)


def calculate_quantity(risk_usd, sl_points, symbol=None):
    """
    (輔助試算用，不再是實際下單的主要依據，見下方open_position()的修正記錄)
    依「這筆單願意承擔多少美元風險」和「停損距離(points)」，換算出對應的下單數量。
    因為幣安XAUUSDT是1張合約=1金衡盎司=每點1美元價格波動對應1美元損益，
    所以 數量 = 風險金額 / 停損點數。
    """
    if sl_points <= 0:
        return 0.0
    quantity = risk_usd / sl_points
    precision = get_symbol_precision(symbol)
    return round(quantity, precision)


def estimate_risk(quantity, sl_points, account_balance=None, symbol=None):
    """
    反過來算：使用者自己決定要下多少口數，這裡回推「如果觸及停損，實際會虧多少
    美元」以及「這筆虧損佔帳戶餘額的百分比」，給使用者參考用，不會自動套用或
    修改任何設定——真正要下單用哪個數量，由使用者自己決定並填進設定裡。

    改成這個方向的原因(使用者本人在對話裡提出的理由)：ATR動態停損模式下，
    每一筆的停損距離(sl_points)都不一樣，如果反過來用「固定風險金額 / 停損距離」
    去derive下單數量，會導致每筆單的實際部位大小跟著波動、難以預期；改成
    「使用者自己決定固定口數」，部位大小本身變得穩定可預期，風險則誠實地
    隨著市場波動大小浮動，這樣使用者才有一個穩定的基準去判斷「我能接受多大
    的部位」。

    account_balance沒有提供的話，會即時查詢目前帳戶的可用餘額。
    回傳 (success, data_or_error)，成功時data包含 dollar_risk、risk_pct、account_balance。
    """
    if quantity <= 0 or sl_points <= 0:
        return False, "數量和停損距離都必須大於0"

    if account_balance is None:
        success, balance_data = get_account_balance()
        if not success:
            return False, balance_data
        account_balance = 0.0
        for asset in balance_data:
            if asset.get("asset") == "USDT":
                account_balance = float(asset.get("availableBalance", 0))
                break

    dollar_risk = quantity * sl_points
    risk_pct = (dollar_risk / account_balance * 100) if account_balance > 0 else None

    return True, {
        "quantity": quantity,
        "sl_points": sl_points,
        "account_balance": account_balance,
        "dollar_risk": round(dollar_risk, 2),
        "risk_pct": round(risk_pct, 2) if risk_pct is not None else None,
    }


def set_leverage(leverage, symbol=None):
    symbol = symbol or DEFAULT_SYMBOL
    return _signed_request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage})


def get_account_balance():
    """查帳戶餘額，主要用來確認API金鑰有沒有接對、測試網/正式環境有沒有搞錯。"""
    return _signed_request("GET", "/fapi/v2/balance")


def get_position_info(symbol=None):
    symbol = symbol or DEFAULT_SYMBOL
    return _signed_request("GET", "/fapi/v2/positionRisk", {"symbol": symbol})


def place_market_order(side, quantity, symbol=None, reduce_only=False):
    """
    送出市價單。side是"BUY"或"SELL"，quantity是張數(已經套用過精度)。
    reduce_only=True代表這是平倉單(只能減少部位、不會反向開新倉)，
    下平倉單時一律加這個保護，避免手誤或邏輯錯誤導致意外開出反向部位。
    """
    symbol = symbol or DEFAULT_SYMBOL
    if quantity <= 0:
        return False, "下單數量必須大於0"

    precision = get_symbol_precision(symbol)
    quantity = round(quantity, precision)

    params = {
        "symbol": symbol,
        "side": side,
        "type": "MARKET",
        "quantity": quantity,
    }
    if reduce_only:
        params["reduceOnly"] = "true"
    return _signed_request("POST", "/fapi/v1/order", params)


def open_position(direction, quantity, symbol=None):
    """
    依訊號方向開倉，quantity是直接指定的下單數量(張數)——不再從風險金額反推。

    修正記錄：原本這裡是「給風險金額+停損距離，反推出下單數量」，但ATR動態
    停損模式下每筆的停損距離都不一樣，用這種方式算出來的部位大小會跟著每筆
    的停損距離浮動，不容易預期、也不容易對帳。改成直接指定固定數量，部位
    大小變得穩定可預期，使用者可以搭配estimate_risk()先試算這個數量對應的
    風險大小，自己決定要用多少，而不是讓系統自動幫忙決定。

    回傳(success, order_result_or_error)。
    """
    if not is_enabled():
        return False, "執行模組未啟用(未設定API金鑰)"

    if quantity <= 0:
        return False, "下單數量必須大於0"

    side = "BUY" if direction == "bullish" else "SELL"
    return place_market_order(side, quantity, symbol=symbol)


def close_position(direction, symbol=None):
    """
    平掉目前的部位。direction是原本開倉時的方向(平倉方向要反過來)，
    實際數量直接查詢目前帳戶部位大小，不用呼叫端自己算，避免因為
    浮點數誤差或多筆疊加導致平倉數量對不上實際部位。
    """
    if not is_enabled():
        return False, "執行模組未啟用(未設定API金鑰)"

    success, position_data = get_position_info(symbol=symbol)
    if not success:
        return False, position_data

    position_amt = 0.0
    target_symbol = symbol or DEFAULT_SYMBOL
    for p in position_data:
        if p["symbol"] == target_symbol:
            position_amt = float(p["positionAmt"])
            break

    if position_amt == 0:
        return False, "目前沒有未平倉部位可以平"

    side = "SELL" if position_amt > 0 else "BUY"
    quantity = abs(position_amt)
    return place_market_order(side, quantity, symbol=symbol, reduce_only=True)
