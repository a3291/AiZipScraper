"""End-to-end checks with a fake backend: extract -> identify -> publish."""
import io
import json
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from common import backend, paths
import cli

PASS = 0
FAIL = 0


def ok(cond, name):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"FAIL: {name}")


def make_config(**ai_over):
    tmp = Path(tempfile.mkdtemp())
    ai = {
        "base_url": "http://fake", "model": "fake", "api_key": "",
        "temperature": 0.2, "timeout": 30, "page_chars": 500,
        "remind_at": 900000, "force_publish_at": 990000, "max_turns": -1,
        "estimate_chunk": 4, "publish_retries": 3,
    }
    ai.update(ai_over)
    cfg = {"concurrency": 1, "ai": ai, "limits": {
        "extract_timeout_s": 60,
        "sentence_max_ratio": 0.5}}
    p = tmp / "config.json"
    p.write_text(json.dumps(cfg), "utf-8")
    return p, tmp


def make_targets(single=False):
    """Default extractor only accepts archives: every scenario is driven by a
    zip target. single=True yields a directory holding just one zip."""
    tmp = Path(tempfile.mkdtemp())
    text = "Alpha sentence one. Beta sentence two. " * 20
    with zipfile.ZipFile(tmp / "pack.zip", "w") as zf:
        zf.writestr("inner/readme.txt", text)
        zf.writestr("inner/data.csv", "x,y\n1,2\n")
    return tmp


IDENTITY = {
    "title": "Sample Pack", "category": "documents",
    "summary": "A small sample pack.", "tags": ["sample"],
    "language": ["en"], "confidence": 0.9,
}


def fake_backend(replies):
    calls = {"n": 0}

    def chat(messages, contract, model, cfg):
        name = contract.get("name")
        if name == "chatlog_summary":
            reply = json.dumps({"summary": "digest text"})
        else:
            reply = replies[calls["n"]] if calls["n"] < len(replies) else replies[-1]
        calls["n"] += 1
        return {"content": reply, "seconds": 0.0, "tokens_in": 0, "tokens_out": 0}

    backend.chat = chat
    backend.endpoint_available = lambda cfg, timeout=10: True
    backend.resolve_model = lambda cfg, timeout=10: "fake-model"
    return calls


def run_scan(replies, single=False, **ai_over):
    cfg_path, _ = make_config(**ai_over)
    targets = make_targets(single)
    old_runs = paths.RUNS_DIR
    paths.RUNS_DIR = Path(tempfile.mkdtemp())
    try:
        fake_backend(replies)
        code = cli.main(["scan", str(targets), "--config", str(cfg_path), "--workers", "1"])
        return code, targets, paths.RUNS_DIR
    finally:
        paths.RUNS_DIR = old_runs


def load_run(runs_dir):
    run_dir = next(runs_dir.iterdir())
    reg = json.loads((run_dir / "registry.json").read_text("utf-8"))
    msgs = json.loads((run_dir / "messages.json").read_text("utf-8"))
    ses = json.loads((run_dir / "sessions.json").read_text("utf-8"))
    run_log = json.loads((run_dir / "run.json").read_text("utf-8"))
    ctx = json.loads((run_dir / "context.json").read_text("utf-8"))
    return reg, msgs, ses, run_log, ctx


def read_sidecar(target):
    return json.loads(Path(str(target) + ".publish.json").read_text("utf-8"))


def test_direct_publish():
    pub = json.dumps({"action": "publish", "page": None, "identity": IDENTITY})
    code, targets, runs_dir = run_scan([pub])
    ok(code == 0, "e2e: exit 0")
    reg, msgs, ses, run_log, ctx = load_run(runs_dir)
    ok(all(e["state"] == "published" for e in reg["targets"].values()),
        "e2e: all targets published")
    ok(len(run_log["runs"]) == 1, "e2e: run.json has one entry per extractor run")
    ok(all(r["ok"] for r in run_log["runs"]), "e2e: extractor runs normal")
    ok(set(ctx["packages"].keys()) == {"t1"}, "e2e: context per target")
    side = read_sidecar(targets / "pack.zip")
    ok(side["identity"]["title"] == "Sample Pack", "e2e: identity filled")
    ok(side["warnings"] == [], "e2e: warnings empty")
    ok(side["identity"]["category"] == "documents", "e2e: category filled")
    ok(set(side.keys()) == {"identity", "warnings"}, "e2e: no extra keys")
    ok(list(side["identity"].keys()) ==
       ["title", "category", "summary", "tags", "language", "confidence"],
       "e2e: identity key order from template")
    shutil.rmtree(runs_dir, ignore_errors=True)
    shutil.rmtree(targets, ignore_errors=True)


