"""
PostgreSQL 持久化模組。

用途：把 Binance 逐筆成交(trades)寫進資料庫，讓服務重啟後(Zeabur重新部署)
不會遺失歷史資料，分析模組可以立刻接續，不用從零重新累積。

設計原則：
- 這是「盡力而為」的持久化：DATABASE_URL沒設定時，整個模組會靜默停用，
  binance_client.py 照樣可以只用記憶體運作(退回到原本的行為)，不會讓服務掛掉。
- 用 psycopg2 直接下SQL，不用ORM，因為schema很單純(一張表)，不需要那個重量級。
- 寫入是批次+週期性flush(在binance_client.py那邊控制頻率)，不是每筆成交都馬上寫，
  避免資料庫被過於頻繁的小型寫入拖慢。
"""

import os
import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger("db")

_pool = None
_enabled = False
_last_write_ok_at = None    # 給health_monitor.py檢查資料庫寫入是否還正常用
_last_write_error = None


def is_enabled():
    return _enabled


def get_write_health():
    """回傳最近一次寫入成功的時間、以及最近一次錯誤訊息(如果有的話)，給health_monitor.py用。"""
    return {"last_write_ok_at": _last_write_ok_at, "last_write_error": _last_write_error}


def init_schema():
    """
    啟動時呼叫一次：建立連線池、確保資料表存在。
    如果沒有設定 DATABASE_URL，直接跳過，_enabled保持False，
    後續所有db函式呼叫都會是no-op，不會拋錯讓服務起不來。
    """
    global _pool, _enabled

    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        logger.info("未設定 DATABASE_URL，資料持久化功能停用，僅使用記憶體暫存")
        return

    try:
        import psycopg2
        from psycopg2 import pool as pg_pool

        # APP_NAMESPACE有設定時，所有連線的search_path都指到該schema，
        # 這個服務的所有資料表就自動落在自己的schema裡，跟另一個角色的服務
        # 共用同一台Postgres也互不干擾(修正記錄見README)
        from app.role import APP_NAMESPACE
        pool_kwargs = {}
        if APP_NAMESPACE:
            pool_kwargs["options"] = f"-c search_path={APP_NAMESPACE},public"
        _pool = pg_pool.SimpleConnectionPool(1, 5, database_url, **pool_kwargs)

        conn = _pool.getconn()
        try:
            with conn.cursor() as cur:
                if APP_NAMESPACE:
                    cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{APP_NAMESPACE}"')
                    conn.commit()
                # 行情資料(gold_trades)固定放public schema、兩個角色共用(修正記錄見README)：
                # 成交紀錄不分研究/正式，live端重啟時直接讀lab端累積的歷史回填K棒，
                # 不用從零收集。只有MARKET_DATA_WRITE=1的服務會寫入(預設lab寫、live不寫)，
                # 避免兩個服務把同一條成交流寫兩次。
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS public.gold_trades (
                        id BIGSERIAL PRIMARY KEY,
                        trade_time BIGINT NOT NULL,
                        price DOUBLE PRECISION NOT NULL,
                        qty DOUBLE PRECISION NOT NULL,
                        is_buyer_maker BOOLEAN,
                        inserted_at TIMESTAMPTZ DEFAULT now()
                    );
                """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_gold_trades_time
                    ON public.gold_trades (trade_time);
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS paper_trades (
                        id BIGSERIAL PRIMARY KEY,
                        direction TEXT NOT NULL,
                        entry_price DOUBLE PRECISION NOT NULL,
                        entry_time TIMESTAMPTZ NOT NULL,
                        sl_price DOUBLE PRECISION NOT NULL,
                        peak_price DOUBLE PRECISION,
                        trailing_active BOOLEAN NOT NULL DEFAULT false,
                        chan_reason TEXT,
                        profile_reason TEXT,
                        status TEXT NOT NULL DEFAULT 'open',
                        exit_price DOUBLE PRECISION,
                        exit_time TIMESTAMPTZ,
                        exit_reason TEXT,
                        pnl_points DOUBLE PRECISION,
                        interval_seconds INTEGER NOT NULL DEFAULT 60
                    );
                """)
                # 舊版schema用tp_price(固定停利)，改成移動停損後不再需要，
                # 用ADD COLUMN IF NOT EXISTS確保舊資料庫升級時不會噴錯
                cur.execute("""
                    ALTER TABLE paper_trades
                    ADD COLUMN IF NOT EXISTS peak_price DOUBLE PRECISION;
                """)
                cur.execute("""
                    ALTER TABLE paper_trades
                    ADD COLUMN IF NOT EXISTS trailing_active BOOLEAN NOT NULL DEFAULT false;
                """)
                # 支援1分K/5分K平行追蹤：舊資料庫的既有紀錄都當作1分K(60秒)的歷史，
                # 這樣升級後不會把舊資料誤判成5分K的紀錄
                cur.execute("""
                    ALTER TABLE paper_trades
                    ADD COLUMN IF NOT EXISTS interval_seconds INTEGER NOT NULL DEFAULT 60;
                """)
                # 支援同一個K線週期跑多種策略平行追蹤(例如1分K纏論 vs 1分K多條件共振)：
                # 光用interval_seconds已經不夠當唯一識別碼(兩個引擎都可能是60秒週期)，
                # 新增engine_id當真正的查詢鍵。舊資料庫的既有紀錄一律回填成
                # "chan_profile_<interval_seconds>"，因為resonance_fvg策略在這次
                # 修改之前從來沒有接過即時模擬單，所有既有資料一定都屬於chan_profile。
                cur.execute("""
                    ALTER TABLE paper_trades
                    ADD COLUMN IF NOT EXISTS engine_id TEXT;
                """)
                cur.execute("""
                    UPDATE paper_trades
                    SET engine_id = 'chan_profile_' || interval_seconds::text
                    WHERE engine_id IS NULL;
                """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_paper_trades_status
                    ON paper_trades (status);
                """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_paper_trades_engine
                    ON paper_trades (engine_id, status);
                """)
                # 記錄真實下單的滑價/價差資料(修正記錄見README)：原本這些數字只是
                # 曇花一現地顯示在Telegram通知裡，沒有真正存下來，沒辦法回頭做
                # 「哪個時段特別容易滑價」這種統計分析。開倉/平倉各自獨立記錄一組
                # (預期成交價、實際成交價、真正執行滑點、決策當下買賣價差)，純模擬
                # 的引擎或沒有真實下單的交易，這幾欄會是NULL，不影響任何既有查詢。
                cur.execute("""
                    ALTER TABLE paper_trades
                    ADD COLUMN IF NOT EXISTS entry_expected_price DOUBLE PRECISION;
                """)
                cur.execute("""
                    ALTER TABLE paper_trades
                    ADD COLUMN IF NOT EXISTS entry_actual_price DOUBLE PRECISION;
                """)
                cur.execute("""
                    ALTER TABLE paper_trades
                    ADD COLUMN IF NOT EXISTS entry_slippage_points DOUBLE PRECISION;
                """)
                cur.execute("""
                    ALTER TABLE paper_trades
                    ADD COLUMN IF NOT EXISTS entry_spread_points DOUBLE PRECISION;
                """)
                cur.execute("""
                    ALTER TABLE paper_trades
                    ADD COLUMN IF NOT EXISTS exit_expected_price DOUBLE PRECISION;
                """)
                cur.execute("""
                    ALTER TABLE paper_trades
                    ADD COLUMN IF NOT EXISTS exit_actual_price DOUBLE PRECISION;
                """)
                cur.execute("""
                    ALTER TABLE paper_trades
                    ADD COLUMN IF NOT EXISTS exit_slippage_points DOUBLE PRECISION;
                """)
                cur.execute("""
                    ALTER TABLE paper_trades
                    ADD COLUMN IF NOT EXISTS exit_spread_points DOUBLE PRECISION;
                """)
                # 開/平倉當下盤口報價是否過期(修正記錄見README)。修正前的舊資料
                # 這兩欄是NULL，修正後一律寫入True/False——NULL vs 非NULL就是
                # 「修正前/修正後」的天然分界線，讓使用者分得出哪些統計是乾淨的。
                cur.execute("""
                    ALTER TABLE paper_trades
                    ADD COLUMN IF NOT EXISTS entry_book_stale BOOLEAN;
                """)
                cur.execute("""
                    ALTER TABLE paper_trades
                    ADD COLUMN IF NOT EXISTS exit_book_stale BOOLEAN;
                """)
                # 開倉當時「有沒有真的送出真實下單」(修正記錄見README)。平倉時只有它是
                # True才會送真實平倉單——被風控擋下/下單失敗的帳面部位，出場時絕不能
                # 去動帳戶上的真實部位(那可能是別的引擎的)。NULL=修正前的舊資料。
                cur.execute("""
                    ALTER TABLE paper_trades
                    ADD COLUMN IF NOT EXISTS real_open_executed BOOLEAN;
                """)
                cur.execute("""
                    ALTER TABLE paper_trades
                    ADD COLUMN IF NOT EXISTS real_open_quantity DOUBLE PRECISION;
                """)
                # 交易所backstop停損單(修正記錄見README)：真實開倉成功時順便掛一張
                # STOP_MARKET條件單當最後防線，algo_id記下來，平倉時要憑這個去撤單；
                # backstop_used_legacy記錄這張單當初是用新的Algo端點還是舊端點掛的，
                # 撤單時要照同一個端點打，不能混用。
                cur.execute("""
                    ALTER TABLE paper_trades
                    ADD COLUMN IF NOT EXISTS backstop_algo_id TEXT;
                """)
                # 送單前這一側原有的數量(基準，第3條r14/r16)：整個部位生命週期都要扣，
                # 所以要存進資料庫，服務重啟後才不會變成0、把別人的部位算成自己的
                cur.execute("""
                    ALTER TABLE paper_trades
                    ADD COLUMN IF NOT EXISTS real_open_baseline DOUBLE PRECISION;
                """)
                # 開倉單號與成交明細的起始界線(第8條r34)：存進資料庫，服務重啟後出場價才查得到
                cur.execute("""
                    ALTER TABLE paper_trades
                    ADD COLUMN IF NOT EXISTS real_open_order_id BIGINT;
                """)
                cur.execute("""
                    ALTER TABLE paper_trades
                    ADD COLUMN IF NOT EXISTS fill_boundary_id BIGINT;
                """)
                # 損益是推估的(成交價查不到時用偵測當下的價格；使用者決定2026-09-22：用推估值、標示、照算)
                cur.execute("""
                    ALTER TABLE paper_trades
                    ADD COLUMN IF NOT EXISTS pnl_estimated BOOLEAN;
                """)
                cur.execute("""
                    ALTER TABLE paper_trades
                    ADD COLUMN IF NOT EXISTS backstop_used_legacy BOOLEAN;
                """)
                # 部分出場狀態(第15條r74)：部分出場損益、估算標記、「還沒認領」的減少量——只在記憶體裡的話，重啟就丟了
                cur.execute("""
                    ALTER TABLE paper_trades
                    ADD COLUMN IF NOT EXISTS partial_state TEXT;
                """)
                # 背景補登還沒完成的記號(第15條r76)：補登是執行緒，重啟就沒了——記號在資料庫，重啟後照它重新排
                cur.execute("""
                    ALTER TABLE paper_trades
                    ADD COLUMN IF NOT EXISTS open_backfill TEXT;
                """)
                cur.execute("""
                    ALTER TABLE paper_trades
                    ADD COLUMN IF NOT EXISTS exit_backfill TEXT;
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS app_settings (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL,
                        updated_at TIMESTAMPTZ DEFAULT now()
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS settings_audit (
                        id BIGSERIAL PRIMARY KEY,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        action TEXT NOT NULL,
                        engine_id TEXT,
                        version TEXT,
                        detail JSONB
                    );
                """)
            conn.commit()

            # 舊版部署可能還有 tp_price 欄位且是 NOT NULL(固定停利機制的殘留)，
            # 改成移動停損後新增的資料列不會再帶tp_price，這裡放寬約束避免insert失敗。
            # 用獨立的try/except是因為全新資料庫根本沒有這個欄位，執行會報錯是正常的，不影響整體初始化。
            try:
                with conn.cursor() as cur:
                    cur.execute("ALTER TABLE paper_trades ALTER COLUMN tp_price DROP NOT NULL;")
                conn.commit()
            except Exception:
                conn.rollback()
        finally:
            _pool.putconn(conn)

        _enabled = True
        logger.info("資料庫連線成功，gold_trades / paper_trades 資料表已就緒")
    except Exception as e:
        logger.error(f"資料庫初始化失敗，退回記憶體模式: {e}")
        _pool = None
        _enabled = False


