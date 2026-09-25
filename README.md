# AiZipScraper

AI-powered scraper for archives and files — identifies what a package *is, what's inside, and what it's for*, and anchors the result next to the file as a sidecar JSON.

Inspired by media library scrapers (like Plex): no unnamed archive black holes — every package gets a searchable, verifiable profile.

## Features

- **Full extraction, original files preserved**: a pluggable extractor unpacks zip/7z archives (or copies plain files) into a per-target directory; original files stay untouched
- **Password polling**: passwords live in the extractor's own `password.json`; encrypted archives are tried automatically — the password never appears on the command line or in any artifact
- **Paged AI identification**: extracted content is packed into sentence-aligned pages with a catalog and a metadata tail page; the AI browses pages through a strict JSON contract (`read_page` / `publish`) instead of one giant prompt
- **Context guardrails**: `max_turns` cap, near-limit reminder, forced publish at the token ceiling, invalid-JSON tolerance, page-stall detection — every failure path degrades to a flagged `unknown`, never a crash
- **Sidecar anchoring**: the result lands in `<name>.publish.json` next to the target, keyed by SHA256 — re-scans skip already-published targets; `check` detects hash drift, low confidence and orphan sidecars
- **Batch friendly**: concurrent extract + identify pools behind one barrier, per-target failure isolation, full run archives under `runs/`, JSONL export

## Production–consumption pipeline

The project is a three-stage pipeline. Each stage only consumes the previous stage's on-disk artifacts and produces files; the inter-stage interface is files, not function calls. `cli.py` is the sole orchestrator; stage scripts never import each other.

```
⓪ Static contract layer   jsons/        (in git; produced by humans, consumed by programs)
   scraper.json   single source of config truth (ai keys + concurrency + limits)
   prompt.json    prompt registry + action contract (category enum mirrors schema.py)
   publish.json   sidecar template: <A_*> filled by the program / <I_*> filled by the AI
        │  (read-only for the whole scan)
        ▼
① scan_extract            cli.py (extract section) + run_extractor.py + extractors/<name>/
   §1 ROSTER     every file is a target (runs/ and *.publish.json excluded);
                 entry_id = first 8 hex of sha256; cached decided before any work
   §2 EXTRACT    subprocess pool × concurrency; per-target out dir (concurrency-safe);
                 extractor holds its own passwords; heartbeat file on start, removed on success
   §3 CONTRACT   _result.json seven-key contract validated at the source;
                 on violation exit 3, nothing is written
   §4 PUBLISH    runs/<run_id>/extracted/<entry_id>/ + _result.json
        ↓ barrier (extract pool joined before the identify pool opens;
          extract failures stop here, recorded on the checklist only)
② identify_publish        cli.py (identify section) + context_builder + ai_identify/backend + publisher
   §1 CONTEXT    reads extracted/ only (underscore files excluded) → sentence-split,
                 page-softened pages + files_index + tail page → context.json
   §2 SESSION    run_session drives the AI through the JSON contract;
                 backend.py is the only network egress (OpenAI-compatible);
                 messages.json appended atomically per turn
   §3 GATE       program fills anchoring/structure, AI fills identity only →
                 schema.validate gate; invalid sidecars are never written
   §4 PUBLISH    <name>.publish.json sidecar + runs/<run_id>/{checklist,context,messages}.json
        ↓ barrier (identify pool done → status = done / interrupted)
③ audit                   run_logger.py + show/check/export   (read-only, produces nothing)
   §1 REPORT     run report assembled purely from disk; zero printing during the scan,
                 one report at the end; any run can be replayed standalone
   §2 READOUT    show/check/export consume sidecars only;
                 check re-hashes targets, flags low confidence and orphans
   §3 CHECKLIST  checklist.json records extract/ai/publish phase per target;
                 the only cross-run idempotency credential is the sidecar itself
```

`extractors/` is a sealed domain: extractors never import project modules, never read `jsons/`, and are swapped in/out as whole directories.

## Installation

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12+.

```bash
git clone <repo-url> AiZipScraper
cd AiZipScraper
uv sync
```

## Quick start

```bash
# Batch-scrape a directory (cached targets are skipped automatically)
uv run python main.py scan D:/downloads

# Force a full re-scrape
uv run python main.py scan D:/downloads --force

# Show sidecars
uv run python main.py show D:/downloads

# Health check: unscraped / hash mismatch / low confidence / orphans
uv run python main.py check D:/downloads

# Export a JSONL digest for spreadsheets or search
uv run python main.py export D:/downloads -o summary.jsonl

# Replay a run report standalone
uv run python scripts/run_logger.py <run_id>
```

