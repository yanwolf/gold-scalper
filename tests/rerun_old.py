"""
拿現在的測試跑舊版程式(BINANCE_LESSONS.md 用法第5點 r34/r36/r37)：python -m tests.rerun_old <舊版目錄>
把舊版複製到暫存目錄、換上現在的 tests/，逐項分成三類：
  斷言失敗        —— 舊版沒有這個修正，預期中的
  被測程式拋錯    —— 舊版程式本身在這個情境下會拋(例如缺值檢查不存在)
  框架/測試崩掉   —— traceback 最後一層在 tests/：框架或測試用到了舊版沒有的東西，要修框架(加前提或「有才重設」)
舊版怎麼取得：部署前在 git 打標籤(例如 lessons-r38)，要比對時 `git worktree add ../gs-r38 lessons-r38`；
或用存在專案裡的：`legacy:r38` ＝ 以現在的專案為底、把 app/ 換成 tests/legacy/r38/app(zip 沒有 .git，r40 的做法)。
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

RUNNER = r'''
import unittest, json
import tests.test_lessons as T
res = unittest.TestResult()
unittest.defaultTestLoader.loadTestsFromModule(T).run(res)
out = {"run": res.testsRun, "assert": [t._testMethodName for t, _ in res.failures], "program": [], "framework": []}
for t, tb in res.errors:
    last = [l for l in tb.splitlines() if 'File "' in l][-1]
    (out["framework"] if "tests/" in last else out["program"]).append(t._testMethodName + "：" + tb.strip().splitlines()[-1][:80])
print("RESULT" + json.dumps(out, ensure_ascii=False))
'''


def run(old_dir):
    tmp = tempfile.mkdtemp()
    dst = os.path.join(tmp, "old")
    if old_dir.startswith("legacy:"):
        # 以現在的專案為底(靜態檔、requirements一樣)，只把app/的.py換成存在tests/legacy/裡的舊版
        shutil.copytree(HERE, dst, ignore=shutil.ignore_patterns("__pycache__", ".git", "tests"))
        for f in os.listdir(os.path.join(HERE, "tests", "legacy", old_dir[7:], "app")):
            shutil.copy(os.path.join(HERE, "tests", "legacy", old_dir[7:], "app", f), os.path.join(dst, "app", f))
    else:
        shutil.copytree(old_dir, dst, ignore=shutil.ignore_patterns("__pycache__", ".git", "tests"))
    shutil.copytree(os.path.join(HERE, "tests"), os.path.join(dst, "tests"))
    r = subprocess.run([sys.executable, "-c", RUNNER], cwd=dst, capture_output=True, text=True)
    line = next((l for l in r.stdout.splitlines() if l.startswith("RESULT")), None)
    if line is None:
        return None, r.stderr[-400:]
    import json
    return json.loads(line[6:]), ""


def main():
    if len(sys.argv) < 2 or not (os.path.isdir(sys.argv[1]) or sys.argv[1].startswith("legacy:")):
        print("用法：python -m tests.rerun_old <舊版目錄 或 legacy:rNN>")
        return 2
    res, err = run(sys.argv[1])
    if res is None:
        print(f"測試沒跑完(框架整個崩掉)：{err}")
        return 1
    print(f"舊版 {sys.argv[1]}：共 {res['run']} 項｜斷言失敗 {len(res['assert'])}｜被測程式拋錯 {len(res['program'])}｜"
          f"框架/測試崩掉 {len(res['framework'])}")
    for k in ("program", "framework"):
        for x in res[k]:
            print(f"  [{'被測程式' if k == 'program' else '框架/測試'}] {x}")
    return 1 if res["framework"] else 0


if __name__ == "__main__":
    sys.exit(main())
