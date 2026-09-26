"""paths.py — project path constants (anchored to the project root, CWD-independent)."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
CONFIG = ROOT / "config.json"
JSONS = ROOT / "jsons"
EXTRACTORS = ROOT / "extractors"
RUNS_DIR = ROOT / "runs"
