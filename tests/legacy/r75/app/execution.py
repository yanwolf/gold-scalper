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


def physical_account_key(account):
    """
    把帳戶名稱對應到「實體帳戶」(修正記錄見README)。使用者確認gold與gold_1m是同一個
    幣安帳戶的兩把金鑰——風控的「帳戶層級每日虧損上限」要把同一實體帳戶底下所有
    引擎加總，用名稱分組會漏掉。對應關係由環境變數EXECUTION_SAME_PHYSICAL_ACCOUNTS
    設定，格式「群組;群組」、群組內用逗號，例如 "gold,gold_1m;btc,btc_1m"。
    預設 "gold,gold_1m"(符合使用者目前狀況)。不在任何群組的帳戶各自獨立。
    """
    groups = os.getenv("EXECUTION_SAME_PHYSICAL_ACCOUNTS", "gold,gold_1m")
    for group in groups.split(";"):
        members = [m.strip() for m in group.split(",") if m.strip()]
        if account in members:
            return members[0]
    return account


def _resolve_symbol(symbol):
    """
    沒指定symbol時一律退回DEFAULT_SYMBOL，不分帳戶。

    修正記錄(見README)：原本寫成「只有預設帳戶gold才自動補DEFAULT_SYMBOL，其他
    帳戶名稱回傳None」，本意是為未來BTC帳戶預留(BTC帳戶不該默默用到黃金symbol)，
    但實際上變成一個地雷——使用者新增第二個黃金帳戶「gold_1m」後，手動測試面板
    一下單就撞到「沒有指定symbol，也沒有預設值可用」。系統目前只交易黃金，任何
    帳戶沒指定就用黃金才是合理預設；未來加BTC時，那個引擎會明確傳symbol="BTCUSDT"
    覆寫，不受影響。
    """
    return (symbol or DEFAULT_SYMBOL).upper()

_symbol_precision_cache = {}  # 已不再使用，保留避免其他地方有殘留參照
_symbol_filters_cache = {}  # {(account, symbol): precision}


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
        "symbol": _resolve_symbol(symbol),
    }


def _sign(params: dict, account=DEFAULT_ACCOUNT) -> dict:
    """依官方文件的HMAC SHA256簽章方式，把params組成query string、算出簽章，回傳含簽章的dict。"""
    _, api_secret = _get_credentials(account)
    query_string = urlencode(params)
    signature = hmac.new((api_secret or "").encode("utf-8"), query_string.encode("utf-8"), hashlib.sha256).hexdigest()
    params = dict(params)
    params["signature"] = signature
    return params


def _signed_request(method, path, params=None, account=DEFAULT_ACCOUNT, return_status=False):
    """呼叫需要簽章的私有端點(帳戶、下單、部位查詢等)。回傳(success, data_or_error)，
    return_status=True時回傳(success, data_or_error, http_status_code)——只有
    place_algo_stop()判斷「404=端點不存在，該退回舊寫法」時需要用到狀態碼本身，
    其他呼叫端不用管這個參數。"""
    api_key, api_secret = _get_credentials(account)
    if not api_key or not api_secret:
        error = f"帳戶「{account}」尚未設定API金鑰(BINANCE_API_KEY_{account.upper()}/BINANCE_API_SECRET_{account.upper()})"
        return (False, error, None) if return_status else (False, error)

    params = dict(params or {})
    params["timestamp"] = int(time.time() * 1000)
    params.setdefault("recvWindow", 5000)
    signed_params = _sign(params, account=account)

    url = f"{_base_url(account)}{path}"
    headers = {"X-MBX-APIKEY": api_key}

    try:
        resp = requests.request(method, url, headers=headers, params=signed_params, timeout=10)
        try:
            data = resp.json()
        except Exception:
            data = resp.text
        if resp.status_code >= 400:
            logger.error(f"幣安API錯誤(帳戶{account}, {resp.status_code}): {data}")
            return (False, data, resp.status_code) if return_status else (False, data)
        return (True, data, resp.status_code) if return_status else (True, data)
    except Exception as e:
        logger.error(f"幣安API請求失敗(帳戶{account}): {e}")
        return (False, str(e), None) if return_status else (False, str(e))


def get_symbol_precision(symbol=None, account=DEFAULT_ACCOUNT):
    """
    查合約的數量精度(quantityPrecision)，下單數量要依這個精度四捨五入，
    不然幣安會直接拒絕訂單。內部呼叫get_symbol_filters()、只回傳精度那一項，
    保留這個函式是為了不用改動既有呼叫點的介面。
    """
    return get_symbol_filters(symbol, account=account)["quantity_precision"]


def get_symbol_filters(symbol=None, account=DEFAULT_ACCOUNT):
    """
    查合約完整的下單規則：數量精度、stepSize(數量最小增量)、tickSize(價格最小
    增量)、minQty、minNotional(最小名目金額)。結果依(帳戶, 商品)快取。這是公開
    端點，不需要簽章，但測試網/正式環境的base_url仍然依帳戶決定。

    BINANCE_LESSONS.md 第4條：下單數量/價格沒照交易所精度會被 -1111 拒絕；
    quantityPrecision只是小數位數，不等於stepSize本身(例如stepSize=5的整數
    商品，quantityPrecision=0但5顆一批，round()對不上)，這裡改成直接讀
    LOT_SIZE/PRICE_FILTER/MIN_NOTIONAL三個filter的原始值，round_quantity()/
    round_price()用這些值做無條件捨去/取整，而不是單純的小數位四捨五入。
    """
    symbol = _resolve_symbol(symbol)

    cache_key = (account, symbol)
    if cache_key in _symbol_filters_cache:
        return _symbol_filters_cache[cache_key]

    result = {
        "quantity_precision": 3, "step_size": 0.001, "tick_size": 0.01,
        "min_qty": 0.001, "min_notional": 0.0,
    }
    try:
        resp = requests.get(f"{_base_url(account)}/fapi/v1/exchangeInfo", timeout=10)
        data = resp.json()
        for s in data.get("symbols", []):
            if s["symbol"] != symbol:
                continue
            result["quantity_precision"] = s.get("quantityPrecision", 3)
            for f in s.get("filters", []):
                ftype = f.get("filterType")
                if ftype == "LOT_SIZE":
                    result["step_size"] = float(f.get("stepSize", result["step_size"]))
                    result["min_qty"] = float(f.get("minQty", result["min_qty"]))
                elif ftype == "PRICE_FILTER":
                    result["tick_size"] = float(f.get("tickSize", result["tick_size"]))
                elif ftype in ("MIN_NOTIONAL", "NOTIONAL"):
                    result["min_notional"] = float(f.get("notional", f.get("minNotional", result["min_notional"])) or 0)
            break
        _symbol_filters_cache[cache_key] = result
    except Exception as e:
        logger.error(f"查詢合約下單規則失敗(帳戶{account}, {symbol}): {e}")
        # 查不到就回保守預設值(符合先前查到的XAUUSDT規格)，不快取，讓下一次呼叫重試

    return result


