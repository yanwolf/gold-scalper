"""
黃金極短線分析工具 - 後端主程式 (FastAPI)

架構參考自 crypto-screener（均線三刀流）：Python/FastAPI 後端 + 前端 PWA 分離。
本檔案僅負責：
1. 啟動時開始背景執行緒，持續從 OANDA 拉 XAU_USD 即時報價
2. 提供 REST endpoint 給前端輪詢最新價格 / 健康檢查
3. 提供 WebSocket endpoint 給前端做即時推播（比輪詢更適合極短線）

後續要加的分析模組（分價量表、纏論中樞/背馳等）建議獨立成
app/analysis.py，在這裡 import 進來、加新的 endpoint 即可，
不用動到 streaming 這一層。
"""

import asyncio
import logging
import os
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse

from app.oanda_client import streamer
from app.binance_client import binance_streamer
from app.analysis import build_candles, compute_volume_profile, poc_and_value_area, analyze_chan
from app.signal_engine import compute_full_signal
from app.notifier import notifier
from app.paper_trading import PAPER_TRADING_ENGINES
from app.health_monitor import health_monitor
from app import backtest as backtest_module
from app import sweep as sweep_module
from app import settings as settings_module
from app import execution as execution_module
from app import db

logger = logging.getLogger("main")

app = FastAPI(title="Gold Scalping Analyzer", version="0.1.0")

# 開發階段先全開，正式上線建議改成前端網域白名單
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup_event():
    db.init_schema()  # 要在 binance_streamer.start() 之前，回填歷史資料時才讀得到
    streamer.start()
    binance_streamer.start()
    notifier.start()
    for engine in PAPER_TRADING_ENGINES.values():
        engine.start()  # 1分K跟5分K兩個引擎平行啟動，各自獨立追蹤
    health_monitor.start()  # 放最後，確保要監控的元件都已經start()過了


@app.on_event("shutdown")
async def shutdown_event():
    streamer.stop()
    binance_streamer.stop()
    notifier.stop()
    for engine in PAPER_TRADING_ENGINES.values():
        engine.stop()
    health_monitor.stop()


@app.get("/dashboard")
async def dashboard():
    """
    分價量表視覺化頁面。直接跟API同源(same-origin)提供，避免瀏覽器/App沙盒
    環境擋掉跨網域fetch的問題(手機瀏覽器對第三方頁面打外部API常常會被擋)。
    """
    return FileResponse(os.path.join(os.path.dirname(__file__), "static", "dashboard.html"))


@app.get("/")
async def root():
    return RedirectResponse(url="/dashboard")


@app.get("/health")
async def health():
    """
    Zeabur 或任何平台的健康檢查都可以打這個 endpoint。
    同時回報資料源的連線狀態，方便快速判斷是哪一邊出問題。
    """
    return {
        "service": "ok",
        "database_persistence_enabled": db.is_enabled(),
        "telegram_notifier_enabled": notifier.is_enabled,
        "oanda_stream": streamer.status,
        "binance_stream": binance_streamer.status,
        "active_health_alerts": health_monitor.get_status()["active_alerts"],
    }


@app.get("/health/monitor")
async def health_monitor_status():
    """
    背景執行緒健康監控的詳細狀態：最後檢查時間、目前有哪些告警在生效中、
    以及每一項檢查各自的狀態。有設定Telegram的話，問題發生/恢復時會主動推播，
    這支endpoint是給想直接查看目前狀態(不用等告警)的用途，dashboard也會顯示。
    """
    return health_monitor.get_status()


# ---------------------------------------------------------------------------
# Telegram 通知設定 endpoint：給dashboard的通知設定面板用
# ---------------------------------------------------------------------------

@app.get("/notify/status")
async def notify_status():
    return notifier.status


@app.post("/notify/test")
async def notify_test():
    """從dashboard按「傳送測試通知」時打這支，回傳是否成功、失敗原因是什麼。"""
    success, error = notifier.send_test_message()
    return {"success": success, "error": error}


@app.post("/notify/toggle")
async def notify_toggle(muted: bool):
    """暫停/恢復通知。這是記憶體狀態，服務重啟會重置回「未暫停」，不是永久設定。"""
    notifier.set_muted(muted)
    return notifier.status


@app.get("/notify/detect-chat-id")
async def notify_detect_chat_id():
    """
    列出最近有跟這個bot說過話的對話，方便使用者在dashboard上直接找到自己的chat_id，
    不用手動組Telegram API網址去看JSON。只需要TELEGRAM_BOT_TOKEN就能用。
    """
    return notifier.detect_recent_chats()


