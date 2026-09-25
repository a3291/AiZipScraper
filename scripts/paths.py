"""paths.py — single source of project paths (anchored to the project root, CWD-independent)."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
JSONS = ROOT / "jsons"
EXTRACTORS = ROOT / "extractors"
RUNS_DIR = ROOT / "runs"
