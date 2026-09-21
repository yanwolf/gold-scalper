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
