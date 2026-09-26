"""End-to-end scan tests with a fake backend: extraction return-value
judging, conversation flow, publish sidecars, per-run states.

One run carries exactly one target: a scan over a folder of N files
produces N runs; every run directory stores its documents directly
(context.json / messages.json / memo.json / chatlog.json / publish.json),
no grouping envelope."""
import json
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import backend
import cli
import paths

FAILS = []


def ok(cond, label):
    print(("PASS " if cond else "FAIL ") + label)
    if not cond:
        FAILS.append(label)


def make_config(**ai_over):
    tmp = Path(tempfile.mkdtemp())
    ai = {
        "base_url": "http://fake", "model": "fake", "api_key": "",
        "temperature": 0.2, "timeout": 30, "probe_timeout": 5, "page_chars": 500,
        "remind_at": 900000, "force_publish_at": 990000, "max_turns": -1,
        "estimate_chunk": 4, "publish_retries": 3,
    }
    ai.update(ai_over)
    cfg = {"concurrency": 1, "ai": ai, "limits": {
        "extract_timeout_s": 60,
        "sentence_max_ratio": 0.5,
        "sniff_bytes": 8192}}
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
            reply = json.dumps({"summary": "summary text"})
        else:
            reply = replies[calls["n"]] if calls["n"] < len(replies) else replies[-1]
        calls["n"] += 1
        return {"content": reply, "seconds": 0.0, "tokens_in": 0, "tokens_out": 0}

    backend.chat = chat
    backend.endpoint_available = lambda cfg, timeout=10: True
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


def run_dir_of(runs_dir, name="pack.zip"):
    """The run directory whose registry target ends with name."""
    for d in sorted(runs_dir.iterdir()):
        reg = json.loads((d / "registry.json").read_text("utf-8"))
        if reg["target"].endswith(name):
            return d
    raise AssertionError(f"no run for {name}")


def load_run(runs_dir, name="pack.zip"):
    """(registry, messages-or-None, run_dir) of the run for name; every
    document is stored directly in the run directory."""
    d = run_dir_of(runs_dir, name)
    reg = json.loads((d / "registry.json").read_text("utf-8"))
    p = d / "messages.json"
    msgs = json.loads(p.read_text("utf-8"))["messages"] if p.exists() else None
    return reg, msgs, d


def read_sidecar(target):
    return json.loads(Path(str(target) + ".publish.json").read_text("utf-8"))


def cleanup(runs_dir, targets):
    shutil.rmtree(runs_dir, ignore_errors=True)
    shutil.rmtree(targets, ignore_errors=True)


def test_direct_publish():
    pub = json.dumps({"action": "publish", "page": None, "identity": IDENTITY})
    code, targets, runs_dir = run_scan([pub])
    ok(code == 0, "e2e: exit 0")
    run_ids = sorted(d.name for d in runs_dir.iterdir())
    ok(len(run_ids) == 1, "e2e: one target -> one run")
    reg, msgs, d = load_run(runs_dir)
    ok(reg["state"] == "ok", "e2e: run ok")
    ok(reg["target"].endswith("pack.zip"), "e2e: registry carries the target")
    run_log = json.loads((d / "run.json").read_text("utf-8"))
    ok(len(run_log["runs"]) == 1, "e2e: run.json has one extractor entry")
    ok(run_log["runs"][0]["ok"], "e2e: extractor run normal")
    ctx = json.loads((d / "context.json").read_text("utf-8"))
    ok(ctx["pages"][0]["text"].startswith("Catalog"), "e2e: catalog is page 1")
    ok("text files packed" in ctx["pages"][-1]["text"],
        "e2e: metadata is the last page")
    side = read_sidecar(targets / "pack.zip")
    ok(list(side["identity"].keys()) == list(IDENTITY.keys()),
        "e2e: identity key order from template")
    pub = json.loads((d / "publish.json").read_text("utf-8"))
    ok(pub["identity"] == side["identity"], "e2e: publish copy in run dir")
    ok(any(m.get("prompt_key") == "chatlog" and "no history yet" in m["text"]
           for m in msgs), "e2e: chatlog opens without history")
    cleanup(runs_dir, targets)


def test_page_flow():
    replies = [
        json.dumps({"action": "read_page", "page": 1, "identity": None}),
        json.dumps({"action": "read_page", "page": 1, "identity": None}),
        json.dumps({"action": "publish", "page": None, "identity": IDENTITY}),
    ]
    code, targets, runs_dir = run_scan(replies, single=True)
    reg, msgs, d = load_run(runs_dir)
    keys = [m.get("prompt_key") for m in msgs]
    ok("page_deliver" in keys, "flow: page delivered")
    ok(keys.count("stall_to_publish") == 1, "flow: stall warned once")
    ok(reg["state"] == "ok", "flow: ok after stall")
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
    reg, msgs, d = load_run(runs_dir)
    keys = [m.get("prompt_key") for m in msgs]
    ok("force_publish" in keys, "retry: forced publish active")
    ok(keys.count("publish_retry") == 1, "retry: one retry prompt")
    ok(reg["state"] == "ok", "retry: ok after fix")
    cleanup(runs_dir, targets)


