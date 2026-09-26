"""make_fixtures.py — 构造测试压缩包（普通 zip / 普通 7z / 加密 zip）。"""
import os
import subprocess
import sys
from pathlib import Path

out = Path(__file__).resolve().parent.parent / "dev" / "test_fixtures"
out.mkdir(parents=True, exist_ok=True)

# --- 普通 zip ---
zdir = out / "_z"; zdir.mkdir(exist_ok=True)
(zdir / "README.md").write_text("本数据集收录2024年中国主要城市空气质量监测数据，含 PM2.5、AQI 等字段。", encoding="utf-8")
(zdir / "data.csv").write_text("city,pm25,aqi\n北京,45,120\n上海,32,88\n", encoding="utf-8")
(zdir / "docs").mkdir(exist_ok=True)
(zdir / "docs" / "说明.txt").write_text("字段说明：pm25 为年均浓度。", encoding="utf-8")
zpath = out / "air_quality_2024.zip"
import zipfile
with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
    for p in zdir.rglob("*"):
        if p.is_file():
            zf.write(p, p.relative_to(zdir))

# --- 普通 7z ---
sdir = out / "_s"; sdir.mkdir(exist_ok=True)
(sdir / "game_readme.txt").write_text("独立解谜游戏《雾岛》，Unity 引擎开发，v1.2。", encoding="utf-8")
(sdir / "assets").mkdir(exist_ok=True)
(sdir / "assets" / "config.ini").write_text("[graphics]\nresolution=1920x1080\n", encoding="utf-8")
s7z = out / "mistisland_game.7z"
if s7z.exists():
    s7z.unlink()
subprocess.run([sys.executable, "-c", f"""
import py7zr
with py7zr.SevenZipFile(r"{s7z}", "w") as z:
    z.writeall(r"{sdir}", arcname=".")
"""], check=True)

# --- 加密 zip（AES，pyzipper 生成）---
edir = out / "_e"; edir.mkdir(exist_ok=True)
(edir / "secret_readme.txt").write_text("机密项目资料：星尘计划二期方案。", encoding="utf-8")
epath = out / "encrypted_pack.zip"
import pyzipper
with pyzipper.AESZipFile(epath, "w", compression=pyzipper.ZIP_DEFLATED,
                         encryption=pyzipper.WZ_AES) as zf:
    zf.setpassword(b"mypass123")
    for p in edir.rglob("*"):
        if p.is_file():
            zf.write(p, p.relative_to(edir).as_posix())

# --- 密码文件 ---
(out / "passwords.txt").write_text("wrong1\nmypass123\nanother\n", encoding="utf-8")

print("fixtures ready:", [p.name for p in out.iterdir()])