def load_minute_bars(days=3):
    """
    服務啟動時用：直接在資料庫端把最近N天的逐筆成交聚合成1分鐘OHLCV K棒
    (修正記錄見README)。原本K棒是從記憶體「最近10萬筆成交」即時聚合，緩衝區
    用筆數封頂，成交量大的日子10萬筆只涵蓋約1小時，15分K湊不到5根、ATR(14)
    永遠是None，引擎靜悄悄失效。改成在DB聚合回填3天的1分鐘K棒(只有幾千筆)，
    再由記憶體即時維護，K棒歷史長度就跟成交量脫鉤了。
    回傳時間遞增的 [{"bucket_start","open","high","low","close","volume"}, ...]。
    """
    if not _enabled:
        return []
    from datetime import timedelta
    since_ms = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
    try:
        conn = _pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT bucket,
                           (array_agg(price ORDER BY trade_time ASC, id ASC))[1]  AS open,
                           MAX(price) AS high,
                           MIN(price) AS low,
                           (array_agg(price ORDER BY trade_time DESC, id DESC))[1] AS close,
                           SUM(qty) AS volume
                    FROM (
                        SELECT id, trade_time, price, qty, (trade_time / 60000) * 60000 AS bucket
                        FROM public.gold_trades
                        WHERE trade_time >= %s
                    ) t
                    GROUP BY bucket
                    ORDER BY bucket ASC;
                    """,
                    (since_ms,),
                )
                rows = cur.fetchall()
        finally:
            _pool.putconn(conn)
        return [
            {"bucket_start": int(r[0]), "open": float(r[1]), "high": float(r[2]),
             "low": float(r[3]), "close": float(r[4]), "volume": float(r[5])}
            for r in rows
        ]
    except Exception as e:
        logger.error(f"回填1分鐘K棒失敗: {e}")
        return []


def insert_trades(trades):
    """
    批次寫入逐筆成交。trades是 [{"time","price","qty","is_buyer_maker"}, ...]。
    寫入失敗只記錄log、不拋出例外，避免因為資料庫短暫問題影響主要的即時資料流。
    成功/失敗都會更新 _last_write_ok_at / _last_write_error，給health_monitor.py檢查用。
    """
    global _last_write_ok_at, _last_write_error

    if not _enabled or not trades:
        return

    try:
        from psycopg2.extras import execute_values

        conn = _pool.getconn()
        try:
            with conn.cursor() as cur:
                execute_values(
                    cur,
                    "INSERT INTO public.gold_trades (trade_time, price, qty, is_buyer_maker) VALUES %s",
                    [(t["time"], t["price"], t["qty"], t.get("is_buyer_maker")) for t in trades],
                )
            conn.commit()
            _db_write_ok("寫入逐筆成交")
        finally:
            _pool.putconn(conn)

        _last_write_ok_at = datetime.now(timezone.utc)
        _last_write_error = None
    except Exception as e:
        _db_write_error("寫入逐筆成交", e)
        _last_write_error = str(e)


def load_recent_trades(limit=100000):
    """
    服務啟動時呼叫：從資料庫撈最近N筆成交，回填進記憶體，
    讓分析模組不用等重新累積就能立刻有資料可用。
    """
    if not _enabled:
        return []

    try:
        conn = _pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT trade_time, price, qty, is_buyer_maker
                    FROM public.gold_trades
                    ORDER BY trade_time DESC
                    LIMIT %s;
                    """,
                    (limit,),
                )
                rows = cur.fetchall()
        finally:
            _pool.putconn(conn)

        # 資料庫是由新到舊撈出來的，回填進deque前要反轉成舊到新，跟即時資料流的順序一致
        rows.reverse()
        return [
            {"time": r[0], "price": r[1], "qty": r[2], "is_buyer_maker": r[3]}
            for r in rows
        ]
    except Exception as e:
        logger.error(f"讀取歷史成交失敗: {e}")
        return []


