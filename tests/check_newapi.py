"""
「呼叫新介面前先加存在性前提」的靜態檢查(BINANCE_LESSONS.md 用法第5點 r65；r78、r80 改寫)。
gold-scalper 犯了三次(r41 SettingsValidationError、r44 s["estimated"]、r58 manual_result／eng._position.get)，
以前只靠 tests/rerun_old 在執行期抓——那是事後才知道，而且要先存好舊版。

規則：測試檔裡用到的程式介面——模組屬性(pt.X、ex.X…，別名從測試檔的 import 自動列出)、多層的鏈(pt.db.X、
pt.notifier_module.notifier.X，照程式模組自己的 import 自動解析)、引擎屬性(eng.X／self.eng.X)——
如果在 tests/legacy/ 任一版不存在，**這一處**必須先被守住(r78 起看每一處用法，不看整個方法／檔案)：
  - 同一個(模組, 屬性)的 hasattr(...)／getattr(...)
  - 執行時在這一處之前：寫在前面的敘述裡(同一個區塊或外層區塊的前一個敘述)、在 setUp 裡，
    或這一處在「以它為條件」的分支裡——`if hasattr(X, "a"): X.a`、`X.a if hasattr(X, "a") else …`(r80：守護寫在後面、
    執行時先判斷)、`hasattr(X, "a") and X.a`。寫在 else 那一邊、或同一個敘述裡但不是條件的，不算
  - 範圍：每個函式(測試方法、輔助方法、模組層級的輔助函式)各自算；模組最上層的敘述也算一個範圍(r80)
  - 賦值(X.a = …)不算用法：舊版上是新增屬性，不會崩

靜態分不出的(寫明，不假裝有擋)：
  - 「前提量到被測的結果」(第13種)、「前提只看有查、沒看結果」(第16種)：只能靠突變檢查與舊版重跑在執行期抓。
  - 動態取的屬性(getattr(X, 變數))、別名存進變數之後再用(fn = pt.newfn; fn())：看不到。
執行：python -m unittest tests.check_newapi -v
"""
import ast
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(os.path.dirname(HERE), "app")
# 測試檔慣用的別名(自動列出的會再併進來；這份留著給自我驗證的樣本用)
BASE_ALIASES = {"pt": "paper_trading", "ex": "execution", "pf": "preflight", "RG": "risk_guard", "TS": "trading_stats",
                "m": "main", "S": "settings", "N": "notifier"}
ENGINE = {"eng", "self.eng"}


# ------------------------------------------------------------------ 程式這邊：每個模組有哪些名稱、匯入了什麼
def _module_info(path):
    """一個 .py：(頂層名稱, 引擎屬性, {匯入的名稱: 模組}, {實例名稱})。
    實例名稱＝頂層 `x = 本模組的類別(...)`：它的方法與 self.X 也算這個模組的介面(N＝notifier.notifier)。"""
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    top, eng, imports, instances = set(), set(), {}, set()
    classes = {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}

    def members(cls):
        out = set()
        for x in ast.walk(cls):
            if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.add(x.name)
            if isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name) and x.value.id == "self" \
                    and isinstance(x.ctx, ast.Store):
                out.add(x.attr)
        return out

    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            top.add(n.name)
        elif isinstance(n, ast.Assign):
            names = {t.id for t in n.targets if isinstance(t, ast.Name)}
            top |= names
            if isinstance(n.value, ast.Call) and isinstance(n.value.func, ast.Name) and n.value.func.id in classes:
                instances |= names
                top |= members(classes[n.value.func.id])
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                name = (a.asname or a.name).split(".")[0]
                top.add(name)
                if isinstance(n, ast.ImportFrom) and n.module == "app":
                    imports[name] = a.name                       # from app import db / notifier as notifier_module
                elif isinstance(n, ast.ImportFrom) and n.module and n.module.startswith("app."):
                    imports[name] = n.module.split(".", 1)[1]    # from app.notifier import notifier(實例)→ notifier
                elif isinstance(n, ast.Import) and a.name.startswith("app.") and a.asname:
                    imports[name] = a.name.split(".", 1)[1]
    if "PaperTradingEngine" in classes:
        eng = members(classes["PaperTradingEngine"])
    return top, eng, imports, instances


