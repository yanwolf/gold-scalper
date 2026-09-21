"""
「先索引、沒先確認有東西」的靜態檢查(BINANCE_LESSONS.md 用法第5點 r34/r36/r37)。
突變或舊版程式讓清單是空的時，`x[0]`、`m.call_args[0]` 會讓測試本身崩掉——後面的斷言都沒檢查，失敗訊息看不出是哪個前提。

規則：測試方法裡對 X 取 [0]／[-1] 之前，同一個方法裡更早的一行要確認過 X 有東西：
  len(X)、X.called、X.call_count、assertTrue(X、one(X、pick(X  (X 的最左邊名稱也算，例如 pl.call_args 看 pl)
前提本身也算：「前提」那一行自己就索引、而更早沒確認，一樣報出來(r36：插入腳本自己犯過)。
排除(不會空、或不是讀取)：賦值目標、for 迴圈變數、行尾註明「# 固定長度」的來源(函式固定回傳 tuple)、
同一行的短路保護(`X and X[0]`)、從函式呼叫算出來的值(例如 `ast.parse(原文).body[0]`，不是測試累積的狀態)。
執行：python -m unittest tests.check_indexing -v
"""
import ast
import os
import unittest


def _root(node):
    while isinstance(node, (ast.Attribute, ast.Subscript, ast.Call)):
        node = node.value if not isinstance(node, ast.Call) else node.func
    return node.id if isinstance(node, ast.Name) else None


def problems(source):
    out = []
    tree = ast.parse(source)
    lines = source.splitlines()
    for cls in [n for n in tree.body if isinstance(n, ast.ClassDef)]:
        for fn in [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name.startswith("test")]:
            loop_vars, fixed, targets = set(), set(), set()
            for n in ast.walk(fn):
                if isinstance(n, (ast.For, ast.comprehension)):
                    loop_vars |= {x.id for x in ast.walk(n.target) if isinstance(x, ast.Name)}
                if isinstance(n, ast.Assign):
                    if "# 固定長度" in lines[n.lineno - 1]:
                        fixed |= {x.id for t in n.targets for x in ast.walk(t) if isinstance(x, ast.Name)}
                    targets |= {id(t) for t in n.targets}
            for n in ast.walk(fn):
                if not (isinstance(n, ast.Subscript) and isinstance(n.slice, (ast.Constant, ast.UnaryOp))):
                    continue
                idx = ast.literal_eval(n.slice) if isinstance(n.slice, (ast.Constant, ast.UnaryOp)) else None
                if idx not in (0, -1) or id(n) in targets or isinstance(n.value, ast.Call):
                    continue
                if isinstance(n.value, ast.Subscript):   # 巢狀取值：只看最內層那一次索引
                    continue
                x_src = ast.get_source_segment(source, n.value)
                root = _root(n.value)
                if root in loop_vars or root in fixed or x_src is None:
                    continue
                if any(isinstance(c, ast.Call) for c in ast.walk(n.value)):
                    continue  # 從函式呼叫算出來的值，不是測試累積的清單
                if f"{x_src} and {x_src}[" in lines[n.lineno - 1]:
                    continue  # 同一行的短路保護
                earlier = "\n".join(lines[fn.lineno - 1:n.lineno - 1])
                keys = [f"len({x_src})", f"{root}.called", f"{root}.call_count", f"assertTrue({x_src}",
                        f"one({x_src}", f"pick({x_src}"]
                if not any(k in earlier for k in keys):
                    out.append(f"{cls.name}.{fn.name} 第{n.lineno}行：{x_src}[{idx}] 之前沒確認 {x_src} 有東西")
    return out


SELFTEST = [  # (測試主體, 預期是否被報出)
    ("x[0]", True),
    ("self.assertEqual(len(x), 1)\n        x[0]", False),
    ("self.assertTrue(m.called)\n        m.call_args[0]", False),
    ("x[0] = 1", False),
    ("for a in xs:\n            a[0]", False),
    ("self.assertIn('p', x[0], '前提')", True),              # 前提本身就索引
    ("st = f()  # 固定長度\n        st[0]", False),
    ("self.assertEqual(x and x[-1], 1)", False),               # 同一行短路保護
    ("t = ast.parse(s).body[0]", False),                       # 從函式呼叫算出來的值
]


class TestIndexing(unittest.TestCase):
    def test_selftest_fixed_samples(self):
        for body, flagged in SELFTEST:
            src = f"class C:\n    def test_x(self):\n        {body}\n"
            self.assertEqual(bool(problems(src)), flagged, body)

    def test_lessons_tests_check_before_indexing(self):
        path = os.path.join(os.path.dirname(__file__), "test_lessons.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        self.assertGreater(src.count("def test_"), 50, "前提：真的掃到了測試")
        self.assertEqual(problems(src), [])


if __name__ == "__main__":
    unittest.main()