def _round_step(value, step):
    """無條件捨去到step的倍數(不是四捨五入)：避免數量湊整後超過原本要下的量/可用保證金。"""
    if step <= 0:
        return value
    import math
    return math.floor(value / step + 1e-9) * step


def round_quantity(quantity, symbol=None, account=DEFAULT_ACCOUNT):
    """
    照交易所LOT_SIZE規則處理下單數量：無條件捨去到stepSize，並檢查是否低於
    minQty。回傳(quantity, error)，error不是None時代表數量太小下不了單，
    呼叫端應該放棄這次下單而不是硬送出去等交易所拒絕(BINANCE_LESSONS.md第4條)。
    """
    filters = get_symbol_filters(symbol, account=account)
    step = filters["step_size"]
    qty = _round_step(quantity, step)
    # 用stepSize的小數位數決定顯示精度，避免浮點數捨去後出現像0.30000000000000004這種尾數
    decimals = filters["quantity_precision"]
    qty = round(qty, decimals)
    if qty < filters["min_qty"] or qty <= 0:
        return qty, f"數量{qty}低於最小下單量{filters['min_qty']}"
    return qty, None


def round_price(price, symbol=None, account=DEFAULT_ACCOUNT):
    """照交易所PRICE_FILTER規則把價格取整到tickSize(用於未來要掛真實停損單時)。"""
    filters = get_symbol_filters(symbol, account=account)
    tick = filters["tick_size"]
    if tick <= 0:
        return price
    import math
    rounded = round(price / tick) * tick
    # tickSize的小數位數用來修掉浮點誤差
    decimals = max(0, len(str(tick).split(".")[-1])) if "." in str(tick) else 0
    return round(rounded, decimals)


def max_leverage(symbol=None, account=DEFAULT_ACCOUNT):
    """
    查這個帳戶對這個商品目前允許的最高槓桿(GET /fapi/v1/leverageBracket)。
    新子帳戶/小市值幣常見上限比預期低(BINANCE_LESSONS.md第5條)，preflight用
    這個提前示警，而不是等到真的下單被-4421拒絕才發現(那個是open flow裡的
    被動重試，這裡是主動查詢)。查不到回傳None，呼叫端自行決定要不要當作異常。
    """
    symbol = _resolve_symbol(symbol)
    success, result = _signed_request("GET", "/fapi/v1/leverageBracket", {"symbol": symbol}, account=account)
    if not success or not isinstance(result, list) or not result:
        return None
    try:
        brackets = result[0].get("brackets", [])
        return max(int(b["initialLeverage"]) for b in brackets) if brackets else None
    except Exception:
        return None


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


def get_book_ticker(symbol, account=DEFAULT_ACCOUNT):
    """
    查詢目前最佳買一/賣一(公開端點，不需要簽章/API金鑰)。用來在下單前
    後拿到「決策當下」的真實盤口，才能事後算出「真正執行滑點」跟
    「當下買賣價差」，不是拿中間價或事後的市價去比(修正記錄見README)。
    回傳(success, {"bid": float, "ask": float})。
    """
    try:
        resp = requests.get(f"{_base_url(account)}/fapi/v1/ticker/bookTicker", params={"symbol": symbol}, timeout=10)
        data = resp.json()
        if "bidPrice" in data and "askPrice" in data:
            return True, {"bid": float(data["bidPrice"]), "ask": float(data["askPrice"])}
        return False, data
    except Exception as e:
        logger.error(f"查詢盤口失敗({symbol}): {e}")
        return False, str(e)


def extract_fill_price(order_result):
    """
    從幣安下單API的回傳結果裡取出實際平均成交價(avgPrice)。市價單成交時
    幣安會回傳avgPrice欄位，如果因為任何原因缺失或格式不對(例如"0.00"
    代表還沒完全成交)，安全回傳None(修正記錄見README)。
    """
    if not isinstance(order_result, dict):
        return None
    try:
        avg_price = float(order_result.get("avgPrice") or 0)
    except (TypeError, ValueError):
        avg_price = 0.0
    if avg_price > 0:
        return avg_price
    # r71(實盤2026-09-25)：RESULT回應FILLED、成交1張，avgPrice跟cumQuote整個不存在。
    # 有cumQuote(成交額)時，成交額÷成交量就是實際均價(U本位1張=1單位標的)，不是估算
    try:
        cum_quote = float(order_result.get("cumQuote") or 0)
        qty = float(order_result.get("executedQty") or 0)
    except (TypeError, ValueError):
        return None
    return round(cum_quote / qty, 8) if cum_quote > 0 and qty > 0 else None


