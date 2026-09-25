"""backend.py — AI 服务器/API 连接层（与识别会话逻辑分离）。

OpenAI 兼容协议：/models 探测与模型解析、/chat/completions 请求。
默认后端 LM Studio；任何 OpenAI 兼容端点均可（Ollama /v1、vLLM、llama.cpp server）。
"""
from __future__ import annotations

import json
import urllib.request

PROVIDER_DEFAULTS = {
    "lmstudio": "http://localhost:1234/v1",
    "ollama": "http://localhost:11434/v1",
}


def api_headers(api_key: str) -> dict:
    return {"Content-Type": "application/json",
            "Authorization": f"Bearer {api_key or 'none'}"}


def post(url: str, payload: dict, timeout: int, api_key: str = "") -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), headers=api_headers(api_key))
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def resolve_model(cfg: dict) -> str | None:
    """model 留空时向后端 /models 查询，取第一个可用模型。"""
    if cfg.get("model"):
        return cfg["model"]
    try:
        req = urllib.request.Request(
            f"{cfg['base_url'].rstrip('/')}/models",
            headers=api_headers(cfg.get("api_key", "")))
        with urllib.request.urlopen(req, timeout=5) as resp:
            models = (json.loads(resp.read()).get("data")) or []
        return models[0]["id"] if models else None
    except (OSError, KeyError, json.JSONDecodeError):
        return None


def endpoint_available(cfg: dict) -> bool:
    try:
        req = urllib.request.Request(
            f"{cfg['base_url'].rstrip('/')}/models",
            headers=api_headers(cfg.get("api_key", "")))
        with urllib.request.urlopen(req, timeout=3):
            return True
    except OSError:
        return False
