"""
「呼叫新介面前先加存在性前提」的靜態檢查(BINANCE_LESSONS.md 用法第5點 r65)。
gold-scalper 犯了三次(r41 SettingsValidationError、r44 s["estimated"]、r58 manual_result／eng._position.get)，
以前只靠 tests/rerun_old 在執行期抓——那是事後才知道，而且要先存好舊版。

規則：測試方法裡用到的「模組屬性」(pt.X／ex.X／pf.X／RG.X／TS.X／m.X／S.X)或「引擎屬性」(self.eng.X／eng.X)，
如果在 tests/legacy/ 任一版的原始碼裡不存在(用語法樹找頂層定義與 PaperTradingEngine 的方法／self.X 賦值)，
那個測試方法(或它所在類別的 setUp)裡必須先有 hasattr(...)／getattr(...) 守護，否則在舊版上重跑會是「測試崩掉」而不是斷言失敗。

靜態分不出的(寫明，不假裝有擋)：
  - 「前提量到被測的結果」(第13種)：前提「真的送單了」合法，「結果是X」當前提也合法，兩者只差在斷言的是不是被測那一步的產物，
    靜態看不出；只能靠突變檢查(結果在被測步驟之前就存在時突變下照樣通過→報空跑)與舊版重跑在執行期抓。
  - 「前提只看有查、沒看結果」(第16種)：同上，靜態看不出「查」跟「查的結果」哪個才是該斷言的。
執行：python -m unittest tests.check_newapi -v
"""
import ast
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ALIASES = {"pt": "paper_trading", "ex": "execution", "pf": "preflight", "RG": "risk_guard", "TS": "trading_stats",
           "m": "main", "S": "settings", "N": "notifier"}
ENGINE_ALIASES = ("eng", "self.eng")


def names_in(path):
    """一個 .py 檔的頂層名稱；另外回傳 PaperTradingEngine 的方法與 self.X 屬性。"""
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    top, eng = set(), set()
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            top.add(n.name)
        elif isinstance(n, ast.Assign):
            top |= {t.id for t in n.targets if isinstance(t, ast.Name)}
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            top |= {(a.asname or a.name).split(".")[0] for a in n.names}   # 匯入的名稱也是模組屬性(pt.db、m.settings_module)
        if isinstance(n, ast.ClassDef) and n.name == "TelegramNotifier":
            # N 是這個類別的實例：它的方法與 self.X 屬性都算 notifier 模組的介面
            for x in ast.walk(n):
                if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    top.add(x.name)
                if isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name) and x.value.id == "self" and isinstance(x.ctx, ast.Store):
                    top.add(x.attr)
        if isinstance(n, ast.ClassDef) and n.name == "PaperTradingEngine":
            for x in ast.walk(n):
                if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    eng.add(x.name)
                if isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name) and x.value.id == "self" and isinstance(x.ctx, ast.Store):
                    eng.add(x.attr)
    return top, eng


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
                top, e = names_in(os.path.join(app, f))
                mods[f[:-3]] = top
                eng |= e
        out[v] = (mods, eng)
    return out


def _uses(fn_node):
    """回傳 [(顯示名, 模組別名或'engine', 屬性, 行號)]"""
    out = []
    for n in ast.walk(fn_node):
        if not isinstance(n, ast.Attribute):
            continue
        base = n.value
        if isinstance(base, ast.Name) and base.id in ALIASES:
            out.append((f"{base.id}.{n.attr}", ALIASES[base.id], n.attr, n.lineno))
        elif isinstance(base, ast.Name) and base.id == "eng":
            out.append((f"eng.{n.attr}", "engine", n.attr, n.lineno))
        elif isinstance(base, ast.Attribute) and isinstance(base.value, ast.Name) and base.value.id == "self" and base.attr == "eng":
            out.append((f"self.eng.{n.attr}", "engine", n.attr, n.lineno))
    return out


def _guarded(src_of_fn, attr):
    return (f'"{attr}"' in src_of_fn or f"'{attr}'" in src_of_fn) and ("hasattr(" in src_of_fn or "getattr(" in src_of_fn)


def _test_assigned_engine_attrs(tree):
    """測試框架自己掛在引擎上的屬性(eng.alerts = [] 之類)：不是程式的介面，不檢查。"""
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Store):
            b = n.value
            if (isinstance(b, ast.Name) and b.id == "eng") or \
                    (isinstance(b, ast.Attribute) and isinstance(b.value, ast.Name) and b.value.id == "self" and b.attr == "eng"):
                out.add(n.attr)
    return out


def problems(test_source, legacy):
    out = []
    tree = ast.parse(test_source)
    test_attrs = _test_assigned_engine_attrs(tree)
    for cls in [c for c in tree.body if isinstance(c, ast.ClassDef)]:
        setup_src = ""
        for f in cls.body:
            if isinstance(f, ast.FunctionDef) and f.name == "setUp":
                setup_src = ast.get_source_segment(test_source, f) or ""
        for fn in [f for f in cls.body if isinstance(f, ast.FunctionDef) and f.name.startswith("test")]:
            fn_src = (ast.get_source_segment(test_source, fn) or "") + setup_src
            for shown, mod, attr, line in _uses(fn):
                if mod == "engine" and attr in test_attrs:
                    continue
                missing = []
                for v, (mods, eng) in legacy.items():
                    exists = attr in eng if mod == "engine" else (mod in mods and attr in mods[mod])
                    if mod != "engine" and mod not in mods:
                        continue
                    if not exists:
                        missing.append(v)
                if missing and not _guarded(fn_src, attr):
                    out.append(f"{cls.name}.{fn.name} 第{line}行：{shown} 在舊版 {missing} 不存在，方法裡沒有 hasattr/getattr 守護(在舊版上會是崩掉，不是斷言失敗)")
    return out


def _fake_legacy(engine_attrs=(), **mods):
    return {"rX": ({k: set(v) for k, v in mods.items()}, set(engine_attrs))}


SELFTEST = [  # (測試原始碼, 人造舊版, 預期是否報出)
    ("class A:\n    def test_x(self):\n        pt.newfn()\n", _fake_legacy(paper_trading=["oldfn"]), True),
    ("class A:\n    def test_x(self):\n        self.assertTrue(hasattr(pt, 'newfn'))\n        pt.newfn()\n", _fake_legacy(paper_trading=["oldfn"]), False),
    ("class A:\n    def test_x(self):\n        pt.oldfn()\n", _fake_legacy(paper_trading=["oldfn"]), False),
    ("class A:\n    def test_x(self):\n        eng._new_lock\n", _fake_legacy(engine_attrs=["_lock"]), True),
    ("class A:\n    def setUp(self):\n        self.assertTrue(hasattr(self.eng, '_new_lock'))\n    def test_x(self):\n        self.eng._new_lock\n", _fake_legacy(engine_attrs=["_lock"]), False),
    ("class A:\n    def test_x(self):\n        zz.newfn()\n", _fake_legacy(paper_trading=["oldfn"]), False),   # 不在別名清單裡：不管
    ("def _engine():\n    eng.alerts = []\nclass A:\n    def test_x(self):\n        eng.alerts\n", _fake_legacy(engine_attrs=["_lock"]), False),  # 框架自己掛的
]


class TestNewApi(unittest.TestCase):
    def test_selftest_fixed_samples(self):
        for src, legacy, flagged in SELFTEST:
            self.assertEqual(bool(problems(src, legacy)), flagged, src)

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