def test_page_flow():
    replies = [
        json.dumps({"action": "read_page", "page": 1, "identity": None}),
        json.dumps({"action": "read_page", "page": 1, "identity": None}),
        json.dumps({"action": "publish", "page": None, "identity": IDENTITY}),
    ]
    code, targets, runs_dir = run_scan(replies, single=True)
    reg, msgs, ses, run_log, ctx = load_run(runs_dir)
    keys = [m.get("prompt_key") for m in msgs["packages"]["t1"]]
    ok("page_deliver" in keys, "flow: page delivered")
    ok(keys.count("stall_to_publish") == 1, "flow: stall warned once")
    entry = reg["targets"][str(targets / "pack.zip")]
    ok(entry["state"] == "published", "flow: published after flow")
    side = read_sidecar(targets / "pack.zip")
    ok(side["identity"]["confidence"] == 0.9, "flow: confidence intact")
    shutil.rmtree(runs_dir, ignore_errors=True)
    shutil.rmtree(targets, ignore_errors=True)


def test_publish_retry():
    bad = json.dumps({"action": "publish", "page": None,
                      "identity": {"title": "x"}})
    good = json.dumps({"action": "publish", "page": None, "identity": IDENTITY})
    code, targets, runs_dir = run_scan(
        [json.dumps({"action": "read_page", "page": 1, "identity": None}),
         bad, good],
        single=True, max_turns=0, force_publish_at=5)
    reg, msgs, ses, run_log, ctx = load_run(runs_dir)
    keys = [m.get("prompt_key") for m in msgs["packages"]["t1"]]
    ok("force_publish" in keys, "retry: forced publish active")
    ok(keys.count("publish_retry") == 1, "retry: one retry prompt")
    ok(reg["targets"][str(targets / "pack.zip")]["state"] == "published",
        "retry: published after fix")
    shutil.rmtree(runs_dir, ignore_errors=True)
    shutil.rmtree(targets, ignore_errors=True)


def test_bad_json_retry():
    good = json.dumps({"action": "publish", "page": None, "identity": IDENTITY})
    code, targets, runs_dir = run_scan(["not json", good])
    reg, msgs, ses, run_log, ctx = load_run(runs_dir)
    keys = [m.get("prompt_key") for m in msgs["packages"]["t1"]]
    ok(keys.count("bad_json_retry") == 1, "bad json: retry prompt sent")
    ok(reg["targets"][str(targets / "pack.zip")]["state"] == "published",
        "bad json: published after fix")
    shutil.rmtree(runs_dir, ignore_errors=True)
    shutil.rmtree(targets, ignore_errors=True)


def test_forced_roll():
    cfg_path, _ = make_config(force_publish_at=5)
    tmp_t = make_targets(single=True)
    old_runs = paths.RUNS_DIR
    paths.RUNS_DIR = Path(tempfile.mkdtemp())
    try:
        fake_backend([
            json.dumps({"action": "read_page", "page": 1, "identity": None}),
            json.dumps({"action": "publish", "page": None, "identity": IDENTITY}),
        ])
        code = cli.main(["scan", str(tmp_t), "--config", str(cfg_path), "--workers", "1"])
        reg, msgs, ses, run_log, ctx = load_run(paths.RUNS_DIR)
        entry = ses["packages"]["t1"]
        ok(len(entry["sessions"]) >= 2, "roll: new session opened")
        ok(all(e["state"] == "published" for e in reg["targets"].values()),
            "roll: still published")
    finally:
        paths.RUNS_DIR = old_runs
        shutil.rmtree(tmp_t, ignore_errors=True)
        shutil.rmtree(paths.RUNS_DIR, ignore_errors=True)


def test_help_action():
    replies = [
        json.dumps({"action": "help", "page": None, "identity": None}),
        json.dumps({"action": "publish", "page": None, "identity": IDENTITY}),
    ]
    code, targets, runs_dir = run_scan(replies, single=True)
    reg, msgs, ses, run_log, ctx = load_run(runs_dir)
    keys = [m.get("prompt_key") for m in msgs["packages"]["t1"]]
    ok(keys.count("help") == 1, "help: recap sent once")
    ok(reg["targets"][str(targets / "pack.zip")]["state"] == "published", "help: published")
    shutil.rmtree(runs_dir, ignore_errors=True)
    shutil.rmtree(targets, ignore_errors=True)


