"""
r115 的測試：進出場診斷(app/trade_diagnostics.py)——被洗出場比例、贏單最大逆向、中樞突破單有回抽／直接走。
執行：python -m unittest tests.test_trade_diagnostics -v
"""
import logging
import os
import unittest
from datetime import datetime, timezone

logging.disable(logging.CRITICAL)

from app import signal as S  # noqa: E402
from app import trade_diagnostics as D  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
T0 = 1_780_000_020_000 - (1_780_000_020_000 % 60_000)
MIN = 60_000


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def klines(prices, start=T0):
    """每分鐘一根：開=前一個價、收=這個價，高低多 0.1。"""
    out = []
    for i, p in enumerate(prices):
        o = prices[i - 1] if i else p
        out.append([start + i * MIN, str(o), str(max(o, p) + 0.1), str(min(o, p) - 0.1), str(p), "1", start + (i + 1) * MIN - 1])
    return out


def chan_result(reason, zg=4110.0, zd=4100.0):
    return {"chan": {"reason": reason}, "chan_detail": {"latest_zhongshu": {"zg": zg, "zd": zd}}}


class EntryMeta(unittest.TestCase):
    def test_uses_the_real_signal_texts(self):
        zs = {"zg": 4110.0, "zd": 4100.0}
        up = S._evaluate_chan_bias({"latest_zhongshu": zs, "bi_list": [{"end_price": 4115.0}]})
        down = S._evaluate_chan_bias({"latest_zhongshu": zs, "bi_list": [{"end_price": 4095.0}]})
        inside = S._evaluate_chan_bias({"latest_zhongshu": zs, "bi_list": [{"end_price": 4105.0}]})
        self.assertEqual((up["bias"], down["bias"]), ("bullish", "bearish"), "前提：訊號文字真的是突破／破壞")
        m_up = D.entry_meta("chan_profile", chan_result(up["reason"]), 3.0, 6.0)
        m_dn = D.entry_meta("chan_profile", chan_result(down["reason"]), 3.0, 6.0)
        self.assertEqual((m_up["kind"], m_up["level"]), ("breakout", 4110.0), "多單：被突破的是中樞上界")
        self.assertEqual((m_dn["kind"], m_dn["level"]), ("breakout", 4100.0), "空單：被跌破的是中樞下界")
        self.assertEqual(D.entry_meta("chan_profile", chan_result(inside["reason"]), 3.0, 6.0)["kind"], "other")

    def test_beichi_and_other_strategies(self):
        m = D.entry_meta("chan_profile", chan_result("上漲段出現盤整背馳，動能較前一段減弱"), 3.0, 6.0)
        self.assertEqual((m["kind"], m["level"], m["atr"], m["sl_points"]), ("beichi", None, 3.0, 6.0))
        self.assertEqual(D.entry_meta("pullback_resume", chan_result("價格站上中樞上界 1，突破確立"), 3.0, 6.0)["kind"], "n/a")
        self.assertEqual(D.entry_meta(None, {}, 3.0, 6.0)["kind"], "other", "沒有理由文字：其他")


def trade(direction, entry_i, exit_i, entry_price, exit_price, pnl, kind="other", level=None, atr=2.0, slp=4.0,
          reason="觸及停損"):
    return {"direction": direction, "entry_time": iso(T0 + entry_i * MIN), "exit_time": iso(T0 + exit_i * MIN),
            "entry_price": entry_price, "exit_price": exit_price, "pnl_points": pnl, "exit_reason": reason,
            "diag": {"atr": atr, "sl_points": slp, "kind": kind, "level": level}}


class Annotate(unittest.TestCase):
    def test_washed_short_then_falls(self):
        # 空 100 進場、反彈到 104 停損，之後 4 小時內跌到 95(= 100 - 2×2 ATR 以下)
        prices = [100] * 10 + [102, 104] + [103] * 20 + [96, 95] + [95] * 300
        t = trade("bearish", 9, 11, 100.0, 104.0, -4.0)
        D.annotate([t], klines(prices))
        self.assertIs(t["diag_after_favorable"], True)

    def test_not_washed_when_price_keeps_going(self):
        prices = [100] * 10 + [102, 104] + [106] * 300
        t = trade("bearish", 9, 11, 100.0, 104.0, -4.0)
        D.annotate([t], klines(prices))
        self.assertIs(t["diag_after_favorable"], False)

    def test_not_enough_data_is_none(self):
        prices = [100] * 10 + [102, 104] + [103] * 30
        t = trade("bearish", 9, 11, 100.0, 104.0, -4.0)
        D.annotate([t], klines(prices))
        self.assertIn("diag_mae_ratio", t, "前提：這筆有被診斷到")
        self.assertIsNone(t["diag_after_favorable"])

    def test_winner_mae_ratio(self):
        # 多 100 進場、先跌到 97(含影線 96.9，逆向 3.1)、再漲到 110 出場；初始停損距離 4 → 0.775
        prices = [100] * 5 + [98, 97, 99, 105, 110]
        t = trade("bullish", 4, 9, 100.0, 110.0, 10.0, slp=4.0, reason="觸及移動停損")
        D.annotate([t], klines(prices))
        self.assertAlmostEqual(t["diag_mae_ratio"], round(3.1 / 4.0, 3))
        self.assertGreater(t["diag_mfe_ratio"], 2.0)
        self.assertNotIn("diag_after_favorable", t, "賺錢的單不判斷被洗")

    def test_breakout_retest_vs_runaway(self):
        # 多單突破中樞上界 110：A 回測到 110 以下；B 一路走
        a_prices = [111] * 3 + [110.5, 109.8, 112] + [113] * 130
        b_prices = [111] * 3 + [112, 113, 114] + [115] * 130
        ta = trade("bullish", 2, 100, 111.0, 113.0, 2.0, kind="breakout", level=110.0)
        tb = trade("bullish", 2, 100, 111.0, 115.0, 4.0, kind="breakout", level=110.0)
        D.annotate([ta], klines(a_prices))
        D.annotate([tb], klines(b_prices))
        self.assertEqual((ta["diag_retest"], tb["diag_retest"]), (True, False))
        short = trade("bullish", 2, 5, 111.0, 112.0, 1.0, kind="breakout", level=110.0)
        D.annotate([short], klines([111] * 30))
        self.assertIsNone(short["diag_retest"], "資料不到 2 小時：不判斷")

    def test_empty_klines_noop(self):
        t = trade("bullish", 0, 1, 1.0, 2.0, 1.0)
        self.assertEqual(D.annotate([t], []), [t])
        self.assertNotIn("diag_mae_ratio", t)