# ---------------------------------------------------------------------------
# 執行期可調整設定：模擬單風控參數 + 達標門檻，透過dashboard線上調整，
# 不用進Zeabur後台改環境變數、重新部署。修改需要密碼保護(SETTINGS_PASSWORD)。
# ---------------------------------------------------------------------------

@app.get("/settings")
async def get_settings():
    """
    回傳目前生效的設定值 + 每個欄位的說明定義(標籤/說明文字/型別/範圍)，
    dashboard的設定面板直接讀這個來動態產生表單。這支不需要密碼，
    純讀取不會改動任何東西。
    """
    return settings_module.get_settings_with_meta()


@app.post("/settings")
async def update_settings(payload: dict = Body(...)):
    """
    更新設定。payload格式: {"password": "...", "values": {"paper_sl_points": 8.0, ...}}。
    密碼要跟SETTINGS_PASSWORD環境變數一致才能通過，密碼本身沒設定的話一律拒絕
    (代表使用者還沒去Zeabur做過這唯一一次的初始設定)。
    """
    password = payload.get("password", "")
    ok, error = settings_module.verify_password(password)
    if not ok:
        return {"success": False, "error": error}

    values = payload.get("values", {})
    updated = settings_module.update_settings(values)
    return {"success": True, "values": updated}


@app.get("/settings/engine/{engine_id}")
async def get_engine_settings(engine_id: str):
    """
    引擎專屬參數(修正記錄見README)：回傳這個引擎目前的覆寫值、實際生效的
    完整設定、以及可覆寫欄位的meta。四個模擬單引擎原本共用同一組策略參數，
    但1分K跟15分K的ATR量級差很多(同樣1x倍數在1分K換算出來的停損只有1~2點，
    出場太快、利潤被滑點吃光)，所以讓每個引擎可以各自覆寫TRADING_RELEVANT_KEYS
    裡的欄位，沒覆寫的沿用全域。不需要密碼，純讀取。
    """
    if engine_id not in PAPER_TRADING_ENGINES:
        return {"error": f"沒有engine_id={engine_id}的追蹤引擎，可用的有: {list(PAPER_TRADING_ENGINES.keys())}"}
    return {
        "engine_id": engine_id,
        "overrides": settings_module.get_engine_overrides(engine_id),
        "effective": settings_module.get_settings(engine_id=engine_id),
        "global": settings_module.get_settings(),
        "meta": {k: settings_module.FIELD_META[k] for k in settings_module.TRADING_RELEVANT_KEYS},
        "last_changed_at": settings_module.get_last_changed_at(engine_id=engine_id),
    }


@app.post("/settings/engine/{engine_id}")
async def update_engine_settings(engine_id: str, payload: dict = Body(...)):
    """
    更新引擎專屬覆寫。payload: {"password": "...", "values": {欄位: 值 或 null}}，
    值是null/空字串代表清除該欄位覆寫、回頭沿用全域。只在值真的改變時才算
    異動(才會更新這個引擎的績效統計分界)，整份表單重送不會誤觸。
    """
    ok, error = settings_module.verify_password(payload.get("password", ""))
    if not ok:
        return {"success": False, "error": error}
    if engine_id not in PAPER_TRADING_ENGINES:
        return {"success": False, "error": f"沒有engine_id={engine_id}的追蹤引擎"}
    applied, cleared = settings_module.update_engine_overrides(engine_id, payload.get("values", {}))
    return {
        "success": True, "applied": applied, "cleared": cleared,
        "overrides": settings_module.get_engine_overrides(engine_id),
        "effective": settings_module.get_settings(engine_id=engine_id),
    }


@app.post("/settings/engine/{engine_id}/clear")
async def clear_engine_settings(engine_id: str, payload: dict = Body(...)):
    """清除這個引擎的全部專屬覆寫，回頭完全沿用全域設定。"""
    ok, error = settings_module.verify_password(payload.get("password", ""))
    if not ok:
        return {"success": False, "error": error}
    if engine_id not in PAPER_TRADING_ENGINES:
        return {"success": False, "error": f"沒有engine_id={engine_id}的追蹤引擎"}
    cleared = settings_module.clear_engine_overrides(engine_id)
    return {"success": True, "cleared": cleared, "effective": settings_module.get_settings(engine_id=engine_id)}


