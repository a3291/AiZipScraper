"""backend.py — AI server/API connection: endpoint auto-identification,
model resolution, chat turns (OpenAI messages style and LM Studio native style)."""
from __future__ import annotations

import json
import urllib.error
import urllib.request

# probed in order when ai.base_url is empty; the first candidate passing
# reachability + a minimal session wins
CANDIDATE_ENDPOINTS = [
    "http://localhost:1234/api/v1/chat",           # LM Studio native
    "http://localhost:1234/v1/chat/completions",   # LM Studio OpenAI-compatible
    "http://localhost:11434/v1/chat/completions",  # Ollama OpenAI-compatible
]
_PROBE_TEXT = "connectivity test. reply only: OK"


def api_headers(api_key: str) -> dict:
    return {"Content-Type": "application/json",
            "Authorization": f"Bearer {api_key or 'none'}"}


def post(url: str, payload: dict, timeout: int, api_key: str = "") -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), headers=api_headers(api_key))
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _probe_post(url: str, payload: dict, timeout: int,
                api_key: str) -> tuple[int | None, str]:
    """POST for probing: any HTTP response (4xx/5xx included) counts as reachable."""
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), headers=api_headers(api_key))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except OSError:
        return None, ""


def _get(url: str, timeout: int, api_key: str = "") -> dict:
    req = urllib.request.Request(url, headers=api_headers(api_key))
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def endpoint_mode(endpoint: str) -> str:
    """Full path …/chat → RESPONSES (native); …/chat/completions or a bare base
    (e.g. …/v1) → CHAT with the path appended."""
    e = endpoint.rstrip("/")
    return "RESPONSES" if e.endswith("/chat") else "CHAT"


def _chat_url(endpoint: str) -> str:
    e = endpoint.rstrip("/")
    return e if e.endswith(("/chat", "/chat/completions")) else e + "/chat/completions"


def _models_url(endpoint: str) -> str:
    e = endpoint.rstrip("/")
    for suffix in ("/chat/completions", "/chat"):
        if e.endswith(suffix):
            return e[:-len(suffix)] + "/models"
    return e + "/models"


def _extract_content(js) -> str | None:
    """Text from a CHAT- or RESPONSES-style body; None if none found."""
    if not isinstance(js, dict):
        return None
    out = js.get("output")
    if isinstance(out, list):
        texts = [o["content"] for o in out
                 if isinstance(o, dict) and o.get("type") == "message"
                 and isinstance(o.get("content"), str) and o["content"]]
        if texts:
            return "".join(texts)
    ch = js.get("choices")
    if isinstance(ch, list) and ch and isinstance(ch[0], dict):
        msg = ch[0].get("message")
        if isinstance(msg, dict) and isinstance(msg.get("content"), str) and msg["content"]:
            return msg["content"]
    return None


def _session_body(mode: str, model: str) -> dict:
    if mode == "CHAT":
        return {"model": model,
                "messages": [{"role": "user", "content": _PROBE_TEXT}],
                "stream": False}
    return {"model": model, "input": [{"type": "text", "content": _PROBE_TEXT}]}


def _try_endpoint(endpoint: str, model: str, api_key: str,
                  timeout: int) -> tuple[str, str] | None:
    """Probe one candidate (reachability + minimal session). Returns (endpoint,
    resolved model) on success, None on failure."""
    status, _ = _probe_post(endpoint, {}, timeout, api_key)
    if status is None:
        return None
    if not model:
        try:
            data = _get(_models_url(endpoint), timeout, api_key).get("data") or []
        except (OSError, json.JSONDecodeError, KeyError):
            return None
        if not data:
            return None
        model = data[0]["id"]
    status, raw = _probe_post(endpoint, _session_body(endpoint_mode(endpoint), model),
                              timeout, api_key)
    if status != 200:
        return None
    try:
        js = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return (endpoint, model) if _extract_content(js) is not None else None


def endpoint_available(cfg: dict) -> bool:
    """Resolve endpoint/model in place. Empty base_url → probe candidates;
    explicit base_url → check its /models. Fills cfg['base_url'], cfg['mode'],
    and cfg['model'] when it was empty."""
    api_key = cfg.get("api_key", "")
    timeout = min(int(cfg.get("timeout", 30)), 30)
    if not cfg.get("base_url"):
        for cand in CANDIDATE_ENDPOINTS:
            got = _try_endpoint(cand, cfg.get("model", ""), api_key, timeout)
            if got is not None:
                cfg["base_url"], model = got
                cfg["mode"] = endpoint_mode(cand)
                if not cfg.get("model"):
                    cfg["model"] = model
                return True
        return False
    cfg["mode"] = endpoint_mode(cfg["base_url"])
    try:
        _get(_models_url(cfg["base_url"]), 3, api_key)
        return True
    except (OSError, json.JSONDecodeError):
        return False


def resolve_model(cfg: dict) -> str | None:
    """When model is empty, query the backend /models and take the first available model."""
    if cfg.get("model"):
        return cfg["model"]
    try:
        models = _get(_models_url(cfg["base_url"]), 5,
                      cfg.get("api_key", "")).get("data") or []
    except (OSError, json.JSONDecodeError, KeyError):
        return None
    return models[0]["id"] if models else None


def chat(cfg: dict, messages: list[dict], contract: dict, timeout: int) -> dict:
    """One chat turn. CHAT mode sends the messages array plus the JSON-schema
    response_format; RESPONSES mode sends the conversation as one labeled text
    input. Returns a normalized body with the text at choices[0].message.content;
    usage is passed through when present."""
    mode = cfg.get("mode") or endpoint_mode(cfg["base_url"])
    if mode == "CHAT":
        payload = {"model": cfg["model"], "messages": messages,
                   "temperature": cfg["temperature"], "stream": False,
                   "response_format": {"type": "json_schema", "json_schema": contract}}
    else:
        text = "\n\n".join(f"[{m['role']}]\n{m['content']}" for m in messages)
        payload = {"model": cfg["model"],
                   "input": [{"type": "text", "content": text}]}
    try:
        js = post(_chat_url(cfg["base_url"]), payload, timeout, cfg.get("api_key", ""))
    except json.JSONDecodeError as e:
        raise OSError(f"non-JSON response: {e}") from e
    content = _extract_content(js)
    if content is None or "choices" in js:
        return js
    norm = {"choices": [{"message": {"content": content}}]}
    if isinstance(js.get("usage"), dict):
        norm["usage"] = js["usage"]
    return norm
