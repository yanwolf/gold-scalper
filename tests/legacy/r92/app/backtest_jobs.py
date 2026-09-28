"""
背景回測任務 (r90)。

為什麼要有：回測可以指定結束日期、一次連續跑好幾段(例如往回 6 段、每段 30 天)，總時間可能好幾分鐘，
手機瀏覽器/反向代理等不了那麼久。改成：
  送出 → 立刻拿到 job_id → 背景跑 → 網頁每幾秒輪詢一次進度 → 跑完直接顯示
結果存在記憶體，有資料庫時也存一份(backtest_jobs 資料表)——網頁關掉、手機鎖屏、重新整理都不影響，
回來用 job_id 再查就拿得到；服務重啟時還在跑的任務會標成「中斷」，已完成的結果照樣讀得到。

一次只跑一個回測(單一工作執行緒排隊)：回測吃 CPU，同時跑好幾個只會全部變慢、還可能拖到即時資料。
回測本身完全不寫正式設定、不碰真實下單。
"""

import json
import logging
import queue
import threading
import time
import uuid
from datetime import datetime, timezone

from app import backtest as backtest_module
from app import db
from app import trading_stats   # 用模組取函式(呼叫時才找)：測試會把 app/ 的舊版檔換進來，舊版沒有新函式時不能在匯入時就崩

logger = logging.getLogger("backtest_jobs")

MAX_WINDOWS = 12
MAX_MEMORY_JOBS = 20
DAY_MS = 24 * 60 * 60 * 1000

