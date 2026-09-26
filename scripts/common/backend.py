"""HTTP access to an OpenAI-compatible chat backend (LM Studio, Ollama, ...)."""
import json
import time
import urllib.request

_TIMEOUT_GRACE = 30


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


def endpoint_available(cfg, timeout=10):
    try:
        _get(cfg, "/models", timeout)
        return True
    except Exception:
        return False


def resolve_model(cfg, timeout=10):
    model = cfg.get("model", "")
    if model:
        return model
    data = _get(cfg, "/models", timeout)
    models = data.get("data") or []
    if not models:
        raise RuntimeError(f"{endpoint(cfg)} lists no models; set ai.model in config")
    return models[0].get("id", "")


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
    with urllib.request.urlopen(req, timeout=cfg["timeout"] + _TIMEOUT_GRACE) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    usage = data.get("usage") or {}
    return {
        "content": _extract_content(data),
        "seconds": round(time.monotonic() - start, 2),
        "tokens_in": usage.get("prompt_tokens", 0),
        "tokens_out": usage.get("completion_tokens", 0),
    }


def _extract_content(payload):
    choices = payload.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return content if isinstance(content, str) else ""