def test_publish_unforced_retry():
    """A malformed publish gets a remind with the missing fields, forced or
    not; the run recovers when the model fixes it."""
    bad = json.dumps({"action": "publish", "page": None, "identity": {"title": "x"}})
    good = json.dumps({"action": "publish", "page": None, "identity": IDENTITY})
    code, targets, runs_dir = run_scan([bad, good])
    reg, msgs, d = load_run(runs_dir)
    keys = [m.get("prompt_key") for m in msgs]
    ok(keys.count("publish_retry") == 1, "unforced retry: one remind sent")
    ok(reg["state"] == "ok", "unforced retry: ok after fix")
    ok((targets / "pack.zip.publish.json").exists(),
        "unforced retry: sidecar written")
    cleanup(runs_dir, targets)


def test_publish_retries_exhausted():
    """publish_retries reminds in total; the next malformed publish gives
    up: the run is an error and no sidecar is written."""
    bad = json.dumps({"action": "publish", "page": None, "identity": {"title": "x"}})
    code, targets, runs_dir = run_scan([bad], publish_retries=3)
    reg, msgs, d = load_run(runs_dir)
    keys = [m.get("prompt_key") for m in msgs]
    ok(keys.count("publish_retry") == 3, "exhausted: three reminds")
    ok(reg["state"] == "error", "exhausted: error state")
    ok("gave up" in reg["error"], "exhausted: reason in error")
    ok(not (targets / "pack.zip.publish.json").exists(),
        "exhausted: no sidecar")
    cleanup(runs_dir, targets)


def test_bad_json_retry():
    good = json.dumps({"action": "publish", "page": None, "identity": IDENTITY})
    code, targets, runs_dir = run_scan(["not json", good])
    reg, msgs, d = load_run(runs_dir)
    keys = [m.get("prompt_key") for m in msgs]
    ok(keys.count("bad_json_retry") == 1, "bad json: retry prompt sent")
    ok(reg["state"] == "ok", "bad json: ok after fix")
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
        reg, msgs, d = load_run(paths.RUNS_DIR)
        sess = {m["session"] for m in msgs}
        ok(len(sess) >= 2, "roll: new session opened")
        ok(reg["state"] == "ok", "roll: still ok")
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
    reg, msgs, d = load_run(runs_dir)
    keys = [m.get("prompt_key") for m in msgs]
    ok(keys.count("help") == 1, "help: recap sent once")
    ok(reg["state"] == "ok", "help: ok")
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
        reg, msgs, d = load_run(paths.RUNS_DIR)
        keys = [m.get("prompt_key") for m in msgs]
        ok(keys.count("chatlog_deliver") == 2, "chatlog: page rereadable")
        chatlog = json.loads((d / "chatlog.json").read_text("utf-8"))
        ok(chatlog["sections"], "chatlog: persisted under run")
        ok(reg["state"] == "ok", "chatlog: ok")
    finally:
        paths.RUNS_DIR = old_runs
        shutil.rmtree(tmp_t, ignore_errors=True)
        shutil.rmtree(paths.RUNS_DIR, ignore_errors=True)


def test_memo():
    replies = [
        json.dumps({"action": "write_memo", "page": None,
                    "memo": "note: sample zip", "identity": None}),
        json.dumps({"action": "read_memo", "page": 1, "memo": None, "identity": None}),
        json.dumps({"action": "publish", "page": None, "identity": IDENTITY}),
    ]
    code, targets, runs_dir = run_scan(replies, single=True)
    reg, msgs, d = load_run(runs_dir)
    keys = [m.get("prompt_key") for m in msgs]
    ok("memo_saved" in keys, "memo: saved")
    ok("memo_deliver" in keys, "memo: delivered on read")
    ok(any(m.get("prompt_key") == "memo_deliver" and m["text"].endswith("note: sample zip")
           for m in msgs), "memo: delivered page text")
    memo = json.loads((d / "memo.json").read_text("utf-8"))
    ok(memo["sections"] == ["note: sample zip"], "memo: appended section")
    ok(memo["pages"], "memo: paged document")
    ok(reg["state"] == "ok", "memo: ok")
    cleanup(runs_dir, targets)


def test_memo_reject():
    replies = [
        json.dumps({"action": "write_memo", "page": None,
                    "memo": 123, "identity": None}),
        json.dumps({"action": "publish", "page": None, "identity": IDENTITY}),
    ]
    code, targets, runs_dir = run_scan(replies, single=True)
    reg, msgs, d = load_run(runs_dir)
    keys = [m.get("prompt_key") for m in msgs]
    ok(keys.count("memo_reject") == 1, "memo reject: non-string write refused")
    ok(reg["state"] == "ok", "memo reject: still ok")
    cleanup(runs_dir, targets)


