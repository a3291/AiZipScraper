"""AiZipScraper command line: scan / backend.

scan is one pipeline: register targets (non-recursive) under a path, extract
them in child processes, build paged context, run one identify conversation per
target, then fill the publish template, check it against the template and write
<target>.publish.json next to the target; a complete copy of each published
document is kept under runs/<run_id>/publishes/. The registry is the single
source of target state (ok / error; an error skips the target's remaining
stages).
"""
import argparse
import copy
import multiprocessing
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import ai_identify
import backend
import context_builder
import paths
import prompt_builder
import registry
import run_extractor
import schema


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


def _publish(pb, target, identity):
    """Fill the publish template with the model's identity; publish is the
    pure content conclusion — no process notes from any domain."""
    tmpl = pb.publish_template
    doc = copy.deepcopy(tmpl)
    doc["identity"] = {k: identity.get(k) for k in tmpl["identity"]}
    problems = schema.check(doc, tmpl)
    if problems:
        return None, doc, problems
    side = Path(str(target) + ".publish.json")
    paths.write_json(side, doc)
    return side, doc, []


def _extract_one(name, target, entry, run_dir, timeout_s):
    """Run extract() in a child process; judge normality from its return.

    Returns None when normal, else the reason (exception, timeout, worker
    death, non-dict return, zero kept files). The full extractor result is
    logged to run.json either way; nothing from it reaches the publish.
    """
    out_dir = Path(run_dir) / entry["out_dir"]
    q = multiprocessing.Queue()
    start = time.monotonic()
    proc = multiprocessing.Process(
        target=run_extractor.run, args=(name, target, str(out_dir), q))
    proc.start()
    proc.join(timeout_s)
    elapsed = round(time.monotonic() - start, 2)
    result, err = None, None
    if proc.is_alive():
        proc.terminate()
        proc.join()
        err = f"timeout after {timeout_s}s"
    elif q.empty():
        err = f"extractor died (exit {proc.exitcode})"
    else:
        tag, payload = q.get()
        if tag == "err":
            err = payload
        elif not isinstance(payload, dict):
            err = f"extractor returned {type(payload).__name__}, expected dict"
        else:
            kept = payload.get("files_kept")
            if not isinstance(kept, int) or isinstance(kept, bool) or kept <= 0:
                warns = "; ".join(str(w) for w in payload.get("warnings", []))
                err = warns or f"extractor kept no files (files_kept={kept!r})"
            else:
                result = payload
    registry.log_extractor_run(run_dir, {
        "key": entry["key"],
        "name": name,
        "target": target,
        "ok": err is None,
        **({"error": err} if err else {"result": result}),
        "elapsed_s": elapsed,
        "at": paths.now(),
    })
    return err


def _identify_one(pb, cfg, model, target, entry, run_dir):
    ai = cfg["ai"]
    limits = cfg["limits"]
    out_dir = Path(run_dir) / entry["out_dir"]
    pkg = context_builder.build(
        out_dir, ai["page_chars"], limits["sentence_max_ratio"],
        limits["sniff_bytes"],
    )
    paths.update_pkg(Path(run_dir) / "context.json", entry["key"], pkg)
    mlog = ai_identify.MessageLog(run_dir, entry["key"])
    identity, warns, stats = ai_identify.run_session(pkg, pb, cfg, model, mlog)
    for w in warns:
        print(f"  [identify] {entry['key']} note: {w}")
    side, doc, problems = _publish(pb, target, identity)
    if problems:
        registry.update(run_dir, target, state="error", error="; ".join(problems))
        print(f"  [publish] {entry['key']} FAILED template check: {'; '.join(problems)}")
        return
    pub_dir = Path(run_dir) / "publishes"
    pub_dir.mkdir(exist_ok=True)
    shutil.copy(side, pub_dir / side.name)
    registry.update(run_dir, target, state="ok")
    print(
        f"  [done] {entry['key']} {Path(target).name} -> {identity['title']} "
        f"({identity['category']}, {identity['confidence']}) "
        f"pages {stats['pages_read']}/{stats['pages']}, rolls {stats['rolls']}, turns {stats['turns']}"
    )


def cmd_scan(args):
    cfg = ai_identify.load_config(args.config)
    ai_cfg = cfg["ai"]
    if not backend.endpoint_available(ai_cfg, ai_cfg["probe_timeout"]):
        print(f"FAIL backend: {backend.endpoint(ai_cfg)} unreachable")
        return 1
    model = ai_cfg["model"]
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

    workers = args.workers or cfg["concurrency"]
    timeout_s = int(cfg["limits"]["extract_timeout_s"])

    def do_extract(item):
        path, entry = item
        err = _extract_one(args.extractor, path, entry, run_dir, timeout_s)
        if err is not None:
            registry.update(run_dir, path, state="error", error=err)
            print(f"  [extract] {entry['key']} error: {err}")
            return
        print(f"  [extract] {entry['key']} ok: {Path(path).name}")

    def do_identify(item):
        path, entry = item
        try:
            _identify_one(pb, cfg, model, path, entry, run_dir)
        except Exception as exc:
            registry.update(run_dir, path, state="error", error=repr(exc))
            print(f"  [identify] {entry['key']} FAILED: {exc!r}")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(do_extract, registry.by_state(run_dir, "")))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(do_identify, registry.by_state(run_dir, "")))

    registry.set_status(run_dir, "done")
    counts = {}
    for _, e in registry.all_targets(run_dir):
        counts[e["state"]] = counts.get(e["state"], 0) + 1
    print("summary: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return 0


def cmd_backend(args):
    cfg = ai_identify.load_config(args.config)
    ai = cfg["ai"]
    probe = ai["probe_timeout"]
    if not backend.endpoint_available(ai, probe):
        print(f"FAIL: {backend.endpoint(ai)} unreachable")
        return 1
    model = ai["model"]
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
    r = backend.chat([{"role": "user", "content": 'Reply with {"ok": true}'}], contract, model, ai)
    print(f"OK: {backend.endpoint(ai)} model={model} reply={r['content'][:80]!r} in {r['seconds']}s")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="aizipscraper",
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