# ---------------------------------------------------------------------------
# 幣安期貨下單執行(測試網優先)：手動測試用endpoint，還沒自動接上模擬單引擎的
# 開倉/平倉事件——因為現在1分K/5分K/15分K三個引擎平行運作，如果都自動對同一個
# 帳戶下單會互相打架，這部分需要另外設計，見README。這裡先讓使用者能手動驗證
# 認證/簽章/精度換算/下單流程本身能不能正常運作。修改設定用同一組密碼保護，
# 避免任何拿到網址的人亂觸發下單。
# ---------------------------------------------------------------------------

@app.get("/execution/status")
async def execution_status(account: str = "gold"):
    """回傳指定帳戶(預設gold)目前狀態：有沒有啟用、現在打的是測試網還正式環境。不需要密碼，純讀取。"""
    return execution_module.status(account=account)


@app.get("/execution/account")
async def execution_account(password: str = "", account: str = "gold"):
    """
    查詢指定帳戶(預設gold)的幣安期貨帳戶餘額，用來確認API金鑰有沒有接對、
    測試網/正式環境有沒有搞錯。account參數是為了未來多帳戶(例如BTC用獨立
    子帳戶)預留的，現在只有gold帳戶已經設定金鑰，其他帳戶名稱查詢會得到
    「尚未設定API金鑰」的錯誤，這是預期中的行為(修正記錄見README)。
    需要密碼(跟策略參數設定共用同一組SETTINGS_PASSWORD)，避免任何拿到網址的人
    都能查看帳戶資訊。
    """
    ok, error = settings_module.verify_password(password)
    if not ok:
        return {"success": False, "error": error}

    success, data = execution_module.get_account_balance(account=account)
    return {"success": success, "data": data}


@app.get("/execution/position")
async def execution_position(password: str = "", account: str = "gold", symbol: Optional[str] = None):
    """
    查詢指定帳戶目前的實際持倉，包含幣安直接算好的**強平價格**(liquidationPrice)——
    這是幣安依照該商品實際的維持保證金分級表算出來的精確數字，比自己土法煉鋼
    估算可靠，維持保證金比率因商品、部位大小分級而異，不用自己猜。也會一併
    回傳未實現損益、進場均價、槓桿倍數等，方便隨時掌握真實部位現況，不用等
    到快被強平才知道。需要密碼，跟其他執行相關endpoint一致。
    """
    ok, error = settings_module.verify_password(password)
    if not ok:
        return {"success": False, "error": error}

    success, data = execution_module.get_position_info(symbol=symbol, account=account)
    return {"success": success, "data": data}


@app.get("/execution/estimate-risk")
async def execution_estimate_risk(quantity: float, sl_points: float, password: str = "", account: str = "gold"):
    """
    部位風險試算：給定候選下單數量和停損距離，回推「如果觸及停損，實際會虧多少
    美元、佔目前帳戶餘額多少百分比」，純粹給使用者參考、幫助決定要在
    execution_quantity設定裡填多少，不會實際下單也不會修改任何設定。
    需要密碼，跟其他執行相關endpoint一致。
    """
    ok, error = settings_module.verify_password(password)
    if not ok:
        return {"success": False, "error": error}

    success, data = execution_module.estimate_risk(quantity, sl_points, account=account)
    return {"success": success, "data": data if success else None, "error": None if success else data}


@app.get("/execution/estimate-quantity")
async def execution_estimate_quantity(
    target_price_move: float, target_pnl_usd: float = 1.0,
    symbol: str = "XAUUSDT", password: str = "", account: str = "gold",
):
    """
    部位數量試算(反過來算)：給定「價格每變動多少，希望對應賺賠多少美元」，
    回推需要的下單數量，並列出不同槓桿倍數下對應的名目部位價值和所需保證金。

    例如黃金想要「跳動1點=賺賠1美元」，帶target_price_move=1、target_pnl_usd=1；
    BTC想要「跳動100點=賺賠1美元」，帶target_price_move=100、target_pnl_usd=1、
    symbol=BTCUSDT。注意：槓桿不影響算出來的數量或損益敏感度，只影響保證金，
    這個endpoint刻意把兩者分開列出來，不會給「所需槓桿」這種不存在的單一答案。
    需要密碼，跟其他執行相關endpoint一致。
    """
    ok, error = settings_module.verify_password(password)
    if not ok:
        return {"success": False, "error": error}

    success, data = execution_module.estimate_quantity_for_target(
        target_price_move, target_pnl_usd, symbol=symbol, account=account,
    )
    return {"success": success, "data": data if success else None, "error": None if success else data}


