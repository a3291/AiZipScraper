"""extractors/default — 默认提取器（环2，可插拔；每个提取器一个自持目录）。

CLI 契约：传入路径（单文件）+ 传出路径（目录）；可并发（每目标独立传出目录）。
自持性：只依赖 uv 环境内的第三方库，不 import 项目外围模块；
密码自持——从本目录 password.json 读取（{"passwords": ["...", ...]}）。
行为在本脚本内定死（不接线）：压缩包（zip/7z）全量解压保留原始文件，
普通文件直接复制；沙箱防越界；zip 炸弹护栏（总量/条目数上限）。
由 scripts/run_extractor.py 加载执行，结果 dict 经运行器落 _result.json。
下游 context 组织器只读传出目录，与本结果 dict 解耦。
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

# 脚本内定死的护栏（不接线）
MAX_TOTAL_UNCOMPRESSED = 4 * 1024 ** 3   # 解压总量上限 4GB
MAX_ENTRIES = 50000                      # 条目数上限


# ---------- 内聚哈希（不引用外围） ----------

def sha256_file(path, buf: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(buf):
            h.update(chunk)
    return h.hexdigest()


def entry_id_for(path) -> str:
    return sha256_file(path)[:8]


# ---------- 自持密码 ----------

def load_passwords() -> list[str]:
    pw_path = HERE / "password.json"
    try:
        data = json.loads(pw_path.read_text(encoding="utf-8-sig"))
        return [str(p) for p in data.get("passwords", [])]
    except (OSError, json.JSONDecodeError):
        return []


def _safe_join(base: Path, name: str) -> Path | None:
    """归一化成员路径，越界返回 None。"""
    norm = name.replace("\\", "/").lstrip("/")
    if any(p == ".." for p in norm.split("/")) or ":" in norm:
        return None
    p = (base / norm).resolve()
    return p if str(p).startswith(str(base.resolve())) else None


# ---------- 清单统计 ----------

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
        log.append("zipfile 打不开，尝试 pyzipper（AES zip）")
        zf = pyzipper.AESZipFile(path)
    with zf:
        infos = zf.infolist()
        names = [i.filename for i in infos]
        total = sum(i.file_size for i in infos)
        protected = any(i.flag_bits & 0x1 for i in infos)
    return _stats_from_names(names, total, protected)


def _list_7z(path: str, log: list[str]) -> dict:
    if py7zr is None:
        raise RuntimeError("py7zr 未安装，无法处理 7z")
    with py7zr.SevenZipFile(path) as z:
        names = z.getnames()
        total = sum(e.uncompressed for e in z.list() if not e.is_directory)
        protected = z.needs_password()
    return _stats_from_names(names, total, protected)


# ---------- 密码轮询 ----------

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
                log.append(f"密码轮询命中（第 {passwords.index(pw)+1} 个候选）")
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
            log.append(f"密码轮询命中（第 {passwords.index(pw)+1} 个候选）")
            return pw
        except Exception:
            continue
    return None


# ---------- 主入口 ----------

def extract(path: str, out_dir: str | Path,
            passwords: list[str] | None = None) -> dict:
    """环2 主入口：任意路径 → extracted/<entry_id>/ + 结果 dict。

    压缩包：全量解压（原始文件保留）；普通文件：直接复制。
    密码候选默认读本目录 password.json。
    """
    log: list[str] = []
    passwords = passwords if passwords is not None else load_passwords()
    out = Path(out_dir)
    ext = os.path.splitext(path)[1].lower()
    sha = sha256_file(path)

    if ext not in ARCHIVE_EXTS:
        # 普通文件：直接复制
        out.mkdir(parents=True, exist_ok=True)
        dest = out / os.path.basename(path)
        if Path(path).resolve() != dest.resolve():
            shutil.copy2(path, dest)
        st = _stats_from_names([os.path.basename(path)],
                               os.path.getsize(path), False)
        return {"entry_id": sha[:8], "kind": "file", "sha256": sha,
                "structure": st, "password_found": False,
                "warnings": log, "files_kept": 1}

    # 压缩包
    if ext == ".zip":
        st = _list_zip(path, log)
    else:
        if py7zr is None:
            raise RuntimeError("py7zr 未安装，无法处理 7z")
        st = _list_7z(path, log)

    if st["entry_count"] > MAX_ENTRIES:
        raise ValueError(f"条目数 {st['entry_count']} 超上限 {MAX_ENTRIES}，拒绝解压")
    if st["total_uncompressed"] > MAX_TOTAL_UNCOMPRESSED:
        raise ValueError(f"解压总量 {st['total_uncompressed']} 超上限，拒绝解压")

    password = None
    if st["password_protected"] and passwords:
        poller = poll_zip_password if ext == ".zip" else poll_7z_password
        password = poller(path, passwords, log)
    if st["password_protected"] and password is None:
        log.append("加密包无可用密码，解压中止（仅登记元数据）")
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
                    log.append(f"越界成员已跳过: {info.filename}")
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
            # 成员级净化：越界/坏名（如 "."、".."、绝对路径）剔除
            valid = [n for n in names if _safe_join(out, n) is not None
                     and _safe_join(out, n) != out.resolve()]
            skipped = [n for n in names if n not in valid]
            for n in skipped:
                log.append(f"越界成员已跳过: {n}")
            try:
                z.extract(path=out, targets=valid)
                kept = len(valid)
            except Exception as e:
                # 批量被个别坏成员阻断时，回退逐成员提取
                if len(valid) > 1 and type(e).__name__ == "Bad7zFile":
                    kept = 0
                    for n in valid:
                        try:
                            z.extract(path=out, targets=[n])
                            kept += 1
                        except Exception:
                            log.append(f"成员提取失败已跳过: {n}")
                else:
                    raise

    return {"entry_id": sha[:8], "kind": "archive", "sha256": sha,
            "structure": st, "password_found": password is not None,
            "warnings": log, "files_kept": kept}
