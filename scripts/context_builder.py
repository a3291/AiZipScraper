"""context_builder.py — packer: reads only the extracted/ directory → context.json.

Paging rules: sentence splitting, page-boundary softening (aligned to sentence
ends), sentences longer than page_chars*sentence_max_ratio skipped whole;
files_index records each file's start index and page; the tail_page metadata
page goes last.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".rst", ".csv", ".tsv", ".json", ".jsonl",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".xml", ".html",
    ".htm", ".css", ".js", ".ts", ".jsx", ".tsx", ".py", ".pyw", ".java",
    ".kt", ".go", ".rs", ".c", ".h", ".cpp", ".hpp", ".cs", ".rb", ".php",
    ".sh", ".bat", ".ps1", ".sql", ".lua", ".r", ".swift", ".m", ".pl",
    ".log", ".srt", ".ass", ".vtt", ".svg", ".gitignore", ".env", ".list",
}


def _looks_binary(data: bytes) -> bool:
    if b"\x00" in data[:4096]:
        return True
    if not data:
        return False
    sample = data[:4096]
    printable = sum(b in (9, 10, 13) or 32 <= b < 127 or b >= 128 for b in sample)
    return printable / len(sample) < 0.85


def _split_sentences(text: str) -> list[str]:
    """Split on sentence delimiters, keeping the delimiter at the sentence end."""
    return [p for p in re.split(r"(?<=[。！？!?.；;\n])", text) if p.strip()]


def read_text_blocks(extracted_dir: str | Path,
                     max_text_file_bytes: int) -> list[dict]:
    """Walk extracted/ and produce text blocks. Returns [{file, text, size}] (binary registered only)."""
    base = Path(extracted_dir)
    blocks: list[dict] = []
    files = sorted(p for p in base.rglob("*")
                   if p.is_file() and not p.name.startswith("_"))
    for p in files:
        rel = p.relative_to(base).as_posix()
        size = p.stat().st_size
        ext = p.suffix.lower()
        if ext in TEXT_EXTS and size <= max_text_file_bytes:
            try:
                data = p.read_bytes()
                if not _looks_binary(data):
                    text = data.decode("utf-8", errors="replace")
                    blocks.append({"file": rel, "text": text, "size": size})
                    continue
            except OSError:
                pass
        blocks.append({"file": rel, "text": "", "size": size})  # binary/oversize: register only
    return blocks


def _tail_page(blocks: list[dict]) -> str:
    """Trailing metadata page: file index + structure stats (derived solely from the extracted/ directory)."""
    total = len(blocks)
    total_bytes = sum(b["size"] for b in blocks)
    exts: dict[str, int] = {}
    for b in blocks:
        e = os.path.splitext(b["file"])[1].lower() or "(none)"
        exts[e] = exts.get(e, 0) + 1
    top_exts = dict(sorted(exts.items(), key=lambda kv: -kv[1])[:20])
    lines = [f"[Metadata page] files: {total}, total bytes: {total_bytes}",
             f"Extension stats: {top_exts}",
             "File index (start index = character offset into content; binary files have no text):"]
    for b in blocks:
        kind = f"{b['size']}B" if not b["text"] else f"{len(b['text'])} chars"
        lines.append(f"  - {b['file']} ({kind})")
    return "\n".join(lines)


def build(extracted_dir: str | Path, page_chars: int,
          max_text_file_bytes: int = 33554432,
          sentence_max_ratio: float = 0.1) -> dict:
    """Main entry: extracted/ → context dict (for serialization into context.json).

    Return shape: {version, page_chars, pages: [{no, chars, text}],
    tail_page: {no, chars, text}, files_index, stats: {sentences_skipped,
    page_count, file_count}} — the in-memory handover to the session engine.
    """
    blocks = read_text_blocks(extracted_dir, max_text_file_bytes)
    tail = _tail_page(blocks)

    max_sent = int(page_chars * sentence_max_ratio)   # per-sentence cap
    page_texts: list[str] = []
    cur: list[str] = []
    cur_chars = 0
    sentences_skipped = 0
    file_pos: dict[str, dict] = {}   # file -> {page, start}

    def flush():
        nonlocal cur, cur_chars
        if cur:
            page_texts.append("\n".join(cur))
            cur, cur_chars = [], 0

    def _cursor(page_no: int, offset_in_page: int) -> int:
        """Start index = lengths of all previous pages (incl. page separators) + offset within the page."""
        return sum(len(t) + 1 for t in page_texts[:page_no - 1]) + offset_in_page

    for b in blocks:
        if not b["text"]:
            file_pos[b["file"]] = {"page": None, "start": None, "binary": True}
            continue
        offset_in_page = cur_chars
        page_before = len(page_texts) + 1
        for sent in _split_sentences(b["text"]):
            if len(sent) > max_sent:
                sentences_skipped += 1
                continue
            if cur_chars + len(sent) > page_chars and cur:
                flush()
                offset_in_page = 0
                page_before = len(page_texts) + 1
            if b["file"] not in file_pos:
                file_pos[b["file"]] = {"page": page_before,
                                       "start": _cursor(page_before, offset_in_page)}
            cur.append(sent)
            cur_chars += len(sent)
    flush()

    files_index = [{"file": b["file"], **file_pos[b["file"]],
                    **({} if b["text"] else {"size": b["size"]})}
                   for b in blocks]

    # catalog page
    cat = [f"Catalog: {len(page_texts)} content pages (the last page is the metadata page, shown by default)."]
    for i, pg in enumerate(page_texts, 1):
        first = next((ln for ln in pg.splitlines() if ln.strip()), "(empty)")
        cat.append(f"  Page {i}: {first[:80]}")
    cat.append(tail)

    return {
        "version": "2.0",
        "page_chars": page_chars,
        "pages": [{"no": i + 1, "chars": len(p), "text": p}
                  for i, p in enumerate(page_texts)],
        "tail_page": {"no": len(page_texts) + 1, "chars": len(tail), "text": tail},
        "files_index": files_index,
        "stats": {"sentences_skipped": sentences_skipped,
                  "page_count": len(page_texts), "file_count": len(blocks)},
    }
