"""
執行期可調整的策略參數設定。

用途：讓使用者不用進Zeabur後台改環境變數、重新部署，就能透過dashboard直接
調整模擬單風控參數和達標門檻。有接資料庫的話會持久化(服務重啟後設定不會
消失)，沒接資料庫則退回記憶體模式(重啟會回到環境變數的預設值)。

修改設定需要密碼保護(SETTINGS_PASSWORD環境變數)，避免任何拿到這個網址的人
都能亂改交易參數——這是使用者主動要求加上的保護機制。密碼本身還是要先在
Zeabur設定過一次，之後所有的參數調整才能完全在網頁上完成，不用再進後台。

FIELD_META是每個設定項的說明(標籤、類型、範圍、給使用者看的中文說明)，
dashboard的設定面板直接讀這份定義動態產生表單，不用在前後端各寫一份重複的文字。
"""

import os
import threading
import logging
from datetime import datetime, timezone

logger = logging.getLogger("settings")

_lock = threading.Lock()
_loaded_from_db = False
_last_changed_at = None  # 給dashboard標示「哪些交易是新設定生效後產生的」用

# 每個設定項的定義：型別、預設值(來自環境變數，跟舊版行為相容)、
# 給使用者看的標籤和說明文字、輸入框的建議範圍(給前端input的min/max/step用)
FIELD_META = {
    "paper_sl_points": {
        "label": "初始停損 (points)",
        "type": "float", "step": 0.5, "min": 0.5, "max": 50,
        "default": float(os.getenv("PAPER_SL_POINTS", "5.0")),
        "help": "剛進場時的保護停損距離。價格反向超過這個距離就出場，避免小波動也不砍。",
    },
    "paper_trail_trigger_points": {
        "label": "移動停損觸發距離 (points)",
        "type": "float", "step": 0.5, "min": 0.5, "max": 50,
        "default": float(os.getenv("PAPER_TRAIL_TRIGGER_POINTS", "6.0")),
        "help": "獲利達到這個距離後才會「啟動」移動停損，在那之前用的是上面的初始停損。",
    },
    "paper_trail_distance_points": {
        "label": "移動停損跟隨距離 (points)",
        "type": "float", "step": 0.5, "min": 0.5, "max": 50,
        "default": float(os.getenv("PAPER_TRAIL_DISTANCE_POINTS", "5.0")),
        "help": "移動停損啟動後，永遠跟在最高(多單)/最低(空單)價後面這個距離，讓利潤隨趨勢延伸。",
    },
    "paper_reversal_confirm_count": {
        "label": "訊號反轉確認次數",
        "type": "int", "step": 1, "min": 1, "max": 5,
        "default": int(os.getenv("PAPER_REVERSAL_CONFIRM_COUNT", "2")),
        "help": "要連續看到幾次反向訊號才真的出場，避免訊號瞬間閃爍一次就被洗出場。設1等於沒有確認機制。",
    },
    "readiness_min_trades": {
        "label": "達標門檻：最少樣本數",
        "type": "int", "step": 1, "min": 1, "max": 500,
        "default": int(os.getenv("READINESS_MIN_TRADES", "30")),
        "help": "模擬單/回測至少要有這麼多筆已平倉交易，樣本數不足時評估沒有意義。",
    },
    "readiness_min_win_rate": {
        "label": "達標門檻：最低勝率 (%)",
        "type": "float", "step": 1, "min": 0, "max": 100,
        "default": float(os.getenv("READINESS_MIN_WIN_RATE", "40")),
        "help": "低於這個勝率不算達標。",
    },
    "readiness_min_profit_factor": {
        "label": "達標門檻：最低獲利因子",
        "type": "float", "step": 0.1, "min": 0.1, "max": 10,
        "default": float(os.getenv("READINESS_MIN_PROFIT_FACTOR", "1.3")),
        "help": "獲利因子 = 總獲利 ÷ 總虧損，低於這個值不算達標。1.0代表打平。",
    },
    "readiness_max_drawdown_points": {
        "label": "達標門檻：最大可接受回撤 (points)",
        "type": "float", "step": 1, "min": 1, "max": 500,
        "default": float(os.getenv("READINESS_MAX_DRAWDOWN_POINTS", "30")),
        "help": "從歷史高點回落最多的那一段(points)超過這個值就不算達標，衡量連續虧損的嚴重程度。",
    },
    "paper_use_atr_stops": {
        "label": "停損計算方式 (0 = 固定點數 / 1 = ATR動態)",
        "type": "int", "step": 1, "min": 0, "max": 1,
        "default": int(os.getenv("PAPER_USE_ATR_STOPS", "0")),
        "help": "設0：停損/移動停損用下面的固定點數設定(不吃ATR)。"
                "設1：改用ATR(近期實際波動幅度) x 倍數動態計算，取代固定點數，"
                "跟著市場波動自動放寬/收緊。可以搭配參數掃描比較兩種方式哪個表現比較好。",
    },
    "paper_atr_sl_multiplier": {
        "label": "ATR初始停損倍數",
        "type": "float", "step": 0.1, "min": 0.5, "max": 5,
        "default": float(os.getenv("PAPER_ATR_SL_MULTIPLIER", "1.5")),
        "help": "初始停損 = ATR x 這個倍數(只有上面的ATR開關打開時才生效)。",
    },
    "paper_atr_trigger_multiplier": {
        "label": "ATR移動停損觸發倍數",
        "type": "float", "step": 0.1, "min": 0.5, "max": 5,
        "default": float(os.getenv("PAPER_ATR_TRIGGER_MULTIPLIER", "1.5")),
        "help": "移動停損觸發距離 = ATR x 這個倍數(只有上面的ATR開關打開時才生效)。",
    },
    "paper_atr_trail_multiplier": {
        "label": "ATR移動停損跟隨倍數",
        "type": "float", "step": 0.1, "min": 0.5, "max": 5,
        "default": float(os.getenv("PAPER_ATR_TRAIL_MULTIPLIER", "1.2")),
        "help": "移動停損跟隨距離 = ATR x 這個倍數，通常比初始停損倍數略小一點"
                "(只有上面的ATR開關打開時才生效)。",
    },
    "paper_use_chop_filter": {
        "label": "震盪濾網 (0 = 關閉 / 1 = 開啟)",
        "type": "int", "step": 1, "min": 0, "max": 1,
        "default": int(os.getenv("PAPER_USE_CHOP_FILTER", "0")),
        "help": "開啟後，偵測到目前是震盪盤(用Choppiness Index判斷)時會暫停開新倉，"
                "不管震盪發生在哪個時段都能即時反應，不是綁定固定時間。"
                "已經開倉的部位不受影響，出場規則照常運作。",
    },
    "paper_chop_threshold": {
        "label": "震盪濾網門檻 (Choppiness Index)",
        "type": "float", "step": 1, "min": 30, "max": 100,
        "default": float(os.getenv("PAPER_CHOP_THRESHOLD", "61.8")),
        "help": "Choppiness Index超過這個值就判定為震盪盤、暫停開新倉(只有上面的"
                "濾網開關打開時才生效)。數值介於0~100，數字越高代表越震盪，"
                "61.8是業界常見的預設門檻。",
    },
    "execution_engine_index": {
        "label": "真實下單引擎A (0=全部純模擬 / 1=1分K纏論 / 2=5分K纏論 / 3=15分K纏論 / 4=1分K共振)",
        "type": "int", "step": 1, "min": 0, "max": 4,
        "default": int(os.getenv("EXECUTION_ENGINE_INDEX", "0")),
        "help": "被指定的引擎，開倉/平倉時才會同步在幣安期貨(依BINANCE_USE_TESTNET"
                "決定測試網或正式環境)送出對應的市價單，其他引擎維持純模擬、不會下單。"
                "改用引擎編號取代原本的『週期』設定，因為現在同一個K線週期可能有多個"
                "策略的引擎平行運作(例如1分K纏論跟1分K共振都是60秒週期)，光用週期已經"
                "無法唯一指定要哪一個。設0代表這個槽位不綁任何引擎。只能填0~4這五個值，"
                "填其他數字等於沒有任何引擎會被觸發(安全的失敗模式)。"
                "搭配下面的「真實下單引擎B」可以同時綁兩個引擎——但兩個引擎必須用"
                "『不同的幣安帳戶』(每個引擎在paper_trading.py裡各自綁定的帳戶名稱)，"
                "不然同一個帳戶會發生部位互相抵銷的問題。",
    },
    "execution_engine_index_2": {
        "label": "真實下單引擎B (0=不綁 / 1=1分K纏論 / 2=5分K纏論 / 3=15分K纏論 / 4=1分K共振)",
        "type": "int", "step": 1, "min": 0, "max": 4,
        "default": int(os.getenv("EXECUTION_ENGINE_INDEX_2", "0")),
        "help": "第二個真實下單槽位，用途是讓兩個引擎同時做真實下單(例如15分K跟1分K"
                "同時跑，加快累積滑價統計資料)。跟引擎A設成同一個編號沒有意義(等於"
                "只綁一個)。重要：兩個槽位綁的引擎必須各自使用『不同的幣安帳戶』——"
                "1分K纏論引擎綁定的帳戶是「gold_1m」(讀取BINANCE_API_KEY_GOLD_1M/"
                "BINANCE_API_SECRET_GOLD_1M)，其他引擎是「gold」(讀取原本的"
                "BINANCE_API_KEY/BINANCE_API_SECRET)，兩個帳戶的金鑰都要在Zeabur"
                "設定好才能同時運作。同一個幣安帳戶被兩個引擎同時下單會發生部位"
                "互相抵銷、平倉對不上的問題(修正記錄見README)。",
    },
    "execution_quantity": {
        "label": "真實下單口數 (XAU張數)",
        "type": "float", "step": 0.1, "min": 0.001, "max": 1000,
        "default": float(os.getenv("EXECUTION_QUANTITY", "1.0")),
        "help": "只有上面指定的那個真實下單週期才會用到。這是每筆單直接下的固定"
                "數量，不再從風險金額反推——因為ATR動態停損模式下每筆的停損距離"
                "都不一樣，反推出來的數量會跟著浮動、難以預期。改成固定數量後，"
                "部位大小穩定可預期，風險則誠實隨市場波動大小浮動。可以先用"
                "dashboard的「部位風險試算」功能，輸入候選數量看對應的風險金額/"
                "風險百分比，自己決定要填多少再存進這裡。",
    },
    "execution_leverage": {
        "label": "真實下單槓桿倍數",
        "type": "int", "step": 1, "min": 1, "max": 125,
        "default": int(os.getenv("EXECUTION_LEVERAGE", "10")),
        "help": "每次真實開倉前，系統會自動呼叫幣安API確認/設定這個槓桿倍數"
                "(不再需要手動按「設定槓桿」按鈕才會生效)。如果設定槓桿失敗，"
                "這筆單會直接放棄下單(視為執行失敗)，不會用不確定的槓桿下單。"
                "注意：槓桿只影響這筆部位要墊多少保證金，不影響每點的損益"
                "金額(那是由下單口數決定)，可以用dashboard的「部位試算」工具"
                "分別看數量和槓桿的效果。",
    },
    "execution_margin_type": {
        "label": "保證金模式 (0 = 逐倉 / 1 = 全倉)",
        "type": "int", "step": 1, "min": 0, "max": 1,
        "default": int(os.getenv("EXECUTION_MARGIN_TYPE", "0")),
        "help": "每次真實開倉前，系統會自動呼叫幣安API確認/設定這個模式。逐倉"
                "(預設)：這筆部位有自己專屬的保證金，就算被強制平倉也只會虧掉"
                "分配給這筆單的保證金，不會牽連帳戶其他資金，比較符合這個系統"
                "「每筆單風險都要能事先算清楚」的設計精神。全倉：整個帳戶餘額"
                "共用當保證金池，能撐住單一部位不利時不被強平，但代價是可能"
                "拖累到整個帳戶的資金。如果設定失敗，這筆單會直接放棄下單。",
    },
    "execution_assumed_spread_points": {
        "label": "假設買賣價差成本 (points，用於績效統計調整+進場前安全邊際檢查)",
        "type": "float", "step": 0.5, "min": 0, "max": 200,
        "default": float(os.getenv("EXECUTION_ASSUMED_SPREAD_POINTS", "0")),
        "help": "市價單開倉+平倉各會吃到大約半個買賣價差，兩者加起來大約等於"
                "一個完整價差，這是不管每筆單賺賠都躲不掉的固定交易成本——"
                "跟ATR停損距離是兩件事，停損是價格走錯方向的防線，這個是"
                "交易成本，不該混在一起、也不該讓停損變寬去『補償』它。"
                "設定這個值之後，dashboard的模擬單績效和回測結果會同時顯示"
                "「原始統計」和「扣掉這個價差成本後」兩組數字；同時也會用在"
                "下面的「最小安全邊際倍數」檢查，決定每筆訊號當下值不值得真的"
                "送真錢下單。預設0代表兩項功能都不生效(維持原本行為)，建議先用"
                "「查詢目前部位」工具實際觀察幾筆真實成交的價差大小，再填一個"
                "貼近實際觀察值的數字進來。",
    },
    "execution_min_edge_ratio": {
        "label": "真實下單最小安全邊際倍數 (停損距離 ÷ 價差成本)",
        "type": "float", "step": 0.5, "min": 0, "max": 20,
        "default": float(os.getenv("EXECUTION_MIN_EDGE_RATIO", "0")),
        "help": "每次真實開倉前，檢查這筆單的停損距離是不是至少是「假設買賣"
                "價差成本」的這個倍數——如果ATR偏低導致停損距離太小、價差"
                "成本佔比過高，代表這個時段送真錢下單划不來(就算訊號完全"
                "正確，觸及停損時的實際虧損也會被價差放大將近一倍)，會自動"
                "跳過這次真實下單，模擬單照常記錄不受影響，等波動度回升、"
                "安全邊際重新充足時才恢復。例如設2.0，代表停損距離至少要是"
                "價差成本的2倍才會真的下單。預設0代表不檢查(維持原本行為)，"
                "跟「假設買賣價差成本」任一個是0，這道檢查都不會生效——建議"
                "兩個都設定好之後再一起使用，不要只設定其中一個。這是用「假設的"
                "固定值」跟ATR比，是間接的代理指標；如果想直接看「當下」真實"
                "價差夠不夠窄，請搭配下面的「真實下單最大可接受價差」一起用。",
    },
    "execution_max_spread_points": {
        "label": "真實下單最大可接受即時價差 (points)",
        "type": "float", "step": 0.5, "min": 0, "max": 100,
        "default": float(os.getenv("EXECUTION_MAX_SPREAD_POINTS", "0")),
        "help": "每次真實開倉前，直接查詢決策當下的真實買賣盤口(不是假設值)，"
                "如果當下價差超過這個上限，暫停這次真實下單，模擬單照常記錄"
                "不受影響。跟上面的「最小安全邊際倍數」不同：那個是用「假設的"
                "固定價差值」去跟ATR停損距離比，是間接的代理指標；這個是直接"
                "看當下真實盤口，能抓到「這一刻價差真的瞬間變寬」的異常時刻"
                "(例如低流動性時段、大單瞬間吃掉盤口深度、重大消息公布前後)，"
                "比用固定假設值判斷更即時、更直接。例如觀察到平時真實價差穩定"
                "在0.5points內，可以設2~3當上限，價差瞬間跳到5、6points以上的"
                "異常時刻就會自動跳過。預設0代表不檢查(維持原本行為)，建議先用"
                "「幣安下單測試」面板多測幾次、觀察真實價差的正常範圍，再抓一個"
                "合理的上限。",
    },
    "execution_daily_loss_limit_usd": {
        "label": "每日虧損上限 (USD)",
        "type": "float", "step": 5, "min": 1, "max": 100000,
        "default": float(os.getenv("EXECUTION_DAILY_LOSS_LIMIT_USD", "50")),
        "help": "只有上面指定的真實下單週期才會用到。今天(UTC日)已實現虧損達到"
                "這個金額時，會暫停送出新的真實開倉單，直到隔天(UTC)重置。"
                "已經開的部位不受影響，該停損/該出場照樣正常進行——這道防線只擋"
                "「新增風險」的動作，不擋「降低風險」的動作。模擬單追蹤本身也"
                "完全不受影響，繼續正常記錄每一筆。",
    },
    "execution_max_consecutive_losses": {
        "label": "連續虧損上限 (筆數)",
        "type": "int", "step": 1, "min": 1, "max": 50,
        "default": int(os.getenv("EXECUTION_MAX_CONSECUTIVE_LOSSES", "3")),
        "help": "只有上面指定的真實下單週期才會用到。連續虧損達到這個筆數時，"
                "會暫停送出新的真實開倉單，直到出現一筆獲利為止(獲利會重置這個"
                "計數)。已經開的部位不受影響。",
    },
}

