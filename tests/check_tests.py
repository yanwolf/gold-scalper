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


RESTORERS = ("_engine", "_reset_module_state", "_restore_namespaces")


def _is_patch_ctx(expr):
    """with 的項目是 mock.patch(...)／mock.patch.object(...)／patch.dict(...) 之類。"""
    f = expr.func if isinstance(expr, ast.Call) else None
    while isinstance(f, ast.Attribute):
        if f.attr == "patch":
            return True
        f = f.value
    return isinstance(f, ast.Name) and f.id == "patch"


def restore_order_problems(source):
    """
    結構性還原(r80)要排在裝模擬之前：_engine()／_reset_module_state() 不能在模擬已經生效時呼叫——
    在 with mock.patch… 裡面、同一個函式裡 .start() 之後、或 setUp(含同檔的父類別)已經 start() 了模擬的類別的測試方法裡。
    (執行期另外有保險：還原會跳過正在生效的 patch；這裡是把寫錯的順序直接擋下來。)
    """
    tree = ast.parse(source)
    par = {c: p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)}
    classes = {c.name: c for c in tree.body if isinstance(c, ast.ClassDef)}

    def starts(fn):
        return [n.lineno for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "start" and not n.args]

    def setup_starts(cls, seen=()):
        if cls is None or cls.name in seen:
            return False
        su = next((f for f in cls.body if isinstance(f, ast.FunctionDef) and f.name == "setUp"), None)
        if su is not None and starts(su):
            return True
        return any(setup_starts(classes.get(b.id), seen + (cls.name,)) for b in cls.bases if isinstance(b, ast.Name))

    out = []
    for cls in [c for c in tree.body if isinstance(c, ast.ClassDef)]:
        inherited = setup_starts(cls)
        for fn in [f for f in cls.body if isinstance(f, ast.FunctionDef)]:
            st = starts(fn)
            for n in ast.walk(fn):
                if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in RESTORERS):
                    continue
                why = None
                p = par.get(n)
                while p is not None and p is not fn:
                    if isinstance(p, ast.With) and any(_is_patch_ctx(i.context_expr) for i in p.items):
                        why = "在 with mock.patch… 裡面"
                    p = par.get(p)
                if why is None and any(l < n.lineno for l in st):
                    why = "同一個函式裡 .start() 之後"
                if why is None and inherited and fn.name != "setUp":
                    why = "setUp 已經裝上模擬(start())"
                if why:
                    out.append(f"{cls.name}.{fn.name} 第{n.lineno}行：{n.func.id}() {why}——結構性還原要排在裝模擬之前")
    return out


# 刻意在模擬生效時還原、驗證執行期保險的那一項(寫明理由，不是漏掉)
RESTORE_ORDER_ALLOWED = {
    "FrameworkState.test_r80_restore_does_not_undo_active_mocks": "驗證執行期保險：還原會跳過正在生效的 patch",
}

RESTORE_SELFTEST = [  # (原始碼, 預期報出)
    ("class A:\n    def setUp(self):\n        self.eng = _engine()\n        self.p = mock.patch.object(ex, 'x')\n"
     "        self.p.start()\n    def test_x(self):\n        pass\n", False),
    ("class A:\n    def setUp(self):\n        self.p = mock.patch.object(ex, 'x')\n        self.p.start()\n"
     "        self.eng = _engine()\n", True),
    ("class A:\n    def test_x(self):\n        with mock.patch.object(ex, 'x'):\n            _engine()\n", True),
    ("class A:\n    def test_x(self):\n        _engine()\n        with mock.patch.object(ex, 'x'):\n            pass\n", False),
    ("class H:\n    def setUp(self):\n        self.eng = _engine()\n        mock.patch('t').start()\n"
     "class B(H):\n    def test_x(self):\n        _engine()\n", True),
    ("class A:\n    def test_x(self):\n        with mock.patch.dict(d, {}):\n            _reset_module_state()\n", True),
]


class TestChecker(unittest.TestCase):
    def test_restore_order_selftest(self):
        for src, flagged in RESTORE_SELFTEST:
            self.assertEqual(bool(restore_order_problems(src)), flagged, src)

    def test_lessons_restore_before_mocks(self):
        path = os.path.join(os.path.dirname(__file__), "test_lessons.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        self.assertGreater(src.count("_engine()"), 10, "前提：真的掃到了取引擎的地方")
        got = [p for p in restore_order_problems(src) if p.split(" ")[0] not in RESTORE_ORDER_ALLOWED]
        self.assertEqual(got, [])
        self.assertTrue(all(any(p.startswith(k + " ") for p in restore_order_problems(src)) for k in RESTORE_ORDER_ALLOWED),
                        "允許清單裡的每一項都要真的還會被報出(不然是過期的豁免)")

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
