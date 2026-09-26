"""Per-run target registry (the single source of target state) and the
run.json log of extractor subprocesses."""
import json
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


def create(run_dir, run_id):
    with _LOCK:
        _save(run_dir, {
            "run_id": run_id,
            "started_at": now(),
            "status": "running",
            "targets": {},
        })


def register(run_dir, targets):
    with _LOCK:
        reg = _load(run_dir)
        for i, path in enumerate(targets, 1):
            key = f"t{i}"
            reg["targets"][path] = {
                "key": key,
                "state": "",
                "out_dir": f"extracted/{key}",
                "error": "",
            }
        _save(run_dir, reg)


def update(run_dir, path, state=None, error=None):
    with _LOCK:
        reg = _load(run_dir)
        entry = reg["targets"][path]
        if state is not None:
            entry["state"] = state
        if error is not None:
            entry["error"] = error
        _save(run_dir, reg)


def set_status(run_dir, status):
    with _LOCK:
        reg = _load(run_dir)
        reg["status"] = status
        _save(run_dir, reg)


def by_state(run_dir, state):
    reg = _load(run_dir)
    return [(p, e) for p, e in reg["targets"].items() if e["state"] == state]


def all_targets(run_dir):
    reg = _load(run_dir)
    return list(reg["targets"].items())


def log_extractor_run(run_dir, entry):
    append_json_list(Path(run_dir) / "run.json", "runs", entry)
