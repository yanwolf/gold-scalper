"""
完整驗證一個指令跑完(BINANCE_LESSONS.md 用法第5點 r28)：python scripts/verify.py
每一步都要真的執行、而且有前提(第19種)；任何一步沒執行或失敗都不會印「全部通過」。
"""
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
results = []


def step(name, ok, detail=""):
    results.append((name, ok))
    print(f"{'✅' if ok else '❌'} {name}{('：' + detail) if detail else ''}")


def run(args):
    return subprocess.run(args, cwd=ROOT, capture_output=True, text=True)


# 1. pyflakes：沒裝就失敗；先掃金絲雀；app、tests、scripts全掃；import * 算失敗
r = run([PY, "-m", "pyflakes", "--version"])
if r.returncode != 0:
    step("pyflakes", False, "沒有安裝")
else:
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write("def f():\n    return undefined_canary_name\n")
    canary = run([PY, "-m", "pyflakes", f.name]).stdout
    out = run([PY, "-m", "pyflakes", "app", "tests", "scripts"]).stdout
    bad = [l for l in out.splitlines() if ("undefined name" in l and "'fastapi'" not in l) or "unable to detect" in l]
    step("pyflakes", "undefined_canary_name" in canary and "undefined name 'fastapi'" in out and not bad,
         f"金絲雀{'有' if 'undefined_canary_name' in canary else '沒有'}報出、掃到main.py已知誤報、問題 {len(bad)} 項 {bad[:3]}")

# 2. 行為測試＋測試靜態檢查：要全部通過、數量達標
r = run([PY, "-m", "unittest", "tests.test_lessons", "tests.check_tests", "tests.check_indexing"])
ran = next((int(l.split()[1]) for l in r.stderr.splitlines() if l.startswith("Ran ")), 0)
step("行為測試＋靜態檢查", r.returncode == 0 and ran >= 60, f"執行 {ran} 項，{'全部通過' if r.returncode == 0 else r.stderr.strip().splitlines()[-1]}")

# 3. 改寫工具自我驗證
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import patch  # noqa: E402
st = patch.selftest()
step("改寫工具自我驗證", all(st.values()), "、".join(f"{k}{'OK' if v else '失敗'}" for k, v in st.items()))

# 4. 逐項突變檢查(含檢查器自我驗證)
r = run([PY, "-m", "tests.mutation_check"])
summary = next((l for l in r.stdout.splitlines() if l.startswith("全部")), "")
step("逐項突變檢查", r.returncode == 0 and bool(summary), summary)

# 5. 全部情境沒有被吞掉的程式錯誤(攔所有logger)
r = run([PY, "-m", "tests.error_scan"])
step("錯誤掃描", r.returncode == 0, (r.stdout.strip().splitlines() or ["(沒有輸出)"])[-1][:160])

# 6. 在舊版程式上重跑：tests/legacy/ 下每一版都跑，另外 GS_PREV=<目錄> 可加一版。框架/測試崩掉要是0(用法第5點r34/r37/r40)
olds = [f"legacy:{v}" for v in sorted(os.listdir(os.path.join(ROOT, "tests", "legacy")))] if os.path.isdir(os.path.join(ROOT, "tests", "legacy")) else []
if os.environ.get("GS_PREV"):
    olds.append(os.environ["GS_PREV"])
for old in olds:
    r = run([PY, "-m", "tests.rerun_old", old])
    step(f"在舊版程式上重跑({old})", r.returncode == 0, (r.stdout.strip().splitlines() or ["(沒有輸出)"])[0][:160])

ok = all(o for _, o in results) and len(results) == 5 + len(olds) and len(olds) >= 1
print("\n" + ("全部通過" if ok else f"有 {sum(1 for _, o in results if not o)} 步失敗"))
sys.exit(0 if ok else 1)
