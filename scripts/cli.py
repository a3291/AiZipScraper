"""AiZipScraper command line: scan / backend.

scan is one pipeline over N targets, but every target gets its own run_id
and its own closed flow: register the target in its run, extract, build
paged context, converse, publish, close the run. An extractor error or an
AI error stops that run only — other targets are unaffected. The registry
of each run is the single source of its state (ok / error; an error skips
the run's remaining stages). The published document is copied into the
run's publish.json.
"""
import argparse
import copy
import multiprocessing
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


def _extract(name, target, run_dir, timeout_s):
    """Run extract() in a child process; judge normality from its return.

    Returns None when normal, else the reason (exception, timeout, worker
    death, non-dict return, zero kept files). The full extractor result is
    logged to run.json either way; nothing from it reaches the publish.
    """
    out_dir = Path(run_dir) / "extracted"
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
        "name": name,
        "target": target,
        "ok": err is None,
        **({"error": err} if err else {"result": result}),
        "elapsed_s": elapsed,
        "at": paths.now(),
    })
    return err


def _run_target(pb, cfg, model, extractor_name, target, timeout_s):
    """One target, one run, one closed flow: extract -> context -> converse
    -> publish. Any failure marks the run error and stops it."""
    run_id, run_dir = registry.new_run_dir(paths.RUNS_DIR)
    registry.create(run_dir, run_id, target)
    print(f"run {run_id}: {Path(target).name}")

    err = _extract(extractor_name, target, run_dir, timeout_s)
    if err is not None:
        registry.update(run_dir, state="error", error=err)
        registry.set_status(run_dir, "done")
        print(f"  [extract] error: {err}")
        return "error"

    ai = cfg["ai"]
    limits = cfg["limits"]
    pkg = context_builder.build(
        Path(run_dir) / "extracted", ai["page_chars"],
        limits["sentence_max_ratio"], limits["sniff_bytes"],
    )
    paths.write_json(Path(run_dir) / "context.json", pkg)
    mlog = ai_identify.MessageLog(run_dir)
    try:
        identity, warns, stats = ai_identify.run_session(pkg, pb, cfg, model, mlog)
    except Exception as exc:
        registry.update(run_dir, state="error", error=repr(exc))
        registry.set_status(run_dir, "done")
        print(f"  [identify] FAILED: {exc!r}")
        return "error"
    for w in warns:
        print(f"  [identify] note: {w}")

    side, doc, problems = _publish(pb, target, identity)
    if problems:
        registry.update(run_dir, state="error", error="; ".join(problems))
        registry.set_status(run_dir, "done")
        print(f"  [publish] FAILED template check: {'; '.join(problems)}")
        return "error"
    paths.write_json(Path(run_dir) / "publish.json", doc)
    registry.update(run_dir, state="ok")
    registry.set_status(run_dir, "done")
    print(
        f"  [done] -> {identity['title']} "
        f"({identity['category']}, {identity['confidence']}) "
        f"pages {stats['pages_read']}/{stats['pages']}, rolls {stats['rolls']}, turns {stats['turns']}"
    )
    return "ok"


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
    print(f"scan {len(targets)} targets, extractor={args.extractor}, model={model}")

    workers = args.workers or cfg["concurrency"]
    timeout_s = int(cfg["limits"]["extract_timeout_s"])

    states = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        states = list(pool.map(
            lambda t: _run_target(pb, cfg, model, args.extractor, t, timeout_s),
            targets))

    counts = {}
    for s in states:
        counts[s] = counts.get(s, 0) + 1
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
