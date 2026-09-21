"""
測試的靜態檢查(BINANCE_LESSONS.md 用法第5點 r19/r21/r24)：看斷言本身(不看描述)判斷否定句。
規則：每個測試至少要有一條「正向」斷言——程式什麼都沒做時會失敗的那種。只有否定句斷言的測試，
程式什麼都沒做也會通過(空跑)。前提斷言本身就是正向的(assertTrue(gp.called)、assertEqual(status, "gone"))。

配合本專案的 unittest 寫法(r24：直接套別人的規則會判反)：
  否定句：assertFalse、assertNotIn、assertIsNone、assertIsNot、assertNotEqual、
          assertEqual/assertIs 第二個參數是 0 / [] / {} / () / "" / None
  正向：其他全部(assertTrue、assertIn、assertEqual(x, 非空值)、assertIs(x, True/False)、assertRaises…)
  assertIs(x, False) 算正向：程式什麼都沒做時值通常是 None，不會剛好是 False。
這個檢查自成一個情境(r25)，並用固定人造資料自我驗證。執行：python -m unittest tests.check_tests -v
"""
import ast
import os
import unittest

NEG_FUNCS = {"assertFalse", "assertNotIn", "assertIsNone", "assertIsNot", "assertNotEqual"}
EMPTY = (0, 0.0, "", None)


def _is_empty_literal(node):
    if isinstance(node, ast.Constant) and node.value in EMPTY and node.value is not False:
        return True
    return isinstance(node, (ast.List, ast.Dict, ast.Tuple, ast.Set)) and not (
        getattr(node, "elts", None) or getattr(node, "keys", None))


def classify(call):
    name = call.func.attr
    if name in NEG_FUNCS:
        return "neg"
    if name in ("assertEqual", "assertIs") and len(call.args) >= 2 and _is_empty_literal(call.args[1]):
        return "neg"
    return "pos"


def scan(source):
    """回傳 {測試名: (正向數, 否定數)}"""
    out = {}
    for cls in ast.parse(source).body:
        if not isinstance(cls, ast.ClassDef):
            continue
        for fn in cls.body:
            if isinstance(fn, ast.FunctionDef) and fn.name.startswith("test"):
                pos = neg = 0
                for n in ast.walk(fn):
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr.startswith("assert"):
                        if classify(n) == "pos":
                            pos += 1
                        else:
                            neg += 1
                out[f"{cls.name}.{fn.name}"] = (pos, neg)
    return out


def problems(source):
    return [f"{k}：正向 {p}、否定句 {n} —— 沒有正向斷言，程式什麼都沒做也會通過"
            for k, (p, n) in scan(source).items() if p == 0]


SELFTEST = [  # (測試主體, 預期是否被報出)
    ("self.assertFalse(x)", True),
    ("self.assertFalse(x); self.assertTrue(y)", False),
    ("self.assertEqual(x, 0)", True),
    ("self.assertEqual(x, 5)", False),
    ("self.assertIsNone(x)", True),
    ("self.assertIs(x, False)", False),
    ("with m:\n            self.assertTrue(x)", False),
    ("pass", True),
    ("self.assertEqual(c, []); self.assertNotIn(a, b)", True),
    ("with self.assertRaises(E):\n            f()", False),
]


class TestChecker(unittest.TestCase):
    def test_selftest_fixed_samples(self):
        for body, flagged in SELFTEST:
            src = f"class C:\n    def test_x(self):\n        {body}\n"
            self.assertEqual(bool(problems(src)), flagged, body)

    def test_lessons_tests_all_have_a_positive_assertion(self):
        path = os.path.join(os.path.dirname(__file__), "test_lessons.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        self.assertGreater(len(scan(src)), 50, "前提：真的掃到了測試")
        self.assertEqual(problems(src), [])


if __name__ == "__main__":
    unittest.main()
