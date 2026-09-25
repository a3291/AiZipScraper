# AiZipScraper

[中文说明](README.zh-CN.md)

AI-powered scraper for archives — identifies the contents of zip / 7z archives, produces a structured metadata summary, and anchors it next to the file as a sidecar JSON.

Inspired by media library scrapers (like Plex): no unnamed archive black holes — every package gets a searchable profile of *what it is, what's inside, and what it's for*.

## Features

- **Two scrape depths**
  - `listing`: zero-extraction, reads only the archive's central directory (absolutely safe, fast preview)
  - `listing+sample` (default): whitelisted text files extracted in a sandbox, giving the AI real content evidence
- **Structured AI identification**: targets [LM Studio](https://lmstudio.ai/) by default; works with any OpenAI-compatible backend (Ollama `/v1`, vLLM, llama.cpp server…). Output is enforced by a JSON Schema — title / category / summary / tags / confidence are directly usable
- **Password polling**: provide a password file and encrypted archives are tried automatically; on success sampling proceeds and the password itself is never written to disk
- **Sidecar metadata anchoring**: results land in `<name>.zip.meta.json` keyed by SHA256 — idempotent skip on re-scrape, rename detection and path repair
- **Batch friendly**: serial processing, per-package failure isolation, cache skipping, a health check (orphan sidecars / hash drift / low confidence), and JSONL export
- **Hard-coded safety limits**: 256KB per file / 4MB total / 30k entries, normalized path validation against zip bombs and path traversal; nothing is executed, no macros parsed

## Installation

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12+.

```bash
git clone <repo-url> AiZipScraper
cd AiZipScraper
uv sync
```

## Quick start

```bash
# Batch-scrape a directory (default depth: listing+sample)
uv run python main.py scan D:/downloads --pwfile passwords.txt

# Fast preview mode (no extraction)
uv run python main.py scan D:/downloads --depth listing

# Display sidecars with human-readable labels
uv run python main.py show D:/downloads

# Health check: orphan sidecars / hash mismatch / low confidence
uv run python main.py check D:/downloads

# Export a JSONL digest for spreadsheets or search
uv run python main.py export D:/downloads -o summary.jsonl
```

Once installed as a CLI tool, `uv run scraper <subcommand>` works too.

## Configuration

The AI backend is configured via `scraper.json` in the repo root:

```json
{
  "ai": {
    "provider": "lmstudio",
    "base_url": "http://localhost:1234/v1",
    "model": "",
    "api_key": "lm-studio",
    "temperature": 0.2,
    "timeout": 300
  }
}
```

| Field | Description |
|-------|-------------|
| `provider` | `lmstudio` (default, `:1234/v1`) or `ollama` (`:11434/v1`); an explicit `base_url` takes precedence |
| `base_url` | Any OpenAI-compatible endpoint |
| `model` | Leave empty to auto-pick the first loaded model on the backend |
| `api_key` | Arbitrary for local backends |
| `temperature` / `timeout` | Sampling temperature and request timeout (seconds) |

Use `--config other.json` to swap configuration ad hoc.

## Sidecar format

A `<name>.<ext>.meta.json` is created next to every archive:

```json
{
  "schema_version": "1.0",
  "anchoring":  { "sha256": "…", "source_path": "…", "file_size": 0, "mtime": "…" },
  "scrape":     { "scraped_at": "…", "engine": "lmstudio:auto", "depth": "listing+sample", "confidence": 0.86 },
  "identity":   { "title": "…", "category": "dataset", "summary": "…", "tags": [], "language": [] },
  "structure":  { "entry_count": 0, "top_extensions": {}, "top_level_dirs": [], "notable_files": [] },
  "sample_evidence": [{ "file": "README.md", "excerpt": "…first 200 chars…" }],
  "flags":      { "password_protected": false, "exe_present": false, "macro_docs": false },
  "warnings":   []
}
```

- `anchoring.sha256` is the primary anchor: a renamed/moved file is still recognized by hash, no re-scrape needed
- `sample_evidence` keeps verbatim excerpts backing the AI's conclusions, for easy spot-checking
- The correct password is **never** written to the sidecar — only a `password_protected` flag

## Safety boundaries

Sample extraction happens in a throwaway sandbox directory under hard-coded, non-configurable limits:

- Whitelisted extensions (`.txt/.md/.json/.csv/…`) plus README / docs-like filenames
- 256KB per file, 4MB total, 30k entry cap
- Normalized member path validation; absolute paths and `..` traversal are rejected
- No execution, no macro parsing, no binary reading

## Project layout

```
├── main.py          # Unified entry point
├── cli.py           # scan / show / check / export subcommands
├── extractor.py     # Signal harvesting: listing + sandbox sampling + password polling
├── ai_identify.py   # OpenAI-compatible AI identification layer
├── sidecar.py       # Sidecar I/O / idempotency / rename repair / orphan detection
├── schema.py        # Sidecar contract (v1.0) and validation
├── scraper.json     # AI backend configuration
└── dev/             # Dev notes & test fixtures (not tracked)
```

## License

See [LICENSE.txt](LICENSE.txt).
