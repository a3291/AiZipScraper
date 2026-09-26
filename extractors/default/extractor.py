"""Default extractor: unpack archives with sampling caps, or copy plain files
and folders.

Self-contained: no project imports; reads its own config.json/password.json
from this directory. extract(in_path, out_dir) -> {"files_kept", "warnings"}.
"""
import fnmatch
import json
import shutil
import zipfile
from pathlib import Path, PurePosixPath

HERE = Path(__file__).resolve().parent
ARCHIVE_EXTS = {".zip", ".7z"}
DEFAULTS = {
    "caps": {
        "per_file_bytes": 262144,
        "total_bytes": 4194304,
        "file_count": 8,
        "list_entries": 30000,
    },
    "text_exts": [
        ".txt", ".md", ".csv", ".json", ".xml", ".html", ".htm", ".ini",
        ".log", ".py", ".js", ".srt", ".nfo", ".url", ".cfg", ".yaml", ".yml",
    ],
    "notable": [
        "readme*", "*.exe", "*.msi", "*.dll", "*.bat", "*.cmd", "*.ps1",
        "*.url", "*.lnk", "password*", "auth*", "*.key", "license*",
    ],
}


def _load_cfg():
    p = HERE / "config.json"
    cfg = json.loads(p.read_text("utf-8")) if p.is_file() else {}
    merged = json.loads(json.dumps(DEFAULTS))
    for section, values in cfg.items():
        if isinstance(values, dict):
            merged.setdefault(section, {}).update(values)
    return merged


def _load_passwords():
    p = HERE / "password.json"
    if not p.is_file():
        return []
    data = json.loads(p.read_text("utf-8"))
    return [str(x) for x in data.get("passwords", [])]


def _safe_rel(name):
    p = PurePosixPath(name.replace("\\", "/"))
    if p.is_absolute() or ".." in p.parts or (len(name) > 1 and name[1] == ":"):
        return None
    parts = [x for x in p.parts if x not in ("/",)]
    return "/".join(parts) if parts else None


def _select(entries, cfg, warnings):
    """entries: (name, size, is_dir) tuples -> [(size, rel, name)]."""
    caps = cfg["caps"]
    tset = {e.lower() for e in cfg["text_exts"]}
    patterns = cfg["notable"]
    out = []
    for name, size, is_dir in entries:
        if len(out) >= caps["file_count"]:
            warnings.append("file count cap reached; remaining members skipped")
            break
        rel = _safe_rel(name)
        if rel is None:
            warnings.append(f"skipped unsafe member: {name}")
            continue
        base = rel.rsplit("/", 1)[-1].lower()
        if Path(rel).suffix.lower() not in tset and not any(
            fnmatch.fnmatch(base, pat) for pat in patterns
        ):
            continue
        if size > caps["per_file_bytes"]:
            warnings.append(f"skipped {rel}: {size} bytes over per-file cap")
            continue
        out.append((size, rel, name))
    return out


def _write_member(data, out_dir, rel):
    dest = out_dir / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)


def _extract_dir(src, out_dir, cfg, warnings):
    caps = cfg["caps"]
    tset = {e.lower() for e in cfg["text_exts"]}
    kept = 0
    total = 0
    for p in sorted(src.rglob("*")):
        if not p.is_file():
            continue
        if p.suffix.lower() not in tset:
            continue
        if kept >= caps["file_count"]:
            warnings.append("file count cap reached; remaining files skipped")
            break
        rel = p.relative_to(src).as_posix()
        size = p.stat().st_size
        if size > caps["per_file_bytes"]:
            warnings.append(f"skipped {rel}: {size} bytes over per-file cap")
            continue
        if total + size > caps["total_bytes"]:
            warnings.append("total size cap reached; remaining files skipped")
            break
        dest = out_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(p, dest)
        kept += 1
        total += size
    return {"files_kept": kept, "warnings": warnings}


def _copy_file(src, out_dir, warnings):
    dest = out_dir / src.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)
    return {"files_kept": 1, "warnings": warnings}