def analyze_execution_quality(direction, bid, ask, actual_fill_price, is_close=False):
    """
    比較「決策當下的真實盤口」跟「實際成交價」，拆解出「真正執行滑點」跟
    「當下買賣價差」兩個獨立數字——市價買單本來就會成交在賣一(ask)、
    市價賣單會成交在買一(bid)，這是不管執行多快都躲不掉的固定成本(價差)，
    跟「執行過程中價格又額外跑掉」的真正滑點是不同的兩件事，混在一起看
    會誤判執行品質(修正記錄見README，使用者用真實成交案例驗證過這個
    區分方式)。

    direction: "bullish"或"bearish"，這筆單原本的方向。
    is_close: False代表這是開倉(多單買入對應ask、空單賣出對應bid)；
              True代表這是平倉(多單出場賣出對應bid、空單出場買回對應ask)，
              方向要對調。
    回傳 {expected_fill_price, spread, slippage_points} 或 bid/ask缺失時回傳None。
    正值slippage_points代表對使用者不利(不管方向)，方便直接判讀。
    """
    if bid is None or ask is None:
        return None

    is_bullish = direction == "bullish"
    if is_close:
        expected_fill_price = bid if is_bullish else ask
        slippage_points = (expected_fill_price - actual_fill_price) if is_bullish else (actual_fill_price - expected_fill_price)
    else:
        expected_fill_price = ask if is_bullish else bid
        slippage_points = (actual_fill_price - expected_fill_price) if is_bullish else (expected_fill_price - actual_fill_price)

    return {
        "expected_fill_price": expected_fill_price,
        "spread": ask - bid,
        "slippage_points": slippage_points,
    }


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

    symbol = _resolve_symbol(symbol)

    quantity = target_pnl_usd / target_price_move
    quantity, _ = round_quantity(quantity, symbol, account=account)  # 這裡只是試算建議值，數量太小也不擋，交給使用者自己判斷

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
    quantity, _ = round_quantity(quantity, symbol, account=account)  # 同樣是試算建議值
    return quantity


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
    symbol = _resolve_symbol(symbol)
    return _signed_request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage}, account=account)


def set_position_mode(hedge, account=DEFAULT_ACCOUNT):
    """
    設定帳戶的持倉模式(修正記錄見README)：hedge=True是雙向持倉(Hedge Mode，LONG跟
    SHORT是兩個獨立部位)，False是單向持倉(只有一個淨部位)。

    使用者確認gold/gold_1m兩把金鑰指向同一個帳戶。單向模式下兩個引擎方向相反時
    「開倉」會互相抵銷(15分K空1、1分K開多1→淨0，15分K的空單等於被平掉)，這是
    交易所模式的本質，程式邏輯繞不過；雙向模式下兩側獨立，搭配「只平自己口數」
    才能真正互不影響。

    這支API跟marginType一樣不是冪等的：已經是目標模式會回-4059「No need to
    change position side」，視為成功。帳戶有未平倉部位或掛單時不能切換(-4068)，
    這種情況回傳失敗、呼叫端會放棄這次下單並在通知裡說明——使用者把部位平掉
    一次之後就會自動切過去。這是帳戶層級設定，跟symbol無關。
    """
    success, result = _signed_request(
        "POST", "/fapi/v1/positionSide/dual", {"dualSidePosition": "true" if hedge else "false"}, account=account
    )
    if not success and isinstance(result, dict) and result.get("code") == -4059:
        return True, {"msg": "已經是目標持倉模式，不需要變更"}
    return success, result


# 最後一次「確認過」的持倉模式(BINANCE_LESSONS.md第7條，r10)。這不是拿來省查詢的快取——
# 每次送單還是即時偵測；只在偵測失敗(逾時、限流)時當退路：有舊值用舊值，從沒偵測
# 成功過就假設單向。只有兩種情況會寫入：偵測成功、或反轉假設後重送成功；重送也失敗
# 就清掉，不留沒驗證過的值給下一張單用。
_last_known_hedge = {}


def _mode_fallback(account):
    return _last_known_hedge.get(account, False)


def get_position_mode(account=DEFAULT_ACCOUNT):
    """查詢帳戶目前是不是雙向持倉。回傳(success, bool或錯誤)。"""
    success, result = _signed_request("GET", "/fapi/v1/positionSide/dual", account=account)
    if success and isinstance(result, dict):
        hedge = bool(result.get("dualSidePosition"))
        _last_known_hedge[account] = hedge
        return True, hedge
    return False, result


def get_open_orders(symbol=None, account=DEFAULT_ACCOUNT):
    """
    查詢帳戶目前所有「未成交掛單」(不是部位)。symbol=None時查整個帳戶——切換
    持倉模式是帳戶層級的檢查，別的symbol殘留掛單一樣會擋住切換。
    """
    params = {}
    if symbol:
        params["symbol"] = _resolve_symbol(symbol)
    return _signed_request("GET", "/fapi/v1/openOrders", params, account=account)


def cancel_all_open_orders(symbol=None, account=DEFAULT_ACCOUNT):
    """
    取消指定symbol(預設是本專案交易的symbol)所有未成交掛單。這支API需要symbol。
    回傳(success, result)。
    """
    symbol = _resolve_symbol(symbol)
    return _signed_request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol}, account=account)


