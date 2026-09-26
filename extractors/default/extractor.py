"""extractors/default — default extractor (pluggable; one self-contained directory per extractor).

CLI contract: an incoming path (single file) + an outgoing directory;
each target gets its own out dir.
Self-contained: depends only on third-party packages in the uv environment,
does not import project modules; holds its own passwords — read from password.json
in this directory ({"passwords": ["...", ...]}).
Scraping policy lives in config.json in this directory (missing file or keys
fall back to the in-code defaults below): archives are sampled, not fully
extracted — only whitelisted text files and notable-named members land in the
out dir, bounded by a per-file cap, a cumulative budget and a file-count cap;
plain files are copied as-is. Member paths are normalized and escaping members
are skipped; listing above entry_limit is truncated; password polling is
capped at max_password_tries tries.
Structure flags (exe_present, macro_docs, nested_archives from the entry list;
multi_part from the input file name) are computed here.
Loaded and executed by scripts/run_extractor.py; the result dict lands in
_result.json via the runner.
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
EXE_EXTS = {".exe", ".com", ".msi", ".bat", ".cmd", ".scr"}
MACRO_DOC_EXTS = {".docm", ".dotm", ".xlsm", ".xlam", ".pptm", ".ppsm"}
SPLIT_VOLUME_RE = re.compile(r"\.(zip|7z)\.\d{1,4}$|\.part\d+\.rar$|\.r\d+$", re.I)

# scraping policy defaults; config.json in this directory overrides these
DEFAULT_CONFIG = {
    "max_single_file_bytes": 256 * 1024,   # members over this are skipped
    "max_total_bytes": 4 * 1024 * 1024,    # cumulative sampling budget
    "max_files": 8,                        # sampled file count cap
    "entry_limit": 30000,                  # listing truncated above this
    "max_password_tries": 32,              # password polling cap per archive
    "text_exts": [".txt", ".md", ".rst", ".nfo", ".json", ".xml", ".csv",
                  ".ini", ".cfg", ".yml", ".yaml", ".log", ".toml", ".text"],
}


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        data = json.loads((HERE / "config.json").read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return cfg
    cfg.update({k: v for k, v in data.items() if k in DEFAULT_CONFIG})
    return cfg


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


# ---------- sampling helpers ----------

def _looks_binary(data: bytes) -> bool:
    if b"\x00" in data:
        return True
    text_bytes = bytes(range(0x20, 0x7F)) + b"\n\r\t"
    sample = data[:1024]
    if not sample:
        return False
    weird = sum(1 for b in sample if b not in text_bytes and b < 0x80)
    return weird / len(sample) > 0.3


def _is_candidate(name: str, text_exts: set[str]) -> bool:
    """Sampling candidate: whitelisted text extension or notable file name."""
    return (os.path.splitext(name)[1].lower() in text_exts
            or bool(NOTABLE_NAME_RE.search(os.path.basename(name))))


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
        "password_protected": password_protected,
        "nested_archives": [n for n in files
                            if os.path.splitext(n)[1].lower() in ARCHIVE_EXTS][:20],
        "exe_present": any(os.path.splitext(n)[1].lower() in EXE_EXTS for n in files),
        "macro_docs": any(os.path.splitext(n)[1].lower() in MACRO_DOC_EXTS for n in files),
        "file_list": files[:20000],
    }


def _list_zip(path: str, log: list[str], entry_limit: int) -> dict:
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        if pyzipper is None:
            raise
        log.append("zipfile failed to open; trying pyzipper (AES zip)")
        zf = pyzipper.AESZipFile(path)
    with zf:
        infos = zf.infolist()
        if len(infos) > entry_limit:
            log.append(f"entry count {len(infos)} exceeds entry_limit {entry_limit}; listing truncated")
            infos = infos[:entry_limit]
        names = [i.filename for i in infos]
        total = sum(i.file_size for i in infos)
        protected = any(i.flag_bits & 0x1 for i in infos)
    return _stats_from_names(names, total, protected)


def _list_7z(path: str, log: list[str], entry_limit: int) -> dict:
    if py7zr is None:
        raise RuntimeError("py7zr not installed; cannot handle 7z")
    with py7zr.SevenZipFile(path) as z:
        names = z.getnames()
        sizes = {e.filename: e.uncompressed for e in z.list()}
        protected = z.needs_password()
    if len(names) > entry_limit:
        log.append(f"entry count {len(names)} exceeds entry_limit {entry_limit}; listing truncated")
        names = names[:entry_limit]
    kept = set(names)
    total = sum(sz for n, sz in sizes.items() if n in kept and not n.endswith("/"))
    return _stats_from_names(names, total, protected)


# ---------- password polling ----------

def poll_zip_password(path: str, passwords: list[str], log: list[str],
                      max_tries: int) -> str | None:
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
        for pw in passwords[:max_tries]:
            try:
                with zf.open(target, pwd=pw.encode("utf-8")) as f:
                    f.read(16)
                log.append(f"password hit (candidate #{passwords.index(pw) + 1})")
                return pw
            except Exception:
                continue
    return None


def poll_7z_password(path: str, passwords: list[str], log: list[str],
                     max_tries: int) -> str | None:
    if py7zr is None:
        return None
    for pw in passwords[:max_tries]:
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
    """Main entry: any path → sampled extracted/<entry_id>/ + result dict.

    Archives: sampled into the out dir — whitelisted text / notable-named
    members only, bounded by the config caps. Plain files: copied as-is.
    Password candidates default to this directory's password.json.
    """
    cfg = load_config()
    text_exts = {e.lower() for e in cfg["text_exts"]}
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
        st["multi_part"] = bool(SPLIT_VOLUME_RE.search(os.path.basename(path)))
        return {"entry_id": sha[:8], "kind": "file", "sha256": sha,
                "structure": st, "password_found": False,
                "warnings": log, "files_kept": 1}

    # archive: listing stats come from the full (entry-limited) listing
    if ext == ".zip":
        st = _list_zip(path, log, cfg["entry_limit"])
    else:
        if py7zr is None:
            raise RuntimeError("py7zr not installed; cannot handle 7z")
        st = _list_7z(path, log, cfg["entry_limit"])
    st["multi_part"] = bool(SPLIT_VOLUME_RE.search(os.path.basename(path)))

    password = None
    if st["password_protected"] and passwords:
        poller = poll_zip_password if ext == ".zip" else poll_7z_password
        password = poller(path, passwords, log, cfg["max_password_tries"])
    if st["password_protected"] and password is None:
        log.append("no working password for encrypted archive; sampling aborted (metadata only)")
        return {"entry_id": sha[:8], "kind": "archive", "sha256": sha,
                "structure": st, "password_found": False,
                "warnings": log, "files_kept": 0}

    out.mkdir(parents=True, exist_ok=True)
    budget = cfg["max_total_bytes"]
    single = cfg["max_single_file_bytes"]
    cap_files = cfg["max_files"]
    kept = 0
    if ext == ".zip":
        cls = pyzipper.AESZipFile if (password and pyzipper) else zipfile.ZipFile
        with cls(path) as zf:
            for info in zf.infolist():
                if kept >= cap_files or budget <= 0:
                    break
                name = info.filename
                if info.is_dir() or not _is_candidate(name, text_exts):
                    continue
                dest = _safe_join(out, name)
                if dest is None:
                    log.append(f"escaped member skipped: {name}")
                    continue
                if info.file_size > single:
                    log.append(f"member over single-file cap, skipped: {name}")
                    continue
                kw = {"pwd": password.encode("utf-8")} if password else {}
                try:
                    with zf.open(info, **kw) as src:
                        data = src.read(single)
                except Exception as e:
                    log.append(f"member read failed, skipped: {name}: {type(e).__name__}")
                    continue
                if _looks_binary(data):
                    log.append(f"binary-looking member skipped: {name}")
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
                budget -= len(data)
                kept += 1
    else:
        with py7zr.SevenZipFile(path, password=password) as z:
            candidates = [n for n in z.getnames()
                          if not n.endswith("/") and _is_candidate(n, text_exts)]
            for n in candidates:
                if kept >= cap_files or budget <= 0:
                    break
                dest = _safe_join(out, n)
                if dest is None or dest == out.resolve():
                    log.append(f"escaped member skipped: {n}")
                    continue
                try:
                    data = z.read(targets=[n])[n].read(single + 1)
                except Exception as e:
                    log.append(f"member read failed, skipped: {n}: {type(e).__name__}")
                    continue
                if len(data) > single:
                    log.append(f"member over single-file cap, skipped: {n}")
                    continue
                if _looks_binary(data):
                    log.append(f"binary-looking member skipped: {n}")
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
                budget -= len(data)
                kept += 1

    return {"entry_id": sha[:8], "kind": "archive", "sha256": sha,
            "structure": st, "password_found": password is not None,
            "warnings": log, "files_kept": kept}