_jobs = {}
_order = []            # 建立順序(舊→新)，記憶體只留最近 MAX_MEMORY_JOBS 個
_lock = threading.Lock()
_queue = queue.Queue()
_worker_started = False


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _iso(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat() if ms is not None else None


# r91：清單上要分得出哪個任務是哪組設定——summary 帶這幾個關鍵參數
LABEL_PARAM_KEYS = ("interval_seconds", "strategy_type", "daily_smc_filter_mode", "daily_smc_against_weight",
                    "trend_filter_mode", "trend_interval_seconds", "trend_slow_multiplier")


def _label_params(job):
    """關鍵參數：以第一段實際用到的值為準(留空＝沿用正式設定時，結果裡才有實際值)，沒有就用送出時的參數。"""
    params = job.get("params") or {}
    out = {k: params.get(k) for k in LABEL_PARAM_KEYS}
    for w in job.get("window_results") or []:
        res = w.get("result") if isinstance(w, dict) else None
        if isinstance(res, dict) and not res.get("error"):
            for k in LABEL_PARAM_KEYS:
                if res.get(k) is not None:
                    out[k] = res[k]
            break
    return out


def _headline(job):
    rows = [w for w in (job.get("window_results") or []) if isinstance(w, dict) and not w.get("error")]
    if job.get("windows") != 1 or not rows:
        return None
    w = rows[0]
    return {k: w.get(k) for k in ("total_trades", "total_pnl_points", "profit_factor", "max_drawdown_points")}


def _summary_of(job):
    """給清單/資料庫 summary 欄位用的精簡版(不含每段完整結果)。"""
    return {
        "label_params": _label_params(job),
        "job_id": job["job_id"], "created_at": job["created_at"], "status": job["status"],
        "finished_at": job.get("finished_at"), "error": job.get("error"),
        "days": job["days"], "windows": job["windows"], "end_date": job.get("end_date"),
        "label": job.get("label"), "aggregate": job.get("aggregate"),
        # r92：只跑一段的任務沒有合計，清單上用那一段的結果當摘要
        "headline": _headline(job),
    }


def _persist(job):
    """有資料庫就寫一份。寫失敗只記 log：結果還在記憶體裡，不影響這次查詢。"""
    try:
        db.save_backtest_job(job["job_id"], job["status"], json.dumps(_summary_of(job), ensure_ascii=False, default=str),
                             json.dumps(job, ensure_ascii=False, default=str))
    except Exception as e:
        logger.warning(f"回測任務寫入資料庫失敗(結果仍在記憶體): {e}")


def _window_row(idx, res, start_ms, end_ms):
    row = {"index": idx, "start": _iso(start_ms), "end": _iso(end_ms)}
    if not isinstance(res, dict) or res.get("error"):
        row["error"] = (res or {}).get("error") if isinstance(res, dict) else "回測沒有結果"
        return row
    for k in ("total_trades", "win_rate", "total_pnl_points", "profit_factor", "max_drawdown_points",
              "daily_bias_days", "daily_smc_breakdown", "skipped_daily_smc", "skipped_trend", "daily_smc_error"):
        row[k] = res.get(k)
    return row


def _aggregate(results):
    trades = []
    for r in results:
        if isinstance(r, dict) and not r.get("error"):
            trades.extend(r.get("recent_trades") or [])
    st = trading_stats.compute_stats(trades)
    return {"total_trades": st["total_trades"], "win_rate": st["win_rate"], "total_pnl_points": st["total_pnl_points"],
            "profit_factor": st["profit_factor"],
            "profit_factor_infinite": st["profit_factor"] is None and st["total_trades"] > 0,
            "max_drawdown_points": st["max_drawdown_points"],
            "daily_smc_breakdown": trading_stats.compute_daily_smc_breakdown(trades)}


def _set_progress(job_id, **kw):
    with _lock:
        job = _jobs.get(job_id)
        if job:
            job["progress"].update(kw)


def _run(job_id):
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            return
        job["status"] = "running"
        job["started_at"] = _now_iso()
        params, days, windows, end_ms = dict(job["params"]), job["days"], job["windows"], job["end_time_ms"]
    _persist(job)
    results = [None] * windows
    try:
        # 最舊的一段先跑(畫面上由舊到新排)
        for n, k in enumerate(range(windows - 1, -1, -1)):
            w_end = end_ms - k * days * DAY_MS
            w_start = w_end - days * DAY_MS + 1
            _set_progress(job_id, window=n + 1, windows=windows, step=0, steps=0, phase="抓取歷史資料")

            def cb(i, total, _jid=job_id):
                _set_progress(_jid, step=i, steps=total, phase="重播計算")

            try:
                res = backtest_module.run_backtest(days=days, end_time_ms=w_end, progress_cb=cb, **params)
            except Exception as e:
                logger.error(f"回測任務第 {n + 1} 段出錯: {type(e).__name__}: {e}")
                res = {"error": f"{type(e).__name__}: {e}"}
            results[n] = res
            with _lock:
                job["window_results"].append({**_window_row(n, res, w_start, w_end), "result": res})
            _persist(job)
        with _lock:
            job["aggregate"] = _aggregate(results) if windows > 1 else None
            job["status"] = "done"
            job["finished_at"] = _now_iso()
    except Exception as e:   # 防禦：外層出錯也要把任務結束掉，不能永遠停在「執行中」
        logger.error(f"回測任務出錯: {type(e).__name__}: {e}")
        with _lock:
            job["status"] = "error"
            job["error"] = f"{type(e).__name__}: {e}"
            job["finished_at"] = _now_iso()
    _persist(job)


def _worker():
    while True:
        job_id = _queue.get()
        try:
            _run(job_id)
        finally:
            _queue.task_done()


def _ensure_worker():
    global _worker_started
    with _lock:
        if _worker_started:
            return
        _worker_started = True
    threading.Thread(target=_worker, name="backtest-jobs", daemon=True).start()


def start_job(params, days=30, windows=1, end_date=None, label=None, now=None):
    """
    送出一個回測任務，立刻回傳 {"job_id", "queue_position"}；參數錯誤回 {"error"}。
    params：run_backtest 的其他參數(不含 days／end_time_ms／progress_cb)。
    windows：從結束日期往回連續跑幾段，每段 days 天(最舊的先跑)。
    """
    try:
        windows = int(windows)
        days = int(days)
    except (TypeError, ValueError):
        return {"error": "天數和段數要是整數"}
    if not 1 <= windows <= MAX_WINDOWS:
        return {"error": f"連續段數要在 1～{MAX_WINDOWS} 之間"}
    if days < 1:
        return {"error": "天數至少 1 天"}
    try:
        end_ms = backtest_module.resolve_end_time_ms(end_date, now=now)
    except ValueError:
        return {"error": f"結束日期格式要是 YYYY-MM-DD(UTC)，收到 {end_date}"}
    bad = {"days", "end_time_ms", "progress_cb"} & set(params)
    if bad:
        return {"error": f"參數不能包含 {sorted(bad)}"}
    job_id = uuid.uuid4().hex[:12]
    job = {
        "job_id": job_id, "created_at": _now_iso(), "status": "queued", "label": label,
        "params": params, "days": days, "windows": windows, "end_date": end_date or None,
        "end_time_ms": end_ms, "end_time": _iso(end_ms),
        "progress": {"window": 0, "windows": windows, "step": 0, "steps": 0, "phase": "排隊中"},
        "window_results": [], "aggregate": None, "error": None, "finished_at": None,
    }
    with _lock:
        _jobs[job_id] = job
        _order.append(job_id)
        while len(_order) > MAX_MEMORY_JOBS:
            old = _order.pop(0)
            if _jobs.get(old, {}).get("status") in ("done", "error", "interrupted"):
                _jobs.pop(old, None)
            else:
                _order.insert(0, old)   # 還在跑/排隊的不丟
                break
        position = sum(1 for j in _jobs.values() if j["status"] in ("queued", "running"))
    _persist(job)
    _ensure_worker()
    _queue.put(job_id)
    return {"job_id": job_id, "queue_position": position}


def get_job(job_id):
    """記憶體優先；沒有就查資料庫。資料庫裡還是排隊/執行中、記憶體卻沒有＝服務重啟過，標成中斷。"""
    with _lock:
        job = _jobs.get(job_id)
        if job:
            return json.loads(json.dumps(job, default=str))
    try:
        ok, payload = db.load_backtest_job(job_id)
    except Exception as e:
        return {"error": f"讀取回測任務失敗：{type(e).__name__}: {e}"}
    if not ok:
        return {"error": f"讀取回測任務失敗：{payload}"}
    if not payload:
        return None
    try:
        job = json.loads(payload)
    except (TypeError, ValueError):
        return {"error": "回測任務的資料讀不懂(資料庫內容損壞)"}
    if job.get("status") in ("queued", "running"):
        job["status"] = "interrupted"
        job["error"] = "服務重啟過，這個任務沒有跑完；已完成的段落照樣顯示，請重新送出"
    return job


def list_jobs(limit=10):
    """最近的任務(新→舊)，只回精簡資訊。記憶體裡的為準，資料庫補上重啟前的。"""
    with _lock:
        mem = {jid: _summary_of(_jobs[jid]) for jid in _order if jid in _jobs}
    out = dict(mem)
    try:
        ok, rows = db.list_backtest_jobs(limit=limit)
        if ok:
            for raw in rows:
                try:
                    s = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if s.get("job_id") and s["job_id"] not in out:
                    if s.get("status") in ("queued", "running"):
                        s["status"] = "interrupted"
                    if "label_params" not in s:
                        # r91 以前存的任務：summary 沒有參數，從完整內容補(最多 limit 筆，只會發生在舊任務)
                        full = get_job(s["job_id"])
                        if isinstance(full, dict) and full.get("job_id"):
                            s["label_params"] = _label_params(full)
                    out[s["job_id"]] = s
    except Exception as e:
        logger.warning(f"讀取回測任務清單失敗: {e}")
    return sorted(out.values(), key=lambda s: s.get("created_at") or "", reverse=True)[:limit]


def wait_idle(timeout=30.0):
    """測試用：等排隊的任務都跑完。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _queue.unfinished_tasks == 0:
            return True
        time.sleep(0.05)
    return False
