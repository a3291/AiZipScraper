"""extractor.py — 信号采集提取器（两档深度 + 密码轮询）。

输入：压缩包路径
输出：中间 JSON（structure 信号 + sample_evidence + flags + 安全日志）
与 AI 层、侧车写入完全解耦。

安全约束（定死，不可配置）：
- 单文件解压上限 256KB，累计 4MB，条目 3 万
- 成员路径归一化校验防穿越；不执行内容、不解析宏、不看图片
- 抽样仅限白名单文本类文件
"""
from __future__ import annotations

import hashlib
import io
import os
import re
import stat
import tempfile
import zipfile
from datetime import datetime
from pathlib import PurePosixPath, Path

from py7zr import SevenZipFile, exceptions as sz_exceptions

SINGLE_FILE_LIMIT = 256 * 1024        # 256KB
TOTAL_LIMIT = 4 * 1024 * 1024         # 4MB
ENTRY_LIMIT = 30000                   # 3 万条目
EXCERPT_CHARS = 200                   # 每文件摘录字符数
MAX_PASSWORD_TRIES = 32               # 单包密码轮询上限

TEXT_EXTS = {
    ".txt", ".md", ".rst", ".nfo", ".json", ".xml", ".csv",
    ".ini", ".cfg", ".yml", ".yaml", ".log", ".toml", ".text",
}
NOTABLE_NAME_RE = re.compile(
    r"readme|说明|info|version|license|changelog|使用|帮助", re.IGNORECASE
)
ARCHIVE_EXTS = {".zip", ".7z", ".rar", ".gz", ".tar", ".bz2", ".xz"}
EXE_EXTS = {".exe", ".dll", ".bat", ".cmd", ".msi", ".scr", ".com", ".ps1"}
MACRO_EXTS = {".docm", ".xlsm", ".pptm", ".xlam", ".doc", ".xls"}

try:
    import pyzipper  # zip AES 支持，可选依赖
except ImportError:
    pyzipper = None


# ---------- 公共小工具 ----------

def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def is_encrypted_zip_member(info: zipfile.ZipInfo) -> bool:
    return bool(info.flag_bits & 0x1)


def _safe_member_name(name: str) -> bool:
    """成员名归一化校验：拒绝绝对路径与向上穿越。"""
    p = PurePosixPath(name.replace("\\", "/"))
    if p.is_absolute() or (len(name) > 1 and name[1] == ":"):
        return False
    return ".." not in p.parts