@app.post("/execution/set-leverage")
async def execution_set_leverage(payload: dict = Body(...)):
    """手動設定槓桿倍數：payload格式 {"password": "...", "leverage": 10, "account": "gold"}。"""
    ok, error = settings_module.verify_password(payload.get("password", ""))
    if not ok:
        return {"success": False, "error": error}

    leverage = payload.get("leverage")
    if not leverage or leverage <= 0:
        return {"success": False, "error": "leverage必須是正整數"}

    account = payload.get("account", "gold")
    success, result = execution_module.set_leverage(int(leverage), account=account)
    return {"success": success, "result": result}


@app.post("/execution/test-order")
async def execution_test_order(payload: dict = Body(...)):
    """
    手動測試下單：payload格式 {"password": "...", "direction": "bullish"/"bearish",
    "quantity": 2.5, "account": "gold"}。用來驗證整條「送出市價單」的流程實際能
    不能跑通，不會自動觸發，一定要手動呼叫這支API才會下單。account不指定的話
    預設"gold"(向後相容現有測試面板)，之後新增BTC等帳戶可以指定不同的account。

    務必先確認 GET /execution/status?account=... 顯示 testnet: true，再呼叫
    這支API，避免不小心對正式環境送出真實訂單。

    下單前會先查詢當下的真實買一/賣一(book ticker)，下單後拿實際成交價
    比對，算出「真正執行滑點」跟「當下買賣價差」兩個數字回傳，並發送
    Telegram通知(標示【手動測試】)——這樣手動測試也能跟即時模擬單一樣，
    直接驗證真實執行品質，不用另外肉眼比對(修正記錄見README)。
    """
    ok, error = settings_module.verify_password(payload.get("password", ""))
    if not ok:
        return {"success": False, "error": error}

    account = payload.get("account", "gold")
    if not execution_module.status(account=account)["testnet"]:
        return {"success": False, "error": "目前設定是正式環境(非測試網)，這支測試用endpoint拒絕執行，避免誤觸真實下單"}

    direction = payload.get("direction")
    quantity = payload.get("quantity", 1.0)
    symbol = execution_module._resolve_symbol(payload.get("symbol"))

    if direction not in ("bullish", "bearish"):
        return {"success": False, "error": "direction必須是bullish或bearish"}

    book_ok, book = execution_module.get_book_ticker(symbol, account=account) if symbol else (False, None)
    bid, ask = (book["bid"], book["ask"]) if book_ok else (None, None)

    success, result = execution_module.open_position(direction, quantity, symbol=symbol, account=account)

    execution_quality = None
    actual_fill_price = None
    if success:
        actual_fill_price = execution_module.extract_fill_price(result)
        if actual_fill_price:
            execution_quality = execution_module.analyze_execution_quality(direction, bid, ask, actual_fill_price, is_close=False)

    slippage_note = None
    if execution_quality:
        slippage_note = (
            f"預期成交價{execution_quality['expected_fill_price']:.2f}(依決策當下ask/bid) vs "
            f"實際成交價{actual_fill_price:.2f}，真正執行滑點{execution_quality['slippage_points']:+.2f}points"
            f"，當下價差{execution_quality['spread']:.2f}points"
        )
    elif success:
        # 不要靜默略過——明確講出是「成交價拿不到」還是「盤口bid/ask拿不到」
        # (修正記錄見README)
        if not actual_fill_price:
            slippage_note = f"(無法計算執行品質：幣安訂單回應裡沒有avgPrice，原始回應：{result})"
        elif not (bid and ask):
            slippage_note = f"(無法計算執行品質：查不到當下bid/ask，成交價是{actual_fill_price:.2f}，book_ticker查詢結果：{book}，book_ok={book_ok})"

    try:
        notifier.notify_trade_event(
            action="open", label="手動測試", direction=direction, price=bid or ask or 0,
            executed=success, execution_error=None if success else result,
            account=account, slippage_note=slippage_note,
        )
    except Exception as e:
        logger.error(f"手動測試下單通知發送失敗: {e}")

    return {"success": success, "result": result, "execution_quality": execution_quality}


