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
    errs = []
    with mock.patch.object(T.pt.logger, "error", side_effect=lambda m, *a: errs.append(str(m))):
        r = unittest.TextTestRunner(stream=open("/dev/null", "w")).run(unittest.defaultTestLoader.loadTestsFromModule(T))
    bad = [e for e in errs if any(k in e for k in PROGRAM_ERRORS) and not any(i in e for i in INJECTED)]
    print(f"全部 {r.testsRun} 項，失敗 {len(r.failures) + len(r.errors)}；錯誤紀錄 {len(errs)} 筆，非注入的程式錯誤：{bad or '無'}")
    return 1 if (bad or not r.wasSuccessful()) else 0


if __name__ == "__main__":
    sys.exit(main())
