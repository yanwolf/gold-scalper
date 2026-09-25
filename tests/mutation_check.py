"""
突變驗證(BINANCE_LESSONS.md 用法第5點 r18/r21/r22)。

突變：PaperTradingEngine._side_qty 一律回「查不到」
  → 送單前基準查不到、不送進場單(第3條r16)；掛停損前確認不到、不掛(第2條r19)；平倉確認查不到、待平倉(第8條r13)

逐項比對(r21)：全部測試都在突變下跑，突變下仍通過的每一項都要有理由，否則就是空跑。理由分三類，全部由程式驗證：
  無關   —— 自動判定：這項測試在突變下根本沒呼叫被突變的查詢(呼叫次數=0)，不需要列清單。
  前提   —— 同一個情境裡，另有一項(被引用的那一項)在突變下明確失敗。
  對照組 —— 被引用的那一項在「沒有突變」時通過、「突變」時失敗，證明正常時那條路走得通。
另外報出過期的豁免(列在清單、但已經不在突變下通過／測試不存在)。
檢查器自我驗證(r22)：故意拿掉一項豁免、把一項引用改成突變下會通過的項目，確認兩個都會被報出來。

執行：python -m tests.mutation_check   (有任何問題時以非0結束)
"""
import sys
import unittest
from unittest import mock

import tests.test_lessons as T

# 突變下仍通過、且確實經過被突變查詢的項目：{測試: (類別, 被引用的測試, 說明)}
EXEMPT = {
    "Lesson19.test_t8c_step_error_recovers_and_is_cleared_at_position_end": ("對照組", "Lesson54.test_r54_exit_does_not_close_other_position",
        "平倉送單前的確認(r54)在突變下查不到→照樣送單(原本的設計)；這項測的是送單之後的事。送單前的確認本身由被引用的那一項驗證(突變下失敗)"),
    "Lesson22.test_t8c_error_after_close_confirmed_is_not_restored": ("對照組", "Lesson54.test_r54_exit_does_not_close_other_position",
        "平倉送單前的確認(r54)在突變下查不到→照樣送單(原本的設計)；這項測的是送單之後的事。送單前的確認本身由被引用的那一項驗證(突變下失敗)"),
    "Lesson22.test_t8c_exit_price_none_still_closes": ("對照組", "Lesson54.test_r54_exit_does_not_close_other_position",
        "平倉送單前的確認(r54)在突變下查不到→照樣送單(原本的設計)；這項測的是送單之後的事。送單前的確認本身由被引用的那一項驗證(突變下失敗)"),
    "Lesson25.test_t8a_cleanup_error_after_record_does_not_stop_other_cleanup": ("對照組", "Lesson54.test_r54_exit_does_not_close_other_position",
        "平倉送單前的確認(r54)在突變下查不到→照樣送單(原本的設計)；這項測的是送單之後的事。送單前的確認本身由被引用的那一項驗證(突變下失敗)"),
    "Lesson25.test_t8a_record_is_written_before_cleanup_and_pnl_error_does_not_block": ("對照組", "Lesson54.test_r54_exit_does_not_close_other_position",
        "平倉送單前的確認(r54)在突變下查不到→照樣送單(原本的設計)；這項測的是送單之後的事。送單前的確認本身由被引用的那一項驗證(突變下失敗)"),
    "Lesson44.test_r44_partial_close_fill_is_not_closed": ("對照組", "Lesson54.test_r54_exit_does_not_close_other_position",
        "平倉送單前的確認(r54)在突變下查不到→照樣送單(原本的設計)；這項測的是送單之後的事。送單前的確認本身由被引用的那一項驗證(突變下失敗)"),
    "LiveAckResponse.test_close_fill_price_from_trades_when_response_lacks_it": ("對照組", "Lesson54.test_r54_exit_does_not_close_other_position",
        "平倉送單前的確認(r54)在突變下查不到→照樣送單(原本的設計)；這項測的是送單之後的事。送單前的確認本身由被引用的那一項驗證(突變下失敗)"),
    "Lesson72.test_r72_partial_close_weighting_waits_for_all_segments": ("對照組", "Lesson54.test_r54_exit_does_not_close_other_position",
        "平倉送單前的確認(r54)在突變下查不到→照樣送單(原本的設計)；這項測的是送單之後分段加權的出場價(r72)。送單前的確認本身由被引用的那一項驗證(突變下失敗)"),
    "LiveFilledNoAvgPrice.test_r71_close_backfills_exit_price_and_notifies": ("對照組", "Lesson54.test_r54_exit_does_not_close_other_position",
        "平倉送單前的確認(r54)在突變下查不到→照樣送單(原本的設計)；這項測的是送單之後的成交價補登(r71)。送單前的確認本身由被引用的那一項驗證(突變下失敗)"),
    "LiveFilledNoAvgPrice.test_r71_close_notice_does_not_dump_raw_response": ("對照組", "Lesson54.test_r54_exit_does_not_close_other_position",
        "平倉送單前的確認(r54)在突變下查不到→照樣送單(原本的設計)；這項測的是送單之後的成交價補登(r71)。送單前的確認本身由被引用的那一項驗證(突變下失敗)"),
    "LiveFilledNoAvgPrice.test_r71_backfill_gives_up_with_one_alert": ("對照組", "Lesson54.test_r54_exit_does_not_close_other_position",
        "平倉送單前的確認(r54)在突變下查不到→照樣送單(原本的設計)；這項測的是送單之後的成交價補登(r71)。送單前的確認本身由被引用的那一項驗證(突變下失敗)"),
    "LiveFilledNoAvgPrice.test_r71_partial_trade_rows_are_not_a_fill_price": ("對照組", "Lesson54.test_r54_exit_does_not_close_other_position",
        "平倉送單前的確認(r54)在突變下查不到→照樣送單(原本的設計)；這項測的是送單之後的成交價補登(r71)。送單前的確認本身由被引用的那一項驗證(突變下失敗)"),
    "Lesson54.test_r54_control_exit_closes_other_position_when_price_unknown": ("對照組", "Lesson54.test_r54_exit_does_not_close_other_position",
        "這項本身就是對照組：均價比對失效時必須出事；突變讓部位查詢查不到，同樣會送單"),
    # 目前沒有。r22 對照時原本列了 3 項，檢查器報出它們在突變下其實已經會失敗(前提斷言「真的查了部位」
    # 抓到了突變)，屬於過期豁免，已刪除。
}


