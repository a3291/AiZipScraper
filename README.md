English | [简体中文](README.zh-CN.md)

# AiZipScraper

AI-powered scraper for archives and files — identifies what a package *is, what's inside, and what it's for*, and anchors the result next to the file as a sidecar JSON.

Inspired by media library scrapers (like Plex): each package keeps a searchable, verifiable profile next to the file.

## Features

- **Sampled extraction, original files preserved**: a pluggable extractor samples zip/7z archives into a per-target directory (plain files are copied as-is) — whitelisted text files and notable-named members only, under per-file (256KB), cumulative (4MB) and file-count (8) caps; original files are not modified
- **Password polling**: passwords live in the extractor's own `password.json`; encrypted archives are tried automatically — the password is not passed on the command line and not written to artifacts
- **Paged AI identification**: extracted content is packed into sentence-aligned pages with a catalog and a metadata tail page; the AI browses pages through a JSON contract (`read_page` / `publish`)
- **Context guardrails**: `max_turns` cap (negative = unlimited), near-limit reminder, forced publish at the token ceiling, invalid-JSON tolerance, page-stall detection — failure paths degrade to a flagged `unknown`
- **Sidecar anchoring**: the result lands in `<name>.publish.json` next to the target, keyed by SHA256 — re-scans skip already-published targets; `check` detects hash drift, low confidence and orphan sidecars
- **Batch friendly**: concurrent extract + identify pools behind one barrier, per-target failure isolation, full run archives under `runs/`, JSONL export

## Production–consumption pipeline

Six rings plus a static contract layer. File-boundary handovers: targets →
extracted/ + _result.json → sidecar. Rings 3–5 run in-process under `cli.py`,
handing dicts in memory — the runs/ JSON files
(context.json, messages.json, checklist.json) are archives written alongside,
so ring 6 can replay any run standalone. `cli.py` is the orchestrator and the
only file importing across rings — ring scripts do not import each other.

```
static contract layer   jsons/scraper.json · prompt.json · publish.json
                        (human-edited; read by cli / ai_identify / publisher)

main.py ──> cli.py
  ring 1  find_targets      → target list (every file; runs/ and *.publish.json
                              excluded; entry_id = sha256[:8]; a target with an
                              existing sidecar is recorded skipped)
  ring 2  extract_one ── subprocess ──> run_extractor.py ── load ──> extractors/<name>/
          │                 → runs/<id>/extracted/<entry_id>/ + _result.json
          │                 (extractor reads its own config.json and
          │                  password.json; heartbeat file written at start,
          │                  removed on success)
  ring 3  context_builder   eats extracted/ only (underscore-prefixed files skipped)
          │                 → context dict; archived into runs/<id>/context.json
  ring 4  ai_identify       eats the ring-3 context dict + prompt.json + scraper.json
          │   └ backend.py  → runs/<id>/messages.json (appended atomically per turn)
          │                   + identity dict
  ring 5  publisher         eats program fields + identity + publish.json template
          │                 → <name>.publish.json sidecar (validation failure
          │                   writes nothing)
  ring 6  run_logger        eats runs/<id>/{checklist,context,messages}.json + sidecars
                            → run report; no printing during the scan, one report
                              at the end; callable standalone for any run_id
```

Rings 1–2 run as a worker pool, then rings 3–5 run per target after the pool
joins, handing dicts in memory (the runs/ files are archives for ring 6 and
standalone replay); ring 6 is read-only. `extractors/` does not import project
modules and does not read `jsons/`; extractor directories are swapped in and
out whole.

## Installation

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12+.

```bash
git clone https://github.com/a3291/AiZipScraper.git
cd AiZipScraper
uv sync
```

## Quick start