class Summarize(unittest.TestCase):
    def test_groups(self):
        ts = [
            dict(trade("bearish", 0, 1, 1, 1, -4.0), diag_after_favorable=True),
            dict(trade("bearish", 0, 1, 1, 1, -3.0, reason="訊號反轉"), diag_after_favorable=False),
            dict(trade("bearish", 0, 1, 1, 1, -2.0), diag_after_favorable=None),
            dict(trade("bullish", 0, 1, 1, 1, 10.0, kind="breakout", level=1.0), diag_mae_ratio=0.8, diag_retest=False),
            dict(trade("bullish", 0, 1, 1, 1, 5.0, kind="breakout", level=1.0), diag_mae_ratio=0.2, diag_retest=True),
            dict(trade("bullish", 0, 1, 1, 1, -6.0, kind="breakout", level=1.0), diag_after_favorable=False, diag_retest=True),
            dict(trade("bearish", 0, 1, 1, 1, 1.0), diag_mae_ratio=1.2),   # 逆向超過停損距離才回頭(停損在K線內沒被碰到的極端情況)
            {"pnl_points": 99.0},   # 沒有 diag：不算
        ]
        s = D.summarize(ts)
        w = s["washed"]
        self.assertEqual((w["losers"], w["judged"], w["washed"], w["washed_pct"], w["washed_loss_points"]),
                         (4, 3, 1, 33.3, -4.0))
        self.assertEqual(w["by_exit"]["停損"], {"judged": 2, "washed": 1})
        m = s["winner_mae"]
        self.assertEqual((m["winners"], m["near_stop_pct"], m["buckets"]["<25%"], m["buckets"]["75~100%"], m["buckets"]["≥100%"]),
                         (3, 66.7, 1, 1, 1))
        b = s["breakout"]
        self.assertEqual((b["retest"]["trades"], b["retest"]["total_pnl_points"]), (2, -1.0))
        self.assertEqual((b["runaway"]["trades"], b["runaway"]["biggest_win"]), (1, 10.0))
        self.assertEqual(s["kinds"]["breakout"]["trades"], 3)
        self.assertIsNone(D.summarize([{"pnl_points": 1.0}]), "沒有 diag 的交易：沒有診斷")
        bd = s["by_direction"]                       # r121：多單／空單分開
        self.assertEqual((bd["bullish"]["trades"], bd["bullish"]["total_pnl_points"]), (3, 9.0))
        self.assertEqual((bd["bearish"]["trades"], bd["bearish"]["total_pnl_points"]), (4, -8.0))


class Wiring(unittest.TestCase):
    def test_backtest_result_and_positions_have_diag(self):
        from tests.test_pullback_resume import _run, BAND_PATH
        res = _run(path=BAND_PATH, pr_mode=2, pr_exit_mode=1, pr_sides=1)
        self.assertGreater(res["total_trades"], 0, "前提：有交易")
        t = res["recent_trades"][0]
        self.assertEqual(t["diag"]["kind"], "n/a", "非纏論策略不分種類")
        self.assertIn("diag_mae_ratio", t)
        self.assertIn("n/a", res["entry_diagnostics"]["kinds"])

    def test_job_aggregate(self):
        from app import backtest_jobs as J
        ts = [dict(trade("bullish", 0, 1, 1, 1, 3.0, kind="breakout", level=1.0), diag_retest=False, diag_mae_ratio=0.1)]
        ag = J._aggregate([{"recent_trades": ts}])
        self.assertEqual(ag["entry_diagnostics"]["breakout"]["runaway"]["trades"], 1)

    def test_dashboard(self):
        with open(os.path.join(ROOT, "app/static/dashboard.html"), encoding="utf-8") as f:
            h = f.read()
        self.assertIn("entryDiagnosticsHtml(data.entry_diagnostics", h)
        self.assertIn("entryDiagnosticsHtml(ag.entry_diagnostics", h)


if __name__ == "__main__":
    unittest.main()
