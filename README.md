# gg-web-batch-runner

Batch file analyzer via Gemini Web UI (CDP attach).

Behavior notes:
- Each worker keeps using the same conversation tab across requests.
- Page refresh only happens after a request is fully completed and `refresh_every` threshold is reached.
- No retry; failures are appended to `failed.jsonl`.

## Quick Start

1. Start Chrome with CDP and dedicated profile:

```powershell
& "C:\Program Files\Google\Chrome\Application\chrome.exe" `
  --remote-debugging-port=9222 `
  --user-data-dir="D:\nanobot-root\home\profiles\chrome-cdp-clean"
```

2. Create venv and install:

```bash
python -m venv .venv
. .venv/bin/activate   # Windows: .venv\Scripts\Activate.ps1
pip install -U pip playwright
```

3. Run:

```bash
python gg_batch.py \
  --input-dir ./inputs \
  --prompt-file ./prompt.txt \
  --output-dir ./outputs \
  --concurrency 3 \
  --refresh-every 5
```
