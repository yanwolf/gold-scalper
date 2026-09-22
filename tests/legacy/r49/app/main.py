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
import functools
import threading
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse

from app.oanda_client import streamer
from app.binance_client import binance_streamer
from app.analysis import build_candles, compute_volume_profile, poc_and_value_area, analyze_chan, interpret_volume_profile
from app.signal_engine import compute_full_signal
from app.notifier import notifier
from app.paper_trading import PAPER_TRADING_ENGINES
from app.health_monitor import health_monitor
from app import backtest as backtest_module
from app import sweep as sweep_module
from app import settings as settings_module
from app import execution as execution_module
from app import db
from app import role as role_module
from app import risk_guard
from app import preflight as preflight_module

logger = logging.getLogger("main")

app = FastAPI(title="Gold Scalping Analyzer", version="0.1.0")

# 開發階段先全開，正式上線建議改成前端網域白名單
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# 服務角色閘門(修正記錄見README)：live角色下，研究/實驗用的路由一律403，
# 參數也不能直接改(只能走 /settings/import)。用middleware擋在最外層，
# 不用每個endpoint各自判斷，之後新加的實驗endpoint只要放進role.LAB_ONLY_*就會被擋。
@app.middleware("http")
async def role_gate(request, call_next):
    if role_module.is_path_blocked(request.method, request.url.path):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=403, content={
            "success": False,
            "error": f"這是正式執行端(APP_ROLE=live)，不提供 {request.method} {request.url.path}；研究/改參數請到 lab 端，參數請用「匯入參數集」更新",
        })
    return await call_next(request)


@app.on_event("startup")
async def startup_event():
    logger.info(f"服務角色: {role_module.describe()}")
    db.init_schema()  # 要在 binance_streamer.start() 之前，回填歷史資料時才讀得到
    streamer.start()
    binance_streamer.start()
    notifier.start()
    for engine in PAPER_TRADING_ENGINES.values():
        engine.start()  # 依角色載入的引擎平行啟動，各自獨立追蹤
    health_monitor.start()  # 放最後，確保要監控的元件都已經start()過了
    # 開機跑一次交易所相容性自檢(BINANCE_LESSONS.md)，異常發Telegram，
    # 不要等到真的下單才發現API又改了。丟背景thread避免拖慢啟動。
    threading.Thread(target=lambda: preflight_module.run_and_report(send=True, force=True), daemon=True).start()
    if role_module.is_live():
        # 正式端啟動時跟交易所對帳：DB記得有部位但交易所沒有(或反過來)就立刻告警，
        # 避免程序重啟後出現沒人管的孤兒單
        # (注意：這裡不能再寫 import threading——函式內任何地方出現 import，
        # Python 會把 threading 當成整個函式的區域變數，上面那行用到 threading
        # 時就會 UnboundLocalError，整個服務啟動失敗。threading 已在檔案最上方匯入)
        threading.Thread(target=_reconcile_with_exchange_on_startup, daemon=True).start()


