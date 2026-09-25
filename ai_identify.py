"""ai_identify.py — Ollama 本地模型识别层（schema 约束输出）。

输入：extractor 的中间 JSON；输出：identity + confidence + 语义 warnings。
Ollama 不可用或输出非法时优雅降级（category=unknown + warning）。
"""
from __future__ import annotations

import json
import urllib.request

from schema import IDENTITY_JSON_SCHEMA

OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL = "qwen2.5:14b"           # N卡 12GB+ 档位；可在 CLI 覆盖
LOW_CONFIDENCE = 0.6
EXCERPT_BUDGET = 4000            # 摘录封顶字符


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


def _ollama_available() -> bool:
    try:
        with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=3):
            return True
    except OSError:
        return False


def identify(extracted: dict, model: str = MODEL) -> tuple[dict, float, list[str]]:
    """返回 (identity_dict, confidence, warnings)。"""
    warnings: list[str] = []
    if not _ollama_available():
        return _fallback("Ollama 服务不可用（localhost:11434），已降级为 unknown"), 0.0, warnings

    exts = extracted["structure"].get("top_extensions", {})
    hint = _hint_from_extensions(exts)
    prompt = _build_prompt(extracted, hint)

    for attempt in (1, 2):
        try:
            payload = json.dumps({
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "format": IDENTITY_JSON_SCHEMA,
                "stream": False,
                "options": {"temperature": 0.2},
            }).encode("utf-8")
            req = urllib.request.Request(
                OLLAMA_URL, data=payload, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=300) as resp:
                content = json.loads(resp.read())["message"]["content"]
            out = json.loads(content)
            break
        except (OSError, KeyError, json.JSONDecodeError) as e:
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
