"""
全部情境跑一遍，攔下程式寫進錯誤紀錄的內容，確認只有測試注入的錯誤、沒有被try吞掉的程式錯誤(第14條)。
這是全域檢查，自成一次執行(用法第5點r25)，不放在任何情境的最後。執行：python -m tests.error_scan
"""
import sys
import unittest
from unittest import mock

import tests.test_lessons as T

PROGRAM_ERRORS = ("NameError", "AttributeError", "TypeError", "KeyError", "UnboundLocal", "IndexError", "ValueError")
INJECTED = ("inj-", "boom", "Read timed out", "-2019", "-2022", "-1001", "NO_BASELINE", "沒有未平倉", "Internal", "ReduceOnly")


def main():
    # 攔「所有」logger(第19種r27：攔截沒裝在某些地方，那些地方的錯誤從來沒檢查過)。
    # 第一版只攔paper_trading的logger，網頁入口(main)、下單層(execution)寫的錯誤都沒被掃到。
    import logging
    errs = []
    real_error = logging.Logger.error
    def capture(self, msg, *a, **k):
        errs.append((self.name, str(msg)))
    # 也攔sys.stderr(r31)：背景執行緒沒接住的例外、以及只print到stderr的錯誤都在這裡，logging攔不到
    import io
    import threading
    # 金絲雀(第19種)：先在「另一個」緩衝區裡故意讓背景執行緒丟例外，它的traceback必須攔得到——否則
    # 「stderr沒有traceback」分不出是真的沒有、還是攔截沒裝好。跟測試期間的輸出分開，才不會被當成程式錯誤
    canary_buf = io.StringIO()
    with mock.patch.object(sys, "stderr", canary_buf):
        th = threading.Thread(target=lambda: (_ for _ in ()).throw(RuntimeError("inj-canary-stderr")))
        th.start()
        th.join()
    canary_seen = "inj-canary-stderr" in canary_buf.getvalue()
    stderr_buf = io.StringIO()
    # r74：數有沒有測試真的開了背景補登執行緒(會在背景打網路、把未知補成已知)。只有驗證正式路徑那一項(目標函式有
    # _backfill_selftest 標記)可以。金絲雀：先確認計數真的裝上了
    real_start = threading.Thread.start
    bf = []
    current = {"test": "(金絲雀)"}   # r76：記下是哪一支測試開的，報出來才找得到
    def spy(t):
        if t.name == "fill-backfill" and not getattr(getattr(t, "_target", None), "_backfill_selftest", False):
            bf.append(current["test"])
        return real_start(t)
    class _Result(unittest.TextTestResult):
        def startTest(self, test):
            current["test"] = test.id()
            super().startTest(test)
    with mock.patch.object(threading.Thread, "start", spy):
        threading.Thread(target=lambda: None, name="fill-backfill").start()
    bf_canary = len(bf) == 1
    bf.clear()
    with mock.patch.object(logging.Logger, "error", capture), mock.patch.object(sys, "stderr", stderr_buf), \
            mock.patch.object(threading.Thread, "start", spy):
        with open("/dev/null", "w") as devnull:
            r = unittest.TextTestRunner(stream=devnull, resultclass=_Result).run(
                unittest.defaultTestLoader.loadTestsFromModule(T))
    tracebacks = [l for l in stderr_buf.getvalue().splitlines() if l.startswith("Traceback") or "Exception in thread" in l]
    errs.extend(("stderr", l) for l in tracebacks)
    loggers = sorted({n for n, _ in errs})
    bad = [f"[{n}] {e}" for n, e in errs
           if (n == "stderr" or any(k in e for k in PROGRAM_ERRORS)) and not any(i in e for i in INJECTED)]
    print(f"全部 {r.testsRun} 項，失敗 {len(r.failures) + len(r.errors)}；錯誤紀錄 {len(errs)} 筆，來自 {loggers}；"
          f"非注入的程式錯誤：{bad or '無'}")
    # 前提(第19種)：攔截真的裝上了——至少攔到兩個不同模組寫的錯誤(測試裡有注入到網頁入口與引擎)
    if len(loggers) < 2:
        print("前提不成立：攔到的錯誤少於兩個模組，攔截可能沒裝上")
        return 1
    if not canary_seen:
        print("前提不成立：背景執行緒的金絲雀traceback沒攔到，stderr攔截可能沒裝上")
        return 1
    if not bf_canary:
        print("前提不成立：背景補登執行緒的計數金絲雀沒數到，計數可能沒裝上")
        return 1
    if bf:
        by_test = {}
        for name in bf:
            by_test[name] = by_test.get(name, 0) + 1
        print(f"有測試真的開了背景補登執行緒(框架預設應該收下來、不真的開，r74)：{by_test}")
        return 1
    print(f"stderr攔截：金絲雀有攔到；測試期間的traceback {len(tracebacks)} 行；真的開的背景補登執行緒 0 個")
    del real_error
    return 1 if (bad or not r.wasSuccessful()) else 0


if __name__ == "__main__":
    sys.exit(main())
