#!/usr/bin/env python3
"""
Arkham Intelligence — Nobitex Transfer Scraper v2
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🎯 Key improvements:
  • Interactive MAX_ROWS: set limit at startup
  • Fallback preserves all data from Phase 2
  • Ctrl+C saves progress before exit
  • User confirmation before switching methods

Install:
  pip install playwright playwright-stealth httpx
  playwright install chromium
"""

import asyncio
import hashlib
import json
import csv
import time
import signal
from pathlib import Path
from typing import Optional
import sys

import httpx
from playwright.async_api import async_playwright, Page, BrowserContext, Request, Response
from playwright_stealth import Stealth

# ── Config ────────────────────────────────────────────────────────────────────
USER_DATA_DIR   = "./user_data"
TARGET_URL      = "https://intel.arkm.com/explorer/entity/nobitex"
API_URL         = "https://api.arkm.com/transfers"
OUTPUT_JSON     = "nobitex_transfers.json"
OUTPUT_CSV      = "nobitex_transfers.csv"

PAGE_SIZE       = 100 # 16 Arkham default size
REQUEST_DELAY   = 2.5
CF_WAIT_SECS    = 90

# ── Global state ──────────────────────────────────────────────────────────────
MAX_TRANSFERS   = None  # Set by user at startup
auth: dict = {
    "cookies":     {},
    "x_payload":   None,
    "x_timestamp": None,
    "user_agent":  None,
    "origin":      "https://intel.arkm.com",
    "referer":     "https://intel.arkm.com/",
}

# Shared transfer store (survives Phase 2 → Phase 2b transition)
global_transfers: dict[str, dict] = {}


# ══════════════════════════════════════════════════════════════════════════════
# User Prompts
# ══════════════════════════════════════════════════════════════════════════════

def prompt_max_transfers() -> Optional[int]:
    """Interactive limit selector at startup."""
    print("\n" + "=" * 70)
    print(" 🎯 TRANSFER LIMIT CONFIGURATION")
    print("=" * 70)
    print("Option 1: Fetch ALL transfers (no limit)")
    print("          ⏱️  Estimated time: 30-60 minutes for ~10,000 transfers")
    print("")
    print("Option 2: Set a custom limit (100, 500, 1000, 5000, etc.)")
    print("          ⏱️  Example: 500 transfers ≈ 2 minutes")
    print("")
    
    while True:
        choice = input("👉  Choose (1 or 2): ").strip()
        
        if choice == "1":
            print("\n✅  Unlimited mode — will fetch until all data exhausted\n")
            return None
        elif choice == "2":
            while True:
                try:
                    limit = int(input("👉  Enter max transfers: ").strip())
                    if limit > 0:
                        print(f"\n✅  Will stop after {limit:,} transfers\n")
                        return limit
                    else:
                        print("   ❌  Must be > 0, try again")
                except ValueError:
                    print("   ❌  Invalid number, try again")
        else:
            print("   ❌  Choose 1 or 2\n")


def prompt_continue_with_fallback(rows_so_far: int) -> bool:
    """Confirm before switching to click-based fallback."""
    print("\n" + "=" * 70)
    print(" ⚠️  SWITCHING TO FALLBACK MODE")
    print("=" * 70)
    print(f"✅  Already fetched: {rows_so_far:,} transfers (saved safely)")
    print("🔄  Switching to click-based browser pagination (slower)")
    print("🔐  Your existing data will NOT be lost")
    print("")
    
    choice = input("👉  Continue? (y/n): ").strip().lower()
    return choice in ("y", "yes")


def prompt_emergency_save(rows_fetched: int) -> bool:
    """Ask to save before exiting on Ctrl+C."""
    print("\n" + "=" * 70)
    print(" 💾 INTERRUPT DETECTED")
    print("=" * 70)
    print(f"📊  Transfers fetched so far: {rows_fetched:,}")
    print("💾  Save progress to JSON/CSV?")
    print("")
    
    choice = input("👉  Save? (y/n): ").strip().lower()
    return choice in ("y", "yes")


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 1 — Browser / Cloudflare bypass
# ══════════════════════════════════════════════════════════════════════════════

