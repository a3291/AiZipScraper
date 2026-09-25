"""run_extractor.py — 提取脚本通用运行器。

用法: uv run python scripts/run_extractor.py <extractor名> <传入路径> <传出目录>
  extractor名 = extractors/ 下的目录名或文件名（不含 .py），如 default。
  目录形态: extractors/<name>/__init__.py（自持密码等配置于同目录）。
  文件形态: extractors/<name>.py。

契约：提取脚本暴露 extract(in_path, out_dir) -> dict；运行器把结果 dict
落为 <传出目录>/_result.json（CLI 读取后交给环3/环5）。
退出码：0 成功；2 参数错误；3 提取器加载/执行失败。
子进程由 CLI 以项目 uv 环境的解释器启动；提取器只依赖 uv 环境内依赖，
不 import 项目外围模块。
"""
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXIT_ARGS, EXIT_FAIL = 2, 3


def load_extractor(name: str):
    """按名加载提取器模块：目录（__init__.py）或单文件。"""
    base = ROOT / "extractors"
    for cand in (base / name / "__init__.py", base / f"{name}.py"):
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
        print("用法: run_extractor.py <extractor名> <传入路径> <传出目录>",
              file=sys.stderr)
        return EXIT_ARGS
    name, in_path, out_dir = argv
    if name.startswith("_") or "/" in name or "\\" in name:
        print(f"非法提取器名: {name}", file=sys.stderr)
        return EXIT_FAIL

    try:
        mod = load_extractor(name)
        if mod is None or not hasattr(mod, "extract"):
            print(f"提取器不存在或缺 extract(): extractors/{name}", file=sys.stderr)
            return EXIT_FAIL
        result = mod.extract(in_path, out_dir)
    except Exception as e:
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        return EXIT_FAIL

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "_result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
