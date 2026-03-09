#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from playwright.async_api import BrowserContext, Page, async_playwright

DEFAULT_EXTS = ["pdf", "txt", "md", "csv", "json", "png", "jpg", "jpeg", "webp"]

# Selector candidates (Gemini UI may change)
ATTACH_INPUT_SELECTORS = [
    "input[type='file']",
]
PROMPT_BOX_SELECTORS = [
    "rich-textarea[contenteditable='true']",
    "div[contenteditable='true'][role='textbox']",
]
RESPONSE_SELECTORS = [
    "model-response .response-content",
    "message-content .model-response-text",
    "div[data-message-author-role='model']",
]
RESPONSE_COUNT_SELECTORS = [
    "model-response",
    "div[data-message-author-role='model']",
]
STOP_SELECTORS = [
    "button[aria-label*='Stop']",
    "button[aria-label*='停止']",
    "button:has-text('Stop')",
    "button:has-text('停止')",
]


@dataclass
class Job:
    idx: int
    path: Path


@dataclass
class Result:
    ok: bool
    input_path: str
    output_path: str | None = None
    error: str | None = None


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--cdp-url", required=True)
    p.add_argument("--target-url", required=True)
    p.add_argument("--input")
    p.add_argument("--input-dir")
    p.add_argument("--prompt")
    p.add_argument("--prompt-file")
    p.add_argument("--output-dir", required=True)
    p.add_argument("--concurrency", type=int, default=1)
    p.add_argument("--refresh-every", type=int, default=5)
    p.add_argument("--request-timeout-seconds", type=int, default=120)
    p.add_argument("--failed-jsonl")
    p.add_argument("--extensions", default=",".join(DEFAULT_EXTS))
    return p.parse_args()


def load_prompt(args: argparse.Namespace) -> str:
    if args.prompt:
        return args.prompt
    if args.prompt_file:
        return Path(args.prompt_file).read_text(encoding="utf-8")
    raise ValueError("Provide --prompt or --prompt-file")


def collect_inputs(args: argparse.Namespace) -> list[Path]:
    if bool(args.input) == bool(args.input_dir):
        raise ValueError("Provide exactly one of --input or --input-dir")

    exts = {e.strip().lower() for e in args.extensions.split(",") if e.strip()}
    if args.input:
        p = Path(args.input)
        if not p.exists() or not p.is_file():
            raise FileNotFoundError(str(p))
        return [p]

    root = Path(args.input_dir)
    if not root.exists() or not root.is_dir():
        raise NotADirectoryError(str(root))

    files = [p for p in sorted(root.iterdir()) if p.is_file() and p.suffix.lower().lstrip(".") in exts]
    return files


def safe_name(path: Path) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", path.name)


async def find_prompt_box(page: Page):
    for sel in PROMPT_BOX_SELECTORS:
        loc = page.locator(sel).last
        try:
            await loc.wait_for(state="visible", timeout=1200)
            return loc
        except Exception:
            pass
    raise RuntimeError("prompt box not found")


async def upload_file(page: Page, file_path: Path, timeout_ms: int):
    for sel in ATTACH_INPUT_SELECTORS:
        loc = page.locator(sel).first
        try:
            await loc.set_input_files(str(file_path), timeout=timeout_ms)
            return
        except Exception:
            pass
    raise RuntimeError("file input selector not found or upload failed")


async def latest_response_text(page: Page) -> str:
    for sel in RESPONSE_SELECTORS:
        loc = page.locator(sel)
        try:
            c = await loc.count()
            if c > 0:
                txt = (await loc.nth(c - 1).inner_text()).strip()
                if txt:
                    return txt
        except Exception:
            pass
    return ""