_settings = {key: meta["default"] for key, meta in FIELD_META.items()}
_LAST_CHANGED_DB_KEY = "_meta_last_changed_at"  # app_settings表裡的保留key，不是FIELD_META裡的一般設定項
_KEY_CHANGED_DB_PREFIX = "_meta_changed_at:"     # 全域設定「每個欄位各自」的最後修改時間，key格式 _meta_changed_at:<欄位>

# 引擎專屬覆寫(修正記錄見README)：四個模擬單引擎原本共用同一組策略參數，
# 1分K跟15分K的ATR量級差很多(同樣1x倍數在1分K換算出來的停損只有1~2點，
# 出場太快、利潤被滑點吃光；15分K卻活得好好的)。這裡讓每個引擎可以針對
# TRADING_RELEVANT_KEYS裡的欄位各自覆寫，沒覆寫的欄位沿用全域設定。
# 資料庫key格式：engine:<engine_id>:<欄位>，時間戳記 engine:<engine_id>:_meta_changed_at:<欄位>
_ENGINE_DB_PREFIX = "engine:"
_MANUAL_BOUNDARY_FIELD = "_meta_manual_boundary"  # engine:<id>:_meta_manual_boundary
_engine_manual_boundary = {}  # {engine_id: iso}  使用者手動「從現在起重新統計」的時間
_engine_overrides = {}        # {engine_id: {key: value}}
_engine_key_changed_at = {}   # {engine_id: {key: iso}}  覆寫或清除覆寫的時間
_global_key_changed_at = {}   # {key: iso}  全域設定每個欄位的最後修改時間