def ensure_position_mode(hedge, account=DEFAULT_ACCOUNT, auto_cancel_orders=True):
    """
    確保帳戶是目標持倉模式，但盡量「不切換」：
      1. 先GET目前模式，已經一致就直接回成功，完全不打切換API。
      2. 不一致才POST切換。切換被-4067(有掛單)擋下時，如果auto_cancel_orders
         =True，會先把帳戶上殘留的掛單全部取消(逐symbol呼叫allOpenOrders)再重試
         一次；殘留掛單通常是之前開倉留下的止損/止盈條件單，本來就該清掉。
      3. 被-4068(有未平倉部位)擋下時無法自動處理，回傳失敗並說明。

    回傳(success, result)。失敗時result是dict，帶有code/msg以及人看得懂的hint。
    這樣就不會因為每次進場都硬切一次模式而被交易所擋下，
    自動觸發的訊號跟dashboard手動測試單都改走這支。
    """
    ok, current = get_position_mode(account=account)
    if ok and current == bool(hedge):
        return True, {"msg": "持倉模式已符合，未切換", "hedge": bool(hedge)}

    success, result = set_position_mode(hedge, account=account)
    if success:
        return True, result

    code = result.get("code") if isinstance(result, dict) else None
    if code == -4067 and auto_cancel_orders:
        oo_ok, orders = get_open_orders(account=account)
        symbols = sorted({o.get("symbol") for o in orders if o.get("symbol")}) if oo_ok and isinstance(orders, list) else []
        cancelled = []
        for sym in symbols:
            c_ok, _ = cancel_all_open_orders(symbol=sym, account=account)
            if c_ok:
                cancelled.append(sym)
        success, result = set_position_mode(hedge, account=account)
        if success:
            if isinstance(result, dict):
                result = dict(result, cancelled_open_orders=cancelled)
            return True, result
        if isinstance(result, dict):
            result = dict(result, hint=f"已嘗試取消掛單({cancelled or '無'})後仍無法切換")
        return False, result

    if isinstance(result, dict):
        if code == -4067:
            result = dict(result, hint="帳戶有未成交掛單，請先取消掛單再切換")
        elif code == -4068:
            result = dict(result, hint="帳戶有未平倉部位，請先平倉再切換")
    return False, result


def resolve_position_mode(hedge_wanted, account=DEFAULT_ACCOUNT):
    """
    決定「這筆單實際要用哪種持倉模式」，永遠不因為切不過去就放棄下單。

    先嘗試ensure_position_mode()切到目標模式；切不過去(例如測試網明明沒掛單
    也回-4067)就退回使用帳戶「目前」的模式下單，並帶回warning讓通知裡說明。
    回傳(effective_hedge: bool, warning: str或None)。
    """
    # auto_cancel_orders=False(BINANCE_LESSONS.md第7條「守衛只能碰自己記錄過id的單」)：
    # 切模式被-4067(有掛單)擋下時，以前會把整個帳戶所有幣的掛單全撤——當初就是這樣
    # 撤掉了crypto-screener的KAS條件單，現在也會撤掉自己其他引擎的backstop。
    # 切不過去本來就會退回帳戶現有模式下單，不需要為了切模式去撤別人的單。
    # (dashboard的「取消全部掛單」是使用者手動按的，不受影響)
    ok, result = ensure_position_mode(hedge_wanted, account=account, auto_cancel_orders=False)
    if ok:
        return bool(hedge_wanted), None
    mode_ok, current = get_position_mode(account=account)
    # 偵測失敗不能假設「設定想要的模式」(r10第7條)：有舊值用舊值，沒有就假設單向；
    # 假設錯了會被-4061拒絕，再依被拒的單反轉重送
    effective = bool(current) if mode_ok else _mode_fallback(account)
    hint = result.get("hint", "") if isinstance(result, dict) else ""
    warning = (
        f"持倉模式無法切換成{'雙向' if hedge_wanted else '單向'}({result}{'，' + hint if hint else ''})，"
        f"改用帳戶目前的{'雙向' if effective else '單向'}模式下單"
    )
    return effective, warning


def current_hedge_mode(account=DEFAULT_ACCOUNT, default=None):
    """
    直接問交易所目前是不是雙向。偵測失敗時(r10第7條)：有舊值用舊值，從沒偵測成功過
    就假設單向。default參數保留只為了相容舊呼叫，不再使用——以前偵測失敗就回傳
    呼叫端給的default(設定值，預設雙向)，單向帳戶平倉時會去找LONG側、找不到部位，
    平倉單根本沒送出去。
    """
    ok, current = get_position_mode(account=account)
    return bool(current) if ok else _mode_fallback(account)


def set_margin_type(margin_type, symbol=None, account=DEFAULT_ACCOUNT):
    """
    設定保證金模式：margin_type是"ISOLATED"(逐倉)或"CROSSED"(全倉)。逐倉是
    每筆部位有自己專屬的保證金，就算被強制平倉也只會虧掉分配給那筆單的
    保證金，不會牽連帳戶其他資金——比較符合這個系統「每筆單的風險都要能
    事先算清楚、互相不牽連」的設計精神(風控斷路器、固定口數等都是同樣的
    思路)，所以open_position()預設會用逐倉模式。

    注意：幣安這支API**不是冪等的**——如果目前已經是你要設定的模式，
    幣安會回傳錯誤(code -4046 "No need to change margin type")，這裡把
    這個特定錯誤視為「本來就設定好了、等同成功」，不會誤判成真的失敗，
    避免每次開倉都被這個誤判擋下(修正記錄見README)。
    """
    symbol = _resolve_symbol(symbol)
    success, result = _signed_request(
        "POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": margin_type}, account=account
    )
    if not success and isinstance(result, dict) and result.get("code") == -4046:
        return True, {"msg": "已經是目標保證金模式，不需要變更"}
    return success, result


def get_account_balance(account=DEFAULT_ACCOUNT):
    """查指定帳戶餘額，主要用來確認API金鑰有沒有接對、測試網/正式環境有沒有搞錯。"""
    return _signed_request("GET", "/fapi/v2/balance", account=account)


def usdt_balance_line(account=DEFAULT_ACCOUNT):
    """
    回傳一行「帳戶餘額」文字給Telegram用(出場通知/啟動對帳)，例如
    「帳戶餘額：999.34 USDT(未實現 +0.28)」。查不到就回None、不影響主流程。
    """
    try:
        ok, result = get_account_balance(account=account)
        if not ok or not isinstance(result, list):
            return None
        for row in result:
            if row.get("asset") == "USDT":
                bal = float(row.get("balance", 0) or 0)
                upnl = float(row.get("crossUnPnl", 0) or 0)
                text = f"帳戶餘額：{bal:.2f} USDT"
                if abs(upnl) >= 0.005:
                    text += f"(未實現 {'+' if upnl >= 0 else ''}{upnl:.2f})"
                return text
    except Exception:
        return None
    return None


