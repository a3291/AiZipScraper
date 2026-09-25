"""main.py — unified project entry.

Usage is identical to scripts/cli.py:
    uv run python main.py scan <path> [--force] [--extractor ...] [--config ...]
    uv run python main.py show <path>
    uv run python main.py check <path>
    uv run python main.py export <path> -o digest.jsonl
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))

from cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
