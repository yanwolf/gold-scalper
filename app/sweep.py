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

# 固定點數模式下才有意義的參數：ATR模式開啟時，run_backtest會直接忽略這些
# 固定點數值(改用ATR動態計算)，這時候掃這些參數只會拿到一堆跟對照組一模一樣
# 的無意義結果(這是實際發生過的bug，見README修正記錄)
FIXED_MODE_PARAM_CANDIDATES = {
    "paper_sl_points": [3.0, 5.0, 8.0, 12.0],
    "paper_trail_trigger_points": [3.0, 6.0, 10.0],
    "paper_trail_distance_points": [3.0, 5.0, 8.0],
}

# ATR模式開啟時才有意義的參數：固定點數模式下，改這些倍數同樣不會有任何效果
ATR_MODE_PARAM_CANDIDATES = {
    "paper_atr_sl_multiplier": [1.0, 1.5, 2.0, 3.0],
    "paper_atr_trigger_multiplier": [1.0, 1.5, 2.0],
    "paper_atr_trail_multiplier": [0.8, 1.2, 1.8],
}

# 不管哪種模式都有實際效果的參數
ALWAYS_ACTIVE_PARAM_CANDIDATES = {
    "paper_reversal_confirm_count": [1, 2, 3],
}

# 震盪濾網開啟時才有意義的參數：濾網關閉時，改門檻同樣不會有任何效果
CHOP_FILTER_PARAM_CANDIDATES = {
    "paper_chop_threshold": [40.0, 50.0, 70.0, 80.0],
}

PARAM_LABELS = {
    "paper_sl_points": "初始停損",
    "paper_trail_trigger_points": "移動停損觸發距離",
    "paper_trail_distance_points": "移動停損跟隨距離",
    "paper_reversal_confirm_count": "訊號反轉確認次數",
    "paper_atr_sl_multiplier": "ATR初始停損倍數",
    "paper_atr_trigger_multiplier": "ATR移動停損觸發倍數",
    "paper_atr_trail_multiplier": "ATR移動停損跟隨倍數",
    "paper_chop_threshold": "震盪濾網門檻",
}

_jobs = {}
_jobs_lock = threading.Lock()


def _build_combos(baseline_overrides=None):
    """
    baseline(目前生效中的設定，或使用者指定的覆寫參數疊加上去之後的結果)當
    對照組，其他每組都是「改一個參數」的變化版本。

    baseline_overrides：使用者可以指定一組覆寫值，疊加在正式設定上面組成
    真正要用的baseline，不需要真的去改「策略參數設定」那邊的正式設定——
    跟單次回測面板的「回測交易參數(獨立於正式設定)」是同一個概念，讓使用者
    想用某組假設參數當基準做敏感度分析時，不用被迫先去改動正式設定
    (那樣會導致即時模擬單的績效統計排除舊交易，修正記錄見README)。
    不提供的話(None)完全比照原本行為，直接用正式設定當baseline。

    重要：只測「在目前模式下真的會影響結果」的參數——如果baseline目前是ATR
    動態模式，就測ATR的三個倍數，不測固定點數(因為固定點數模式關閉時完全不會
    被用到，測了也是白測，會出現一堆數值跟對照組一樣的無意義結果)；反過來
    如果baseline是固定點數模式，就只測固定點數，不測ATR倍數。震盪濾網門檻
    也是同樣邏輯：只有濾網開啟時才測門檻數值，濾網關閉時測門檻沒有意義。

    另外固定加兩組「切換開關」的對照(ATR動態停損、震盪濾網)，方便直接比較
    開啟/關閉哪個表現比較好。
    """
    baseline = dict(settings_module.get_settings())
    if baseline_overrides:
        for key, value in baseline_overrides.items():
            if value is not None and key in baseline:
                baseline[key] = value

    combos = [{"label": "目前設定(對照組)" if not baseline_overrides else "指定基準(對照組)", "params": dict(baseline)}]

    mode_specific_candidates = (
        ATR_MODE_PARAM_CANDIDATES if baseline["paper_use_atr_stops"] else FIXED_MODE_PARAM_CANDIDATES
    )
    all_candidates = {**mode_specific_candidates, **ALWAYS_ACTIVE_PARAM_CANDIDATES}
    if baseline["paper_use_chop_filter"]:
        all_candidates = {**all_candidates, **CHOP_FILTER_PARAM_CANDIDATES}

    for param_key, candidates in all_candidates.items():
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
    atr_toggle_label = "ATR動態停損(關閉，改用固定點數)" if baseline["paper_use_atr_stops"] else "ATR動態停損(開啟)"
    combos.append({"label": atr_toggle_label, "params": atr_toggle_params})

    chop_toggle_params = dict(baseline)
    chop_toggle_params["paper_use_chop_filter"] = 0 if baseline["paper_use_chop_filter"] else 1
    chop_toggle_label = "震盪濾網(關閉)" if baseline["paper_use_chop_filter"] else "震盪濾網(開啟)"
    combos.append({"label": chop_toggle_label, "params": chop_toggle_params})

    return combos


def start_sweep(days=2, interval_seconds=60, baseline_overrides=None):
    """啟動一次掃描，立刻回傳job_id，實際運算在背景執行緒進行。"""
    job_id = str(uuid.uuid4())[:8]
    combos = _build_combos(baseline_overrides=baseline_overrides)

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
                use_chop_filter=bool(combo["params"]["paper_use_chop_filter"]),
                chop_threshold=combo["params"]["paper_chop_threshold"],
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