def _reconcile_with_exchange_on_startup():
    import time
    time.sleep(10)  # 等串流跟引擎都熱身完
    lines = []
    for engine_id, engine in PAPER_TRADING_ENGINES.items():
        if not getattr(engine, "execution_index", None):
            continue
        try:
            if not getattr(engine, "_seeded_from_db", True):
                lines.append(f"{engine.label}: 持倉紀錄還沒從資料庫載入(讀不到)，無法對帳，請檢查資料庫")
                continue
            db_pos = engine.get_position() if hasattr(engine, "get_position") else getattr(engine, "_position", None)
            ok, info = execution_module.get_position_info(account=engine.execution_account)
            if not ok:
                lines.append(f"{engine.label}: 查詢交易所部位失敗 {info}")
                continue
            # 依「方向」對帳，不是加總淨部位(BINANCE_LESSONS.md第7條)：雙向模式下
            # 多0.1+空0.1淨額是0會誤判空手；我的多單已停損、帳上剩別的空單時，
            # 只看有沒有部位會誤判成自己還在場。
            sym = execution_module._resolve_symbol(getattr(engine, "execution_symbol", None))
            if not isinstance(info, list) or not any(r.get("symbol") == sym for r in info):
                # 帶symbol查詢卻沒有這個幣的列＝查詢異常(第2條r17/r18)，不能當成「空手」
                lines.append(f"{engine.label}: 查不到部位資料(回傳空清單)，這次無法對帳，請手動確認")
                continue
            long_qty = short_qty = 0.0
            for row in info if isinstance(info, list) else []:
                try:
                    amt = float(row.get("positionAmt", 0))
                except (TypeError, ValueError):
                    continue
                side = row.get("positionSide", "BOTH")
                if side == "LONG" or (side == "BOTH" and amt > 0):
                    long_qty += abs(amt)
                elif side == "SHORT" or (side == "BOTH" and amt < 0):
                    short_qty += abs(amt)
            has_db = bool(db_pos and db_pos.get("real_open_executed"))
            if has_db:
                my_side_long = db_pos.get("direction") == "bullish"
                # 扣掉送單前就有的部位(基準，第3條r16)，剩下的才是自己的
                my_qty = max(0.0, (long_qty if my_side_long else short_qty) - float(db_pos.get("real_open_baseline") or 0))
                other_qty = short_qty if my_side_long else long_qty
                if my_qty > 1e-9:
                    lines.append(f"{engine.label}: 對帳一致(有{'多' if my_side_long else '空'}單 {my_qty})")
                else:
                    lines.append(
                        f"{engine.label}: 對帳不一致 — 程式記錄有{'多' if my_side_long else '空'}單，交易所這一側沒有部位"
                        + (f"(反方向另有 {other_qty}，不是這筆)" if other_qty > 1e-9 else "") + "，請手動確認"
                    )
            elif long_qty > 1e-9 or short_qty > 1e-9:
                lines.append(f"{engine.label}: 對帳不一致 — 程式記錄沒有真實部位，交易所有部位(多 {long_qty} / 空 {short_qty})，請手動確認")
            else:
                lines.append(f"{engine.label}: 對帳一致(空手)")
        except Exception as e:
            lines.append(f"{engine.label}: 對帳時發生錯誤 {e}")
    if lines:
        # 對帳訊息順便帶帳戶餘額，手機上一眼看到真錢還剩多少(修正記錄見README)
        try:
            from app import execution as _exec
            bal_line = _exec.usdt_balance_line()
            if bal_line:
                lines.append(bal_line)
        except Exception:
            pass
        text = "🟡 正式端啟動對帳\n" + "\n".join(lines)
        logger.info(text)
        if notifier.is_enabled:
            notifier.send_raw_message(text)
        db.insert_settings_audit("startup_reconcile", detail={"lines": lines})


@app.api_route("/live-proxy/{path:path}", methods=["GET", "POST"])
async def live_proxy(path: str, request: __import__("fastapi").Request):
    """
    lab端代理到live端(修正記錄見README)：讓使用者在同一個網頁的「正式端」分頁監看和
    控制正式服務，不用兩個網址切來切去。只放行LIVE_PROXY_ALLOWED_PREFIXES裡的路徑，
    密碼跟著body原樣轉過去由live端驗證。
    """
    if role_module.is_live() or not role_module.LIVE_BASE_URL:
        return {"success": False, "error": "這個服務沒有設定 LIVE_BASE_URL(或本身就是live)，無法代理"}
    target_path = "/" + path
    if not any(target_path.startswith(pfx) for pfx in role_module.LIVE_PROXY_ALLOWED_PREFIXES):
        return {"success": False, "error": f"不允許代理的路徑: {target_path}"}
    import requests as _rq
    url = role_module.LIVE_BASE_URL + target_path
    try:
        if request.method == "GET":
            resp = _rq.get(url, params=dict(request.query_params), timeout=15)
        else:
            body = await request.body()
            resp = _rq.post(url, data=body, headers={"Content-Type": request.headers.get("content-type", "application/json")}, timeout=30)
        try:
            return resp.json()
        except ValueError:
            return {"success": False, "error": f"live端回傳非JSON(HTTP {resp.status_code})"}
    except Exception as e:
        return {"success": False, "error": f"連不到live端: {e}"}


