"""schema.py — 侧车契约单源：大类枚举、低置信阈值、校验。

字段名英文（机器友好）。
"""
from __future__ import annotations

CATEGORIES = ["dataset", "media", "software", "documents", "mixed", "unknown"]

LOW_CONFIDENCE = 0.6   # 低于此值在 warnings/报告中标"建议人工复核"

IDENTITY_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "category": {"type": "string", "enum": CATEGORIES},
        "summary": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "language": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number"},
    },
    "required": ["title", "category", "summary", "tags", "language", "confidence"],
}


def validate(doc: dict) -> list[str]:
    """校验侧车文档结构，返回问题列表（空列表 = 合法）。"""
    problems: list[str] = []
    anch = doc.get("anchoring")
    if not isinstance(anch, dict):
        problems.append("anchoring 缺失或非对象")
    else:
        for key in ("sha256", "source_path", "file_size", "mtime"):
            if key not in anch:
                problems.append(f"anchoring.{key} 缺失")
        if not (isinstance(anch.get("sha256"), str) and len(anch["sha256"]) == 64):
            problems.append("anchoring.sha256 不是 64 位十六进制串")

    scrape = doc.get("scrape")
    if not isinstance(scrape, dict):
        problems.append("scrape 缺失或非对象")
    else:
        if scrape.get("depth") not in ("full", "direct"):
            problems.append(f"scrape.depth 非法: {scrape.get('depth')!r}")
        c = scrape.get("confidence")
        if not (isinstance(c, (int, float)) and 0.0 <= c <= 1.0):
            problems.append("scrape.confidence 不在 [0,1]")

    ident = doc.get("identity")
    if not isinstance(ident, dict):
        problems.append("identity 缺失或非对象")
    else:
        if ident.get("category") not in CATEGORIES:
            problems.append(f"identity.category 非法: {ident.get('category')!r}")
        for key in ("title", "summary"):
            if not isinstance(ident.get(key), str):
                problems.append(f"identity.{key} 应为字符串")
        for key in ("tags", "language"):
            if not isinstance(ident.get(key), list):
                problems.append(f"identity.{key} 应为数组")

    flags = doc.get("flags")
    if not isinstance(flags, dict):
        problems.append("flags 缺失或非对象")
    else:
        for key in ("password_protected", "multi_part", "exe_present", "macro_docs"):
            if not isinstance(flags.get(key), bool):
                problems.append(f"flags.{key} 应为布尔值")
        if not isinstance(flags.get("nested_archives"), list):
            problems.append("flags.nested_archives 应为数组")

    if not isinstance(doc.get("warnings"), list):
        problems.append("warnings 应为数组")
    if not isinstance(doc.get("structure"), dict):
        problems.append("structure 缺失或非对象")

    return problems
