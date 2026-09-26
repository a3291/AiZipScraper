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

import backend
import cli
import paths

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
    }
    ai.update(ai_over)
    cfg = {"concurrency": 1, "ai": ai, "limits": {
        "extract_timeout_s": 60, "max_text_file_bytes": 1 << 20,
        "sentence_max_ratio": 0.5}}
    p = tmp / "config.json"
    p.write_text(json.dumps(cfg), "utf-8")
    return p, tmp


def make_targets(single=False):
    tmp = Path(tempfile.mkdtemp())
    text = "Alpha sentence one. Beta sentence two. " * 20
    if single:
        (tmp / "note.txt").write_text(text, "utf-8")
        return tmp
    with zipfile.ZipFile(tmp / "pack.zip", "w") as zf:
        zf.writestr("inner/readme.txt", text)
        zf.writestr("inner/data.csv", "x,y\n1,2\n")
    (tmp / "note.txt").write_text(text, "utf-8")
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
    ok(len(run_log["runs"]) == 2, "e2e: run.json has one entry per extractor run")
    ok(all(r["exit"] == 0 for r in run_log["runs"]), "e2e: extractor exits 0")
    ok(set(ctx["packages"].keys()) == {"t1", "t2"}, "e2e: context per target")
    zip_side = read_sidecar(targets / "pack.zip")
    ok(zip_side["identity"]["title"] == "Sample Pack", "e2e: identity filled")
    ok(zip_side["warnings"] == [], "e2e: warnings empty")
    txt_side = read_sidecar(targets / "note.txt")
    ok(txt_side["identity"]["category"] == "documents", "e2e: second sidecar")
    ok(set(zip_side.keys()) == {"identity", "warnings"}, "e2e: no extra keys")
    ok(list(zip_side["identity"].keys()) ==
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
    ok(keys.count("dup_page") == 1, "flow: duplicate page warned once")
    entry = reg["targets"][str(targets / "note.txt")]
    ok(entry["state"] == "published", "flow: published after flow")
    side = read_sidecar(targets / "note.txt")
    ok(side["identity"]["confidence"] == 0.9, "flow: confidence intact")
    shutil.rmtree(runs_dir, ignore_errors=True)
    shutil.rmtree(targets, ignore_errors=True)


def test_publish_retry():
    bad = json.dumps({"action": "publish", "page": None,
                      "identity": {"title": "x"}})
    good = json.dumps({"action": "publish", "page": None, "identity": IDENTITY})
    code, targets, runs_dir = run_scan([bad, good], single=True)
    reg, msgs, ses, run_log, ctx = load_run(runs_dir)
    keys = [m.get("prompt_key") for m in msgs["packages"]["t1"]]
    ok(keys.count("publish_retry") == 1, "retry: one retry prompt")
    ok(reg["targets"][str(targets / "note.txt")]["state"] == "published",
        "retry: published after fix")
    shutil.rmtree(runs_dir, ignore_errors=True)
    shutil.rmtree(targets, ignore_errors=True)


def test_bad_json_bail():
    code, targets, runs_dir = run_scan(["not json", "still not json"])
    reg, msgs, ses, run_log, ctx = load_run(runs_dir)
    side = read_sidecar(targets / "note.txt")
    ok(side["identity"]["category"] == "unknown", "bail: degraded identity")
    ok(any("gave up" in w for w in side["warnings"]), "bail: reason in warnings")
    ok(side["identity"]["title"] == "", "bail: empty title")
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
    ok(reg["targets"][str(targets / "note.txt")]["state"] == "published", "help: published")
    shutil.rmtree(runs_dir, ignore_errors=True)
    shutil.rmtree(targets, ignore_errors=True)


if __name__ == "__main__":
    test_direct_publish()
    test_page_flow()
    test_publish_retry()
    test_bad_json_bail()
    test_forced_roll()
    test_help_action()
    print(f"test_scan: {PASS} pass, {FAIL} fail")
    sys.exit(1 if FAIL else 0)
