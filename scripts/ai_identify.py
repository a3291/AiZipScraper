"""Conversation engine: paged evidence in, identity out.

chatlog is the only mode: remind_at asks the model for a summary and rolls the
history into the chatlog document; force_publish_at rolls without a summary
(the session's raw messages are filed instead — the soft boundary keeps the
messages); max_turns caps rolls (-1 unlimited; when the cap is reached the
watermarks fall back to their prompt-publish / force-publish meanings). Every
raw message is archived with its session number; a roll opens a new session.
"""
import json
from pathlib import Path

import backend
import context_builder
import schema
from paths import read_pkgs, update_pkg

REQUIRED_AI_KEYS = {
    "base_url", "model", "api_key", "temperature", "timeout", "probe_timeout",
    "page_chars", "remind_at", "force_publish_at", "max_turns",
    "estimate_chunk", "publish_retries",
}
REQUIRED_PROMPT_KEYS = {
    "system", "first", "chatlog", "chatlog_deliver", "help", "remind",
    "force_publish", "bad_json_retry", "publish_retry",
    "stall_to_publish", "page_deliver", "chatlog_summarize",
    "chatlog_summarize_reply", "memo", "memo_deliver", "memo_saved",
    "memo_reject",
}


def load_config(path=None):
    path = Path(path) if path else Path(__file__).resolve().parent.parent / "config.json"
    cfg = json.loads(path.read_text("utf-8"))
    ai = cfg.get("ai", {})
    missing = REQUIRED_AI_KEYS - set(ai)
    if missing:
        raise ValueError(f"config.json missing ai keys: {sorted(missing)}")
    if not isinstance(ai.get("base_url"), str) or not ai["base_url"].strip():
        raise ValueError("config.json ai.base_url must be a non-empty string")
    if not isinstance(cfg.get("limits"), dict):
        raise ValueError("config.json: limits section missing")
    return cfg


class MessageLog:
    """Appends every raw message (with its session number) to messages.json;
    session boundaries live in sessions.json."""

    def __init__(self, run_dir, key):
        self.run_dir = Path(run_dir)
        self.key = key
        self._entries = read_pkgs(self.run_dir / "messages.json")["packages"].get(key, [])
        self._sessions = (
            read_pkgs(self.run_dir / "sessions.json")["packages"]
            .get(key, {"sessions": []})["sessions"]
        )
        self.session = len(self._sessions)

    def new_session(self):
        self.session += 1
        self._sessions.append({"no": self.session})
        update_pkg(self.run_dir / "sessions.json", self.key, {"sessions": self._sessions})

    def append(self, role, text, prompt_key=None):
        entry = {"session": self.session, "role": role, "text": text}
        if prompt_key:
            entry["prompt_key"] = prompt_key
        self._entries.append(entry)
        update_pkg(self.run_dir / "messages.json", self.key, self._entries)


def _estimate_tokens(messages, chunk):
    return sum(len(m["content"]) for m in messages) // chunk


def _extract_json(text):
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except ValueError:
        return None


