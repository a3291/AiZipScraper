"""cli.py — command-line entry: scan / show / check / export.

scan writes runs/<run_id>/ archives and a <name>.publish.json sidecar next to
each target; show/check/export are read-only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import backend
import context_builder
import paths
import publisher
import run_logger
import schema
from ai_identify import MessageLog, load_config, load_prompts, run_session

ROOT = paths.ROOT
RUNS_DIR = paths.RUNS_DIR
PH_DONE, PH_FAIL, PH_SKIP = "done", "failed", "skipped"
CL_LOCK = threading.Lock()   # lock for concurrent checklist.json writes (shared by the api pool)
CTX_LOCK = threading.Lock()  # lock for concurrent context.json read-merge-writes


def _atomic_write_json(path: Path, obj: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat()


# ---------- scan ----------

def find_targets(root: str | Path, recursive: bool = True) -> list[Path]:
    """Every file is a target; runs/ and *.publish.json are excluded."""
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


# ---------- checklist ----------
def _new_pkg_rec(target: Path) -> dict:
    return {
        "name": target.name, "path": str(target),
        "extract": {"phase": "pending", "entry_id": "", "files_kept": 0, "dir": ""},
        "ai": {"phase": "pending", "published": False},
        "publish": {"phase": "pending", "sidecar": ""},
        "stats": {"pages": 0, "pages_read": 0, "messages": 0, "turns": 0,
                  "tokens_in": 0, "tokens_out": 0, "rolls": 0,
                  "sentences_skipped": 0, "elapsed_s": 0.0},
    }


# ---------- main flow ----------

def extract_one(target: Path, run_dir: Path,
                extractor_name: str, timeout_s: int) -> tuple[dict | None, str | None]:
    """Subprocess extraction of one target: pass the path, time-boxed, collect the result.

    Returns (result dict, None) or (None, error message);
    completion = exit code 0 + parseable _result.json.
    """
    entry_id = sha256_file(target)[:8]
    out_dir = run_dir / "extracted" / entry_id
    cmd = [sys.executable, str(paths.SCRIPTS / "run_extractor.py"),
           extractor_name, str(target), str(out_dir)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout_s, cwd=ROOT)
    except subprocess.TimeoutExpired:
        return None, f"extraction timed out (>{timeout_s}s)"
    if proc.returncode != 0:
        tail = (proc.stderr.strip().splitlines() or ["extractor failed"])[-1]
        return None, tail
    try:
        return json.loads((out_dir / "_result.json").read_text(
            encoding="utf-8-sig")), None
    except (OSError, json.JSONDecodeError) as e:
        return None, f"_result.json read failed: {e}"


def identify_and_publish(target: Path, run_dir: Path, cfg: dict, prompts: dict,
                         cl: dict, ex: dict,
                         extractor_name: str = "default") -> dict:
    """Identify and publish one target: pack → session → validate & write (ex is the extraction result dict)."""
    rec = {"name": target.name, "outcome": "", "detail": ""}
    entry_id = ex["entry_id"]
    pkg = cl["packages"].setdefault(str(target), _new_pkg_rec(target))
    pkg["extract"].update(phase=PH_DONE, entry_id=entry_id,
                          files_kept=ex["files_kept"], dir=f"extracted/{entry_id}")
    _save(cl, run_dir)

    # pack (reads extracted/ only)
    limits = cfg["limits"]
    try:
        ctx_pkg = context_builder.build(
            run_dir / "extracted" / entry_id,
            page_chars=cfg["page_chars"],
            max_text_file_bytes=limits["max_text_file_bytes"],
            sentence_max_ratio=limits["sentence_max_ratio"])
    except Exception as e:
        pkg["ai"].update(phase=PH_FAIL, detail=f"context: {e}")
        rec["outcome"] = "fail-context"
        rec["detail"] = f"{type(e).__name__}: {e}"
        return rec
    ctx_path = run_dir / "context.json"
    try:
        with CTX_LOCK:
            ctx_all = json.loads(ctx_path.read_text(encoding="utf-8-sig")) \
                if ctx_path.exists() else {"packages": {}}
            ctx_all["packages"][entry_id] = ctx_pkg
            _atomic_write_json(ctx_path, ctx_all)
    except OSError as e:
        rec["outcome"] = "fail-context"
        rec["detail"] = f"context write failed: {e}"
        return rec

    # session
    mlog = MessageLog(run_dir, entry_id)
    t0 = datetime.now().astimezone()
    try:
        identity, conf, warnings, stats = run_session(
            ctx_pkg, prompts, cfg, mlog, hint_exts=ex["structure"]["top_extensions"],
            depth="full" if ex["kind"] == "archive" else "direct")
    except Exception as e:
        pkg["ai"].update(phase=PH_FAIL, detail=f"{type(e).__name__}: {e}")
        rec["outcome"] = "fail-ai"
        rec["detail"] = f"{type(e).__name__}: {e}"
        return rec
    elapsed = (datetime.now().astimezone() - t0).total_seconds()
    warnings.extend(ex["warnings"])   # extractor's own warnings pass through unchanged
    published = stats["published"]
    pkg["ai"].update(phase=PH_DONE, published=published,
                     detail=identity.get(schema.FALLBACK_KEY, ""))
    pkg["stats"].update(pages=stats["pages"], pages_read=stats["pages_read"],
                        messages=mlog.count(), turns=stats["turns"],
                        tokens_in=stats["tokens_in"],
                        tokens_out=stats["tokens_out"], rolls=stats["rolls"],
                        sentences_skipped=ctx_pkg["stats"]["sentences_skipped"],
                        elapsed_s=round(elapsed, 1))
    _save(cl, run_dir)

    # validate & write
    st = ex["structure"]
    program = {
        "sha256": ex["sha256"],
        "source_path": str(target.resolve()),
        "file_size": target.stat().st_size,
        "mtime": datetime.fromtimestamp(target.stat().st_mtime) \
            .astimezone().isoformat(),
        "scraped_at": now_iso(),
        "engine": f"{cfg.get('base_url') or 'unknown'}:{cfg.get('model') or 'auto'}",
        "depth": "full" if ex["kind"] == "archive" else "direct",
        "structure": ex["structure"],
        "flags": {
            "password_protected": st["password_protected"],
            "multi_part": st["multi_part"],
            "nested_archives": st["nested_archives"],
            "exe_present": st["exe_present"],
            "macro_docs": st["macro_docs"],
        },
    }
    try:
        side, problems = publisher.publish(
            target, program, identity, warnings,
            template_path=paths.EXTRACTORS / extractor_name / "publish.json")
    except Exception as e:
        pkg["publish"].update(phase=PH_FAIL, detail=f"{type(e).__name__}: {e}")
        rec["outcome"] = "fail-publish"
        rec["detail"] = f"{type(e).__name__}: {e}"
        return rec
    if side is None:
        pkg["publish"].update(phase=PH_FAIL, detail=f"validation failed: {problems}")
        rec["outcome"] = "fail-publish"
        rec["detail"] = f"validation failed: {problems}"
        return rec
    pkg["publish"].update(phase=PH_DONE, sidecar=str(side))
    rec["outcome"] = "ok"
    rec["detail"] = f"confidence={conf} pages={stats['pages']}"
    return rec


def process_one(target: Path, run_dir: Path,
                cfg: dict, prompts: dict, cl: dict,
                extractor_name: str = "default") -> dict:
    """Full chain for one target (extract → identify → publish)."""
    ex, err = extract_one(target, run_dir, extractor_name,
                          cfg["limits"]["extract_timeout_s"])
    if err is not None:
        rec = {"name": target.name, "outcome": "fail-extract", "detail": err}
        pkg = cl["packages"].setdefault(str(target), _new_pkg_rec(target))
        pkg["extract"].update(phase=PH_FAIL, detail=err)
        return rec
    return identify_and_publish(target, run_dir, cfg, prompts, cl, ex,
                                extractor_name)


def _save(cl: dict, run_dir: Path) -> None:
    with CL_LOCK:
        cl["updated_at"] = now_iso()
        _atomic_write_json(run_dir / "checklist.json", cl)


def cmd_scan(args) -> int:
    cfg = load_config(args.config)
    ex_dir = paths.EXTRACTORS / args.extractor
    for name in ("prompt.json", "publish.json"):
        if not (ex_dir / name).is_file():
            raise SystemExit(f"extractor '{args.extractor}' is missing "
                             f"{name} ({ex_dir / name})")
    prompts = load_prompts(ex_dir / "prompt.json")
    if args.auto_chatlog:
        cfg["chatlog"] = True
    backend.resolve_endpoint(cfg)   # once per run, before the pools
    conc = cfg["concurrency"]
    if args.workers is not None:
        conc = args.workers
    targets = find_targets(args.path, recursive=not args.no_recurse)
    if not targets:
        print("no target files found")
        return 0

    run_dir = new_run_dir()
    cl = {
        "run_id": run_dir.name,
        "started_at": now_iso(),
        "status": "running",
        "concurrency": conc,
        "packages": {str(t): _new_pkg_rec(t) for t in targets},
    }
    _save(cl, run_dir)

    try:
        # cached targets are recorded skipped
        cached_set = {t for t in targets
                      if not args.force
                      and Path(str(t) + ".publish.json").exists()}
        for t in cached_set:
            pkg = cl["packages"].setdefault(str(t), _new_pkg_rec(t))
            for seg in ("extract", "ai", "publish"):
                pkg[seg]["phase"] = PH_SKIP
        _save(cl, run_dir)
        work = [t for t in targets if t not in cached_set]

        # extraction pool (per-target out dir)
        ex_map: dict[Path, tuple[dict | None, str | None]] = {}
        if work:
            timeout_s = cfg["limits"]["extract_timeout_s"]
            with ThreadPoolExecutor(max_workers=conc) as pool:
                futs = {pool.submit(extract_one, t, run_dir,
                                    args.extractor, timeout_s): t for t in work}
                for f in as_completed(futs):
                    ex_map[futs[f]] = f.result()
            for t, (ex, err) in ex_map.items():
                if err is not None:
                    pkg = cl["packages"].setdefault(str(t), _new_pkg_rec(t))
                    pkg["extract"].update(phase=PH_FAIL, detail=err)
            _save(cl, run_dir)

        # identification pool
        id_targets = [t for t in work
                      if not (t in ex_map and ex_map[t][0] is None)]
        if id_targets:
            with ThreadPoolExecutor(max_workers=conc) as pool:
                futs = {pool.submit(identify_and_publish, t, run_dir, cfg,
                                    prompts, cl, ex_map[t][0],
                                    args.extractor): t
                        for t in id_targets}
                for f in as_completed(futs):
                    t = futs[f]
                    try:
                        rec = f.result()
                    except Exception as e:
                        # worker crash: print and continue
                        print(f"identify worker crashed: {type(e).__name__}: {e}",
                              file=sys.stderr)
                        continue
                    # remove the extracted files of published targets
                    if args.auto_extracted_clean and rec["outcome"] == "ok":
                        entry_id = ex_map[t][0]["entry_id"]
                        shutil.rmtree(run_dir / "extracted" / entry_id,
                                      ignore_errors=True)
    except KeyboardInterrupt:
        cl["status"] = "interrupted"
        _save(cl, run_dir)
        print(f"interrupted: scene archived (run {run_dir.name})")
        print(run_logger.run_report(run_dir))
        return 130

    cl["status"] = "done"
    _save(cl, run_dir)
    print(run_logger.run_report(run_dir))
    return 0


# ---------- read-only commands (read .publish.json) ----------

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
                print(f"no sidecar: {target}.publish.json")
                return 1
            docs.append(got)
    else:
        for sp in sorted(target.glob("*.publish.json")):
            docs.append((sp, json.loads(sp.read_text(encoding="utf-8-sig"))))
    for sp, doc in docs:
        print(f"== {sp.name} ==")
        a, s, i = doc["anchoring"], doc["scrape"], doc["identity"]
        print(f"  title: {i['title'] or '(unidentified)'}")
        print(f"  category: {i['category']}   confidence: "
              f"{s['confidence']:.2f}   depth: {s['depth']}")
        print(f"  tags: {', '.join(i['tags'])}")
        print(f"  summary: {i['summary']}")
        print(f"  source_path: {a['source_path']}")
        print(f"  scraped_at: {s['scraped_at']}   engine: {s['engine']}")
        flags = doc["flags"]
        marks = [n for n, on in (("encrypted", flags["password_protected"]),
                                 ("has exe", flags["exe_present"]),
                                 ("macro docs", flags["macro_docs"]),
                                 ("multi-part", flags["multi_part"])) if on]
        if marks:
            print(f"  flags: {', '.join(marks)}")
        if doc["warnings"]:
            print("  warnings:")
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
            print(f"unscraped: {t}")
            problems += 1
            continue
        sp, doc = got
        errs = schema.validate(doc)
        if errs:
            print(f"invalid sidecar: {sp}: {errs}")
            problems += 1
            continue
        if doc["anchoring"]["sha256"] != sha256_file(t):
            print(f"hash mismatch (file changed): {t}")
            problems += 1
        if doc["scrape"]["confidence"] < schema.LOW_CONFIDENCE:
            print(f"low confidence: {t} ({doc['scrape']['confidence']:.2f})")
    orphans = [p for p in Path(args.path).glob("*.publish.json")
               if not Path(str(p)[:-len(".publish.json")]).exists()]
    for o in orphans:
        print(f"orphan sidecar (no target file): {o}")
        problems += 1
    print(f"\ncheck complete: {len(targets)} targets, {len(orphans)} orphans")
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
    print(f"exported {count} records → {out_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="scraper",
                                 description="archive/file AI scraping & recognition")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("scan", help="batch scrape (any files)")
    p.add_argument("path", help="file or directory")
    p.add_argument("--force", action="store_true",
                   help="rescan even if a sidecar already exists")
    p.add_argument("--config", help="AI config file path (default config.json in the project root)")
    p.add_argument("--extractor", default="default",
                   help="extractor name (directory or file under extractors/, default: default)")
    p.add_argument("--no-recurse", action="store_true",
                   help="do not recurse into subdirectories")
    p.add_argument("--auto-extracted-clean", action="store_true",
                   help="delete runs/<run_id>/extracted/<entry_id>/ after each target publishes successfully")
    p.add_argument("--auto-chatlog", action="store_true",
                   help="fold the session history into archived JSON summaries when it grows "
                        "(remind_at = summary trigger, force_publish_at = forced roll, "
                        "max_turns = roll cap)")
    p.add_argument("--workers", type=int,
                   help="override concurrency for this run (defaults to config.json)")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("show", help="display sidecars")
    p.add_argument("path")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("check",
                       help="health check: missing / hash mismatch / low confidence / orphans")
    p.add_argument("path")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("export", help="export digest as JSONL")
    p.add_argument("path")
    p.add_argument("-o", "--output", required=True)
    p.set_defaults(func=cmd_export)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