def get_position_info(symbol=None, account=DEFAULT_ACCOUNT):
    symbol = _resolve_symbol(symbol)
    return _signed_request("GET", "/fapi/v2/positionRisk", {"symbol": symbol}, account=account)


def get_user_trades(symbol=None, account=DEFAULT_ACCOUNT, limit=100, from_id=None):
    """
    成交明細(GET /fapi/v1/userTrades)：交易所端觸發的停損、App手動平倉的實際成交價從這裡查
    (第8條r30)。回傳(success, list)，每筆有 id / orderId / side / positionSide / qty / price / realizedPnl / time。
    """
    params = {"symbol": _resolve_symbol(symbol), "limit": limit}
    if from_id is not None:
        params["fromId"] = int(from_id)  # 從這個成交id往後(含)查，分頁用(第8條r37)
    return _signed_request("GET", "/fapi/v1/userTrades", params, account=account)


def get_order_status(symbol, order_id, account=DEFAULT_ACCOUNT):
    """查詢指定訂單的目前狀態，用來在avgPrice還沒被填入時重新確認實際成交價。"""
    return _signed_request("GET", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id}, account=account)


# 「結果不明」的送單回應(BINANCE_LESSONS.md第3條，r12)：網路逾時、連線中斷(回傳字串)、
# 幣安的未知錯誤/逾時碼。這些情況單可能其實已經成交，不能當成送單失敗。
# 只有交易所明確拒絕(4xx帶其他錯誤碼)才是確定沒成交。
AMBIGUOUS_CODES = (-1000, -1001, -1006, -1007, "UNCONFIRMED")
FINAL_ORDER_STATES = ("FILLED", "CANCELED", "EXPIRED", "REJECTED", "EXPIRED_IN_MATCH")
ORDER_CONFIRM_POLLS = 5          # 送單後沒確認成交時，最多再查幾次訂單
ORDER_CONFIRM_INTERVAL = 0.3     # 每次間隔(秒)
FILL_PRICE_POLLS = 3             # 已成交但沒有均價時，查訂單幾次(第15條r71；以前只查1次)
FILL_PRICE_INTERVAL = 0.5        # 每次間隔(秒)；還是沒有就交給背景補登，不在送單流程裡等太久


def extract_filled_qty(order_result):
    """訂單回應裡交易所確認的成交量(executedQty)；沒有或格式不對回None。"""
    if not isinstance(order_result, dict):
        return None
    try:
        q = float(order_result.get("executedQty") or 0)
    except (TypeError, ValueError):
        return None
    return q if q > 0 else None


def is_ambiguous_result(result):
    if not isinstance(result, dict):
        return True
    return result.get("code") in AMBIGUOUS_CODES or "code" not in result


MODE_MISMATCH_CODES = (-4061, -1106)


def _is_mode_mismatch(result):
    return isinstance(result, dict) and result.get("code") in MODE_MISMATCH_CODES


def _convert_mode_params(params, side, is_closing, hedge_now):
    """
    帳戶持倉模式跟送單時假設的不一樣(被切換過)時，把參數換成目前模式的寫法
    (BINANCE_LESSONS.md第7條雙向模式)：雙向要帶positionSide、不能帶reduceOnly；
    單向相反。is_closing決定positionSide：開多BUY/平多SELL=LONG，開空SELL/平空BUY=SHORT。
    """
    params = dict(params)
    params.pop("positionSide", None)
    params.pop("reduceOnly", None)
    if hedge_now:
        is_long_side = (side == "BUY") != bool(is_closing)
        params["positionSide"] = "LONG" if is_long_side else "SHORT"
    elif is_closing:
        params["reduceOnly"] = "true"
    return params


def _resend_on_mode_mismatch(method, path, params, side, is_closing, result, account):
    """送單回-4061/-1106時：查交易所實際模式(不用任何快取)、換參數重送一次。"""
    if not _is_mode_mismatch(result):
        return None
    mode_ok, hedge_now = get_position_mode(account=account)
    if not mode_ok:
        # 重新偵測本身失敗(逾時、限流)：被-4061/-1106拒絕已經證明原本的假設是錯的，
        # 直接反轉(原本帶positionSide=以為雙向→改成單向，反之亦然)，不要放棄重送
        # (BINANCE_LESSONS.md第7條，r5)
        hedge_now = "positionSide" not in params
        logger.warning(f"持倉模式重新偵測失敗({hedge_now})，改用反轉後的假設重送")
    logger.warning(f"持倉模式不符({result.get('code')})，改用{'雙向' if hedge_now else '單向'}參數重送一次")
    resend = _signed_request(method, path, _convert_mode_params(params, side, is_closing, hedge_now), account=account)
    # r10：重送成功才記住那個假設；重送也失敗就清掉。反轉的依據是被拒的單本身，不是記住的值(r8)
    if resend[0]:
        _last_known_hedge[account] = hedge_now
    else:
        _last_known_hedge.pop(account, None)
    return resend


