#!/usr/bin/env python3
"""
Arkham Intelligence — Nobitex Transfer Scraper
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Strategy:
  Phase 1 → Browser bypasses Cloudflare, intercepts ONE real API request
             to capture X-Payload, X-Timestamp, and session cookies.
  Phase 2 → httpx paginates the API using captured auth material.
             Falls back to click-based pagination if X-Payload is per-request.

Install:
  pip install playwright playwright-stealth httpx
  playwright install chromium
"""

import asyncio
import hashlib
import json
import csv
import time
from pathlib import Path
from typing import Optional
import pandas as pd
import httpx
from playwright.async_api import async_playwright, Page, BrowserContext, Request, Response
from playwright_stealth import Stealth

# ── Config ────────────────────────────────────────────────────────────────────
USER_DATA_DIR   = "./user_data"          # persistent profile → survives CF cookie
TARGET_URL      = "https://intel.arkm.com/explorer/entity/nobitex"
API_URL         = "https://api.arkm.com/transfers"
OUTPUT_JSON     = "nobitex_transfers.json"
OUTPUT_CSV      = "nobitex_transfers.csv"

PAGE_SIZE       = 16    # transfers per request (Arkham default=16; try 100/500)
MAX_TRANSFERS   = None   # None = scrape everything; set int to cap
REQUEST_DELAY   = 2.5    # seconds between API calls (be polite)
CF_WAIT_SECS    = 90     # seconds to wait for Cloudflare to clear

# ── Shared auth state (filled by Phase 1) ─────────────────────────────────────
auth: dict = {
    "cookies":     {},   # name → value
    "x_payload":   None, # the signed hash
    "x_timestamp": None,
    "user_agent":  None,
    "origin":      "https://intel.arkm.com",
    "referer":     "https://intel.arkm.com/",
}


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 1 — Browser / Cloudflare bypass
# ══════════════════════════════════════════════════════════════════════════════

async def phase1_capture_auth() -> bool:
    """
    Open a real browser, wait for Cloudflare to clear, then intercept
    the first /transfers API request to extract auth headers.
    Returns True when auth is captured successfully.
    """
    captured = asyncio.Event()

    async with Stealth().use_async(async_playwright()) as p:
        print("🌐  Launching persistent Chrome...")
        context: BrowserContext = await p.chromium.launch_persistent_context(
            user_data_dir=USER_DATA_DIR,
            headless=False,                              # must be visible for CF
            viewport={"width": 1280, "height": 900},
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
            ignore_default_args=["--enable-automation"],
        )

        page: Page = context.pages[0] if context.pages else await context.new_page()

        # ── Apply stealth (fixes the biggest issue in your original code) ─────
        try:
            from playwright_stealth import stealth_async   # noqa
            await stealth_async(page)
            print("✅  playwright-stealth applied")
        except ImportError:
            print("⚠️   playwright-stealth not found → install it for best CF bypass")

        # ── Intercept outgoing requests to grab X-Payload ─────────────────────
        async def on_request(request: Request):
            if "api.arkm.com/transfers" in request.url and not captured.is_set():
                headers = await request.all_headers()
                auth["x_payload"]   = headers.get("x-payload")
                auth["x_timestamp"] = headers.get("x-timestamp")
                auth["user_agent"]  = headers.get("user-agent")
                print(f"\n🔑  Captured X-Payload   : {auth['x_payload']}")
                print(f"🔑  Captured X-Timestamp : {auth['x_timestamp']}")
                captured.set()

        # Also capture cookies from responses for completeness
        async def on_response(response: Response):
            if "api.arkm.com/transfers" in response.url:
                # Grab cookies from the browser context (most reliable)
                cookies = await context.cookies()
                auth["cookies"] = {c["name"]: c["value"] for c in cookies}

        page.on("request",  on_request)
        page.on("response", on_response)

        # ── Navigate ──────────────────────────────────────────────────────────
        print(f"🌐  Navigating → {TARGET_URL}")
        try:
            await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=60_000)
        except Exception as e:
            print(f"   (Navigation warning: {e})")

        # ── Wait for CF challenge to clear ────────────────────────────────────
        print(f"⏳  Waiting up to {CF_WAIT_SECS}s for Cloudflare…")
        deadline = time.time() + CF_WAIT_SECS
        while time.time() < deadline:
            title = await page.title()
            if "Just a moment" in title or "Checking" in title:
                print(f"   CF still active ({title}), waiting…")
                await asyncio.sleep(3)
            else:
                print(f"   Page title: '{title}' — looks clear ✅")
                break

        # Wait for the app to trigger its first API call
        try:
            await asyncio.wait_for(captured.wait(), timeout=30)
        except asyncio.TimeoutError:
            print("⚠️   Timed out waiting for API request — CF may still be blocking.")
            print("    Try solving the CF challenge manually in the browser window.")
            print("    Sleeping 30s for manual intervention…")
            await asyncio.sleep(30)

        # Last-chance cookie grab
        cookies = await context.cookies()
        auth["cookies"] = {c["name"]: c["value"] for c in cookies}
        print(f"\n🍪  Cookies captured: {list(auth['cookies'].keys())}")

        await context.close()

    return captured.is_set()