@app.post("/execution/test-close")
async def execution_test_close(payload: dict = Body(...)):
    """
    手動測試平倉：payload格式 {"password": "...", "direction": "bullish"/"bearish", "account": "gold"}。
    跟test-order一樣，會計算執行品質並發送Telegram通知(修正記錄見README)。
    """
    ok, error = settings_module.verify_password(payload.get("password", ""))
    if not ok:
        return {"success": False, "error": error}

    account = payload.get("account", "gold")
    if not execution_module.status(account=account)["testnet"]:
        return {"success": False, "error": "目前設定是正式環境(非測試網)，這支測試用endpoint拒絕執行，避免誤觸真實下單"}

    direction = payload.get("direction")
    if direction not in ("bullish", "bearish"):
        return {"success": False, "error": "direction必須是bullish或bearish"}

    symbol = execution_module._resolve_symbol(payload.get("symbol"))
    book_ok, book = execution_module.get_book_ticker(symbol, account=account) if symbol else (False, None)
    bid, ask = (book["bid"], book["ask"]) if book_ok else (None, None)

    success, result = execution_module.close_position(direction, symbol=symbol, account=account)

    execution_quality = None
    actual_fill_price = None
    if success:
        actual_fill_price = execution_module.extract_fill_price(result)
        if actual_fill_price:
            execution_quality = execution_module.analyze_execution_quality(direction, bid, ask, actual_fill_price, is_close=True)

    slippage_note = None
    if execution_quality:
        slippage_note = (
            f"預期成交價{execution_quality['expected_fill_price']:.2f}(依決策當下ask/bid) vs "
            f"實際成交價{actual_fill_price:.2f}，真正執行滑點{execution_quality['slippage_points']:+.2f}points"
            f"，當下價差{execution_quality['spread']:.2f}points"
        )
    elif success:
        if not actual_fill_price:
            slippage_note = f"(無法計算執行品質：幣安訂單回應裡沒有avgPrice，原始回應：{result})"
        elif not (bid and ask):
            slippage_note = f"(無法計算執行品質：查不到當下bid/ask，成交價是{actual_fill_price:.2f})"

    try:
        notifier.notify_trade_event(
            action="close", label="手動測試", direction=direction, price=bid or ask or 0,
            exit_reason="手動測試平倉", pnl_points=0,
            executed=success, execution_error=None if success else result,
            account=account, slippage_note=slippage_note,
        )
    except Exception as e:
        logger.error(f"手動測試平倉通知發送失敗: {e}")

    return {"success": success, "result": result, "execution_quality": execution_quality}


@app.get("/paper-trading/summary")
async def paper_trading_summary(limit: int = 50, engine_id: str = "chan_profile_900"):
    """
    模擬單績效摘要：總筆數、勝率、總損益(points)、獲利因子、最大回撤、
    目前開倉狀態、最近N筆紀錄、以及對照「達標門檻」的評估結果。
    用來在正式接軌Pepperstone MT5自動下單前，評估這套訊號邏輯值不值得真的接execution。

    engine_id指定要看哪一個追蹤引擎(不再用interval_seconds查詢，因為現在同一個
    K線週期可能有多個策略的引擎平行運作，例如1分K纏論"chan_profile_60"跟
    1分K共振"resonance_fvg_60"都是60秒週期但是不同引擎，光用週期已經無法唯一
    區分。可用的engine_id可以查PAPER_TRADING_ENGINES.keys()，目前有：
    chan_profile_60、chan_profile_300、chan_profile_900、resonance_fvg_60)。
    """
    engine = PAPER_TRADING_ENGINES.get(engine_id)
    if engine is None:
        return {"error": f"沒有engine_id={engine_id}的追蹤引擎，可用的有: {list(PAPER_TRADING_ENGINES.keys())}"}
    return engine.get_summary(limit=limit)


@app.get("/paper-trading/slippage-by-hour")
async def paper_trading_slippage_by_hour(engine_id: str = "chan_profile_300"):
    """
    按小時(UTC)分組統計真實下單的滑價/價差資料，用來找出「哪個時段特別
    容易滑價」這種規律——原本這些數字只是曇花一現顯示在Telegram通知裡，
    使用者實際觀察到某幾筆單滑點特別大、懷疑跟時段有關，這支endpoint
    讓他能用資料驗證，不用肉眼從Telegram訊息裡一則一則回頭找、憑印象猜
    (修正記錄見README)。

    只統計「有真實下單過」的交易，純模擬的交易不會有滑價資料、不會被
    納入統計。開倉/平倉滑點分開統計，因為進場和出場當下的市況不一定相關。
    """
    return db.get_slippage_stats_by_hour(engine_id=engine_id)


@app.get("/paper-trading/trades-by-hour")
async def paper_trading_trades_by_hour(engine_id: str = "chan_profile_300", hour_utc: int = 0, side: str = "entry"):
    """
    滑價時段統計的drill-down：撈某個UTC小時內有真實下單滑價資料的個別交易，
    讓使用者能點進統計表裡的某個小時、看到該小時每一筆交易的完整脈絡
    (精確時間、方向、進場理由、預期價vs實際價、最後賺賠)，用來判斷某個
    極端滑點值到底是系統性問題還是單次意外(修正記錄見README)。
    side="entry"看開倉滑價、"exit"看平倉滑價。
    """
    if side not in ("entry", "exit"):
        return {"error": "side必須是entry或exit"}
    if not (0 <= hour_utc <= 23):
        return {"error": "hour_utc必須在0~23之間"}
    return {"trades": db.get_trades_by_hour(engine_id=engine_id, hour_utc=hour_utc, side=side)}


