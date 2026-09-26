"""Extractor worker: load extractors/<name>/extractor.py and run extract().
run() executes inside a child process started by the orchestrator and hands
the extract() return value (or the exception) back through a queue. There is
no CLI entry and no exit-code protocol; the orchestrator judges normality
from the returned dict."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent


def load_extractor(name):
    if not name or name.startswith("_") or "/" in name or "\\" in name or name in (".", ".."):
        raise ValueError(f"bad extractor name: {name!r}")
    path = ROOT / "extractors" / name / "extractor.py"
    if not path.is_file():
        raise FileNotFoundError(f"extractor not found: {path}")
    spec = importlib.util.spec_from_file_location(f"extractors.{name}.extractor", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(name, in_path, out_dir, q):
    try:
        module = load_extractor(name)
        result = module.extract(in_path, Path(out_dir))
        q.put(("ok", result))
    except Exception as exc:
        q.put(("err", repr(exc)))