# ---------------------------------------------------------------------------
# 模擬單(paper trading)持久化函式
# ---------------------------------------------------------------------------

def insert_open_paper_trade(position):
    """
    新增一筆開倉中的模擬單。position需含 direction, entry_price, entry_time(ISO字串),
    sl_price, peak_price, trailing_active, chan_reason, profile_reason, interval_seconds
    (哪個K線週期，僅供顯示參考)、engine_id(真正的查詢鍵，用來區分同一個K線週期底下
    不同策略的平行追蹤引擎，例如"chan_profile_60"跟"resonance_fvg_60"都是60秒週期
    但屬於不同引擎，不能共用同一份歷史紀錄)。

    如果這筆單有真實下單(execution_index符合設定)，position可以額外帶
    entry_expected_price/entry_actual_price/entry_slippage_points/
    entry_spread_points這四個欄位，把「決策當下的預期成交價、幣安實際成交價、
    真正執行滑點、當下買賣價差」一起存進資料庫，之後才能回頭做「哪個時段
    容易滑價」這種統計分析，不然這些數字原本只是曇花一現顯示在Telegram
    通知裡，沒有真正留存(修正記錄見README)。純模擬的引擎不會有這幾個值，
    存進去會是NULL。

    回傳新增的資料庫id，沒有資料庫時回傳None(呼叫端要能接受id=None，代表這筆單只存在記憶體)。
    """
    if not _enabled:
        return None

    try:
        conn = _pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO paper_trades
                        (direction, entry_price, entry_time, sl_price, peak_price, trailing_active,
                         chan_reason, profile_reason, status, interval_seconds, engine_id,
                         entry_expected_price, entry_actual_price, entry_slippage_points, entry_spread_points)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'open', %s, %s, %s, %s, %s, %s)
                    RETURNING id;
                    """,
                    (
                        position["direction"], position["entry_price"], position["entry_time"],
                        position["sl_price"], position.get("peak_price"), position.get("trailing_active", False),
                        position.get("chan_reason"), position.get("profile_reason"),
                        position.get("interval_seconds", 60), position.get("engine_id", "chan_profile_60"),
                        position.get("entry_expected_price"), position.get("entry_actual_price"),
                        position.get("entry_slippage_points"), position.get("entry_spread_points"),
                    ),
                )
                new_id = cur.fetchone()[0]
            conn.commit()
            _db_write_ok("新增模擬單")
            return new_id
        finally:
            _pool.putconn(conn)
    except Exception as e:
        _db_write_error("新增模擬單", e)
        return None


def update_paper_trade_stop(trade_id, sl_price, peak_price, trailing_active):
    """
    移動停損更新：每次追蹤引擎調整停損價位時呼叫，讓服務重啟後能從資料庫正確
    恢復目前的移動停損進度，不會重置回entry時的初始停損。trade_id是None時直接跳過。
    """
    if not _enabled or trade_id is None:
        return

    try:
        conn = _pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE paper_trades
                    SET sl_price = %s, peak_price = %s, trailing_active = %s
                    WHERE id = %s AND status = 'open';
                    """,
                    (sl_price, peak_price, trailing_active, trade_id),
                )
            conn.commit()
            _db_write_ok("更新移動停損")
        finally:
            _pool.putconn(conn)
    except Exception as e:
        _db_write_error("更新移動停損", e)


def update_paper_trade_fills(trade_id, order_id, boundary_id):
    """記下開倉單號與成交明細的起始界線(第8條r34)。trade_id是None時跳過。"""
    if not _enabled or trade_id is None:
        return
    try:
        conn = _pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE paper_trades SET real_open_order_id = %s, fill_boundary_id = %s WHERE id = %s;",
                            (order_id, boundary_id, trade_id))
            conn.commit()
            _db_write_ok("記錄成交明細界線")
        finally:
            _pool.putconn(conn)
    except Exception as e:
        _db_write_error("記錄成交明細界線", e)


