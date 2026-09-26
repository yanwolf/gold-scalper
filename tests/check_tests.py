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


# ---------------------------------------------------------------- r85：執行時長出來的狀態，重啟後讀不讀得回來
APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
POSITION_NAMES = ("position", "pos", "pos2", "p")
# paper_trades 的欄位裡，還原持倉時刻意不讀的(寫明理由)：都是平倉後、或只給網頁看的
NOT_SELECTED_COLUMNS = {
    "status": "WHERE 條件本身",
    "exit_price": "平倉後", "exit_time": "平倉後", "exit_reason": "平倉後", "pnl_points": "平倉後", "pnl_estimated": "平倉後",
    "exit_expected_price": "平倉後", "exit_actual_price": "平倉後", "exit_slippage_points": "平倉後",
    "exit_spread_points": "平倉後", "exit_book_stale": "平倉後",
    "exit_backfill": "出場補登記號：重啟時由 load_pending_backfills 另外讀",
    "entry_expected_price": "執行品質，只給網頁看", "entry_slippage_points": "執行品質，只給網頁看",
    "entry_spread_points": "執行品質，只給網頁看", "entry_book_stale": "執行品質，只給網頁看",
}


def runtime_keys(sources):
    """程式裡寫到部位上的鍵：position["x"] = …／position.setdefault("x", …)／position.update(x=…)。底線開頭(同一次呼叫內的暫存)不算。"""
    out = {}
    for name, src in sources.items():
        for n in ast.walk(ast.parse(src)):
            key = None
            if isinstance(n, ast.Subscript) and isinstance(n.ctx, ast.Store) and isinstance(n.value, ast.Name) \
                    and n.value.id in POSITION_NAMES and isinstance(n.slice, ast.Constant) and isinstance(n.slice.value, str):
                key = n.slice.value
            elif isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name) \
                    and n.func.value.id in POSITION_NAMES and n.func.attr == "setdefault" and n.args \
                    and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str):
                key = n.args[0].value
            if key and not key.startswith("_"):
                out.setdefault(key, f"{name}:{n.lineno}")
    return out


def _literal_tuple(tree, name):
    for n in tree.body:
        if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in n.targets):
            v = n.value
            if isinstance(v, ast.BinOp):   # A + (…)
                return _literal_tuple(tree, v.left.id) + tuple(ast.literal_eval(v.right))
            return tuple(ast.literal_eval(v)) if not isinstance(v, ast.Dict) else tuple(ast.literal_eval(v))
    return ()


def restored_keys(db_src, pt_src):
    """重啟時會讀回來的部位鍵：load_open_paper_trade 組出來的字典鍵＋存在 partial_state 的 RUNTIME_STATE_KEYS＋程式在還原時另外設的。"""
    db_tree, pt_tree = ast.parse(db_src), ast.parse(pt_src)
    fn = next(n for n in db_tree.body if isinstance(n, ast.FunctionDef) and n.name == "load_open_paper_trade")
    keys = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Dict):
            keys |= {k.value for k in n.keys if isinstance(k, ast.Constant)}
        if isinstance(n, ast.Subscript) and isinstance(n.ctx, ast.Store) and isinstance(n.slice, ast.Constant):
            keys.add(n.slice.value)
    return keys | set(_literal_tuple(pt_tree, "RUNTIME_STATE_KEYS")), set(_literal_tuple(pt_tree, "NOT_RESTORED_KEYS"))


def state_key_problems(sources, db_src, pt_src):
    restored, exempt = restored_keys(db_src, pt_src)
    written = runtime_keys(sources)
    out = [f"部位鍵 {k}({where})執行時寫、重啟後讀不回來：加進 RUNTIME_STATE_KEYS，或寫明理由列進 NOT_RESTORED_KEYS"
           for k, where in sorted(written.items()) if k not in restored and k not in exempt]
    out += [f"NOT_RESTORED_KEYS 裡的 {k} 程式已經不寫了(過期的豁免)" for k in sorted(exempt) if k not in written]
    return out


