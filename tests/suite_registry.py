"""
測試檔登記表(BINANCE_LESSONS.md r101)：「逐檔」的檢查各自掃哪些測試檔。

以前突變檢查、錯誤掃描、舊版重跑、新介面檢查都寫死只看 tests/test_lessons.py，r95 之後新增的測試檔一個都沒進去，
也沒有任何東西會提醒(r100 對照否定句檢查時才發現)。現在分兩種：
  - **自動掃全部 tests/test_*.py，不用登記**：行為測試(scripts/verify.py 第 2 步)、否定句檢查(check_tests)、
    索引前先確認(check_indexing)。
  - **要登記**：下面四種檢查不是每個測試檔都適用(例如舊版重跑：新功能的測試在舊版上本來就沒有那個功能)，
    每個測試檔都要在 REGISTRY 列出參加哪幾種、為什麼——**沒登記的測試檔由 tests/check_tests 直接報**，不會悄悄漏掉；
    登記了但檔案已經不在也報(過期)。
"""
import glob
import os

CHECKS = ("newapi", "mutation", "rerun_old", "error_scan")
CHECK_NAMES = {
    "newapi": "新介面前提(tests/check_newapi)",
    "mutation": "逐項突變(tests/mutation_check)",
    "rerun_old": "舊版重跑(tests/rerun_old)",
    "error_scan": "錯誤掃描(tests/error_scan)",
}

_ENGINE = "碰到即時引擎的平倉／下單路徑(ExecHarness)：要過突變檢查(部位查詢查不到時不能空跑)與錯誤掃描(沒有被吞掉的程式錯誤)"
_BACKTEST = "回測／策略模組，不碰交易所下單路徑(沒有呼叫被突變的部位查詢、也不經過引擎的 try 包裝)；新功能的測試，舊版沒有那個功能，不重跑舊版"

# {測試檔: (參加的檢查, 理由)}
REGISTRY = {
    "test_lessons.py": (set(CHECKS), "幣安踩坑清單的回歸測試：每一條都要在舊版上跑得起來、不能空跑、沒有吞掉的錯誤"),
    "test_close_reject.py": ({"mutation", "error_scan"},
                             _ENGINE + "。第16條r99 的新功能(退讓、停止追蹤)，舊版沒有，不重跑舊版"),
    "test_fees.py": ({"mutation", "error_scan"}, _ENGINE + "。r103 的新功能(手續費紀錄)，不重跑舊版"),
    "test_band_touch_engine.py": ({"mutation", "error_scan"},
                                  "前瞻引擎(純模擬)走即時引擎的 _tick：突變檢查與錯誤掃描都適用；r102 新功能，不重跑舊版"),
    "test_klines_rate_limit.py": ({"error_scan"},
                                  "IP 權重與K線限流(binance_weight)，含回測任務與自檢；不經過部位查詢，錯誤掃描適用；不重跑舊版"),
    "test_backtest_jobs.py": (set(), _BACKTEST),
    "test_daily_smc.py": (set(), _BACKTEST),
    "test_mean_reversion.py": (set(), _BACKTEST),
    "test_pullback_resume.py": (set(), _BACKTEST),
    "test_breakout_trend.py": (set(), _BACKTEST),
}

HERE = os.path.dirname(os.path.abspath(__file__))


def test_files():
    """tests/ 底下實際存在的測試檔(只看檔名 test_*.py)。"""
    return sorted(os.path.basename(p) for p in glob.glob(os.path.join(HERE, "test_*.py")))


def modules_for(check):
    """參加某一種檢查的測試模組名稱(例如 'tests.test_lessons')，test_lessons 一定排第一。"""
    assert check in CHECKS, check
    names = [f for f, (checks, _) in REGISTRY.items() if check in checks and os.path.exists(os.path.join(HERE, f))]
    names.sort(key=lambda f: (f != "test_lessons.py", f))
    return ["tests." + f[:-3] for f in names]


def registration_problems(files=None, registry=None):
    """沒登記的測試檔、登記了但不存在的(過期)、理由空白的、登記了不認得的檢查名稱。"""
    files = test_files() if files is None else files
    registry = REGISTRY if registry is None else registry
    out = []
    for f in files:
        if f not in registry:
            out.append(f"沒登記：{f}(要在 tests/suite_registry.py 寫明參加哪些逐檔檢查、為什麼)")
    for f, (checks, why) in registry.items():
        if f not in files:
            out.append(f"過期登記：{f} 已經不存在")
        if not str(why or "").strip():
            out.append(f"沒寫理由：{f}")
        bad = set(checks) - set(CHECKS)
        if bad:
            out.append(f"不認得的檢查：{f} {sorted(bad)}")
    return out
