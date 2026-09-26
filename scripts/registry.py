"""Per-run registry (the single source of run state) and the run.json log
of extractor subprocesses. One run carries exactly one target."""
import threading
import time
from pathlib import Path

from paths import append_json_list, now, read_json, write_json

_LOCK = threading.Lock()


def new_run_dir(runs_dir):
    run_id = time.strftime("%Y%m%d-%H%M%S")
    run_dir = Path(runs_dir) / run_id
    n = 2
    while run_dir.exists():
        run_dir = Path(runs_dir) / f"{run_id}-{n}"
        n += 1
    run_dir.mkdir(parents=True)
    return run_id, run_dir


def _path(run_dir):
    return Path(run_dir) / "registry.json"


def _load(run_dir):
    return read_json(_path(run_dir))


def _save(run_dir, doc):
    write_json(_path(run_dir), doc)


def create(run_dir, run_id, target):
    with _LOCK:
        _save(run_dir, {
            "run_id": run_id,
            "target": str(target),
            "started_at": now(),
            "status": "running",
            "state": "",
            "error": "",
        })


def update(run_dir, state=None, error=None):
    with _LOCK:
        reg = _load(run_dir)
        if state is not None:
            reg["state"] = state
        if error is not None:
            reg["error"] = error
        _save(run_dir, reg)


def set_status(run_dir, status):
    with _LOCK:
        reg = _load(run_dir)
        reg["status"] = status
        _save(run_dir, reg)


def load(run_dir):
    return _load(run_dir)


def log_extractor_run(run_dir, entry):
    append_json_list(Path(run_dir) / "run.json", "runs", entry)
