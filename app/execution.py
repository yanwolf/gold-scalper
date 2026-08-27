"""
幣安期貨(USDⓈ-M)下單執行模組 —— 測試網優先，支援多帳戶。

用途：讓模擬單系統能實際在幣安期貨送出市價單，驗證「訊號 -> 換算部位大小 ->
下單 -> 查詢部位」這條流程能不能跑通。

多帳戶設計(修正記錄見README)：使用者未來計畫陸續加入大型加密貨幣(例如BTC)
交易，如果多個策略/商品共用同一個帳戶同時下單，幣安只認得「淨部位」，會
發生部位互相抵銷、跟各策略內部記錄的狀態對不上的問題(已在對話中討論過)。
最乾淨的解法是每個策略/商品用獨立的幣安子帳戶，這裡預先把執行模組改成
支援「多組具名帳戶」，之後真的要接BTC時，只要在Zeabur多設一組環境變數、
幫對應的模擬單引擎指定帳戶名稱，不用再改這支檔案的邏輯。

帳戶名稱對應環境變數：account="gold"(預設)對應BINANCE_API_KEY_GOLD/
BINANCE_API_SECRET_GOLD；account="btc"對應BINANCE_API_KEY_BTC/
BINANCE_API_SECRET_BTC，以此類推(帳戶名稱轉大寫接在後面)。為了向後相容
現有已經在用的環境變數命名，"gold"這個預設帳戶額外會退回嘗試沒有帳戶
後綴的舊版BINANCE_API_KEY/BINANCE_API_SECRET(如果新版命名沒設定的話)，
現有的黃金真實下單設定不用改任何環境變數就能繼續運作。

測試網/正式環境切換也支援per-帳戶覆蓋：BINANCE_USE_TESTNET_<帳戶>沒設定時，
退回共用的BINANCE_USE_TESTNET(預設"1"，測試網)。

安全設計：
- 沒有設定對應帳戶的API金鑰時，該帳戶的操作會靜默失敗回傳明確錯誤訊息，
  不影響其他帳戶或其他功能(跟db.py/notifier.py同樣的設計原則)
- 每個帳戶預設都是測試網，要故意設成"0"才會打正式環境，避免不小心接到
  真錢帳戶去
- 所有函式都回傳(success, data_or_error)，不會讓例外往外亂噴
- 不同帳戶的symbol精度快取分開存，避免不同帳戶剛好symbol重名時互相污染
  (雖然目前還沒有這種情境，但多帳戶架構下先做對比較安全)

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

DEFAULT_ACCOUNT = "gold"
DEFAULT_SYMBOL = os.getenv("BINANCE_GOLD_SYMBOL", "xauusdt").upper()

_symbol_precision_cache = {}  # {(account, symbol): precision}


def _get_credentials(account=DEFAULT_ACCOUNT):
    """
    取得指定帳戶的API金鑰/密鑰。優先找具名的BINANCE_API_KEY_<帳戶>，
    帳戶是預設值"gold"且具名版本沒設定時，退回嘗試舊版沒有帳戶後綴的
    BINANCE_API_KEY/BINANCE_API_SECRET，確保現有部署不用改環境變數。
    """
    suffix = account.upper()
    api_key = os.getenv(f"BINANCE_API_KEY_{suffix}")
    api_secret = os.getenv(f"BINANCE_API_SECRET_{suffix}")

    if not api_key and account == DEFAULT_ACCOUNT:
        api_key = os.getenv("BINANCE_API_KEY")
    if not api_secret and account == DEFAULT_ACCOUNT:
        api_secret = os.getenv("BINANCE_API_SECRET")

    return api_key, api_secret


def use_testnet(account=DEFAULT_ACCOUNT):
    """
    是否使用測試網，支援per-帳戶覆蓋：BINANCE_USE_TESTNET_<帳戶>沒設定時，
    退回共用的BINANCE_USE_TESTNET(預設"1"，測試網)。
    """
    suffix = account.upper()
    per_account = os.getenv(f"BINANCE_USE_TESTNET_{suffix}")
    if per_account is not None:
        return per_account != "0"
    return os.getenv("BINANCE_USE_TESTNET", "1") != "0"


def _base_url(account=DEFAULT_ACCOUNT):
    return TESTNET_BASE_URL if use_testnet(account) else MAINNET_BASE_URL


def is_enabled(account=DEFAULT_ACCOUNT):
    api_key, api_secret = _get_credentials(account)
    return bool(api_key and api_secret)


def status(account=DEFAULT_ACCOUNT, symbol=None):
    """給dashboard/API endpoint顯示目前執行模組(指定帳戶)的狀態用。"""
    return {
        "account": account,
        "enabled": is_enabled(account),
        "testnet": use_testnet(account),
        "base_url": _base_url(account),
        "symbol": symbol or (DEFAULT_SYMBOL if account == DEFAULT_ACCOUNT else None),
    }


def _sign(params: dict, account=DEFAULT_ACCOUNT) -> dict:
    """依官方文件的HMAC SHA256簽章方式，把params組成query string、算出簽章，回傳含簽章的dict。"""
    _, api_secret = _get_credentials(account)
    query_string = urlencode(params)
    signature = hmac.new((api_secret or "").encode("utf-8"), query_string.encode("utf-8"), hashlib.sha256).hexdigest()
    params = dict(params)
    params["signature"] = signature
    return params


def _signed_request(method, path, params=None, account=DEFAULT_ACCOUNT):
    """呼叫需要簽章的私有端點(帳戶、下單、部位查詢等)。回傳(success, data_or_error)。"""
    api_key, api_secret = _get_credentials(account)
    if not api_key or not api_secret:
        return False, f"帳戶「{account}」尚未設定API金鑰(BINANCE_API_KEY_{account.upper()}/BINANCE_API_SECRET_{account.upper()})"

    params = dict(params or {})
    params["timestamp"] = int(time.time() * 1000)
    params.setdefault("recvWindow", 5000)
    signed_params = _sign(params, account=account)

    url = f"{_base_url(account)}{path}"
    headers = {"X-MBX-APIKEY": api_key}

    try:
        resp = requests.request(method, url, headers=headers, params=signed_params, timeout=10)
        data = resp.json()
        if resp.status_code >= 400:
            logger.error(f"幣安API錯誤(帳戶{account}, {resp.status_code}): {data}")
            return False, data
        return True, data
    except Exception as e:
        logger.error(f"幣安API請求失敗(帳戶{account}): {e}")
        return False, str(e)


def get_symbol_precision(symbol=None, account=DEFAULT_ACCOUNT):
    """
    查合約的數量精度(quantityPrecision)，下單數量要依這個精度四捨五入，
    不然幣安會直接拒絕訂單。結果依(帳戶, 商品)快取起來，不同帳戶就算剛好
    查同一個symbol也分開存，避免未來多帳戶情境下互相污染。這是公開端點，
    不需要簽章，但測試網/正式環境的base_url仍然依帳戶決定。
    """
    symbol = symbol or (DEFAULT_SYMBOL if account == DEFAULT_ACCOUNT else None)
    if not symbol:
        return 3

    cache_key = (account, symbol)
    if cache_key in _symbol_precision_cache:
        return _symbol_precision_cache[cache_key]

    try:
        resp = requests.get(f"{_base_url(account)}/fapi/v1/exchangeInfo", timeout=10)
        data = resp.json()
        for s in data.get("symbols", []):
            if s["symbol"] == symbol:
                precision = s.get("quantityPrecision", 3)
                _symbol_precision_cache[cache_key] = precision
                return precision
    except Exception as e:
        logger.error(f"查詢合約精度失敗(帳戶{account}, {symbol}): {e}")

    return 3  # 查不到的話用一個保守預設值(0.001精度，符合先前查到的XAUUSDT最小交易單位)


def get_mark_price(symbol, account=DEFAULT_ACCOUNT):
    """
    查詢目前市價(公開端點，不需要簽章/API金鑰)。用symbol的即時mark price，
    給部位試算工具算名目部位價值用。
    """
    try:
        resp = requests.get(f"{_base_url(account)}/fapi/v1/ticker/price", params={"symbol": symbol}, timeout=10)
        data = resp.json()
        if "price" in data:
            return True, float(data["price"])
        return False, data
    except Exception as e:
        logger.error(f"查詢市價失敗({symbol}): {e}")
        return False, str(e)


def estimate_quantity_for_target(target_price_move, target_pnl_usd=1.0, symbol=None,
                                  current_price=None, account=DEFAULT_ACCOUNT,
                                  leverage_options=(5, 10, 20, 25, 50, 75, 100)):
    """
    給定「價格每變動多少(target_price_move)，希望對應賺賠多少美元(target_pnl_usd)」，
    回推需要的下單數量，並列出不同槓桿倍數下對應的名目部位價值和所需保證金。

    重要觀念澄清：**槓桿不影響這個數量或損益敏感度的計算**，數量才是決定
    「每點賺賠多少錢」的唯一變數，槓桿只影響「這筆部位要墊多少保證金才
    開得起」。這裡刻意把兩者分開列出來，避免誤解成調槓桿可以改變損益
    敏感度——這是使用者在對話中討論過的觀念，這個計算器就是用來把數量和
    槓桿兩件事講清楚，不是給「所需槓桿」一個單一答案(因為不存在這種東西，
    任何槓桿倍數搭配正確數量都能達到同樣的損益敏感度，差別只在保證金)。

    例如：黃金(1張合約=1金衡盎司)要「跳動1點(=1美元)對應賺賠1美元」，
    數量算出來是1張；BTC(1張合約=1顆BTC)要「跳動100點(=100美元)對應
    賺賠1美元」，數量算出來是0.01張。

    current_price沒有提供的話會即時查詢(公開端點，不需要API金鑰)。
    回傳(success, data_or_error)，成功時data包含quantity、current_price、
    notional_value、以及margin_by_leverage(每個槓桿倍數對應的保證金)。
    """
    if target_price_move <= 0:
        return False, "價格變動量必須大於0"
    if target_pnl_usd <= 0:
        return False, "目標損益金額必須大於0"

    symbol = symbol or (DEFAULT_SYMBOL if account == DEFAULT_ACCOUNT else None)
    if not symbol:
        return False, f"帳戶「{account}」沒有指定symbol，也沒有預設值可用"

    quantity = target_pnl_usd / target_price_move
    precision = get_symbol_precision(symbol, account=account)
    quantity = round(quantity, precision)

    if current_price is None:
        price_ok, price_result = get_mark_price(symbol, account=account)
        if not price_ok:
            return False, price_result
        current_price = price_result

    notional_value = quantity * current_price
    margin_by_leverage = {
        lev: round(notional_value / lev, 2) for lev in leverage_options
    }

    return True, {
        "symbol": symbol,
        "quantity": quantity,
        "target_price_move": target_price_move,
        "target_pnl_usd": target_pnl_usd,
        "current_price": current_price,
        "notional_value": round(notional_value, 2),
        "margin_by_leverage": margin_by_leverage,
    }


def calculate_quantity(risk_usd, sl_points, symbol=None, account=DEFAULT_ACCOUNT):
    """
    (輔助試算用，不再是實際下單的主要依據，見下方open_position()的修正記錄)
    依「這筆單願意承擔多少美元風險」和「停損距離(points)」，換算出對應的下單數量。
    """
    if sl_points <= 0:
        return 0.0
    quantity = risk_usd / sl_points
    precision = get_symbol_precision(symbol, account=account)
    return round(quantity, precision)


def estimate_risk(quantity, sl_points, account_balance=None, symbol=None, account=DEFAULT_ACCOUNT):
    """
    反過來算：使用者自己決定要下多少口數，這裡回推「如果觸及停損，實際會虧多少
    美元」以及「這筆虧損佔帳戶餘額的百分比」，給使用者參考用，不會自動套用或
    修改任何設定——真正要下單用哪個數量，由使用者自己決定並填進設定裡。

    account_balance沒有提供的話，會即時查詢指定帳戶的可用餘額。
    回傳 (success, data_or_error)，成功時data包含 dollar_risk、risk_pct、account_balance。
    """
    if quantity <= 0 or sl_points <= 0:
        return False, "數量和停損距離都必須大於0"

    if account_balance is None:
        success, balance_data = get_account_balance(account=account)
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


def set_leverage(leverage, symbol=None, account=DEFAULT_ACCOUNT):
    symbol = symbol or (DEFAULT_SYMBOL if account == DEFAULT_ACCOUNT else None)
    return _signed_request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage}, account=account)


def get_account_balance(account=DEFAULT_ACCOUNT):
    """查指定帳戶餘額，主要用來確認API金鑰有沒有接對、測試網/正式環境有沒有搞錯。"""
    return _signed_request("GET", "/fapi/v2/balance", account=account)


def get_position_info(symbol=None, account=DEFAULT_ACCOUNT):
    symbol = symbol or (DEFAULT_SYMBOL if account == DEFAULT_ACCOUNT else None)
    return _signed_request("GET", "/fapi/v2/positionRisk", {"symbol": symbol}, account=account)


def place_market_order(side, quantity, symbol=None, reduce_only=False, account=DEFAULT_ACCOUNT):
    """
    送出市價單(指定帳戶)。side是"BUY"或"SELL"，quantity是張數(已經套用過精度)。
    reduce_only=True代表這是平倉單(只能減少部位、不會反向開新倉)，
    下平倉單時一律加這個保護，避免手誤或邏輯錯誤導致意外開出反向部位。
    """
    symbol = symbol or (DEFAULT_SYMBOL if account == DEFAULT_ACCOUNT else None)
    if not symbol:
        return False, f"帳戶「{account}」沒有指定symbol，也沒有預設值可用"
    if quantity <= 0:
        return False, "下單數量必須大於0"

    precision = get_symbol_precision(symbol, account=account)
    quantity = round(quantity, precision)

    params = {
        "symbol": symbol,
        "side": side,
        "type": "MARKET",
        "quantity": quantity,
    }
    if reduce_only:
        params["reduceOnly"] = "true"
    return _signed_request("POST", "/fapi/v1/order", params, account=account)


def open_position(direction, quantity, symbol=None, account=DEFAULT_ACCOUNT):
    """
    依訊號方向在指定帳戶開倉，quantity是直接指定的下單數量(張數)。
    account預設"gold"，之後新增BTC等其他商品時，讓對應的模擬單引擎傳入
    account="btc"之類的帳戶名稱，各自用獨立的子帳戶下單，不會共用同一個
    帳戶的部位、也就不會有淨部位互相抵銷的問題(修正記錄見README)。

    回傳(success, order_result_or_error)。
    """
    if not is_enabled(account):
        return False, f"帳戶「{account}」的執行模組未啟用(未設定API金鑰)"

    if quantity <= 0:
        return False, "下單數量必須大於0"

    side = "BUY" if direction == "bullish" else "SELL"
    return place_market_order(side, quantity, symbol=symbol, account=account)


def close_position(direction, symbol=None, account=DEFAULT_ACCOUNT):
    """
    平掉指定帳戶目前的部位。direction是原本開倉時的方向(平倉方向要反過來)，
    實際數量直接查詢目前帳戶部位大小，不用呼叫端自己算，避免因為
    浮點數誤差或多筆疊加導致平倉數量對不上實際部位。
    """
    if not is_enabled(account):
        return False, f"帳戶「{account}」的執行模組未啟用(未設定API金鑰)"

    success, position_data = get_position_info(symbol=symbol, account=account)
    if not success:
        return False, position_data

    position_amt = 0.0
    target_symbol = symbol or (DEFAULT_SYMBOL if account == DEFAULT_ACCOUNT else None)
    for p in position_data:
        if p["symbol"] == target_symbol:
            position_amt = float(p["positionAmt"])
            break

    if position_amt == 0:
        return False, "目前沒有未平倉部位可以平"

    side = "SELL" if position_amt > 0 else "BUY"
    quantity = abs(position_amt)
    return place_market_order(side, quantity, symbol=symbol, reduce_only=True, account=account)