@app.get("/backtest/run")
async def backtest_run(
    days: int = 2,
    symbol: str = "XAUUSDT",
    interval_seconds: int = 300,
    bucket_size: float = 1.0,
    trade_limit: int = 3000,
    sl_points: Optional[float] = None,
    trail_trigger_points: Optional[float] = None,
    trail_distance_points: Optional[float] = None,
    reversal_confirm_count: Optional[int] = None,
    use_atr: Optional[bool] = None,
    atr_sl_multiplier: Optional[float] = None,
    atr_trigger_multiplier: Optional[float] = None,
    atr_trail_multiplier: Optional[float] = None,
    use_chop_filter: Optional[bool] = None,
    chop_threshold: Optional[float] = None,
    strategy_type: Optional[str] = None,
    resonance_min_conditions: int = 4,
):
    """
    歷史回測：抓Binance過去N天(上限7天)的K線資料，套用跟即時模擬單完全相同的
    訊號邏輯和交易規則，快速驗證策略表現，不用乾等即時模擬單累積樣本數。

    symbol可以指定任何幣安期貨合約(預設XAUUSDT)，用來驗證這套訊號邏輯換到
    別的商品(例如BTCUSDT)適不適用。注意：換商品時bucket_size(分價量表箱寬)
    通常也要跟著調整——1.0是配合黃金約4000多美元的價位調的，BTC價位通常在
    幾萬美元，箱寬還是1的話會切出大量沒意義的小格子，建議依商品價位等比例
    放大(例如BTC可以試50~100)。這個參數只影響回測，不影響即時模擬單。
    這是on-demand計算，天數越多、跑的時間越久(纏論分析在大量K棒上會變慢)。

    sl_points等這些交易參數沒有明確指定的話，會自動退回使用目前dashboard
    設定面板生效中的參數(跟即時模擬單一致)。**但dashboard的回測面板現在
    會把所有這些參數當作獨立輸入欄位讓使用者直接填**，不用先去「策略參數
    設定」把正式設定改掉才能測試不同組合——這是刻意的設計：如果使用者
    只是想「試試看回測結果」，不該被迫先動到正式設定(那會觸發
    settings_changed_at更新，讓即時模擬單的績效統計排除舊交易、變成
    「又要重新開始累積」，這是使用者實際遇到的困擾，回測本身從來不會
    寫入設定，只是介面沒有提供獨立輸入欄位，逼得使用者繞去改正式設定
    才能測試，修正記錄見README)。真的想要「正式套用」某組參數到即時
    模擬單時，再自己去策略參數設定手動填入、儲存，那個動作才會(也應該)
    觸發settings_changed_at。

    strategy_type不指定時預設"chan_profile"(即時模擬單目前使用的策略)，可以
    指定"resonance_fvg"測試多條件共振+FVG這套實驗性策略——這是目前唯一能
    測試這套策略的地方，不會影響即時模擬單(修正記錄見README)。

    resonance_min_conditions只有strategy_type="resonance_fvg"才會用到：四個
    子條件(RSI/EMA-FVG/價格行為/成交量)要符合幾個(含)以上才給訊號，預設4是
    嚴格AND邏輯(全部符合)，調低可以放寬門檻，用回測比較不同門檻下的訊號量
    /勝率/獲利因子取捨。

    重要：run_backtest()本身是同步、吃CPU的函式，如果直接在這個async函式裡
    呼叫，會整個卡住FastAPI唯一的事件循環，導致回測跑的時候其他所有請求
    (health check、dashboard、甚至Binance背景資料接收)都會被凍結，
    嚴重的話整個服務看起來像掛掉一樣(修正記錄見README)。
    用asyncio.to_thread()丟到背景執行緒跑，讓事件循環保持暢通。
    """
    return await asyncio.to_thread(
        backtest_module.run_backtest,
        days=days,
        symbol=symbol,
        interval_seconds=interval_seconds,
        bucket_size=bucket_size,
        trade_limit=trade_limit,
        sl_points=sl_points,
        trail_trigger_points=trail_trigger_points,
        trail_distance_points=trail_distance_points,
        reversal_confirm_count=reversal_confirm_count,
        use_atr=use_atr,
        atr_sl_multiplier=atr_sl_multiplier,
        atr_trigger_multiplier=atr_trigger_multiplier,
        atr_trail_multiplier=atr_trail_multiplier,
        use_chop_filter=use_chop_filter,
        chop_threshold=chop_threshold,
        strategy_type=strategy_type,
        resonance_min_conditions=resonance_min_conditions,
    )


