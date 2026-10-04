"""Regenerates the README screenshots from real runs.

Starts the real server (embedded sandbox), drives a few runs through the real
LangGraph agent, real Chromium and the live sandbox, and photographs the UI.
The model's decisions come from the scripted policy used by the tests, so the
screenshots are reproducible without an API key. Nothing here is used by the
product itself.

    python scripts/capture_screenshots.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8420
BASE = f"http://127.0.0.1:{PORT}"
TMP = tempfile.mkdtemp()
os.environ.update({"EMBED_SANDBOX": "1", "SANDBOX_URL": BASE, "RUNS_DIR": os.path.join(TMP, "runs"),
                   "SANDBOX_DB": os.path.join(TMP, "sandbox.db"), "OLLAMA_MODEL": "gpt-oss:120b",
                   "OLLAMA_API_KEY": "screenshot-placeholder"})
sys.path[:0] = [ROOT, os.path.join(ROOT, "tests")]

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

from scripted_policy import InvoicePolicy  # noqa: E402
from worker import api  # noqa: E402
from worker.llm.scripted import ScriptedLLM  # noqa: E402

OUT = os.path.join(ROOT, "docs", "screenshots")


def use_policy(policy) -> None:
    api.manager.llm_factory = lambda: ScriptedLLM(policy)
    api.manager.chat_model_factory = lambda: None


async def wait_for(client, rid, pred, timeout=120):
    for _ in range(int(timeout / 0.5)):
        r = (await client.get(f"{BASE}/api/runs/{rid}")).json()
        if pred(r):
            return r
        await asyncio.sleep(0.5)
    raise TimeoutError(rid)


async def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    server = uvicorn.Server(uvicorn.Config(api.app, host="127.0.0.1", port=PORT, log_level="warning"))
    srv = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.1)

    async with httpx.AsyncClient(timeout=60) as c, async_playwright() as p:
        browser = await p.chromium.launch()
        ctx = await browser.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=2)
        page = await ctx.new_page()

        async def shoot(name, url=None, wait=1.2, full=False):
            if url:
                await page.goto(url)
            await page.wait_for_timeout(int(wait * 1000))
            await page.screenshot(path=os.path.join(OUT, name), full_page=full)
            print("saved", name)

        # 1. Composer
        await c.post(f"{BASE}/api/sandbox/reset", json={"seed": 7, "chaos": {}})
        truth = (await c.get(f"{BASE}/acme/admin/state", headers={"x-admin-token": os.environ["SANDBOX_ADMIN_TOKEN"]})).json()["truth"]
        await shoot("composer.png", f"{BASE}/#/")

        # 2. A run where the ERP lies about saving: caught by verification, repaired, verified
        await c.post(f"{BASE}/api/sandbox/reset", json={"seed": 7, "chaos": {"lying_ui": True, "modal": True}})
        vendor = truth["vendors"]["email"]
        use_policy(InvoicePolicy(vendor, BASE))
        task = (f"Find the latest invoice from {vendor}, extract the amount and due date, enter it into our ERP "
                f"and tell me once it is done.")
        rid = (await c.post(f"{BASE}/api/runs", json={"task": task})).json()["id"]
        await page.goto(f"{BASE}/#/run/{rid}")
        await wait_for(c, rid, lambda r: r["status"] in ("done", "failed"))
        await shoot("run-verified.png", wait=1.5)
        await page.click('[data-tab="facts"]')
        await shoot("run-facts.png", wait=0.6)
        await page.click('[data-tab="checks"]')
        await shoot("run-checks.png", wait=0.6)
        await page.click('[data-step="10"]')
        await page.click('[data-step="10"]')
        await shoot("run-step-detail.png", wait=0.8)

        # 3. Approval: a bill over the policy limit pauses the graph until a human decides
        await c.post(f"{BASE}/api/sandbox/reset", json={"seed": 7, "chaos": {}})
        big = truth["vendors"]["big"]
        use_policy(InvoicePolicy(big, BASE))
        rid2 = (await c.post(f"{BASE}/api/runs", json={"task": f"Enter the newest {big} invoice into the ERP."})).json()["id"]
        await page.goto(f"{BASE}/#/run/{rid2}")
        await wait_for(c, rid2, lambda r: r["status"] == "waiting_for_user")
        await shoot("run-approval.png", wait=1.5)
        await c.post(f"{BASE}/api/runs/{rid2}/reply", json={"answer": "approve"})
        await wait_for(c, rid2, lambda r: r["status"] in ("done", "failed"))

        # 4. Light theme and phone width
        await page.goto(f"{BASE}/#/run/{rid}")
        await page.evaluate("localStorage.setItem('proof-theme', 'light')")
        await page.reload()
        await page.click('[data-tab="facts"]')
        await shoot("run-light.png", wait=1.5)
        await page.set_viewport_size({"width": 390, "height": 844})
        await page.reload()
        await shoot("mobile.png", wait=1.5)
        await page.evaluate("localStorage.setItem('proof-theme', 'dark')")
        await browser.close()

    server.should_exit = True
    await srv


if __name__ == "__main__":
    asyncio.run(main())