@app.get("/app/role")
async def app_role():
    """服務角色與控制狀態：dashboard載入時先問這支，決定要顯示哪些分頁/按鈕。"""
    return {
        **role_module.describe(),
        "active_engines": list(PAPER_TRADING_ENGINES.keys()),
        "manual_halt": risk_guard.get_manual_halt(),
    }


@app.post("/control/halt")
async def control_halt(payload: dict = Body(...)):
    """緊急停止：payload {"password", "reason"}。之後所有引擎不再送新的真實開倉單，既有部位照常管理。"""
    ok, error = settings_module.verify_password(payload.get("password", ""))
    if not ok:
        return {"success": False, "error": error}
    state = risk_guard.set_manual_halt(True, payload.get("reason") or "手動停止")
    db.insert_settings_audit("manual_halt", detail=state)
    if notifier.is_enabled:
        notifier.send_raw_message(f"🛑 手動緊急停止已啟動\n原因：{state['reason']}\n所有引擎暫停新的真實開倉，既有部位照常管理出場")
    return {"success": True, "manual_halt": state}


@app.post("/control/resume")
async def control_resume(payload: dict = Body(...)):
    ok, error = settings_module.verify_password(payload.get("password", ""))
    if not ok:
        return {"success": False, "error": error}
    state = risk_guard.set_manual_halt(False)
    db.insert_settings_audit("manual_resume", detail=state)
    if notifier.is_enabled:
        notifier.send_raw_message("🟢 手動緊急停止已解除，恢復真實開倉")
    return {"success": True, "manual_halt": state}


# 網頁交易入口的保護(第8條r24「網頁請求也是另一條執行緒」)：例外穿出去時網頁只看到連線中斷、
# 沒有推播。包一層：回錯誤給網頁、照第8條節奏推播、恢復時通知。引擎的平倉(強制平倉)另外有
# 自己的外層包裝，結帳前出錯會記待平倉。
_web_trade_errors = {}


