"""Project layout constants and JSON file IO."""
import json
import os
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
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


def read_or(path, default):
    """The existing doc or the default; missing file is first read, not a
    fallback mid-flow."""
    p = Path(path)
    return read_json(p) if p.exists() else default


def append_json_list(path, key, item):
    """Atomically append item to the list stored under key."""
    with _WRITE_LOCK:
        p = Path(path)
        doc = read_json(p) if p.exists() else {key: []}
        doc[key].append(item)
        write_json(p, doc)


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")