def _zip_password(zf, infos, passwords, warnings):
    for info in sorted(infos, key=lambda i: i.file_size):
        if info.is_dir() or info.file_size == 0:
            continue
        for cand in passwords:
            try:
                zf.read(info, pwd=cand.encode("utf-8"))
                return cand.encode("utf-8")
            except Exception:
                continue
        break
    warnings.append("password not found; members not extracted")
    return None


def _open_zip(src):
    """Prefer pyzipper (AES support); fall back to stdlib zipfile."""
    try:
        import pyzipper
        return pyzipper.AESZipFile(src)
    except ImportError:
        return zipfile.ZipFile(src)


def _extract_zip(src, out_dir, cfg, passwords, warnings):
    caps = cfg["caps"]
    with _open_zip(src) as zf:
        infos = zf.infolist()
        if len(infos) > caps["list_entries"]:
            warnings.append(f"listing truncated at {caps['list_entries']} entries")
            infos = infos[:caps["list_entries"]]
        if any(i.flag_bits & 0x1 for i in infos):
            pwd = _zip_password(zf, infos, passwords, warnings)
            if pwd is None:
                return {"files_kept": 0, "warnings": warnings}
        else:
            pwd = None
        entries = [(i.filename, i.file_size, i.is_dir()) for i in infos]
        selected = _select(entries, cfg, warnings)
        kept = 0
        total = 0
        for size, rel, member in selected:
            if kept >= caps["file_count"] or total + size > caps["total_bytes"]:
                warnings.append("caps reached; remaining members skipped")
                break
            data = zf.read(member, pwd=pwd)
            _write_member(data, out_dir, rel)
            kept += 1
            total += size
    return {"files_kept": kept, "warnings": warnings}


def _7z_password(src, py7zr, probe_name, passwords, warnings):
    try:
        with py7zr.SevenZipFile(src, mode="r") as z:
            z.read(targets=[probe_name])
        return None
    except Exception:
        pass
    for cand in passwords:
        try:
            with py7zr.SevenZipFile(src, mode="r", password=cand) as z:
                z.read(targets=[probe_name])
            return cand
        except Exception:
            continue
    warnings.append("password not found; members not extracted")
    return False


def _extract_7z(src, out_dir, cfg, passwords, warnings):
    try:
        import py7zr
    except ImportError:
        warnings.append("py7zr missing; 7z not extracted")
        return {"files_kept": 0, "warnings": warnings}
    caps = cfg["caps"]
    try:
        with py7zr.SevenZipFile(src, mode="r") as z:
            infos = z.list()
    except Exception as exc:
        warnings.append(f"listing failed: {exc}")
        return {"files_kept": 0, "warnings": warnings}
    if len(infos) > caps["list_entries"]:
        warnings.append(f"listing truncated at {caps['list_entries']} entries")
        infos = infos[:caps["list_entries"]]
    entries = [(i.filename, i.uncompressed, i.is_directory) for i in infos]
    selected = _select(entries, cfg, warnings)
    if not selected:
        return {"files_kept": 0, "warnings": warnings}
    names = [member for _, _, member in selected]
    pwd = _7z_password(src, py7zr, names[0], passwords, warnings)
    if pwd is False:
        return {"files_kept": 0, "warnings": warnings}
    try:
        with py7zr.SevenZipFile(src, mode="r", password=pwd) as z:
            data_map = z.read(targets=names)
    except Exception as exc:
        warnings.append(f"7z read failed: {exc}")
        return {"files_kept": 0, "warnings": warnings}
    kept = 0
    total = 0
    for size, rel, member in selected:
        if kept >= caps["file_count"] or total + size > caps["total_bytes"]:
            warnings.append("caps reached; remaining members skipped")
            break
        bio = data_map.get(member)
        if bio is None:
            continue
        _write_member(bio.read(), out_dir, rel)
        kept += 1
        total += size
    return {"files_kept": kept, "warnings": warnings}


def extract(in_path, out_dir):
    src = Path(in_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cfg = _load_cfg()
    passwords = _load_passwords()
    warnings = []
    if src.is_dir():
        return _extract_dir(src, out_dir, cfg, warnings)
    if src.suffix.lower() in ARCHIVE_EXTS:
        return _extract_zip(src, out_dir, cfg, passwords, warnings)
    return _copy_file(src, out_dir, warnings)
