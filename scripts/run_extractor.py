"""Subprocess entry: load an extractor and run it against one target.

Usage: run_extractor.py <extractor_name> <target_path> <out_dir>
Success prints the extract() result dict as JSON on stdout and exits 0.
Bad arguments exit 2; extractor load or run failure exits 3 with a traceback
on stderr. Exit code is the only success signal; stdout/stderr are archived
by the caller in run.json.
"""
import importlib.util
import json
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_extractor(name):
    if not name or name.startswith("_") or "/" in name or "\\" in name or name in (".", ".."):
        raise ValueError(f"bad extractor name: {name}")
    module_path = ROOT / "extractors" / name / "extractor.py"
    if not module_path.is_file():
        raise FileNotFoundError(f"extractor not found: {module_path}")
    spec = importlib.util.spec_from_file_location(f"extractor_{name}", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(argv):
    if len(argv) != 4:
        print(__doc__, file=sys.stderr)
        return 2
    name, in_path, out_dir = argv[1], argv[2], argv[3]
    try:
        module = load_extractor(name)
    except Exception:
        traceback.print_exc()
        return 3
    try:
        result = module.extract(in_path, Path(out_dir))
    except Exception:
        traceback.print_exc()
        return 3
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
