"""Training sync orchestrator — continuous training pipeline.

Runs the full cycle: build dataset → train on Modal → eval → pull adapters.
Provides a background thread API for the menubar to trigger/monitor training.

Usage:
    python -m linus.sync          # Run one cycle
    python -m linus.sync --loop   # Run on 12h schedule
"""

import json
import logging
import os
import subprocess
import sys
import threading
import time
import urllib.request

import snoopy.config as config

log = logging.getLogger(__name__)

DATA_DIR = config.DATA_DIR
LINUS_DIR = DATA_DIR / "linus"
STATE_PATH = LINUS_DIR / "training_state.json"
ADAPTER_DIR = LINUS_DIR / "adapters_modal"

APP_NAME = "linus"
EVAL_THRESHOLD_SCORE = 0.35
SCHEDULE_INTERVAL_S = 12 * 3600  # 12 hours

_DEFAULT_STATE = {
    "status": "idle",
    "last_train_complete_ts": None,
    "last_train_loss": None,
    "last_eval_metrics": None,
    "adapter_version": 0,
    "train_count": 0,
    "last_error": None,
    "last_error_ts": None,
}


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            log.warning("Corrupt state file, resetting")
    return dict(_DEFAULT_STATE)


def save_state(state: dict):
    LINUS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, STATE_PATH)


def check_internet() -> bool:
    try:
        req = urllib.request.Request("https://modal.com", method="HEAD")
        urllib.request.urlopen(req, timeout=5)
        return True
    except (urllib.error.URLError, OSError, TimeoutError):
        return False


def build_dataset() -> dict | None:
    from linus.dataset import build_dataset as _build

    db_path = str(config.DB_PATH)
    output_dir = LINUS_DIR

    try:
        stats = _build(db_path=db_path, output_dir=output_dir)
    except Exception:
        log.exception("Dataset build failed")
        return None

    if stats.get("total_examples", 0) == 0:
        log.warning("Dataset empty — nothing to train on")
        return None

    return stats


def run_training() -> dict | None:
    """Call the deployed train function on Modal via the Python SDK."""
    import modal

    train_path = LINUS_DIR / "sft_train.jsonl"
    val_path = LINUS_DIR / "sft_val.jsonl"

    if not train_path.exists() or not val_path.exists():
        log.error("Dataset files not found")
        return None

    train_jsonl = train_path.read_text()
    val_jsonl = val_path.read_text()
    log.info(
        "Uploading dataset: train=%dKB, val=%dKB",
        len(train_jsonl) // 1024,
        len(val_jsonl) // 1024,
    )

    try:
        train_fn = modal.Function.from_name(APP_NAME, "train")
        result = train_fn.remote(train_jsonl=train_jsonl, val_jsonl=val_jsonl)
        return result
    except Exception:
        log.exception("Modal train call failed")
        return None


