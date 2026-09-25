"""ai_identify.py — AI 识别层（OpenAI 兼容 API + JSON Schema 结构化输出）。

默认后端 LM Studio（http://localhost:1234/v1）；任何 OpenAI 兼容端点均可，
包括 Ollama 的 /v1 兼容层、vLLM、llama.cpp server。
结构化输出直接复用 schema.IDENTITY_JSON_SCHEMA，各后端共用同一契约。

配置来源：scraper.json（或 CLI --config 指定路径）。
服务不可用或输出非法时优雅降级（category=unknown + warning）。
"""
from __future__ import annotations

import json
import urllib.request
from pathlib import Path

from schema import IDENTITY_JSON_SCHEMA

PROVIDER_DEFAULTS = {
    "lmstudio": "http://localhost:1234/v1",
    "ollama": "http://localhost:11434/v1",
}

DEFAULT_CONFIG = {
    "ai": {
        "provider": "lmstudio",
        "base_url": PROVIDER_DEFAULTS["lmstudio"],
        "model": "",              # 留空 = 取后端已加载的第一个模型
        "api_key": "lm-studio",
        "temperature": 0.2,
        "timeout": 300,
    }
}

LOW_CONFIDENCE = 0.6
EXCERPT_BUDGET = 4000            # 摘录封顶字符


def load_config(path: str | None = None) -> dict:
    """读配置文件并合并默认值。仅保留 ai 段有效键。"""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    p = Path(path) if path else Path("scraper.json")
    user_base_url = None
    if p.exists():
        try:
            ai = json.loads(p.read_text(encoding="utf-8")).get("ai", {})
            user_base_url = ai.get("base_url")
            cfg["ai"].update({k: v for k, v in ai.items() if k in cfg["ai"]})
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            pass  # 配置损坏时静默用默认值
    # provider 变更时 base_url 跟随其默认值，除非用户显式指定了 base_url
    provider = cfg["ai"].get("provider")
    if provider in PROVIDER_DEFAULTS and not user_base_url:
        cfg["ai"]["base_url"] = PROVIDER_DEFAULTS[provider]
    return cfg["ai"]


def _api_headers(api_key: str) -> dict:
    return {"Content-Type": "application/json",
            "Authorization": f"Bearer {api_key or 'none'}"}


def _post(url: str, payload: dict, timeout: int) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers=_api_headers(payload.pop("_api_key", "none")))
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def resolve_model(cfg: dict) -> str | None:
    """model 留空时向后端 /models 查询，取第一个可用模型。"""
    if cfg.get("model"):
        return cfg["model"]
    try:
        req = urllib.request.Request(
            f"{cfg['base_url'].rstrip('/')}/models",
            headers=_api_headers(cfg.get("api_key", "")))
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        models = data.get("data") or []
        return models[0]["id"] if models else None
    except (OSError, KeyError, json.JSONDecodeError):
        return None


def _build_prompt(extracted: dict, category_hint: str) -> str:
    st = extracted["structure"]
    ev = extracted.get("sample_evidence", [])
    exts = st.get("top_extensions", {})

    parts = [
        "你是压缩包内容分析器。根据以下信号判断这个压缩包是什么。",
        f"扩展名统计: {json.dumps(exts, ensure_ascii=False)}",
        f"顶层目录: {st.get('top_level_dirs', [])}",
        f"值得注意的文件: {st.get('notable_files', [])}",
        f"条目数: {st.get('entry_count')}, 解压后总量: {st.get('total_uncompressed')} 字节",
        f"程序预判大类（仅供参考，可推翻）: {category_hint}",
        f"安全标记: {json.dumps(extracted.get('flags', {}), ensure_ascii=False)}",
    ]
    if ev:
        parts.append("抽样内容摘录（原文证据）:")
        used = 0
        for e in ev:
            if used >= EXCERPT_BUDGET:
                break
            cut = e["excerpt"][: EXCERPT_BUDGET - used]
            parts.append(f"--- {e['file']} ---\n{cut}")
            used += len(cut)
    parts.append(
        "请输出 JSON：title(简洁名称)、category(dataset/media/software/documents/mixed/unknown)、"
        "summary(两到三句话)、tags(标签数组)、language(语言代码数组)、confidence(0到1的小数)。"
        "证据不足时 category 用 unknown 且 confidence 降低。只输出 JSON。"
    )
    return "\n".join(parts)


def _hint_from_extensions(exts: dict[str, int]) -> str:
    """扩展名统计 → 大类猜测，作为提示而非结论。"""
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


def _endpoint_available(cfg: dict) -> bool:
    try:
        req = urllib.request.Request(
            f"{cfg['base_url'].rstrip('/')}/models",
            headers=_api_headers(cfg.get("api_key", "")))
        with urllib.request.urlopen(req, timeout=3):
            return True
    except OSError:
        return False


def identify(extracted: dict, cfg: dict | None = None) -> tuple[dict, float, list[str]]:
    """返回 (identity_dict, confidence, warnings)。

    cfg 为 ai 配置段（load_config 的返回值）。
    """
    warnings: list[str] = []
    cfg = cfg or load_config()
    if not _endpoint_available(cfg):
        return _fallback(f"AI 后端不可用（{cfg['base_url']}），已降级为 unknown"), 0.0, warnings

    model = resolve_model(cfg)
    if model is None:
        return _fallback(f"AI 后端无已加载模型（{cfg['base_url']}）"), 0.0, warnings

    exts = extracted["structure"].get("top_extensions", {})
    hint = _hint_from_extensions(exts)
    prompt = _build_prompt(extracted, hint)

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": cfg.get("temperature", 0.2),
        "stream": False,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "archive_identity",
                "strict": True,
                "schema": IDENTITY_JSON_SCHEMA,
            },
        },
    }

    out = None
    timeout = int(cfg.get("timeout", 300))
    url = f"{cfg['base_url'].rstrip('/')}/chat/completions"
    for attempt in (1, 2):
        try:
            resp = _post(url, {**payload, "_api_key": cfg.get("api_key", "")}, timeout)
            content = resp["choices"][0]["message"]["content"]
            out = json.loads(content)
            break
        except (OSError, KeyError, IndexError, json.JSONDecodeError) as e:
            if attempt == 2:
                return _fallback(f"AI 识别两次尝试均失败: {type(e).__name__}"), 0.0, warnings

    # 程序校验
    conf = out.get("confidence")
    if not (isinstance(conf, (int, float)) and 0.0 <= conf <= 1.0):
        warnings.append("AI 返回的 confidence 非法，按 0.5 处理")
        conf = 0.5
    if out.get("category") not in IDENTITY_JSON_SCHEMA["properties"]["category"]["enum"]:
        warnings.append(f"AI 返回的 category 非法: {out.get('category')!r}，按 unknown 处理")
        out["category"] = "unknown"

    identity = {
        "title": str(out.get("title", ""))[:200],
        "category": out["category"],
        "summary": str(out.get("summary", ""))[:2000],
        "tags": [str(t) for t in out.get("tags", [])][:20],
        "language": [str(t) for t in out.get("language", [])][:10],
    }
    if conf < LOW_CONFIDENCE:
        warnings.append(f"低置信度 {conf:.2f} < {LOW_CONFIDENCE}，建议人工复核")
    return identity, round(float(conf), 2), warnings


def _fallback(reason: str) -> dict:
    return {
        "title": "",
        "category": "unknown",
        "summary": "",
        "tags": [],
        "language": [],
        "_fallback_reason": reason,
    }
