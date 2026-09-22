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
    # 第25種(r44)：assertTrue(all(...)) 對空清單成立——程式什麼都沒做、清單是空的也會通過，算否定句
    if name == "assertTrue" and call.args and isinstance(call.args[0], ast.Call) and \
            isinstance(call.args[0].func, ast.Name) and call.args[0].func.id == "all":
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


# 第20種(r63，四次了)：ExecHarness 在 setUp 把這些 mock 掉，放在它底下的測試碰不到真的實作
HARNESS_MOCKED = ("notify_trade_event(", "_send_telegram_message", "_sqlite_pool(")


def harness_problems(source):
    """繼承 ExecHarness 的測試類別裡，不能呼叫它 mock 掉的東西(通知格式、真的 SQL)，要放在不 mock 的情境。"""
    out = []
    for cls in ast.parse(source).body:
        if not isinstance(cls, ast.ClassDef):
            continue
        if not any(isinstance(b, ast.Name) and b.id == "ExecHarness" for b in cls.bases):
            continue
        for fn in cls.body:
            if isinstance(fn, ast.FunctionDef):
                seg = ast.get_source_segment(source, fn) or ""
                for k in HARNESS_MOCKED:
                    if k in seg:
                        out.append(f"{cls.name}.{fn.name}：在 ExecHarness 底下用到 {k.rstrip('(')}(它被框架 mock 掉了，第20種)")
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
    ("self.assertTrue(all(x > 0 for x in xs))", True),       # 第25種：空清單時也成立
    ("self.assertEqual(len(xs), 2)\n        self.assertTrue(all(x > 0 for x in xs))", False),
]


class TestChecker(unittest.TestCase):
    def test_selftest_fixed_samples(self):
        for body, flagged in SELFTEST:
            src = f"class C:\n    def test_x(self):\n        {body}\n"
            self.assertEqual(bool(problems(src)), flagged, body)

    def test_harness_selftest(self):
        bad = "class ExecHarness: pass\nclass A(ExecHarness):\n    def test_x(self):\n        N.notify_trade_event(action=1)\n"
        ok = "class ExecHarness: pass\nclass B(unittest.TestCase):\n    def test_x(self):\n        N.notify_trade_event(action=1)\n"
        ok2 = "class ExecHarness: pass\nclass A(ExecHarness):\n    def test_x(self):\n        self.notes[-1]\n"
        self.assertEqual(len(harness_problems(bad)), 1, "ExecHarness 底下呼叫通知格式：要報出")
        self.assertEqual(harness_problems(ok), [], "不 mock 的情境：可以")
        self.assertEqual(harness_problems(ok2), [], "只讀框架收集到的通知參數：可以")

    def test_no_harness_mocked_calls_in_harness_tests(self):
        path = os.path.join(os.path.dirname(__file__), "test_lessons.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        self.assertGreater(src.count("(ExecHarness)"), 10, "前提：真的掃到了繼承 ExecHarness 的類別")
        self.assertEqual(harness_problems(src), [])

    def test_lessons_tests_all_have_a_positive_assertion(self):
        path = os.path.join(os.path.dirname(__file__), "test_lessons.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        self.assertGreater(len(scan(src)), 50, "前提：真的掃到了測試")
        self.assertEqual(problems(src), [])


if __name__ == "__main__":
    unittest.main()