def test_refused_target():
    tmp = Path(tempfile.mkdtemp())
    (tmp / "plain.txt").write_text("just text", "utf-8")
    old_runs = paths.RUNS_DIR
    paths.RUNS_DIR = Path(tempfile.mkdtemp())
    try:
        fake_backend([])
        code = cli.main(["scan", str(tmp), "--config", str(make_config()[0]), "--workers", "1"])
        reg, msgs, d = load_run(paths.RUNS_DIR, "plain.txt")
        ok(reg["state"] == "error", "refused: error state")
        ok("not a zip/7z archive" in reg["error"], "refused: reason recorded")
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
        reg, msgs, d = load_run(paths.RUNS_DIR)
        keys = [m.get("prompt_key") for m in msgs]
        ok("remind" in keys, "max_turns: remind fallback at cap")
        ok(reg["state"] == "ok", "max_turns: still ok")
    finally:
        paths.RUNS_DIR = old_runs
        shutil.rmtree(tmp_t, ignore_errors=True)
        shutil.rmtree(paths.RUNS_DIR, ignore_errors=True)


def test_forced_help_once_then_refused():
    """At force the engine recaps the protocol itself once; afterwards a
    help call is refused with a remind that counts."""
    replies = [
        json.dumps({"action": "read_page", "page": 1, "identity": None}),
        json.dumps({"action": "help", "page": None, "identity": None}),
        json.dumps({"action": "publish", "page": None, "identity": IDENTITY}),
    ]
    code, targets, runs_dir = run_scan(
        replies, single=True, max_turns=0, force_publish_at=5)
    reg, msgs, d = load_run(runs_dir)
    keys = [m.get("prompt_key") for m in msgs]
    ok(keys.count("force_publish") == 1, "forced: force announced")
    ok(keys.count("help") == 1, "forced: engine recaps once, model help refused")
    ok(keys.count("publish_retry") == 1, "forced: refused help counts")
    ok(reg["state"] == "ok", "forced: ok after fix")
    cleanup(runs_dir, targets)


def test_forced_nonpublish_counts():
    """In force any non-publish return is reminded and counted."""
    replies = [
        json.dumps({"action": "read_page", "page": 1, "identity": None}),
        json.dumps({"action": "read_page", "page": 1, "identity": None}),
        json.dumps({"action": "publish", "page": None, "identity": IDENTITY}),
    ]
    code, targets, runs_dir = run_scan(
        replies, single=True, max_turns=0, force_publish_at=5)
    reg, msgs, d = load_run(runs_dir)
    keys = [m.get("prompt_key") for m in msgs]
    ok(keys.count("publish_retry") == 1, "forced: non-publish reminded")
    ok(reg["state"] == "ok", "forced: ok after publish")
    cleanup(runs_dir, targets)


def test_forced_nonpublish_exhausted():
    """The remind budget is shared with publish retries; exhausted, the
    run bails and no sidecar is written."""
    replies = [
        json.dumps({"action": "read_page", "page": 1, "identity": None}),
        json.dumps({"action": "read_page", "page": 1, "identity": None}),
        json.dumps({"action": "read_page", "page": 1, "identity": None}),
    ]
    code, targets, runs_dir = run_scan(
        replies, single=True, max_turns=0, force_publish_at=5, publish_retries=1)
    reg, msgs, d = load_run(runs_dir)
    keys = [m.get("prompt_key") for m in msgs]
    ok(keys.count("publish_retry") == 1, "exhausted: one remind for one retry")
    ok(reg["state"] == "error", "exhausted: run error")
    ok("gave up" in reg["error"], "exhausted: reason recorded")
    ok(not (targets / "pack.zip.publish.json").exists(),
        "exhausted: no sidecar")
    cleanup(runs_dir, targets)


def test_force_resets_countdown():
    """Unforced reminds consume the pre-force budget; entering the forced
    tail resets it: the tail always accommodates 1 + publish_retries
    rounds regardless of what was spent before."""
    bad = json.dumps({"action": "publish", "page": None, "identity": {"title": "x"}})
    good = json.dumps({"action": "publish", "page": None, "identity": IDENTITY})
    code, targets, runs_dir = run_scan(
        [bad, bad, bad, good], single=True, max_turns=0,
        force_publish_at=5, publish_retries=2)
    reg, msgs, d = load_run(runs_dir)
    keys = [m.get("prompt_key") for m in msgs]
    ok(keys.count("publish_retry") == 3, "reset: 1 unforced + 2 forced reminds")
    ok(keys.count("force_publish") == 1, "reset: force announced once")
    ok(reg["state"] == "ok", "reset: ok after fresh countdown")
    ok((targets / "pack.zip.publish.json").exists(), "reset: sidecar written")
    cleanup(runs_dir, targets)


if __name__ == "__main__":
    test_direct_publish()
    test_page_flow()
    test_publish_retry()
    test_publish_unforced_retry()
    test_publish_retries_exhausted()
    test_bad_json_retry()
    test_forced_roll()
    test_forced_help_once_then_refused()
    test_forced_nonpublish_counts()
    test_forced_nonpublish_exhausted()
    test_force_resets_countdown()
    test_help_action()
    test_read_chatlog()
    test_memo()
    test_memo_reject()
    test_refused_target()
    test_max_turns_zero_remind()
    print(f"test_scan: done, {len(FAILS)} fail")
    sys.exit(1 if FAILS else 0)
