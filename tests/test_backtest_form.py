"""
r122 的測試：回測面板重新排版(策略／期間放最上面、進階收合、各策略專屬參數只在選到時顯示、前後兩個半年一鍵送兩個任務、
結果收合與兩個半年對照表)。純前端，這裡檢查網頁上的對應關係不要跟後端走散。
執行：python -m unittest tests.test_backtest_form -v
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def html():
    with open(os.path.join(ROOT, "app/static/dashboard.html"), encoding="utf-8") as f:
        return f.read()


class Form(unittest.TestCase):
    def setUp(self):
        self.h = html()

    def test_all_old_ids_still_exist_once(self):
        for i in ("backtestInterval", "backtestDays", "backtestResolution", "backtestEndDate", "backtestEndDateClear",
                  "backtestWindows", "backtestStrategy", "backtestResonanceThreshold", "backtestSymbol", "backtestBucketSize",
                  "backtestRunBtn", "backtestParamsBody"):
            self.assertEqual(self.h.count(f'id="{i}"'), 1, i)

    def test_strategy_options_match_backend(self):
        from app import backtest as B
        sel = re.search(r'<select id="backtestStrategy">(.*?)</select>', self.h, re.S)
        self.assertIsNotNone(sel, "前提：找到策略選單")
        values = re.findall(r'<option value="(\w+)"', sel.group(1))
        self.assertEqual(set(values), {"chan_profile", "resonance_fvg"} | set(B.HOURLY_STRATEGIES))
        js_hourly = re.search(r"const BT_HOURLY = \[(.*?)\];", self.h)
        self.assertIsNotNone(js_hourly, "前提：找到 BT_HOURLY")
        self.assertEqual(set(re.findall(r"'(\w+)'", js_hourly.group(1))), set(B.HOURLY_STRATEGIES),
                         "1小時K策略清單要跟後端一致(K線週期自動切換靠它)")
        hint = re.search(r"const BT_STRATEGY_HINT = \{(.*?)\};", self.h, re.S)
        self.assertIsNotNone(hint, "前提：找到策略說明")
        for v in values:
            self.assertIn(f"{v}: '", hint.group(1), f"{v} 要有說明")

    def test_strategy_groups(self):
        groups = re.findall(r'<div class="bt-strat-group" data-strategies="([^"]+)">', self.h)
        self.assertEqual(len(groups), 6, "停損共用一組＋SMC／均值回歸／回拉轉強／斐波那契／突破各一組")
        flat = " ".join(groups).split()
        for st in ("smc_structure", "mean_reversion", "pullback_resume", "fib_pullback", "breakout_trend"):
            self.assertIn(st, flat)
        m = re.search(r'id="backtestParamsBody"(.*?)id="backtestRunBtn"', self.h, re.S)
        self.assertIsNotNone(m, "前提：找到參數本體")
        body = m.group(1)
        # 這段從參數本體的 id 開始、到執行按鈕前：少算本體跟外層群組 2 個開頭，多算按鈕外框 1 個開頭
        self.assertEqual(body.count("<div"), body.count("</div>") - 1, "區塊包起來之後 div 還是成對")
        for fid in ("backtestFpEntry", "backtestBoChannel", "backtestPrMode"):
            grp_start = body.index(f'id="{fid}"')
            last_open = body.rfind('class="bt-strat-group"', 0, grp_start)
            self.assertGreater(last_open, -1, f"{fid} 在策略專屬區塊裡")

    def test_period_presets_and_pair(self):
        self.assertIn('<option value="two_halves" selected>', self.h)
        two = re.search(r"if\(v === 'two_halves'\) return \[(.*?)\];", self.h)
        self.assertIsNotNone(two, "前提：找到「前後兩個半年」")
        self.assertEqual(two.group(1).count("days: 30, windows: 6"), 2, "兩個任務：到現在、到 180 天前")
        self.assertIn("end_date: btHalfEnd()", two.group(1))
        self.assertIn("d.setUTCDate(d.getUTCDate() - 180)", self.h)
        self.assertIn("localStorage.setItem(BT_PAIR_KEY", self.h)
        self.assertIn('<div id="backtestPairCompare"></div>', self.h)
        self.assertIn("g.mt5_profit_factor >= 1.15", self.h, "對照表的標準跟事先定的一樣")

    def test_folding(self):
        self.assertIn('<details id="backtestAdvanced" class="bt-adv">', self.h)
        self.assertIn('class="bt-fold" onclick="event.stopPropagation()"', self.h, "段落卡的「更多」不觸發點開整段")
        for t in ("進出場診斷(多空、消息震盪、被洗、進場特徵)", "依開倉時日線結構分組", "出場後多快再進場"):
            self.assertIn(f"btFoldSection('{t}'", self.h)
        self.assertIn("btFormSync();   // r122", self.h, "載入引擎參數後也要同步顯示")


if __name__ == "__main__":
    unittest.main()
