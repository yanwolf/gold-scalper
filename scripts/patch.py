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
