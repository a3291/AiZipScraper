"""Conversation engine: paged evidence in, identity out.

chatlog is the only mode: remind_at asks the model for a summary and folds the
history into a digest; force_publish_at folds without a summary; max_turns caps
folds (-1 unlimited; when the cap is reached the watermarks fall back to their
prompt-publish / force-publish meanings). Every raw message is archived with
its session number; a fold opens a new session.
"""
import json
import threading
from pathlib import Path

import backend
import schema
from paths import read_json, write_json

REQUIRED_AI_KEYS = {
    "base_url", "model", "api_key", "temperature", "timeout",
    "page_chars", "remind_at", "force_publish_at", "max_turns",
}
REQUIRED_PROMPT_KEYS = {
    "system", "first", "chatlog", "help", "remind", "force_publish",
    "bad_json_retry", "publish_retry", "dup_page",
    "stall_to_publish", "page_deliver", "chatlog_summarize",
    "chatlog_summarize_reply",
}
ESTIMATE_CHUNK = 4
ROLL_HARD_CAP = 20
MAX_FORCED_RETRIES = 2
BAD_JSON_TOLERANCE = 1
PUBLISH_RETRIES = 3

_MSG_LOCK = threading.Lock()


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
    """Appends every raw message (with its session number) to messages.json and
    tracks session boundaries in sessions.json."""

    def __init__(self, run_dir, key):
        self._run_dir = Path(run_dir)
        self._key = key
        self._entries = self._read("messages.json").get("packages", {}).get(key, [])
        self._sessions = (
            self._read("sessions.json").get("packages", {}).get(key, {"sessions": []})["sessions"]
        )
        self.session = len(self._sessions)

    def _read(self, name):
        p = self._run_dir / name
        return read_json(p) if p.exists() else {"packages": {}}

    def new_session(self):
        self.session += 1
        self._sessions.append({"no": self.session})
        self._flush()

    def append(self, role, text, prompt_key=None):
        entry = {"session": self.session, "role": role, "text": text}
        if prompt_key:
            entry["prompt_key"] = prompt_key
        self._entries.append(entry)
        self._flush()

    def count(self):
        return len(self._entries)

    def _flush(self):
        with _MSG_LOCK:
            msg = self._read("messages.json")
            ses = self._read("sessions.json")
            msg["packages"][self._key] = self._entries
            ses["packages"][self._key] = {"sessions": self._sessions}
            write_json(self._run_dir / "messages.json", msg)
            write_json(self._run_dir / "sessions.json", ses)


def _estimate_tokens(messages):
    return sum(len(m["content"]) for m in messages) // ESTIMATE_CHUNK


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


def _str_list(value, cap, warns, name):
    if not isinstance(value, list):
        if value not in (None, "", [],):
            warns.append(f"{name} {value!r} is not a list; set empty")
        return []
    out = []
    for item in value[:cap]:
        s = str(item).strip()
        if s:
            out.append(s)
    return out


def _validate_identity(ident):
    warns = []
    title = str(ident.get("title", "")).strip()[:300]
    category = ident.get("category")
    if category not in schema.CATEGORIES:
        warns.append(f"category {category!r} not in list; set unknown")
        category = "unknown"
    summary = str(ident.get("summary", "")).strip()[:2000]
    tags = _str_list(ident.get("tags"), 30, warns, "tags")
    language = _str_list(ident.get("language"), 12, warns, "language")
    conf = ident.get("confidence")
    if isinstance(conf, bool) or not isinstance(conf, (int, float)) or not 0.0 <= conf <= 1.0:
        warns.append(f"confidence {conf!r} invalid; set 0.5")
        conf = 0.5
    identity = {
        "title": title,
        "category": category,
        "summary": summary,
        "tags": tags,
        "language": language,
        "confidence": round(float(conf), 3),
    }
    if identity["confidence"] < schema.LOW_CONFIDENCE:
        warns.append(
            f"low confidence ({identity['confidence']}); needs human review"
        )
    return identity, warns


