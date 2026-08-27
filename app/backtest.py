"""
歷史回測模組。

用途：抓Binance期貨XAUUSDT的歷史K線資料，還原成逐筆成交格式，
按時間順序重播，套用跟即時模擬單完全相同的訊號邏輯(signal_engine)
和交易規則(trading_core)，快速驗證這套策略在過去一段時間表現如何，
不用像即時模擬單一樣乾等好幾天才能累積到有意義的樣本數。

重要設計：Walk-forward重播，避免look-ahead bias(用到未來資料)。
每一步只用「當下時間點為止」的歷史資料去計算訊號，不會偷看後面的價格
才回頭決定進場，這樣回測結果才有參考價值。

資料還原的限制：Binance K線本身沒有逐筆明細，這裡用開高低收四個價位
各自帶1/4成交量，還原成4筆「合成成交」塞回trades清單，讓後續能沿用
既有的build_candles/compute_volume_profile邏輯，不用另外寫一套。
這是近似值，分價量表的精細度會比即時模式(用真實逐筆成交)粗糙一些，
但足夠用來抓策略的大方向表現。
"""

import bisect
import logging
from datetime import datetime, timezone

import requests

from app.signal_engine import compute_signal_from_trades, DEFAULT_STRATEGY_TYPE
from app import trading_core
from app import settings as settings_module
from app.trading_stats import compute_stats, assess_readiness

logger = logging.getLogger("backtest")

BINANCE_KLINES_URL = "https://fapi.binance.com/fapi/v1/klines"
MAX_KLINES_PER_REQUEST = 1500

DEFAULT_INTERVAL_SECONDS = 60
DEFAULT_BUCKET_SIZE = 1.0
DEFAULT_TRADE_LIMIT = 3000

# 回測「逐步重播」用的資料窗口大小，刻意跟即時資料流的CHAN_LOOKBACK_TRADES分開設定，
# 不要import共用同一個常數。原因：即時資料流的100000是為了「連續運作、真實時間
# 跨度」而設(見signal_engine.py)，但回測是每一步都要重新算一次纏論，窗口越大、
# 單步成本越高，30天回測有上千步，等於把這個放大成本乘了上千遍，實測會拖到
# 30-60秒以上，加上真實部署還要跟Binance來回抓K線，容易在手機瀏覽器/反向代理
# 逾時前跑不完(修正記錄見README)。20000是先前已經驗證過「7天回測15秒內」的
# 安全值，回測本身用合成成交重播，不需要跟即時5分K/15分K一樣長的真實時間跨度。
BACKTEST_CHAN_WINDOW_TRADES = 20000

MAX_BACKTEST_DAYS = 30  # 天數上限，從7天拉長到30天，為之後測試更長週期(例如1小時K)預留空間。
                        # 運算時間本身不會因為天數變長而爆炸(TARGET_STEP_COUNT的取樣間隔機制
                        # 會自動控制重播步數)，唯一會變長的是抓歷史K線的階段(要打更多次Binance
                        # API分頁請求)，這段是網路等待、不是佔用CPU運算，不會卡住伺服器
                        # (回測本來就是丟到背景執行緒跑，見main.py的asyncio.to_thread)。
TARGET_STEP_COUNT = 1200  # 重播步數的目標上限，天數越長會自動拉大取樣間隔(stride)來控制在這附近


def fetch_historical_klines(symbol="XAUUSDT", interval="1m", days=2):
    """
    分頁抓取Binance期貨歷史K線，回傳由舊到新排序的原始K線資料。
    公開市場資料，不需要API Key。
    """
    days = min(days, MAX_BACKTEST_DAYS)
    end_time = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_time = end_time - days * 24 * 60 * 60 * 1000

    all_klines = []
    cursor = start_time

    while cursor < end_time:
        params = {
            "symbol": symbol,
            "interval": interval,
            "startTime": cursor,
            "endTime": end_time,
            "limit": MAX_KLINES_PER_REQUEST,
        }
        resp = requests.get(BINANCE_KLINES_URL, params=params, timeout=15)
        resp.raise_for_status()
        batch = resp.json()

        if not batch:
            break

        all_klines.extend(batch)

        last_open_time = batch[-1][0]
        if last_open_time <= cursor:
            break  # 保險：避免因為API回傳異常造成無窮迴圈
        cursor = last_open_time + 1

        if len(batch) < MAX_KLINES_PER_REQUEST:
            break  # 這批資料不滿，代表已經抓到最新的了

    return all_klines