# 真正會影響模擬單交易行為的欄位。readiness_*(達標門檻)只影響「怎麼評估績效」，
# 不影響實際交易邏輯本身——改了門檻不代表舊交易是「別的規則」跑出來的，
# 所以「設定變更時間」只在這些欄位被改動時才更新，避免使用者只是調整門檻
# 卻導致歷史交易被誤判成「舊設定」而被排除在績效統計之外。
TRADING_RELEVANT_KEYS = {
    "paper_sl_points", "paper_trail_trigger_points", "paper_trail_distance_points",
    "paper_reversal_confirm_count", "paper_use_atr_stops", "paper_atr_sl_multiplier",
    "paper_atr_trigger_multiplier", "paper_atr_trail_multiplier",
    "paper_use_chop_filter", "paper_chop_threshold",
}


def _cast(key, raw_value):
    caster = float if FIELD_META[key]["type"] == "float" else int
    return caster(raw_value)


def _load_from_db():
    """服務啟動後第一次呼叫get_settings()/update_settings()時，把資料庫裡存的值蓋過環境變數預設值。"""
    global _loaded_from_db, _last_changed_at
    if _loaded_from_db:
        return

    from app import db
    migrated_engine_index = None
    if db.is_enabled():
        stored = db.get_app_settings()
        with _lock:
            for key, raw_value in stored.items():
                if key not in FIELD_META:
                    continue
                try:
                    _settings[key] = _cast(key, raw_value)
                except (TypeError, ValueError):
                    logger.warning(f"資料庫裡的設定值 {key}={raw_value} 型別轉換失敗，忽略")
            if _LAST_CHANGED_DB_KEY in stored:
                _last_changed_at = stored[_LAST_CHANGED_DB_KEY]

            # 全域設定每個欄位各自的最後修改時間 / 引擎專屬覆寫與其時間戳記
            for key, raw_value in stored.items():
                if key.startswith(_KEY_CHANGED_DB_PREFIX):
                    _global_key_changed_at[key[len(_KEY_CHANGED_DB_PREFIX):]] = raw_value
                elif key.startswith(_ENGINE_DB_PREFIX):
                    rest = key[len(_ENGINE_DB_PREFIX):]
                    engine_id, _, field = rest.partition(":")
                    if not engine_id or not field:
                        continue
                    if field == _MANUAL_BOUNDARY_FIELD:
                        _engine_manual_boundary[engine_id] = raw_value
                    elif field.startswith(_KEY_CHANGED_DB_PREFIX):
                        _engine_key_changed_at.setdefault(engine_id, {})[field[len(_KEY_CHANGED_DB_PREFIX):]] = raw_value
                    elif field in TRADING_RELEVANT_KEYS:
                        try:
                            _engine_overrides.setdefault(engine_id, {})[field] = _cast(field, raw_value)
                        except (TypeError, ValueError):
                            logger.warning(f"引擎覆寫設定 {key}={raw_value} 型別轉換失敗，忽略")

            # 一次性遷移：舊版設定欄位是execution_interval_seconds(0/60/300/900)，
            # 改版後換成execution_engine_index(0~4)，兩者欄位名稱不同，直接改名
            # 會讓使用者原本設定好的值悄悄消失、被重置回預設值0(全部純模擬)——
            # 這是實際發生過的問題，這裡自動把舊值換算成新值，使用者不用手動
            # 重新設定一次。只有在「舊欄位還留著、新欄位從來沒被存過」時才會
            # 觸發，避免蓋掉使用者已經在新欄位上做過的選擇。
            OLD_INTERVAL_TO_NEW_INDEX = {0: 0, 60: 1, 300: 2, 900: 3}
            if "execution_interval_seconds" in stored and "execution_engine_index" not in stored:
                try:
                    old_value = int(float(stored["execution_interval_seconds"]))
                    if old_value in OLD_INTERVAL_TO_NEW_INDEX:
                        migrated_engine_index = OLD_INTERVAL_TO_NEW_INDEX[old_value]
                        _settings["execution_engine_index"] = migrated_engine_index
                        logger.info(
                            f"偵測到舊版execution_interval_seconds={old_value}設定，"
                            f"自動遷移成execution_engine_index={migrated_engine_index}"
                        )
                except (TypeError, ValueError):
                    pass

        # 把遷移後的值實際存回資料庫，這樣只需要遷移這一次，之後重啟服務會
        # 直接讀到execution_engine_index這個新欄位，不用每次都重新判斷
        if migrated_engine_index is not None:
            db.save_app_settings({"execution_engine_index": migrated_engine_index})

        # 一次性遷移：舊版只有一個全域的_meta_last_changed_at，沒有「每欄位」的
        # 修改時間。把它種進每個還沒有時間戳的交易相關欄位並持久化——語意上
        # 舊版就是「所有欄位最後一次改動都在那個時間」。只做這一次，之後
        # 不再動態退回全域時間戳，否則全域任何一個欄位改動都會讓沒被碰到的
        # 欄位跟著「看起來剛改過」，導致有覆寫的引擎被錯誤重置統計。
        with _lock:
            seed = {
                k: _last_changed_at for k in TRADING_RELEVANT_KEYS
                if k not in _global_key_changed_at and _last_changed_at
            }
            _global_key_changed_at.update(seed)
        if seed:
            db.save_app_settings({f"{_KEY_CHANGED_DB_PREFIX}{k}": v for k, v in seed.items()})
    _loaded_from_db = True


