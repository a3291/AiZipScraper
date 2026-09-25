"""context_builder.py — 环3 打包：只读 extracted/ 目录 → context.json。

与环2解耦：不看 extractor 的结果 dict，只认目录里的文件。
分页规则：句子切分、页界软化（对齐句末）、单句超 page_chars*10% 整句跳过；
files_index 记录每文件起始索引与页号；tail_page 元数据页放末尾。
"""
from __future__ import annotations

import os
from pathlib import Path

TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".rst", ".csv", ".tsv", ".json", ".jsonl",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".xml", ".html",
    ".htm", ".css", ".js", ".ts", ".jsx", ".tsx", ".py", ".pyw", ".java",
    ".kt", ".go", ".rs", ".c", ".h", ".cpp", ".hpp", ".cs", ".rb", ".php",
    ".sh", ".bat", ".ps1", ".sql", ".lua", ".r", ".swift", ".m", ".pl",
    ".log", ".srt", ".ass", ".vtt", ".svg", ".gitignore", ".env", ".list",
}
MAX_TEXT_FILE_BYTES = 32 * 1024 * 1024   # 单文本文件读取上限（脚本内定死）
SENTENCE_RE = None  # 延迟编译


def _looks_binary(data: bytes) -> bool:
    if b"\x00" in data[:4096]:
        return True
    if not data:
        return False
    sample = data[:4096]
    printable = sum(b in (9, 10, 13) or 32 <= b < 127 or b >= 128 for b in sample)
    return printable / len(sample) < 0.85


def _split_sentences(text: str) -> list[str]:
    """按句界符切分，保留分隔符在句尾。"""
    import re
    parts = re.split(r"(?<=[。！？!?.；;\n])", text)
    return [p for p in parts if p.strip()]


def read_text_blocks(extracted_dir: str | Path) -> list[dict]:
    """遍历 extracted/，产出文本块。返回 [{file, text, size}]（binary 只登记）。"""
    base = Path(extracted_dir)
    blocks: list[dict] = []
    files = sorted(p for p in base.rglob("*")
                   if p.is_file() and not p.name.startswith("_"))
    for p in files:
        rel = p.relative_to(base).as_posix()
        size = p.stat().st_size
        ext = p.suffix.lower()
        if ext in TEXT_EXTS and size <= MAX_TEXT_FILE_BYTES:
            try:
                data = p.read_bytes()
                if not _looks_binary(data):
                    text = data.decode("utf-8", errors="replace")
                    blocks.append({"file": rel, "text": text, "size": size})
                    continue
            except OSError:
                pass
        blocks.append({"file": rel, "text": "", "size": size})  # binary/大文件仅登记
    return blocks


def _tail_page(blocks: list[dict]) -> str:
    """末尾元数据页：文件索引 + 结构统计（只由 extracted/ 目录算出）。"""
    total = len(blocks)
    total_bytes = sum(b["size"] for b in blocks)
    exts: dict[str, int] = {}
    for b in blocks:
        e = os.path.splitext(b["file"])[1].lower() or "(无)"
        exts[e] = exts.get(e, 0) + 1
    top_exts = dict(sorted(exts.items(), key=lambda kv: -kv[1])[:20])
    lines = [f"【元数据页】文件数: {total}, 总字节: {total_bytes}",
             f"扩展名统计: {top_exts}",
             "文件索引（起始索引=内容字符偏移；binary 文件无正文）:"]
    for b in blocks:
        kind = f"{b['size']}B" if not b["text"] else f"{len(b['text'])}字"
        lines.append(f"  - {b['file']} ({kind})")
    return "\n".join(lines)


def build(extracted_dir: str | Path, page_chars: int = 3000) -> dict:
    """环3 主入口：extracted/ → context dict（供序列化为 context.json）。"""
    blocks = read_text_blocks(extracted_dir)
    tail = _tail_page(blocks)

    max_sent = page_chars // 10   # 单句上限 10%
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
        """字符起始索引 = 之前所有页长度(含页间分隔符) + 页内偏移。"""
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

    # 目录页
    cat = [f"目录页：共 {len(page_texts)} 个内容页（末尾页为元数据页，已默认展示）。"]
    for i, pg in enumerate(page_texts, 1):
        first = next((ln for ln in pg.splitlines() if ln.strip()), "(空)")
        cat.append(f"  第{i}页: {first[:80]}")
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
