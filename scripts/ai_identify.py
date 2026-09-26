"""ai_identify.py — recognition session engine.

run_session(context_pkg, prompts, cfg, mlog, hint_exts)
  → (identity, confidence, warnings, stats); appends to messages.json per turn.
  context_pkg shape (from context_builder.build): pages [{no, chars, text}],
  tail_page {no, text}, stats.page_count.
  identity always carries "confidence"; a degraded identity carries
  schema.FALLBACK_KEY and stats["published"] = False.
Guardrails: max_turns cap (negative = unlimited), one invalid-JSON tolerance,
page stall → forced publish, 2 more page turns after force → degrade.
Chatlog mode (cfg["chatlog"], from --auto-chatlog): remind_at = summary
trigger (side-call digest); force_publish_at = forced roll without a digest
(soft boundary: raw messages stay archived in messages.json). A roll re-issues
the opening prompt with the digests and read-progress merged into the
tail-page section — a context channel parallel to the page context, never a
dialogue message. max_turns = roll cap (-1 unlimited; once reached the
watermarks fall back to their original meanings).
"""
from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path

import backend
import paths
import schema

CATEGORIES = schema.CATEGORIES
LOW_CONFIDENCE = schema.LOW_CONFIDENCE
JSONS = paths.JSONS

REQUIRED_AI_KEYS = ["base_url", "model", "api_key", "temperature", "timeout",
                    "page_chars", "remind_at", "force_publish_at", "max_turns"]

REQUIRED_PROMPT_KEYS = ["system", "first", "remind", "force_publish",
                        "bad_json_retry", "force_publish_only", "dup_page",
                        "stall_to_publish", "page_deliver",
                        "chatlog_summarize", "chatlog_roll"]

SUMMARY_MAX_CHARS = 4000   # a longer summary counts as invalid → forced roll


def load_config(path: str | None = None) -> dict:
    """Read jsons/scraper.json; missing or mistyped keys raise."""
    p = Path(path) if path else JSONS / "scraper.json"
    try:
        raw = json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(f"config file unreadable: {p} ({e})") from e

    ai = raw.get("ai") or {}
    missing = [k for k in REQUIRED_AI_KEYS if k not in ai]
    if missing:
        raise ValueError(f"scraper.json missing ai keys: {missing} (file {p})")

    conc = raw.get("concurrency")
    if not isinstance(conc, int) or conc < 1:
        raise ValueError(f"scraper.json concurrency must be a positive integer (got: {conc!r})")

    limits = raw.get("limits")
    if not isinstance(limits, dict):
        raise ValueError(f"scraper.json limits must be an object (got: {limits!r})")
    ets = limits.get("extract_timeout_s")
    if not isinstance(ets, int) or ets < 1:
        raise ValueError("scraper.json limits.extract_timeout_s must be a positive integer")
    mtb = limits.get("max_text_file_bytes")
    if not isinstance(mtb, int) or mtb < 1:
        raise ValueError("scraper.json limits.max_text_file_bytes must be a positive integer")
    ratio = limits.get("sentence_max_ratio")
    if not isinstance(ratio, (int, float)) or not 0 < ratio <= 1:
        raise ValueError("scraper.json limits.sentence_max_ratio must be in (0, 1]")

    cfg = dict(ai)
    cfg["concurrency"] = conc
    cfg["limits"] = dict(limits)
    return cfg


def load_prompts(path: str | None = None) -> dict:
    """Read jsons/prompt.json (prompt registry + action contract).

    The publish action's category enum must match schema.CATEGORIES.
    """
    p = Path(path) if path else JSONS / "prompt.json"
    data = json.loads(p.read_text(encoding="utf-8-sig"))
    contract = data["contract"]
    try:
        enum = contract["schema"]["properties"]["identity"]["anyOf"][1] \
            ["properties"]["category"]["enum"]
    except (KeyError, IndexError, TypeError):
        raise ValueError(f"prompt.json contract lacks the identity.category enum: {p}")
    if sorted(enum) != sorted(schema.CATEGORIES):
        raise ValueError(f"prompt.json category enum {enum} does not match "
                         f"schema.CATEGORIES {schema.CATEGORIES}")
    prompts = {k: v["text"] for k, v in data["prompts"].items()}
    missing = [k for k in REQUIRED_PROMPT_KEYS if k not in prompts]
    if missing:
        raise ValueError(f"prompt.json missing prompt keys: {missing} (file {p})")
    roles = {k: v.get("role", "user") for k, v in data["prompts"].items()}
    return {"contract": contract, "prompts": prompts, "roles": roles,
            "summary_contract": data.get("summary_contract")}


