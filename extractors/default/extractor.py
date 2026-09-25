"""extractors/default — default extractor (pluggable; one self-contained directory per extractor).

CLI contract: an incoming path (single file) + an outgoing directory;
concurrency-safe (each target gets its own out dir).
Self-contained: depends only on third-party packages in the uv environment,
never imports project modules; holds its own passwords — read from password.json
in this directory ({"passwords": ["...", ...]}).
Behavior is fixed in this script (not wired to config): archives (zip/7z) are
fully extracted with original files preserved; plain files are copied as-is;
sandboxed against path escape; zip-bomb guardrails (total size / entry caps).
Loaded and executed by scripts/run_extractor.py; the result dict lands in
_result.json via the runner.
The downstream context packer reads only the out dir, decoupled from this dict.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import zipfile
from pathlib import Path

try:
    import pyzipper
except ImportError:
    pyzipper = None
try:
    import py7zr
except ImportError:
    py7zr = None

HERE = Path(__file__).resolve().parent
ARCHIVE_EXTS = {".zip", ".7z"}
NOTABLE_NAME_RE = re.compile(r"(readme|说明|index|manifest|license|changelog)", re.I)

# guardrails fixed in-script (not wired to config)
MAX_TOTAL_UNCOMPRESSED = 4 * 1024 ** 3   # total uncompressed cap: 4GB
MAX_ENTRIES = 50000                      # entry count cap


# ---------- self-contained hashing (no external imports) ----------

def sha256_file(path, buf: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(buf):
            h.update(chunk)
    return h.hexdigest()


def entry_id_for(path) -> str:
    return sha256_file(path)[:8]


# ---------- self-held passwords ----------

def load_passwords() -> list[str]:
    pw_path = HERE / "password.json"
    try:
        data = json.loads(pw_path.read_text(encoding="utf-8-sig"))
        return [str(p) for p in data.get("passwords", [])]
    except (OSError, json.JSONDecodeError):
        return []


def _safe_join(base: Path, name: str) -> Path | None:
    """Normalize a member path; returns None if it escapes the base."""
    norm = name.replace("\\", "/").lstrip("/")
    if any(p == ".." for p in norm.split("/")) or ":" in norm:
        return None
    p = (base / norm).resolve()
    return p if str(p).startswith(str(base.resolve())) else None


# ---------- listing stats ----------

def _stats_from_names(names: list[str], total_uncompressed: int,
                      password_protected: bool) -> dict:
    exts: dict[str, int] = {}
    top: set[str] = set()
    files = []
    for n in names:
        if n.endswith("/"):
            top.add(n.split("/")[0] + "/")
            continue
        files.append(n)
        top.add(n.split("/")[0] + ("/" if "/" in n else ""))
        e = os.path.splitext(n)[1].lower()
        exts[e] = exts.get(e, 0) + 1
    top_exts = dict(sorted(exts.items(), key=lambda kv: -kv[1])[:30])
    return {
        "entry_count": len(names),
        "dir_count": sum(1 for n in names if n.endswith("/")),
        "total_uncompressed": total_uncompressed,
        "top_extensions": top_exts,
        "top_level_dirs": sorted(top)[:50],
        "notable_files": [n for n in files
                          if NOTABLE_NAME_RE.search(os.path.basename(n))][:20],
        "file_list": files[:20000],
        "password_protected": password_protected,
    }


def _list_zip(path: str, log: list[str]) -> dict:
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        if pyzipper is None:
            raise
        log.append("zipfile failed to open; trying pyzipper (AES zip)")
        zf = pyzipper.AESZipFile(path)
    with zf:
        infos = zf.infolist()
        names = [i.filename for i in infos]
        total = sum(i.file_size for i in infos)
        protected = any(i.flag_bits & 0x1 for i in infos)
    return _stats_from_names(names, total, protected)


def _list_7z(path: str, log: list[str]) -> dict:
    if py7zr is None:
        raise RuntimeError("py7zr not installed; cannot handle 7z")
    with py7zr.SevenZipFile(path) as z:
        names = z.getnames()
        total = sum(e.uncompressed for e in z.list() if not e.is_directory)
        protected = z.needs_password()
    return _stats_from_names(names, total, protected)


# ---------- password polling ----------

def poll_zip_password(path: str, passwords: list[str], log: list[str]) -> str | None:
    if pyzipper is None:
        return None
    try:
        zf = pyzipper.AESZipFile(path)
    except Exception:
        return None
    with zf:
        names = [i.filename for i in zf.infolist() if i.flag_bits & 0x1]
        if not names:
            return None
        target = names[0]
        for pw in passwords:
            try:
                with zf.open(target, pwd=pw.encode("utf-8")) as f:
                    f.read(16)
                log.append(f"password hit (candidate #{passwords.index(pw) + 1})")
                return pw
            except Exception:
                continue
    return None


def poll_7z_password(path: str, passwords: list[str], log: list[str]) -> str | None:
    if py7zr is None:
        return None
    for pw in passwords:
        try:
            with py7zr.SevenZipFile(path, password=pw) as z:
                z.read(targets=[z.getnames()[0]])
            log.append(f"password hit (candidate #{passwords.index(pw) + 1})")
            return pw
        except Exception:
            continue
    return None


# ---------- main entry ----------

def extract(path: str, out_dir: str | Path,
            passwords: list[str] | None = None) -> dict:
    """Main entry: any path → extracted/<entry_id>/ + result dict.

    Archives: fully extracted (original files preserved); plain files: copied as-is.
    Password candidates default to this directory's password.json.
    """
    log: list[str] = []
    passwords = passwords if passwords is not None else load_passwords()
    out = Path(out_dir)
    ext = os.path.splitext(path)[1].lower()
    sha = sha256_file(path)

    if ext not in ARCHIVE_EXTS:
        # plain file: copy as-is
        out.mkdir(parents=True, exist_ok=True)
        dest = out / os.path.basename(path)
        if Path(path).resolve() != dest.resolve():
            shutil.copy2(path, dest)
        st = _stats_from_names([os.path.basename(path)],
                               os.path.getsize(path), False)
        return {"entry_id": sha[:8], "kind": "file", "sha256": sha,
                "structure": st, "password_found": False,
                "warnings": log, "files_kept": 1}

    # archive
    if ext == ".zip":
        st = _list_zip(path, log)
    else:
        if py7zr is None:
            raise RuntimeError("py7zr not installed; cannot handle 7z")
        st = _list_7z(path, log)

    if st["entry_count"] > MAX_ENTRIES:
        raise ValueError(f"entry count {st['entry_count']} exceeds cap {MAX_ENTRIES}; refusing to extract")
    if st["total_uncompressed"] > MAX_TOTAL_UNCOMPRESSED:
        raise ValueError("total uncompressed size exceeds cap; refusing to extract")

    password = None
    if st["password_protected"] and passwords:
        poller = poll_zip_password if ext == ".zip" else poll_7z_password
        password = poller(path, passwords, log)
    if st["password_protected"] and password is None:
        log.append("no working password for encrypted archive; extraction aborted (metadata only)")
        return {"entry_id": sha[:8], "kind": "archive", "sha256": sha,
                "structure": st, "password_found": False,
                "warnings": log, "files_kept": 0}

    out.mkdir(parents=True, exist_ok=True)
    kept = 0
    if ext == ".zip":
        cls = pyzipper.AESZipFile if (password and pyzipper) else zipfile.ZipFile
        with cls(path) as zf:
            for info in zf.infolist():
                dest = _safe_join(out, info.filename)
                if dest is None:
                    log.append(f"escaped member skipped: {info.filename}")
                    continue
                if info.is_dir():
                    dest.mkdir(parents=True, exist_ok=True)
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                kw = {"pwd": password.encode("utf-8")} if password else {}
                with zf.open(info, **kw) as src, open(dest, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                kept += 1
    else:
        with py7zr.SevenZipFile(path, password=password) as z:
            names = z.getnames()
            # member-level sanitization: escaping/bad names (".", "..", absolute paths) dropped
            valid = [n for n in names if _safe_join(out, n) is not None
                     and _safe_join(out, n) != out.resolve()]
            skipped = [n for n in names if n not in valid]
            for n in skipped:
                log.append(f"escaped member skipped: {n}")
            try:
                z.extract(path=out, targets=valid)
                kept = len(valid)
            except Exception as e:
                # bulk blocked by a single bad member: fall back to per-member extraction
                if len(valid) > 1 and type(e).__name__ == "Bad7zFile":
                    kept = 0
                    for n in valid:
                        try:
                            z.extract(path=out, targets=[n])
                            kept += 1
                        except Exception:
                            log.append(f"member extraction failed, skipped: {n}")
                else:
                    raise

    return {"entry_id": sha[:8], "kind": "archive", "sha256": sha,
            "structure": st, "password_found": password is not None,
            "warnings": log, "files_kept": kept}