async def process_one(
    page: Page,
    worker_id: int,
    job: Job,
    prompt_tpl: str,
    out_dir: Path,
    timeout_s: int,
) -> Result:
    timeout_ms = timeout_s * 1000

    prompt = prompt_tpl.replace("{{file_name}}", job.path.name).replace("{{file_path}}", str(job.path))

    # upload
    await upload_file(page, job.path, timeout_ms)

    # send
    box = await find_prompt_box(page)
    await box.click()
    await box.fill(prompt)

    async def _response_count() -> int:
        c = 0
        for sel in RESPONSE_COUNT_SELECTORS:
            try:
                c = max(c, await page.locator(sel).count())
            except Exception:
                pass
        return c

    async def _stop_visible() -> bool:
        visible = False
        for sel in STOP_SELECTORS:
            try:
                visible = visible or await page.locator(sel).first.is_visible(timeout=100)
            except Exception:
                pass
        return visible

    response_count_before = await _response_count()
    await box.press("Enter")

    # wait response with safer completion rule:
    # 1) response count increased
    # 2) latest text stable for several ticks
    # 3) stop button not visible
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        if await _response_count() > response_count_before:
            break
        await page.wait_for_timeout(250)

    last = ""
    stable = 0
    while asyncio.get_event_loop().time() < deadline:
        cur = await latest_response_text(page)
        if cur and cur == last:
            stable += 1
        else:
            stable = 0
        last = cur or last

        if last and stable >= 4 and not await _stop_visible():
            break

        await page.wait_for_timeout(500)

    if not last:
        raise RuntimeError("no model response captured")

    out_file = out_dir / f"{safe_name(job.path)}.json"
    payload = {
        "ok": True,
        "input": {"path": str(job.path), "name": job.path.name},
        "worker_id": worker_id,
        "response_text": last,
        "ts": datetime.now(UTC).isoformat(),
    }
    out_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return Result(ok=True, input_path=str(job.path), output_path=str(out_file))


def append_failed_jsonl(path: Path, obj: dict[str, Any]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


async def worker_loop(
    worker_id: int,
    queue: asyncio.Queue[Job],
    context: BrowserContext,
    target_url: str,
    prompt_tpl: str,
    out_dir: Path,
    timeout_s: int,
    refresh_every: int,
    failed_jsonl: Path,
    summary: list[Result],
):
    page = context.pages[worker_id] if len(context.pages) > worker_id else await context.new_page()
    await page.goto(target_url, wait_until="domcontentloaded", timeout=timeout_s * 1000)
    handled = 0

    while True:
        try:
            job = queue.get_nowait()
        except asyncio.QueueEmpty:
            return

        try:
            res = await process_one(page, worker_id, job, prompt_tpl, out_dir, timeout_s)
            summary.append(res)
        except Exception as e:
            shot = out_dir / "debug" / f"worker{worker_id}-{safe_name(job.path)}.png"
            html_dump = out_dir / "debug" / f"worker{worker_id}-{safe_name(job.path)}.html"
            shot.parent.mkdir(parents=True, exist_ok=True)
            try:
                await page.screenshot(path=str(shot), full_page=True)
            except Exception:
                pass
            try:
                html_dump.write_text(await page.content(), encoding="utf-8")
            except Exception:
                pass
            append_failed_jsonl(
                failed_jsonl,
                {
                    "ts": datetime.now(UTC).isoformat(),
                    "input_path": str(job.path),
                    "worker_id": worker_id,
                    "stage": "process_one",
                    "error": str(e),
                    "page_url": page.url,
                    "debug_artifact": str(shot),
                    "debug_html": str(html_dump),
                },
            )
            summary.append(Result(ok=False, input_path=str(job.path), error=str(e)))
        finally:
            handled += 1
            queue.task_done()

            if refresh_every > 0 and handled % refresh_every == 0:
                try:
                    await page.reload(wait_until="domcontentloaded", timeout=timeout_s * 1000)
                except Exception:
                    await page.goto(target_url, wait_until="domcontentloaded", timeout=timeout_s * 1000)


async def main_async(args: argparse.Namespace):
    files = collect_inputs(args)
    if not files:
        raise RuntimeError("No input files found")

    prompt_tpl = load_prompt(args)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    failed_jsonl = Path(args.failed_jsonl) if args.failed_jsonl else (out_dir / "failed.jsonl")

    q: asyncio.Queue[Job] = asyncio.Queue()
    for i, p in enumerate(files):
        q.put_nowait(Job(idx=i, path=p))

    summary: list[Result] = []

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(args.cdp_url)
        context = browser.contexts[0] if browser.contexts else await browser.new_context()

        workers = [
            asyncio.create_task(
                worker_loop(
                    i,
                    q,
                    context,
                    args.target_url,
                    prompt_tpl,
                    out_dir,
                    args.request_timeout_seconds,
                    args.refresh_every,
                    failed_jsonl,
                    summary,
                )
            )
            for i in range(max(1, args.concurrency))
        ]
        await asyncio.gather(*workers)

    ok = sum(1 for r in summary if r.ok)
    fail = len(summary) - ok
    (out_dir / "summary.json").write_text(
        json.dumps(
            {
                "total": len(summary),
                "ok": ok,
                "failed": fail,
                "failed_jsonl": str(failed_jsonl),
                "ts": datetime.now(UTC).isoformat(),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    ns = parse_args()
    asyncio.run(main_async(ns))
