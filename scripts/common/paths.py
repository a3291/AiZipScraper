"""Project layout constants and JSON file IO."""
import json
import os
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = ROOT / "scripts"
EXTRACTORS = ROOT / "extractors"
RUNS_DIR = ROOT / "runs"


def write_json(path, obj):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), "utf-8")
    os.replace(tmp, path)


def read_json(path):
    return json.loads(Path(path).read_text("utf-8"))


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")
