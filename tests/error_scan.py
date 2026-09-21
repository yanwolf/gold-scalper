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
    with mock.patch.object(logging.Logger, "error", capture):
        with open("/dev/null", "w") as devnull:
            r = unittest.TextTestRunner(stream=devnull).run(unittest.defaultTestLoader.loadTestsFromModule(T))
    loggers = sorted({n for n, _ in errs})
    bad = [f"[{n}] {e}" for n, e in errs if any(k in e for k in PROGRAM_ERRORS) and not any(i in e for i in INJECTED)]
    print(f"全部 {r.testsRun} 項，失敗 {len(r.failures) + len(r.errors)}；錯誤紀錄 {len(errs)} 筆，來自 {loggers}；"
          f"非注入的程式錯誤：{bad or '無'}")
    # 前提(第19種)：攔截真的裝上了——至少攔到兩個不同模組寫的錯誤(測試裡有注入到網頁入口與引擎)
    if len(loggers) < 2:
        print("前提不成立：攔到的錯誤少於兩個模組，攔截可能沒裝上")
        return 1
    del real_error
    return 1 if (bad or not r.wasSuccessful()) else 0


if __name__ == "__main__":
    sys.exit(main())