def run_session(context_pkg, pb, cfg, model, mlog):
    """Drive one identify conversation. Returns (identity, warnings, stats)."""
    missing = REQUIRED_PROMPT_KEYS - set(pb.prompts)
    if missing:
        raise ValueError(f"prompts.json missing keys: {sorted(missing)}")
    ai = cfg["ai"]
    pages = context_pkg["pages"]
    tail = context_pkg["tail_page"]
    page_total = len(pages) + 1
    rf = pb.response_format("session_actions")
    summary_limit = pb.limit("chatlog_summary", "summary")

    warned = []
    stats = {
        "pages": page_total, "pages_read": 0, "turns": 0, "rolls": 0,
        "tokens_in": 0, "tokens_out": 0,
    }
    read_pages = set()
    digests = []
    usage_tokens = 0
    rolls = 0
    max_turns = ai["max_turns"]
    forced = False
    reminded = False
    summary_tried = False
    forced_retries = 0
    dupes = 0
    bad_json_left = BAD_JSON_TOLERANCE
    retry_left = PUBLISH_RETRIES

    system = pb.get("system")
    first = pb.get("first", catalog=context_pkg["catalog"], tail=tail["text"])
    add = pb.get("add") if pb.has("add") else None

    def chatlog_msg():
        block = "\n".join(digests) if digests else "(no digests yet)"
        read = ", ".join(str(n) for n in sorted(read_pages)) or "none"
        return pb.get(
            "chatlog", roll_no=rolls, summaries_block=block,
            pages_read=read, page_total=page_total,
        )

    def rebuild():
        msgs = [system, first]
        if add:
            msgs.append(add)
        msgs.append(chatlog_msg())
        return msgs

    messages = rebuild()
    mlog.new_session()
    initial_keys = ["system", "first"] + (["add"] if add else []) + ["chatlog"]
    for m, key in zip(messages, initial_keys):
        mlog.append(m["role"], m["content"], key)

    def say(msg, key):
        messages.append(msg)
        mlog.append(msg["role"], msg["content"], key)

    def roll(with_digest, digest_text=None):
        nonlocal rolls, usage_tokens, reminded, summary_tried, forced, messages
        rolls += 1
        stats["rolls"] = rolls
        if with_digest and digest_text:
            digests.append(f"----- digest {rolls} -----\n{digest_text}")
        messages = rebuild()
        usage_tokens = 0
        reminded = False
        summary_tried = False
        mlog.new_session()
        roll_keys = ["system", "first"] + (["add"] if add else []) + ["chatlog"]
        for m, key in zip(messages, roll_keys):
            mlog.append(m["role"], m["content"], key)

    def ask_summary():
        m = pb.get("chatlog_summarize")
        try:
            r = backend.chat(messages + [m], pb.response_format("chatlog_summary"), model, cfg)
        except Exception as exc:
            warned.append(f"chatlog summary side call failed: {exc}")
            return None
        stats["tokens_in"] += r["tokens_in"]
        stats["tokens_out"] += r["tokens_out"]
        mlog.append(m["role"], m["content"], "chatlog_summarize")
        obj = _extract_json(r["content"])
        text = obj.get("summary") if isinstance(obj, dict) else None
        if not isinstance(text, str) or not text.strip():
            warned.append("chatlog summary invalid or empty")
            return None
        text = text.strip()
        if len(text) > summary_limit:
            warned.append(f"chatlog summary over limit ({len(text)} > {summary_limit})")
            return None
        mlog.append("assistant", r["content"], "chatlog_summarize_reply")
        return text

    def bail(reason):
        warned.append(f"gave up: {reason}")
        stats["pages_read"] = len(read_pages)
        identity = {
            "title": "", "category": "unknown", "summary": "",
            "tags": [], "language": [], "confidence": 0.0,
        }
        return identity, warned, stats

    def stall(why):
        nonlocal dupes, forced
        dupes += 1
        if dupes >= 2:
            forced = True
            say(pb.get("stall_to_publish"), "stall_to_publish")
        else:
            say(pb.get("dup_page", why=why), "dup_page")

    while True:
        can_roll = (max_turns < 0 or rolls < max_turns) and rolls < ROLL_HARD_CAP
        if usage_tokens >= ai["force_publish_at"]:
            if can_roll:
                roll(with_digest=False)
                continue
            if not forced:
                forced = True
                say(pb.get("force_publish"), "force_publish")
        elif usage_tokens >= ai["remind_at"]:
            if can_roll and not summary_tried:
                summary_tried = True
                digest = ask_summary()
                if digest is not None:
                    roll(with_digest=True, digest_text=digest)
                    continue
            elif not can_roll and not reminded:
                reminded = True
                say(pb.get("remind"), "remind")

        stats["turns"] += 1
        r = backend.chat(messages, rf, model, cfg)
        usage_tokens = r["tokens_in"] or _estimate_tokens(messages)
        stats["tokens_in"] += r["tokens_in"]
        stats["tokens_out"] += r["tokens_out"]
        reply = r["content"]
        mlog.append("assistant", reply)

        obj = _extract_json(reply)
        if obj is None or not isinstance(obj, dict):
            if bad_json_left > 0:
                say(pb.get("bad_json_retry", attempt_left=bad_json_left), "bad_json_retry")
                bad_json_left -= 1
                continue
            return bail("reply never became valid JSON")

        action = obj.get("action")
        if action == "help":
            say(pb.get("help", page_total=page_total), "help")
            continue

        if action == "publish":
            ident = obj.get("identity")
            tmpl = pb.publish_template["identity"]
            problems = schema.check(ident if isinstance(ident, dict) else {}, tmpl)
            if problems:
                if retry_left > 0:
                    say(pb.get(
                        "publish_retry",
                        missing="; ".join(p.split(".", 1)[-1] for p in problems),
                        attempt_left=retry_left,
                    ), "publish_retry")
                    retry_left -= 1
                    continue
                return bail("publish stayed malformed after retries")
            identity, norm_warns = _validate_identity(ident)
            warned.extend(norm_warns)
            stats["pages_read"] = len(read_pages)
            return identity, warned, stats

        if forced:
            forced_retries += 1
            if forced_retries > MAX_FORCED_RETRIES:
                return bail(f"kept requesting pages after forced publish ({action!r})")

        page = obj.get("page")
        ok = (
            isinstance(page, int) and not isinstance(page, bool)
            and 1 <= page <= page_total
        )
        if not ok:
            stall("missing or out-of-range page")
            continue
        if page in read_pages:
            stall(f"page {page} was already read")
            continue
        read_pages.add(page)
        dupes = 0
        body = pages[page - 1]["text"] if page <= len(pages) else tail["text"]
        say(pb.get("page_deliver", page=page, page_total=page_total, page_text=body),
            "page_deliver")