def _hint_from_extensions(exts: dict[str, int]) -> str:
    total = sum(exts.values()) or 1
    ratio = lambda names: sum(n for k, n in exts.items() if k in names) / total
    if ratio({".shp", ".dbf", ".shx", ".geojson", ".tif", ".tiff", ".gdb"}) > 0.3:
        return "dataset"
    if ratio({".mp4", ".mkv", ".avi", ".mp3", ".flac", ".jpg", ".png", ".pdf"}) > 0.5:
        return "media"
    if ratio({".py", ".js", ".ts", ".java", ".go", ".rs", ".c", ".cpp", ".h"}) > 0.2:
        return "software"
    if ratio({".docx", ".doc", ".xlsx", ".pptx", ".txt", ".md", ".epub"}) > 0.5:
        return "documents"
    return "mixed"


def _extract_json(content: str) -> dict | None:
    text = content.strip()
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        text = m.group(1)
    else:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            text = text[start:end + 1]
    try:
        out = json.loads(text)
        return out if isinstance(out, dict) else None
    except json.JSONDecodeError:
        return None


def _estimate_tokens(messages: list[dict]) -> int:
    return sum(len(m.get("content", "")) for m in messages) // 4


# ---------- messages.json (organized per message, atomic rewrite per turn) ----------

class MessageLog:
    """Re-read, merge and atomically rewrite messages.json under a class-level lock."""

    _lock = threading.Lock()   # class-level lock

    def __init__(self, run_dir: Path, entry_id: str):
        self.path = run_dir / "messages.json"
        self.entry_id = entry_id

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return {"version": "2.0", "packages": {}}

    def append(self, role: str, text: str, prompt_key: str | None = None):
        with MessageLog._lock:
            data = self._load()
            msgs = data.setdefault("packages", {}) \
                       .setdefault(self.entry_id, {"messages": []})["messages"]
            msg = {"role": role, "text": text}
            if prompt_key:
                msg["prompt_key"] = prompt_key
            msgs.append(msg)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            os.replace(tmp, self.path)

    def count(self) -> int:
        with MessageLog._lock:
            data = self._load()
            return len(data.get("packages", {})
                       .get(self.entry_id, {}).get("messages", []))


# ---------- session ----------

def _say(messages: list[dict], mlog: "MessageLog", R: dict, P: dict,
         key: str, text: str | None = None) -> None:
    """Append one prompt message to the session and mirror it into messages.json."""
    role = R[key]
    content = text if text is not None else P[key]
    messages.append({"role": role, "content": content})
    mlog.append(role, content, key)