def update_paper_trade_partial_state(trade_id, state):
    """
    部分出場狀態(第15條r74)存成一個JSON：partial_realized_usd、usd_estimated、partial_pnl_unknown、
    unanchored_reduce_qty(還沒認領的減少量)、unanchored_est_usd。每次變動整份覆寫；trade_id是None時跳過。
    """
    if not _enabled or trade_id is None:
        return False
    try:
        conn = _pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE paper_trades SET partial_state = %s WHERE id = %s;",
                            (json.dumps(state or {}, ensure_ascii=False, default=str), trade_id))
            conn.commit()
            _db_write_ok("記錄部位執行狀態")
            return True
        finally:
            _pool.putconn(conn)
    except Exception as e:
        _db_write_error("記錄部位執行狀態", e)
        return False


_BACKFILL_COLUMNS = {"open": "open_backfill", "close": "exit_backfill"}


def set_paper_trade_backfill(trade_id, action, payload):
    """
    「補登還沒完成」的記號(第15條r76)。action是"open"(進場)或"close"(出場)；payload是補登要用的資料(JSON字串)，
    None代表清掉(補登完成或放棄)。出場的記號要在寫平倉紀錄之前寫：結帳後、排補登前當掉，重啟後才知道要補。
    """
    col = _BACKFILL_COLUMNS[action]   # 欄位名只從固定表裡取，不接外部字串
    if not _enabled or trade_id is None:
        return
    try:
        conn = _pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(f"UPDATE paper_trades SET {col} = %s WHERE id = %s;", (payload, trade_id))
            conn.commit()
            _db_write_ok("記錄補登記號")
        finally:
            _pool.putconn(conn)
    except Exception as e:
        _db_write_error("記錄補登記號", e)


def end_paper_trade_backfill(trade_id, action, how):
    """
    結束補登記號(第15條r78)：標成 {"ended": how}，**不是刪掉**。刪掉就分不出「放棄過」跟「r76 以前、從來沒有記號的舊紀錄」，
    舊紀錄的重排規則會把放棄過的又撿回來、每次重啟再查一輪。how：found / gave_up / found_elsewhere。
    """
    set_paper_trade_backfill(trade_id, action, json.dumps({"ended": how, "at": datetime.now(timezone.utc).isoformat()}))


def backfill_mark_state(raw):
    """記號的狀態：None(從來沒有)／"pending"(還沒完成)／"ended"(已結束)。"""
    if raw is None:
        return None
    return "ended" if str(raw).startswith(ENDED_PREFIX) else "pending"


ENDED_PREFIX = '{"ended"'


def load_pending_backfills(engine_id):
    """
    重啟時用(第15條r76)：這個引擎還有「補登還沒完成」記號的紀錄(不分開倉中或已平倉)。
    回傳(ok, [{"trade_id", "open", "close"}])；讀取失敗回(False, 錯誤)，不能當成「沒有要補的」。
    """
    if not _enabled:
        return True, []
    try:
        conn = _pool.getconn()
        try:
            with conn.cursor() as cur:
                # 已結束的(r78)不撈：歷史紀錄越來越多，每次重啟只該看還沒完成的
                cur.execute("""
                    SELECT id, open_backfill, exit_backfill FROM paper_trades
                    WHERE engine_id = %s
                      AND ((open_backfill IS NOT NULL AND open_backfill NOT LIKE %s)
                        OR (exit_backfill IS NOT NULL AND exit_backfill NOT LIKE %s))
                    ORDER BY id;
                """, (engine_id, ENDED_PREFIX + "%", ENDED_PREFIX + "%"))
                rows = cur.fetchall()
        finally:
            _pool.putconn(conn)
        return True, [{"trade_id": r[0],
                       "open": r[1] if backfill_mark_state(r[1]) == "pending" else None,
                       "close": r[2] if backfill_mark_state(r[2]) == "pending" else None} for r in rows]
    except Exception as e:
        logger.error(f"讀取補登記號失敗: {e}")
        return False, f"{type(e).__name__}: {e}"


def _parse_partial_state(raw):
    """讀回部分出場狀態。壞掉的內容不能當成「沒有部分出場」(會把估算當成沒發生)：記成部分損益未知。"""
    if not raw:
        return {}
    try:
        st = json.loads(raw)
        if not isinstance(st, dict):
            raise ValueError(f"不是物件：{type(st).__name__}")
        return st
    except Exception as e:
        logger.error(f"部分出場狀態讀不懂，這筆的部分損益記未知: {e}")
        return {"partial_pnl_unknown": True}


def update_paper_trade_baseline(trade_id, baseline):
    """記下送單前這一側原有的數量(基準，第3條r16)。trade_id是None時跳過。"""
    if not _enabled or trade_id is None:
        return
    try:
        conn = _pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE paper_trades SET real_open_baseline = %s WHERE id = %s;", (baseline, trade_id))
            conn.commit()
            _db_write_ok("記錄基準數量")
        finally:
            _pool.putconn(conn)
    except Exception as e:
        _db_write_error("記錄基準數量", e)


def update_paper_trade_backstop(trade_id, backstop_algo_id, backstop_used_legacy):
    """
    記下這筆倉位的交易所backstop停損單algoId，平倉時才知道要撤哪一張
    (修正記錄見README)。trade_id是None時直接跳過。
    """
    if not _enabled or trade_id is None:
        return
    try:
        conn = _pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE paper_trades SET backstop_algo_id = %s, backstop_used_legacy = %s WHERE id = %s;",
                    (backstop_algo_id, backstop_used_legacy, trade_id),
                )
            conn.commit()
            _db_write_ok("記錄backstop停損單")
        finally:
            _pool.putconn(conn)
    except Exception as e:
        _db_write_error("記錄backstop停損單", e)


