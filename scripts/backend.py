"""HTTP access to an OpenAI-compatible chat backend (LM Studio, Ollama, ...)."""
import json
import time
import urllib.request


def endpoint(cfg):
    return str(cfg["base_url"]).rstrip("/")


def api_headers(cfg):
    headers = {"Content-Type": "application/json"}
    if cfg.get("api_key"):
        headers["Authorization"] = f"Bearer {cfg['api_key']}"
    return headers


def _get(cfg, path, timeout):
    req = urllib.request.Request(f"{endpoint(cfg)}{path}", headers=api_headers(cfg))
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def endpoint_available(cfg, timeout):
    try:
        _get(cfg, "/models", timeout)
        return True
    except Exception:
        return False


def chat(messages, contract, model, cfg):
    """One chat call under a json-schema contract. Returns content, elapsed
    seconds and token usage."""
    body = {
        "model": model,
        "messages": messages,
        "temperature": cfg["temperature"],
        "response_format": {"type": "json_schema", "json_schema": contract},
    }
    req = urllib.request.Request(
        f"{endpoint(cfg)}/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers=api_headers(cfg),
        method="POST",
    )
    start = time.monotonic()
    with urllib.request.urlopen(req, timeout=cfg["timeout"]) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    usage = data.get("usage") or {}
    return {
        "content": _extract_content(data),
        "seconds": round(time.monotonic() - start, 2),
        "tokens_in": usage.get("prompt_tokens", 0),
        "tokens_out": usage.get("completion_tokens", 0),
    }


def _extract_content(payload):
    """Pull the reply text out of a chat completion; a response without it is
    a backend contract violation, not an empty reply."""
    choices = payload["choices"]
    message = choices[0]["message"]
    content = message["content"]
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict))
    if not isinstance(content, str):
        raise RuntimeError(f"backend returned {type(content).__name__} content, expected string")
    return content