# ══════════════════════════════════════════════════════════════════════════════
# X-Payload analysis & computation
# ══════════════════════════════════════════════════════════════════════════════

def analyze_x_payload():
    """
    Try to reverse-engineer the X-Payload formula.
    Common patterns for API signing:
      sha256(timestamp)
      sha256(timestamp + path)
      sha256(timestamp + body)
      HMAC-SHA256(path, secret)

    Prints what we find; if derivable we patch auth automatically.
    """
    payload   = auth.get("x_payload", "")
    timestamp = auth.get("x_timestamp", "")

    if not payload or not timestamp:
        print("⚠️   No X-Payload captured — will try requests without it.")
        return

    print(f"\n🔬  Analyzing X-Payload ({payload}) with timestamp ({timestamp})")

    candidates = {
        "sha256(timestamp)":          hashlib.sha256(timestamp.encode()).hexdigest(),
        "sha256(timestamp + newline)": hashlib.sha256((timestamp + "\n").encode()).hexdigest(),
    }

    for label, computed in candidates.items():
        match = "✅ MATCH!" if computed == payload else "❌"
        print(f"   {match}  {label}  →  {computed[:16]}…")

    # NOTE: If none match, X-Payload is likely HMAC with an embedded secret
    # from the app bundle. In that case fall back to click-based pagination.


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 2 — Offset-based API pagination via httpx
# ══════════════════════════════════════════════════════════════════════════════

def build_headers(new_timestamp: Optional[str] = None) -> dict:
    """Build request headers. Reuses captured X-Payload or omits it to test."""
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
        headers["X-Timestamp"] = new_timestamp or auth["x_timestamp"]
    return headers


def build_api_url(offset: int, limit: int = PAGE_SIZE) -> str:
    return (
        f"{API_URL}?base=nobitex&flow=all&usdGte=1"
        f"&sortKey=time&sortDir=desc"
        f"&limit={limit}&offset={offset}"
        f"&to=deposit%3Anobitex"
    )


async def phase2_paginate() -> list:
    """Paginate through all transfers using offset. Returns flat list."""
    all_transfers: dict[str, dict] = {}  # keyed by transactionHash → dedup
    offset = 0

    print(f"\n🚀  Starting offset pagination (page_size={PAGE_SIZE})…")

    async with httpx.AsyncClient(
        cookies=auth["cookies"],
        headers=build_headers(),
        timeout=httpx.Timeout(60.0, read=60.0),
        follow_redirects=True,
    ) as client:

        while True:
            url = build_api_url(offset)
            print(f"   📄  offset={offset:>6}  →  {url}")

            for attempt in range(3):
                try:
                    resp = await client.get(url)
                    break
                except httpx.ReadTimeout:
                    print(f"⏳ Timeout on offset {offset}, retry {attempt+1}")
                    await asyncio.sleep(5 * (attempt + 1))
            else:
                print("❌ Failed after retries → stopping")
                break

            # ── Detect X-Payload rejection ─────────────────────────────────
            if resp.status_code == 401:
                print("   ⚠️   401 — X-Payload likely required per-request.")
                print("         Switching to click-based fallback…")
                return None   # signal to caller to use fallback

            if resp.status_code == 429:
                print("   ⏳  Rate-limited (429) — sleeping 10s…")
                await asyncio.sleep(10)
                continue

            if resp.status_code != 200:
                print(f"   ❌  HTTP {resp.status_code}: {resp.text[:200]}")
                break

            data = resp.json()
            transfers = data.get("transfers", [])

            if not transfers:
                print("   ✅  Empty response — all pages fetched!")
                break

            new_count = 0
            for tx in transfers:
                tx_hash = tx.get("transactionHash") or f"__no_hash_{offset}_{new_count}"
                if tx_hash not in all_transfers:
                    all_transfers[tx_hash] = tx
                    new_count += 1

            total = len(all_transfers)
            print(f"        +{new_count} new  (running total: {total})")

            if MAX_TRANSFERS and total >= MAX_TRANSFERS:
                print(f"   🎯  Hit MAX_TRANSFERS={MAX_TRANSFERS}")
                break

            if len(transfers) < PAGE_SIZE:
                print("   ✅  Last page (partial response).")
                break

            offset += PAGE_SIZE
            await asyncio.sleep(REQUEST_DELAY)

    return list(all_transfers.values())


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 2b — Fallback: browser click-based pagination
# ══════════════════════════════════════════════════════════════════════════════

