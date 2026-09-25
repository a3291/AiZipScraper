"""run_extractor.py — extractor runner (subprocess entry).

Usage: uv run python scripts/run_extractor.py <extractor name> <in path> <out dir>
  name = a directory or file name under extractors/ (without .py), e.g. default.
  Directory form: extractors/<name>/extractor.py (config lives alongside).
  File form: extractors/<name>.py.

Contract:
  1. The extractor script exposes extract(in_path, out_dir) -> dict.
  2. The result dict must contain every key in RESULT_REQUIRED_KEYS with correct
     types, and result["structure"] every key in STRUCTURE_REQUIRED_KEYS;
     on violation exit code 3 and _result.json is not written.
  3. After validation passes, <out dir>/_result.json is written for the caller.
  4. Heartbeat: <out dir>/_heartbeat.json (pid/started_at) written at start and
     removed on success; a leftover file means the extractor hung mid-run, for
     external observation.
Exit codes: 0 success; 2 bad args; 3 extractor load/execution/contract failure.
The subprocess is started by the caller with the project uv environment's
interpreter; extractors depend only on uv-environment packages and never import
project modules.
"""
import importlib.util
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXTRACTORS = ROOT / "extractors"
EXIT_ARGS, EXIT_FAIL = 2, 3

# _result.json contract: key → type
RESULT_REQUIRED_KEYS = {
    "entry_id": str, "kind": str, "sha256": str,
    "structure": dict, "password_found": bool,
    "warnings": list, "files_kept": int,
}

# inner keys of the structure dict (consumed by cli/publisher)
STRUCTURE_REQUIRED_KEYS = {
    "entry_count": int, "dir_count": int, "total_uncompressed": int,
    "top_extensions": dict, "top_level_dirs": list, "notable_files": list,
    "password_protected": bool, "multi_part": bool,
    "nested_archives": list, "exe_present": bool, "macro_docs": bool,
}


def _validate_result(result) -> str | None:
    """Validate the extractor result contract; returns an error description or None."""
    if not isinstance(result, dict):
        return f"extract() returned {type(result).__name__}, expected dict"
    for key, typ in RESULT_REQUIRED_KEYS.items():
        if key not in result:
            return f"missing required key: {key}"
        if not isinstance(result[key], typ):
            return f"result key {key} should be {typ.__name__}, got {type(result[key]).__name__}"
    structure = result["structure"]
    for key, typ in STRUCTURE_REQUIRED_KEYS.items():
        if key not in structure:
            return f"missing required structure key: {key}"
        if not isinstance(structure[key], typ):
            return (f"structure key {key} should be {typ.__name__}, "
                    f"got {type(structure[key]).__name__}")
    return None


def _heartbeat(out_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "_heartbeat.json").write_text(json.dumps({
        "pid": os.getpid(),
        "started_at": datetime.now().astimezone().isoformat(),
    }, ensure_ascii=False), encoding="utf-8")


def load_extractor(name: str):
    """Load an extractor module by name: directory (extractor.py) or single file."""
    for cand in (EXTRACTORS / name / "extractor.py", EXTRACTORS / f"{name}.py"):
        if cand.is_file():
            spec = importlib.util.spec_from_file_location(
                f"extractors.{name}", cand)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[f"extractors.{name}"] = mod
            spec.loader.exec_module(mod)
            return mod
    return None


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("Usage: run_extractor.py <extractor name> <in path> <out dir>",
              file=sys.stderr)
        return EXIT_ARGS
    name, in_path, out_dir = argv
    if name.startswith("_") or "/" in name or "\\" in name:
        print(f"illegal extractor name: {name}", file=sys.stderr)
        return EXIT_FAIL
    out = Path(out_dir)

    try:
        mod = load_extractor(name)
        if mod is None or not hasattr(mod, "extract"):
            print(f"extractor missing or has no extract(): extractors/{name}",
                  file=sys.stderr)
            return EXIT_FAIL
        _heartbeat(out)
        result = mod.extract(in_path, out)
        err = _validate_result(result)
        if err is not None:
            print(f"extractor result contract violation: {err}", file=sys.stderr)
            return EXIT_FAIL
    except Exception as e:
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        return EXIT_FAIL

    out.mkdir(parents=True, exist_ok=True)
    (out / "_heartbeat.json").unlink(missing_ok=True)   # heartbeat removed
    (out / "_result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