def place_market_order(side, quantity, symbol=None, reduce_only=False, account=DEFAULT_ACCOUNT, position_side=None):
    """
    送出市價單(指定帳戶)。side是"BUY"或"SELL"，quantity是張數(已經套用過精度)。
    reduce_only=True代表這是平倉單(只能減少部位、不會反向開新倉)，
    單向模式下平倉單一律加這個保護，避免手誤或邏輯錯誤導致意外開出反向部位。

    position_side="LONG"/"SHORT"代表雙向持倉模式(修正記錄見README)：訂單要指定
    要動哪一側的部位；雙向模式下幣安不接受reduceOnly參數(改由side+positionSide
    的組合決定是開倉還是平倉：BUY+LONG開多、SELL+LONG平多、SELL+SHORT開空、
    BUY+SHORT平空)，所以有position_side時不會送reduceOnly。
    """
    symbol = _resolve_symbol(symbol)
    if quantity <= 0:
        return False, "下單數量必須大於0"

    # 照交易所LOT_SIZE規則無條件捨去到stepSize，而不是單純四捨五入到小數位數
    # (BINANCE_LESSONS.md第4條：quantityPrecision不等於stepSize本身，某些商品
    # 兩者算出來的結果會不一樣)。開倉數量太小時直接擋下不送單；平倉單即使算出來
    # 低於minQty也照樣送出去(用原始quantity)，因為這代表要平掉的是先前已經成功
    # 開倉的部位，寧可讓交易所自己判斷，也不要在這裡卡住導致部位平不掉、變孤兒倉。
    rounded_quantity, qty_error = round_quantity(quantity, symbol, account=account)
    if qty_error and not reduce_only:
        return False, qty_error
    if qty_error:
        logger.warning(f"平倉數量精度提醒(帳戶{account}, {symbol}): {qty_error}，仍照原數量送出")
    else:
        quantity = rounded_quantity

    params = {
        "symbol": symbol,
        "side": side,
        "type": "MARKET",
        "quantity": quantity,
        # 幣安期貨下單預設回應是ACK：只回「收到了」(status NEW、executedQty 0、沒有avgPrice)。
        # 實盤2026-09-22 00:00開倉就是這樣——程式當成已成交、但沒有成交價。要求RESULT，回應直接帶成交結果
        "newOrderRespType": "RESULT",
    }
    if position_side:
        params["positionSide"] = position_side
    elif reduce_only:
        params["reduceOnly"] = "true"
    success, result = _signed_request("POST", "/fapi/v1/order", params, account=account)
    if not success:
        # 是平倉單：單向模式帶reduceOnly，雙向模式是SELL+LONG或BUY+SHORT
        is_closing = reduce_only or (position_side and (side == "SELL") == (position_side == "LONG"))
        retry = _resend_on_mode_mismatch("POST", "/fapi/v1/order", params, side, is_closing, result, account)
        if retry is not None:
            success, result = retry

    # 市價單理論上會立刻成交，但幣安(尤其測試網)偶爾撮合結果回填進這次API回應
    # 的時間點會比訂單狀態實際確認慢半拍，導致這次回應裡的avgPrice還是"0"
    # (代表當下狀態可能還是"NEW"，不是"FILLED")——如果不處理，執行品質分析
    # 會誤判成「幣安沒有回傳成交價」而完全無法計算。這裡在偵測到avgPrice缺失
    # 時，短暫等待後重新查詢訂單狀態，拿確認後的實際成交價(r71起查FILL_PRICE_POLLS次，以前只查1次)，
    # 次數有上限，避免真的有問題時卡住整個下單流程太久；再查不到交給背景補登(修正記錄見README)。
    # 沒確認成交(ACK或撮合回填慢)：多查幾次訂單。還是沒確認就不能回報成功——平倉時「成功」代表
    # 會直接撤交易所停損、結帳。改回「結果不明」，交給呼叫端查部位確認(開倉：看得到就認領；平倉：確認沒了才結帳)
    # 不是最終狀態(NEW、部分成交)：多查幾次；還不是就撤掉那張單再查一次拿最終成交量(第15條r43/r44：
    # 卡在NEW放著不管，之後才成交的數量沒人知道，平倉單還會跟下一輪重試的平倉單重疊)
    if success and isinstance(result, dict) and result.get("orderId") and result.get("status") not in FINAL_ORDER_STATES:
        oid = result["orderId"]
        for _ in range(ORDER_CONFIRM_POLLS):
            time.sleep(ORDER_CONFIRM_INTERVAL)
            ok_s, polled = get_order_status(symbol, oid, account=account)
            if ok_s and isinstance(polled, dict):
                result = polled
                if polled.get("status") in FINAL_ORDER_STATES:
                    break
        if result.get("status") not in FINAL_ORDER_STATES:
            logger.error(f"訂單{oid}查{ORDER_CONFIRM_POLLS}次仍是{result.get('status')}，撤單後再查一次")
            _signed_request("DELETE", "/fapi/v1/order", {"symbol": symbol, "orderId": oid}, account=account)
            ok_s, polled = get_order_status(symbol, oid, account=account)
            if ok_s and isinstance(polled, dict) and polled.get("status") in FINAL_ORDER_STATES:
                result = polled
            else:
                return False, {"code": "UNCONFIRMED", "msg": f"送出後查不到最終狀態、撤單後也查不到，結果不明",
                               "orderId": oid, "last": result}
    # 最終狀態而成交0(EXPIRED、CANCELED、REJECTED)：交易所明確說沒成交，不是結果不明(第15條r43)
    if success and isinstance(result, dict) and result.get("status") in FINAL_ORDER_STATES and extract_filled_qty(result) is None:
        return False, {"code": "NOT_FILLED", "msg": f"訂單最終狀態{result.get('status')}、成交0", "orderId": result.get("orderId")}

    # 已成交但回應沒有均價(第15條r71：FILLED卻沒有avgPrice/cumQuote)：查訂單幾次，不是只查一次。
    # 查到就換成查到的那份；都沒有就照原回應回傳(成交是確定的)，由呼叫端查成交明細、再不行排背景補登
    if success and extract_fill_price(result) is None and isinstance(result, dict) and result.get("orderId"):
        oid = result["orderId"]
        logger.warning(f"訂單{oid}已回應(狀態:{result.get('status')})但沒有成交均價，原始回應：{result}")
        for i in range(FILL_PRICE_POLLS):
            time.sleep(FILL_PRICE_INTERVAL)
            ok_p, polled = get_order_status(symbol, oid, account=account)
            if ok_p and extract_fill_price(polled) is not None:
                logger.info(f"訂單{oid}第{i + 1}次查詢拿到成交均價{extract_fill_price(polled)}")
                result = polled
                break
        else:
            logger.warning(f"訂單{oid}查{FILL_PRICE_POLLS}次仍沒有成交均價，交給呼叫端查成交明細／背景補登")

    return success, result


