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
import logging

logger = logging.getLogger("db")

_pool = None
_enabled = False


def is_enabled():
    return _enabled


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

        _pool = pg_pool.SimpleConnectionPool(1, 5, database_url)

        conn = _pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS gold_trades (
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
                    ON gold_trades (trade_time);
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS paper_trades (
                        id BIGSERIAL PRIMARY KEY,
                        direction TEXT NOT NULL,
                        entry_price DOUBLE PRECISION NOT NULL,
                        entry_time TIMESTAMPTZ NOT NULL,
                        sl_price DOUBLE PRECISION NOT NULL,
                        tp_price DOUBLE PRECISION NOT NULL,
                        chan_reason TEXT,
                        profile_reason TEXT,
                        status TEXT NOT NULL DEFAULT 'open',
                        exit_price DOUBLE PRECISION,
                        exit_time TIMESTAMPTZ,
                        exit_reason TEXT,
                        pnl_points DOUBLE PRECISION
                    );
                """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_paper_trades_status
                    ON paper_trades (status);
                """)
            conn.commit()
        finally:
            _pool.putconn(conn)

        _enabled = True
        logger.info("資料庫連線成功，gold_trades / paper_trades 資料表已就緒")
    except Exception as e:
        logger.error(f"資料庫初始化失敗，退回記憶體模式: {e}")
        _pool = None
        _enabled = False


def insert_trades(trades):
    """
    批次寫入逐筆成交。trades是 [{"time","price","qty","is_buyer_maker"}, ...]。
    寫入失敗只記錄log、不拋出例外，避免因為資料庫短暫問題影響主要的即時資料流。
    """
    if not _enabled or not trades:
        return

    try:
        from psycopg2.extras import execute_values

        conn = _pool.getconn()
        try:
            with conn.cursor() as cur:
                execute_values(
                    cur,
                    "INSERT INTO gold_trades (trade_time, price, qty, is_buyer_maker) VALUES %s",
                    [(t["time"], t["price"], t["qty"], t.get("is_buyer_maker")) for t in trades],
                )
            conn.commit()
        finally:
            _pool.putconn(conn)
    except Exception as e:
        logger.error(f"寫入逐筆成交失敗: {e}")


def load_recent_trades(limit=20000):
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
                    FROM gold_trades
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
    sl_price, tp_price, chan_reason, profile_reason。回傳新增的資料庫id，
    沒有資料庫時回傳None(呼叫端要能接受id=None，代表這筆單只存在記憶體)。
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
                        (direction, entry_price, entry_time, sl_price, tp_price, chan_reason, profile_reason, status)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, 'open')
                    RETURNING id;
                    """,
                    (
                        position["direction"], position["entry_price"], position["entry_time"],
                        position["sl_price"], position["tp_price"],
                        position.get("chan_reason"), position.get("profile_reason"),
                    ),
                )
                new_id = cur.fetchone()[0]
            conn.commit()
            return new_id
        finally:
            _pool.putconn(conn)
    except Exception as e:
        logger.error(f"新增模擬單失敗: {e}")
        return None


def close_paper_trade(trade_id, exit_price, exit_time, exit_reason, pnl_points):
    """把一筆開倉中的模擬單標記為已平倉。trade_id是None時(該筆單沒有db id)直接跳過。"""
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
                        exit_reason = %s, pnl_points = %s
                    WHERE id = %s;
                    """,
                    (exit_price, exit_time, exit_reason, pnl_points, trade_id),
                )
            conn.commit()
        finally:
            _pool.putconn(conn)
    except Exception as e:
        logger.error(f"平倉模擬單失敗: {e}")


def get_open_paper_trade():
    """服務啟動時呼叫：查有沒有還沒平倉的模擬單(正常情況下最多一筆)，用來回填記憶體狀態。"""
    if not _enabled:
        return None

    try:
        conn = _pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, direction, entry_price, entry_time, sl_price, tp_price,
                           chan_reason, profile_reason
                    FROM paper_trades
                    WHERE status = 'open'
                    ORDER BY entry_time DESC
                    LIMIT 1;
                """)
                row = cur.fetchone()
        finally:
            _pool.putconn(conn)

        if not row:
            return None
        return {
            "id": row[0], "direction": row[1], "entry_price": row[2],
            "entry_time": row[3].isoformat() if row[3] else None,
            "sl_price": row[4], "tp_price": row[5],
            "chan_reason": row[6], "profile_reason": row[7],
        }
    except Exception as e:
        logger.error(f"讀取開倉中模擬單失敗: {e}")
        return None


def get_closed_paper_trades(limit=500):
    """撈最近N筆已平倉的模擬單，由新到舊排序，給績效統計用。"""
    if not _enabled:
        return []

    try:
        conn = _pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT direction, entry_price, entry_time, exit_price, exit_time,
                           exit_reason, pnl_points, chan_reason, profile_reason
                    FROM paper_trades
                    WHERE status = 'closed'
                    ORDER BY exit_time DESC
                    LIMIT %s;
                    """,
                    (limit,),
                )
                rows = cur.fetchall()
        finally:
            _pool.putconn(conn)

        return [
            {
                "direction": r[0], "entry_price": r[1],
                "entry_time": r[2].isoformat() if r[2] else None,
                "exit_price": r[3],
                "exit_time": r[4].isoformat() if r[4] else None,
                "exit_reason": r[5], "pnl_points": r[6],
                "chan_reason": r[7], "profile_reason": r[8],
            }
            for r in rows
        ]
    except Exception as e:
        logger.error(f"讀取模擬單歷史失敗: {e}")
        return []