def get_settings(engine_id=None):
    """
    回傳目前生效的完整設定(dict)，數值已經是正確型別(float/int)。
    engine_id有給的話，會把該引擎的專屬覆寫疊在全域設定上再回傳(只有
    TRADING_RELEVANT_KEYS能被覆寫，execution_*/readiness_*永遠是全域)。
    """
    _load_from_db()
    with _lock:
        merged = dict(_settings)
        if engine_id:
            merged.update(_engine_overrides.get(engine_id, {}))
        return merged


def get_engine_overrides(engine_id):
    """回傳某個引擎目前的專屬覆寫(只含真的被覆寫的欄位)。"""
    _load_from_db()
    with _lock:
        return dict(_engine_overrides.get(engine_id, {}))


def update_engine_overrides(engine_id, updates: dict):
    """
    設定/清除某個引擎的專屬覆寫。updates是 {欄位: 值 或 None}，值是None代表
    「清除這個欄位的覆寫、回頭沿用全域」。只接受TRADING_RELEVANT_KEYS。
    跟update_settings一樣只在「值真的有改變」時才算異動、才更新時間戳記，
    避免整份表單重送導致誤觸發績效統計的新舊分界。回傳(applied, cleared)。
    """
    _load_from_db()
    from app import db
    now = datetime.now(timezone.utc).isoformat()
    applied, cleared = {}, []
    with _lock:
        current = _engine_overrides.setdefault(engine_id, {})
        for key, value in updates.items():
            if key not in TRADING_RELEVANT_KEYS:
                continue
            if value is None or value == "":
                if key in current:
                    del current[key]
                    cleared.append(key)
                    _engine_key_changed_at.setdefault(engine_id, {})[key] = now
                continue
            try:
                casted = _cast(key, value)
            except (TypeError, ValueError):
                continue
            meta = FIELD_META[key]
            casted = max(meta["min"], min(meta["max"], casted))
            if current.get(key) == casted:
                continue
            current[key] = casted
            applied[key] = casted
            _engine_key_changed_at.setdefault(engine_id, {})[key] = now
        if not current:
            _engine_overrides.pop(engine_id, None)

    if db.is_enabled():
        if applied:
            payload = {f"{_ENGINE_DB_PREFIX}{engine_id}:{k}": v for k, v in applied.items()}
            payload.update({f"{_ENGINE_DB_PREFIX}{engine_id}:{_KEY_CHANGED_DB_PREFIX}{k}": now for k in applied})
            db.save_app_settings(payload)
        if cleared:
            db.delete_app_settings([f"{_ENGINE_DB_PREFIX}{engine_id}:{k}" for k in cleared])
            db.save_app_settings({f"{_ENGINE_DB_PREFIX}{engine_id}:{_KEY_CHANGED_DB_PREFIX}{k}": now for k in cleared})
    return applied, cleared


