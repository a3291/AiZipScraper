"""schema.py — sidecar contract: category enum, low-confidence threshold, validation."""
from __future__ import annotations

CATEGORIES = ["dataset", "media", "software", "documents", "mixed", "unknown"]

LOW_CONFIDENCE = 0.6   # below this, mark "manual review recommended" in warnings/reports

# degraded-identity marker carried in identity dicts and in sidecar documents
FALLBACK_KEY = "_fallback_reason"


def validate(doc: dict) -> list[str]:
    """Validate a sidecar document; returns a list of problems (empty = valid)."""
    problems: list[str] = []
    anch = doc.get("anchoring")
    if not isinstance(anch, dict):
        problems.append("anchoring missing or not an object")
    else:
        for key in ("sha256", "source_path", "file_size", "mtime"):
            if key not in anch:
                problems.append(f"anchoring.{key} missing")
        if not (isinstance(anch.get("sha256"), str) and len(anch["sha256"]) == 64):
            problems.append("anchoring.sha256 is not a 64-char hex string")

    scrape = doc.get("scrape")
    if not isinstance(scrape, dict):
        problems.append("scrape missing or not an object")
    else:
        if scrape.get("depth") not in ("full", "direct"):
            problems.append(f"scrape.depth invalid: {scrape.get('depth')!r}")
        c = scrape.get("confidence")
        if not (isinstance(c, (int, float)) and 0.0 <= c <= 1.0):
            problems.append("scrape.confidence not in [0,1]")

    ident = doc.get("identity")
    if not isinstance(ident, dict):
        problems.append("identity missing or not an object")
    else:
        if ident.get("category") not in CATEGORIES:
            problems.append(f"identity.category invalid: {ident.get('category')!r}")
        for key in ("title", "summary"):
            if not isinstance(ident.get(key), str):
                problems.append(f"identity.{key} should be a string")
        for key in ("tags", "language"):
            if not isinstance(ident.get(key), list):
                problems.append(f"identity.{key} should be an array")

    flags = doc.get("flags")
    if not isinstance(flags, dict):
        problems.append("flags missing or not an object")
    else:
        for key in ("password_protected", "multi_part", "exe_present", "macro_docs"):
            if not isinstance(flags.get(key), bool):
                problems.append(f"flags.{key} should be a boolean")
        if not isinstance(flags.get("nested_archives"), list):
            problems.append("flags.nested_archives should be an array")

    if not isinstance(doc.get("warnings"), list):
        problems.append("warnings should be an array")
    if not isinstance(doc.get("structure"), dict):
        problems.append("structure missing or not an object")
    fb = doc.get(FALLBACK_KEY)
    if fb is not None and not isinstance(fb, str):
        problems.append(f"{FALLBACK_KEY} should be a string when present")

    return problems