def _decode_bytes(data: bytes) -> str:
    for enc in ("utf-8", "gbk"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _looks_binary(data: bytes) -> bool:
    if b"\x00" in data:
        return True
    text_bytes = bytes(range(0x20, 0x7F)) + b"\n\r\t"
    sample = data[:1024]
    if not sample:
        return False
    weird = sum(1 for b in sample if b not in text_bytes and b < 0x80)
    return weird / len(sample) > 0.3


# ---------- 后端抽象 ----------

class Listing:
    """归一化的清单信号。"""

    def __init__(self) -> None:
        self.files: list[dict] = []      # {name,size}
        self.dir_count = 0
        self.total_uncompressed = 0
        self.entry_count = 0
        self.password_protected = False
        self.aes_zip = False
        self.compression = ""

    def extensions(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for f in self.files:
            ext = os.path.splitext(f["name"])[1].lower()
            counts[ext or "(无扩展名)"] = counts.get(ext or "(无扩展名)", 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: -kv[1])[:20])

    def top_level_dirs(self) -> list[str]:
        seen: list[str] = []
        for f in self.files:
            parts = PurePosixPath(f["name"].replace("\\", "/")).parts
            if len(parts) > 1 and parts[0] not in seen:
                seen.append(parts[0])
        return seen[:20]

    def flags_scan(self) -> dict:
        nested, exe, macro = [], False, False
        for f in self.files:
            ext = os.path.splitext(f["name"])[1].lower()
            if ext in ARCHIVE_EXTS:
                nested.append(f["name"])
            elif ext in EXE_EXTS:
                exe = True
            elif ext in MACRO_EXTS:
                macro = True
        return {"nested_archives": nested[:20], "exe_present": exe, "macro_docs": macro}


def list_zip(path: str, log: list[str]) -> Listing:
    """优先 zipfile，AES 加密 zip 退化用 pyzipper。"""
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        if pyzipper is None:
            raise
        log.append("zipfile 打不开，尝试 pyzipper（AES zip）")
        zf = pyzipper.AESZipFile(path)
        return _read_zip_listing(zf, log)
    try:
        return _read_zip_listing(zf, log)
    except (zipfile.BadZipFile, NotImplementedError):
        if pyzipper is None:
            log.append("检测到 AES zip 但未安装 pyzipper")
            raise
        zf.close()
        log.append("改用 pyzipper 打开（AES zip）")
        return _read_zip_listing(pyzipper.AESZipFile(path), log)


def _read_zip_listing(zf, log: list[str]) -> Listing:
    ls = Listing()
    ls.compression = "zip"
    infos = zf.infolist()
    if len(infos) > ENTRY_LIMIT:
        log.append(f"条目数 {len(infos)} 超上限 {ENTRY_LIMIT}，截断")
        infos = infos[:ENTRY_LIMIT]
    for info in infos:
        if info.is_dir():
            ls.dir_count += 1
            continue
        if not _safe_member_name(info.filename):
            log.append(f"拒绝危险成员名: {info.filename!r}")
            continue
        if is_encrypted_zip_member(info):
            ls.password_protected = True
            if info.compress_type == 99:
                ls.aes_zip = True
        ls.files.append({"name": info.filename, "size": info.file_size})
        ls.total_uncompressed += info.file_size
    ls.entry_count = len(ls.files)
    return ls


def list_7z(path: str, log: list[str]) -> Listing:
    ls = Listing()
    ls.compression = "7z"
    with SevenZipFile(path, mode="r") as sz:
        if sz.needs_password():
            ls.password_protected = True
        ls.files = [
            {"name": f.filename, "size": f.uncompressed}
            for f in sz.list()
            if not f.is_directory
        ][:ENTRY_LIMIT]
        ls.dir_count = sum(1 for f in sz.list() if f.is_directory)
    ls.entry_count = len(ls.files)
    ls.total_uncompressed = sum(f["size"] for f in ls.files)
    return ls


# ---------- 密码轮询 ----------

def poll_zip_password(path: str, candidates: list[str], log: list[str]) -> str | None:
    """zip 密码轮询：统一用 pyzipper（兼容普通 zip / ZipCrypto / AES）。

    选最小的加密文本成员做试读，成功返回密码。
    """
    if pyzipper is not None:
        zf = pyzipper.AESZipFile(path)
    else:
        zf = zipfile.ZipFile(path)
    encrypted = [i for i in zf.infolist()
                 if is_encrypted_zip_member(i) and not i.is_dir()]
    if not encrypted:
        return None
    encrypted.sort(key=lambda i: i.file_size)
    target = next((i for i in encrypted
                   if os.path.splitext(i.filename)[1].lower() in TEXT_EXTS),
                  encrypted[0])
    for idx, pw in enumerate(candidates[:MAX_PASSWORD_TRIES]):
        try:
            with zf.open(target, pwd=pw.encode("utf-8")) as fh:
                fh.read(64)
            log.append(f"密码命中（第 {idx + 1} 个候选）")
            return pw
        except (RuntimeError, zipfile.BadZipFile, pyzipper.BadZipFile if pyzipper else RuntimeError):
            continue
        except NotImplementedError:
            log.append("成员压缩算法不受支持，跳过该候选目标")
            return None
    log.append(f"密码轮询失败（尝试 {min(len(candidates), MAX_PASSWORD_TRIES)} 次）")
    return None


def poll_7z_password(path: str, candidates: list[str], log: list[str]) -> str | None:
    for idx, pw in enumerate(candidates[:MAX_PASSWORD_TRIES]):
        try:
            with SevenZipFile(path, mode="r", password=pw) as sz:
                names = [f.filename for f in sz.list()
                         if not f.is_directory and
                         os.path.splitext(f.filename)[1].lower() in TEXT_EXTS]
                if not names:
                    return pw  # 无可试成员且能列出即视为正确
                with tempfile.TemporaryDirectory() as sandbox:
                    sz.extract(path=sandbox, targets=[names[0]])
            log.append(f"密码命中（第 {idx + 1} 个候选）")
            return pw
        except (sz_exceptions.Bad7zFile, sz_exceptions.PasswordRequired,
                sz_exceptions.LzmaError, sz_exceptions.DecompressionError,
                sz_exceptions.UnsupportedCompressionError, OSError):
            continue
    log.append(f"密码轮询失败（尝试 {min(len(candidates), MAX_PASSWORD_TRIES)} 次）")
    return None


# ---------- 抽样解压 ----------

def sample_zip(path: str, password: str | None, log: list[str]) -> list[dict]:
    if pyzipper:
        zf = pyzipper.AESZipFile(path)
    else:
        zf = zipfile.ZipFile(path)
    return _sample_from_zf(zf, log, password)


def _sample_from_zf(zf, log: list[str], password: str | None = None) -> list[dict]:
    evidence: list[dict] = []
    budget = TOTAL_LIMIT
    pwd_bytes = password.encode("utf-8") if password else None
    for info in zf.infolist():
        if budget <= 0 or len(evidence) >= 8:
            break
        name = info.filename
        if info.is_dir() or not _safe_member_name(name):
            continue
        ext = os.path.splitext(name)[1].lower()
        base = os.path.basename(name)
        if ext not in TEXT_EXTS and not NOTABLE_NAME_RE.search(base):
            continue
        if info.file_size > SINGLE_FILE_LIMIT:
            log.append(f"超单文件上限，跳过: {name}")
            continue
        try:
            with zf.open(info, pwd=pwd_bytes) as fh:
                data = fh.read(min(info.file_size, SINGLE_FILE_LIMIT))
        except (RuntimeError, zipfile.BadZipFile,
                pyzipper.BadZipFile if pyzipper else RuntimeError) as e:
            log.append(f"读取失败 {name}: {type(e).__name__}")
            continue
        budget -= len(data)
        if _looks_binary(data):
            log.append(f"疑似二进制，跳过: {name}")
            continue
        evidence.append({"file": name, "excerpt": _decode_bytes(data)[:EXCERPT_CHARS]})
    return evidence


def sample_7z(path: str, password: str | None, log: list[str]) -> list[dict]:
    evidence: list[dict] = []
    budget = TOTAL_LIMIT
    with tempfile.TemporaryDirectory() as sandbox:
        with SevenZipFile(path, mode="r", password=password) as sz:
            candidates = [
                f.filename for f in sz.list()
                if not f.is_directory and (
                    os.path.splitext(f.filename)[1].lower() in TEXT_EXTS
                    or NOTABLE_NAME_RE.search(os.path.basename(f.filename))
                )
            ][:8]
            if candidates:
                sz.extract(path=sandbox, targets=candidates)
        for name in candidates:
            if budget <= 0:
                break
            if not _safe_member_name(name):
                log.append(f"拒绝危险成员名: {name!r}")
                continue
            fp = Path(sandbox) / name
            if not fp.is_file() or fp.resolve().is_relative_to(Path(sandbox).resolve()) is False:
                log.append(f"成员落点越界或缺失: {name}")
                continue
            try:
                data = fp.read_bytes()
            except OSError as e:
                log.append(f"读取失败 {name}: {type(e).__name__}")
                continue
            if len(data) > SINGLE_FILE_LIMIT:
                log.append(f"超单文件上限，跳过: {name}")
                continue
            budget -= len(data)
            if _looks_binary(data):
                log.append(f"疑似二进制，跳过: {name}")
                continue
            evidence.append({"file": name, "excerpt": _decode_bytes(data)[:EXCERPT_CHARS]})
    return evidence


# ---------- 顶层入口 ----------

def extract(path: str, depth: str = "listing+sample",
            passwords: list[str] | None = None) -> dict:
    """主入口：压缩包路径 → 中间 JSON。

    depth: 'listing' 仅零解压清单；'listing+sample' 抽样解压（默认）。
    """
    log: list[str] = []
    ext = os.path.splitext(path)[1].lower()
    opener = list_zip if ext == ".zip" else list_7z
    sampler = sample_zip if ext == ".zip" else sample_7z
    poller = poll_zip_password if ext == ".zip" else poll_7z_password

    ls = opener(path, log)

    password: str | None = None
    if ls.password_protected and passwords:
        password = poller(path, passwords, log)

    evidence: list[dict] = []
    if depth == "listing+sample":
        if ls.password_protected and password is None:
            log.append("加密包无可用密码，抽样降级为仅清单")
        else:
            try:
                evidence = sampler(path, password, log)
            except Exception as e:  # 抽样失败不阻断清单结果
                log.append(f"抽样阶段异常: {type(e).__name__}: {e}")

    flags = ls.flags_scan()
    flags["password_protected"] = ls.password_protected
    flags["multi_part"] = bool(
        re.search(r"\.(part\d+|z\d+|r\d+|\d{3})$", path, re.IGNORECASE)
    )

    return {
        "compression": ls.compression,
        "depth": depth,
        "sha256": sha256_file(path),
        "file_size": os.path.getsize(path),
        "mtime": datetime.fromtimestamp(os.path.getmtime(path)).astimezone().isoformat(),
        "password_found": password is not None,
        "structure": {
            "entry_count": ls.entry_count,
            "dir_count": ls.dir_count,
            "total_uncompressed": ls.total_uncompressed,
            "top_extensions": ls.extensions(),
            "top_level_dirs": ls.top_level_dirs(),
            "notable_files": [
                f["name"] for f in ls.files
                if NOTABLE_NAME_RE.search(os.path.basename(f["name"]))
            ][:20],
        },
        "flags": flags,
        "sample_evidence": evidence,
        "security_log": log,
    }
