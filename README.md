English | [简体中文](README.zh-CN.md)

# AiScraper

AI-driven content scraper for archives and files: register targets, extract
text with a pluggable extractor, and let a local LLM identify each target
through a paged conversation, writing one `publish.json` next to each target.

## Layout

```
main.py                       entry point
config.json                   run configuration
scripts/
  cli.py                      scan / backend commands
  registry.py                 target registry (single source of target state)
  backend.py                  AI backend connection
  prompt_builder.py           prompts.json loader, {_contract:xxx} injection, scene assembly
  context_scanner.py          file -> text recognition
  context_builder.py          page packing (sentence-aligned pages, metadata tail page)
  ai_identify.py              conversation engine (chatlog-only, session-split archive)
  schema.py                   template-led validation
  run_extractor.py            extractor subprocess (exit code = success)
extractors/
  _contract.json              session_actions + chatlog_summary contracts
  default/
    extractor.py              zip/7z sampling extractor (self-contained)
    config.json               extraction caps and whitelists
    password.json             archive password candidates
    prompts.json              prompt registry: {role, frontier, text}
    publish.json              publish document template
runs/<run_id>/
  registry.json               targets and their states
  run.json                    extractor subprocess log (exit code, stderr, elapsed)
  context.json                paged context per target
  sessions.json               session boundaries and roll count per target
  messages.json               every raw message, tagged with its session
```

## Usage

```
python main.py backend                       check backend connectivity
python main.py scan <path>                   one pipeline: register -> extract -> identify -> publish
python main.py scan <path> --extractor default --workers 4
```

`scan` registers the files and folders directly under `<path>` (non-recursive;
hidden entries, `_`-prefixed entries, `runs/` and existing `*.publish.json`
are skipped), extracts each target in a subprocess pool, runs one identify
conversation per target, and writes `<target>.publish.json` next to each
target. Extractor success is judged by exit code alone; each run's stdout,
stderr and exit code land in `run.json`.

## Configuration (config.json)

| key | meaning |
|---|---|
| `concurrency` | parallel workers for `scan` |
| `ai.base_url` | OpenAI-compatible endpoint (required, non-empty) |
| `ai.model` | model id; empty picks the first model the endpoint lists |
| `ai.temperature` / `ai.timeout` | sampling and HTTP timeout |
| `ai.page_chars` | characters per content page |
| `ai.remind_at` | token watermark: ask the model for a chatlog summary and fold |
| `ai.force_publish_at` | token watermark: fold without a summary |
| `ai.max_turns` | fold cap; `-1` unlimited; when reached the watermarks fall back to prompt-publish / force-publish |
| `limits.extract_timeout_s` | per-target extractor subprocess timeout |
| `limits.max_text_file_bytes` | largest file packed into pages |
| `limits.sentence_max_ratio` | sentences longer than page_chars x ratio are dropped whole |

## Contracts

`extractors/_contract.json` holds both contracts in template form:
`session_actions` (every model reply: `read_page` / `publish` / `help`) and
`chatlog_summary` (the fold summary side call). `schema.py` checks documents
against a template — filled template values must match literally, empty values
are slots that must exist with the matching type — and derives the
`response_format` JSON schema sent to the backend from the same template.
Prompt entries split into frontier (the standing frame: contract references,
protocol labels, tokenized structure) and text (the registered content: role
persona, payload slots); frontiers reference contracts with `{_contract:<name>}`
and the rendered protocol is injected at load time.

## Conversation engine

The opening scene is `system | context | chatlog`; every model reply must be
one JSON object matching `session_actions`. `read_page` delivers a page,
`publish` ends the session with an identity, `help` re-sends the protocol
recap. When tokens reach `remind_at` the model is asked (in a side call) to
summarize the conversation; the digest folds into the chatlog section and a new
session opens. `force_publish_at` folds without waiting for a summary. Raw
messages are never discarded: `messages.json` keeps every message under its
session number, `sessions.json` records the boundaries.

Give-up paths (persistent malformed output, stalling page requests) publish a
degraded identity — category `unknown`, confidence 0 — with the reason recorded
in the document's `warnings`.

## Publish documents

`publish.json` is the shape of the result: `identity` (title, category,
summary, tags, language, confidence) filled by the model, `warnings` filled by
the program. A filled document is checked against the template before it is
written as `<target>.publish.json`.

## Extending

Copy `extractors/default/` to `extractors/<name>/`, keep `extractor.py` with an
`extract(in_path, out_dir) -> {"files_kept", "warnings"}` function (no project
imports; the module's own config/password files stay inside its folder), adjust
its `prompts.json` and `publish.json` as needed, then run
`scan <path> --extractor <name>`.

## License

Apache-2.0