def reset_engine_stats_boundary(engine_id):
    """
    「從現在起重新統計」(修正記錄見README)：不改任何參數，只把這個引擎的績效
    統計分界設到現在，之後get_summary只統計這個時間點之後的交易。用途：
    修了資料層的bug(例如盤口過期導致帳面損益錯誤)之後，舊資料是髒的但參數
    沒變，原本的分界機制不會觸發；與其等髒資料被稀釋，直接手動畫一條線。
    回傳新的分界時間(ISO)。
    """
    _load_from_db()
    from app import db
    now = datetime.now(timezone.utc).isoformat()
    with _lock:
        _engine_manual_boundary[engine_id] = now
    if db.is_enabled():
        db.save_app_settings({f"{_ENGINE_DB_PREFIX}{engine_id}:{_MANUAL_BOUNDARY_FIELD}": now})
    return now


def clear_engine_overrides(engine_id):
    """清除某個引擎的全部專屬覆寫，回頭完全沿用全域設定。"""
    current = get_engine_overrides(engine_id)
    if not current:
        return []
    _, cleared = update_engine_overrides(engine_id, {k: None for k in current})
    return cleared


def get_settings_with_meta():
    """給dashboard設定面板用：回傳目前值 + 每個欄位的說明定義，前端可以直接動態產生表單。"""
    return {"values": get_settings(), "meta": FIELD_META, "last_changed_at": _last_changed_at}