## Configuration

`jsons/scraper.json` is the single source of config truth; missing keys raise instead of silently defaulting:

```json
{
  "concurrency": 4,
  "ai": {
    "provider": "lmstudio",
    "base_url": "http://localhost:1234/v1",
    "model": "",
    "api_key": "lm-studio",
    "temperature": 0.2,
    "timeout": 300,
    "page_chars": 3000,
    "remind_at": 32000,
    "force_publish_at": 60000,
    "max_turns": 24
  },
  "limits": {
    "extract_timeout_s": 1800,
    "max_text_file_bytes": 33554432,
    "sentence_max_ratio": 0.1
  }
}
```

| Key | Meaning |
|-----|---------|
| `concurrency` | one value governs both the extract pool and the identify pool (they never overlap; `--workers` overrides per run) |
| `ai.provider` / `ai.base_url` | `lmstudio` default; any OpenAI-compatible endpoint works (Ollama `/v1`, vLLM, llama.cpp server) — an explicit `base_url` takes precedence |
| `ai.model` | leave empty to auto-pick the first loaded model on the backend |
| `ai.page_chars` | target page size in characters |
| `ai.remind_at` / `ai.force_publish_at` | estimated-token thresholds: nudge the AI, then force a publish |
| `ai.max_turns` | hard cap on session turns |
| `limits.extract_timeout_s` | per-target extraction time box |
| `limits.max_text_file_bytes` | files above this size are registered but not read as text |
| `limits.sentence_max_ratio` | sentences longer than `page_chars × ratio` are skipped whole |

Prompts live in `jsons/prompt.json`; the sidecar template in `jsons/publish.json`. Swap config ad hoc with `--config other.json`.

## Sidecar format

`<name>.publish.json` is created next to every target, always passing `schema.py` validation — invalid sidecars are never written:

```json
{
  "anchoring":  { "sha256": "…", "source_path": "…", "file_size": 0, "mtime": "…" },
  "scrape":     { "scraped_at": "…", "engine": "…", "depth": "full", "confidence": 0.86 },
  "identity":   { "title": "…", "category": "dataset", "summary": "…", "tags": [], "language": [] },
  "structure":  { "entry_count": 0, "dir_count": 0, "total_uncompressed": 0,
                  "top_extensions": {}, "top_level_dirs": [], "notable_files": [] },
  "flags":      { "password_protected": false, "multi_part": false, "nested_archives": [],
                  "exe_present": false, "macro_docs": false },
  "warnings":   []
}
```

- `anchoring.sha256` is the primary anchor: `check` re-hashes the target to detect content drift
- `flags.password_protected` only signals encryption — the working password is never persisted
- `warnings` includes the extractor's own notes, passed through untouched

## Project layout

```
├── main.py                        # unified entry point (delegates to scripts/cli.py)
├── scripts/                       # pipeline stages (never import each other)
│   ├── cli.py                     # orchestrator: scan / show / check / export
│   ├── run_extractor.py           # extractor runner (subprocess entry, contract validation)
│   ├── context_builder.py         # extracted/ → paged context
│   ├── ai_identify.py             # recognition session engine
│   ├── backend.py                 # AI server/API connection layer (only network egress)
│   ├── publisher.py               # template fill → validate → sidecar
│   ├── run_logger.py              # run report assembler (pure disk reader)
│   ├── schema.py                  # sidecar contract single source
│   └── paths.py                   # single source of project paths
├── extractors/                    # sealed extractor domain (pluggable directories)
│   └── default/                   # extractor.py + self-held password.json
├── jsons/                         # static contract layer (human-edited, program-read)
└── runs/                          # per-scan archives (not tracked)
```

## Safety boundaries

- Extraction is sandboxed: normalized member paths, `..`/absolute-path traversal rejected, zip-bomb caps on total size (4GB) and entry count (50k)
- Nothing is executed and no macros are parsed; only text is read as evidence
- The AI controls *nothing* — it may only read pages and publish an identity through the JSON contract; program-side fields (hashes, structure, anchoring) never pass through the model
- Invalid AI output degrades to a flagged `unknown` sidecar; nothing invalid ever lands on disk