def close_paper_trade(trade_id, exit_price, exit_time, exit_reason, pnl_points,
                       exit_expected_price=None, exit_actual_price=None,
                       exit_slippage_points=None, exit_spread_points=None, pnl_estimated=False):
    """
    把一筆開倉中的模擬單標記為已平倉。trade_id是None時(該筆單沒有db id)直接跳過。
    exit_expected_price等四個欄位是平倉時的滑價/價差資料，用法跟
    insert_open_paper_trade()的entry_*系列欄位一樣，不提供的話(純模擬或
    沒有真實下單)存NULL(修正記錄見README)。
    """
    if not _enabled or trade_id is None:
        return

    try:
        conn = _pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE paper_trades
                    SET status = 'closed', exit_price = %s, exit_time = %s,
                        exit_reason = %s, pnl_points = %s, pnl_estimated = %s,
                        exit_expected_price = COALESCE(%s, exit_expected_price),
                        exit_actual_price = COALESCE(%s, exit_actual_price),
                        exit_slippage_points = COALESCE(%s, exit_slippage_points),
                        exit_spread_points = COALESCE(%s, exit_spread_points)
                    WHERE id = %s;
                    """,
                    # COALESCE(實盤2026-09-22)：r25起平倉流程是「先送平倉單、寫出場成交價，最後才結帳」，
                    # 這裡以前無條件覆寫，沒傳值就把剛寫進去的成交價與滑點清成空的——網頁因此寫「成交價查不到」
                    (
                        exit_price, exit_time, exit_reason, pnl_points, bool(pnl_estimated),
                        exit_expected_price, exit_actual_price, exit_slippage_points, exit_spread_points,
                        trade_id,
                    ),
                )
            conn.commit()
            _db_write_ok("平倉模擬單")
        finally:
            _pool.putconn(conn)
    except Exception as e:
        _db_write_error("平倉模擬單", e)


# 資料庫寫入失敗不能只寫日誌(第8條r36)：開倉單號、成交界線、待平倉、backstop都靠資料庫才能跨重啟，
# 寫不進去時下次重啟才發現全沒了。照第8條節奏推播，恢復時通知一次。推播路徑不拿任何引擎的鎖(r37的死鎖)。
_db_fail_counts = {}


def _db_write_error(op, e):
    logger.error(f"{op}失敗: {e}")
    n = _db_fail_counts.get(op, 0) + 1
    _db_fail_counts[op] = n
    try:
        from app import alert_cadence
        from app.notifier import notifier
        if alert_cadence.should_alert(n):
            notifier.send_raw_message(
                f"⚠️ 資料庫寫入失敗「{op}」(第 {n} 次)\n錯誤：{type(e).__name__}: {e}\n"
                f"記憶體裡的狀態仍正確，但重啟後可能遺失；請檢查資料庫")
    except Exception:
        pass


def _db_write_ok(op):
    n = _db_fail_counts.pop(op, 0)
    if n:
        try:
            from app.notifier import notifier
            notifier.send_raw_message(f"✅ 資料庫寫入已恢復「{op}」(失敗 {n} 次後)")
        except Exception:
            pass


def load_open_paper_trade(engine_id="chan_profile_60"):
    """
    服務啟動時呼叫：查有沒有還沒平倉的模擬單(每個engine_id各自最多一筆)，
    用來回填記憶體狀態。engine_id區分是哪一個追蹤引擎在查(例如1分K纏論
    "chan_profile_60" 跟 1分K共振 "resonance_fvg_60" 是不同引擎，即使
    interval_seconds同樣是60也不會查到彼此的資料)。
    """
    if not _enabled:
        return True, None  # 沒接資料庫(純記憶體模式)：確實沒有持倉紀錄

    try:
        conn = _pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, direction, entry_price, entry_time, sl_price, peak_price, trailing_active,
                           chan_reason, profile_reason, interval_seconds, engine_id,
                           real_open_executed, real_open_quantity,
                           backstop_algo_id, backstop_used_legacy, real_open_baseline,
                           real_open_order_id, fill_boundary_id, entry_actual_price, partial_state, open_backfill
                    FROM paper_trades
                    WHERE status = 'open' AND engine_id = %s
                    ORDER BY entry_time DESC
                    LIMIT 1;
                """, (engine_id,))
                row = cur.fetchone()
        finally:
            _pool.putconn(conn)

        if not row:
            return True, None
        pos = {
            "id": row[0], "direction": row[1], "entry_price": row[2],
            "entry_time": row[3].isoformat() if row[3] else None,
            "sl_price": row[4], "peak_price": row[5], "trailing_active": row[6],
            "chan_reason": row[7], "profile_reason": row[8], "interval_seconds": row[9],
            "engine_id": row[10],
            "real_open_executed": row[11], "real_open_quantity": row[12],
            # 服務重啟後要能認回這張backstop停損單(平倉時才知道要撤哪張)(修正記錄見README)
            "backstop_algo_id": row[13], "backstop_used_legacy": row[14],
            "real_open_baseline": row[15] or 0.0,
            "real_open_order_id": row[16], "fill_boundary_id": row[17],
            # r74：進場成交價以前沒還原——重啟後算部分出場／最後出場的實際損益、判斷重開都用得到
            "entry_actual_price": row[18],
        }
        pos.update(_parse_partial_state(row[19]))   # r74：部分出場狀態(含還沒認領的減少量)
        # r78：進場補登記號的狀態(從來沒有／還沒完成／已結束)——舊紀錄的重排規則只給「從來沒有」的
        pos["open_backfill_state"] = backfill_mark_state(row[20])
        return True, pos
    except Exception as e:
        # 讀取失敗≠沒有持倉(第8條r37)：以前這裡回None，跟「沒有持倉」一模一樣——資料庫短暫連不上的那次重啟，
        # 引擎就以為自己空手，不管交易所上的真實部位、還可能再開新倉
        logger.error(f"讀取開倉中模擬單失敗: {e}")
        return False, f"{type(e).__name__}: {e}"


def get_open_paper_trade(engine_id="chan_profile_60"):
    """舊介面(只回部位或None)。會分不出「讀取失敗」與「沒有持倉」——需要分辨的地方用 load_open_paper_trade。"""
    ok, pos = load_open_paper_trade(engine_id)
    return pos if ok else None


