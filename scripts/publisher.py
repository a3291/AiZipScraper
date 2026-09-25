"""publisher.py — publisher: publish.json template → fill → validate → <name>.publish.json.

Legitimacy gate: the sidecar has exactly one generation path and always passes
schema validation; invalid output never hits disk.
The program fills <A_*> (anchoring/structure/stats), the AI fills <I_*>
(identity/confidence).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import paths
import schema

PLACEHOLDER_RE = re.compile(r"<([AI]_[A-Z0-9_]+)>")


def load_template(path: str | None = None) -> dict:
    p = Path(path) if path else paths.JSONS / "publish.json"
    return json.loads(p.read_text(encoding="utf-8-sig"))


def _coerce(value: str, ph: str):
    """Placeholder value → target type (I_TAGS/I_LANGUAGE/I_WARNINGS/flags etc. are JSON strings)."""
    if ph in ("I_TAGS", "I_LANGUAGE", "A_WARNINGS", "A_TOP_LEVEL_DIRS",
              "A_NOTABLE_FILES"):
        return json.loads(value) if isinstance(value, str) else value
    if ph in ("A_FILE_SIZE", "A_ENTRY_COUNT", "A_DIR_COUNT",
              "A_TOTAL_UNCOMPRESSED"):
        return int(value)
    if ph == "I_CONFIDENCE":
        return float(value)
    if ph in ("A_TOP_EXTENSIONS", "A_FLAGS"):
        return json.loads(value) if isinstance(value, str) else value
    return value


def fill(template: dict, program: dict, identity: dict,
         warnings: list[str]) -> dict:
    """Fill the template. program holds program-side fields, identity the AI-side identity."""
    ctx = {
        "A_SHA256": program["sha256"],
        "A_SOURCE_PATH": program["source_path"],
        "A_FILE_SIZE": str(program["file_size"]),
        "A_MTIME": program["mtime"],
        "A_SCRAPED_AT": program["scraped_at"],
        "A_ENGINE": program["engine"],
        "A_DEPTH": program["depth"],
        "A_ENTRY_COUNT": str(program["structure"]["entry_count"]),
        "A_DIR_COUNT": str(program["structure"]["dir_count"]),
        "A_TOTAL_UNCOMPRESSED": str(program["structure"]["total_uncompressed"]),
        "A_TOP_EXTENSIONS": json.dumps(program["structure"]["top_extensions"],
                                       ensure_ascii=False),
        "A_TOP_LEVEL_DIRS": json.dumps(program["structure"]["top_level_dirs"],
                                       ensure_ascii=False),
        "A_NOTABLE_FILES": json.dumps(program["structure"]["notable_files"],
                                      ensure_ascii=False),
        "A_FLAGS": json.dumps(program["flags"], ensure_ascii=False),
        "A_WARNINGS": json.dumps(warnings, ensure_ascii=False),
        "I_CONFIDENCE": str(identity.get("confidence", 0.0)),
        "I_TITLE": identity.get("title", ""),
        "I_CATEGORY": identity.get("category", "unknown"),
        "I_SUMMARY": identity.get("summary", ""),
        "I_TAGS": json.dumps(identity.get("tags", []), ensure_ascii=False),
        "I_LANGUAGE": json.dumps(identity.get("language", []), ensure_ascii=False),
    }

    def fill_node(node):
        if isinstance(node, dict):
            return {k: fill_node(v) for k, v in node.items()}
        if isinstance(node, str):
            m = PLACEHOLDER_RE.fullmatch(node.strip())
            if m:
                return _coerce(ctx[m.group(1)], m.group(1))
        return node

    doc = fill_node(template["fields"])
    doc["_fallback_reason"] = identity.get("_fallback_reason")
    return doc


def publish(target: str | Path, program: dict, identity: dict,
            warnings: list[str], template_path: str | None = None) -> tuple[Path | None, list[str]]:
    """Main entry: fill template → validate → write <name>.publish.json.

    Returns (sidecar path | None, list of validation problems).
    None = validation failed, nothing written.
    """
    template = load_template(template_path)
    doc = fill(template, program, identity, warnings)
    problems = schema.validate(doc)
    if problems:
        return None, problems
    side = Path(str(target) + ".publish.json")
    side.write_text(json.dumps(doc, ensure_ascii=False, indent=1),
                    encoding="utf-8")
    return side, []