def column_problems(db_src):
    """paper_trades 的欄位：還原持倉的 SELECT 沒選的，要在 NOT_SELECTED_COLUMNS 寫明理由(新欄位忘了進 SELECT 就報)。"""
    import re
    i = db_src.index("def load_open_paper_trade")
    sel = set(re.findall(r"\w+", re.search(r"SELECT(.*?)FROM paper_trades", db_src[i:], re.S).group(1)))
    j = db_src.index("CREATE TABLE IF NOT EXISTS paper_trades")
    base = re.findall(r"^\s+(\w+)\s+(?:SERIAL|TEXT|DOUBLE|BOOLEAN|INTEGER|BIGINT|TIMESTAMPTZ|REAL|NUMERIC)",
                      db_src[j:db_src.index(");", j)], re.M)
    cols = list(dict.fromkeys(base + re.findall(r"ADD COLUMN IF NOT EXISTS (\w+)", db_src)))
    out = [f"paper_trades.{c} 還原持倉時沒讀、也沒寫明理由" for c in cols if c not in sel and c not in NOT_SELECTED_COLUMNS]
    out += [f"NOT_SELECTED_COLUMNS 裡的 {c} 已經不存在或已經有讀(過期的豁免)" for c in NOT_SELECTED_COLUMNS
            if c not in cols or c in sel]
    return out, len(cols), len(sel)


class TestStateKeys(unittest.TestCase):
    def _src(self, f):
        with open(os.path.join(APP_DIR, f), encoding="utf-8") as fh:
            return fh.read()

    def test_selftest(self):
        db_src = ("def load_open_paper_trade():\n    pos = {'id': 1, 'sl_price': 2}\n    pos['entry_actual_price'] = 3\n")
        pt_src = "A = ('usd_estimated',)\nRUNTIME_STATE_KEYS = A + ('pending_close',)\nNOT_RESTORED_KEYS = {'gone_checks': 'x'}\n"
        ok = {"p": "position['pending_close'] = 1\nposition['gone_checks'] = 1\nposition.setdefault('usd_estimated', 1)\n"
                   "pos['sl_price'] = 1\nposition['_tmp'] = 1\n"}
        self.assertEqual(state_key_problems(ok, db_src, pt_src), [], "都讀得回來或有理由")
        bad = {"p": "position['pending_close'] = 1\nposition['gone_checks'] = 1\nposition['last_close_win'] = True\n"}
        self.assertEqual(len(state_key_problems(bad, db_src, pt_src)), 1, "執行時長出來、沒進清單(crypto-screener 的 lastCloseWin)")
        stale = {"p": "position['pending_close'] = 1\n"}
        self.assertEqual(len(state_key_problems(stale, db_src, pt_src)), 1, "豁免清單裡的鍵程式已經不寫：過期")
        bad2 = {"p": "position.setdefault('cooldown_until', 1)\nposition['gone_checks'] = 1\n"}
        self.assertEqual(len(state_key_problems(bad2, db_src, pt_src)), 1, "setdefault 長出來的也算")

    def test_runtime_position_keys_are_restored(self):
        sources = {f: self._src(f) for f in ("paper_trading.py", "trading_core.py")}
        written = runtime_keys(sources)
        self.assertGreater(len(written), 20, "前提：真的掃到了寫進部位的鍵")
        self.assertIn("pending_close", written, "前提：掃得到待平倉")
        self.assertEqual(state_key_problems(sources, self._src("db.py"), self._src("paper_trading.py")), [])

    def test_columns_are_read_on_restore(self):
        probs, n_cols, n_sel = column_problems(self._src("db.py"))
        self.assertGreater(n_cols, 30, "前提：真的解析到了 paper_trades 的欄位")
        self.assertGreater(n_sel, 15, "前提：真的解析到了還原持倉的 SELECT")
        self.assertEqual(probs, [])
        fake = self._src("db.py").replace("ADD COLUMN IF NOT EXISTS exit_backfill TEXT;",
                                          "ADD COLUMN IF NOT EXISTS exit_backfill TEXT;\n ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS cooldown_until TEXT;")
        self.assertNotEqual(fake, self._src("db.py"), "前提：反向樣本真的插進去了")
        self.assertEqual(len(column_problems(fake)[0]), 1, "自我驗證：新增欄位沒進 SELECT 就報")


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
