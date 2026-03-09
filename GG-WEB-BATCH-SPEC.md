# GG Web Batch Runner Spec (v1.1)

## Goal
Use Gemini Web via CDP as a batch "file analysis API" runner.

## Confirmed Rules
1. Batch uses user `concurrency` to open matching number of tabs/workers.
2. Add `refresh_every` option: after K requests per worker, refresh page to avoid slowdown.
3. Output format is controlled by user prompt/template (system does not enforce response schema).
4. No retry.
5. Failed requests are recorded to a JSONL file (`failed.jsonl`).
6. No context retention between files (each file request is isolated).

## CLI Inputs
- `--cdp-url` (default: `http://127.0.0.1:9222`)
- `--target-url` (default: `https://gemini.google.com/`)
- `--input` single file path
- `--input-dir` directory path (mutually exclusive with `--input`)
- `--prompt` inline prompt
- `--prompt-file` prompt template file
- `--output-dir` output directory
- `--concurrency` default 1
- `--refresh-every` default 5 (0 = disabled)
- `--request-timeout-seconds` default 120
- `--failed-jsonl` default `<output-dir>/failed.jsonl`
- `--extensions` comma list default: pdf,txt,md,csv,json,png,jpg,jpeg,webp

## Outputs
- Per file: `<output-dir>/<safe_name>.json`
- Failures: `<failed-jsonl>` JSONL entries
- Summary: `<output-dir>/summary.json`

## Failure JSONL record
- `ts`, `input_path`, `worker_id`, `stage`, `error`, `page_url`, `debug_artifact`

## Notes
- CDP profile should be dedicated/clean for stability.
- This project assumes Gemini UI selectors may drift; selectors are centralized in code constants.