def app_info(app_dir=APP):
    return {f[:-3]: _module_info(os.path.join(app_dir, f)) for f in os.listdir(app_dir) if f.endswith(".py")}


def legacy_names(legacy_root):
    """{版本: ({模組: 頂層名稱}, 引擎屬性)}"""
    out = {}
    for v in sorted(os.listdir(legacy_root)):
        app = os.path.join(legacy_root, v, "app")
        if not os.path.isdir(app):
            continue
        mods, eng = {}, set()
        for f in os.listdir(app):
            if f.endswith(".py"):
                top, e, _, _ = _module_info(os.path.join(app, f))
                mods[f[:-3]] = top
                eng |= e
        out[v] = (mods, eng)
    return out


# ------------------------------------------------------------------ 測試這邊：別名、用法、守護
def test_aliases(tree):
    """測試檔自己的 import(函式裡的也算)：from app import X as Y、import app.X as Y、from app.X import obj as Y。"""
    out = dict(BASE_ALIASES)
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module == "app":
            for a in n.names:
                out[a.asname or a.name] = a.name
        elif isinstance(n, ast.ImportFrom) and n.module and n.module.startswith("app."):
            for a in n.names:
                out[a.asname or a.name] = n.module.split(".", 1)[1]
        elif isinstance(n, ast.Import):
            for a in n.names:
                if a.name.startswith("app.") and a.asname:
                    out[a.asname] = a.name.split(".", 1)[1]
    return out


def _dotted(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def make_resolver(aliases, info):
    """'pt.db' → 'db'、'pt.notifier_module.notifier' → 'notifier'、'eng' → 'engine'；認不得回 None。"""
    def resolve(dotted):
        if dotted is None:
            return None
        if dotted in ENGINE:
            return "engine"
        if dotted in aliases:
            return aliases[dotted]
        if "." not in dotted:
            return None
        base, last = dotted.rsplit(".", 1)
        mb = resolve(base)
        if mb in (None, "engine") or mb not in info:
            return None
        _top, _eng, imports, instances = info[mb]
        if last in imports:
            return imports[last]
        if last in instances:
            return mb
        return None
    return resolve


def _parents(root):
    par = {}
    for p in ast.walk(root):
        for c in ast.iter_child_nodes(p):
            par[c] = p
    return par


def _is_guard(node, mod, attr, resolve):
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("hasattr", "getattr")
            and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant) and node.args[1].value == attr
            and resolve(_dotted(node.args[0])) == mod)


def _contains_guard(node, mod, attr, resolve):
    """node 執行時會判斷到的守護：不進函式／類別／lambda 的內容(定義它不等於執行它)。"""
    stack = [node]
    while stack:
        x = stack.pop()
        if _is_guard(x, mod, attr, resolve):
            return True
        stack.extend(c for c in ast.iter_child_nodes(x)
                     if not isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)))
    return False


def _block_of(stmt, parent):
    """stmt 所在的敘述串列(body／orelse／finalbody／handlers 的 body…)。"""
    for field in ("body", "orelse", "finalbody"):
        lst = getattr(parent, field, None)
        if isinstance(lst, list) and any(s is stmt for s in lst):
            return lst
    return None


def _stmt_chain(node, par):
    """由內到外：node 所在的每一層敘述。"""
    out = []
    while node in par:
        if isinstance(node, ast.stmt):
            out.append(node)
        node = par[node]
    return out