@app.post("/backtest/sweep")
async def backtest_sweep_start(
    days: int = 2,
    interval_seconds: int = 300,
    use_atr: Optional[bool] = None,
    atr_sl_multiplier: Optional[float] = None,
    atr_trigger_multiplier: Optional[float] = None,
    atr_trail_multiplier: Optional[float] = None,
    sl_points: Optional[float] = None,
    trail_trigger_points: Optional[float] = None,
    trail_distance_points: Optional[float] = None,
    reversal_confirm_count: Optional[int] = None,
    use_chop_filter: Optional[bool] = None,
    chop_threshold: Optional[float] = None,
):
    """
    參數掃描：對模擬單風控參數做「一次改一個參數」的敏感度測試，一次跑多組回測，
    自動比較哪個參數方向、哪個數值表現比較好，不用手動一個一個試。

    立刻回傳job_id，實際運算在背景執行緒進行(不是asyncio.to_thread，是獨立的
    plain thread，因為要跑好幾組回測、耗時比單次回測長很多，用背景執行緒讓
    這支API能馬上回應，前端輪詢 GET /backtest/sweep/{job_id} 追蹤進度)。

    天數預設用2天(單組回測較快)，掃描本身會跑約10組回測，全部跑完可能要
    1-2分鐘，請求發起後用輪詢確認進度，不要每次都拉長天數，跑更多天在
    掃描情境下時間會乘以組數，容易太久。

    use_atr等這些參數不指定的話，掃描一律用「目前正式設定」當基準(對照組)，
    這是原本的行為。指定的話，會疊加在正式設定上面組成真正要用的基準，
    不用被迫先去改動正式設定才能用某組假設參數當基準做敏感度分析——跟單次
    回測面板的「回測交易參數(獨立於正式設定)」是同一組欄位、同一個概念，
    不會寫入正式設定、不會影響即時模擬單(修正記錄見README)。
    """
    baseline_overrides = {}
    if use_atr is not None:
        baseline_overrides["paper_use_atr_stops"] = 1 if use_atr else 0
    if atr_sl_multiplier is not None:
        baseline_overrides["paper_atr_sl_multiplier"] = atr_sl_multiplier
    if atr_trigger_multiplier is not None:
        baseline_overrides["paper_atr_trigger_multiplier"] = atr_trigger_multiplier
    if atr_trail_multiplier is not None:
        baseline_overrides["paper_atr_trail_multiplier"] = atr_trail_multiplier
    if sl_points is not None:
        baseline_overrides["paper_sl_points"] = sl_points
    if trail_trigger_points is not None:
        baseline_overrides["paper_trail_trigger_points"] = trail_trigger_points
    if trail_distance_points is not None:
        baseline_overrides["paper_trail_distance_points"] = trail_distance_points
    if reversal_confirm_count is not None:
        baseline_overrides["paper_reversal_confirm_count"] = reversal_confirm_count
    if use_chop_filter is not None:
        baseline_overrides["paper_use_chop_filter"] = 1 if use_chop_filter else 0
    if chop_threshold is not None:
        baseline_overrides["paper_chop_threshold"] = chop_threshold

    job_id = sweep_module.start_sweep(days=days, interval_seconds=interval_seconds, baseline_overrides=baseline_overrides or None)
    return {"job_id": job_id}


@app.get("/backtest/sweep/{job_id}")
async def backtest_sweep_status(job_id: str):
    """查詢參數掃描的進度和目前已完成的結果(結果會隨著背景執行緒跑完一組一組累積)。"""
    job = sweep_module.get_job(job_id)
    if job is None:
        return {"error": "找不到這個掃描任務(job_id可能錯誤，或服務重啟過導致任務遺失)"}
    return job


@app.get("/price/latest")
async def latest_price():
    """
    主要訊號源（OANDA XAU_USD）。未來實際下單走 CFD 經紀商時，
    這裡的價格基準應該跟執行端保持一致。
    """
    return streamer.get_latest() or {"message": "尚未收到任何報價，請稍後再試"}


@app.get("/price/recent")
async def recent_ticks(limit: int = 200):
    return streamer.get_recent_ticks(limit=limit)


