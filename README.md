[English](README.md) | 简体中文

# AiZipScraper

AI-driven content scraper for archives and files: register targets, pull text
through a pluggable extractor, let a local LLM identify each target through a
paged conversation, and write a `publish.json` next to the target.

## Layout

```
main.py                       entry
config.json                   run configuration
scripts/
  cli.py                      scan / backend commands
  ai_identify.py              conversation engine (chatlog-only mode, session archiving)
  paths.py                    layout constants, JSON IO
  schema.py                   template-led validation
  backend.py                  AI backend access
  prompt_builder.py           prompts.json loading, {_contract:xxx} injection
  registry.py                 target registry (single source of target state)
  run_extractor.py            worker: loads <name>/extractor.py, returns extract() result
  context_scanner.py          file -> text recognition
  context_builder.py          paged packing (catalog head page, sentence-aligned pages, metadata tail page)
extractors/
  _contract.json              session_actions + chatlog_summary contracts
  default/
    extractor.py              zip/7z sampling extractor
    config.json               extractor caps (per-file/total bytes, file count, listing)
    password.json             password candidates for encrypted archives
    prompts.json              conversation prompt registry ({role, frontier, text})
    publish.json              publish document template
runs/<run_id>/
  registry.json               target states
  run.json                    extractor subprocess records
  context.json                paged context per target
  chatlog.json                rolled-up chatlog document per target
  memo.json                   model working notes per target
  sessions.json               session boundaries per target
  messages.json               every raw message with its session number
  publishes/                  complete copies of each <target>.publish.json
```

## Install

```
pip install pyzipper py7zr
```

## Quick start

```
python main.py backend
python main.py scan D:\downloads --workers 4
```

Recommended: `python main.py scan D:\downloads --extractor default --workers 4`
(scan is non-recursive; one conversation per target; publishes
`<target>.publish.json` next to each target).

## Configuration (config.json)

| key | default | meaning |
|---|---|---|
| concurrency | 4 | parallel workers for extract and identify pools |
| ai.base_url | http://localhost:1234/v1 | OpenAI-compatible endpoint (required) |
| ai.model | — | model name; required, non-empty |
| ai.api_key | lm-studio | bearer token when the endpoint asks for one |
| ai.temperature | 0.7 | sampling temperature |
| ai.timeout | 300 | per-request timeout in seconds |
| ai.probe_timeout | 10 | endpoint probe timeout in seconds (backend check) |
| ai.page_chars | 3000 | page size in characters |
| ai.remind_at | 32000 | watermark: ask for a summary and roll (remind-to-publish when capped) |
| ai.force_publish_at | 60000 | watermark: roll raw messages in (force publish when capped) |
| ai.max_turns | -1 | roll cap; -1 unlimited |
| ai.estimate_chunk | 4 | chars-per-token estimate when usage is not reported |
| ai.publish_retries | 3 | re-input chances for a malformed publish after force |
| limits.extract_timeout_s | 1800 | extractor subprocess timeout |
| limits.sentence_max_ratio | 0.1 | max sentence length as a ratio of page_chars (oversized sentences are dropped whole) |
| limits.sniff_bytes | 8192 | binary-sniff head size in bytes; a head that looks binary makes the file unusable as text |

## Pipeline (scan)

1. **register** — first level under the path (hidden entries, `_`-prefixed
   names, `runs/`, existing `*.publish.json` skipped); each target becomes
   `t1..tN` in `registry.json` with an empty state until processed.
2. **extract** — `extractors/<name>/extractor.py` runs in a child process per
   target; its `extract(in_path, out_dir)` return value decides normality.
   Extractor names must not start with `_` or contain path separators.
   Abnormal (exception, timeout, worker death, non-dict return, zero kept
   files) marks the target `error` with the reason; no conversation runs.
   Every run is logged to `run.json` with `ok`, the returned `result` or the
   `error` reason, and elapsed time.
3. **context** — files under the target's `extracted/<tN>/` are recognized by
   `context_scanner` and packed into pages: a catalog head page, content
   pages, and a metadata tail page; archived as `context.json`.
   `_`-prefixed names are skipped.
4. **identify** — one conversation per target. Opening scene:
   system, context (head page: the catalog), optional `add`, chatlog (tail
   page), memo. Every raw message is archived to `messages.json` with its
   session number; `sessions.json` records session boundaries.
5. **publish** — the template `publish.json` is filled with the identity the
   model produced — publish is the pure content conclusion, no process notes
   from any domain (extractor notes stay in `run.json`, conversation notes go
   to the console) — checked against itself, and written as
   `<target>.publish.json`; a complete copy goes to
   `runs/<run_id>/publishes/`. A target ends as `ok` or `error`; an error
   at any stage skips its remaining stages.

## Conversation engine

The model drives with one JSON action per turn
(`{_contract:session_actions}`):

- `read_page` — request a context page (page 1 is the catalog; the metadata
  page is the last). Pages already read or out-of-range requests stall into
  publish.
- `read_chatlog` — request a chatlog page; chatlog pages stay rereadable.
- `read_memo` / `write_memo` — read the memo; replace it whole with the
  `memo` field. The memo is per target at `runs/<run_id>/memo.json`, persists
  across sessions and rolls, and is capped at `page_chars`; non-string or
  oversized writes are refused with `memo_reject`.
- `publish` — final identity. A malformed publish before any force aborts the
  target as `error` (no file written); after force it gets `publish_retries`
  re-input chances.
- `help` — protocol recap, any time.

Rolling: at `remind_at` the engine asks the model (side call,
`{_contract:chatlog_summary}`) for a summary, checks it against the
`chatlog_summary` contract, and rolls the session into the chatlog document;
at `force_publish_at` the session's raw messages roll in without a
summary (soft boundary — the messages survive in the chatlog document). Each
roll opens a new session. `max_turns: -1` rolls without limit; when the cap
is reached the watermarks fall back to their prompt-publish /
force-publish meanings.

Prompt entries are `{role, frontier, text}`: frontier is the standing frame
(contract references, protocol labels, page tokens), text is the registered
content (role persona, payload slots). Contracts live in
`extractors/_contract.json` and are injected through `{_contract:<name>}`.

## Publish format

`<target>.publish.json` mirrors the template: identity (title, category,
summary, tags, language, confidence) and nothing else. Each
published document is also copied under `runs/<run_id>/publishes/`. A target
whose publish fails the template check is marked `error` and no file is
written.

## Default extractor

Accepts only `.zip` / `.7z` archives; folders and plain files are refused
(and their targets end up `error`, no conversation). Members are sampled
under its own caps (`extractors/default/config.json`): per-file 256 KiB,
total 4 MiB, 8 files, 30k listing entries. Encrypted archives try the
password candidates in `extractors/default/password.json`. Quantity policy
belongs to the extractor, not the root config.