def klines_to_synthetic_trades(klines):
    """
    把K線(open_time, open, high, low, close, volume, ...)還原成逐筆成交近似值。
    每根K線拆成開/高/低/收四個時間點的合成成交，時間平均分配在該根K線的區間內，
    確保還原後的trades清單仍然是時間遞增排序。
    """
    trades = []
    for k in klines:
        open_time = int(k[0])
        close_time = int(k[6])
        o, h, l, c, v = float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])

        span = max(close_time - open_time, 4)
        qty_each = v / 4 if v > 0 else 0.0001  # 完全沒成交量的K線給極小值，避免分價量表除零

        trades.append({"time": open_time, "price": o, "qty": qty_each})
        trades.append({"time": open_time + span // 3, "price": h, "qty": qty_each})
        trades.append({"time": open_time + span * 2 // 3, "price": l, "qty": qty_each})
        trades.append({"time": close_time, "price": c, "qty": qty_each})

    trades.sort(key=lambda t: t["time"])
    return trades


def run_backtest(
    days=2,
    symbol="XAUUSDT",
    interval_seconds=DEFAULT_INTERVAL_SECONDS,
    bucket_size=DEFAULT_BUCKET_SIZE,
    trade_limit=DEFAULT_TRADE_LIMIT,
    sl_points=None,
    trail_trigger_points=None,
    trail_distance_points=None,
    reversal_confirm_count=None,
    use_atr=None,
    atr_sl_multiplier=None,
    atr_trigger_multiplier=None,
    atr_trail_multiplier=None,
    use_chop_filter=None,
    chop_threshold=None,
    strategy_type=None,
    resonance_min_conditions=4,
):
    """
    執行完整回測流程：抓歷史資料 -> 還原成成交 -> 逐根K線重播 -> 套用交易規則 -> 統計績效。
    回傳格式跟 /paper-trading/summary 一致，方便dashboard共用同一套渲染邏輯。

    效能設計重點(修正記錄，見README)：
    - 用bisect在已排序的trades時間清單上做二分搜尋定位每一步的「目前為止」邊界，
      取代原本每一步都重新掃過整份trades清單的O(n^2)寫法
    - 每一步只保留最近BACKTEST_CHAN_WINDOW_TRADES筆，避免隨著回測天數增加，
      切片大小跟著無限成長。這個窗口大小刻意跟即時資料流的CHAN_LOOKBACK_TRADES
      分開設定，不要共用同一個常數(曾經共用過，導致即時資料流的窗口調大時
      意外拖垮回測效能，詳見README修正記錄)

    sl_points/trail_trigger_points/trail_distance_points/reversal_confirm_count/
    use_atr/atr_*_multiplier 沒有明確傳入(None)時，會即時從settings.py讀取目前
    生效的參數(使用者在dashboard調整過的值)，讓「不指定參數的回測」跟「即時模擬單
    目前實際在用的參數」保持一致，不會兩邊對不上。

    ATR動態停損模式：use_atr開啟時，每一步都會用「當下這個時間點的ATR x 倍數」
    重新計算停損/移動停損距離(不是整場回測固定用同一個值)，這樣才能正確模擬
    「停損距離跟著市場波動即時調整」的效果，跟即時模擬單的行為完全一致。
    ATR資料不足的步驟(回測最開始那幾步)會自動退回用固定點數。

    strategy_type不指定時用DEFAULT_STRATEGY_TYPE("chan_profile")，明確傳入
    "resonance_fvg"可以測試多條件共振+FVG這套實驗性策略——這是目前唯一能
    測試這套策略的地方，即時模擬單(paper_trading.py)完全不會用到，確保
    這個還沒驗證過的策略不會意外影響正在運作的即時系統(修正記錄見README)。
    resonance_fvg模式下，震盪濾網已經內建在訊號判斷本身裡(choppiness_index
    超過門檻直接判定中性)，不會再套用外層chan_profile專用的use_chop_filter
    設定，避免兩套濾網互相打架、門檻定義還不一致。

    resonance_min_conditions只有resonance_fvg模式才會用到：四個子條件
    (RSI/EMA-FVG/價格行為/成交量)裡要符合幾個(含)以上才給訊號，預設4是
    原本的嚴格AND邏輯，調低可以放寬門檻——用真實資料回測後發現嚴格AND
    訊號量偏少(30天僅25筆)但獲利因子/勝率數字不錯，這個參數讓使用者可以
    直接用回測比較不同門檻的訊號量/品質取捨，不用用猜的(修正記錄見README)。

    symbol讓回測可以指定任何幣安期貨合約(不只是XAUUSDT)，用來驗證這套訊號
    邏輯換到別的商品上適不適用(例如BTCUSDT)——纏論/分價量表/ATR/Choppiness
    Index這些都是純數學運算，不預設任何特定商品，理論上換商品不用改程式碼，
    但實際適不適合要看真實資料的回測結果，不能只憑理論猜測。這個參數只影響
    回測，不影響即時模擬單(即時系統仍然固定追蹤BINANCE_GOLD_SYMBOL環境變數
    指定的商品，預設XAUUSDT)。
    """
    s = settings_module.get_settings()
    if sl_points is None:
        sl_points = s["paper_sl_points"]
    if trail_trigger_points is None:
        trail_trigger_points = s["paper_trail_trigger_points"]
    if trail_distance_points is None:
        trail_distance_points = s["paper_trail_distance_points"]
    if reversal_confirm_count is None:
        reversal_confirm_count = s["paper_reversal_confirm_count"]
    if use_atr is None:
        use_atr = bool(s["paper_use_atr_stops"])
    if atr_sl_multiplier is None:
        atr_sl_multiplier = s["paper_atr_sl_multiplier"]
    if atr_trigger_multiplier is None:
        atr_trigger_multiplier = s["paper_atr_trigger_multiplier"]
    if atr_trail_multiplier is None:
        atr_trail_multiplier = s["paper_atr_trail_multiplier"]
    if use_chop_filter is None:
        use_chop_filter = bool(s["paper_use_chop_filter"])
    if chop_threshold is None:
        chop_threshold = s["paper_chop_threshold"]
    if strategy_type is None:
        strategy_type = DEFAULT_STRATEGY_TYPE

    klines = fetch_historical_klines(symbol=symbol, days=days)
    if not klines:
        return {"error": "抓不到歷史K線資料，請稍後再試"}

    trades = klines_to_synthetic_trades(klines)
    trade_times = [t["time"] for t in trades]  # 給bisect搜尋用的平行時間清單

    # 以每根K線的收盤時間為一個重播步驟，跟即時模式「每次檢查訊號」的頻率概念一致。
    # 天數越長，K線數越多，全部逐根重播會讓運算時間暴增(纏論分析是K棒數量的函數，
    # 重播步數又跟K線數量同步成長，兩者疊加會讓耗時遠超過HTTP請求能負擔的時間)。
    # 用stride(取樣間隔)把總重播步數控制在TARGET_STEP_COUNT附近：天數短時每根K線
    # 都檢查(stride=1)，天數長時跳著檢查，犧牲一些精確度換取能在合理時間內跑完。
    all_step_times = sorted({int(k[6]) for k in klines})
    stride = max(1, len(all_step_times) // TARGET_STEP_COUNT)
    step_times = all_step_times[::stride]

    position = None
    closed_trades = []

    for step_time in step_times:
        # 二分搜尋定位「這個時間點為止」的邊界，取代線性掃描，避免look-ahead bias
        cutoff_index = bisect.bisect_right(trade_times, step_time)
        if cutoff_index < 20:  # 資料太少，跳過這一步(通常是回測最一開始的幾步)
            continue

        # 只取最近BACKTEST_CHAN_WINDOW_TRADES筆(跟即時資料流的窗口大小分開設定)，
        # 避免切片大小隨著回測進度不斷成長拖慢速度
        window_start = max(0, cutoff_index - BACKTEST_CHAN_WINDOW_TRADES)
        trades_so_far = trades[window_start:cutoff_index]

        current_price = trades_so_far[-1]["price"]

        result = compute_signal_from_trades(
            trades_so_far,
            interval_seconds=interval_seconds,
            bucket_size=bucket_size,
            trade_limit=trade_limit,
            current_price=current_price,
            strategy_type=strategy_type,
            resonance_min_conditions=resonance_min_conditions,
        )

        # ATR動態停損模式：每一步都用「當下的ATR x 倍數」重新計算距離，
        # 而不是整場回測固定用同一個值，才能正確模擬跟即時模擬單一致的行為
        atr = result.get("atr")
        if use_atr and atr:
            step_sl_points = atr * atr_sl_multiplier
            step_trail_trigger_points = atr * atr_trigger_multiplier
            step_trail_distance_points = atr * atr_trail_multiplier
        else:
            step_sl_points = sl_points
            step_trail_trigger_points = trail_trigger_points
            step_trail_distance_points = trail_distance_points

        # resonance_fvg策略專用：9EMA動態防守出場，current_ema9=None時
        # check_exit()完全不會啟用這個判斷，chan_profile模式維持原有行為不變
        current_ema9 = None
        if strategy_type == "resonance_fvg" and result.get("emas"):
            current_ema9 = result["emas"].get(9)

        if position:
            trading_core.update_trailing_stop(position, current_price, step_trail_trigger_points, step_trail_distance_points)
            exit_reason = trading_core.check_exit(
                position, current_price, result["stage"], result["direction"],
                reversal_confirm_count=reversal_confirm_count,
                current_ema9=current_ema9,
            )
            if exit_reason:
                exit_time_iso = datetime.fromtimestamp(step_time / 1000, tz=timezone.utc).isoformat()
                closed = trading_core.close_position(position, current_price, exit_reason, exit_time_iso)
                closed_trades.append(closed)
                position = None

        if position is None and result["stage"] == "訊號" and result["direction"]:
            # 震盪濾網：開啟時，偵測到當下這個時間點是震盪盤就跳過這次進場機會，
            # 現有部位不受影響(這段邏輯在position為None時才會跑，本來就只影響
            # 新開倉，不影響出場判斷)。choppiness_index資料不足時不擋單。
            # resonance_fvg策略的震盪濾網已經內建在訊號判斷本身裡，這裡不重複套用
            # chan_profile專用的use_chop_filter設定，避免兩套濾網門檻不一致互相打架。
            choppiness_index = result.get("choppiness_index")
            is_choppy = (
                strategy_type != "resonance_fvg"
                and use_chop_filter
                and choppiness_index is not None
                and choppiness_index >= chop_threshold
            )
            if not is_choppy:
                entry_time_iso = datetime.fromtimestamp(step_time / 1000, tz=timezone.utc).isoformat()
                position = trading_core.open_position(
                    direction=result["direction"],
                    current_price=current_price,
                    entry_time=entry_time_iso,
                    sl_points=step_sl_points,
                    chan_reason=result["chan"]["reason"],
                    profile_reason=result["profile"]["reason"],
                )

    stats = compute_stats(closed_trades)
    readiness = assess_readiness(stats)

    return {
        **stats,
        "open_position_at_end": position,  # 回測結束時如果還有未平倉部位，僅供參考，不計入統計
        # 回傳全部已平倉交易(不像即時模擬單那樣只給最近N筆)，因為回測的總筆數
        # 本身就有上限(受重播步數的取樣間隔控制，見TARGET_STEP_COUNT)，不會像
        # 即時模擬單一樣無限累積。之前這裡限制只回傳最近100筆，導致天數長、
        # 筆數多的回測(例如7天224筆)看不到最大回撤發生的那段期間的交易紀錄
        # (因為那段時間不在「最近100筆」範圍內)，統計數字跟看得到的紀錄對不上，
        # 這裡修正成回傳全部，讓使用者能對照到任何時間點的交易明細。
        "recent_trades": sorted(closed_trades, key=lambda t: t["exit_time"], reverse=True),
        "readiness": readiness,
        "backtest_days": days,
        "kline_count": len(klines),
        "synthetic_trade_count": len(trades),
        "replay_step_count": len(step_times),
        "replay_stride": stride,  # 1代表每根K線都檢查，>1代表跳著檢查(天數長時的效能取捨)
        "sl_points": sl_points,
        "trail_trigger_points": trail_trigger_points,
        "trail_distance_points": trail_distance_points,
        "reversal_confirm_count": reversal_confirm_count,
        "use_atr": use_atr,
        "atr_sl_multiplier": atr_sl_multiplier,
        "atr_trigger_multiplier": atr_trigger_multiplier,
        "atr_trail_multiplier": atr_trail_multiplier,
        "use_chop_filter": use_chop_filter,
        "chop_threshold": chop_threshold,
        "strategy_type": strategy_type,
        "resonance_min_conditions": resonance_min_conditions,
        "symbol": symbol,
    }