@app.get("/price/latest/binance")
async def latest_price_binance():
    """
    輔助confirmation訊號源（Binance XAUUSDT 永續合約）。
    24/7 交易，可用來觀察 CFD 黃金收盤期間（週末）的價格動向、或跟主訊號源做交叉驗證。
    """
    return binance_streamer.get_latest() or {"message": "尚未收到任何報價，請稍後再試"}


@app.get("/price/recent/binance")
async def recent_ticks_binance(limit: int = 200):
    return binance_streamer.get_recent_ticks(limit=limit)


# ---------------------------------------------------------------------------
# 分析模組 endpoint：K線 / 分價量表 / 纏論分型-筆-中樞-背馳
# 資料源固定用 Binance 的逐筆成交（真實成交量）。
# ---------------------------------------------------------------------------

@app.get("/analysis/candles")
async def analysis_candles(interval_seconds: int = 300, trade_limit: int = 100000):
    """
    K線聚合。預設5分鐘一根，資料源是 Binance 的逐筆成交(aggTrade)。
    interval_seconds 可調整週期，例如 60 就是1分鐘K線，方便之後往下切。
    """
    trades = binance_streamer.get_recent_trades(limit=trade_limit)
    candles = build_candles(trades, interval_seconds=interval_seconds)
    return {
        "interval_seconds": interval_seconds,
        "candle_count": len(candles),
        "candles": candles,
    }


@app.get("/analysis/volume-profile")
async def analysis_volume_profile(bucket_size: float = 1.0, trade_limit: int = 100000):
    """
    分價量表。bucket_size 是每個價格箱的寬度(單位:USD)，例如0.5會分得更細。
    trade_limit 控制回看多少筆逐筆成交，數字越大涵蓋的時間範圍越長。
    """
    trades = binance_streamer.get_recent_trades(limit=trade_limit)
    profile = compute_volume_profile(trades, bucket_size=bucket_size)
    poc_info = poc_and_value_area(profile)
    return {
        "bucket_size": bucket_size,
        "trade_count": len(trades),
        "profile": profile,
        **poc_info,
    }


@app.get("/analysis/chan")
async def analysis_chan(interval_seconds: int = 300, trade_limit: int = 100000):
    """
    纏論分析：分型 -> 筆 -> 中樞 -> 背馳判斷。
    預設用5分鐘K線(interval_seconds=300)，之後要往下切1分鐘只要改參數即可，
    不用動到分析邏輯本身。
    """
    trades = binance_streamer.get_recent_trades(limit=trade_limit)
    candles = build_candles(trades, interval_seconds=interval_seconds)
    result = analyze_chan(candles)
    return {
        "interval_seconds": interval_seconds,
        "source_candle_count": len(candles),
        **result,
    }


@app.get("/signal/latest")
async def signal_latest(interval_seconds: int = 300, bucket_size: float = 1.0, trade_limit: int = 3000):
    """
    綜合訊號：纏論(中樞突破/背馳) + 分價量表(POC/Value Area)，
    兩者方向一致且至少一邊夠強才會是「訊號」，否則是「關注」或「中性」。
    這是未來要接給MT5 EA輪詢的endpoint，也是Telegram通知、模擬單追蹤共用的
    核心邏輯(見 app/signal_engine.py)，三邊都保證用同一份計算結果。
    """
    return compute_full_signal(interval_seconds=interval_seconds, bucket_size=bucket_size, trade_limit=trade_limit)


@app.websocket("/ws/price")
async def ws_price(websocket: WebSocket):
    """
    OANDA 即時推播：每 0.5 秒檢查一次共享狀態，若價格有變化就推給前端。
    """
    await websocket.accept()
    last_sent_time = None
    try:
        while True:
            latest = streamer.get_latest()
            if latest and latest.get("time") != last_sent_time:
                await websocket.send_json(latest)
                last_sent_time = latest.get("time")
            await asyncio.sleep(0.5)
    except WebSocketDisconnect:
        pass


@app.websocket("/ws/price/binance")
async def ws_price_binance(websocket: WebSocket):
    """Binance XAUUSDT 即時推播，跟 /ws/price 是獨立連線，前端可以同時訂閱兩條。"""
    await websocket.accept()
    last_sent_time = None
    try:
        while True:
            latest = binance_streamer.get_latest()
            if latest and latest.get("time") != last_sent_time:
                await websocket.send_json(latest)
                last_sent_time = latest.get("time")
            await asyncio.sleep(0.5)
    except WebSocketDisconnect:
        pass


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("app.main:app", host="0.0.0.0", port=port)
