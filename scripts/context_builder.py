"""Packs extracted files into numbered pages with a trailing metadata page.

Page boundaries align to sentence ends; a sentence longer than
page_chars * sentence_max_ratio is dropped whole; pages never split a sentence.
File recognition is delegated to context_scanner.
"""
import os
from pathlib import Path

import context_scanner


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


def build(out_dir, page_chars, sentence_max_ratio, max_text_bytes):
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
            text = context_scanner.file_to_text(p, max_text_bytes)
            if text is None:
                skipped.append((rel, size))
                continue
            files.append((rel, size, len(text)))
            chunks.append(f"## {rel}\n{text.strip()}")

    corpus = "\n\n".join(chunks)
    max_sent = max(1, int(page_chars * sentence_max_ratio))
    pages, cur, cur_chars = [], [], 0
    for sent in _split_sentences(corpus):
        if len(sent) > max_sent:
            continue
        if cur and cur_chars + len(sent) > page_chars:
            pages.append("\n".join(cur))
            cur, cur_chars = [], 0
        cur.append(sent)
        cur_chars += len(sent)
    if cur:
        pages.append("\n".join(cur))

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