def guarded(use, mod, attr, scope, par, resolve, setup_scope=None):
    if setup_scope is not None and _contains_guard(setup_scope, mod, attr, resolve):
        return True
    # 1. 在「以它為條件」的分支裡(if／條件運算式的 body、and 的後段)
    child, p = use, par.get(use)
    while p is not None:
        if isinstance(p, ast.If) and any(s is child for s in p.body) and _contains_guard(p.test, mod, attr, resolve):
            return True
        if isinstance(p, ast.IfExp) and child is p.body and _contains_guard(p.test, mod, attr, resolve):
            return True
        if isinstance(p, ast.BoolOp) and isinstance(p.op, ast.And):
            idx = next(i for i, v in enumerate(p.values) if v is child)
            if any(_contains_guard(v, mod, attr, resolve) for v in p.values[:idx]):
                return True
        if p is scope:
            break
        child, p = p, par.get(p)
    # 2. 寫在前面的敘述裡(同一個區塊或外層區塊、排在這一處所在敘述之前)
    chain = _stmt_chain(use, par)
    for su in chain:
        if su is scope:
            break   # 只看這個範圍裡面：同一個類別裡「前一個方法」守過不算(r80 自己寫錯過一次)
        parent = par.get(su)
        blk = _block_of(su, parent) if parent is not None else None
        if blk is None:
            continue
        idx = next(i for i, s in enumerate(blk) if s is su)
        if any(_contains_guard(s, mod, attr, resolve) for s in blk[:idx]
               if not isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))):
            return True
    return False


def _local_names(scope, skip_nested):
    """這個範圍裡被重新綁定的名稱(迴圈變數、賦值、參數、推導式)：跟別名同名時，它不是那個模組(for m in … 的 m)。"""
    out = set()
    stack = [scope]
    while stack:
        n = stack.pop()
        for c in ast.iter_child_nodes(n):
            if skip_nested and isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            stack.append(c)
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
            out.add(n.id)
        elif isinstance(n, ast.arg):
            out.add(n.arg)
    return out - ENGINE


def _uses(scope, resolve, skip_nested=False):
    """[(Attribute 節點, 顯示名, 模組或'engine', 屬性)]。賦值不算。skip_nested：模組層級不看函式與類別裡面。"""
    out = []
    shadowed = _local_names(scope, skip_nested)
    stack = [scope]
    while stack:
        n = stack.pop()
        for c in ast.iter_child_nodes(n):
            if skip_nested and isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            stack.append(c)
        if isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Load):
            base = _dotted(n.value)
            mod = None if (base and base.split(".")[0] in shadowed) else resolve(base)
            if mod is not None:
                out.append((n, f"{base}.{n.attr}", mod, n.attr))
    return out


def _test_assigned_engine_attrs(tree):
    """測試框架自己掛在引擎上的屬性(eng.alerts = [] 之類)：不是程式的介面，不檢查。"""
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Store) and _dotted(n.value) in ENGINE:
            out.add(n.attr)
    return out


def problems(test_source, legacy, info=None):
    tree = ast.parse(test_source)
    info = app_info() if info is None else info
    resolve = make_resolver(test_aliases(tree), info)
    par = _parents(tree)
    test_attrs = _test_assigned_engine_attrs(tree)
    units = [(None, "(模組最上層)", tree, None, True)]
    units += [(None, f.name, f, None, False) for f in tree.body if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))]
    for cls in [c for c in tree.body if isinstance(c, ast.ClassDef)]:
        setup = next((f for f in cls.body if isinstance(f, ast.FunctionDef) and f.name == "setUp"), None)
        units += [(cls.name, f.name, f, setup, False) for f in cls.body if isinstance(f, ast.FunctionDef)]
    out = []
    for cls_name, fn_name, scope, setup, top in units:
        for node, shown, mod, attr in _uses(scope, resolve, skip_nested=top):
            if mod == "engine" and attr in test_attrs:
                continue
            missing = []
            for v, (mods, eng) in legacy.items():
                if mod != "engine" and mod not in mods:
                    continue
                if not (attr in eng if mod == "engine" else attr in mods[mod]):
                    missing.append(v)
            if missing and not guarded(node, mod, attr, scope, par, resolve, setup):
                where = f"{cls_name}.{fn_name}" if cls_name else fn_name
                out.append(f"{where} 第{node.lineno}行：{shown} 在舊版 {missing} 不存在，執行到這一處之前沒有守護同一個屬性"
                           f"(在舊版上會是崩掉，不是斷言失敗)")
    return out


