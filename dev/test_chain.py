"""dev/test_chain.py — 链环形态冒烟（mock AI 后端）。跑完可删。"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))            # schema.py 在根
sys.path.insert(0, str(ROOT / "scripts"))   # 运行时模块在 scripts/

import ai_identify as ai
import backend
import cli

# mock 后端：翻页1次后发布
backend.endpoint_available = lambda c: True
backend.resolve_model = lambda c: "mock-model"
calls = {"n": 0}
def fake_post(url, payload, timeout, api_key=""):
    calls["n"] += 1
    if calls["n"] == 1:
        content = json.dumps({"action": "read_page", "page": 1, "identity": None})
    else:
        content = json.dumps({"action": "publish", "page": None, "identity": {
            "title": "空气质量数据集", "category": "dataset",
            "summary": "中国城市空气质量监测数据。", "tags": ["env", "csv"],
            "language": ["zh"], "confidence": 0.88}})
    return {"choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 900, "completion_tokens": 60}}
backend.post = fake_post

# 建独立夹具目录（不污染 test_fixtures）
fx = Path(tempfile.mkdtemp()) / "fx"
fx.mkdir()
shutil.copy2(ROOT / "dev/test_fixtures/air_quality_2024.zip", fx / "a.zip")

run_dir = cli.new_run_dir()
cfg = ai.load_config()
prompts = ai.load_prompts()
cl = {"run_id": run_dir.name, "started_at": cli.now_iso(),
      "status": "running", "packages": {}}

rec = cli.process_one(fx / "a.zip", run_dir, cfg, prompts, cl)
fails = []
def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  {info}" if not cond else ""))
    if not cond:
        fails.append(name)

check("process ok", rec["outcome"] == "ok", rec)
pkg = cl["packages"][str(fx / "a.zip")]
check("ai published", pkg["ai"]["published"] is True, pkg["ai"])
check("publish done", pkg["publish"]["phase"] == "done", pkg["publish"])
check("pages_read", pkg["stats"]["pages_read"] == 1, pkg["stats"])
check("messages count", pkg["stats"]["messages"] >= 4, pkg["stats"])

side = Path(str(fx / "a.zip") + ".publish.json")
doc = json.loads(side.read_text(encoding="utf-8-sig"))
check("sidecar identity", doc["identity"]["title"] == "空气质量数据集"
      and doc["scrape"]["confidence"] == 0.88, doc["identity"])
check("messages.json", (run_dir / "messages.json").exists())
msgs = json.loads((run_dir / "messages.json").read_text(encoding="utf-8-sig"))
mlist = msgs["packages"][pkg["extract"]["dir"].split("/")[1]]["messages"]
check("prompt_key 留痕", all("prompt_key" in m for m in mlist[:2]), mlist[:2])
check("context.json", (run_dir / "context.json").exists())

# show / check / export
import io, contextlib
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc_show = cli.cmd_show(type("A", (), {"path": str(fx / "a.zip")})())
out = buf.getvalue()
check("show 输出", rc_show == 0 and "空气质量数据集" in out, out)

with contextlib.redirect_stdout(io.StringIO()) as buf2:
    rc_check = cli.cmd_check(type("A", (), {"path": str(fx)})())
check("check 干净", rc_check == 0, buf2.getvalue())

exp = fx / "out.jsonl"
with contextlib.redirect_stdout(io.StringIO()):
    rc_exp = cli.cmd_export(type("A", (), {"path": str(fx), "output": str(exp)})())
lines = exp.read_text(encoding="utf-8").strip().splitlines()
check("export 1条", rc_exp == 0 and len(lines) == 1
      and json.loads(lines[0])["title"] == "空气质量数据集", lines)

# 分页/软化单测
import context_builder as cb
big = fx / "big"
big.mkdir()
(big / "t.txt").write_text(("第一句话。" * 100) + ("b" * 900) + "。尾部。")
ctx = cb.build(big, page_chars=300)
check("切页生效", ctx["stats"]["page_count"] >= 2, ctx["stats"])
check("长句跳过", ctx["stats"]["sentences_skipped"] == 1, ctx["stats"])
idx = {f["file"]: f for f in ctx["files_index"]}
check("files_index", idx["t.txt"]["page"] == 1 and idx["t.txt"]["start"] == 0, idx)
check("tail_page 在末尾", ctx["tail_page"]["no"] == ctx["stats"]["page_count"] + 1)

shutil.rmtree(run_dir, ignore_errors=True)
shutil.rmtree(fx.parent, ignore_errors=True)
print("\n" + ("ALL PASS" if not fails else f"FAILED: {fails}"))
sys.exit(1 if fails else 0)
