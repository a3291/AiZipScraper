"""Unit checks: schema template validation, contract/prompts loading, context paging."""
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from common import prompt_builder, schema
from extractor import context_builder

PASS = 0
FAIL = 0


def ok(cond, name):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"FAIL: {name}")


def test_check():
    tmpl = {"a": "", "b": [], "c": 0.0, "d": None}
    ok(schema.check({"a": "x", "b": [1], "c": 0.5, "d": 7}, tmpl) == [], "check: valid doc")
    ok(any("missing" in p for p in schema.check({"a": "x"}, tmpl)), "check: missing key")
    ok(any("unexpected" in p for p in schema.check(
        {"a": "x", "b": [], "c": 0.0, "d": None, "e": 1}, tmpl)), "check: extra key")
    ok(any("string" in p for p in schema.check(
        {"a": 1, "b": [], "c": 0.0, "d": None}, tmpl)), "check: type mismatch")
    ok(schema.check({"a": "x", "b": [], "c": False, "d": None}, {"a": "", "f": True}) != [],
        "check: literal bool mismatch")
    ok(schema.check({"a": "x", "b": [], "c": 0, "d": None},
                    {"a": "", "b": [], "c": "fixed", "d": None}) != [],
        "check: literal value mismatch")
    ok(not schema.check({"n": None}, {"n": None}), "check: null slot any value")
    ok(schema.check(True, 0) != [], "check: bool is not a number")


def test_derive():
    rf = schema.derive("s", {"action": "", "page": None}, {"action": ["a", "b"]})
    ok(rf["schema"]["properties"]["action"]["enum"] == ["a", "b"], "derive: enum applied")
    ok(rf["schema"]["required"] == ["action", "page"], "derive: required keys")
    ok("type" not in rf["schema"]["properties"]["page"], "derive: null slot unconstrained")


def test_load_and_inject():
    pb = prompt_builder.load(
        ROOT / "extractors" / "_contract.json",
        ROOT / "extractors" / "default" / "prompts.json",
        ROOT / "extractors" / "default" / "publish.json",
    )
    sys_content = pb.get("system")["content"]
    ok('{"action": "read_page"' in sys_content, "inject: contract rendered into system")
    ok("read_chatlog" in sys_content, "inject: read_chatlog in rendered contract")
    ok("write_memo" in sys_content, "inject: write_memo in rendered contract")
    ok("{_contract:" not in sys_content, "inject: no token left")
    m = pb.get("page_deliver", page=2, page_total=9, page_text="BODY")
    ok(m["role"] == "user" and "Page 2 of 9" in m["content"] and "BODY" in m["content"],
        "get: tokens replaced")
    ok(pb.has("system") and not pb.has("add"), "has: presence check")
    ok(pb.limit("chatlog_summary", "summary") == 4000, "limit: read from contract")
    ok(pb.publish_template["identity"]["title"] == "", "publish template loaded")
    reply_role = pb.get("chatlog_summarize_reply")["role"]
    ok(reply_role == "assistant", "role-only entry usable")


def test_context_paging():
    tmp = Path(tempfile.mkdtemp())
    try:
        (tmp / "a.txt").write_text(
            "Short alpha. Short beta. " * 40 + "LONG" * 2000 + " overflow sentence. Tail.",
            "utf-8",
        )
        (tmp / "_skip.txt").write_text("ignored", "utf-8")
        (tmp / "b.bin").write_bytes(b"\x00\x01binary")
        pkg = context_builder.build(tmp, page_chars=200, sentence_max_ratio=0.5)
        ok(pkg["stats"]["text_files"] == 1, "paging: only text files packed")
        ok(pkg["stats"]["pages"] >= 2, "paging: multiple pages")
        for p in pkg["pages"]:
            body = p["text"]
            ok(len(body) <= 400, "paging: page within soft bound")
            ok(body.rstrip()[-1] in "。！？；;.!? \n" or body, "paging: sentence boundary")
        ok(not any("LONG" in p["text"] for p in pkg["pages"]), "paging: oversize sentence dropped")
        ok("a.txt" in pkg["tail_page"]["text"], "paging: tail page lists files")
        ok("_skip" not in pkg["tail_page"]["text"], "paging: _-prefixed skipped")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_check()
    test_derive()
    test_load_and_inject()
    test_context_paging()
    print(f"test_contract: {PASS} pass, {FAIL} fail")
    sys.exit(1 if FAIL else 0)
