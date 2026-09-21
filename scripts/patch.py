"""
改程式用的取代函式(BINANCE_LESSONS.md第14條，r15)：必須恰好命中count次，否則中止。
str.replace比對不到時什麼都不做、也不報錯；多命中時會把所有出現處都換掉。
"""
import sys


def patch(path, old, new, count=1):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    n = text.count(old)
    if n != count:
        sys.exit(f"patch中止：{path} 預期命中{count}次，實際{n}次：{old[:80]!r}")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text.replace(old, new))


def apply(changes):
    """
    一次套用多處修改(可跨多個檔)：先全部在記憶體裡依序比對，每一處都要恰好命中count次，
    全部通過才一次寫入；任何一處對不上就中止，而且一個檔都不寫(BINANCE_LESSONS.md 第14條r25：
    逐一patch()時中途中止，前面的修改已經生效，重跑前得先確認哪些已改)。
    changes: [(path, old, new) 或 (path, old, new, count), ...]，同一個檔的多處修改依序套用。
    """
    texts = {}
    for i, ch in enumerate(changes):
        path, old, new = ch[0], ch[1], ch[2]
        count = ch[3] if len(ch) > 3 else 1
        if old.endswith("\n") != new.endswith("\n"):
            # 結尾換行不一致：替換後下一行會黏上來、或多出空行(r26那次的語法錯誤就是這樣來的)
            sys.exit(f"apply中止(一個檔都沒寫)：第{i + 1}處 {path} 比對字串與替換字串的結尾換行不一致：{old[-40:]!r}")
        if path not in texts:
            with open(path, encoding="utf-8") as f:
                texts[path] = f.read()
        n = texts[path].count(old)
        if n != count:
            sys.exit(f"apply中止(一個檔都沒寫)：第{i + 1}處 {path} 預期命中{count}次，實際{n}次：{old[:80]!r}")
        texts[path] = texts[path].replace(old, new)
    for path, text in texts.items():
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    return len(changes)


def selftest():
    """改寫工具自我驗證：第二處比對不到時第一個檔不被動到；結尾換行不一致時中止；正常時兩個檔都改到。"""
    import os
    import tempfile
    d = tempfile.mkdtemp()
    a, b = os.path.join(d, "a.txt"), os.path.join(d, "b.txt")
    def reset():
        for p in (a, b):
            with open(p, "w", encoding="utf-8") as f:
                f.write("x\ny\n")
    def aborted(changes):
        try:
            apply(changes)
            return False
        except SystemExit:
            return True
    reset()
    r1 = aborted([(a, "x\n", "X\n"), (b, "不存在\n", "Z\n")]) and open(a, encoding="utf-8").read() == "x\ny\n"
    reset()
    r2 = aborted([(a, "x\n", "X")]) and open(a, encoding="utf-8").read() == "x\ny\n"
    reset()
    apply([(a, "x\n", "X\n"), (b, "y\n", "Y\n")])
    r3 = open(a, encoding="utf-8").read() == "X\ny\n" and open(b, encoding="utf-8").read() == "x\nY\n"
    return {"第二處對不到時第一個檔不動": r1, "結尾換行不一致時中止": r2, "正常時兩個檔都改到": r3}