def place_algo_stop(direction, quantity, stop_price, symbol=None, account=DEFAULT_ACCOUNT, position_side=None):
    """
    在交易所掛一張真實的STOP_MARKET條件單，當作程式內停損邏輯的最後防線
    (修正記錄見README：本專案的停損/移動停損平常都是程式內判斷現價跟sl_price
    比對，Zeabur服務掛掉或斷線時完全沒有保護。這張單只是backstop，不會跟著
    移動停損每一格都更新，但至少讓服務中斷時部位不會裸奔到天荒地老)。

    direction是這筆倉位的方向("bullish"/"bearish")，停損單方向要反過來
    (多單的保護是SELL，空單的保護是BUY)。stop_price是觸發價，已經照
    round_price()處理過tickSize由呼叫端負責。

    BINANCE_LESSONS.md第1條：條件單已搬到Algo服務(POST /fapi/v1/algoOrder，
    algoType=CONDITIONAL，stopPrice改叫triggerPrice)，先打新端點，只有
    404(端點不存在)才退回舊的/fapi/v1/order。第8條：用reduceOnly+quantity
    (不是closePosition)，之後要移動這張單時才能「先掛新、再撤舊」，不會有
    裸倉窗口。

    回傳(success, algo_id_or_error, used_legacy)。used_legacy=True代表這個
    帳戶/環境還在用舊端點，之後cancel_algo_stop()要照這個決定打哪支API。
    """
    if quantity <= 0:
        return False, "下單數量必須大於0", False
    symbol = _resolve_symbol(symbol)
    side = "SELL" if direction == "bullish" else "BUY"

    params = {
        "symbol": symbol, "side": side, "algoType": "CONDITIONAL", "type": "STOP_MARKET",
        "quantity": quantity, "triggerPrice": stop_price, "workingType": "MARK_PRICE",
    }
    if position_side:
        params["positionSide"] = position_side
    else:
        params["reduceOnly"] = "true"

    success, data, status = _signed_request("POST", "/fapi/v1/algoOrder", params, account=account, return_status=True)
    if not success and _is_mode_mismatch(data):
        # 停損單一定是平倉方向
        retry = _resend_on_mode_mismatch("POST", "/fapi/v1/algoOrder", params, side, True, data, account)
        if retry is not None:
            success, data = retry
            status = 200 if success else status

    if not success and status == 404:
        # 新端點不存在，這個環境還在用舊寫法：STOP_MARKET掛回/fapi/v1/order，
        # 參數名稱換回stopPrice(第1條)。參數錯誤(-1111等)不會是404，不會誤判到這裡。
        legacy_params = {
            "symbol": symbol, "side": side, "type": "STOP_MARKET",
            "quantity": quantity, "stopPrice": stop_price, "workingType": "MARK_PRICE",
        }
        if position_side:
            legacy_params["positionSide"] = position_side
        else:
            legacy_params["reduceOnly"] = "true"
        success, result = _signed_request("POST", "/fapi/v1/order", legacy_params, account=account)
        if not success:
            return False, result, True
        return True, result.get("orderId"), True

    if not success:
        return False, data, False
    algo_id = data.get("algoId") or data.get("clientAlgoId") if isinstance(data, dict) else None
    if not algo_id:
        return False, f"Algo下單回應沒有algoId：{data}", False
    return True, algo_id, False


def cancel_algo_stop(algo_id, symbol=None, account=DEFAULT_ACCOUNT, used_legacy=False):
    """
    撤掉place_algo_stop()掛的backstop停損單——不管是程式自己出場、force_close、
    或手動平倉，只要部位要關掉，這張單都要一併撤掉(修正記錄見README)：
    否則部位平掉後這張reduceOnly單還留著，等於一個沒有對應部位的孤兒掛單，
    下次系統打算重新開反向倉位時可能會被交易所拒絕或造成混淆。

    如果這張單「已經不存在」(查無此單/已經被觸發成交)，視為成功處理掉，
    但用回傳的第三個值告訴呼叫端「這不是我們主動撤的，可能已經觸發」，
    呼叫端要用這個線索去判斷部位是不是已經被交易所自己平掉了。

    回傳(success, result, already_gone)。
    """
    if not algo_id:
        return True, None, False
    symbol = _resolve_symbol(symbol)
    if used_legacy:
        success, result = _signed_request("DELETE", "/fapi/v1/order", {"symbol": symbol, "orderId": algo_id}, account=account)
    else:
        success, result = _signed_request("DELETE", "/fapi/v1/algoOrder", {"algoId": algo_id}, account=account)

    if success:
        return True, result, False

    # 「查無此單」代表已經不在掛單清單裡——可能已經觸發成交，也可能早被撤過，
    # 兩種情況都不該再重試或回報成失敗
    msg = str(result.get("msg", result) if isinstance(result, dict) else result)
    if "does not exist" in msg or "Unknown order" in msg or "-2011" in msg or "-2013" in msg:
        return True, result, True

    logger.error(f"撤銷backstop停損單失敗(帳戶{account}, algoId={algo_id}): {result}")
    return False, result, False


def get_algo_stop_status(algo_id, symbol=None, account=DEFAULT_ACCOUNT, used_legacy=False):
    """
    查backstop停損單目前狀態，主要用途是cancel_algo_stop()發現單子已經不在時，
    回頭確認它是不是真的被觸發成交了(而不是被別的地方手動撤掉)，好讓平倉通知
    可以準確說明「這筆是交易所backstop自動觸發平倉，不是程式判斷出場」。
    回傳(success, status_dict_or_error)。
    """
    if not algo_id:
        return False, "沒有algo_id"
    symbol = _resolve_symbol(symbol)
    if used_legacy:
        return _signed_request("GET", "/fapi/v1/order", {"symbol": symbol, "orderId": algo_id}, account=account)
    return _signed_request("GET", "/fapi/v1/algoOrder", {"algoId": algo_id}, account=account)


