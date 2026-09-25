"""main.py — 项目统一入口。

用法与 cli.py 完全一致：
    uv run python main.py scan <路径> [--pwfile ...] [--force] [--config ...]
    uv run python main.py show <路径>
    uv run python main.py check <路径>
    uv run python main.py export <路径> -o 汇总.jsonl
"""
import sys

from cli import main

if __name__ == "__main__":
    sys.exit(main())
