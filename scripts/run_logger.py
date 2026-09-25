"""run_logger.py — run log summarizer: prints one full run report when a run ends.

Input comes from run-directory files:
  - checklist.json  → run_id, status, concurrency, per-target extract/ai/publish
                      phases and stats
  - context.json    → actual page count per package
  - messages.json   → actual message count per package
  - .publish.json   → confidence (low-confidence list)

Standalone use: uv run python scripts/run_logger.py <run_id or runs/<run_id> path>
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import paths
import schema


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
    """Summarize a run directory into a multi-line text report."""
    cl = _load(run_dir / "checklist.json")
    if not cl:
        return f"run report unavailable: {run_dir} has no checklist.json"

    ctx = _load(run_dir / "context.json").get("packages", {})
    msgs = _load(run_dir / "messages.json").get("packages", {})

    lines = ["===== run report =====",
             f"run: {cl.get('run_id')}  status: {cl.get('status')}"
             f"  started: {cl.get('started_at')}  elapsed: {_elapsed(cl)}"]
    conc = cl.get("concurrency")
    lines.append(f"concurrency: {conc if isinstance(conc, int) else '?'}"
                 f"   targets: {len(cl.get('packages', {}))}")

    counters: dict[str, int] = {}
    low_conf: list[str] = []
    for path_str, pkg in cl.get("packages", {}).items():
        name = pkg.get("name", Path(path_str).name)
        ex, ai, pb = pkg.get("extract", {}), pkg.get("ai", {}), pkg.get("publish", {})
        phase = pb.get("phase") or ai.get("phase") or ex.get("phase") or "pending"
        counters[phase] = counters.get(phase, 0) + 1

        entry = ex.get("entry_id", "")
        pages_real = len(ctx.get(entry, {}).get("pages", []))
        msgs_real = len(msgs.get(entry, {}).get("messages", []))
        st = pkg.get("stats", {})

        head = f"{name}: {phase}"
        if phase == "skipped":
            lines.append(f"  {head}  sidecar exists")
            continue
        details = []
        if ex.get("phase"):
            details.append(f"extract {ex['phase']}"
                           + (f" files_kept={ex['files_kept']}"
                              if ex["phase"] == "done"
                              else "")
                           + (f" ({ex['detail']})" if ex.get("detail")
                              and ex["phase"] == "failed" else ""))
        if ai.get("phase"):
            details.append(f"identify {ai['phase']}"
                           + ("" if ai.get("published", True) else " (degraded)")
                           + (f" ({ai['detail']})" if ai.get("detail")
                              and ai["phase"] == "failed" else ""))
        if pb.get("phase"):
            details.append(f"publish {pb['phase']}"
                           + (f" ({pb['detail']})" if pb.get("detail")
                              and pb["phase"] == "failed" else ""))
        lines.append(f"  {head}  " + " | ".join(details))

        if pages_real or msgs_real or st:
            lines.append(f"    pages {pages_real or st.get('pages', 0)}"
                         f"  messages {msgs_real or st.get('messages', 0)}"
                         f"  tokens {st.get('tokens_in', 0)}/{st.get('tokens_out', 0)}"
                         f"  turns {st.get('turns', 0)}"
                         f"  sentences_skipped {st.get('sentences_skipped', 0)}"
                         f"  elapsed {st.get('elapsed_s', '?')}s")

        side = pb.get("sidecar")
        if side:
            try:
                doc = json.loads(Path(side).read_text(encoding="utf-8-sig"))
                c = doc.get("scrape", {}).get("confidence", 0.0)
                if c < schema.LOW_CONFIDENCE:
                    low_conf.append(f"{name} ({c:.2f})")
            except (OSError, json.JSONDecodeError, ValueError):
                pass

    lines.append("Summary: " + (" | ".join(f"{k} {v}"
                                           for k, v in counters.items()) or "none"))
    if low_conf:
        lines.append("Low confidence (manual review or --force rescan recommended): "
                     + "  ".join(low_conf))
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("Usage: run_logger.py <run_id or run directory path>", file=sys.stderr)
        return 2
    p = Path(argv[0])
    if not p.is_absolute():
        p = paths.RUNS_DIR / p
    print(run_report(p))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
