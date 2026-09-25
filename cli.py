"""cli.py — 刮削器命令行入口。

子命令：
  scan    批量刮削（默认 listing+sample 深度）
  show    中文映射展示侧车内容
  check   体检：孤儿侧车 / 哈希不匹配 / 低置信
  export  汇总导出 JSONL
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import extractor
import sidecar
from ai_identify import identify, LOW_CONFIDENCE, MODEL
from schema import SCHEMA_VERSION, validate

ARCHIVE_EXTS = {".zip", ".7z"}

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


def scrape_one(archive: Path, depth: str, passwords: list[str],
               force: bool, model: str) -> dict:
    """刮削单个包，返回运行记录（含 outcome）。"""
    rec = {"path": str(archive), "outcome": "", "detail": ""}
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
            return rec

        ex = extractor.extract(str(archive), depth=depth, passwords=passwords)
        identity, confidence, ai_warnings = identify(ex, model=model)

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
                "engine": model,
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
        return rec
    except Exception as e:
        rec["outcome"] = "fail"
        rec["detail"] = f"{type(e).__name__}: {e}"
        return rec


def cmd_scan(args) -> int:
    depth = args.depth
    passwords = load_passwords(args.pwfile)
    target = Path(args.path)
    if target.is_file():
        archives = [target]
    else:
        archives = find_archives(target, recursive=not args.no_recurse)
    if not archives:
        print("未找到压缩包")
        return 0

    counters = {"ok": 0, "skip": 0, "skip+pathfix": 0, "fail": 0}
    low_conf: list[str] = []
    for i, a in enumerate(archives, 1):
        rec = scrape_one(a, depth, passwords, args.force, args.model)
        counters[rec["outcome"]] = counters.get(rec["outcome"], 0) + 1
        mark = {"ok": "+", "skip": "=", "skip+pathfix": "~", "fail": "!"}[rec["outcome"]]
        print(f"[{i}/{len(archives)}] {mark} {a.name}  {rec['detail']}")
        if rec["outcome"] == "ok":
            doc = sidecar.load(a)
            if doc and doc["scrape"]["confidence"] < LOW_CONFIDENCE:
                low_conf.append(f"{a} ({doc['scrape']['confidence']:.2f})")

    print(f"\n共 {len(archives)} 个包 | 新刮 {counters['ok']} | "
          f"缓存跳过 {counters['skip'] + counters['skip+pathfix']} | "
          f"失败 {counters['fail']}")
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
    p.add_argument("--model", default=MODEL, help=f"Ollama 模型名（默认 {MODEL}）")
    p.add_argument("--no-recurse", action="store_true", help="不递归子目录")
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
