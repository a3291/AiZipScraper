"""Contract/pagination tests: template validation, contract loading and
{_contract:xxx} injection, sentence-aligned pagination."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import context_builder
import prompt_builder
import schema

ROOT = Path(__file__).resolve().parent.parent
FAILS = []


def ok(cond, label):
    print(("PASS " if cond else "FAIL ") + label)
    if not cond:
        FAILS.append(label)


def test_template_check():
    tmpl = {"a": "", "b": [], "c": 0, "d": None, "e": "fixed", "f": {"g": ""}}
    ok(schema.check({"a": "x", "b": [1], "c": 2, "d": 1, "e": "fixed", "f": {"g": "y"}}, tmpl) == [],
        "check: full pass")
    ok(any("missing" in p for p in schema.check({}, tmpl)), "check: missing keys")
    ok(any("unexpected" in p for p in schema.check(
        {**tmpl, "z": 1}, tmpl)), "check: unexpected key")
    ok(any("expected string" in p for p in schema.check({"a": 1}, {"a": ""})),
        "check: string slot type")
    ok(any("expected number" in p for p in schema.check({"c": True}, {"c": 0})),
        "check: number slot rejects bool")
    ok(any("expected string" in p for p in schema.check({"a": None}, {"a": ""})),
        "check: null fails string slot")


def test_json_schema():
    s = schema.to_json_schema({"action": "", "page": None}, {"action": ["a", "b"]})
    ok(s["properties"]["action"]["type"] == "string", "derive: empty string -> string")
    ok(s["properties"]["action"]["enum"] == ["a", "b"], "derive: enum attached")
    ok(s["properties"]["page"] == {}, "derive: null slot -> any")
    ok(s["required"] == ["action", "page"], "derive: all keys required")
    ok(s["additionalProperties"] is False, "derive: closed object")


def test_load_and_inject():
    pb = prompt_builder.load(
        ROOT / "extractors" / "_contract.json",
        ROOT / "extractors" / "default" / "prompts.json",
        ROOT / "extractors" / "default" / "publish.json",
    )
    ok("contract session_actions" in pb.get("system")["content"], "inject: session_actions rendered")
    ok("contract chatlog_summary" in pb.get("chatlog_summarize")["content"],
        "inject: chatlog_summary rendered")
    ok('{"action": "read_page"' in pb.get("system")["content"], "inject: variants listed")
    sys_msg = pb.get("system")
    ok(sys_msg["role"] == "system", "get: role kept")
    first = pb.get("first", head="CAT")
    ok("CAT" in first["content"], "get: tokens replaced")
    cl = pb.get("chatlog", chatlog_total=0, chatlog_tail="(no history yet)")
    ok("(no history yet)" in cl["content"], "get: chatlog tail token replaced")
    page = pb.get("page_deliver", page=2, page_total=5, page_text="BODY")
    ok(page["content"].startswith("Page 2 of 5:"), "get: frontier first")
    ok("BODY" in page["content"], "get: payload present")
    rf = pb.response_format("session_actions")
    ok(set(rf["schema"]["properties"]) == {"action", "page", "memo", "identity"},
        "response_format: template keys")
    ok(rf["schema"]["properties"]["action"]["enum"][0] == "read_page",
        "response_format: action enum")


def test_publish_template():
    tmpl = {"identity": {"title": "", "category": "", "summary": "", "tags": [],
                         "language": [], "confidence": 0.0}, "warnings": []}
    doc = json.loads(json.dumps(tmpl))
    doc["identity"] = {"title": "T", "category": "media", "summary": "S",
                       "tags": ["a"], "language": ["en"], "confidence": 0.9}
    doc["warnings"] = ["w"]
    ok(schema.check(doc, tmpl) == [], "publish: filled doc passes")
    bad = json.loads(json.dumps(tmpl))
    bad["identity"]["confidence"] = "high"
    ok(any("confidence" in p for p in schema.check(bad, tmpl)), "publish: bad confidence caught")
    bad2 = json.loads(json.dumps(tmpl))
    del bad2["warnings"]
    ok(any("warnings" in p for p in schema.check(bad2, tmpl)), "publish: warnings required")


def test_paginate():
    pages = context_builder.paginate("Aaa. Bbb. Ccc. Ddd.", 8, 0.5)
    ok(all(len(p) <= 8 for p in pages) and "".join(pages).replace("\n", "") == "Aaa.Bbb.Ccc.Ddd.",
        "paginate: breaks at sentence ends, nothing lost")
    long_sent = "X" * 100 + ". Y."
    pages = context_builder.paginate(long_sent, 50, 0.1)
    ok(pages == ["Y."], "paginate: oversized sentence dropped whole")
    pages = context_builder.paginate("No terminal punctuation here", 100, 0.5)
    ok(pages == ["No terminal punctuation here"], "paginate: tail kept without terminator")


if __name__ == "__main__":
    test_template_check()
    test_json_schema()
    test_load_and_inject()
    test_publish_template()
    test_paginate()
    total = 18
    print(f"test_contract: {total - len(FAILS)} pass, {len(FAILS)} fail")
    sys.exit(1 if FAILS else 0)