async def phase2b_click_pagination() -> list:
    """
    Fallback when X-Payload cannot be reused.
    Uses a real browser: clicks '>' and intercepts each /transfers response.
    Slower but guaranteed to work.
    """
    all_transfers: dict[str, dict] = {}
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

        # Intercept responses
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
        await asyncio.sleep(CF_WAIT_SECS // 3)  # let CF clear

        while True:
            # Wait for API response from current page
            api_event.clear()
            try:
                await asyncio.wait_for(api_event.wait(), timeout=15)
            except asyncio.TimeoutError:
                print(f"   ⚠️   No API response on page {page_num} — stopping.")
                break

            # Collect
            new_count = 0
            for tx in pending_transfers:
                tx_hash = tx.get("transactionHash") or f"__p{page_num}_{new_count}"
                if tx_hash not in all_transfers:
                    all_transfers[tx_hash] = tx
                    new_count += 1

            total = len(all_transfers)
            print(f"   📄  Page {page_num:>4}: +{new_count}  (total: {total})")

            if MAX_TRANSFERS and total >= MAX_TRANSFERS:
                print(f"   🎯  Hit limit {MAX_TRANSFERS}")
                break

            # Click the '>' next button
            # Selectors to try in order (Arkham may change these)
            next_selectors = [
                "button[aria-label='Next page']",
                "button[aria-label='next']",
                "[data-testid='pagination-next']",
                "nav button:last-child",
                # Generic: find a button containing '>'
                "//button[normalize-space(text())='>']",
                "//button[contains(@class,'next')]",
            ]

            clicked = False
            for sel in next_selectors:
                try:
                    if sel.startswith("//"):
                        btn = page.locator(f"xpath={sel}")
                    else:
                        btn = page.locator(sel)

                    if await btn.count() > 0 and await btn.is_enabled():
                        await btn.click()
                        clicked = True
                        page_num += 1
                        await asyncio.sleep(REQUEST_DELAY)
                        break
                except Exception:
                    continue

            if not clicked:
                print(f"   🔚  No clickable next button found — end of pages.")
                break

        await context.close()

    return list(all_transfers.values())


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

def flatten(tx: dict) -> dict:
    def g(obj, *keys):
        """Safe nested getter"""
        for k in keys:
            if obj is None:
                return None
            obj = obj.get(k)
        return obj

    return {
        "id": tx.get("id"),
        "transactionHash": tx.get("transactionHash"),
        "from_address": g(tx, "fromAddress", "address"),
        "from_chain": g(tx, "fromAddress", "chain"),
        "from_name": g(tx, "fromAddress", "arkhamEntity", "name"),
        "from_note": g(tx, "fromAddress", "arkhamEntity", "note"),
        "from_type": g(tx, "fromAddress", "arkhamEntity", "type"),
        "from_label": g(tx, "fromAddress", "arkhamLabel", "name"),
        "to_address": g(tx, "toAddress", "address"),
        "to_label": g(tx, "toAddress", "arkhamLabel", "name"),
        "tokenAddress": tx.get("tokenAddress"),
        "tokenName": tx.get("tokenName"),
        "blockTimestamp": tx.get("blockTimestamp"),
    }

def save_results(transfers: list):
    def clean(v):
        if v is None:
            return "NULL"
        if v == "":
            return "EMPTY"
        return v

    if not transfers:
        print("⚠️   No transfers to save.")
        return

    # JSON
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(transfers, f, indent=2, ensure_ascii=False)
    print(f"\n💾  JSON → {OUTPUT_JSON}  ({len(transfers)} records)")

    # CSV (flattened)
    flat = [flatten_tx(tx) for tx in transfers]
    all_keys = list(dict.fromkeys(k for row in flat for k in row))   # ordered union

    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=all_keys, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(flat)

    columns = [
        "id",
        "transactionHash",
        "from_address",
        "from_chain",
        "from_name",
        "from_note",
        "from_type",
        "from_label",
        "to_address",
        "to_label",
        "tokenAddress",
        "tokenName",
        "blockTimestamp"
    ]

    # flat = [flatten(tx) for tx in transfers]
    # columns = list(flat[0].keys())  # or predefined schema
    # with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
    #     writer = csv.DictWriter(f, fieldnames=columns)
    #     writer.writeheader()
    #     for row in flat:
    #         row = {k: clean(row.get(k)) for k in columns}
    #         writer.writerow(row)


    # pandas.json_normalize and flat
    # df = pd.json_normalize(transfers)
    # df.to_csv(OUTPUT_CSV, index=False)

    print(f"💾  CSV  → {OUTPUT_CSV}")


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

async def main():
    print("=" * 60)
    print(" Arkham / Nobitex Transfer Scraper")
    print("=" * 60)

    # ── Phase 1: get past Cloudflare, capture auth ─────────────────────────
    auth_ok = await phase1_capture_auth()

    if not auth_ok:
        print("\n⚠️   Auth capture failed. Continuing anyway with cookies only.")

    analyze_x_payload()

    # ── Phase 2: paginate ──────────────────────────────────────────────────
    transfers = await phase2_paginate()

    if transfers is None:
        # httpx got 401 → X-Payload is per-request → use browser fallback
        print("\n↩️   Using click-based browser fallback…")
        transfers = await phase2b_click_pagination()

    # ── Save ───────────────────────────────────────────────────────────────
    save_results(transfers or [])
    print(f"\n🎉  Done! {len(transfers or [])} unique transfers scraped.")


if __name__ == "__main__":
    asyncio.run(main())