def get_last_changed_at(engine_id=None):
    """
    給paper_trading.py的get_summary()用：回傳「交易相關參數」上次被實際修改的
    時間(ISO字串，從未修改過則是None)。只有TRADING_RELEVANT_KEYS裡的欄位被
    改動才會更新這個時間，單純調整達標門檻(readiness_*)不會——因為門檻只影響
    「怎麼評估績效」，不影響實際交易邏輯，舊交易依然是同一套規則跑出來的，
    不該被排除在績效統計之外。

    用來讓dashboard標示「哪些交易紀錄是新設定生效後產生的」，並讓績效統計
    只計算新設定底下的交易，避免使用者誤以為改參數後畫面/數字沒反應——
    舊的已平倉交易本來就不會被追溯修改，只有進場時間晚於這個時間點的交易
    才是新設定底下的結果。

    engine_id有給的話，算的是「這個引擎實際生效的參數」上次改變的時間：
    有覆寫的欄位只看該引擎的覆寫時間(全域改了不影響它)；沒覆寫的欄位看
    全域該欄位的修改時間(以及這個引擎清除覆寫、回頭沿用全域的時間)。
    這樣改15分K用的全域參數時，1分K如果自己有覆寫就不會被連帶重置統計
    (修正記錄見README)。
    """
    if engine_id is None:
        return _last_changed_at
    _load_from_db()
    with _lock:
        ov = _engine_overrides.get(engine_id, {})
        ek = _engine_key_changed_at.get(engine_id, {})
        times = []
        for key in TRADING_RELEVANT_KEYS:
            if ek.get(key):
                times.append(ek[key])
            if key not in ov:
                t = _global_key_changed_at.get(key)
                if t:
                    times.append(t)
        manual = _engine_manual_boundary.get(engine_id)
        if manual:
            times.append(manual)
        return max(times) if times else None