def run_session(context_pkg, pb, cfg, model, mlog):
    """Drive one identify conversation. Returns (identity, warnings, stats)."""
    missing = REQUIRED_PROMPT_KEYS - set(pb.prompts)
    if missing:
        raise ValueError(f"prompts.json missing keys: {sorted(missing)}")
    ai = cfg["ai"]
    pages = context_pkg["pages"]
    page_total = len(pages)
    rf = pb.response_format("session_actions")

    warned = []
    stats = {
        "pages": page_total, "pages_read": 0, "turns": 0, "rolls": 0,
        "tokens_in": 0, "tokens_out": 0,
    }
    read_pages = set()
    chatlog_pages = []
    usage_tokens = 0
    memo_limit = ai["page_chars"]

    def _load_memo(run_dir, key):
        return read_pkgs(Path(run_dir) / "memo.json")["packages"].get(key, {}).get("text", "")

    def _save_memo(run_dir, key, text):
        update_pkg(Path(run_dir) / "memo.json", key, {"text": text})

    memo_text = _load_memo(mlog.run_dir, mlog.key)

    def memo_msg():
        body = memo_text if memo_text else "(memo is empty)"
        return pb.get("memo", memo_text=body)

    rolls = 0
    max_turns = ai["max_turns"]
    forced = False
    reminded = False
    summary_tried = False
    retry_left = ai["publish_retries"]

    system = pb.get("system")
    first = pb.get("first", head=pages[0]["text"])
    add = pb.prompts.get("add") if "add" in pb.prompts else None

    def chatlog_msg():
        read = ", ".join(str(n) for n in sorted(read_pages)) or "none"
        if chatlog_pages:
            tail_text, total = chatlog_pages[-1], len(chatlog_pages)
        else:
            tail_text, total = "(no history yet)", 0
        return pb.get(
            "chatlog", roll_no=rolls, pages_read=read, page_total=page_total,
            chatlog_total=total, chatlog_tail=tail_text,
        )

    def rebuild():
        pairs = [("system", system), ("first", first)]
        if add:
            pairs.append(("add", add))
        pairs.append(("chatlog", chatlog_msg()))
        pairs.append(("memo", memo_msg()))
        return pairs

    def open_session():
        nonlocal messages
        pairs = rebuild()
        messages = [m for _, m in pairs]
        mlog.new_session()
        for key, m in pairs:
            mlog.append(m["role"], m["content"], key)

    messages = []
    open_session()

    def say(key, **tokens):
        msg = pb.get(key, **tokens)
        messages.append(msg)
        mlog.append(msg["role"], msg["content"], key)

    def roll(with_summary, summary_text=None):
        """Roll the current session into the chatlog document (built and
        persisted by context_builder as runs/<run_id>/chatlog.json), then
        reopen with a new session. With a summary the summary is filed;
        without one the raw session messages are filed so the model can still
        reach them through chatlog pages."""
        nonlocal rolls, usage_tokens, reminded, summary_tried
        nonlocal chatlog_pages
        rolls += 1
        stats["rolls"] = rolls
        if with_summary and summary_text:
            body = summary_text
            kind = "summary"
        else:
            body = "\n".join(f"{m['role']}: {m['content']}" for m in messages)
            kind = "raw"
        chatlog_pages = context_builder.write_chatlog(
            mlog.run_dir, mlog.key, f"===== {kind} {rolls} =====\n{body}",
            ai["page_chars"], cfg["limits"]["sentence_max_ratio"],
        )
        usage_tokens = 0
        reminded = False
        summary_tried = False
        open_session()

    def ask_summary():
        m = pb.get("chatlog_summarize")
        try:
            r = backend.chat(messages + [m], pb.response_format("chatlog_summary"), model, ai)
        except Exception as exc:
            warned.append(f"chatlog summary side call failed: {exc}")
            return None
        stats["tokens_in"] += r["tokens_in"]
        stats["tokens_out"] += r["tokens_out"]
        mlog.append(m["role"], m["content"], "chatlog_summarize")
        obj = _extract_json(r["content"])
        problems = schema.check(
            obj if isinstance(obj, dict) else {},
            pb.contracts["chatlog_summary"]["template"],
        )
        if problems:
            warned.append("chatlog summary failed contract: " + "; ".join(problems))
            return None
        text = obj.get("summary")
        if not isinstance(text, str) or not text.strip():
            warned.append("chatlog summary invalid or empty")
            return None
        mlog.append("assistant", r["content"], "chatlog_summarize_reply")
        return text.strip()

    def bail(reason):
        warned.append(f"gave up: {reason}")
        stats["pages_read"] = len(read_pages)
        identity = {
            "title": "", "category": "unknown", "summary": "",
            "tags": [], "language": [], "confidence": 0.0,
        }
        return identity, warned, stats

    def stall():
        nonlocal forced
        forced = True
        say("stall_to_publish")

    while True:
        can_roll = max_turns < 0 or rolls < max_turns
        if usage_tokens >= ai["force_publish_at"]:
            if can_roll:
                roll(with_summary=False)
                continue
            if not forced:
                forced = True
                say("force_publish")
        elif usage_tokens >= ai["remind_at"]:
            if can_roll and not summary_tried:
                summary_tried = True
                summary = ask_summary()
                if summary is not None:
                    roll(with_summary=True, summary_text=summary)
                    continue
            elif not can_roll and not reminded:
                reminded = True
                say("remind")

        stats["turns"] += 1
        r = backend.chat(messages, rf, model, ai)
        usage_tokens = r["tokens_in"] or _estimate_tokens(messages, ai["estimate_chunk"])
        stats["tokens_in"] += r["tokens_in"]
        stats["tokens_out"] += r["tokens_out"]
        reply = r["content"]
        mlog.append("assistant", reply)

        obj = _extract_json(reply)
        if obj is None or not isinstance(obj, dict):
            say("bad_json_retry")
            continue

        action = obj.get("action")
        if action == "help":
            say("help", page_total=page_total)
            continue

        if action == "publish":
            ident = obj.get("identity")
            tmpl = pb.publish_template["identity"]
            problems = schema.check(ident if isinstance(ident, dict) else {}, tmpl)
            if problems:
                if forced and retry_left > 0:
                    say(
                        "publish_retry",
                        missing="; ".join(p.split(".", 1)[-1] for p in problems),
                        attempt_left=retry_left,
                    )
                    retry_left -= 1
                    continue
                return bail("publish output malformed")
            stats["pages_read"] = len(read_pages)
            return ident, warned, stats

        if action == "read_memo":
            say("memo_deliver", memo_text=memo_text or "(memo is empty)")
            continue

        if action == "write_memo":
            incoming = obj.get("memo")
            if not isinstance(incoming, str):
                say("memo_reject", why="memo field must be a string")
                continue
            if len(incoming) > memo_limit:
                say("memo_reject", why=f"memo over {memo_limit} chars")
                continue
            memo_text = incoming
            _save_memo(mlog.run_dir, mlog.key, memo_text)
            say("memo_saved", chars=len(memo_text))
            continue

        if action == "read_chatlog":
            cpage = obj.get("page")
            ok_c = (
                isinstance(cpage, int) and not isinstance(cpage, bool)
                and 1 <= cpage <= len(chatlog_pages)
            )
            if not ok_c:
                stall()
                continue
            body = chatlog_pages[cpage - 1]
            say(
                "chatlog_deliver", page=cpage,
                chatlog_total=len(chatlog_pages), page_text=body,
            )
            continue

        page = obj.get("page")
        ok = (
            isinstance(page, int) and not isinstance(page, bool)
            and 1 <= page <= page_total
        )
        if not ok:
            stall()
            continue
        if page in read_pages:
            stall()
            continue
        read_pages.add(page)
        body = pages[page - 1]["text"]
        say("page_deliver", page=page, page_total=page_total, page_text=body)