# ------------------------------------------------------------------ 自我驗證
def _fake_legacy(engine_attrs=(), **mods):
    return {"rX": ({k: set(v) for k, v in mods.items()}, set(engine_attrs))}


# 自我驗證用的「程式這邊」：paper_trading 匯入了 db、notifier(as notifier_module)；notifier 有實例 notifier
FAKE_INFO = {
    "paper_trading": ({"db", "notifier_module"}, set(), {"db": "db", "notifier_module": "notifier"}, set()),
    "db": (set(), set(), {}, set()),
    "notifier": ({"notifier"}, set(), {}, {"notifier"}),
    "main": (set(), set(), {}, set()),
    "execution": (set(), set(), {}, set()),
    "stats": (set(), set(), {}, set()),
}
L_PT = _fake_legacy(paper_trading=["oldfn", "db", "notifier_module"])
C = "class A:\n    def test_x(self):\n"
SELFTEST = [  # (測試原始碼, 人造舊版, 預期是否報出, 說明)
    (C + "        pt.newfn()\n", L_PT, True, "沒守"),
    (C + "        self.assertTrue(hasattr(pt, 'newfn'))\n        pt.newfn()\n", L_PT, False, "前面守了"),
    (C + "        pt.oldfn()\n", L_PT, False, "舊版就有"),
    (C + "        eng._new_lock\n", _fake_legacy(engine_attrs=["_lock"]), True, "引擎屬性沒守"),
    ("class A:\n    def setUp(self):\n        self.assertTrue(hasattr(self.eng, '_new_lock'))\n    def test_x(self):\n"
     "        self.eng._new_lock\n", _fake_legacy(engine_attrs=["_lock"]), False, "setUp 守了"),
    (C + "        zz.newfn()\n", L_PT, False, "認不得的名稱不管"),
    ("def _engine():\n    eng.alerts = []\nclass A:\n    def test_x(self):\n        eng.alerts\n",
     _fake_legacy(engine_attrs=["_lock"]), False, "框架自己掛的"),
    # r78：每一處用法
    (C + "        self.assertTrue(hasattr(pt, 'other'))\n        pt.newfn()\n", L_PT, True, "守的是別的屬性"),
    (C + "        pt.newfn()\n        self.assertTrue(hasattr(pt, 'newfn'))\n", L_PT, True, "守護在用法之後"),
    (C + "        mock.patch.object(pt, 'newfn', create=True)\n        hasattr(ex, 'y')\n        pt.newfn()\n",
     dict(L_PT, rY=({"execution": {"y"}, "paper_trading": {"oldfn"}}, set())), True, "patch 的字串＋別的 hasattr"),
    (C + "        pt.db.newfn()\n", _fake_legacy(paper_trading=["db"], db=["oldfn"]), True, "兩層"),
    (C + "        self.assertTrue(hasattr(pt.db, 'newfn'))\n        pt.db.newfn()\n",
     _fake_legacy(paper_trading=["db"], db=["oldfn"]), False, "兩層、守了"),
    ("class A:\n    def _helper(self):\n        pt.newfn()\n", L_PT, True, "輔助方法"),
    ("def _helper():\n    return pt.newfn()\n", L_PT, True, "模組層級輔助函式"),
    ("def _helper():\n    if hasattr(pt, 'newfn'):\n        return pt.newfn()\n", L_PT, False, "if 區塊裡"),
    (C + "        m._resumed['k'] = True\n", _fake_legacy(main=["app"]), True, "pump-dump-hunter r78 的樣子"),
    # r80：條件運算式、分支方向、and、最上層、多層鏈、自動別名
    (C + "        x = pt.newfn() if hasattr(pt, 'newfn') else None\n", L_PT, False, "條件運算式(同一行)"),
    (C + "        x = (pt.newfn()\n             if hasattr(pt, 'newfn') else None)\n", L_PT, False,
     "條件運算式跨行：守護寫在後面、執行時先判斷"),
    (C + "        x = None if hasattr(pt, 'newfn') else pt.newfn()\n", L_PT, True, "用在 else 那一邊"),
    (C + "        if hasattr(pt, 'newfn'):\n            pass\n        else:\n            pt.newfn()\n", L_PT, True,
     "if 的 else 裡"),
    (C + "        ok = hasattr(pt, 'newfn') and pt.newfn()\n", L_PT, False, "and 前段守住"),
    (C + "        ok = hasattr(pt, 'newfn') or pt.newfn()\n", L_PT, True, "or：前段為假才執行後段，沒守住"),
    (C + "        f(hasattr(pt, 'newfn'), pt.newfn())\n", L_PT, True, "同一個敘述裡但不是條件"),
    (C + "        if not hasattr(pt, 'newfn'):\n            self.fail('x')\n        pt.newfn()\n", L_PT, False,
     "前一個敘述(if not … fail)"),
    ("x = pt.newfn()\n", L_PT, True, "模組最上層"),
    ("if hasattr(pt, 'newfn'):\n    pt.newfn()\n", L_PT, False, "模組最上層、守了"),
    ("pt.newfn = 1\n", L_PT, False, "賦值不算用法"),
    ("class A:\n    def test_a(self):\n        hasattr(pt, 'newfn')\n    def test_b(self):\n        pt.newfn()\n", L_PT, True,
     "前一個方法守過不算"),
    ("def f():\n    return hasattr(pt, 'newfn')\nx = pt.newfn()\n", L_PT, True, "前面定義的函式裡有守護，不等於執行過"),
    ("for m in range(3):\n    m.name\n", _fake_legacy(main=["app"]), False, "跟別名同名的迴圈變數不是那個模組"),
    (C + "        pt.notifier_module.notifier.newfn()\n", _fake_legacy(paper_trading=["db", "notifier_module"],
                                                                  notifier=["notifier"]), True, "三層(模組→模組→實例)"),
    ("from app import stats as st\nclass A:\n    def test_x(self):\n        st.newfn()\n",
     _fake_legacy(stats=["oldfn"]), True, "別名從 import 自動列出(沒列在清單裡的模組)"),
]


