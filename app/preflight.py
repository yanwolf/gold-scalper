"""交易所相容性自檢（gold-scalper 版）。

用途：幣安改 API 時，不要等到真的下單才發現。開機跑一次，網頁「/preflight」
也可以手動重跑。每一項都對應 BINANCE_LESSONS.md 的一條，那份清單在
crypto-screener / gold-scalper / pump-dump-hunter 三個專案內容相同，
發現新坑就三份一起更新（見清單最後一節「怎麼用這份清單」）。

跟 pump-dump-hunter 那份的差異：這裡的下單層 app/execution.py 是
「回傳 (success, data_or_error)、不丟例外」的介面，而且是多帳戶設計
(account="gold" / "gold_1m" / ...)，所以這裡改成：
- 呼叫 execution.py 現成的函式，不直接碰 requests
- 用 accounts_to_check() 找出「目前真的有引擎在用」的帳戶，逐一檢查，
  而不是只檢查一個寫死的帳戶
"""
import time
from app import execution as execution_module, settings as settings_module

VERSION = "2026-09-22r52"  # 三個專案共用；複製過去時連同這行一起帶


def accounts_to_check():
    """
    找出目前有引擎綁定真實下單的帳戶清單(去重)。避免每次都檢查一個寫死的
    帳戶名稱——真正在用的帳戶才需要驗證金鑰/槓桿/餘額，沒在用的帳戶查了
    也只會看到「未設定API金鑰」，沒有意義。
    """
    from app.paper_trading import PAPER_TRADING_ENGINES
    accounts = []
    for engine in PAPER_TRADING_ENGINES.values():
        acc = getattr(engine, "execution_account", None)
        if acc and acc not in accounts:
            accounts.append(acc)
    return accounts or [execution_module.DEFAULT_ACCOUNT]


def notify_check():
    """推播狀態(第8條r40)：沒設定→warn；最近有送出失敗→fail，列出最後一筆。"""
    from app.notifier import notifier
    if not notifier.is_enabled:
        return {"item": "Telegram 推播", "status": "warn", "msg": "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 沒有設定，所有告警都不會送出"}
    fails = [e for e in notifier.errors if e["kind"] == "送出失敗"]
    if fails:
        return {"item": "Telegram 推播", "status": "fail", "msg": f"最近 {len(fails)} 次送出失敗，最後一次：{fails[-1]['msg']}"}
    return {"item": "Telegram 推播", "status": "ok", "msg": "已設定，最近沒有送出失敗"}


