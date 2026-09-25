"""ai_identify.py — recognition session engine.

What I am: per-turn session logic with the AI backend (connection logic lives
in backend.py).
My interface: run_session(context_pkg, prompts, cfg, mlog, hint_exts)
  → (identity, confidence, warnings, stats); appends to messages.json per turn.
Guardrails: max_turns, one invalid-JSON tolerance, page stall → forced publish,
2 more page turns after force → degrade; every failure path degrades to unknown.
"""
from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime
from pathlib import Path

import backend
import paths
import schema

CATEGORIES = schema.CATEGORIES
LOW_CONFIDENCE = schema.LOW_CONFIDENCE
JSONS = paths.JSONS

REQUIRED_AI_KEYS = ["provider", "base_url", "model", "api_key", "temperature",
                    "timeout", "page_chars", "remind_at", "force_publish_at",
                    "max_turns"]


def datetime_now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def load_config(path: str | None = None) -> dict:
    """Read jsons/scraper.json — the single source of config truth; missing keys raise (no silent defaults)."""
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

    cfg = dict(ai)
    if cfg["provider"] in backend.PROVIDER_DEFAULTS and not cfg["base_url"]:
        cfg["base_url"] = backend.PROVIDER_DEFAULTS[cfg["provider"]]
    cfg["concurrency"] = conc
    cfg["limits"] = dict(raw.get("limits") or {})
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
    roles = {k: v.get("role", "user") for k, v in data["prompts"].items()}
    return {"contract": contract, "prompts": prompts, "roles": roles}


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
    """The file is the truth: locked read-merge-atomic-write on append; supports concurrent api instances."""

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
    """Append one prompt message to the session and mirror it into messages.json (single point of dual write)."""
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
             "turns": 0, "tokens_in": 0, "tokens_out": 0}

    def bail(reason: str):
        return _fallback(reason), 0.0, warnings, stats

    if not backend.endpoint_available(cfg):
        return bail(f"AI backend unreachable ({cfg['base_url']})")
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

    messages: list[dict] = [
        {"role": R["system"], "content": P["system"]},
        {"role": R["first"], "content": P["first"].format(
            depth="full", hint=hint, max_turns=cfg["max_turns"],
            page_chars=cfg["page_chars"], tail_page=tail_text,
            catalog=catalog)},
    ]
    mlog.append(messages[0]["role"], messages[0]["content"], "system")
    mlog.append(messages[1]["role"], messages[1]["content"], "first")

    payload_base = {
        "model": model,
        "temperature": cfg["temperature"],
        "stream": False,
        "response_format": {"type": "json_schema", "json_schema": contract},
    }
    url = f"{cfg['base_url'].rstrip('/')}/chat/completions"

    reminded = forced = False
    forced_retries = bad_json = dupes = 0
    read_pages: set[int] = set()
    usage_tokens = 0
    pages = context_pkg["pages"]

    for turn in range(1, cfg["max_turns"] + 1):
        stats["turns"] = turn
        tokens = usage_tokens or _estimate_tokens(messages)
        if not forced and tokens >= cfg["force_publish_at"]:
            _say(messages, mlog, R, P, "force_publish")
            forced = True
        elif not reminded and tokens >= cfg["remind_at"]:
            _say(messages, mlog, R, P, "remind")
            reminded = True

        try:
            resp = backend.post(url, {**payload_base, "messages": messages},
                                cfg["timeout"], cfg.get("api_key", ""))
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
            "language": [], "_fallback_reason": reason}