def run_eval() -> dict | None:
    """Call the deployed evaluate function on Modal via the Python SDK."""
    import modal

    val_path = LINUS_DIR / "sft_val.jsonl"
    if not val_path.exists():
        log.error("Val set not found")
        return None

    val_jsonl = val_path.read_text()
    log.info("Uploading val set: %dKB", len(val_jsonl) // 1024)

    try:
        eval_fn = modal.Function.from_name(APP_NAME, "evaluate")
        result = eval_fn.remote(val_jsonl=val_jsonl)
        return result
    except Exception:
        log.exception("Modal eval call failed")
        return None


def pull_adapters() -> bool:
    cmd = [
        sys.executable,
        "-m",
        "modal",
        "volume",
        "get",
        "linus-adapters",
        "latest/",
        str(ADAPTER_DIR) + "/",
    ]
    log.info("Pulling adapters: %s", " ".join(cmd))
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        log.exception("Failed to pull adapters")
        return False


def run_cycle():
    """Run one full training cycle: dataset → train → eval → pull."""
    state = load_state()

    # Recover from crash: if status is not idle, a previous run was interrupted
    if state["status"] != "idle":
        log.warning("Recovering from interrupted state: %s", state["status"])
        state["status"] = "idle"
        save_state(state)

    # Step 1: Check internet
    if not check_internet():
        state["last_error"] = "no_internet"
        state["last_error_ts"] = time.time()
        save_state(state)
        log.warning("No internet connection — skipping cycle")
        return

    # Step 2: Build dataset
    state["status"] = "building_dataset"
    save_state(state)
    log.info("Building dataset...")

    ds_stats = build_dataset()
    if ds_stats is None:
        state["status"] = "idle"
        state["last_error"] = "dataset_build_failed"
        state["last_error_ts"] = time.time()
        save_state(state)
        return

    log.info(
        "Dataset: %d train, %d val examples",
        ds_stats.get("train_examples", 0),
        ds_stats.get("val_examples", 0),
    )

    # Step 3: Train on Modal
    state["status"] = "training"
    save_state(state)
    log.info("Starting training on Modal...")

    train_result = run_training()
    if train_result is None:
        state["status"] = "idle"
        state["last_error"] = "training_failed"
        state["last_error_ts"] = time.time()
        save_state(state)
        return

    state["last_train_loss"] = train_result.get("final_train_loss")
    log.info("Training complete: loss=%.4f", state["last_train_loss"] or 0)

    # Step 4: Eval on Modal
    state["status"] = "evaluating"
    save_state(state)
    log.info("Running evaluation on Modal...")

    eval_result = run_eval()
    if eval_result is None:
        state["status"] = "idle"
        state["last_error"] = "eval_failed"
        state["last_error_ts"] = time.time()
        save_state(state)
        return

    state["last_eval_metrics"] = {
        "score": eval_result.get("score", 0),
        "type_accuracy": eval_result.get("type_accuracy", 0),
        "semantic_similarity": eval_result.get("semantic_similarity", 0),
    }
    log.info("Eval: %s", state["last_eval_metrics"])

    # Gate: only pull adapters if eval passes threshold
    score = eval_result.get("score", 0)
    if score < EVAL_THRESHOLD_SCORE:
        state["status"] = "idle"
        state["last_error"] = "eval_below_threshold"
        state["last_error_ts"] = time.time()
        save_state(state)
        log.warning(
            "Eval below threshold (%.3f < %.3f) — keeping old adapters",
            score,
            EVAL_THRESHOLD_SCORE,
        )
        return

    # Step 5: Pull adapters locally
    state["status"] = "pulling"
    save_state(state)
    log.info("Pulling adapters from Modal volume...")

    if not pull_adapters():
        state["status"] = "idle"
        state["last_error"] = "pull_failed"
        state["last_error_ts"] = time.time()
        save_state(state)
        return

    # Success
    state["status"] = "idle"
    state["last_train_complete_ts"] = time.time()
    state["adapter_version"] = state.get("adapter_version", 0) + 1
    state["train_count"] = state.get("train_count", 0) + 1
    state["last_error"] = None
    state["last_error_ts"] = None
    save_state(state)
    log.info("Cycle complete — adapter v%d deployed", state["adapter_version"])


# ── Background thread API (called by menubar) ──────────────────────────

_train_thread: threading.Thread | None = None
_train_lock = threading.Lock()
_schedule_timer: threading.Timer | None = None


def is_training() -> bool:
    return _train_thread is not None and _train_thread.is_alive()


def trigger_train() -> bool:
    """Start a training cycle in a daemon thread. Returns False if already running."""
    global _train_thread
    with _train_lock:
        if is_training():
            return False
        _train_thread = threading.Thread(target=run_cycle, daemon=True)
        _train_thread.start()
        return True


def _schedule_tick():
    trigger_train()
    start_schedule()  # re-arm


def start_schedule():
    global _schedule_timer
    stop_schedule()
    _schedule_timer = threading.Timer(SCHEDULE_INTERVAL_S, _schedule_tick)
    _schedule_timer.daemon = True
    _schedule_timer.start()


def stop_schedule():
    global _schedule_timer
    if _schedule_timer is not None:
        _schedule_timer.cancel()
        _schedule_timer = None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if "--loop" in sys.argv:
        log.info("Starting scheduled training (every %dh)", SCHEDULE_INTERVAL_S // 3600)
        trigger_train()
        start_schedule()
        try:
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            stop_schedule()
            log.info("Stopped")
    else:
        run_cycle()
