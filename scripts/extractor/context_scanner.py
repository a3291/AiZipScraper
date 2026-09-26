"""Uniform file recognition: file -> text. One function, one job."""
from pathlib import Path

TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv", ".json",
    ".yaml", ".yml", ".xml", ".html", ".htm", ".ini", ".cfg", ".conf",
    ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".c", ".h", ".cpp",
    ".hpp", ".cs", ".go", ".rs", ".rb", ".php", ".sh", ".bat", ".ps1",
    ".sql", ".tex", ".srt", ".ass", ".sub", ".vtt", ".nfo", ".info",
}
_SNIFF_BYTES = 8192


def looks_binary(head):
    if not head:
        return False
    if b"\x00" in head:
        return True
    sample = head[:4096]
    if not sample:
        return False
    printable = sum(
        b in (9, 10, 13) or 32 <= b < 127 or b >= 128 for b in sample
    )
    return printable / len(sample) < 0.9


def file_to_text(path):
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
            head = f.read(_SNIFF_BYTES)
        if looks_binary(head):
            return None
        return p.read_text("utf-8", errors="replace")
    except OSError:
        return None