def load_closed_paper_trades(limit=500, engine_id="chan_profile_60"):
    """撈最近N筆已平倉的模擬單(限定某個引擎)，由新到舊排序，給績效統計用。"""
    if not _enabled:
        return True, []

    try:
        conn = _pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT direction, entry_price, entry_time, exit_price, exit_time,
                           exit_reason, pnl_points, chan_reason, profile_reason,
                           entry_slippage_points, entry_spread_points,
                           exit_slippage_points, exit_spread_points,
                           entry_book_stale, exit_book_stale,
                           real_open_executed, real_open_quantity,
                           entry_actual_price, exit_actual_price,
                           sl_price, peak_price, trailing_active, pnl_estimated
                    FROM paper_trades
                    WHERE status = 'closed' AND engine_id = %s
                    ORDER BY exit_time DESC
                    LIMIT %s;
                    """,
                    (engine_id, limit),
                )
                rows = cur.fetchall()
        finally:
            _pool.putconn(conn)

        return True, [
            {
                "direction": r[0], "entry_price": r[1],
                "entry_time": r[2].isoformat() if r[2] else None,
                "exit_price": r[3],
                "exit_time": r[4].isoformat() if r[4] else None,
                "exit_reason": r[5], "pnl_points": r[6],
                "chan_reason": r[7], "profile_reason": r[8],
                "entry_slippage_points": r[9], "entry_spread_points": r[10],
                "exit_slippage_points": r[11], "exit_spread_points": r[12],
                "entry_book_stale": r[13], "exit_book_stale": r[14],
                # 真實下單資訊：dashboard用來換算USDT(只有真的送過單的交易才顯示)
                "real_open_executed": r[15], "real_open_quantity": r[16],
                "entry_actual_price": r[17], "exit_actual_price": r[18],
                # 真實成交價算出的USDT損益(進出場都有真實成交價才有)
                # 出場當下的停損位/峰值/移動停損是否啟動：停損出場時用來判斷是停損位設在那裡
                # 還是價格跳空穿過(修正記錄見README)
                "sl_price": r[19], "peak_price": r[20], "trailing_active": r[21],
                "real_pnl_usd": (
                    round(((r[18] - r[17]) if r[0] == "bullish" else (r[17] - r[18])) * (r[16] or 0), 2)
                    if r[15] and r[17] and r[18] and r[16] else None
                ),
                "pnl_estimated": bool(r[22]),
                # 真實成交價缺漏時的推估值(使用者決定2026-09-22)：點數×張數，網頁標「估」、照算、分開計數
                "real_pnl_usd_est": (
                    round(r[6] * r[16], 2)
                    if r[15] and r[16] and isinstance(r[6], (int, float)) and not (r[17] and r[18]) else None
                ),
            }
            for r in rows
        ]
    except Exception as e:
        # 讀取失敗≠沒有交易(第8條r47)：以前回[]，風控就當成「今天沒虧、沒連虧」，斷路器永遠不會觸發
        logger.error(f"讀取模擬單歷史失敗: {e}")
        return False, f"{type(e).__name__}: {e}"


def get_closed_paper_trades(limit=500, engine_id="chan_profile_60"):
    """舊介面：讀不到時回[]——分不出「讀取失敗」與「沒有交易」。需要分辨的地方(風控)用 load_closed_paper_trades。"""
    ok, rows = load_closed_paper_trades(limit=limit, engine_id=engine_id)
    return rows if ok else []



def update_paper_trade_real_open(trade_id, executed, quantity=None):
    """開倉真實下單的結果(成功/失敗/被擋)寫回資料庫，重啟後恢復部位時要靠它決定平倉能不能送真單。"""
    if not _enabled or trade_id is None:
        return
    try:
        conn = _pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE paper_trades SET real_open_executed = %s, real_open_quantity = %s WHERE id = %s;",
                    (bool(executed), quantity, trade_id),
                )
            conn.commit()
            _db_write_ok("更新開倉真實下單狀態")
        finally:
            _pool.putconn(conn)
    except Exception as e:
        _db_write_error("更新開倉真實下單狀態", e)


def update_paper_trade_entry_execution(trade_id, expected_price, actual_price, slippage_points, spread_points, book_stale=False):
    """
    開倉的真實下單流程算出滑價/價差資料後，用這個函式補寫回去(insert_open_paper_trade()
    當下還沒有這些資料，因為要先送出真實下單、拿到成交價才能算出來)。trade_id是
    None時(該筆單沒有db id)直接跳過。任一欄位是None也沒關係，照樣寫入(代表那個
    環節沒有資料，例如沒查到bid/ask)。
    """
    if not _enabled or trade_id is None:
        return
    try:
        conn = _pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE paper_trades
                    SET entry_expected_price = %s, entry_actual_price = %s,
                        entry_slippage_points = %s, entry_spread_points = %s,
                        entry_book_stale = %s
                    WHERE id = %s;
                    """,
                    (expected_price, actual_price, slippage_points, spread_points, bool(book_stale), trade_id),
                )
            conn.commit()
            _db_write_ok("更新開倉執行品質資料")
        finally:
            _pool.putconn(conn)
    except Exception as e:
        _db_write_error("更新開倉執行品質資料", e)


def update_paper_trade_exit_execution(trade_id, expected_price, actual_price, slippage_points, spread_points, book_stale=False):
    """平倉版本的update_paper_trade_entry_execution()，補寫exit_*系列欄位。"""
    if not _enabled or trade_id is None:
        return
    try:
        conn = _pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE paper_trades
                    SET exit_expected_price = COALESCE(%s, exit_expected_price),
                        exit_actual_price = COALESCE(%s, exit_actual_price),
                        exit_slippage_points = COALESCE(%s, exit_slippage_points),
                        exit_spread_points = COALESCE(%s, exit_spread_points),
                        exit_book_stale = %s
                    WHERE id = %s;
                    """,
                    (expected_price, actual_price, slippage_points, spread_points, bool(book_stale), trade_id),
                )
            conn.commit()
            _db_write_ok("更新平倉執行品質資料")
        finally:
            _pool.putconn(conn)
    except Exception as e:
        _db_write_error("更新平倉執行品質資料", e)


def get_trades_by_hour(engine_id="chan_profile_60", hour_utc=0, side="entry", limit=100):
    """
    撈某個UTC小時內、有真實下單滑價資料的個別交易，給「點進某個小時看個別
    交易」的drill-down功能用——滑價時段統計只看平均/最大，看不出某個極端值
    (例如一筆38點的滑點)到底發生在哪個確切時間、當時是什麼訊號、最後這筆
    賺賠如何，這個函式把該小時的每一筆完整攤開，讓使用者能對照當時的市況
    (例如是不是剛好碰到重大消息或倫敦開盤)去判斷極端值是系統性問題還是
    單次意外(修正記錄見README)。

    side="entry"依entry_time的小時篩選並回傳開倉滑價；side="exit"依exit_time
    的小時篩選並回傳平倉滑價。兩者都會一併帶出這筆交易的完整資訊(進出場
    價格/時間/理由/損益)，方便對照。
    """
    if not _enabled:
        return []
    if side not in ("entry", "exit"):
        return []

    time_col = "entry_time" if side == "entry" else "exit_time"
    slip_col = "entry_slippage_points" if side == "entry" else "exit_slippage_points"

    try:
        conn = _pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT id, direction, entry_time, entry_price, exit_time, exit_price,
                           exit_reason, pnl_points, chan_reason, profile_reason,
                           entry_expected_price, entry_actual_price, entry_slippage_points, entry_spread_points,
                           exit_expected_price, exit_actual_price, exit_slippage_points, exit_spread_points,
                           entry_book_stale, exit_book_stale
                    FROM paper_trades
                    WHERE engine_id = %s
                      AND {slip_col} IS NOT NULL
                      AND EXTRACT(HOUR FROM {time_col} AT TIME ZONE 'UTC')::int = %s
                    ORDER BY {slip_col} DESC, {time_col} DESC
                    LIMIT %s;
                    """,
                    (engine_id, hour_utc, limit),
                )
                rows = cur.fetchall()
        finally:
            _pool.putconn(conn)

        return [
            {
                "id": r[0], "direction": r[1],
                "entry_time": r[2].isoformat() if r[2] else None, "entry_price": r[3],
                "exit_time": r[4].isoformat() if r[4] else None, "exit_price": r[5],
                "exit_reason": r[6], "pnl_points": r[7],
                "chan_reason": r[8], "profile_reason": r[9],
                "entry_expected_price": r[10], "entry_actual_price": r[11],
                "entry_slippage_points": r[12], "entry_spread_points": r[13],
                "exit_expected_price": r[14], "exit_actual_price": r[15],
                "exit_slippage_points": r[16], "exit_spread_points": r[17],
                "entry_book_stale": r[18], "exit_book_stale": r[19],
            }
            for r in rows
        ]
    except Exception as e:
        logger.error(f"查詢指定小時的個別交易失敗: {e}")
        return []


