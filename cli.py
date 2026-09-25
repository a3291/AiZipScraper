"""cli.py — 刮削器命令行入口。

子命令：
  scan    批量刮削（默认 listing+sample 深度；每次运行落 runs/<run_id>/ 账本与 AI 留痕）
  show    中文映射展示侧车内容
  check   体检：孤儿侧车 / 哈希不匹配 / 低置信
  export  汇总导出 JSONL
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import extractor
import sidecar
from ai_identify import identify, load_config, LOW_CONFIDENCE
from schema import SCHEMA_VERSION, validate

ARCHIVE_EXTS = {".zip", ".7z"}

RUNS_DIR = Path("runs")
REDO_PHASES = {"pending", "extracted", "failed"}   # resume 时需要重做的 phase

SHOW_ZH = {
    "schema_version": "契约版本",
    "sha256": "内容哈希(SHA256)",
    "source_path": "锚定路径",
    "file_size": "文件大小",
    "mtime": "修改时间",
    "scraped_at": "刮削时间",
    "engine": "识别引擎",
    "depth": "识别深度",
    "confidence": "置信度",
    "title": "名称",
    "category": "大类",
    "summary": "总结",
    "tags": "标签",
    "language": "语言",
}


def find_archives(root: str | Path, recursive: bool = True) -> list[Path]:
    root = Path(root)
    it = root.rglob("*") if recursive else root.glob("*")
    return sorted(p for p in it if p.is_file() and p.suffix.lower() in ARCHIVE_EXTS
                  and not p.name.endswith(".meta.json"))


def load_passwords(pwfile: str | None) -> list[str]:
    if not pwfile:
        return []
    lines = Path(pwfile).read_text(encoding="utf-8").splitlines()
    return [ln for ln in (ln.strip() for ln in lines) if ln]


# ---------- run checklist（断点续跑账本） ----------

def _atomic_write_json(path: Path, obj: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def new_run_dir() -> Path:
    """创建 runs/<run_id>/ 目录，run_id 冲突时追加序号。"""
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = RUNS_DIR / run_id
    n = 2
    while run_dir.exists():
        run_dir = RUNS_DIR / f"{run_id}-{n}"
        n += 1
    run_dir.mkdir(parents=True)
    return run_dir


def _save_checklist(run_dir: Path, cl: dict) -> None:
    cl["updated_at"] = datetime.now().astimezone().isoformat()
    _atomic_write_json(run_dir / "checklist.json", cl)


def _load_checklist(run_dir: Path) -> dict:
    # utf-8-sig：兼容外部工具（如 PowerShell）写入的 BOM
    return json.loads((run_dir / "checklist.json").read_text(encoding="utf-8-sig"))


def _pick_resume_run(explicit: str) -> Path | None:
    """--resume [RUN_ID]：给了 id 用 id；没给则取最近一个未完成 run。"""
    if explicit:
        d = RUNS_DIR / explicit
        return d if (d / "checklist.json").exists() else None
    if not RUNS_DIR.exists():
        return None
    for d in sorted(RUNS_DIR.iterdir(), reverse=True):
        cl_path = d / "checklist.json"
        if cl_path.exists():
            try:
                if _load_checklist(d).get("status") != "done":
                    return d
            except (json.JSONDecodeError, OSError):
                continue
    return None


def scrape_one(archive: Path, depth: str, passwords: list[str],
               force: bool, ai_cfg: dict,
               run_dir: Path | None = None, on_phase=None) -> dict:
    """刮削单个包，返回运行记录（含 outcome）。

    run_dir 非空时：AI 留痕落 run_dir/ai_logs/；提取完成的中间 JSON
    落 run_dir/extracted_<hash8>.json，供 resume 时跳过重复解压。
    on_phase(phase, detail)：阶段推进回调，编排层用于记账 checklist。
    phase ∈ cached / extracted / written / failed。
    """
    rec = {"path": str(archive), "outcome": "", "detail": ""}

    def mark(phase: str, detail: str = "") -> None:
        if on_phase:
            on_phase(phase, detail)

    try:
        sha = extractor.sha256_file(archive)
        if not sidecar.needs_scrape(archive, sha, force=force):
            rec["outcome"] = "skip"
            # 顺带做改名校验，若有修正则写回
            doc = sidecar.load(archive)
            doc, changed = sidecar.reconcile_path(archive, doc)
            if changed:
                sidecar.write(archive, doc)
                rec["outcome"] = "skip+pathfix"
            mark("cached", rec["outcome"])
            return rec

        # resume 优化：本 run 已提取过的包直接加载中间 JSON，不重复解压
        ex_cache = run_dir / f"extracted_{sha[:8]}.json" if run_dir else None
        if ex_cache and ex_cache.exists():
            try:
                ex = json.loads(ex_cache.read_text(encoding="utf-8"))
                mark("extracted", "resume: 复用已提取中间结果")
            except (json.JSONDecodeError, OSError):
                ex = None
        else:
            ex = None
        if ex is None:
            ex = extractor.extract(str(archive), depth=depth, passwords=passwords)
            if ex_cache:
                try:
                    _atomic_write_json(ex_cache, ex)
                except OSError:
                    pass
            mark("extracted", f"entries={ex['structure'].get('entry_count')}")
        identity, confidence, ai_warnings = identify(
            ex, cfg=ai_cfg, log_dir=run_dir / "ai_logs" if run_dir else None,
            label=archive.name)
        engine = f"{ai_cfg.get('provider', 'custom')}:{ai_cfg.get('model') or 'auto'}"

        doc = {
            "schema_version": SCHEMA_VERSION,
            "anchoring": {
                "sha256": ex["sha256"],
                "source_path": str(archive.resolve()),
                "file_size": ex["file_size"],
                "mtime": ex["mtime"],
            },
            "scrape": {
                "scraped_at": datetime.now().astimezone().isoformat(),
                "engine": engine,
                "depth": ex["depth"],
                "confidence": confidence,
            },
            "identity": {k: v for k, v in identity.items()
                         if k not in ("confidence",) and not k.startswith("_")},
            "structure": ex["structure"],
            "sample_evidence": ex["sample_evidence"],
            "flags": ex["flags"],
            "warnings": list(ex["security_log"]) + ai_warnings,
        }
        if "_fallback_reason" in identity:
            doc["warnings"].append(identity["_fallback_reason"])
        if ex["password_found"]:
            doc["warnings"].append("密码轮询命中（密码本身不落盘）")

        sidecar.write(archive, doc)
        rec["outcome"] = "ok"
        rec["detail"] = f"confidence={confidence}"
        mark("written", rec["detail"])
        return rec
    except Exception as e:
        rec["outcome"] = "fail"
        rec["detail"] = f"{type(e).__name__}: {e}"
        mark("failed", rec["detail"])
        return rec


def cmd_scan(args) -> int:
    depth = args.depth
    passwords = load_passwords(args.pwfile)
    ai_cfg = load_config(args.config)
    target = Path(args.path)

    # run 目录与账本：新建或续跑
    if args.resume is None:
        run_dir = new_run_dir()
        if target.is_file():
            archives = [target]
        else:
            archives = find_archives(target, recursive=not args.no_recurse)
        if not archives:
            print("未找到压缩包")
            run_dir.rmdir()
            return 0
        cl = {
            "run_id": run_dir.name,
            "started_at": datetime.now().astimezone().isoformat(),
            "status": "running",
            "depth": depth,
            "packages": {str(a): {"phase": "pending", "detail": ""} for a in archives},
        }
        _save_checklist(run_dir, cl)
        print(f"run: {run_dir}")
    else:
        run_dir = _pick_resume_run(args.resume)
        if run_dir is None:
            print(f"无可续跑的 run（--resume {args.resume or '(自动)'}）："
                  f"{RUNS_DIR}/ 下不存在对应账本")
            return 1
        cl = _load_checklist(run_dir)
        cl["status"] = "running"
        _save_checklist(run_dir, cl)
        archives = [Path(p) for p, r in cl["packages"].items()
                    if r["phase"] in REDO_PHASES]
        missing = [p for p in cl["packages"] if not Path(p).exists()
                   and cl["packages"][p]["phase"] in REDO_PHASES]
        print(f"resume run: {run_dir}（待重做 {len(archives)} / 共 {len(cl['packages'])}）")
        if missing:
            print("警告：以下待重做包已不存在于磁盘:")
            for m in missing:
                print(f"  {m}")

    counters = {"ok": 0, "skip": 0, "skip+pathfix": 0, "fail": 0}
    low_conf: list[str] = []

    def on_phase(phase: str, detail: str, pkg: str) -> None:
        cl["packages"].setdefault(pkg, {"phase": phase, "detail": ""})
        cl["packages"][pkg].update(phase=phase, detail=detail)
        _save_checklist(run_dir, cl)

    try:
        for i, a in enumerate(archives, 1):
            rec = scrape_one(a, depth, passwords, args.force, ai_cfg,
                             run_dir=run_dir,
                             on_phase=lambda ph, dt, _p=str(a): on_phase(ph, dt, _p))
            counters[rec["outcome"]] = counters.get(rec["outcome"], 0) + 1
            mark_ch = {"ok": "+", "skip": "=", "skip+pathfix": "~", "fail": "!"}[rec["outcome"]]
            print(f"[{i}/{len(archives)}] {mark_ch} {a.name}  {rec['detail']}")
            if rec["outcome"] == "ok":
                doc = sidecar.load(a)
                if doc and doc["scrape"]["confidence"] < LOW_CONFIDENCE:
                    low_conf.append(f"{a} ({doc['scrape']['confidence']:.2f})")
    except KeyboardInterrupt:
        cl["status"] = "interrupted"
        _save_checklist(run_dir, cl)
        print(f"\n中断：进度已记入 {run_dir / 'checklist.json'}，"
              f"用 --resume 续跑")
        return 130

    cl["status"] = "done"
    _save_checklist(run_dir, cl)
    print(f"\n共 {len(archives)} 个包 | 新刮 {counters['ok']} | "
          f"缓存跳过 {counters['skip'] + counters['skip+pathfix']} | "
          f"失败 {counters['fail']}")
    print(f"run 记录: {run_dir}")
    if low_conf:
        print("低置信（建议人工复核或用 --force 重刮）:")
        for s in low_conf:
            print(f"  {s}")
    return 0


def cmd_show(args) -> int:
    target = Path(args.path)
    docs: list[tuple[Path, dict]] = []
    if target.is_file():
        if target.name.endswith(".meta.json"):
            docs.append((target, json.loads(target.read_text(encoding="utf-8"))))
        else:
            d = sidecar.load(target)
            if d is None:
                print(f"无侧车: {target}")
                return 1
            docs.append((sidecar.sidecar_path(target), d))
    else:
        for sp in sorted(target.glob("*.meta.json")):
            docs.append((sp, json.loads(sp.read_text(encoding="utf-8"))))
    for sp, doc in docs:
        print(f"== {sp.name} ==")
        zh = lambda k: SHOW_ZH.get(k, k)
        a, s, i = doc["anchoring"], doc["scrape"], doc["identity"]
        print(f"  {zh('title')}: {i['title'] or '(未识别)'}")
        print(f"  {zh('category')}: {i['category']}   {zh('confidence')}: {s['confidence']:.2f}   "
              f"{zh('depth')}: {s['depth']}")
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
            print(f"  警告:")
            for w in doc["warnings"][:5]:
                print(f"    - {w}")
        print()
    return 0


def cmd_check(args) -> int:
    problems = 0
    archives = find_archives(args.path)
    orphans = sidecar.find_orphans(args.path)
    if orphans:
        problems += len(orphans)
        print(f"孤儿侧车（无对应压缩包）: {len(orphans)}")
        for o in orphans:
            print(f"  {o}")
    mismatch = 0
    low_conf = 0
    for a in archives:
        doc = sidecar.load(a)
        if doc is None:
            print(f"未刮削: {a}")
            problems += 1
            continue
        errs = validate(doc)
        if errs:
            print(f"侧车非法: {a}: {errs}")
            problems += 1
            continue
        if doc["anchoring"]["sha256"] != extractor.sha256_file(a):
            mismatch += 1
            print(f"哈希不匹配（文件已变化）: {a}")
        if doc["scrape"]["confidence"] < LOW_CONFIDENCE:
            low_conf += 1
    if mismatch:
        problems += mismatch
    print(f"\n体检完成: {len(archives)} 个包, 孤儿 {len(orphans)}, "
          f"哈希不匹配 {mismatch}, 低置信 {low_conf}")
    return 1 if problems else 0


def cmd_export(args) -> int:
    out_path = Path(args.output)
    count = 0
    with out_path.open("w", encoding="utf-8") as out:
        for a in find_archives(args.path):
            doc = sidecar.load(a)
            if doc is None:
                continue
            slim = {
                "file": a.name,
                "path": str(a),
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
    ap = argparse.ArgumentParser(prog="scraper", description="压缩包 AI 刮削识别")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("scan", help="批量刮削")
    p.add_argument("path", help="文件或目录")
    p.add_argument("--depth", choices=["listing", "listing+sample"],
                   default="listing+sample")
    p.add_argument("--pwfile", help="密码文件（一行一个）")
    p.add_argument("--force", action="store_true", help="忽略缓存强制重刮")
    p.add_argument("--config", help="AI 配置文件路径（默认 scraper.json）")
    p.add_argument("--no-recurse", action="store_true", help="不递归子目录")
    p.add_argument("--resume", nargs="?", const="", default=None, metavar="RUN_ID",
                   help="续跑：不带值自动选最近未完成 run；带值用指定 run")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("show", help="中文映射展示侧车")
    p.add_argument("path")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("check", help="体检：孤儿/哈希不匹配/低置信")
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
