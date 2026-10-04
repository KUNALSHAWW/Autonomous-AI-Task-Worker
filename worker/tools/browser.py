"""A real Chromium session driven through Playwright.

Design choices:
  * The model sees a distilled text view with numbered elements (see distill.js),
    never raw HTML or screenshots. Screenshots are kept as evidence only.
  * Every network request goes through `_route`, which enforces the allowed
    origins, the read-only mode and the policy gate in code.
  * Each action reports what changed (URL, status, alerts, new elements), so
    "nothing happened" is visible to both the model and the guard.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urljoin, urlparse

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright
from playwright.async_api import Error as PWError
from playwright.async_api import TimeoutError as PWTimeout

from ..environment import Vault
from ..gate import SAFE_METHODS, Decision, Gate

DISTILL_JS = open(os.path.join(os.path.dirname(__file__), "distill.js")).read()


class AmbiguousField(Exception):
    pass


@dataclass
class PageView:
    url: str
    title: str
    text: str
    fields: dict
    seen: list
    status: int | None
    fingerprint: str


@dataclass
class ActionResult:
    ok: bool
    message: str
    error_kind: str | None = None
    view: PageView | None = None
    blocked: Decision | None = None
    downloads: list[str] = field(default_factory=list)


class BrowserSession:
    def __init__(self, gate: Gate, vault: Vault, workdir: str, headless: bool = True):
        self.gate = gate
        self.vault = vault
        self.workdir = workdir
        self.headless = headless
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.view: PageView | None = None
        self.field_labels: dict = {}
        self.last_status: int | None = None
        self.pending_blocks: list[Decision] = []
        self.new_downloads: list[str] = []
        self._write_by_request: dict = {}
        os.makedirs(os.path.join(workdir, "files"), exist_ok=True)
        os.makedirs(os.path.join(workdir, "shots"), exist_ok=True)

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self._pw = await async_playwright().start()
        exe = os.environ.get("CHROMIUM_PATH") or None
        self._browser = await self._pw.chromium.launch(
            headless=self.headless, executable_path=exe,
            args=["--disable-dev-shm-usage", "--no-sandbox", "--disable-gpu", "--disable-extensions",
                  "--no-zygote", "--renderer-process-limit=1", "--js-flags=--max-old-space-size=128",
                  "--disable-features=site-per-process,IsolateOrigins,Translate,MediaRouter",
                  "--disable-site-isolation-trials",
                  "--disable-background-networking", "--mute-audio"])
        self.context = await self._browser.new_context(accept_downloads=True, viewport={"width": 1200, "height": 850})
        self.context.set_default_timeout(8000)
        await self.context.route("**/*", self._route)
        self.context.on("page", self._wire)  # popups and new tabs get the same download/response handling
        self.page = await self.context.new_page()
        self._wire(self.page)

    async def close(self) -> None:
        for obj in (self.context, self._browser):
            try:
                if obj:
                    await obj.close()
            except PWError:
                pass
        if self._pw:
            await self._pw.stop()

    def _wire(self, page: Page) -> None:
        page.on("response", self._on_response)
        page.on("requestfailed", self._on_request_failed)
        page.on("download", lambda d: asyncio.ensure_future(self._on_download(d)))
        page.on("dialog", lambda d: asyncio.ensure_future(d.accept()))

    # ------------------------------------------------------------------ network policy
    async def _route(self, route, request) -> None:
        url, method = request.url, request.method.upper()
        nav = self.gate.check_navigation(url)
        if nav.action == "deny":
            self.pending_blocks.append(nav)
            await self._refuse(route, request)
            return
        if method in SAFE_METHODS:
            if request.resource_type == "document":
                self.gate.note_read(url)
            await route.continue_()
            return
        fields = self._semantic_fields(request)
        decision = self.gate.check_write(method, url, fields)
        if decision.action == "allow" and decision.message == "sign-in":
            await route.continue_()
            return
        if decision.action == "allow":
            w = self.gate.record_write(method, url, fields, decision.fingerprint)
            self._write_by_request[id(request)] = w
            self._write_by_request[request.url + "|" + method] = w
            await route.continue_()
            return
        if decision.action == "dry_run":
            w = self.gate.record_write(method, url, fields, decision.fingerprint)
            w.outcome = "dry_run"
        self.pending_blocks.append(decision)
        await self._refuse(route, request)

    @staticmethod
    async def _refuse(route, request) -> None:
        # 204 on a navigation keeps the browser on the current page, so a blocked form
        # keeps its values and can be resubmitted once approved. Aborting would land on
        # an error page and lose the form.
        if request.is_navigation_request():
            await route.fulfill(status=204, body="")
        else:
            await route.fulfill(status=403, content_type="application/json",
                                body='{"error": "blocked by the worker policy gate"}')

    def _semantic_fields(self, request) -> dict:
        """Map posted field names to their visible labels so policy rules can read them."""
        body = request.post_data or ""
        ctype = (request.headers.get("content-type") or "").lower()
        raw: dict = {}
        if "json" in ctype:
            try:
                raw = json.loads(body) if body else {}
            except json.JSONDecodeError:
                raw = {"_body": body[:500]}
        elif body:
            raw = dict(parse_qsl(body, keep_blank_values=True))
        out = {}
        for k, v in raw.items():
            label = self.field_labels.get(k)
            out[label or k] = v
        return out

    def _on_response(self, response) -> None:
        req = response.request
        w = self._write_by_request.pop(id(req), None) or self._write_by_request.pop(req.url + "|" + req.method.upper(), None)
        if w:
            w.status = response.status
            w.outcome = self.gate.classify(response.status)
        if self.page and req.is_navigation_request() and response.frame == self.page.main_frame:
            self.last_status = response.status

    def _on_request_failed(self, request) -> None:
        w = self._write_by_request.pop(id(request), None)
        if w and w.outcome == "pending":
            w.outcome = "failed"

    async def _on_download(self, download) -> None:
        name = re.sub(r"[^\w.\-]+", "_", download.suggested_filename or "download.bin")
        path = os.path.join(self.workdir, "files", name)
        try:
            await download.save_as(path)
            self.new_downloads.append(name)
        except PWError:
            pass

    # ------------------------------------------------------------------ observation
    async def snapshot(self) -> PageView:
        page = self.page
        same_page = self.view and urlparse(self.view.url).path == urlparse(page.url).path
        prev = self.view.seen if same_page else []
        try:
            data = await page.evaluate(DISTILL_JS, prev)
        except PWError:
            await asyncio.sleep(0.5)
            data = await page.evaluate(DISTILL_JS, prev)
        text = self.vault.scrub(data["text"])
        self.field_labels.update(data["fields"])
        fp = hashlib.sha1((page.url + text).encode()).hexdigest()[:12]
        self.view = PageView(page.url, data["title"], text, data["fields"], data["seen"], self.last_status, fp)
        return self.view

    async def screenshot(self, name: str) -> str | None:
        path = os.path.join(self.workdir, "shots", name)
        try:
            await self.page.screenshot(path=path, type="jpeg", quality=55)
            return path
        except PWError:
            return None

    async def _settle(self) -> None:
        try:
            await self.page.wait_for_load_state("domcontentloaded", timeout=8000)
        except PWTimeout:
            pass
        await asyncio.sleep(0.25)

    async def _after(self, before: PageView | None, message: str) -> ActionResult:
        await asyncio.sleep(0.15)
        await self._settle()
        # Redirect hops never reach the route handler, so re-check where we actually ended up.
        landed = self.gate.check_navigation(self.page.url)
        if landed.action == "deny":
            try:
                await self.page.goto(before.url if before else "about:blank", wait_until="domcontentloaded")
            except PWError:
                await self.page.goto("about:blank")
            self.pending_blocks = []
            self.view = None
            return ActionResult(False, f"{message}. BLOCKED: the page redirected somewhere you may not go. "
                                       f"{landed.message}", "blocked")
        downloads, self.new_downloads = self.new_downloads, []
        try:
            view = await self.snapshot()
        except PWError as e:
            return ActionResult(False, f"{message}. The page could not be read: {e}", "page_error")
        blocks, self.pending_blocks = self.pending_blocks, []
        changes = []
        if before and view.url != before.url:
            changes.append(f"URL changed to {view.url}")
        if view.status and view.status >= 400:
            changes.append(f"HTTP status {view.status}")
        if downloads:
            changes.append("Downloaded: " + ", ".join(f"files/{d}" for d in downloads) + " (use read_file to read it)")
        if before and view.fingerprint == before.fingerprint and not downloads and not blocks:
            changes.append("No visible change on the page")
        msg = message + (". " + ". ".join(changes) if changes else "")
        for b in blocks:
            if b.action == "approval":
                return ActionResult(False, msg, "needs_approval", view, b, downloads)
            if b.action == "dry_run":
                return ActionResult(True, msg + ". DRY RUN: the submission was recorded but not sent.", None, view, b, downloads)
            return ActionResult(False, msg + f". BLOCKED: {b.message}", "blocked", view, b, downloads)
        error_kind = None
        if view.status and view.status >= 500:
            error_kind = "server_error"
        elif view.status and view.status >= 400 and view.status not in (401, 422):
            error_kind = "http_error"
        return ActionResult(error_kind is None, msg, error_kind, view, None, downloads)

    # ------------------------------------------------------------------ actions
    def _abs(self, url: str) -> str:
        base = self.page.url if self.page and self.page.url.startswith("http") else ""
        return urljoin(base, url) if base else url

    async def goto(self, url: str) -> ActionResult:
        before = self.view
        self.pending_blocks = []
        if url.strip().lower() == "back":
            try:
                await self.page.go_back()
            except PWError as e:
                return ActionResult(False, f"Could not go back: {e}", "nav_error")
            return await self._after(before, "Went back")
        target = self._abs(url.strip())
        if not target.startswith("http"):
            return ActionResult(False, f"'{url}' is not a valid URL. Use a full URL or a path like /some/page", "invalid_args")
        decision = self.gate.check_navigation(target)
        if decision.action == "deny":
            return ActionResult(False, f"BLOCKED: {decision.message}", "blocked")
        self.last_status = None
        for attempt in range(2):
            try:
                await self.page.goto(target, wait_until="domcontentloaded", timeout=15000)
                break
            except PWError as e:
                if "Download is starting" in str(e) or "net::ERR_ABORTED" in str(e):
                    await asyncio.sleep(0.8)
                    break
                if attempt == 1:
                    return ActionResult(False, f"Navigation to {target} failed: {str(e).splitlines()[0]}", "nav_error")
                await asyncio.sleep(1.5)  # GET is idempotent, one automatic retry is safe
        return await self._after(before, f"Opened {target}")

    def _locator(self, element: str):
        element = str(element).strip().lstrip("[").rstrip("]")
        if element.isdigit():
            return self.page.locator(f'[data-aw="{element}"]'), f"[{element}]"
        return self.page.get_by_text(element, exact=False).first, f'"{element}"'

    async def click(self, element: str) -> ActionResult:
        before = self.view
        self.pending_blocks = []
        loc, label = self._locator(element)
        try:
            if await loc.count() == 0:
                return ActionResult(False, f"Element {label} does not exist on the current page. Element numbers "
                                           f"change after every page update; use the latest page view.", "stale_element")
            covered = await loc.evaluate(
                "el => { const r = el.getBoundingClientRect(); const t = document.elementFromPoint(r.left + Math.min(r.width/2, 20), r.top + Math.min(r.height/2, 10));"
                " if (!t || el.contains(t) || t.contains(el)) return ''; const d = t.closest('[role=dialog], .overlay, .modal, dialog') || t;"
                " return d.contains(el) ? '' : (d.innerText || d.tagName).trim().slice(0, 80); }")
            if covered:
                return ActionResult(False, f"Cannot click {label}: it is covered by an overlay/dialog "
                                           f"(\"{covered}\"). Close or dismiss that first.", "blocked_by_overlay")
            await loc.scroll_into_view_if_needed(timeout=3000)
            await loc.click(timeout=5000)
        except PWTimeout:
            return ActionResult(False, f"Clicking {label} timed out (the element may be disabled or hidden).", "timeout")
        except PWError as e:
            return ActionResult(False, f"Clicking {label} failed: {str(e).splitlines()[0]}", "click_error")
        return await self._after(before, f"Clicked {label}")

    async def fill_form(self, fields: dict, submit: bool | str = False) -> ActionResult:
        """Fill fields by element number or by (fuzzy) label. Selects and checkboxes are handled too."""
        before = self.view
        self.pending_blocks = []
        if not self.view:
            await self.snapshot()
        done, problems = [], []
        last_loc = None
        for key, value in fields.items():
            try:
                loc, desc = await self._find_field(str(key))
            except AmbiguousField as e:
                problems.append(str(e))
                continue
            if loc is None:
                problems.append(f"no field matches '{key}'")
                continue
            try:
                value_str = "" if value is None else str(value)
                real = self.vault.resolve(value_str, self.page.url)
            except (KeyError, PermissionError) as e:
                problems.append(f"{key}: {e}")
                continue
            try:
                tag = await loc.evaluate("el => el.tagName + ':' + (el.type || '')")
                if tag.startswith("SELECT"):
                    ok = await self._select(loc, real)
                    if isinstance(ok, str):
                        problems.append(ok)
                        continue
                    if not ok:
                        opts = await loc.evaluate("el => Array.from(el.options).map(o => o.text.trim())")
                        problems.append(f"'{value_str}' is not an option for {desc}. Options: {opts[:30]}")
                        continue
                elif tag == "INPUT:radio":
                    name = await loc.get_attribute("name")
                    group = self.page.locator(f'input[type=radio][name="{name}"]')
                    picked = False
                    for i in range(await group.count()):
                        r = group.nth(i)
                        lab = await r.evaluate("el => ((el.labels && el.labels[0] && el.labels[0].innerText) || el.value || '').trim()")
                        if lab.lower() == real.strip().lower() or (await r.get_attribute("value") or "").lower() == real.strip().lower():
                            await r.check(timeout=4000)
                            picked = True
                            break
                    if not picked:
                        problems.append(f"no radio option '{value_str}' for {desc}")
                        continue
                elif tag == "INPUT:checkbox":
                    want = real.strip().lower() not in ("", "false", "no", "0", "off", "unchecked")
                    await (loc.check(timeout=4000) if want else loc.uncheck(timeout=4000))
                else:
                    await loc.fill(real, timeout=4000)
                done.append(f"{desc} = {value_str if 'secret:' in value_str or tag != 'INPUT:password' else '********'}")
                last_loc = loc
            except PWTimeout:
                problems.append(f"{desc}: timed out (field may be covered, disabled or read-only)")
            except PWError as e:
                problems.append(f"{desc}: {str(e).splitlines()[0]}")
        msg = ("Filled " + "; ".join(done)) if done else "Nothing was filled"
        if problems:
            msg += ". Problems: " + "; ".join(problems)
            if self.view:
                msg += ". Available fields: " + ", ".join(sorted(set(self.view.fields.values())))
        if submit and last_loc is not None and not problems:
            try:
                btn = await self._submit_button(last_loc, submit)
                if btn is None:
                    await last_loc.press("Enter")
                    msg += ". Submitted with Enter"
                else:
                    await btn.click(timeout=5000)
                    msg += ". Clicked the submit button"
            except PWError as e:
                return ActionResult(False, msg + f". Submit failed: {str(e).splitlines()[0]}", "click_error")
        result = await self._after(before, msg)
        if problems and result.error_kind is None:
            result.ok, result.error_kind = False, "invalid_args"
        return result

    async def _submit_button(self, field_loc, submit):
        if isinstance(submit, str) and submit.strip().lstrip("[").rstrip("]").isdigit():
            return self._locator(submit)[0]
        handle = await field_loc.evaluate_handle(
            "el => { const f = el.form || el.closest('form'); if (!f) return null;"
            " return f.querySelector('button[type=submit], input[type=submit], button:not([type])'); }")
        el = handle.as_element()
        return el

    async def _find_field(self, key: str):
        key_clean = key.strip().lstrip("[").rstrip("]")
        if key_clean.isdigit():
            loc = self.page.locator(f'[data-aw="{key_clean}"]')
            return (loc, f"[{key_clean}]") if await loc.count() else (None, "")
        want = re.sub(r"[^a-z0-9#]+", " ", key.lower()).strip()
        best, best_score, tied = None, 0.0, []
        for name, label in (self.view.fields if self.view else {}).items():
            lab = re.sub(r"[^a-z0-9#]+", " ", label.lower()).strip()
            score = 0.0
            if lab == want or name.lower() == key.lower():
                score = 3
            elif lab.startswith(want) or want.startswith(lab):
                score = 2
            elif want in lab:
                score = 1.5
            else:
                overlap = set(want.split()) & set(lab.split())
                score = len(overlap) / max(1, len(set(want.split())))
            if score > best_score:
                best, best_score, tied = name, score, [name]
            elif score == best_score and score > 0 and name != best:
                tied.append(name)
        if best is None or best_score < 0.5:
            return None, ""
        if len(tied) > 1 and best_score < 3:
            labels = ", ".join(f'"{self.view.fields[n]}"' for n in tied)
            raise AmbiguousField(f"'{key}' matches several fields ({labels}); use the full label or the [number]")
        loc = self.page.locator(f'[name="{best}"]').first
        return loc, f'"{self.view.fields[best]}"'

    async def _select(self, loc, value: str) -> bool | str:
        opts = await loc.evaluate("el => Array.from(el.options).map(o => o.text.trim())")
        v = value.strip().lower()
        choice = next((o for o in opts if o.lower() == v), None)
        if not choice:
            partial = [o for o in opts if v and v in o.lower()]
            if len(partial) > 1:
                return f"'{value}' is ambiguous: it matches {partial}. Use the exact option text"
            choice = partial[0] if partial else None
        if not choice:
            return False
        await loc.select_option(label=choice, timeout=4000)
        return True

    async def read(self, page_no: int = 1, find: str = "", chunk: int = 7000) -> ActionResult:
        view = await self.snapshot()
        text = view.text
        if find:
            lines = text.splitlines()
            hits = [i for i, l in enumerate(lines) if find.lower() in l.lower()]
            if not hits:
                return ActionResult(True, f"'{find}' does not appear on the current page.", None, view)
            parts = []
            for i in hits[:8]:
                parts.append("\n".join(lines[max(0, i - 3): i + 4]))
            return ActionResult(True, f"Matches for '{find}':\n" + "\n...\n".join(parts), None, view)
        return ActionResult(True, f"Page part {page_no}", None, view)