async def phase1_capture_auth() -> bool:
    """Open a real browser, wait for Cloudflare to clear, then intercept API request."""
    captured = asyncio.Event()

    async with Stealth().use_async(async_playwright()) as p:
        print("🌐  Launching persistent Chrome...")
        context: BrowserContext = await p.chromium.launch_persistent_context(
            user_data_dir=USER_DATA_DIR,
            headless=False,
            viewport={"width": 1280, "height": 900},
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
            ignore_default_args=["--enable-automation"],
        )

        page: Page = context.pages[0] if context.pages else await context.new_page()

        try:
            from playwright_stealth import stealth_async
            await stealth_async(page)
            print("✅  playwright-stealth applied")
        except ImportError:
            print("⚠️   playwright-stealth not installed (less effective against CF)")

        async def on_request(request: Request):
            if "api.arkm.com/transfers" in request.url and not captured.is_set():
                headers = await request.all_headers()
                auth["x_payload"]   = headers.get("x-payload")
                auth["x_timestamp"] = headers.get("x-timestamp")
                auth["user_agent"]  = headers.get("user-agent")
                print(f"\n🔑  X-Payload   : {auth['x_payload']}")
                print(f"🔑  X-Timestamp : {auth['x_timestamp']}")
                captured.set()

        async def on_response(response: Response):
            if "api.arkm.com/transfers" in response.url:
                cookies = await context.cookies()
                auth["cookies"] = {c["name"]: c["value"] for c in cookies}

        page.on("request",  on_request)
        page.on("response", on_response)

        print(f"🌐  Navigating → {TARGET_URL}")
        try:
            await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=60_000)
        except Exception as e:
            print(f"   (Navigation note: {e})")

        print(f"⏳  Waiting up to {CF_WAIT_SECS}s for Cloudflare…")
        deadline = time.time() + CF_WAIT_SECS
        while time.time() < deadline:
            title = await page.title()
            if "Just a moment" in title or "Checking" in title:
                print(f"   CF still active ({title}), waiting…")
                await asyncio.sleep(3)
            else:
                print(f"   Page title: '{title}' ✅")
                break

        try:
            await asyncio.wait_for(captured.wait(), timeout=30)
        except asyncio.TimeoutError:
            print("⚠️   API request not intercepted — try solving CF manually")
            await asyncio.sleep(30)

        cookies = await context.cookies()
        auth["cookies"] = {c["name"]: c["value"] for c in cookies}
        print(f"🍪  Cookies: {list(auth['cookies'].keys())}\n")

        await context.close()

    return captured.is_set()


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 1b — Quick payload refresh
# ══════════════════════════════════════════════════════════════════════════════

async def phase1_quick_refresh() -> bool:
    """Mini version: refresh stale X-Payload (headless OK since CF already cleared)."""
    captured = asyncio.Event()

    try:
        async with Stealth().use_async(async_playwright()) as p:
            context: BrowserContext = await p.chromium.launch_persistent_context(
                user_data_dir=USER_DATA_DIR,
                headless=True,
                viewport={"width": 1280, "height": 900},
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
                ignore_default_args=["--enable-automation"],
            )

            page: Page = context.pages[0] if context.pages else await context.new_page()

            try:
                from playwright_stealth import stealth_async
                await stealth_async(page)
            except ImportError:
                pass

            async def on_request(request: Request):
                if "api.arkm.com/transfers" in request.url and not captured.is_set():
                    headers = await request.all_headers()
                    auth["x_payload"]   = headers.get("x-payload")
                    auth["x_timestamp"] = headers.get("x-timestamp")
                    print(f"        🔑  {auth['x_payload'][:16]}…")
                    captured.set()

            page.on("request", on_request)

            await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=30_000)

            try:
                await asyncio.wait_for(captured.wait(), timeout=10)
            except asyncio.TimeoutError:
                await context.close()
                return False

            cookies = await context.cookies()
            auth["cookies"] = {c["name"]: c["value"] for c in cookies}

            await context.close()
            return True

    except Exception as e:
        print(f"        ❌  {e}")
        return False


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 2 — Offset-based API pagination
# ══════════════════════════════════════════════════════════════════════════════

def build_headers() -> dict:
    """Build request headers with current auth state."""
    headers = {
        "Accept":          "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin":          auth["origin"],
        "Referer":         auth["referer"],
        "Sec-Fetch-Dest":  "empty",
        "Sec-Fetch-Mode":  "cors",
        "Sec-Fetch-Site":  "same-site",
    }
    if auth["user_agent"]:
        headers["User-Agent"] = auth["user_agent"]
    if auth["x_payload"]:
        headers["X-Payload"]   = auth["x_payload"]
        headers["X-Timestamp"] = auth["x_timestamp"]
    return headers


def build_api_url(offset: int) -> str:
    return (
        f"{API_URL}?base=nobitex&flow=all&usdGte=1"
        f"&sortKey=time&sortDir=desc"
        f"&limit={PAGE_SIZE}&offset={offset}"
        f"&to=deposit%3Anobitex"
    )


