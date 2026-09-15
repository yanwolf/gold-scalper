"""
服務角色(修正記錄見README)：同一份程式碼用APP_ROLE環境變數切成兩種部署——

  APP_ROLE=lab  (預設) 研究端：全部模擬引擎、回測、掃描、Demo帳戶下單、可自由改參數
  APP_ROLE=live        正式端：只跑LIVE_ENGINE_IDS指定的引擎 + 下單 + 風控 + 通知，
                       回測/掃描/實驗引擎完全不載入(避免搶GIL拖慢正式單的tick)，
                       參數唯讀，只能透過「匯入參數集」(需密碼、寫審計)更新，
                       另外提供緊急停止/恢復/強制平倉的控制面板。

兩邊各自訂閱幣安行情、各自一份資料庫schema(APP_NAMESPACE)，互不影響：
lab掛了、重新部署、掃描跑滿CPU，都不會碰到live的部位管理。
"""
import os

APP_ROLE = os.getenv("APP_ROLE", "lab").strip().lower()
if APP_ROLE not in ("lab", "live"):
    APP_ROLE = "lab"

# live角色要啟動哪些引擎(逗號分隔的engine_id)，預設只跑15分K纏論引擎
LIVE_ENGINE_IDS = [x.strip() for x in os.getenv("LIVE_ENGINE_IDS", "chan_profile_900").split(",") if x.strip()]

# 資料庫schema名稱：兩個服務可以共用同一台Postgres，用不同schema隔離所有資料表。
# 沒設定就用public(跟以前完全一樣，向後相容)。
APP_NAMESPACE = os.getenv("APP_NAMESPACE", "").strip()

# lab端用來監看live端心跳的URL(例如 https://gold-live.zeabur.app/health)，
# 沒設定就不監看
LIVE_HEALTH_URL = os.getenv("LIVE_HEALTH_URL", "").strip()

# live角色下一律拒絕的路由前綴(研究/實驗用，跟正式執行無關)
LAB_ONLY_PATH_PREFIXES = (
    "/backtest",
    "/execution/test-order",
    "/execution/test-close",
    "/execution/set-leverage",
    "/settings/engine/",       # 專屬覆寫的直接修改/重設/清除；live要改請走 /settings/import
)
# live角色下拒絕的(方法, 路徑)——全域設定直接修改
LAB_ONLY_METHOD_PATHS = {
    ("POST", "/settings"),
}


def is_live():
    return APP_ROLE == "live"


def active_engine_ids(all_engine_ids):
    """這個角色要啟動的引擎清單。lab全開；live只開LIVE_ENGINE_IDS裡存在的那些。"""
    if not is_live():
        return list(all_engine_ids)
    return [e for e in LIVE_ENGINE_IDS if e in all_engine_ids]


def is_path_blocked(method, path):
    if not is_live():
        return False
    if (method.upper(), path) in LAB_ONLY_METHOD_PATHS:
        return True
    for prefix in LAB_ONLY_PATH_PREFIXES:
        if path.startswith(prefix):
            # /settings/engine/{id} 的GET(純讀取)放行，POST(修改)才擋
            if prefix == "/settings/engine/" and method.upper() == "GET":
                return False
            return True
    return False


def describe():
    return {
        "role": APP_ROLE,
        "namespace": APP_NAMESPACE or "public",
        "live_engine_ids": LIVE_ENGINE_IDS if is_live() else None,
        "live_health_url": LIVE_HEALTH_URL or None,
    }
