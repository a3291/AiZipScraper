"""Project layout constants and JSON file IO."""
import json
import os
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = ROOT / "scripts"
EXTRACTORS = ROOT / "extractors"
RUNS_DIR = ROOT / "runs"

_WRITE_LOCK = threading.Lock()


def write_json(path, obj):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), "utf-8")
    os.replace(tmp, path)


def read_json(path):
    return json.loads(Path(path).read_text("utf-8"))


def read_pkgs(path):
    """Per-key package archive: the existing doc or a fresh {"packages": {}}."""
    p = Path(path)
    return read_json(p) if p.exists() else {"packages": {}}


def update_pkg(path, key, value):
    """Atomically set packages[key] = value (safe across threads)."""
    with _WRITE_LOCK:
        doc = read_pkgs(path)
        doc["packages"][key] = value
        write_json(path, doc)


def append_json_list(path, key, item):
    """Atomically append item to the list stored under key."""
    with _WRITE_LOCK:
        p = Path(path)
        doc = read_json(p) if p.exists() else {key: []}
        doc[key].append(item)
        write_json(p, doc)


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")