async def phase2_paginate() -> Optional[dict]:
    """
    Offset pagination via httpx. Returns dict of transfers (or None to fallback).
    Updates global_transfers directly (preserves data if switching to fallback).
    """
    offset = 0
    request_count = 0
    refresh_interval = 25

    print(f"\n🚀  Starting offset pagination (PAGE_SIZE={PAGE_SIZE})…")

    async with httpx.AsyncClient(
        cookies=auth["cookies"],
        headers=build_headers(),
        timeout=httpx.Timeout(60.0, read=60.0),
        follow_redirects=True,
    ) as client:

        while True:
            # Proactive refresh
            if request_count > 0 and request_count % refresh_interval == 0:
                current_total = len(global_transfers)
                print(f"\n   🔄  Proactive refresh (req #{request_count}, total: {current_total:,})")
                if await phase1_quick_refresh():
                    client.headers.update(build_headers())
                    print(f"       ✅  Resumed\n")
                else:
                    print(f"       ⚠️   Refresh failed, continuing…\n")

            # Check limit
            if MAX_TRANSFERS and len(global_transfers) >= MAX_TRANSFERS:
                print(f"\n   🎯  Hit limit: {MAX_TRANSFERS:,} transfers")
                return global_transfers

            url = build_api_url(offset)
            print(f"   📄  offset={offset:>6} [req #{request_count:>3}]  ", end="", flush=True)

            # Fetch with retries
            for attempt in range(3):
                try:
                    resp = await client.get(url)
                    break
                except httpx.ReadTimeout:
                    print(f"⏳ retry {attempt+1}...", end="", flush=True)
                    await asyncio.sleep(5 * (attempt + 1))
            else:
                print("❌")
                return global_transfers

            request_count += 1

            # Handle 400 "old request" (emergency refresh)
            if resp.status_code == 400:
                body = resp.text[:200]
                if "old request" in body.lower():
                    print(f"⚠️  400 'old'")
                    print(f"       🔄  Emergency refresh…")
                    if await phase1_quick_refresh():
                        client.headers.update(build_headers())
                        print(f"       ✅  Retrying offset {offset}…")
                        await asyncio.sleep(2)
                        continue
                    else:
                        print(f"       ❌  Refresh failed → fallback")
                        return None
                else:
                    print(f"❌ 400 {body[:30]}")
                    return global_transfers

            # Handle other errors
            if resp.status_code == 401:
                print(f"❌ 401 → fallback")
                return None
            if resp.status_code == 429:
                print(f"⏳ 429")
                await asyncio.sleep(10)
                continue
            if resp.status_code != 200:
                print(f"❌ {resp.status_code}")
                return global_transfers

            # Parse response
            try:
                data = resp.json()
            except Exception as e:
                print(f"❌ JSON {e}")
                return global_transfers

            transfers = data.get("transfers", [])

            if not transfers:
                print(f"✅ empty (all pages done)")
                return global_transfers

            # Add new transfers
            new_count = 0
            for tx in transfers:
                tx_hash = tx.get("transactionHash") or f"__no_hash_{offset}_{new_count}"
                if tx_hash not in global_transfers:
                    global_transfers[tx_hash] = tx
                    new_count += 1

            total = len(global_transfers)
            print(f"+{new_count:>2}  (total: {total:>6,})")

            if MAX_TRANSFERS and total >= MAX_TRANSFERS:
                print(f"\n   🎯  Limit reached: {total:,} / {MAX_TRANSFERS:,}")
                return global_transfers

            if len(transfers) < PAGE_SIZE:
                print(f"   ✅  Last page")
                return global_transfers

            offset += PAGE_SIZE
            await asyncio.sleep(REQUEST_DELAY)


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 2b — Fallback: click-based pagination
# ══════════════════════════════════════════════════════════════════════════════

