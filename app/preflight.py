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

VERSION = "2026-09-21"  # 三個專案共用；複製過去時連同這行一起帶


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


def check(account=None, symbol=None):
    """
    對「一個帳戶」跑完整自檢，回傳[(項目, 狀態, 說明), ...]。
    狀態：ok / warn / fail。這支函式本身不會下真的會成交的市價單，
    只查詢公開資料、帳戶資訊、精度、槓桿上限——完全唯讀。
    """
    account = account or execution_module.DEFAULT_ACCOUNT
    symbol = symbol or execution_module.DEFAULT_SYMBOL
    out = []

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

    # 7. 速率限制：positionRisk 權重高，打太兇會被限流（見 BINANCE_LESSONS.md 第6條）
    try:
        t0 = time.time()
        ok, _ = execution_module.get_position_info(symbol, account=account)
        add("查持倉", "ok" if ok else "fail", f"{(time.time() - t0) * 1000:.0f} ms")
    except Exception as e:
        add("查持倉", "fail", f"{e}（若為418/429=被限流，檢查輪詢頻率）")

    return out


def check_all():
    """對「目前有在用」的每個帳戶各跑一次check()，回傳{account: [結果...]}。"""
    return {account: check(account=account) for account in accounts_to_check()}


def run_and_report(send=True):
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
