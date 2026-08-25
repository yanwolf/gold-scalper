"""
參數掃描(parameter sweep)。

用途：不用一個一個手動試、記錄結果，一次跑好幾組不同風控參數的回測，
自動比較哪一組表現比較好。用「一次只變動一個參數，其他維持目前設定」的
敏感度測試方式(而不是全部參數排列組合的full grid)，因為full grid會讓
組數呈指數成長、跑不完；這種方式組數只會線性成長，能在合理時間內跑完，
還是能清楚看出「調高/調低這個參數，方向對不對」。

用背景執行緒跑(送出掃描 -> 立刻回傳job_id -> 前端輪詢進度)，
不在單一HTTP請求裡等全部跑完，避免請求逾時、也避免卡住事件循環。
"""

import threading
import uuid
import logging
from datetime import datetime, timezone

from app import backtest as backtest_module
from app import settings as settings_module

logger = logging.getLogger("sweep")

# 每個參數要測試的候選值清單，掃描時逐一替換這裡的值(其他參數維持baseline不變)
PARAM_CANDIDATES = {
    "paper_sl_points": [3.0, 5.0, 8.0, 12.0],
    "paper_trail_trigger_points": [3.0, 6.0, 10.0],
    "paper_trail_distance_points": [3.0, 5.0, 8.0],
    "paper_reversal_confirm_count": [1, 2, 3],
}

PARAM_LABELS = {
    "paper_sl_points": "初始停損",
    "paper_trail_trigger_points": "移動停損觸發距離",
    "paper_trail_distance_points": "移動停損跟隨距離",
    "paper_reversal_confirm_count": "訊號反轉確認次數",
}

_jobs = {}
_jobs_lock = threading.Lock()


def _build_combos():
    """
    baseline(目前生效中的設定)當對照組，其他每組都是「改一個參數」的變化版本。
    另外加一組「切換ATR動態停損開關」的對照(如果目前是固定點數模式，就多測一組
    ATR動態模式；反過來也一樣)，方便直接比較兩種停損機制哪個表現比較好。
    """
    baseline = settings_module.get_settings()
    combos = [{"label": "目前設定(對照組)", "params": dict(baseline)}]

    for param_key, candidates in PARAM_CANDIDATES.items():
        for value in candidates:
            if value == baseline[param_key]:
                continue  # 跟對照組一樣的值不用重複跑
            params = dict(baseline)
            params[param_key] = value
            combos.append({
                "label": f"{PARAM_LABELS[param_key]} = {value}",
                "params": params,
            })

    atr_toggle_params = dict(baseline)
    atr_toggle_params["paper_use_atr_stops"] = 0 if baseline["paper_use_atr_stops"] else 1
    toggle_label = "ATR動態停損(關閉)" if baseline["paper_use_atr_stops"] else "ATR動態停損(開啟)"
    combos.append({"label": toggle_label, "params": atr_toggle_params})

    return combos


def start_sweep(days=2, interval_seconds=60):
    """啟動一次掃描，立刻回傳job_id，實際運算在背景執行緒進行。"""
    job_id = str(uuid.uuid4())[:8]
    combos = _build_combos()

    job = {
        "id": job_id,
        "status": "running",
        "total": len(combos),
        "completed": 0,
        "results": [],
        "started_at": datetime.now(timezone.utc).isoformat(),
        "days": days,
        "interval_seconds": interval_seconds,
    }
    with _jobs_lock:
        _jobs[job_id] = job

    thread = threading.Thread(target=_run_sweep, args=(job_id, combos, days, interval_seconds), daemon=True)
    thread.start()

    return job_id


def _run_sweep(job_id, combos, days, interval_seconds):
    for combo in combos:
        try:
            result = backtest_module.run_backtest(
                days=days,
                interval_seconds=interval_seconds,
                sl_points=combo["params"]["paper_sl_points"],
                trail_trigger_points=combo["params"]["paper_trail_trigger_points"],
                trail_distance_points=combo["params"]["paper_trail_distance_points"],
                reversal_confirm_count=combo["params"]["paper_reversal_confirm_count"],
                use_atr=bool(combo["params"]["paper_use_atr_stops"]),
                atr_sl_multiplier=combo["params"]["paper_atr_sl_multiplier"],
                atr_trigger_multiplier=combo["params"]["paper_atr_trigger_multiplier"],
                atr_trail_multiplier=combo["params"]["paper_atr_trail_multiplier"],
            )
            summary = {
                "label": combo["label"],
                "params": combo["params"],
                "total_trades": result.get("total_trades"),
                "win_rate": result.get("win_rate"),
                "total_pnl_points": result.get("total_pnl_points"),
                "profit_factor": result.get("profit_factor"),
                "max_drawdown_points": result.get("max_drawdown_points"),
                "error": result.get("error"),
            }
        except Exception as e:
            logger.error(f"參數掃描其中一組失敗({combo['label']}): {e}")
            summary = {"label": combo["label"], "params": combo["params"], "error": str(e)}

        with _jobs_lock:
            job = _jobs.get(job_id)
            if job is None:
                return
            job["results"].append(summary)
            job["completed"] += 1

    with _jobs_lock:
        job = _jobs.get(job_id)
        if job:
            job["status"] = "done"


def get_job(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None