FAT_TAIL_SLIPPAGE_POINTS = 3.0  # 單筆不利滑點超過這個值視為「肥尾事件」，跟dashboard累積滑點卡片的門檻一致


def _slippage_group_sql(time_col, slip_col, spread_col, group_expr):
    """
    組出一段滑價統計SQL：按group_expr分組，回傳筆數/平均/中位數/P90/最大/均價差/
    肥尾筆數/肥尾不利滑點合計/該組不利滑點合計。滑點正值=不利(成交比預期差)。
    平均容易被單筆極端值帶著走，所以一起算中位數和P90——中位數看「這個時段是
    普遍差還是偶爾炸」，P90看「十筆裡最差那筆大概多少」，肥尾筆數看「炸的頻率」。
    """
    return f"""
        SELECT {group_expr} AS grp,
               COUNT(*),
               AVG({slip_col}),
               PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY {slip_col}),
               PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY {slip_col}),
               MAX(ABS({slip_col})),
               AVG({spread_col}),
               COUNT(*) FILTER (WHERE {slip_col} > %s),
               COALESCE(SUM({slip_col}) FILTER (WHERE {slip_col} > %s), 0),
               COALESCE(SUM({slip_col}) FILTER (WHERE {slip_col} > 0), 0)
        FROM paper_trades
        WHERE engine_id = %s AND {slip_col} IS NOT NULL
        GROUP BY grp
        ORDER BY grp;
    """


def _format_slippage_rows(rows, key_name, key_fn):
    out = []
    for r in rows:
        adverse_total = float(r[9]) if r[9] is not None else 0.0
        fat_sum = float(r[8]) if r[8] is not None else 0.0
        out.append({
            key_name: key_fn(r[0]),
            "count": r[1],
            "avg_slippage": round(r[2], 3) if r[2] is not None else None,
            "median_slippage": round(r[3], 3) if r[3] is not None else None,
            "p90_slippage": round(r[4], 3) if r[4] is not None else None,
            "max_abs_slippage": round(r[5], 3) if r[5] is not None else None,
            "avg_spread": round(r[6], 3) if r[6] is not None else None,
            "fat_tail_count": r[7],
            "fat_tail_sum": round(fat_sum, 3),
            "adverse_sum": round(adverse_total, 3),
            "fat_tail_share": round(fat_sum / adverse_total * 100, 1) if adverse_total > 0 else 0.0,
        })
    return out


def get_slippage_stats_by_hour(engine_id="chan_profile_60"):
    """
    按小時(UTC)分組統計真實下單的滑價/價差資料，用來找出「哪個時段特別
    容易滑價」這種規律(修正記錄見README，使用者實際觀察到某幾筆單滑點
    特別大，懷疑跟時段有關，這個函式讓他能用資料驗證，而不是憑印象猜)。

    開倉滑點依entry_time的小時分組、平倉滑點依exit_time的小時分組，分開
    統計——進場和出場當下的市況不一定相關，混在一起看會模糊掉真正的規律。
    只統計「有真實下單過」的交易(entry_slippage_points或exit_slippage_points
    不是NULL的紀錄)，純模擬的交易不會有這些值，自然不會被納入統計。

    每個小時除了平均/最大，還帶中位數、P90、肥尾筆數(>FAT_TAIL_SLIPPAGE_POINTS)
    和肥尾佔該小時不利滑點的比例——平均會被一筆極端值拉歪，光看平均分不出
    「這個時段普遍差」和「偶爾炸一筆」，而這兩種要用不同方法處理(修正記錄見README)。

    另外回傳by_day(依UTC日期分組)和fat_tail_events(所有肥尾事件清單，含日期/
    小時/方向/訊號/出場原因/損益)：肥尾如果全集中在某一天，該擋的是「數據日」
    不是「時段」；事件清單則是之後回頭對照當天新聞/開盤時點用的原始紀錄。

    回傳 {"entry": [...], "exit": [...], "by_day": {"entry": [...], "exit": [...]},
    "fat_tail_events": [...], "fat_tail_threshold": 3.0, "summary": {...}}。
    沒有資料庫或查詢失敗時安全回傳空結構，不會讓呼叫端出錯。
    """
    empty = {"entry": [], "exit": [], "by_day": {"entry": [], "exit": []},
             "fat_tail_events": [], "fat_tail_threshold": FAT_TAIL_SLIPPAGE_POINTS, "summary": {}}
    if not _enabled:
        return empty

    ft = FAT_TAIL_SLIPPAGE_POINTS
    try:
        conn = _pool.getconn()
        try:
            with conn.cursor() as cur:
                hour_expr = lambda col: f"EXTRACT(HOUR FROM {col} AT TIME ZONE 'UTC')::int"
                day_expr = lambda col: f"({col} AT TIME ZONE 'UTC')::date"

                cur.execute(_slippage_group_sql("entry_time", "entry_slippage_points", "entry_spread_points", hour_expr("entry_time")), (ft, ft, engine_id))
                entry_hour = cur.fetchall()
                cur.execute(_slippage_group_sql("exit_time", "exit_slippage_points", "exit_spread_points", hour_expr("exit_time")), (ft, ft, engine_id))
                exit_hour = cur.fetchall()
                cur.execute(_slippage_group_sql("entry_time", "entry_slippage_points", "entry_spread_points", day_expr("entry_time")), (ft, ft, engine_id))
                entry_day = cur.fetchall()
                cur.execute(_slippage_group_sql("exit_time", "exit_slippage_points", "exit_spread_points", day_expr("exit_time")), (ft, ft, engine_id))
                exit_day = cur.fetchall()

                # 肥尾事件清單：開倉或平倉任一邊不利滑點超過門檻的交易，各自列一筆
                cur.execute(
                    """
                    SELECT 'entry' AS side, id, direction, entry_time, entry_slippage_points, entry_spread_points,
                           entry_expected_price, entry_actual_price, chan_reason, exit_reason, pnl_points, entry_book_stale
                    FROM paper_trades
                    WHERE engine_id = %s AND entry_slippage_points > %s
                    UNION ALL
                    SELECT 'exit' AS side, id, direction, exit_time, exit_slippage_points, exit_spread_points,
                           exit_expected_price, exit_actual_price, chan_reason, exit_reason, pnl_points, exit_book_stale
                    FROM paper_trades
                    WHERE engine_id = %s AND exit_slippage_points > %s
                    ORDER BY 5 DESC;
                    """,
                    (engine_id, ft, engine_id, ft),
                )
                event_rows = cur.fetchall()

                # 整體摘要(不分小時)：全部樣本的中位數/P90/肥尾筆數與佔比
                cur.execute(
                    """
                    SELECT side, COUNT(*),
                           PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY slip),
                           PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY slip),
                           COUNT(*) FILTER (WHERE slip > %s),
                           COALESCE(SUM(slip) FILTER (WHERE slip > %s), 0),
                           COALESCE(SUM(slip) FILTER (WHERE slip > 0), 0)
                    FROM (
                        SELECT 'entry' AS side, entry_slippage_points AS slip FROM paper_trades WHERE engine_id = %s AND entry_slippage_points IS NOT NULL
                        UNION ALL
                        SELECT 'exit', exit_slippage_points FROM paper_trades WHERE engine_id = %s AND exit_slippage_points IS NOT NULL
                    ) x
                    GROUP BY side;
                    """,
                    (ft, ft, engine_id, engine_id),
                )
                summary_rows = cur.fetchall()
        finally:
            _pool.putconn(conn)

        events = []
        for r in event_rows:
            ts = r[3]
            events.append({
                "side": r[0], "trade_id": r[1], "direction": r[2],
                "time": ts.isoformat() if ts else None,
                "date_utc": ts.astimezone(timezone.utc).strftime("%Y-%m-%d") if ts else None,
                "hour_utc": ts.astimezone(timezone.utc).hour if ts else None,
                "weekday_utc": ts.astimezone(timezone.utc).strftime("%a") if ts else None,
                "slippage": round(float(r[4]), 3), "spread": round(float(r[5]), 3) if r[5] is not None else None,
                "expected_price": r[6], "actual_price": r[7],
                "chan_reason": r[8], "exit_reason": r[9], "pnl_points": r[10], "book_stale": r[11],
            })

        summary = {}
        for r in summary_rows:
            adverse = float(r[6]) if r[6] is not None else 0.0
            fat_sum = float(r[5]) if r[5] is not None else 0.0
            summary[r[0]] = {
                "count": r[1],
                "median_slippage": round(r[2], 3) if r[2] is not None else None,
                "p90_slippage": round(r[3], 3) if r[3] is not None else None,
                "fat_tail_count": r[4],
                "fat_tail_share": round(fat_sum / adverse * 100, 1) if adverse > 0 else 0.0,
            }

        return {
            "entry": _format_slippage_rows(entry_hour, "hour_utc", int),
            "exit": _format_slippage_rows(exit_hour, "hour_utc", int),
            "by_day": {
                "entry": _format_slippage_rows(entry_day, "date_utc", lambda d: d.isoformat()),
                "exit": _format_slippage_rows(exit_day, "date_utc", lambda d: d.isoformat()),
            },
            "fat_tail_events": events,
            "fat_tail_threshold": ft,
            "summary": summary,
        }
    except Exception as e:
        logger.error(f"查詢滑價時段統計失敗: {e}")
        return empty


