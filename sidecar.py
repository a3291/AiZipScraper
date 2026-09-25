"""sidecar.py — 侧车 JSON 读写、幂等跳过、改名/移动校验、孤儿检测。"""
from __future__ import annotations

import json
import os
from pathlib import Path

from schema import SCHEMA_VERSION, empty_document, validate


def sidecar_path(archive: str | Path) -> Path:
    """数据集A.zip -> 数据集A.zip.meta.json（同目录）。"""
    p = Path(archive)
    return p.with_name(p.name + ".meta.json")


def load(archive: str | Path) -> dict | None:
    sp = sidecar_path(archive)
    if not sp.exists():
        return None
    try:
        doc = json.loads(sp.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return doc if isinstance(doc, dict) else None


def write(archive: str | Path, doc: dict) -> Path:
    """原子写入：临时文件 + os.replace。"""
    problems = validate(doc)
    if problems:
        raise ValueError(f"拒绝写入非法侧车文档: {problems}")
    sp = sidecar_path(archive)
    tmp = sp.with_suffix(".meta.json.tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, sp)
    return sp


def needs_scrape(archive: str | Path, sha256: str, *, force: bool = False) -> bool:
    """幂等判断：True = 需要刮削。

    - 无侧车 → 刮
    - --force → 刮
    - 侧车 schema_version 落后于当前版本 → 刮（升级）
    - 哈希一致 → 跳过；哈希不一致（文件变了）→ 刮
    """
    if force:
        return True
    doc = load(archive)
    if doc is None:
        return True
    if doc.get("schema_version") != SCHEMA_VERSION:
        return True
    return doc.get("anchoring", {}).get("sha256") != sha256


def reconcile_path(archive: str | Path, doc: dict) -> tuple[dict, bool]:
    """改名/移动校验：source_path 与实际不符时修正并加 warning。

    返回 (可能被修改的 doc, 是否有改动)。调用方负责写回。
    """
    actual = str(Path(archive).resolve())
    recorded = doc.get("anchoring", {}).get("source_path", "")
    if recorded == actual:
        return doc, False
    doc = dict(doc)
    doc["anchoring"] = dict(doc["anchoring"], source_path=actual)
    warnings = list(doc.get("warnings", []))
    warnings.append(f"文件已从 {recorded!r} 移动/改名至当前位置，source_path 已修正")
    doc["warnings"] = warnings
    return doc, True


def find_orphans(root: str | Path) -> list[Path]:
    """孤儿侧车：有 .meta.json 但对应压缩包不存在。不自动删除。"""
    orphans: list[Path] = []
    for sp in Path(root).rglob("*.meta.json"):
        name = sp.name
        if not name.endswith(".meta.json"):
            continue
        base = name[: -len(".meta.json")]
        if not (sp.parent / base).exists():
            orphans.append(sp)
    return orphans