def test_read_chatlog():
    cfg_path, _ = make_config(force_publish_at=5)
    tmp_t = make_targets(single=True)
    old_runs = paths.RUNS_DIR
    paths.RUNS_DIR = Path(tempfile.mkdtemp())
    try:
        fake_backend([
            json.dumps({"action": "read_page", "page": 1, "identity": None}),
            json.dumps({"action": "read_chatlog", "page": 1, "identity": None}),
            json.dumps({"action": "publish", "page": None, "identity": IDENTITY}),
        ])
        code = cli.main(["scan", str(tmp_t), "--config", str(cfg_path), "--workers", "1"])
        reg, msgs, ses, run_log, ctx = load_run(paths.RUNS_DIR)
        keys = [m.get("prompt_key") for m in msgs["packages"]["t1"]]
        ok("chatlog_deliver" in keys, "chatlog: page delivered")
        ok(len(ses["packages"]["t1"]["sessions"]) >= 2, "chatlog: fold opened session")
        ok(reg["targets"][str(tmp_t / "pack.zip")]["state"] == "published",
            "chatlog: published after reading chatlog")
    finally:
        paths.RUNS_DIR = old_runs
        shutil.rmtree(tmp_t, ignore_errors=True)
        shutil.rmtree(paths.RUNS_DIR, ignore_errors=True)


def test_memo():
    replies = [
        json.dumps({"action": "write_memo", "page": None,
                    "memo": "Found: dataset", "identity": None}),
        json.dumps({"action": "read_memo", "page": None, "memo": None,
                    "identity": None}),
        json.dumps({"action": "publish", "page": None, "identity": IDENTITY}),
    ]
    code, targets, runs_dir = run_scan(replies, single=True, force_publish_at=5)
    ok(code == 0, "memo: exit 0")
    run_dir = next(runs_dir.iterdir())
    reg = json.loads((run_dir / "registry.json").read_text("utf-8"))
    msgs = json.loads((run_dir / "messages.json").read_text("utf-8"))
    ok(reg["targets"][str(targets / "pack.zip")]["state"] == "published",
        "memo: published")
    memo_doc = json.loads((run_dir / "memo.json").read_text("utf-8"))
    ok(memo_doc["packages"]["t1"]["text"] == "Found: dataset",
        "memo: persisted in memo.json")
    keys = [m.get("prompt_key") for m in msgs["packages"]["t1"]]
    ok("memo_saved" in keys and "memo_deliver" in keys,
        "memo: saved and delivered in conversation")
    ok(keys.count("memo") >= 2, "memo: tail page present in every session")


def test_refused_target():
    """A plain file target is refused by the archive-only extractor: it fails
    in the registry and never reaches the conversation."""
    import tempfile
    tmp = Path(tempfile.mkdtemp())
    (tmp / "plain.txt").write_text("x", "utf-8")
    cfg_path, _ = make_config()
    old_runs = paths.RUNS_DIR
    paths.RUNS_DIR = Path(tempfile.mkdtemp())
    try:
        fake_backend([json.dumps({"action": "publish", "page": None, "identity": IDENTITY})])
        code = cli.main(["scan", str(tmp / "plain.txt"), "--config", str(cfg_path), "--workers", "1"])
        ok(code == 0, "refuse: exit 0")
        run_dir = next(paths.RUNS_DIR.iterdir())
        reg = json.loads((run_dir / "registry.json").read_text("utf-8"))
        msgs_path = run_dir / "messages.json"
        entry = reg["targets"][str(tmp / "plain.txt")]
        ok(entry["state"] == "skipped", "refuse: target skipped")
        ok("not a zip/7z archive" in entry.get("error", ""), "refuse: reason recorded")
        ok(not msgs_path.exists(), "refuse: no conversation opened")
    finally:
        paths.RUNS_DIR = old_runs
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(paths.RUNS_DIR, ignore_errors=True)


if __name__ == "__main__":
    test_direct_publish()
    test_page_flow()
    test_publish_retry()
    test_bad_json_retry()
    test_forced_roll()
    test_help_action()
    test_read_chatlog()
    test_memo()
    test_refused_target()
    print(f"test_scan: {PASS} pass, {FAIL} fail")
    sys.exit(1 if FAIL else 0)
