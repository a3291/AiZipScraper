"""End-to-end scan tests with a fake backend: extraction return-value
judging, conversation flow, publish sidecars, registry states."""
import json
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from common import backend, paths
import cli

FAILS = []


def ok(cond, label):
    print(("PASS " if cond else "FAIL ") + label)
    if not cond:
        FAILS.append(label)


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
    run_log = json.loads((run_dir / "run.json").read_text("utf-8"))
    msgs = ses = ctx = None
    for name, slot in (("messages.json", "m"), ("sessions.json", "s"), ("context.json", "c")):
        p = run_dir / name
        if p.exists():
            doc = json.loads(p.read_text("utf-8"))
            if slot == "m":
                msgs = doc
            elif slot == "s":
                ses = doc
            else:
                ctx = doc
    return reg, msgs, ses, run_log, ctx


def read_sidecar(target):
    return json.loads(Path(str(target) + ".publish.json").read_text("utf-8"))


def cleanup(runs_dir, targets):
    shutil.rmtree(runs_dir, ignore_errors=True)
    shutil.rmtree(targets, ignore_errors=True)


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
    ok(list(side["identity"].keys()) == list(IDENTITY.keys()),
        "e2e: identity key order from template")
    run_dir = next(runs_dir.iterdir())
    pub = json.loads((run_dir / "publish.json").read_text("utf-8"))
    ok(pub["packages"]["t1"]["identity"] == side["identity"],
        "e2e: publish copy in run dir")
    pkg = ctx["packages"]["t1"]
    ok(pkg["pages"][0]["text"].startswith("Catalog"), "e2e: catalog is page 1")
    ok("text files packed" in pkg["pages"][-1]["text"],
        "e2e: metadata is the last page")
    ok(any(m.get("prompt_key") == "chatlog" and "no history yet" in m["text"]
           for m in msgs["packages"]["t1"]), "e2e: chatlog opens without history")
    cleanup(runs_dir, targets)


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
    ok(entry["state"] == "published", "flow: published after stall")
    side = read_sidecar(targets / "pack.zip")
    ok(side["identity"]["confidence"] == 0.9, "flow: confidence intact")
    cleanup(runs_dir, targets)


def test_publish_retry():
    """publish_retries applies only once publish is forced."""
    read = json.dumps({"action": "read_page", "page": 1, "identity": None})
    bad = json.dumps({"action": "publish", "page": None, "identity": {"title": "x"}})
    good = json.dumps({"action": "publish", "page": None, "identity": IDENTITY})
    code, targets, runs_dir = run_scan(
        [read, bad, good], single=True, max_turns=0, force_publish_at=5)
    reg, msgs, ses, run_log, ctx = load_run(runs_dir)
    keys = [m.get("prompt_key") for m in msgs["packages"]["t1"]]
    ok("force_publish" in keys, "retry: forced publish active")
    ok(keys.count("publish_retry") == 1, "retry: one retry prompt")
    ok(reg["targets"][str(targets / "pack.zip")]["state"] == "published",
        "retry: published after fix")
    cleanup(runs_dir, targets)


def test_publish_unforced_bails():
    """A malformed publish before any force gives up at once."""
    bad = json.dumps({"action": "publish", "page": None, "identity": {"title": "x"}})
    code, targets, runs_dir = run_scan([bad])
    reg, msgs, ses, run_log, ctx = load_run(runs_dir)
    keys = [m.get("prompt_key") for m in msgs["packages"]["t1"]]
    ok("publish_retry" not in keys, "unforced bail: no retry prompt")
    side = read_sidecar(targets / "pack.zip")
    ok(side["identity"]["category"] == "unknown", "unforced bail: degraded identity")
    ok(any("gave up" in w for w in side["warnings"]), "unforced bail: reason in warnings")
    cleanup(runs_dir, targets)


def test_bad_json_retry():
    good = json.dumps({"action": "publish", "page": None, "identity": IDENTITY})
    code, targets, runs_dir = run_scan(["not json", good])
    reg, msgs, ses, run_log, ctx = load_run(runs_dir)
    keys = [m.get("prompt_key") for m in msgs["packages"]["t1"]]
    ok(keys.count("bad_json_retry") == 1, "bad json: retry prompt sent")
    ok(reg["targets"][str(targets / "pack.zip")]["state"] == "published",
        "bad json: published after fix")
    cleanup(runs_dir, targets)


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
    cleanup(runs_dir, targets)