def get_open_algo_orders(symbol=None, account=DEFAULT_ACCOUNT):
    """
    查目前掛著的Algo條件單(GET /fapi/v1/openAlgoOrders)。symbol=None時查整個帳戶，
    權重高(BINANCE_LESSONS.md第6條)，只在自檢時用。回傳(success, list_or_error)。
    """
    params = {"symbol": _resolve_symbol(symbol)} if symbol else {}
    success, data = _signed_request("GET", "/fapi/v1/openAlgoOrders", params, account=account)
    if success and isinstance(data, dict):
        data = data.get("orders", data.get("rows", []))
    return success, data


def find_open_stop(algo_id, used_legacy=False, symbol=None, account=DEFAULT_ACCOUNT):
    """
    停損守衛用：這張停損單現在還掛在交易所上嗎？回傳(ok, present)。
    ok=False代表查不到(失敗)，呼叫端不能當成「不見了」(第2條)。
    第1條r15/r16：Algo查詢回404時不能每輪都「查詢失敗、跳過」——那樣舊端點的停損
    不見了也永遠沒人補。404要跟掛單一樣進入退回：改查舊端點的掛單，以它的結果判斷。
    這是每次查詢當下決定的，不存任何旗標，所以服務重啟後也一樣。
    """
    sym = _resolve_symbol(symbol)
    if not used_legacy:
        ok, data, status = _signed_request("GET", "/fapi/v1/openAlgoOrders", {"symbol": sym}, account=account, return_status=True)
        if ok:
            rows = data.get("orders", data.get("rows", [])) if isinstance(data, dict) else (data or [])
            return True, any(str(o.get("algoId")) == str(algo_id) for o in rows)
        if status != 404:
            return False, None
    ok, data = _signed_request("GET", "/fapi/v1/openOrders", {"symbol": sym}, account=account)
    if not ok or not isinstance(data, list):
        return False, None
    return True, any(str(o.get("orderId")) == str(algo_id) or str(o.get("algoId")) == str(algo_id) for o in data)


def open_position(direction, quantity, symbol=None, account=DEFAULT_ACCOUNT, hedge=False):
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
    position_side = ("LONG" if direction == "bullish" else "SHORT") if hedge else None
    return place_market_order(side, quantity, symbol=symbol, account=account, position_side=position_side)


def close_position(direction, symbol=None, account=DEFAULT_ACCOUNT, quantity=None, hedge=False, baseline=0.0):
    """
    平掉指定帳戶「屬於這筆單」的部位(修正記錄見README)。

    修正前的做法是：查帳戶淨部位、有多少平多少、方向由淨部位正負決定——完全
    不看呼叫端原本的方向跟口數。使用者實際遇到：1分K開多單被風控擋下(只有
    帳面部位)，帳面出場時照樣呼叫這裡，帳戶上只有15分K的空單(淨-1)，於是
    送出BUY把15分K的空單平掉了。

    現在的規則：
    1. 雙向模式(hedge=True)：只看這筆單方向那一側(LONG/SHORT)的部位，另一側
       完全不碰；平倉單帶positionSide、不帶reduceOnly。
    2. 單向模式：帳戶淨部位的方向必須跟這筆單一致，否則拒絕(不平掉別人的
       反向部位)。
    3. 兩種模式都只平『這筆單的口數』(quantity)；不給時退回平整側/整個淨部位
       (只給手動測試用)。
    """
    if not is_enabled(account):
        return False, f"帳戶「{account}」的執行模組未啟用(未設定API金鑰)"

    success, position_data = get_position_info(symbol=symbol, account=account)
    if not success:
        return False, position_data

    target_symbol = _resolve_symbol(symbol)
    is_long = direction == "bullish"
    # positionRisk的列本身就說明了帳戶模式：單向只有BOTH列，雙向是LONG/SHORT兩列。
    # 以它為準，不管呼叫端傳進來的hedge是不是猜錯的(r10第7條：偵測失敗時平倉單不能沒送出去)
    sides = {p.get("positionSide", "BOTH") for p in position_data or [] if p.get("symbol") == target_symbol}
    if "BOTH" in sides:
        hedge = False
    elif sides & {"LONG", "SHORT"}:
        hedge = True

    if hedge:
        want_side = "LONG" if is_long else "SHORT"
        position_amt = 0.0
        for p in position_data:
            if p["symbol"] == target_symbol and p.get("positionSide") == want_side:
                position_amt = float(p["positionAmt"])
                break
        # 扣掉送單前就有的部位(基準，第3條r14/r16)：剩下的才是自己的；沒有就不送(第7條r15)
        own = abs(position_amt) - float(baseline or 0)
        if own <= 1e-9:
            return False, f"雙向模式下{want_side}側扣掉基準({baseline})後沒有自己的部位可以平"
        side = "SELL" if is_long else "BUY"
        close_qty = round(own if quantity is None else min(own, float(quantity)), 6)
        return place_market_order(side, close_qty, symbol=symbol, account=account, position_side=want_side)

    position_amt = 0.0
    for p in position_data:
        if p["symbol"] == target_symbol and p.get("positionSide", "BOTH") == "BOTH":
            position_amt = float(p["positionAmt"])
            break

    if position_amt == 0:
        return False, "目前沒有未平倉部位可以平"

    if (position_amt > 0) != is_long:
        return False, (
            f"帳戶淨部位方向({'多' if position_amt > 0 else '空'} {abs(position_amt)})跟這筆單的方向"
            f"({'多' if is_long else '空'})不一致，拒絕平倉以免平掉別的引擎的部位"
        )

    own = abs(position_amt) - float(baseline or 0)
    if own <= 1e-9:
        return False, f"扣掉基準({baseline})後沒有自己的部位可以平，不送單以免平到別人的部位"
    side = "SELL" if position_amt > 0 else "BUY"
    close_qty = round(own if quantity is None else min(own, float(quantity)), 6)
    return place_market_order(side, close_qty, symbol=symbol, reduce_only=True, account=account)