def guard_trade(op):
    def deco(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            from app import alert_cadence
            try:
                res = await fn(*args, **kwargs)
            except Exception as e:
                n = _web_trade_errors.get(op, 0) + 1
                _web_trade_errors[op] = n
                logger.error(f"網頁操作「{op}」出錯(第{n}次): {e}")
                if alert_cadence.should_alert(n):
                    try:
                        notifier.send_raw_message(
                            f"⚠️ 網頁操作「{op}」出錯(第 {n} 次)\n錯誤：{type(e).__name__}: {e}\n"
                            f"這次操作沒有完成，請到幣安確認帳戶狀態")
                    except Exception:
                        pass
                return {"error": f"{type(e).__name__}: {e}", "op": op}
            n = _web_trade_errors.pop(op, 0)
            if n:
                try:
                    notifier.send_raw_message(f"✅ 網頁操作「{op}」已恢復(出錯 {n} 次後)")
                except Exception:
                    pass
            return res
        return wrapper
    return deco


@app.post("/control/flatten")
@guard_trade("強制平倉")
async def control_flatten(payload: dict = Body(...)):
    """
    強制平倉：payload {"password", "engine_id"(可選，不給=全部載入中的引擎), "reason"}。
    走引擎正常的出場流程(含真實平倉單、通知、統計)，並順手啟動緊急停止避免馬上又開新倉。
    """
    ok, error = settings_module.verify_password(payload.get("password", ""))
    if not ok:
        return {"success": False, "error": error}
    reason = payload.get("reason") or "手動緊急平倉"
    targets = [payload["engine_id"]] if payload.get("engine_id") else list(PAPER_TRADING_ENGINES.keys())
    results = {}
    for eid in targets:
        eng = PAPER_TRADING_ENGINES.get(eid)
        if not eng:
            results[eid] = {"closed": False, "message": "沒有這個引擎"}
            continue
        closed, msg = eng.force_close(reason)
        results[eid] = {"closed": closed, "message": msg}
    state = risk_guard.set_manual_halt(True, f"強制平倉後自動停止({reason})")
    db.insert_settings_audit("manual_flatten", detail={"results": results, "reason": reason})
    if notifier.is_enabled:
        notifier.send_raw_message("🛑 手動強制平倉\n" + "\n".join(f"{k}: {v['message']}" for k, v in results.items()) + "\n已同時啟動緊急停止")
    return {"success": True, "results": results, "manual_halt": state}


@app.get("/settings/export")
async def settings_export(engine_id: str):
    """
    匯出參數集(修正記錄見README)：lab端驗證完一組參數後，用這支產生帶版本號的JSON，
    拿到live端「匯入參數集」貼上。內容是該引擎「實際生效」的所有交易相關參數
    (全域+專屬覆寫合併後的結果)，匯入端會把它們全部寫成專屬覆寫，讓正式引擎
    的參數完全釘死、不受對方全域設定影響。
    """
    if engine_id not in PAPER_TRADING_ENGINES:
        return {"error": f"沒有engine_id={engine_id}的引擎，可用的有: {list(PAPER_TRADING_ENGINES.keys())}"}
    import hashlib, json
    from datetime import datetime, timezone
    effective = settings_module.get_settings(engine_id=engine_id)
    params = {k: effective[k] for k in sorted(settings_module.TRADING_RELEVANT_KEYS) if k in effective}
    digest = hashlib.sha256(json.dumps(params, sort_keys=True, default=str).encode()).hexdigest()[:8]
    now = datetime.now(timezone.utc)
    param_set = {
        "format": "gold-scalper-param-set/1",
        "version": f"ps-{now.strftime('%Y%m%d-%H%M')}-{digest}",
        "exported_at": now.isoformat(),
        "source": role_module.describe(),
        "engine_id": engine_id,
        "params": params,
    }
    db.insert_settings_audit("export", engine_id=engine_id, version=param_set["version"], detail={"params": params})
    return param_set


@app.post("/settings/import")
async def settings_import(payload: dict = Body(...)):
    """
    匯入參數集：payload {"password", "engine_id"(目標引擎，預設用參數集裡的), "param_set": {...}, "force": bool}。
    安全規則：
      - 密碼驗證、格式驗證、每個key必須是TRADING_RELEVANT_KEYS且能通過型別轉換
      - 目標引擎有未平倉部位時拒絕(force=true才放行)，避免中途改停損邏輯
      - 寫入方式是「全部寫成專屬覆寫」，之後全域設定怎麼改都不影響這個引擎
      - 每次匯入寫一筆審計(版本號、前後差異)，並發Telegram
    lab和live兩端都能用，但這是live端唯一能改參數的入口。
    """
    ok, error = settings_module.verify_password(payload.get("password", ""))
    if not ok:
        return {"success": False, "error": error}
    ps = payload.get("param_set") or {}
    if not isinstance(ps, dict) or ps.get("format") != "gold-scalper-param-set/1" or not isinstance(ps.get("params"), dict) or not ps.get("params"):
        return {"success": False, "error": "參數集格式不正確(需要 format=gold-scalper-param-set/1 和 params)"}
    engine_id = payload.get("engine_id") or ps.get("engine_id")
    engine = PAPER_TRADING_ENGINES.get(engine_id)
    if not engine:
        return {"success": False, "error": f"沒有engine_id={engine_id}的引擎，可用的有: {list(PAPER_TRADING_ENGINES.keys())}"}
    bad = [k for k in ps["params"] if k not in settings_module.TRADING_RELEVANT_KEYS]
    if bad:
        return {"success": False, "error": f"參數集含有不允許的欄位: {bad}"}
    pos = getattr(engine, "_position", None)
    if pos and not payload.get("force"):
        return {"success": False, "error": "目標引擎目前有未平倉部位，等平倉後再匯入(或帶 force=true 強制)"}
    before = {k: settings_module.get_settings(engine_id=engine_id).get(k) for k in ps["params"]}
    try:
        applied, cleared = settings_module.update_engine_overrides(engine_id, ps["params"])
    except settings_module.SettingsValidationError as e:
        return {"success": False, "error": str(e)}
    after = {k: settings_module.get_settings(engine_id=engine_id).get(k) for k in ps["params"]}
    diff = {k: {"before": before[k], "after": after[k]} for k in ps["params"] if before[k] != after[k]}
    db.insert_settings_audit("import", engine_id=engine_id, version=ps.get("version"),
                             detail={"diff": diff, "source": ps.get("source"), "forced": bool(payload.get("force"))})
    if notifier.is_enabled:
        lines = [f"{k}: {v['before']} → {v['after']}" for k, v in diff.items()] or ["(沒有任何值改變)"]
        notifier.send_raw_message(f"📥 參數集已匯入 {engine.label}\n版本：{ps.get('version')}\n" + "\n".join(lines))
    return {"success": True, "engine_id": engine_id, "version": ps.get("version"), "diff": diff, "applied": applied}


@app.get("/settings/audit")
async def settings_audit(limit: int = 50):
    return {"items": db.get_settings_audit(limit=limit)}


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


@app.get("/preflight")
async def preflight_check(send: bool = False):
    """
    交易所相容性自檢(見BINANCE_LESSONS.md、app/preflight.py)：手動重跑用。
    send=true時異常會額外發一次Telegram(跟開機自動跑的那次一樣)，
    預設false只回傳結果給網頁看，不重複發通知。
    """
    return preflight_module.run_and_report(send=send)


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

    values = payload.get("values")
    if not isinstance(values, dict) or not values:
        # 格式錯或空的不能當成「沒有要改的」照樣回成功(第8條r44)
        return {"success": False, "error": "values 必須是非空的 {欄位: 值}，這次沒有改任何設定"}
    try:
        updated = settings_module.update_settings(values)
    except settings_module.SettingsValidationError as e:
        return {"success": False, "error": str(e)}
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
    if not isinstance(payload.get("values"), dict) or not payload.get("values"):
        return {"success": False, "error": "values 必須是非空的 {欄位: 值}，這次沒有改任何設定"}
    try:
        applied, cleared = settings_module.update_engine_overrides(engine_id, payload.get("values", {}))
    except settings_module.SettingsValidationError as e:
        return {"success": False, "error": str(e)}
    return {
        "success": True, "applied": applied, "cleared": cleared,
        "overrides": settings_module.get_engine_overrides(engine_id),
        "effective": settings_module.get_settings(engine_id=engine_id),
    }


@app.post("/settings/engine/{engine_id}/reset-stats")
async def reset_engine_stats(engine_id: str, payload: dict = Body(...)):
    """
    「從現在起重新統計」：不改參數，只把這個引擎的績效統計分界設到現在。
    修了資料層bug之後舊資料是髒的但參數沒變、原本分界機制不會觸發，用這個
    手動畫線(修正記錄見README)。
    """
    ok, error = settings_module.verify_password(payload.get("password", ""))
    if not ok:
        return {"success": False, "error": error}
    if engine_id not in PAPER_TRADING_ENGINES:
        return {"success": False, "error": f"沒有engine_id={engine_id}的追蹤引擎"}
    boundary = settings_module.reset_engine_stats_boundary(engine_id)
    return {"success": True, "boundary": boundary}


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


@app.get("/execution/account-identity")
async def execution_account_identity(password: str = ""):
    """
    檢查gold與gold_1m兩把金鑰是不是指向同一個幣安帳戶(修正記錄見README)。
    使用者「申請獨立demo API」可能只是同一個測試網帳戶下的第二把金鑰而非子帳戶；
    若是同一帳戶，兩個引擎的部位會在同一個帳本上互相抵銷。判斷方式：兩邊的
    餘額明細與部位快照完全相同就幾乎可以確定是同一帳戶。需要密碼。
    """
    ok, error = settings_module.verify_password(password)
    if not ok:
        return {"success": False, "error": error}
    out = {}
    for acct in ("gold", "gold_1m"):
        if not execution_module.is_enabled(acct):
            out[acct] = {"enabled": False}
            continue
        b_ok, bal = execution_module.get_account_balance(account=acct)
        p_ok, pos = execution_module.get_position_info(account=acct)
        out[acct] = {
            "enabled": True,
            "balances": {b["asset"]: b.get("balance") for b in bal} if b_ok and isinstance(bal, list) else bal,
            "positions": [{"symbol": p["symbol"], "positionAmt": p["positionAmt"]} for p in pos if float(p.get("positionAmt", 0)) != 0] if p_ok and isinstance(pos, list) else pos,
        }
    same = None
    if out.get("gold", {}).get("enabled") and out.get("gold_1m", {}).get("enabled"):
        same = out["gold"].get("balances") == out["gold_1m"].get("balances") and out["gold"].get("positions") == out["gold_1m"].get("positions")
    return {"success": True, "accounts": out, "likely_same_account": same}


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


@app.get("/execution/open-orders")
async def execution_open_orders(password: str = "", account: str = "gold"):
    """查詢帳戶所有未成交掛單(整個帳戶、不限symbol)。殘留掛單會擋住持倉模式切換(-4067)。"""
    ok, error = settings_module.verify_password(password)
    if not ok:
        return {"success": False, "error": error}
    success, data = execution_module.get_open_orders(account=account)
    return {"success": success, "data": data}


@app.post("/execution/cancel-open-orders")
@guard_trade("取消掛單")
async def execution_cancel_open_orders(payload: dict = Body(...)):
    """
    取消帳戶所有未成交掛單：payload {"password": "...", "account": "gold"}。
    會先列出帳戶所有掛單的symbol，再逐一呼叫allOpenOrders取消。只動掛單、不碰部位。
    """
    ok, error = settings_module.verify_password(payload.get("password", ""))
    if not ok:
        return {"success": False, "error": error}
    account = payload.get("account", "gold")
    oo_ok, orders = execution_module.get_open_orders(account=account)
    if not oo_ok:
        return {"success": False, "error": orders}
    symbols = sorted({o.get("symbol") for o in orders if o.get("symbol")})
    results = {}
    for sym in symbols:
        c_ok, c_res = execution_module.cancel_all_open_orders(symbol=sym, account=account)
        results[sym] = {"success": c_ok, "result": c_res}
    return {"success": all(r["success"] for r in results.values()) if results else True,
            "cancelled_symbols": symbols, "results": results, "order_count": len(orders)}


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
@guard_trade("設定槓桿")
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
@guard_trade("手動測試下單")
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
    # 持倉紀錄沒載入時手動下單也要擋(第8條r39)：「沒載入不開倉」寫在引擎裡，這支是直接送單、不經過引擎。
    # (手動測試平倉不擋：那是減少風險的動作)
    not_loaded = [e.label for e in PAPER_TRADING_ENGINES.values()
                  if getattr(e, "execution_account", None) == account and not getattr(e, "_seeded_from_db", True)]
    if not_loaded:
        return {"success": False, "error": f"帳戶 {account} 的引擎({', '.join(not_loaded)})持倉紀錄還沒從資料庫載入，暫停手動下單"}
    # 正式環境要多帶confirm_live=true才放行(dashboard會先跳紅字確認框再帶上)：
    # 接正式金鑰後必須用程式自己的路徑打一張最小單驗證，但不能讓人手滑點到
    if not execution_module.status(account=account)["testnet"] and not payload.get("confirm_live"):
        return {"success": False, "error": "目前設定是正式環境(非測試網)，要在正式環境送測試單請在確認框按確定(confirm_live=true)，避免誤觸真實下單"}

    direction = payload.get("direction")
    quantity = payload.get("quantity", 1.0)
    symbol = execution_module._resolve_symbol(payload.get("symbol"))

    if direction not in ("bullish", "bearish"):
        return {"success": False, "error": "direction必須是bullish或bearish"}

    book_ok, book = execution_module.get_book_ticker(symbol, account=account) if symbol else (False, None)
    bid, ask = (book["bid"], book["ask"]) if book_ok else (None, None)

    hedge = bool(settings_module.get_settings().get("execution_hedge_mode", 1))
    hedge, mode_warning = execution_module.resolve_position_mode(hedge, account=account)
    success, result = execution_module.open_position(direction, quantity, symbol=symbol, account=account, hedge=hedge)

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

    # Telegram改成背景送(修正記錄見README)：手機Safari對這支請求的耐心只有十幾秒，
    # 下單本身要打3~4次幣安API，再同步等Telegram回應就可能超時，畫面顯示「Load failed」
    # 但單其實已經成交，使用者會誤以為失敗而重送。
    import threading as _th
    def _notify_open():
        try:
            notifier.notify_trade_event(
                action="open", label="手動測試", direction=direction, price=bid or ask or 0,
                executed=success, execution_error=None if success else result,
                account=account, slippage_note=slippage_note,
            )
        except Exception as e:
            logger.warning(f"手動測試開倉通知失敗: {e}")
    _th.Thread(target=_notify_open, daemon=True).start()

    return {"success": success, "result": result, "execution_quality": execution_quality,
            "hedge_mode_used": hedge, "warning": mode_warning}


@app.post("/execution/test-close")
@guard_trade("手動測試平倉")
async def execution_test_close(payload: dict = Body(...)):
    """
    手動測試平倉：payload格式 {"password": "...", "direction": "bullish"/"bearish", "account": "gold"}。
    跟test-order一樣，會計算執行品質並發送Telegram通知(修正記錄見README)。
    """
    ok, error = settings_module.verify_password(payload.get("password", ""))
    if not ok:
        return {"success": False, "error": error}

    account = payload.get("account", "gold")
    # 正式環境要多帶confirm_live=true才放行(dashboard會先跳紅字確認框再帶上)：
    # 接正式金鑰後必須用程式自己的路徑打一張最小單驗證，但不能讓人手滑點到
    if not execution_module.status(account=account)["testnet"] and not payload.get("confirm_live"):
        return {"success": False, "error": "目前設定是正式環境(非測試網)，要在正式環境送測試單請在確認框按確定(confirm_live=true)，避免誤觸真實下單"}

    direction = payload.get("direction")
    if direction not in ("bullish", "bearish"):
        return {"success": False, "error": "direction必須是bullish或bearish"}

    symbol = execution_module._resolve_symbol(payload.get("symbol"))
    book_ok, book = execution_module.get_book_ticker(symbol, account=account) if symbol else (False, None)
    bid, ask = (book["bid"], book["ask"]) if book_ok else (None, None)

    # 平倉直接問交易所目前模式，避免設定值跟實際不一致而平不掉
    hedge = execution_module.current_hedge_mode(
        account=account, default=bool(settings_module.get_settings().get("execution_hedge_mode", 1))
    )
    success, result = execution_module.close_position(direction, symbol=symbol, account=account, hedge=hedge)

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

    import threading as _th
    def _notify_close():
        try:
            notifier.notify_trade_event(
                action="close", label="手動測試", direction=direction, price=bid or ask or 0,
                exit_reason="手動測試平倉", pnl_points=0,
                executed=success, execution_error=None if success else result,
                account=account, slippage_note=slippage_note,
            )
        except Exception as e:
            logger.warning(f"手動測試平倉通知失敗: {e}")
    _th.Thread(target=_notify_close, daemon=True).start()

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
    block_market_closed: Optional[bool] = None,
    min_atr_points: Optional[float] = None,
    trend_filter_mode: Optional[int] = None,
    trend_interval_seconds: Optional[int] = None,
    trend_slow_multiplier: Optional[float] = None,
    strategy_type: Optional[str] = None,
    resonance_min_conditions: int = 4,
    target_step_count: Optional[int] = None,
    smc_touch_window: Optional[int] = None,
    smc_wt_level: Optional[float] = None,
    smc_confirm_bos: Optional[int] = None,
    smc_require_ema: Optional[int] = None,
    smc_exit_mode: Optional[int] = None,
    smc_min_rr: Optional[float] = None,
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
        block_market_closed=block_market_closed,
        min_atr_points=min_atr_points,
        trend_filter_mode=trend_filter_mode,
        trend_interval_seconds=trend_interval_seconds,
        trend_slow_multiplier=trend_slow_multiplier,
        strategy_type=strategy_type,
        resonance_min_conditions=resonance_min_conditions,
        target_step_count=target_step_count,
        smc_touch_window=smc_touch_window,
        smc_wt_level=smc_wt_level,
        smc_confirm_bos=smc_confirm_bos,
        smc_require_ema=smc_require_ema,
        smc_exit_mode=smc_exit_mode,
        smc_min_rr=smc_min_rr,
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
    block_market_closed: Optional[bool] = None,
    min_atr_points: Optional[float] = None,
    trend_filter_mode: Optional[int] = None,
    trend_interval_seconds: Optional[int] = None,
    trend_slow_multiplier: Optional[float] = None,
    strategy_type: Optional[str] = None,
    resonance_min_conditions: int = 4,
    symbol: str = "XAUUSDT",
    bucket_size: float = 1.0,
    target_step_count: Optional[int] = None,
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
    if block_market_closed is not None:
        baseline_overrides["paper_block_market_closed"] = 1 if block_market_closed else 0
    if min_atr_points is not None:
        baseline_overrides["paper_min_atr_points"] = min_atr_points
    if trend_filter_mode is not None:
        baseline_overrides["paper_trend_filter_mode"] = trend_filter_mode
    if trend_interval_seconds is not None:
        baseline_overrides["paper_trend_interval_seconds"] = trend_interval_seconds
    if trend_slow_multiplier is not None:
        baseline_overrides["paper_trend_slow_multiplier"] = trend_slow_multiplier

    job_id = sweep_module.start_sweep(
        days=days, interval_seconds=interval_seconds, baseline_overrides=baseline_overrides or None,
        strategy_type=strategy_type, resonance_min_conditions=resonance_min_conditions,
        symbol=symbol, bucket_size=bucket_size, target_step_count=target_step_count,
    )
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
    # 改從1分鐘K棒快取取樣(修正記錄見README)，歷史長度不受成交筆數限制
    candles = binance_streamer.get_recent_candles(interval_seconds=interval_seconds, limit=600)
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
    current_price = trades[-1]["price"] if trades else None
    return {
        "bucket_size": bucket_size,
        "trade_count": len(trades),
        "profile": profile,
        **poc_info,
        "interpretation": interpret_volume_profile(profile, poc_info, current_price, bucket_size=bucket_size),
    }


@app.get("/analysis/chan")
async def analysis_chan(interval_seconds: int = 300, trade_limit: int = 100000):
    """
    纏論分析：分型 -> 筆 -> 中樞 -> 背馳判斷。
    預設用5分鐘K線(interval_seconds=300)，之後要往下切1分鐘只要改參數即可，
    不用動到分析邏輯本身。
    """
    candles = binance_streamer.get_recent_candles(interval_seconds=interval_seconds, limit=600)
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
    # 主畫面訊號卡永遠附上大週期趨勢方向(用全域設定的週期/倍數)，就算濾網關閉也顯示，
    # 讓使用者先看到「如果開了濾網現在會怎麼判」再決定要不要開
    # 趨勢濾網的週期/倍數依「這個K線週期對應的纏論引擎」的專屬參數決定(15分K引擎改成4小時，
    # 主畫面切到15分K時卡片就跟著顯示4小時)，沒有對應引擎或沒覆寫時退回全域設定
    engine_id = f"chan_profile_{int(interval_seconds)}"
    s = settings_module.get_settings(engine_id=engine_id)
    result = compute_full_signal(
        interval_seconds=interval_seconds, bucket_size=bucket_size, trade_limit=trade_limit,
        trend_interval_seconds=int(s.get("paper_trend_interval_seconds", 3600) or 3600),
        trend_slow_multiplier=float(s.get("paper_trend_slow_multiplier", 3.0) or 3.0),
    )
    if result.get("trend_filter") is not None:
        result["trend_filter"]["filter_mode"] = int(s.get("paper_trend_filter_mode", 0) or 0)
        result["trend_filter"]["settings_engine_id"] = engine_id
    return result


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
