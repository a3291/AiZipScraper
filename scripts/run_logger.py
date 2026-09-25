"""run_logger.py — run 日志汇总器：run 结束时打印一次完整 run 报告。

输入全部来自 run 目录的落盘文件（checklist.json / context.json / messages.json）
与侧车本身，不依赖 cli 的内存状态：
  - checklist.json  → run_id、状态、并发、每目标 extract/ai/publish 三段 phase 与统计
  - context.json    → 每包实际页数
  - messages.json   → 每包实际 message 数
  - .publish.json   → 置信度（低置信清单）

也可独立使用：uv run python scripts/run_logger.py <run_id 或 runs/<run_id> 路径>
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import paths
import schema

PH_ZH = {"done": "完成", "failed": "失败", "skipped": "跳过", "pending": "未动"}


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}


def _elapsed(cl: dict) -> str:
    try:
        t0 = datetime.fromisoformat(cl["started_at"])
        t1 = datetime.fromisoformat(cl.get("updated_at", cl["started_at"]))
        return f"{(t1 - t0).total_seconds():.1f}s"
    except (KeyError, ValueError):
        return "?"


def run_report(run_dir: Path) -> str:
    """汇总 run 目录 → 多行文本报告。"""
    cl = _load(run_dir / "checklist.json")
    if not cl:
        return f"run 报告不可用：{run_dir} 缺 checklist.json"

    ctx = _load(run_dir / "context.json").get("packages", {})
    msgs = _load(run_dir / "messages.json").get("packages", {})

    lines = ["===== run 报告 =====",
             f"run: {cl.get('run_id')}  状态: {cl.get('status')}"
             f"  开始: {cl.get('started_at')}  耗时: {_elapsed(cl)}"]
    conc = cl.get("concurrency")
    lines.append(f"并发: {conc if isinstance(conc, int) else '?'}"
                 f"   目标数: {len(cl.get('packages', {}))}")

    counters: dict[str, int] = {}
    low_conf: list[str] = []
    for path_str, pkg in cl.get("packages", {}).items():
        name = pkg.get("name", Path(path_str).name)
        ex, ai, pb = pkg.get("extract", {}), pkg.get("ai", {}), pkg.get("publish", {})
        phase = pb.get("phase") or ai.get("phase") or ex.get("phase") or "pending"
        counters[phase] = counters.get(phase, 0) + 1

        entry = (ex.get("dir") or "extracted/").split("/")[-1]
        pages_real = len(ctx.get(entry, {}).get("pages", []))
        msgs_real = len(msgs.get(entry, {}).get("messages", []))
        st = pkg.get("stats", {})

        head = f"{name}: {PH_ZH.get(phase, phase)}"
        if phase == "skipped":
            lines.append(f"  {head}  侧车已存在")
            continue
        details = []
        if ex.get("phase"):
            details.append(f"提取 {PH_ZH.get(ex['phase'], ex['phase'])}"
                           + (f" files_kept={ex['files_kept']}"
                              if ex["phase"] == "done"
                              else "")
                           + (f" ({ex['detail']})" if ex.get("detail")
                              and ex["phase"] == "failed" else ""))
        if ai.get("phase"):
            details.append(f"识别 {PH_ZH.get(ai['phase'], ai['phase'])}"
                           + ("" if ai.get("published", True) else "（降级）")
                           + (f" ({ai['detail']})" if ai.get("detail")
                              and ai["phase"] == "failed" else ""))
        if pb.get("phase"):
            details.append(f"发布 {PH_ZH.get(pb['phase'], pb['phase'])}"
                           + (f" ({pb['detail']})" if pb.get("detail")
                              and pb["phase"] == "failed" else ""))
        lines.append(f"  {head}  " + " | ".join(details))

        if pages_real or msgs_real or st:
            lines.append(f"    pages {pages_real or st.get('pages', 0)}"
                         f"  messages {msgs_real or st.get('messages', 0)}"
                         f"  tokens {st.get('tokens_in', 0)}/{st.get('tokens_out', 0)}"
                         f"  turns {st.get('turns', 0)}"
                         f"  句跳过 {st.get('sentences_skipped', 0)}"
                         f"  耗时 {st.get('elapsed_s', '?')}s")

        side = pb.get("sidecar")
        if side:
            try:
                doc = json.loads(Path(side).read_text(encoding="utf-8-sig"))
                c = doc.get("scrape", {}).get("confidence", 0.0)
                if c < schema.LOW_CONFIDENCE:
                    low_conf.append(f"{name} ({c:.2f})")
            except (OSError, json.JSONDecodeError, ValueError):
                pass

    lines.append("汇总: " + (" | ".join(f"{PH_ZH.get(k, k)} {v}"
                                       for k, v in counters.items()) or "无"))
    if low_conf:
        lines.append("低置信（建议人工复核或 --force 重刮）: " + "  ".join(low_conf))
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("用法: run_logger.py <run_id 或 run 目录路径>", file=sys.stderr)
        return 2
    p = Path(argv[0])
    if not p.is_absolute():
        p = paths.RUNS_DIR / p
    print(run_report(p))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
