"""AiScraper command line: scan / backend.

scan is one pipeline: register targets (non-recursive) under a path, extract
them with subprocesses, build paged context, run one identify conversation per
target, then fill the publish template, check it against the template and write
<target>.publish.json next to the target. The registry is the single source of
target state.
"""
import argparse
import copy
import json
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import ai_identify
import backend
import context_builder
import paths
import prompt_builder
import registry
import schema

_CTX_LOCK = threading.Lock()


def _find_targets(path):
    root = Path(path)
    if not root.exists():
        raise SystemExit(f"path not found: {root}")
    if root.is_file():
        return [str(root.resolve())]
    targets = []
    for entry in sorted(root.iterdir()):
        name = entry.name
        if name.startswith(".") or name.startswith("_") or name.endswith(".publish.json"):
            continue
        if entry.resolve() == paths.RUNS_DIR:
            continue
        targets.append(str(entry.resolve()))
    return targets


def _publish(pb, target, identity, warnings):
    tmpl = pb.publish_template
    doc = copy.deepcopy(tmpl)
    doc["identity"] = {k: identity.get(k) for k in tmpl["identity"]}
    doc["warnings"] = [str(w) for w in warnings]
    problems = schema.check(doc, tmpl)
    if problems:
        return None, problems
    side = Path(str(target) + ".publish.json")
    paths.write_json(side, doc)
    return side, []


def _archive_pkg(run_dir, filename, key, pkg):
    with _CTX_LOCK:
        p = Path(run_dir) / filename
        doc = paths.read_json(p) if p.exists() else {"packages": {}}
        doc["packages"][key] = pkg
        paths.write_json(p, doc)


def _extract_one(name, target, entry, run_dir, timeout_s):
    out_dir = Path(run_dir) / entry["out_dir"]
    cmd = [sys.executable, str(paths.SCRIPTS / "run_extractor.py"), name, target, str(out_dir)]
    start = time.monotonic()
    try:
        proc = subprocess.run(cmd, capture_output=True, text="utf-8", timeout=timeout_s)
        exit_code, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        exit_code = 3
        stdout = (exc.stdout or b"").decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = f"timeout after {timeout_s}s"
    except Exception as exc:
        exit_code, stdout, stderr = 3, "", repr(exc)
    registry.log_extractor_run(run_dir, {
        "key": entry["key"],
        "path": target,
        "exit": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "elapsed_s": round(time.monotonic() - start, 2),
        "at": paths.now(),
    })
    result = None
    if exit_code == 0 and stdout.strip():
        try:
            result = json.loads(stdout)
        except ValueError:
            result = None
    if result is None:
        err = stderr.strip().splitlines()[-1] if stderr.strip() else f"exit {exit_code}"
        return None, err
    return result, None


def _identify_one(pb, cfg, model, target, entry, result, run_dir):
    ai = cfg["ai"]
    limits = cfg["limits"]
    out_dir = Path(run_dir) / entry["out_dir"]
    pkg = context_builder.build(
        out_dir, ai["page_chars"],
        limits["sentence_max_ratio"], limits["max_text_file_bytes"],
    )
    _archive_pkg(run_dir, "context.json", entry["key"], pkg)
    mlog = ai_identify.MessageLog(run_dir, entry["key"])
    identity, warns, stats = ai_identify.run_session(pkg, pb, cfg, model, mlog)
    extractor_warns = result.get("warnings", []) if isinstance(result, dict) else []
    merged = [str(w) for w in extractor_warns] + [str(w) for w in warns]
    side, problems = _publish(pb, target, identity, merged)
    if problems:
        registry.update(run_dir, target, state="failed", error="; ".join(problems))
        print(f"  [publish] {entry['key']} FAILED template check: {'; '.join(problems)}")
        return
    registry.update(run_dir, target, state="published")
    title = identity.get("title") or "(no title)"
    print(
        f"  [done] {entry['key']} {Path(target).name} -> {title} "
        f"({identity.get('category')}, {identity.get('confidence')}) "
        f"pages {stats['pages_read']}/{stats['pages']}, rolls {stats['rolls']}, turns {stats['turns']}"
    )


def cmd_scan(args):
    cfg = ai_identify.load_config(args.config)
    try:
        model = backend.resolve_model(cfg)
    except Exception as exc:
        print(f"FAIL backend: {exc}")
        return 1
    ex_dir = paths.EXTRACTORS / args.extractor
    pb = prompt_builder.load(
        paths.EXTRACTORS / "_contract.json",
        ex_dir / "prompts.json",
        ex_dir / "publish.json",
    )
    targets = _find_targets(args.path)
    if not targets:
        print("no targets found")
        return 0
    run_id, run_dir = registry.new_run_dir(paths.RUNS_DIR)
    registry.create(run_dir, run_id)
    registry.register(run_dir, targets)
    print(f"run {run_id}: {len(targets)} targets, extractor={args.extractor}, model={model}")

    workers = args.workers or int(cfg.get("concurrency", 2))
    timeout_s = int(cfg["limits"]["extract_timeout_s"])

    def do_extract(item):
        path, entry = item
        result, err = _extract_one(args.extractor, path, entry, run_dir, timeout_s)
        if result is None:
            registry.update(run_dir, path, state="failed", error=err)
            print(f"  [extract] {entry['key']} FAILED: {err}")
        else:
            registry.update(run_dir, path, state="extracted")
            extract_results[path] = result if isinstance(result, dict) else {}
            print(f"  [extract] {entry['key']} ok: {Path(path).name}")

    def do_identify(item):
        path, entry = item
        try:
            _identify_one(pb, cfg, model, path, entry, extract_results.get(path, {}), run_dir)
        except Exception as exc:
            registry.update(run_dir, path, state="failed", error=repr(exc))
            print(f"  [identify] {entry['key']} FAILED: {exc!r}")

    extract_results = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(do_extract, registry.by_state(run_dir, "pending")))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(do_identify, registry.by_state(run_dir, "extracted")))

    registry.set_status(run_dir, "done")
    counts = {}
    for _, e in registry.all_targets(run_dir):
        counts[e["state"]] = counts.get(e["state"], 0) + 1
    print("summary: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return 0


def cmd_backend(args):
    cfg = ai_identify.load_config(args.config)
    if not backend.endpoint_available(cfg):
        print(f"FAIL: {backend.endpoint(cfg)} unreachable")
        return 1
    model = backend.resolve_model(cfg)
    contract = {
        "name": "ping",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
            "additionalProperties": False,
        },
    }
    r = backend.chat([{"role": "user", "content": 'Reply with {"ok": true}'}], contract, model, cfg)
    print(f"OK: {backend.endpoint(cfg)} model={model} reply={r['content'][:80]!r} in {r['seconds']}s")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="aiscraper",
        description="AI-driven content scraper for archives and files",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_scan = sub.add_parser("scan", help="register targets under a path, extract, identify, publish")
    p_scan.add_argument("path", help="folder (non-recursive) or single file to scrape")
    p_scan.add_argument("--extractor", default="default", help="extractor name under extractors/")
    p_scan.add_argument("--config", default=None, help="config.json path")
    p_scan.add_argument("--workers", type=int, default=None, help="parallel workers (default: config concurrency)")

    p_be = sub.add_parser("backend", help="check AI backend connectivity")
    p_be.add_argument("--config", default=None, help="config.json path")

    args = ap.parse_args(argv)
    if args.cmd == "scan":
        return cmd_scan(args)
    return cmd_backend(args)


if __name__ == "__main__":
    sys.exit(main())
