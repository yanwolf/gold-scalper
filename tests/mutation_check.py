"""
突變驗證(BINANCE_LESSONS.md 用法第5點 r18)：把新規則的前置條件弄壞，會用到它的測試必須全部明確失敗；
還通過的就是在空跑(斷言在程式什麼都沒做時也成立)。

突變：PaperTradingEngine._side_qty 一律回「查不到」
  → 送單前基準查不到、不送進場單(第3條r16)
  → 掛停損前確認不到部位、不掛停損(第2條r19)
執行：python -m tests.mutation_check        (有空跑的項目時以非0結束)
"""
import sys
import unittest
from unittest import mock

import tests.test_lessons as T

# 會送進場單的情境
SENDS_ENTRY = [
    "Lesson3.test_t3a_timeout_but_filled_is_claimed_with_exchange_qty_and_price",
    "Lesson3.test_t3b_timeout_not_yet_visible_is_kept_pending_with_deadline",
    "Lesson3.test_t3c_explicit_reject_is_not_pending",
    "Lesson8.test_t8d_open_filled_then_later_step_raises_still_reported_as_filled",
]
# 會掛(或搬移、補掛)停損的情境
PLACES_STOP = [
    "Lesson8.test_t8b_backstop_fail_once_then_recover_sends_recovery",
    "Lesson8.test_t8c1_exception_in_backstop_sync_is_counted",
    "Lesson8.test_t8c1_stale_cancel_failure_during_move_is_counted_at_failure",
    "Lesson8.test_t8c2_failure_state_cleared_by_close_sends_notice",
    "Lesson16.test_t1a_guard_replaces_backstop_missing_three_rounds",
    "Lesson16.test_t1b_algo_404_falls_back_to_legacy_query_instead_of_skipping",
    "Lesson16.test_t8b_guard_replace_hits_minus2021_exits",
    "Lesson16.test_t3c_claim_then_same_round_checks_do_not_close",
    "Lesson19.test_t2b_quantity_is_min_of_exchange_minus_baseline_and_recorded",
]
# 突變豁免：本來就在測「查不到」這個條件。對照組(同樣的呼叫在沒注入時確實會送單/掛單)寫在右邊
EXEMPT = {
    "Lesson16.test_t3a_baseline_query_failure_does_not_send": "對照組 Lesson8.test_t8d(同樣的開倉呼叫，基準查得到時有送單)",
    "Lesson19.test_t2b_query_failure_skips_round_without_counting_failure": "對照組 Lesson19.test_t2b_quantity(同樣的呼叫，查得到時有掛單)",
    "Lesson19.test_t2b_no_backstop_when_own_position_gone": "突變下仍不掛單屬預期；對照組同上",
    "Lesson19.test_t2b_moving_stop_also_confirms_position": "突變下仍不掛單屬預期；對照組同上",
}


def _run(name):
    cls, meth = name.split(".")
    suite = unittest.TestSuite([getattr(T, cls)(meth)])
    return unittest.TextTestRunner(stream=open("/dev/null", "w")).run(suite).wasSuccessful()


def main():
    broken = lambda self, direction: (False, 0.0, None, None)
    hollow = []
    with mock.patch.object(T.pt.PaperTradingEngine, "_side_qty", broken):
        for group, names in (("送進場單", SENDS_ENTRY), ("掛停損", PLACES_STOP)):
            for n in names:
                ok = _run(n)
                print(f"{'空跑!!' if ok else '明確失敗'}  [{group}] {n}")
                if ok:
                    hollow.append(n)
        for n, why in EXEMPT.items():
            print(f"豁免({'通過' if _run(n) else '失敗'})  {n} — {why}")
    # 對照組在沒有突變時必須通過(證明「會送單/會掛單」的前提在正常情況下成立)
    for n in ("Lesson8.test_t8d_open_filled_then_later_step_raises_still_reported_as_filled",
              "Lesson19.test_t2b_quantity_is_min_of_exchange_minus_baseline_and_recorded"):
        print(f"對照組(無突變){'通過' if _run(n) else '失敗!!'}  {n}")
    print("空跑項目：", hollow or "無")
    return 1 if hollow else 0


if __name__ == "__main__":
    sys.exit(main())
