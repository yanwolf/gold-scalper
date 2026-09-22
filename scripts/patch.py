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


import json as _json
import os
PENDING = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".apply_pending.json")


def _fingerprints(changes):
    # 路徑＋新字串：改錨點(舊字串)重跑時新字串不變，才對得上；用舊字串就對不上(r65 pump-dump-hunter 踩過)
    return sorted({f"{c[0]}\n{c[2]}" for c in changes})


def _abort(changes, msg):
    """整批中止：記下這批的指紋，下一次只帶了一部分就擋下(r64/r65：中止後重跑整批)。"""
    with open(PENDING, "w", encoding="utf-8") as f:
        _json.dump(_fingerprints(changes), f)
    sys.exit(msg)


def _check_pending(changes):
    if not os.path.exists(PENDING):
        return
    with open(PENDING, encoding="utf-8") as f:
        prev = set(_json.load(f))
    now = set(_fingerprints(changes))
    missing = prev - now
    if missing:
        sys.exit(f"apply中止(一個檔都沒寫)：上一批中止的修改有 {len(missing)} 處不在這一批裡——中止後要重跑整批，"
                 f"不能只重跑一部分(用法第5點r64)。缺的：{[m.splitlines()[0] + ' … ' + m.splitlines()[1][:40] for m in sorted(missing)][:3]}；"
                 f"確定要放棄那些修改就刪掉 {PENDING}")


def apply(changes):
    """
    一次套用多處修改(可跨多個檔)：先全部在記憶體裡依序比對，每一處都要恰好命中count次，
    全部通過才一次寫入；任何一處對不上就中止，而且一個檔都不寫(BINANCE_LESSONS.md 第14條r25：
    逐一patch()時中途中止，前面的修改已經生效，重跑前得先確認哪些已改)。
    changes: [(path, old, new) 或 (path, old, new, count), ...]，同一個檔的多處修改依序套用。
    """
    _check_pending(changes)
    texts = {}
    for i, ch in enumerate(changes):
        path, old, new = ch[0], ch[1], ch[2]
        count = ch[3] if len(ch) > 3 else 1
        if new and old.endswith("\n") != new.endswith("\n"):  # new為空＝整段刪除，允許
            # 結尾換行不一致：替換後下一行會黏上來、或多出空行(r26那次的語法錯誤就是這樣來的)
            _abort(changes, f"apply中止(一個檔都沒寫)：第{i + 1}處 {path} 比對字串與替換字串的結尾換行不一致：{old[-40:]!r}")
        if path not in texts:
            with open(path, encoding="utf-8") as f:
                texts[path] = f.read()
        n = texts[path].count(old)
        # 裝飾器檢查(r41 gold-scalper踩到)：錨點緊接在 @裝飾器 後面、替換內容又多插入 def/class，
        # 裝飾器就套到新插入的那個函式上——語法合法、編譯檢查抓不到
        pos = texts[path].find(old)
        if pos > 0 and n >= 1:
            prev = texts[path][:pos].rstrip("\n").splitlines()[-1:] if texts[path][:pos].endswith("\n") else []
            added = (new.count("def ") + new.count("class ")) - (old.count("def ") + old.count("class "))
            if prev and prev[0].strip().startswith("@") and added > 0:
                _abort(changes, f"apply中止(一個檔都沒寫)：第{i + 1}處 {path} 錨點緊接在裝飾器 {prev[0].strip()} 後面，"
                       f"又插入新的 def/class——裝飾器會套到新函式上；錨點改選在裝飾器之前")
        if n != count:
            _abort(changes, f"apply中止(一個檔都沒寫)：第{i + 1}處 {path} 預期命中{count}次，實際{n}次：{old[:80]!r}")
        texts[path] = texts[path].replace(old, new)
    # 寫入前先把每個.py檔在記憶體裡編譯一次：全部命中不代表改完是合法程式(兩行黏在一起、
    # 字串串接少了+)，以前要等寫進去之後py_compile才抓到。語法錯就一個檔都不寫
    for path, text in texts.items():
        if path.endswith(".py"):
            try:
                compile(text, path, "exec")
            except SyntaxError as e:
                _abort(changes, f"apply中止(一個檔都沒寫)：改完的 {path} 有語法錯誤(第{e.lineno}行)：{e.msg}")
    for path, text in texts.items():
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    if os.path.exists(PENDING):
        os.remove(PENDING)   # 整批成功寫入：忘掉
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
        if os.path.exists(PENDING):
            os.remove(PENDING)   # 前面故意中止的案例留下的待重跑批次，不能擋到後面的案例
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
    with open(a, "w", encoding="utf-8") as f:
        f.write("    def f():\n        return 1\n    x = 2\n")
    r4 = exact(a, "def f():", 2) == "    def f():\n        return 1\n"
    p = os.path.join(d, "c.py")
    with open(p, "w", encoding="utf-8") as f:
        f.write("x = (1\n     + 2)\n")
    r5 = aborted([(p, "     + 2)\n", "     2)\n")]) and open(p, encoding="utf-8").read() == "x = (1\n     + 2)\n"
    os.remove(PENDING)
    q = os.path.join(d, "e.py")
    src = "class A:\n    @property\n    def a(self):\n        return 1\n"
    with open(q, "w", encoding="utf-8") as f:
        f.write(src)
    r6 = aborted([(q, "    def a(self):\n", "    def b(self):\n        return 0\n\n    def a(self):\n")]) and open(q, encoding="utf-8").read() == src
    os.remove(PENDING)
    apply([(q, "        return 1\n", "        return 2\n")])     # 改被裝飾函式的內容：不能被誤擋
    r7 = "return 2" in open(q, encoding="utf-8").read()
    # 中止後重跑整批(r64/r65)：第二處對不到 → 改錨點只重跑第二處要被擋；帶整批(新字串一樣)就放行；成功後忘掉
    reset()
    aborted([(a, "x\n", "X\n"), (b, "不存在\n", "Z\n")])
    r8 = aborted([(b, "y\n", "Z\n")]) and open(a, encoding="utf-8").read() == "x\ny\n"          # 只重跑一部分：擋下、一個檔都沒寫
    r9 = apply([(a, "x\n", "X\n"), (b, "y\n", "Z\n")]) == 2 and not os.path.exists(PENDING)   # 整批(改錨點、新字串不變)：放行、忘掉
    reset()
    r10 = apply([(a, "x\n", "Q\n")]) == 1   # 沒有待重跑的批次時，不受影響
    return {"裝飾器後面插入新函式時中止": r6, "改被裝飾函式的內容不誤擋": r7,
            "中止後只重跑一部分被擋下": r8, "中止後重跑整批放行並忘掉": r9, "沒有待重跑批次時正常": r10,
            "第二處對不到時第一個檔不動": r1, "結尾換行不一致時中止": r2, "正常時兩個檔都改到": r3, "exact讀出原文含縮排與換行": r4,
            "改完有語法錯誤時一個檔都不寫": r5}


def exact(path, first_line_contains, n_lines):
    """
    從檔案讀出「含first_line_contains的那一行起、連續n_lines行」的原文當比對字串(第14條r31)，
    不手抄縮排——手抄時最容易把縮排或結尾換行抄錯，apply就會對不上(或更糟：對到別的地方)。
    那一行必須恰好出現一次。
    """
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines(keepends=True)
    hits = [i for i, l in enumerate(lines) if first_line_contains in l]
    if len(hits) != 1:
        sys.exit(f"exact中止：{path} 含{first_line_contains!r}的行有{len(hits)}行，預期1行")
    return "".join(lines[hits[0]:hits[0] + n_lines])