def test_read_chatlog():
    cfg_path, _ = make_config(force_publish_at=5)
    tmp_t = make_targets(single=True)
    old_runs = paths.RUNS_DIR
    paths.RUNS_DIR = Path(tempfile.mkdtemp())
    try:
        fake_backend([
            json.dumps({"action": "read_page", "page": 1, "identity": None}),
            json.dumps({"action": "read_chatlog", "page": 1, "identity": None}),
            json.dumps({"action": "read_chatlog", "page": 1, "identity": None}),
            json.dumps({"action": "publish", "page": None, "identity": IDENTITY}),
        ])
        code = cli.main(["scan", str(tmp_t), "--config", str(cfg_path), "--workers", "1"])
        reg, msgs, ses, run_log, ctx = load_run(paths.RUNS_DIR)
        keys = [m.get("prompt_key") for m in msgs["packages"]["t1"]]
        ok(keys.count("chatlog_deliver") == 2, "chatlog: page rereadable")
        chatlog = json.loads(
            (next(paths.RUNS_DIR.iterdir()) / "chatlog.json").read_text("utf-8"))
        ok("t1" in chatlog["packages"], "chatlog: persisted under run")
        ok(reg["targets"][str(tmp_t / "pack.zip")]["state"] == "published",
            "chatlog: published")
    finally:
        paths.RUNS_DIR = old_runs
        shutil.rmtree(tmp_t, ignore_errors=True)
        shutil.rmtree(paths.RUNS_DIR, ignore_errors=True)


def test_memo():
    replies = [
        json.dumps({"action": "write_memo", "page": None,
                    "memo": "note: sample zip", "identity": None}),
        json.dumps({"action": "read_memo", "page": None, "memo": None, "identity": None}),
        json.dumps({"action": "publish", "page": None, "identity": IDENTITY}),
    ]
    code, targets, runs_dir = run_scan(replies, single=True)
    reg, msgs, ses, run_log, ctx = load_run(runs_dir)
    keys = [m.get("prompt_key") for m in msgs["packages"]["t1"]]
    ok("memo_saved" in keys, "memo: saved")
    ok("memo_deliver" in keys, "memo: delivered on read")
    memo = json.loads((next(runs_dir.iterdir()) / "memo.json").read_text("utf-8"))
    ok(memo["packages"]["t1"]["text"] == "note: sample zip", "memo: persisted text")
    ok(reg["targets"][str(targets / "pack.zip")]["state"] == "published", "memo: published")
    cleanup(runs_dir, targets)


def test_memo_reject():
    replies = [
        json.dumps({"action": "write_memo", "page": None,
                    "memo": "x" * 1000, "identity": None}),
        json.dumps({"action": "publish", "page": None, "identity": IDENTITY}),
    ]
    code, targets, runs_dir = run_scan(replies, single=True, page_chars=100)
    reg, msgs, ses, run_log, ctx = load_run(runs_dir)
    keys = [m.get("prompt_key") for m in msgs["packages"]["t1"]]
    ok(keys.count("memo_reject") == 1, "memo reject: oversized write refused")
    ok(reg["targets"][str(targets / "pack.zip")]["state"] == "published",
        "memo reject: still published")
    cleanup(runs_dir, targets)


def test_refused_target():
    tmp = Path(tempfile.mkdtemp())
    (tmp / "plain.txt").write_text("just text", "utf-8")
    old_runs = paths.RUNS_DIR
    paths.RUNS_DIR = Path(tempfile.mkdtemp())
    try:
        fake_backend([])
        code = cli.main(["scan", str(tmp), "--config", str(make_config()[0]), "--workers", "1"])
        reg, msgs, ses, run_log, ctx = load_run(paths.RUNS_DIR)
        entry = reg["targets"][str(tmp / "plain.txt")]
        ok(entry["state"] == "skipped", "refused: skipped state")
        ok("not a zip/7z archive" in entry["error"], "refused: reason recorded")
        ok(msgs is None, "refused: no conversation")
    finally:
        paths.RUNS_DIR = old_runs
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(paths.RUNS_DIR, ignore_errors=True)


def test_max_turns_zero_remind():
    cfg_path, _ = make_config(max_turns=0, remind_at=5)
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
        keys = [m.get("prompt_key") for m in msgs["packages"]["t1"]]
        ok("remind" in keys, "max_turns: remind fallback at cap")
        ok(reg["targets"][str(tmp_t / "pack.zip")]["state"] == "published",
            "max_turns: still published")
    finally:
        paths.RUNS_DIR = old_runs
        shutil.rmtree(tmp_t, ignore_errors=True)
        shutil.rmtree(paths.RUNS_DIR, ignore_errors=True)


if __name__ == "__main__":
    test_direct_publish()
    test_page_flow()
    test_publish_retry()
    test_publish_unforced_bails()
    test_bad_json_retry()
    test_forced_roll()
    test_help_action()
    test_read_chatlog()
    test_memo()
    test_memo_reject()
    test_refused_target()
    test_max_turns_zero_remind()
    print(f"test_scan: done, {len(FAILS)} fail")
    sys.exit(1 if FAILS else 0)