def _all_tests():
    suite = unittest.defaultTestLoader.loadTestsFromModule(T)
    out = []
    def walk(s):
        for x in s:
            if isinstance(x, unittest.TestSuite):
                walk(x)
            else:
                out.append(f"{type(x).__name__}.{x._testMethodName}")
    walk(suite)
    return out


CRASHED = set()


def _run(name):
    """跑一項；測試程式本身拋錯(不是斷言失敗、也不是被測程式拋的)另外記下(用法第5點r34)。"""
    cls, meth = name.split(".")
    res = unittest.TestResult()
    unittest.TestSuite([getattr(T, cls)(meth)]).run(res)
    for _, tb in res.errors:
        frames = [l for l in tb.splitlines() if 'File "' in l]
        if frames and "tests/test_lessons.py" in frames[-1]:
            CRASHED.add(name)
    return res.wasSuccessful()


def run_mutated():
    """回傳 {測試: (突變下是否通過, 被突變查詢的呼叫次數)}"""
    calls = {"n": 0}
    def broken(self, direction):
        calls["n"] += 1
        return (False, 0.0, None, None)
    res = {}
    with mock.patch.object(T.pt.PaperTradingEngine, "_side_qty", broken):
        for name in _all_tests():
            calls["n"] = 0
            res[name] = (_run(name), calls["n"])
    return res