def run_session(context_pkg: dict, prompts: dict, cfg: dict,
                mlog: MessageLog, hint_exts: dict[str, int] | None = None,
                ) -> tuple[dict, float, list[str], dict]:
    """Single-package recognition session. Returns (identity, confidence, warnings, stats)."""
    warnings: list[str] = []
    stats = {"pages": context_pkg["stats"]["page_count"], "pages_read": 0,
             "turns": 0, "tokens_in": 0, "tokens_out": 0, "rolls": 0,
             "published": True}

    def bail(reason: str):
        stats["published"] = False
        return _fallback(reason), 0.0, warnings, stats

    if not backend.endpoint_available(cfg):
        tried = cfg.get("base_url") or ", ".join(backend.CANDIDATE_ENDPOINTS)
        return bail(f"AI backend unreachable ({tried})")
    model = backend.resolve_model(cfg)
    if model is None:
        return bail(f"AI backend has no loaded model ({cfg['base_url']})")

    P = prompts["prompts"]          # key -> text template
    R = prompts["roles"]            # key -> role
    contract = prompts["contract"]

    exts = hint_exts or {}
    hint = _hint_from_extensions(exts)
    tail_text = context_pkg["tail_page"]["text"]   # last page (metadata) shown first by default
    catalog = (f"Content pages: {stats['pages']} total (pages 1..{stats['pages']}); "
               f"the last page is page {context_pkg['tail_page']['no']}.")

    turns_desc = "unlimited" if cfg["max_turns"] < 0 else str(cfg["max_turns"])
    messages: list[dict] = [
        {"role": R["system"], "content": P["system"]},
        {"role": R["first"], "content": P["first"].format(
            depth="full", hint=hint, max_turns=turns_desc,
            page_chars=cfg["page_chars"], tail_page=tail_text,
            catalog=catalog)},
    ]
    mlog.append(messages[0]["role"], messages[0]["content"], "system")
    mlog.append(messages[1]["role"], messages[1]["content"], "first")

    reminded = forced = False
    forced_retries = bad_json = dupes = 0
    read_pages: set[int] = set()
    usage_tokens = 0
    pages = context_pkg["pages"]

    chatlog = bool(cfg.get("chatlog"))
    rolls = 0
    summaries: list[str] = []
    summary_tried = False
    system_msg = dict(messages[0])
    sum_contract = prompts.get("summary_contract")

    def roll(forced_roll: bool, summary: str | None) -> None:
        """Fold the history: re-issue the opening prompt with the digests and
        read-progress merged into the tail-page section (context channel,
        parallel to the page context)."""
        nonlocal rolls, usage_tokens, reminded, summary_tried
        rolls += 1
        stats["rolls"] = rolls
        if not forced_roll and summary:
            summaries.append(summary)
        if summaries:
            block = "\n\n".join(f"===== CHATLOG DIGEST {i + 1} =====\n{s}"
                                for i, s in enumerate(summaries))
        else:
            block = "(no digest was produced for the earlier conversation)"
        section = P["chatlog_roll"].format(
            roll_no=rolls, max_rolls=turns_desc, summaries_block=block,
            pages_read=", ".join(map(str, sorted(read_pages))) or "none",
            page_total=stats["pages"])
        messages.clear()
        messages.extend([
            dict(system_msg),
            {"role": R["first"], "content": P["first"].format(
                depth="full", hint=hint, max_turns=turns_desc,
                page_chars=cfg["page_chars"],
                tail_page=tail_text + "\n\n" + section, catalog=catalog)},
        ])
        mlog.append(R["chatlog_roll"], section, "chatlog_roll")
        usage_tokens = 0
        reminded = False
        summary_tried = False

    def ask_summary() -> str | None:
        """Side-call: ask the model to digest the conversation. Returns the
        summary text, or None when the reply is unusable (raw exchanges stay
        archived in messages.json)."""
        prompt = P["chatlog_summarize"]
        mlog.append(R["chatlog_summarize"], prompt, "chatlog_summarize")
        try:
            resp = backend.chat(cfg, messages + [{"role": R["chatlog_summarize"],
                                                  "content": prompt}],
                                sum_contract, cfg["timeout"])
            content = resp["choices"][0]["message"]["content"]
        except (OSError, KeyError, IndexError) as e:
            warnings.append(f"chatlog summary request failed: "
                            f"{type(e).__name__}: {e}")
            return None
        mlog.append("assistant", content, "chatlog_summarize_reply")
        out = _extract_json(content)
        s = out.get("summary") if isinstance(out, dict) else None
        if not isinstance(s, str) or not s.strip():
            warnings.append("chatlog summary invalid; waiting for the forced roll")
            return None
        if len(s) > SUMMARY_MAX_CHARS:
            warnings.append(f"chatlog summary over limit "
                            f"({len(s)} > {SUMMARY_MAX_CHARS} chars); "
                            f"waiting for the forced roll")
            return None
        return s

    turn = 0
    while chatlog or cfg["max_turns"] < 0 or turn < cfg["max_turns"]:
        turn += 1
        stats["turns"] = turn
        tokens = usage_tokens or _estimate_tokens(messages)
        can_roll = cfg["max_turns"] < 0 or rolls < cfg["max_turns"]
        if chatlog:
            if tokens >= cfg["force_publish_at"]:
                if can_roll:
                    roll(True, None)          # fold without a summary
                elif not forced:
                    _say(messages, mlog, R, P, "force_publish")
                    forced = True
            elif tokens >= cfg["remind_at"]:
                if can_roll and not summary_tried:
                    summary_tried = True
                    s = ask_summary()
                    if s is not None:
                        roll(False, s)
                elif not can_roll and not reminded:
                    _say(messages, mlog, R, P, "remind")
                    reminded = True
        elif not forced and tokens >= cfg["force_publish_at"]:
            _say(messages, mlog, R, P, "force_publish")
            forced = True
        elif not reminded and tokens >= cfg["remind_at"]:
            _say(messages, mlog, R, P, "remind")
            reminded = True

        try:
            resp = backend.chat(cfg, messages, contract, cfg["timeout"])
            usage = resp.get("usage") or {}
            stats["tokens_in"] = usage.get("prompt_tokens", 0)
            stats["tokens_out"] = usage.get("completion_tokens", 0)
            usage_tokens = stats["tokens_in"] + stats["tokens_out"]
            content = resp["choices"][0]["message"]["content"]
        except (OSError, KeyError, IndexError) as e:
            return bail(f"AI session request failed: {type(e).__name__}: {e}")
        messages.append({"role": "assistant", "content": content})
        mlog.append("assistant", content)

        out = _extract_json(content)
        if out is None or out.get("action") not in ("read_page", "publish"):
            bad_json += 1
            if bad_json > 1:
                return bail(f"invalid control JSON from AI ({bad_json} times)")
            _say(messages, mlog, R, P, "bad_json_retry",
                 P["bad_json_retry"].format(attempt_left=2 - bad_json))
            continue
        action = out["action"]

        if action == "publish":
            ident = out.get("identity")
            if not isinstance(ident, dict):
                return bail("publish missing identity object")
            identity, conf, vw = _validate_identity(ident)
            identity["confidence"] = conf
            warnings.extend(vw)
            return identity, conf, warnings, stats

        # read_page
        page = out.get("page")
        valid = isinstance(page, int) and 1 <= page <= len(pages)
        if forced:
            forced_retries += 1
            if forced_retries > 2:
                return bail("kept paging after forced publish; giving up")
            _say(messages, mlog, R, P, "force_publish_only")
            continue
        if not valid or page in read_pages:
            dupes += 1
            if dupes >= 2:
                _say(messages, mlog, R, P, "stall_to_publish")
                forced = True
            else:
                why = "page does not exist" if not valid else "page already read"
                _say(messages, mlog, R, P, "dup_page", P["dup_page"].format(why=why))
            continue
        read_pages.add(page)
        stats["pages_read"] = len(read_pages)
        dupes = 0
        body = pages[page - 1]["text"]
        _say(messages, mlog, R, P, "page_deliver",
             P["page_deliver"].format(page_no=page, page_text=body))

    return bail(f"reached max_turns={cfg['max_turns']} without publishing")


def _validate_identity(out: dict) -> tuple[dict, float, list[str]]:
    warnings: list[str] = []
    conf = out.get("confidence")
    if not (isinstance(conf, (int, float)) and 0.0 <= conf <= 1.0):
        warnings.append("AI returned an invalid confidence; using 0.5")
        conf = 0.5
    if out.get("category") not in CATEGORIES:
        warnings.append(f"AI returned an invalid category: {out.get('category')!r}; using unknown")
        out["category"] = "unknown"
    identity = {
        "title": str(out.get("title", ""))[:200],
        "category": out["category"],
        "summary": str(out.get("summary", ""))[:2000],
        "tags": [str(t) for t in out.get("tags", [])][:20],
        "language": [str(t) for t in out.get("language", [])][:10],
    }
    if conf < LOW_CONFIDENCE:
        warnings.append(f"low confidence {conf:.2f} < {LOW_CONFIDENCE}; manual review recommended")
    return identity, round(float(conf), 2), warnings


def _fallback(reason: str) -> dict:
    return {"title": "", "category": "unknown", "summary": "", "tags": [],
            "language": [], "confidence": 0.0, schema.FALLBACK_KEY: reason}