async def phase2b_click_pagination() -> dict:
    """
    Browser-based pagination. Continues from global_transfers (does NOT lose data).
    """
    page_num = 1

    async with Stealth().use_async(async_playwright()) as p:
        context: BrowserContext = await p.chromium.launch_persistent_context(
            user_data_dir=USER_DATA_DIR,
            headless=False,
            viewport={"width": 1280, "height": 900},
            ignore_default_args=["--enable-automation"],
        )
        page = context.pages[0] if context.pages else await context.new_page()

        try:
            from playwright_stealth import stealth_async
            await stealth_async(page)
        except ImportError:
            pass

        api_event = asyncio.Event()
        pending_transfers = []

        async def on_response(response: Response):
            if "api.arkm.com/transfers" in response.url:
                try:
                    data = await response.json()
                    txs = data.get("transfers", [])
                    pending_transfers.clear()
                    pending_transfers.extend(txs)
                    api_event.set()
                except Exception:
                    pass

        page.on("response", on_response)
        await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=60_000)
        await asyncio.sleep(CF_WAIT_SECS // 3)

        while True:
            # Check limit
            if MAX_TRANSFERS and len(global_transfers) >= MAX_TRANSFERS:
                print(f"\n   🎯  Limit reached: {len(global_transfers):,} / {MAX_TRANSFERS:,}")
                break

            # Wait for API response
            api_event.clear()
            try:
                await asyncio.wait_for(api_event.wait(), timeout=15)
            except asyncio.TimeoutError:
                print(f"   ⏱️  Page {page_num} timeout → stopping")
                break

            # Collect transfers
            new_count = 0
            for tx in pending_transfers:
                tx_hash = tx.get("transactionHash") or f"__p{page_num}_{new_count}"
                if tx_hash not in global_transfers:
                    global_transfers[tx_hash] = tx
                    new_count += 1

            total = len(global_transfers)
            print(f"   📄  Page {page_num:>4}: +{new_count:>3}  (total: {total:>6,})")

            if MAX_TRANSFERS and total >= MAX_TRANSFERS:
                print(f"   🎯  Limit reached")
                break

            # Click next button
            next_selectors = [
                "button[aria-label='Next page']",
                "[data-testid='pagination-next']",
                "//button[normalize-space(text())='>']",
            ]

            clicked = False
            for sel in next_selectors:
                try:
                    btn = page.locator(f"xpath={sel}" if sel.startswith("//") else sel)
                    if await btn.count() > 0 and await btn.is_enabled():
                        await btn.click()
                        clicked = True
                        page_num += 1
                        await asyncio.sleep(REQUEST_DELAY)
                        break
                except Exception:
                    continue

            if not clicked:
                print(f"   🔚  No next button → end of pages")
                break

        await context.close()

    return global_transfers


# ══════════════════════════════════════════════════════════════════════════════
# Export
# ══════════════════════════════════════════════════════════════════════════════
def flatten_tx(tx: dict, prefix: str = "") -> dict:
    """Recursively flatten nested dicts for CSV export."""
    out = {}
    for k, v in tx.items():
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out.update(flatten_tx(v, key))
        elif isinstance(v, list):
            out[key] = json.dumps(v)
        else:
            out[key] = v
    return out

def flatten_tx_(tx: dict) -> dict:
    """Flatten nested dicts for CSV."""
    out = {}
    for k, v in tx.items():
        if isinstance(v, dict):
            for k2, v2 in v.items():
                out[f"{k}.{k2}"] = v2
        elif isinstance(v, list):
            out[k] = json.dumps(v)
        else:
            out[k] = v
    return out

def save_results(transfers: dict):
    """Save to JSON and CSV."""
    if not transfers:
        print("⚠️   No transfers to save")
        return

    transfers_list = list(transfers.values())

    # JSON
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(transfers_list, f, indent=2, ensure_ascii=False)
    print(f"\n💾  JSON → {OUTPUT_JSON}  ({len(transfers_list):,} records)")

    # CSV
    flat = [flatten_tx(tx) for tx in transfers_list]
    all_keys = list(dict.fromkeys(k for row in flat for k in row))

    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=all_keys, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(flat)

    print(f"💾  CSV  → {OUTPUT_CSV}")


# ══════════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════════

async def main():
    global MAX_TRANSFERS

    print("=" * 70)
    print(" Arkham / Nobitex Transfer Scraper v2")
    print("=" * 70)

    # Prompt for limit
    MAX_TRANSFERS = prompt_max_transfers()

    # Phase 1
    auth_ok = await phase1_capture_auth()
    if not auth_ok:
        print("⚠️   Auth capture failed (continuing anyway)")

    # Phase 2 (httpx pagination)
    result = await phase2_paginate()

    # Phase 2b fallback (if needed)
    if result is None:
        rows_so_far = len(global_transfers)
        if rows_so_far > 0:
            if not prompt_continue_with_fallback(rows_so_far):
                print("❌  Aborted. Saving progress…")
                save_results(global_transfers)
                return
        print("↩️   Using click-based browser fallback…\n")
        await phase2b_click_pagination()

    # Save
    save_results(global_transfers)
    print(f"\n🎉  Done! {len(global_transfers):,} unique transfers scraped.")


def handle_interrupt(signum, frame):
    """Save on Ctrl+C."""
    rows = len(global_transfers)
    if rows > 0:
        print("\n")
        if prompt_emergency_save(rows):
            save_results(global_transfers)
            print("✅  Saved")
    print("\n👋  Good luck")
    sys.exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGINT, handle_interrupt)
    asyncio.run(main())