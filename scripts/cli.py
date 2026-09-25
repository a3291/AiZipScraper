"""cli.py — 刮削器命令行入口（链环形态编排层）。

子命令：
  scan    批量刮削任意文件/目录（六环：扫描→提取→打包→会话→校验落盘→收尾）
  show    中文映射展示 .publish.json
  check   体检：侧车缺失 / 哈希不匹配 / 低置信
  export  汇总导出 JSONL

环2 提取为可插拔子进程：--extractor 从 extractors/ 按名选择（目录或单文件），
经 scripts/run_extractor.py 在项目 uv 环境执行；契约 = 传入路径（单文件）
+ 传出路径（目录）；密码由提取器自持（extractors/<name>/password.json），
CLI 不经手。全局并发统一于 jsons/scraper.json 顶层 concurrency
（extract 提取子进程数 / api 识别并发路数）。

run 目录（runs/<random_id>/，仅历史记录，无 resume）：
  checklist.json  context.json  messages.json  extracted/<entry_id>/
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import context_builder
import publisher
import schema
from ai_identify import (LOW_CONFIDENCE, MessageLog, datetime_now_iso,
                         load_config, load_prompts, run_session)

ROOT = Path(__file__).resolve().parent.parent
RUNS_DIR = Path("runs")
PH_DONE, PH_FAIL, PH_SKIP = "done", "failed", "skipped"
EXTRACT_TIMEOUT_S = 1800   # 单目标提取子进程超时（脚本内定死）
CL_LOCK = threading.Lock()   # checklist.json 并发写锁（api 并发段共用）

SHOW_ZH = {
    "schema_version": "契约版本", "sha256": "内容哈希(SHA256)",
    "source_path": "锚定路径", "file_size": "文件大小", "mtime": "修改时间",
    "scraped_at": "刮削时间", "engine": "识别引擎", "depth": "识别深度",
    "confidence": "置信度", "title": "名称", "category": "大类",
    "summary": "总结", "tags": "标签", "language": "语言",
}


def _atomic_write_json(path: Path, obj: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


# ---------- 环1 扫描 ----------

def find_targets(root: str | Path, recursive: bool = True) -> list[Path]:
    """所有文件皆为目标；排除 runs/ 与 *.publish.json。"""
    root = Path(root)
    if root.is_file():
        return [root]
    it = root.rglob("*") if recursive else root.glob("*")
    out = []
    for p in sorted(it):
        if not p.is_file():
            continue
        if RUNS_DIR.parts[-1] in p.parts:
            continue
        if p.name.endswith(".publish.json"):
            continue
        out.append(p)
    return out


def sha256_file(path, buf: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(buf):
            h.update(chunk)
    return h.hexdigest()


def new_run_dir() -> Path:
    run_dir = RUNS_DIR / secrets.token_hex(4)
    n = 2
    while run_dir.exists():
        run_dir = RUNS_DIR / f"{secrets.token_hex(4)}-{n}"
        n += 1
    (run_dir / "extracted").mkdir(parents=True)
    return run_dir


# ---------- 环6 checklist ----------

def _new_pkg_rec(target: Path) -> dict:
    return {
        "name": target.name, "path": str(target),
        "extract": {"phase": "pending", "files_kept": 0, "dir": ""},
        "ai": {"phase": "pending", "published": False},
        "publish": {"phase": "pending", "sidecar": ""},
        "stats": {"pages": 0, "pages_read": 0, "messages": 0, "turns": 0,
                  "tokens_in": 0, "tokens_out": 0,
                  "sentences_skipped": 0, "elapsed_s": 0.0},
    }


# ---------- 主流程 ----------

def extract_one(target: Path, run_dir: Path,
                extractor_name: str) -> tuple[dict | None, str | None]:
    """环2：子进程运行提取脚本，返回 (结果dict, None) 或 (None, 错误信息)。

    密码由提取器自持（extractors/<name>/password.json），CLI 不经手。
    """
    entry_id = sha256_file(target)[:8]
    out_dir = run_dir / "extracted" / entry_id
    cmd = [sys.executable, str(ROOT / "scripts" / "run_extractor.py"),
           extractor_name, str(target), str(out_dir)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=EXTRACT_TIMEOUT_S, cwd=ROOT)
    except subprocess.TimeoutExpired:
        return None, f"提取超时（>{EXTRACT_TIMEOUT_S}s）"
    if proc.returncode != 0:
        tail = (proc.stderr.strip().splitlines() or ["extractor failed"])[-1]
        return None, tail
    try:
        return json.loads((out_dir / "_result.json").read_text(
            encoding="utf-8-sig")), None
    except (OSError, json.JSONDecodeError) as e:
        return None, f"_result.json 读取失败: {e}"


def identify_and_publish(target: Path, run_dir: Path, cfg: dict, prompts: dict,
                         cl: dict, ex: dict) -> dict:
    """环3→环5：打包、会话、模板校验落盘（提取结果 ex 由环2产出）。"""
    rec = {"name": target.name, "outcome": "", "detail": ""}
    entry_id = ex["entry_id"]
    pkg = cl["packages"].setdefault(str(target), _new_pkg_rec(target))
    pkg["extract"].update(phase=PH_DONE, files_kept=ex["files_kept"],
                          dir=f"extracted/{entry_id}")
    _save(cl, run_dir)

    # 环3 打包（只读 extracted/）
    try:
        ctx_pkg = context_builder.build(run_dir / "extracted" / entry_id,
                                        page_chars=cfg["page_chars"])
    except Exception as e:
        pkg["ai"].update(phase=PH_FAIL, detail=f"context: {e}")
        rec["outcome"] = "fail-context"
        rec["detail"] = f"{type(e).__name__}: {e}"
        return rec
    ctx_path = run_dir / "context.json"
    try:
        ctx_all = json.loads(ctx_path.read_text(encoding="utf-8-sig")) \
            if ctx_path.exists() else {"version": "2.0", "packages": {}}
        ctx_all["packages"][entry_id] = ctx_pkg
        _atomic_write_json(ctx_path, ctx_all)
    except OSError as e:
        rec["outcome"] = "fail-context"
        rec["detail"] = f"context 写盘失败: {e}"
        return rec

    # 环4 会话
    mlog = MessageLog(run_dir, entry_id)
    t0 = datetime.now().astimezone()
    try:
        identity, conf, warnings, stats = run_session(
            ctx_pkg, prompts, cfg, mlog, hint_exts=ex["structure"]["top_extensions"])
    except Exception as e:
        pkg["ai"].update(phase=PH_FAIL, detail=f"{type(e).__name__}: {e}")
        rec["outcome"] = "fail-ai"
        rec["detail"] = f"{type(e).__name__}: {e}"
        return rec
    elapsed = (datetime.now().astimezone() - t0).total_seconds()
    warnings.extend(ex["warnings"])
    if ex["password_found"]:
        warnings.append("密码轮询命中（密码本身不落盘）")
    published = "_fallback_reason" not in identity
    identity["confidence"] = conf   # 环5 填模板需要
    pkg["ai"].update(phase=PH_DONE, published=published,
                     detail=identity.get("_fallback_reason", ""))
    pkg["stats"].update(pages=stats["pages"], pages_read=stats["pages_read"],
                        messages=mlog.count(), turns=stats["turns"],
                        tokens_in=stats["tokens_in"],
                        tokens_out=stats["tokens_out"],
                        sentences_skipped=ctx_pkg["stats"]["sentences_skipped"],
                        elapsed_s=round(elapsed, 1))
    _save(cl, run_dir)

    # 环5 校验落盘
    program = {
        "sha256": ex["sha256"],
        "source_path": str(target.resolve()),
        "file_size": target.stat().st_size,
        "mtime": datetime.fromtimestamp(target.stat().st_mtime) \
            .astimezone().isoformat(),
        "scraped_at": datetime_now_iso(),
        "engine": f"{cfg['provider']}:{cfg.get('model') or 'auto'}",
        "depth": "full" if ex["kind"] == "archive" else "direct",
        "structure": ex["structure"],
        "flags": {
            "password_protected": ex["structure"].get("password_protected", False),
            "multi_part": False,
            "nested_archives": [],
            "exe_present": False,
            "macro_docs": False,
        },
    }
    side, problems = publisher.publish(target, program, identity, warnings)
    if side is None:
        pkg["publish"].update(phase=PH_FAIL, detail=f"校验未过: {problems}")
        rec["outcome"] = "fail-publish"
        rec["detail"] = f"校验未过: {problems}"
        return rec
    pkg["publish"].update(phase=PH_DONE, sidecar=str(side))
    rec["outcome"] = "ok"
    rec["detail"] = f"confidence={conf} pages={stats['pages']}"
    return rec


def process_one(target: Path, run_dir: Path,
                cfg: dict, prompts: dict, cl: dict,
                extractor_name: str = "default") -> dict:
    """单目标全链（环2→环5），供脚本/测试直接调用。"""
    ex, err = extract_one(target, run_dir, extractor_name)
    if err is not None:
        rec = {"name": target.name, "outcome": "fail-extract", "detail": err}
        pkg = cl["packages"].setdefault(str(target), _new_pkg_rec(target))
        pkg["extract"].update(phase=PH_FAIL, detail=err)
        return rec
    return identify_and_publish(target, run_dir, cfg, prompts, cl, ex)


def _save(cl: dict, run_dir: Path) -> None:
    with CL_LOCK:
        cl["updated_at"] = datetime_now_iso()
        _atomic_write_json(run_dir / "checklist.json", cl)


def cmd_scan(args) -> int:
    cfg = load_config(args.config)
    prompts = load_prompts()
    conc = cfg["concurrency"]   # 全局并发统一：extract 提取子进程数 / api 识别并发路数
    targets = find_targets(args.path, recursive=not args.no_recurse)
    if not targets:
        print("未找到目标文件")
        return 0

    run_dir = new_run_dir()
    cl = {
        "run_id": run_dir.name,
        "started_at": datetime_now_iso(),
        "status": "running",
        "concurrency": conc,
        "packages": {str(t): _new_pkg_rec(t) for t in targets},
    }
    _save(cl, run_dir)
    print(f"run: {run_dir}（{len(targets)} 个目标，extract×{conc['extract']} "
          f"api×{conc['api']}）")

    counters: dict[str, int] = {}
    low_conf: list[str] = []
    try:
        work = [t for t in targets
                if args.force or not Path(str(t) + ".publish.json").exists()]

        # 并发段1：环2 提取（extract 并发，每目标独立传出目录天然并发安全）
        ex_map: dict[Path, tuple[dict | None, str | None]] = {}
        if work:
            print(f"提取（{args.extractor} × {conc['extract']} 并发）...")
            with ThreadPoolExecutor(max_workers=conc["extract"]) as pool:
                futs = {pool.submit(extract_one, t, run_dir,
                                    args.extractor): t for t in work}
                for f in as_completed(futs):
                    t = futs[f]
                    ex_map[t] = f.result()
                    mark = "+" if ex_map[t][0] else "!"
                    err = ex_map[t][1]
                    print(f"  {mark} {t.name}" + (f"  {err}" if err else ""))
            for t, (ex, err) in ex_map.items():
                if err is not None:
                    pkg = cl["packages"].setdefault(str(t), _new_pkg_rec(t))
                    pkg["extract"].update(phase=PH_FAIL, detail=err)
            _save(cl, run_dir)

        # 并发段2：环3→环5 识别（api 并发路数；messages/checklist 已加锁）
        id_targets = [t for t in work
                      if not (t in ex_map and ex_map[t][0] is None)]
        id_recs: dict[Path, dict] = {}
        if id_targets:
            print(f"识别（× {conc['api']} 并发）...")
            with ThreadPoolExecutor(max_workers=conc["api"]) as pool:
                futs = {pool.submit(identify_and_publish, t, run_dir, cfg,
                                    prompts, cl, ex_map[t][0]): t
                        for t in id_targets}
                for f in as_completed(futs):
                    id_recs[futs[f]] = f.result()

        # 汇总打印（保持目标顺序）
        for i, t in enumerate(targets, 1):
            side_path = Path(str(t) + ".publish.json")
            if side_path.exists() and not args.force:
                counters["cached"] = counters.get("cached", 0) + 1
                print(f"[{i}/{len(targets)}] = {t.name}  cached")
                continue
            if t in ex_map and ex_map[t][0] is None:
                counters["fail-extract"] = counters.get("fail-extract", 0) + 1
                print(f"[{i}/{len(targets)}] ! {t.name}  {ex_map[t][1]}")
                continue
            rec = id_recs.get(t)
            if rec is None:
                continue
            counters[rec["outcome"]] = counters.get(rec["outcome"], 0) + 1
            mark = "+" if rec["outcome"] == "ok" else "!"
            print(f"[{i}/{len(targets)}] {mark} {rec['name']}  {rec['detail']}")
            if rec["outcome"] == "ok":
                doc = json.loads(side_path.read_text(encoding="utf-8-sig"))
                c = doc["scrape"]["confidence"]
                if c < LOW_CONFIDENCE:
                    low_conf.append(f"{t} ({c:.2f})")
    except KeyboardInterrupt:
        cl["status"] = "interrupted"
        _save(cl, run_dir)
        print(f"\n中断：现场已存档 {run_dir}（无 resume，下次 scan 开新 run）")
        return 130

    cl["status"] = "done"
    _save(cl, run_dir)
    print(f"\n共 {len(targets)} 个目标 | "
          + " | ".join(f"{k} {v}" for k, v in counters.items()))
    print(f"run 记录: {run_dir}")
    if low_conf:
        print("低置信（建议人工复核或 --force 重刮）:")
        for s in low_conf:
            print(f"  {s}")
    return 0


# ---------- 只读命令（读 .publish.json） ----------

def _load_publish(target: Path) -> tuple[Path, dict] | None:
    sp = Path(str(target) + ".publish.json") if target.is_file() \
        else None
    if sp and sp.exists():
        return sp, json.loads(sp.read_text(encoding="utf-8-sig"))
    return None


def cmd_show(args) -> int:
    target = Path(args.path)
    docs: list[tuple[Path, dict]] = []
    if target.is_file():
        if target.name.endswith(".publish.json"):
            docs.append((target, json.loads(target.read_text(encoding="utf-8-sig"))))
        else:
            got = _load_publish(target)
            if not got:
                print(f"无侧车: {target}.publish.json")
                return 1
            docs.append(got)
    else:
        for sp in sorted(target.glob("*.publish.json")):
            docs.append((sp, json.loads(sp.read_text(encoding="utf-8-sig"))))
    for sp, doc in docs:
        print(f"== {sp.name} ==")
        zh = lambda k: SHOW_ZH.get(k, k)
        a, s, i = doc["anchoring"], doc["scrape"], doc["identity"]
        print(f"  {zh('title')}: {i['title'] or '(未识别)'}")
        print(f"  {zh('category')}: {i['category']}   {zh('confidence')}: "
              f"{s['confidence']:.2f}   {zh('depth')}: {s['depth']}")
        print(f"  {zh('tags')}: {'、'.join(i['tags'])}")
        print(f"  {zh('summary')}: {i['summary']}")
        print(f"  {zh('source_path')}: {a['source_path']}")
        print(f"  {zh('scraped_at')}: {s['scraped_at']}   {zh('engine')}: {s['engine']}")
        flags = doc["flags"]
        marks = [n for n, on in (("加密", flags["password_protected"]),
                                 ("含exe", flags["exe_present"]),
                                 ("含宏文档", flags["macro_docs"]),
                                 ("分卷", flags["multi_part"])) if on]
        if marks:
            print(f"  标记: {'、'.join(marks)}")
        if doc["warnings"]:
            print("  警告:")
            for w in doc["warnings"][:5]:
                print(f"    - {w}")
        print()
    return 0


def cmd_check(args) -> int:
    problems = 0
    targets = find_targets(args.path)
    for t in targets:
        got = _load_publish(t)
        if not got:
            print(f"未刮削: {t}")
            problems += 1
            continue
        sp, doc = got
        errs = schema.validate(doc)
        if errs:
            print(f"侧车非法: {sp}: {errs}")
            problems += 1
            continue
        if doc["anchoring"]["sha256"] != sha256_file(t):
            print(f"哈希不匹配（文件已变化）: {t}")
            problems += 1
        if doc["scrape"]["confidence"] < LOW_CONFIDENCE:
            print(f"低置信: {t} ({doc['scrape']['confidence']:.2f})")
    orphans = [p for p in Path(args.path).glob("*.publish.json")
               if not Path(str(p)[:-len(".publish.json")]).exists()]
    for o in orphans:
        print(f"孤儿侧车（无对应目标文件）: {o}")
        problems += 1
    print(f"\n体检完成: {len(targets)} 个目标, 孤儿 {len(orphans)}")
    return 1 if problems else 0


def cmd_export(args) -> int:
    out_path = Path(args.output)
    count = 0
    with out_path.open("w", encoding="utf-8") as out:
        for sp in sorted(Path(args.path).glob("*.publish.json")):
            doc = json.loads(sp.read_text(encoding="utf-8-sig"))
            slim = {
                "file": sp.name[:-len(".publish.json")],
                "path": doc["anchoring"]["source_path"],
                "sha256": doc["anchoring"]["sha256"],
                "category": doc["identity"]["category"],
                "title": doc["identity"]["title"],
                "summary": doc["identity"]["summary"],
                "tags": doc["identity"]["tags"],
                "confidence": doc["scrape"]["confidence"],
                "depth": doc["scrape"]["depth"],
                "password_protected": doc["flags"]["password_protected"],
                "warnings": doc["warnings"],
            }
            out.write(json.dumps(slim, ensure_ascii=False) + "\n")
            count += 1
    print(f"导出 {count} 条 → {out_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="scraper", description="压缩包/文件 AI 刮削识别")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("scan", help="批量刮削（任意文件）")
    p.add_argument("path", help="文件或目录")
    p.add_argument("--force", action="store_true", help="已有侧车也强制重刮")
    p.add_argument("--config", help="AI 配置文件路径（默认 jsons/scraper.json）")
    p.add_argument("--extractor", default="default",
                   help="提取器名（extractors/ 目录下目录名或文件名，默认 default）")
    p.add_argument("--no-recurse", action="store_true", help="不递归子目录")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("show", help="中文映射展示侧车")
    p.add_argument("path")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("check", help="体检：缺失/哈希不匹配/低置信/孤儿")
    p.add_argument("path")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("export", help="汇总导出 JSONL")
    p.add_argument("path")
    p.add_argument("-o", "--output", required=True)
    p.set_defaults(func=cmd_export)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