def update_settings(updates: dict):
    """
    updates是 {key: new_value}(new_value可以是字串或數字，這裡會自動轉型別)。
    只接受FIELD_META裡有定義的欄位，其他的靜默忽略。回傳更新後的完整設定。
    """
    _load_from_db()
    from app import db

    global _last_changed_at

    applied = {}
    with _lock:
        for key, value in updates.items():
            if key not in FIELD_META:
                continue
            try:
                casted = _cast(key, value)
            except (TypeError, ValueError):
                continue

            meta = FIELD_META[key]
            # 夾在設定的合理範圍內，避免使用者手滑打進一個荒謬的數字(例如負的停損距離)
            casted = max(meta["min"], min(meta["max"], casted))

            # 只有「新值真的跟目前的值不一樣」才算真的有異動——dashboard的
            # 「儲存變更」按鈕會把整個表單所有欄位都一起送出(不是只送使用者
            # 改的那一個)，如果不做這個檢查，即使只想改execution_engine_index
            # 這種不影響交易邏輯的欄位，也會因為paper_sl_points等交易相關欄位
            # 剛好也在同一份送出的資料裡，被誤判成「這些欄位也被改動了」，
            # 錯誤觸發績效統計的新舊設定分界機制(修正記錄見README)
            if casted == _settings[key]:
                continue

            _settings[key] = casted
            applied[key] = casted

        # 只有「真正影響交易行為」的欄位被改動，才更新這個時間戳記——
        # 單純調整達標門檻(readiness_*)不算，因為那不影響交易邏輯本身，
        # 舊交易依然有效，不該被排除在績效統計之外
        changed_trading_keys = set(applied.keys()) & TRADING_RELEVANT_KEYS
        if changed_trading_keys:
            _last_changed_at = datetime.now(timezone.utc).isoformat()
            # 同時記錄「每個欄位各自」的修改時間，給引擎專屬覆寫的新舊分界計算用
            for k in changed_trading_keys:
                _global_key_changed_at[k] = _last_changed_at

    if db.is_enabled() and applied:
        db_applied = dict(applied)
        if changed_trading_keys:
            db_applied[_LAST_CHANGED_DB_KEY] = _last_changed_at
            for k in changed_trading_keys:
                db_applied[f"{_KEY_CHANGED_DB_PREFIX}{k}"] = _last_changed_at
        db.save_app_settings(db_applied)

    return dict(_settings)


def verify_password(password):
    """回傳 (是否通過, 錯誤訊息或None)。SETTINGS_PASSWORD沒設定時一律拒絕，並提示要先去設定。"""
    expected = os.getenv("SETTINGS_PASSWORD")
    if not expected:
        return False, "尚未設定 SETTINGS_PASSWORD 環境變數，請先到Zeabur設定一次密碼才能啟用線上編輯功能"
    if password != expected:
        return False, "密碼錯誤"
    return True, None
