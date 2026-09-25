"""paths.py — 项目路径单源（全部运行时路径由此计算，锚定项目根，不依赖 CWD）。"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
JSONS = ROOT / "jsons"
EXTRACTORS = ROOT / "extractors"
RUNS_DIR = ROOT / "runs"
