"""Packs extracted files into numbered pages with a trailing metadata page.

Page boundaries align to sentence ends; a sentence longer than
page_chars * sentence_max_ratio is dropped whole (PR: soft page boundary,
oversized sentences are not kept); pages never split a sentence. File
recognition is delegated to context_scanner.
"""
import os
from pathlib import Path

from common import paths
from extractor import context_scanner


def _split_sentences(text):
    parts, buf = [], []
    for ch in text:
        buf.append(ch)
        if ch in "。！？；;\n.!?":
            parts.append("".join(buf).strip())
            buf = []
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return [s for s in parts if s]


def paginate(text, page_chars, sentence_max_ratio):
    """Split text into page strings at sentence ends; sentences longer than
    page_chars * sentence_max_ratio are dropped whole."""
    max_sent = max(1, int(page_chars * sentence_max_ratio))
    pages, cur, cur_chars = [], [], 0
    for sent in _split_sentences(text):
        if len(sent) > max_sent:
            continue
        added = len(sent) + (1 if cur else 0)
        if cur and cur_chars + added > page_chars:
            pages.append("\n".join(cur))
            cur, cur_chars = [], 0
            added = len(sent)
        cur.append(sent)
        cur_chars += added
    if cur:
        pages.append("\n".join(cur))
    return pages


def write_chatlog(run_dir, key, fold_text, page_chars, sentence_max_ratio):
    """Append a folded session to runs/<run_id>/chatlog.json, re-page the
    accumulated document and return its pages."""
    p = Path(run_dir) / "chatlog.json"
    doc = paths.read_json(p) if p.exists() else {"packages": {}}
    pkg = doc["packages"].setdefault(key, {"sections": [], "pages": []})
    pkg["sections"].append(fold_text)
    pkg["pages"] = paginate("\n".join(pkg["sections"]), page_chars, sentence_max_ratio)
    paths.write_json(p, doc)
    return pkg["pages"]


def build(out_dir, page_chars, sentence_max_ratio):
    """Walk out_dir (skipping _-prefixed names), collect text via
    context_scanner, and return {page_chars, pages, tail_page, catalog, stats}."""
    out_dir = Path(out_dir)
    files = []
    skipped = []
    chunks = []
    for root, dirs, names in os.walk(out_dir):
        dirs[:] = sorted(d for d in dirs if not d.startswith("_"))
        for name in sorted(names):
            if name.startswith("_"):
                continue
            p = Path(root) / name
            rel = p.relative_to(out_dir).as_posix()
            size = p.stat().st_size
            text = context_scanner.file_to_text(p)
            if text is None:
                skipped.append((rel, size))
                continue
            files.append((rel, size, len(text)))
            chunks.append(f"## {rel}\n{text.strip()}")

    corpus = "\n\n".join(chunks)
    pages = paginate(corpus, page_chars, sentence_max_ratio)

    ext_stats = {}
    for rel, _size, _chars in files:
        ext = Path(rel).suffix.lower() or "(none)"
        ext_stats[ext] = ext_stats.get(ext, 0) + 1
    top_ext = sorted(ext_stats.items(), key=lambda kv: -kv[1])[:30]

    tail_lines = [
        f"text files packed: {len(files)} / {len(files) + len(skipped)}",
        f"usable-text bytes: {sum(s for _, s, _ in files)}",
        "extensions: " + (", ".join(f"{e} x{n}" for e, n in top_ext) or "(none)"),
    ]
    if files:
        tail_lines.append("files:")
        tail_lines.extend(f"- {rel} ({size} bytes)" for rel, size, _ in files)
    if skipped:
        tail_lines.append("skipped:")
        tail_lines.extend(f"- {rel} ({size} bytes, not usable as text)" for rel, size in skipped)
    tail = "\n".join(tail_lines)

    catalog = (
        "Catalog: " + ", ".join(f"p{i + 1}={len(t)} chars" for i, t in enumerate(pages))
        if pages
        else "Catalog: no content pages (nothing usable was extracted)."
    )
    pages_out = [{"no": i + 1, "chars": len(t), "text": t} for i, t in enumerate(pages)]
    tail_page = {"no": len(pages_out) + 1, "chars": len(tail), "text": tail}
    stats = {
        "entries": len(files) + len(skipped),
        "text_files": len(files),
        "pages": len(pages_out),
        "chars": sum(p["chars"] for p in pages_out),
    }
    return {
        "page_chars": page_chars,
        "pages": pages_out,
        "tail_page": tail_page,
        "catalog": catalog,
        "stats": stats,
    }