# ---------------------------------------------------------------------------
# 執行期可調整設定(app_settings) 持久化函式
# ---------------------------------------------------------------------------

def load_app_settings():
    """讀全部設定：(ok, {key: value})。讀取失敗≠沒有設定(第8條r43)。沒有資料庫時(True, {})。"""
    if not _enabled:
        return True, {}
    try:
        conn = _pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT key, value FROM app_settings;")
                rows = cur.fetchall()
        finally:
            _pool.putconn(conn)
        return True, {r[0]: r[1] for r in rows}
    except Exception as e:
        logger.error(f"讀取設定失敗: {e}")
        return False, f"{type(e).__name__}: {e}"


def get_app_settings():
    """回傳目前資料庫裡存的所有設定，格式 {key: value(字串)}。沒有資料庫時回傳空dict。"""
    if not _enabled:
        return {}

    try:
        conn = _pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT key, value FROM app_settings;")
                rows = cur.fetchall()
        finally:
            _pool.putconn(conn)
        return {r[0]: r[1] for r in rows}
    except Exception as e:
        logger.error(f"讀取設定失敗: {e}")
        return {}


def save_app_settings(updates):
    """
    寫入/更新設定，updates是 {key: value}。用upsert(ON CONFLICT)，
    存在就更新、不存在就新增。value一律存成字串，讀取端自己轉型別。
    """
    if not _enabled or not updates:
        return

    try:
        conn = _pool.getconn()
        try:
            with conn.cursor() as cur:
                for key, value in updates.items():
                    cur.execute(
                        """
                        INSERT INTO app_settings (key, value, updated_at)
                        VALUES (%s, %s, now())
                        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now();
                        """,
                        (key, str(value)),
                    )
            conn.commit()
            _db_write_ok("寫入設定")
        finally:
            _pool.putconn(conn)
    except Exception as e:
        _db_write_error("寫入設定", e)


def delete_app_settings(keys):
    """刪除指定的設定key(給引擎專屬覆寫「清除→沿用全域」用)。keys是可迭代的字串集合。"""
    keys = list(keys)
    if not _enabled or not keys:
        return
    try:
        conn = _pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM app_settings WHERE key = ANY(%s);", (keys,))
            conn.commit()
        finally:
            _pool.putconn(conn)
    except Exception as e:
        logger.error(f"刪除設定失敗: {e}")


# ---------------------------------------------------------------------------
# 參數集匯入 / 緊急控制 的審計紀錄(修正記錄見README)
# ---------------------------------------------------------------------------

def insert_settings_audit(action, engine_id=None, version=None, detail=None):
    if not _enabled:
        return
    import json as _json
    try:
        conn = _pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO settings_audit (action, engine_id, version, detail) VALUES (%s, %s, %s, %s)",
                    (action, engine_id, version, _json.dumps(detail or {}, ensure_ascii=False, default=str)),
                )
            conn.commit()
            _db_write_ok("寫入審計紀錄")
        finally:
            _pool.putconn(conn)
    except Exception as e:
        _db_write_error("寫入審計紀錄", e)


def get_settings_audit(limit=50):
    if not _enabled:
        return []
    try:
        conn = _pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, created_at, action, engine_id, version, detail FROM settings_audit ORDER BY id DESC LIMIT %s",
                    (limit,),
                )
                rows = cur.fetchall()
        finally:
            _pool.putconn(conn)
        return [{"id": r[0], "created_at": r[1].isoformat() if r[1] else None, "action": r[2],
                 "engine_id": r[3], "version": r[4], "detail": r[5]} for r in rows]
    except Exception as e:
        logger.error(f"讀取審計紀錄失敗: {e}")
        return []
