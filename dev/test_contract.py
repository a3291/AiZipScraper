"""dev/test_contract.py — v4.2 契约硬化与心跳验证（跑完可删）。"""
import json
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
out = Path(tempfile.mkdtemp()) / "out"
ok = True


def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name)
    ok = ok and cond


# 1) 契约校验：返回缺键 dict → exit 3 且不落 _result.json
bad_ext = ROOT / "extractors" / "t_bad.py"
bad_ext.write_text('def extract(i, o):\n    return {"entry_id": "x"}\n',
                   encoding="utf-8")
p = subprocess.run([sys.executable, str(ROOT / "scripts/run_extractor.py"),
                    "t_bad", str(bad_ext), str(out)],
                   capture_output=True, text=True)
check("缺键 exit 3", p.returncode == 3)
check("缺键不落 _result.json", not (out / "_result.json").exists())
bad_ext.unlink()

# 2) 心跳生命周期：运行中存在，成功后删除
sleep_ext = ROOT / "extractors" / "t_sleep.py"
sleep_ext.write_text(
    'import time\n'
    'def extract(i, o):\n'
    '    time.sleep(1.5)\n'
    '    return {"entry_id": "x", "kind": "file", "sha256": "y", '
    '"structure": {"entry_count": 0, "dir_count": 0, "total_uncompressed": 0, '
    '"top_extensions": {}, "top_level_dirs": [], "notable_files": [], '
    '"password_protected": False, "multi_part": False, "nested_archives": [], '
    '"exe_present": False, "macro_docs": False}, "password_found": False, '
    '"warnings": [], "files_kept": 0}\n',
    encoding="utf-8")
res_box = {}
t = threading.Thread(target=lambda: res_box.update(p=subprocess.run(
    [sys.executable, str(ROOT / "scripts/run_extractor.py"),
     "t_sleep", str(sleep_ext), str(out)], capture_output=True, text=True)))
t.start()
time.sleep(0.8)
hb_mid = (out / "_heartbeat.json").exists()
print(f"  [diag] 0.8s: heartbeat={hb_mid}")
t.join()
p = res_box["p"]
print(f"  [diag] returncode={p.returncode} stderr={p.stderr.strip()[:120]}")
check("运行中心跳存在", hb_mid)
check("成功后心跳删除", not (out / "_heartbeat.json").exists())
check("合规结果落 _result.json", (out / "_result.json").exists())
sleep_ext.unlink()

print("\nALL PASS" if ok else "\nHAS FAILURES")
sys.exit(0 if ok else 1)