def check(account=None, symbol=None):
    """
    對「一個帳戶」跑完整自檢，回傳[(項目, 狀態, 說明), ...]。
    狀態：ok / warn / fail。這支函式本身不會下真的會成交的市價單，
    只查詢公開資料、帳戶資訊、精度、槓桿上限——完全唯讀。
    """
    account = account or execution_module.DEFAULT_ACCOUNT
    symbol = symbol or execution_module.DEFAULT_SYMBOL
    out = [notify_check()]

    def add(name, st, msg=""):
        out.append({"item": name, "status": st, "msg": str(msg)[:200]})
        return st

    # 1. 公開資料與精度過濾器（stepSize / tickSize 拿不到就會被 -1111 拒絕，
    #    見 BINANCE_LESSONS.md 第4條）
    try:
        f = execution_module.get_symbol_filters(symbol, account=account)
        ok = bool(f.get("step_size") and f.get("tick_size"))
        add("精度過濾器", "ok" if ok else "fail",
            f"step={f.get('step_size')} tick={f.get('tick_size')} minNotional={f.get('min_notional')}")
    except Exception as e:
        add("精度過濾器", "fail", e)

    # 2. API 金鑰有沒有設定（沒有的話以下需要簽章的項目全部略過，不算異常）
    api_key, api_secret = execution_module._get_credentials(account)
    if not api_key or not api_secret:
        add("API 金鑰", "warn", f"帳戶「{account}」未設定，以下需要簽章的項目略過")
        return out
    add("API 金鑰", "ok", f"帳戶「{account}」，{'測試網' if execution_module.use_testnet(account) else '正式網'}")

    # 3. 持倉模式（單向/雙向決定停損單要帶 reduceOnly 還是 positionSide，
    #    見 BINANCE_LESSONS.md 第8條）
    try:
        hedge = execution_module.current_hedge_mode(account=account)
        add("持倉模式", "ok", "雙向 hedge" if hedge else "單向 one-way")
    except Exception as e:
        add("持倉模式", "fail", e)

    # 4. 條件單端點：backstop停損單走這裡掛(BINANCE_LESSONS.md第1條)。用一個
    #    不存在的algoId查詢狀態，只是為了確認端點本身可以打通——預期會拿到
    #    「查無此單」，不是404(端點不存在)。這不會下真的會成交的單。
    try:
        ok, data = execution_module.get_algo_stop_status("999999999999", symbol=symbol, account=account, used_legacy=False)
        msg = str(data.get("msg", data) if isinstance(data, dict) else data)
        endpoint_reachable = ok or "does not exist" in msg or "Unknown order" in msg or "-2013" in msg
        add("條件單端點(backstop)", "ok" if endpoint_reachable else "fail", msg)
    except Exception as e:
        add("條件單端點(backstop)", "fail", e)

    # 5. 帳戶餘額（順便確認金鑰有沒有接對、測試網/正式環境有沒有搞錯）
    try:
        line = execution_module.usdt_balance_line(account=account)
        add("錢包餘額", "ok" if line else "fail", line or "查無USDT資產或查詢失敗")
    except Exception as e:
        add("錢包餘額", "fail", e)

    # 6. 槓桿上限（新子帳戶常被限 5x，見 BINANCE_LESSONS.md 第5條）
    try:
        mx = execution_module.max_leverage(symbol, account=account)
        s = settings_module.get_settings()
        want = int(s.get("execution_leverage", 10) or 10)
        if mx is None:
            add("槓桿上限", "warn", "查不到，無法確認是否足夠")
        else:
            add("槓桿上限", "ok" if mx >= want else "warn", f"上限 {mx}x，設定要用 {want}x")
    except Exception as e:
        add("槓桿上限", "warn", e)

    # 7. 孤兒條件單(BINANCE_LESSONS.md第13條)：交易所上還掛著、但沒有對應部位的條件單。
    #    本專案唯一會掛的條件單是backstop，程式平倉時會撤；這裡是最後一道檢查。
    #    不帶symbol查全部掛單權重高(第6條)，所以只在自檢時查、而且/preflight有冷卻。
    try:
        ok_o, orders = execution_module.get_open_algo_orders(account=account)
        ok_p, positions = _signed_positions(account)
        if not ok_o or not ok_p:
            add("孤兒條件單", "warn", "查詢失敗，無法判斷(查不到≠沒有，第2條)")
        else:
            # 全量表裡找不到某個幣(包括整張空清單)不一定是「沒有」(第2條r15/r18)：
            # 這些幣逐幣再查一次，查不到(失敗或沒有該幣的列)就不下結論
            listed = {p.get("symbol") for p in positions or []}
            missing = {o.get("symbol") for o in orders or [] if o.get("symbol") and o.get("symbol") not in listed}
            positions = list(positions or [])
            for sym in missing:
                ok_s, rows = execution_module.get_position_info(sym, account=account)
                if not ok_s or not any(r.get("symbol") == sym for r in rows or []):
                    positions = None
                    break
                positions.extend(rows)
            if positions is None:
                add("孤兒條件單", "warn", "全量部位表回空清單、逐幣查詢也失敗，無法判斷")
                return out
            held = {(p.get("symbol"), p.get("positionSide", "BOTH")) for p in positions
                    if abs(float(p.get("positionAmt", 0) or 0)) > 0}
            held_symbols = {sym for sym, _ in held}
            orphans = []
            for o in orders or []:
                sym, side = o.get("symbol"), o.get("positionSide", "BOTH")
                if sym not in held_symbols or (side != "BOTH" and (sym, side) not in held):
                    orphans.append(f"{sym} {o.get('side')} algoId={o.get('algoId')}")
            add("孤兒條件單", "ok" if not orphans else "fail",
                "無" if not orphans else f"{len(orphans)} 張沒有對應部位：" + "；".join(orphans[:5]))
    except Exception as e:
        add("孤兒條件單", "warn", e)

    # 8. 速率限制：positionRisk 權重高，打太兇會被限流（見 BINANCE_LESSONS.md 第6條）
    try:
        t0 = time.time()
        ok, _ = execution_module.get_position_info(symbol, account=account)
        add("查持倉", "ok" if ok else "fail", f"{(time.time() - t0) * 1000:.0f} ms")
    except Exception as e:
        add("查持倉", "fail", f"{e}（若為418/429=被限流，檢查輪詢頻率）")

    return out


def _signed_positions(account):
    """查帳戶全部持倉(不帶symbol)，給孤兒條件單比對用。回傳(success, list)。"""
    return execution_module._signed_request("GET", "/fapi/v2/positionRisk", {}, account=account)


def check_all():
    """對「目前有在用」的每個帳戶各跑一次check()，回傳{account: [結果...]}。"""
    return {account: check(account=account) for account in accounts_to_check()}


# 手動重跑的冷卻(BINANCE_LESSONS.md第6條延伸)：/preflight不需要登入，而且會打
# positionRisk、全帳戶掛單這類高權重查詢，不能讓它被反覆觸發到被限流
COOLDOWN_SECONDS = 60
_last_result = None
_last_run_at = 0.0


def run_and_report(send=True, force=False):
    """
    force=False時，冷卻時間內直接回傳上一次的結果(標記cached)，不重打交易所。
    開機那一次用force=True。
    """
    global _last_result, _last_run_at
    now = time.time()
    if not force and _last_result is not None and now - _last_run_at < COOLDOWN_SECONDS:
        return {**_last_result, "cached": True, "next_run_in_seconds": int(COOLDOWN_SECONDS - (now - _last_run_at))}
    _last_run_at = now
    _last_result = _run_and_report(send)
    return _last_result


def _run_and_report(send=True):
    """
    開機跑一次、網頁也能手動重跑。有異常(非ok)就發Telegram列出來，
    全部正常只發一行簡短確認(或send=False時完全不發，只回傳結果給API用)。
    """
    results = check_all()
    all_flat = [(account, r) for account, rs in results.items() for r in rs]
    bad = [(account, r) for account, r in all_flat if r["status"] != "ok"]

    if send:
        from app.notifier import notifier
        if bad:
            lines = "\n".join(
                f"{'❌' if r['status'] == 'fail' else '⚠️'} [{account}] {r['item']}：{r['msg']}"
                for account, r in bad
            )
            notifier.send_raw_message(f"🔎 交易所自檢（{VERSION}）有 {len(bad)} 項異常\n{lines}")
        else:
            notifier.send_raw_message(f"🔎 交易所自檢（{VERSION}）通過，共 {len(all_flat)} 項，帳戶：{', '.join(results.keys())}")

    return {"version": VERSION, "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"), "accounts": results}