def check(exempt, mutated, normal_pass):
    problems, unrelated = [], []
    for name, (passed, n) in mutated.items():
        if not passed:
            continue
        if n == 0:
            unrelated.append(name)
            continue
        if name not in exempt:
            problems.append(f"空跑：{name}（突變下通過、呼叫了被突變的查詢 {n} 次、沒有豁免理由）")
            continue
        kind, cited, _ = exempt[name]
        if cited not in mutated:
            problems.append(f"豁免無效：{name} 引用的 {cited} 不存在")
        elif mutated[cited][0]:
            problems.append(f"豁免無效：{name} 引用的 {cited} 在突變下也通過，證明不了什麼")
        elif kind == "對照組" and not normal_pass.get(cited):
            problems.append(f"豁免無效：{name} 的對照組 {cited} 在沒有突變時不通過")
    for name in exempt:
        if name not in mutated:
            problems.append(f"過期豁免：{name} 已不存在")
        elif not mutated[name][0]:
            problems.append(f"過期豁免：{name} 在突變下已經會失敗，請從清單刪掉")
        elif mutated[name][1] == 0:
            problems.append(f"過期豁免：{name} 已不經過被突變的查詢(自動判定為無關)，請從清單刪掉")
    return problems, unrelated


def self_test():
    """
    檢查器自我驗證(r22)：用固定的人造資料，不依賴當下的豁免清單——清單裡的項目可能本身就過期，
    拿它來弄壞，檢查器根本不會去看引用，自我驗證就空跑了(第一版就是這樣)。
    A：突變下通過、有呼叫查詢；C：突變下失敗；U：突變下通過、沒呼叫查詢；S：列在清單但突變下已失敗
    """
    m = {"A": (True, 2), "C": (False, 1), "U": (True, 0), "S": (False, 3)}
    good = {"A": ("前提", "C", "")}
    cases = [
        ("正確的清單不報錯", good, lambda p: p == []),
        ("拿掉一項豁免→報空跑", {}, lambda p: any(x.startswith("空跑") and "A" in x for x in p)),
        ("引用改成突變下會通過的項目→報無效", {"A": ("前提", "U", "")}, lambda p: any("也通過" in x for x in p)),
        ("對照組在沒突變時也不通過→報無效", {"A": ("對照組", "C", "")}, lambda p: any("沒有突變時不通過" in x for x in p)),
        ("清單裡有突變下已失敗的項目→報過期", dict(good, S=("前提", "C", "")), lambda p: any("過期" in x and "S" in x for x in p)),
    ]
    ok = True
    for label, ex_list, expect in cases:
        normal = {"C": label != "對照組在沒突變時也不通過→報無效"}
        p, _ = check(ex_list, m, normal)
        hit = expect(p)
        ok = ok and hit
        print(f"  自我驗證：{label} → {'OK' if hit else '沒抓到!!'}")
    return ok


def main():
    mutated = run_mutated()
    normal_pass = {n: _run(n) for n in {c for _, c, _ in EXEMPT.values()}}
    problems, unrelated = check(EXEMPT, mutated, normal_pass)
    # 突變下測試本身崩掉：後面的斷言沒被檢查、失敗訊息看不出是哪個前提不成立(用法第5點r34)
    problems += [f"突變下測試本身崩掉：{n}(索引前先確認有東西)" for n in sorted(CRASHED)]
    failed = [n for n, (p, _) in mutated.items() if not p]
    print(f"全部 {len(mutated)} 項：突變下明確失敗 {len(failed)} 項、無關(未呼叫被突變查詢) {len(unrelated)} 項、"
          f"經驗證的豁免 {len(EXEMPT)} 項")
    for n, (k, c, why) in EXEMPT.items():
        print(f"  豁免[{k}] {n}\n      ← {c}：{why}")

    self_ok = self_test()
    print("問題：", "\n  ".join(problems) if problems else "無")
    return 0 if (not problems and self_ok) else 1


if __name__ == "__main__":
    sys.exit(main())
