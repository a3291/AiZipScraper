"""run_extractor.py — 提取脚本运行器（子进程入口）。

用法: uv run python scripts/run_extractor.py <extractor名> <传入路径> <传出目录>
  extractor名 = extractors/ 下的目录名或文件名（不含 .py），如 default。
  目录形态: extractors/<name>/extractor.py（配置放同目录）。
  文件形态: extractors/<name>.py。

契约：
  1. 提取脚本暴露 extract(in_path, out_dir) -> dict。
  2. 结果 dict 必须含 RESULT_REQUIRED_KEYS 全部键且类型正确——运行器在源头
     校验，不合规退出码 3，绝不写出 _result.json（下游拿到即信任）。
  3. 校验通过后写 <传出目录>/_result.json 供调用方读取。
  4. 心跳：起手写 <传出目录>/_heartbeat.json（pid/started_at），成功后删除；
     若该文件残留，说明提取器中途挂死，供外部观测。
退出码：0 成功；2 参数错误；3 提取器加载/执行/契约校验失败。
子进程由调用方以项目 uv 环境的解释器启动；提取器只依赖 uv 环境内依赖，
不 import 项目外围模块。
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

# _result.json 契约：键 → 类型（dict 内部结构由提取器自责）
RESULT_REQUIRED_KEYS = {
    "entry_id": str, "kind": str, "sha256": str,
    "structure": dict, "password_found": bool,
    "warnings": list, "files_kept": int,
}


def _validate_result(result) -> str | None:
    """校验提取结果契约，返回错误描述或 None。"""
    if not isinstance(result, dict):
        return f"extract() 返回 {type(result).__name__}，应为 dict"
    for key, typ in RESULT_REQUIRED_KEYS.items():
        if key not in result:
            return f"结果缺少必需键: {key}"
        if not isinstance(result[key], typ):
            return f"结果键 {key} 应为 {typ.__name__}，实际 {type(result[key]).__name__}"
    return None


def _heartbeat(out_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "_heartbeat.json").write_text(json.dumps({
        "pid": os.getpid(),
        "started_at": datetime.now().astimezone().isoformat(),
    }, ensure_ascii=False), encoding="utf-8")


def load_extractor(name: str):
    """按名加载提取器模块：目录（extractor.py）或单文件。"""
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
        print("用法: run_extractor.py <extractor名> <传入路径> <传出目录>",
              file=sys.stderr)
        return EXIT_ARGS
    name, in_path, out_dir = argv
    if name.startswith("_") or "/" in name or "\\" in name:
        print(f"非法提取器名: {name}", file=sys.stderr)
        return EXIT_FAIL
    out = Path(out_dir)

    try:
        mod = load_extractor(name)
        if mod is None or not hasattr(mod, "extract"):
            print(f"提取器不存在或缺 extract(): extractors/{name}", file=sys.stderr)
            return EXIT_FAIL
        _heartbeat(out)                     # 心跳起手；成功后删除，残留=挂死现场
        result = mod.extract(in_path, out)
        err = _validate_result(result)      # 契约硬化：源头校验，不合规不落盘
        if err is not None:
            print(f"提取结果契约不合规: {err}", file=sys.stderr)
            return EXIT_FAIL
    except Exception as e:
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        return EXIT_FAIL

    out.mkdir(parents=True, exist_ok=True)
    (out / "_heartbeat.json").unlink(missing_ok=True)   # 心跳撤除
    (out / "_result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
