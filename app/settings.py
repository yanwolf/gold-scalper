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

logger = logging.getLogger("settings")

_lock = threading.Lock()
_loaded_from_db = False

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
}

_settings = {key: meta["default"] for key, meta in FIELD_META.items()}


def _cast(key, raw_value):
    caster = float if FIELD_META[key]["type"] == "float" else int
    return caster(raw_value)


def _load_from_db():
    """服務啟動後第一次呼叫get_settings()/update_settings()時，把資料庫裡存的值蓋過環境變數預設值。"""
    global _loaded_from_db
    if _loaded_from_db:
        return

    from app import db
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
    _loaded_from_db = True


def get_settings():
    """回傳目前生效的完整設定(dict)，數值已經是正確型別(float/int)。"""
    _load_from_db()
    with _lock:
        return dict(_settings)


def get_settings_with_meta():
    """給dashboard設定面板用：回傳目前值 + 每個欄位的說明定義，前端可以直接動態產生表單。"""
    return {"values": get_settings(), "meta": FIELD_META}


def update_settings(updates: dict):
    """
    updates是 {key: new_value}(new_value可以是字串或數字，這裡會自動轉型別)。
    只接受FIELD_META裡有定義的欄位，其他的靜默忽略。回傳更新後的完整設定。
    """
    _load_from_db()
    from app import db

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

            _settings[key] = casted
            applied[key] = casted

    if db.is_enabled() and applied:
        db.save_app_settings(applied)

    return dict(_settings)


def verify_password(password):
    """回傳 (是否通過, 錯誤訊息或None)。SETTINGS_PASSWORD沒設定時一律拒絕，並提示要先去設定。"""
    expected = os.getenv("SETTINGS_PASSWORD")
    if not expected:
        return False, "尚未設定 SETTINGS_PASSWORD 環境變數，請先到Zeabur設定一次密碼才能啟用線上編輯功能"
    if password != expected:
        return False, "密碼錯誤"
    return True, None