```bash
# Recommended scan invocation: config from jsons/scraper.json + chatlog roll + post-publish cleanup, serial workers, default extractor, full re-scrape
uv run python main.py scan D:/downloads --config jsons/scraper.json --auto-chatlog --auto-extracted-clean --workers 1 --extractor default --force

# Batch-scrape a directory (cached targets are skipped automatically)
uv run python main.py scan D:/downloads

# Override concurrency for this run only (default: scraper.json "concurrency")
uv run python main.py scan D:/downloads --workers 8

# Use another extractor (a directory under extractors/; default: default)
uv run python main.py scan D:/downloads --extractor my_extractor

# Force a full re-scrape
uv run python main.py scan D:/downloads --force

# Delete each target's extracted files right after it publishes successfully (saves disk; failed targets keep their files)
uv run python main.py scan D:/downloads --auto-extracted-clean

# Roll the AI session history into archived JSON summaries when it grows (see API interaction)
uv run python main.py scan D:/downloads --auto-chatlog

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

`jsons/scraper.json` is the only config file; missing or mistyped keys raise:

```json
{
  "concurrency": 4,
  "ai": {
    "base_url": "http://localhost:1234/v1",
    "model": "",
    "api_key": "lm-studio",
    "temperature": 0.2,
    "timeout": 300,
    "page_chars": 3000,
    "remind_at": 32000,
    "force_publish_at": 60000,
    "max_turns": -1
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
| `concurrency` | one value governs both the extract pool and the identify pool (the pools do not overlap; `--workers` overrides per run) |
| `ai.base_url` | empty → auto-probe: LM Studio native `/api/v1/chat` → LM Studio `/v1/chat/completions` → Ollama `/v1/chat/completions` (first passing reachability + a minimal session wins); a non-empty value is used as-is. Request style follows the URL: full path ending in `/chat` sends the native `{model, input}` body; `/chat/completions` or a bare base (e.g. `/v1`) sends the messages array |
| `ai.model` | leave empty to auto-pick the first loaded model on the backend |
| `ai.page_chars` | target page size in characters |
| `ai.remind_at` / `ai.force_publish_at` | estimated-token thresholds: nudge the AI, then force a publish |
| `ai.max_turns` | session turn cap; negative (default `-1`) = unlimited — the token ceilings, stall detection and invalid-JSON tolerance still terminate the session |
| `limits.extract_timeout_s` | per-target extraction time box |
| `limits.max_text_file_bytes` | files above this size are registered but not read as text |
| `limits.sentence_max_ratio` | sentences longer than `page_chars × ratio` are skipped whole |

Prompts live in `jsons/prompt.json`; the sidecar template in `jsons/publish.json`. Swap config ad hoc with `--config other.json`.

## API interaction

`backend.py` talks to the AI server once per turn of every recognition session.

**Endpoint resolution** (once per run, before the first session):

- `ai.base_url` non-empty → used as-is
- `ai.base_url` empty → candidates probed in order: LM Studio native
  `http://localhost:1234/api/v1/chat` → LM Studio
  `http://localhost:1234/v1/chat/completions` → Ollama
  `http://localhost:11434/v1/chat/completions`; each gets a reachability POST
  (any HTTP response counts) plus a minimal session (text must come back), and
  the first passing candidate wins — endpoint, request style and the
  auto-picked model are recorded for the whole run

**Request style follows the URL:**

- full path ending in `/chat` (LM Studio native) → `{"model", "input": [{"type": "text", "content": …}]}`; the whole conversation (system, first prompt, delivered pages, assistant replies) is rendered into one text with `[role]` labels
- otherwise (OpenAI-compatible) → `{"model", "messages": […], "temperature", "stream": false, "response_format": {"type": "json_schema", "json_schema": …}}` with the publish contract as the schema

`temperature` applies in OpenAI-compatible mode only; the native body carries
only `model` and `input`.

**Responses** in either shape — OpenAI `choices[0].message.content` or the
native `output[]` message list — are normalized to the same extracted text;
`usage` counts are kept when present and feed the token guardrails.

**Session contract:** the model returns one JSON object per turn —
`{"action": "read_page", "page": N}` or `{"action": "publish", "identity": {…}}`.
Guardrails: one invalid-JSON retry; duplicate/nonexistent-page stalls flip into
forced publish; `remind_at` nudges and `force_publish_at` forces at estimated
token totals (two more page turns after force, then give up); `max_turns` caps
the session unless negative. Give-up paths end in a flagged `unknown`
sidecar.

**Chatlog mode** (`--auto-chatlog`): when the history grows past the
watermarks it is folded into a context channel parallel to the page context —
`remind_at` triggers a summary side-call whose JSON digest, together with the
read progress, is merged into the tail-page section of the re-issued opening
prompt (the dialogue channel carries no digest messages; soft boundary:
`messages.json` keeps every raw message); `force_publish_at` rolls without a
digest when no valid one exists; `max_turns` counts rolls (negative =
unlimited) and, once reached, the watermarks fall back to their original
meanings above.

## Sidecar format

`<name>.publish.json` is created next to every target, validated with `schema.py` before writing; a failed validation writes no file:

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
- `flags.password_protected` only signals encryption — the working password is not persisted
- `warnings` includes the extractor's own notes, passed through unchanged

## Project layout

```
├── main.py                        # unified entry point (delegates to scripts/cli.py)
├── scripts/                       # pipeline stages (do not import each other)
│   ├── cli.py                     # orchestrator: scan / show / check / export
│   ├── run_extractor.py           # extractor runner (subprocess entry, contract validation)
│   ├── context_builder.py         # extracted/ → paged context
│   ├── ai_identify.py             # recognition session engine
│   ├── backend.py                 # AI server/API connection (endpoint auto-identification)
│   ├── publisher.py               # template fill → validate → sidecar
│   ├── run_logger.py              # run report assembler (pure disk reader)
│   ├── schema.py                  # sidecar contract (category enum, threshold, validation)
│   └── paths.py                   # project path constants
├── extractors/                    # extractor directories (pluggable, self-contained)
│   └── default/                   # extractor.py + config.json + self-held password.json
├── jsons/                         # static contract layer (human-edited, program-read)
└── runs/                          # per-scan archives (not tracked)
```

## Safety boundaries

- Member paths are normalized; `..`/absolute-path members are skipped; sampled extraction is bounded by the per-file (256KB), cumulative (4MB), file-count (8) and entry-list (30k, truncated) caps
- Member files are not executed, macros are not parsed; only text is read
- The AI reads pages and publishes an identity through the JSON contract; program-side fields (hashes, structure, anchoring) are not sent to the model
- Invalid AI output degrades to a flagged `unknown` sidecar