class TestNewApi(unittest.TestCase):
    def test_selftest_fixed_samples(self):
        for src, legacy, flagged, why in SELFTEST:
            self.assertEqual(bool(problems(src, legacy, FAKE_INFO)), flagged, f"{why}\n{src}")

    def test_selftest_sample_count(self):
        self.assertGreaterEqual(len(SELFTEST), 32, "前提：自我驗證的樣本沒被刪掉")

    def test_real_app_resolves_chains(self):
        """前提：真的程式上，多層的鏈認得出來(認不出來的話，整個檢查對它們是空跑)。"""
        info = app_info()
        self.assertGreater(len(info), 5, "前提：真的解析到了 app/ 的模組")
        r = make_resolver(test_aliases(ast.parse("from app import paper_trading as pt\n")), info)
        self.assertEqual(r("pt.db"), "db")
        self.assertEqual(r("pt.notifier_module"), "notifier")
        self.assertEqual(r("pt.notifier_module.notifier"), "notifier")
        self.assertEqual(r("pt.settings_module"), "settings")

    def test_lessons_tests_guard_new_interfaces(self):
        legacy = legacy_names(os.path.join(HERE, "legacy"))
        self.assertGreaterEqual(len(legacy), 1, "前提：tests/legacy/ 裡真的有舊版")
        self.assertTrue(all(len(m) > 5 and "paper_trading" in m for m, _ in legacy.values()), "前提：舊版真的解析到了模組")
        with open(os.path.join(HERE, "test_lessons.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertGreater(src.count("def test_"), 50, "前提：真的掃到了測試")
        self.assertEqual(problems(src, legacy), [])


if __name__ == "__main__":
    unittest.main()
