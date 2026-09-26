"""Uniform file recognition: file -> text. One function, one job."""
from pathlib import Path

TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv", ".json",
    ".yaml", ".yml", ".xml", ".html", ".htm", ".ini", ".cfg", ".conf",
    ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".c", ".h", ".cpp",
    ".hpp", ".cs", ".go", ".rs", ".rb", ".php", ".sh", ".bat", ".ps1",
    ".sql", ".tex", ".srt", ".ass", ".sub", ".vtt", ".nfo", ".info",
}


_PRINTABLE_RATIO = 0.9


def looks_binary(head):
    if not head:
        return False
    if b"\x00" in head:
        return True
    printable = sum(
        b in (9, 10, 13) or 32 <= b < 127 or b >= 128 for b in head
    )
    return printable / len(head) < _PRINTABLE_RATIO


def file_to_text(path, sniff_bytes):
    """Return the decoded text of path, or None when the file is not usable
    text (unknown extension, empty, binary, undecodable)."""
    p = Path(path)
    if not p.is_file():
        return None
    if p.suffix.lower() not in TEXT_EXTS:
        return None
    if p.stat().st_size == 0:
        return None
    try:
        with p.open("rb") as f:
            head = f.read(sniff_bytes)
        if looks_binary(head):
            return None
        return p.read_text("utf-8", errors="replace")
    except OSError:
        return None
