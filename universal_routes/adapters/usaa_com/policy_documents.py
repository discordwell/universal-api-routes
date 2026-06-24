"""USAA (www.usaa.com) — fetch auto-policy documents.

Ported from infer-takehome/backend/carriers/usaa.py — same selectors,
generalized to the Route ABC. The original (``UsaaFlow``) returned
``(list[Document], dict[doc_id, bytes])``; this version emits a flat
``list[Artifact]`` per the new contract.

USAA's Akamai edge fails plain headless Chromium with
``ERR_HTTP2_PROTOCOL_ERROR``; a Chrome-over-CDP context with a stealth
init-script (set via ``_launch_chrome_cdp`` in :meth:`context_options`)
reaches the login form. The OS-browser fallback (cliclick / osascript)
from the original repo is intentionally dropped here — it required
project-specific settings (``USAA_LOGIN_DRIVER``, profile paths) that
aren't part of this library. ``login_context`` is kept on the subclass
so future phases can swap in alternate login drivers without changing
the orchestrator.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator

import httpx
from playwright.async_api import BrowserContext, Locator, Page

from ...base import (
    Artifact,
    Route,
    filename_from_content_disposition,
    is_pdf_document,
    sanitize_html_for_debug,
)

log = logging.getLogger(__name__)

ROUTE_META = {
    "domain": "usaa.com",
    "targets": [
        "policy declarations",
        "auto policy documents",
        "auto insurance documents",
        "renewal packet",
    ],
    "aliases": [
        "dec page",
        "policy",
        "auto policy",
        "renewal",
    ],
    "description": "USAA auto-policy documents (declarations, renewal packets, ID cards).",
    "mfa_style": "code_input",
}

LOGIN_URL = "https://www.usaa.com/my/logon"
DASHBOARD_URL_CANDIDATES = (
    "https://www.usaa.com/my/usaa",
    "https://www.usaa.com/my/accounts",
    "https://www.usaa.com/",
)
DOCS_URL_CANDIDATES = (
    "https://www.usaa.com/my/auto-insurance/",
    "https://www.usaa.com/my/auto-insurance",
    "https://www.usaa.com/inet/ent_edde/ViewMyDocuments",
    "https://www.usaa.com/inet/gas_pc_pas/GyMemberAutoHistoryServlet",
    (
        "https://www.usaa.com/inet/gas_pc_pas/GyMemberAutoIdServlet"
        "?action=INIT&proofOfInsuranceType=IDCARD"
    ),
    "https://www.usaa.com/my/documents",
    "https://www.usaa.com/my/documents?akredirect=true",
    "https://www.usaa.com/inet/wc/document_center",
    "https://www.usaa.com/my/insurance",
    "https://www.usaa.com/inet/wc/insurance_auto_main",
)
DOCUMENT_CENTER_URL_CANDIDATES = (
    "https://www.usaa.com/my/documents?akredirect=true",
    "https://www.usaa.com/my/documents",
    "https://www.usaa.com/inet/wc/document_center",
)
POLICY_DOCUMENT_SEARCH_TERMS = (
    "Renewal",
    "Renew",
    "Policy",
    "Policy Packet",
    "Declarations",
    "Declaration",
    "Initial",
    "New Policy",
)
DEBUG_DIR = Path("/tmp")
USAA_CHROME_PROFILE_DIR = (
    Path.home() / ".cache" / "universal-api-routes" / "usaa-chrome"
)
MFA_CODE_INPUT_SELECTOR = (
    "input[autocomplete='one-time-code']:visible, "
    "input[inputmode='numeric']:visible, "
    "input[name*='code' i]:visible, "
    "input[id*='code' i]:visible"
)

STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5].map(() => ({})) });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
Object.defineProperty(navigator, 'platform', { get: () => 'MacIntel' });
window.chrome = { runtime: {} };
const origQuery = navigator.permissions ? navigator.permissions.query : null;
if (origQuery) {
  navigator.permissions.query = (params) =>
    params.name === 'notifications'
      ? Promise.resolve({ state: Notification.permission })
      : origQuery(params);
}
"""


@dataclass(frozen=True)
class UsaaDocumentButtonCandidate:
    index: int
    title: str
    date_delivered: str
    account: str
    policy_key: str
    document_kind: str
    row_text: str


class UsaaPolicyDocumentsRoute(Route):
    """USAA portal flow — declarations, renewal packets, ID cards."""

    def context_options(self) -> dict:
        return {
            "_launch_chrome_cdp": True,
            "_chrome_profile_dir": str(USAA_CHROME_PROFILE_DIR),
            "_init_script": STEALTH_INIT_SCRIPT,
            "viewport": {"width": 1280, "height": 800},
            "locale": "en-US",
            "timezone_id": "America/New_York",
            "extra_http_headers": {
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "image/avif,image/webp,*/*;q=0.8"
                ),
            },
        }

    @asynccontextmanager
    async def login_context(
        self,
        runner,
        username: str,
        password: str,
        context_options: dict,
    ) -> AsyncIterator[tuple[BrowserContext, Page]]:
        """Default login driver — yields ``(ctx, page)`` after a Playwright login.

        The Phase 1 orchestrator skips this and calls :meth:`login` directly
        against a context it created. Future phases can call this to swap in
        alternate drivers (OS-browser handoff, novnc, etc.) without changing
        the orchestrator's shape.
        """
        async with runner.new_context(storage_state=None, **context_options) as ctx:
            page = await ctx.new_page()
            await self.login(page, username, password)
            yield ctx, page

    async def login(self, page: Page, username: str, password: str) -> None:
        await self._prepare_page(page)
        log.info("usaa: navigating to login URL")
        try:
            await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            await self._dump_debug(page, "login-navigation-failure")
            raise RuntimeError(
                "USAA login page did not load. Use headed Chromium "
                "(PLAYWRIGHT_HEADLESS=false) for this carrier."
            ) from e

        await page.wait_for_timeout(750)
        if await self._looks_blocked(page):
            await self._dump_debug(page, "akamai-block")
            raise RuntimeError("USAA appears to be blocked by the bot manager")

        try:
            user_field = await self._first_present(
                page.locator("input[name='memberId']:visible").first,
                page.get_by_label(re.compile(r"Online ID|Member ID", re.I)).first,
                page.locator("input[type='text']:visible").first,
                timeout_ms=12000,
            )
            await self._slow_fill(user_field, username)

            next_button = await self._first_present(
                page.locator("#next-button:visible").first,
                page.get_by_role("button", name=re.compile(r"^\s*Next\s*$", re.I)).first,
                page.locator("button[type='submit']:visible").first,
            )
            await next_button.click()
            await self._settle(page, delay_ms=1000, networkidle_timeout_ms=3000)

            pw_field = await self._wait_for_password_field(page)
            await self._slow_fill(pw_field, password)

            submit = await self._first_present(
                page.locator("#next-button:visible").first,
                page.get_by_role(
                    "button", name=re.compile(r"^\s*(Next|Log On|Log In|Submit)\s*$", re.I)
                ).first,
                page.locator("button[type='submit']:visible").first,
            )
            await submit.click()
        except Exception as e:
            await self._dump_debug(page, "login-form-failure")
            raise RuntimeError(f"USAA login form interaction failed: {e}") from e

        await self._settle(page, delay_ms=500, networkidle_timeout_ms=3000)
        body = (await self._body_text(page)).lower()
        if any(
            phrase in body
            for phrase in (
                "password you entered doesn't match",
                "online id or password is incorrect",
                "credentials are incorrect",
                "cannot verify your information",
            )
        ) and "logon" in page.url.lower():
            await self._dump_debug(page, "login-rejected")
            raise RuntimeError("USAA login rejected - check username/password")
        if "logon" in page.url.lower() and await page.locator(
            "input[name='password']:visible, input[type='password']:visible"
        ).count():
            await self._dump_debug(page, "login-still-on-form")
            raise RuntimeError("USAA login did not leave the password form")

    async def mfa_required(self, page: Page) -> bool:
        url = page.url.lower()
        if any(k in url for k in ("mfa", "otp", "verify", "security", "challenge")):
            log.info("usaa: MFA detected via URL=%s", url)
            return True
        if await page.locator(MFA_CODE_INPUT_SELECTOR).count() > 0:
            log.info("usaa: MFA detected via code input")
            return True
        body = (await self._body_text(page)).lower()
        if any(
            phrase in body
            for phrase in (
                "verification code",
                "security code",
                "one-time code",
                "enter the code",
                "we sent",
                "verify your identity",
            )
        ):
            log.info("usaa: MFA detected via body text")
            return True
        return False

    async def submit_mfa(self, page: Page, code: str) -> None:
        try:
            otp_field = await self._first_present(
                page.locator("input[autocomplete='one-time-code']:visible").first,
                page.locator("input[inputmode='numeric']:visible").first,
                page.locator("input[name*='code' i]:visible").first,
                page.locator("input[id*='code' i]:visible").first,
                timeout_ms=20000,
            )
            await self._slow_fill(otp_field, code)
            try:
                submit = await self._first_present(
                    page.get_by_role(
                        "button", name=re.compile(r"continue|next|submit|verify", re.I)
                    ).first,
                    page.locator("button[type='submit']:visible").first,
                    timeout_ms=6000,
                )
                await submit.click()
            except Exception:
                await otp_field.press("Enter")
        except Exception as e:
            await self._dump_debug(page, "mfa-failure")
            raise RuntimeError(f"USAA MFA interaction failed: {e}") from e

        await self._wait_after_mfa_submit(page)

    async def is_authenticated(self, page: Page) -> bool:
        await self._prepare_page(page)
        for url in DASHBOARD_URL_CANDIDATES:
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=12000)
                await self._settle(page, delay_ms=500, networkidle_timeout_ms=2500)
            except Exception:
                continue
            current_url = page.url.lower()
            if "logon" in current_url or "login" in current_url:
                continue
            body = (await self._body_text(page)).lower()
            if any(s in body for s in ("log off", "sign out", "accounts", "policies")):
                return True
        return False

    async def fetch(
        self,
        page: Page,
        http: httpx.AsyncClient,
        ctx: BrowserContext,
    ) -> list[Artifact]:
        await self._prepare_page(page)
        all_artifacts: list[Artifact] = []
        seen: set[str] = set()
        saw_document_candidates = False

        def merge(new: list[Artifact]) -> None:
            self._merge_artifacts(all_artifacts, seen, new)

        merge(await self._fetch_targeted_policy_documents(page, http, ctx))
        if all_artifacts:
            return all_artifacts

        merge(await self._fetch_from_document_surface(page, http, ctx))
        if all_artifacts:
            return all_artifacts

        for url in DOCS_URL_CANDIDATES:
            try:
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                if resp:
                    content_type = resp.headers.get("content-type", "application/pdf")
                    if self._looks_like_document_response(resp):
                        try:
                            body = await resp.body()
                            if self._is_document_body(body, content_type):
                                merge(
                                    self._single_artifact(
                                        body,
                                        content_type,
                                        self._name_from_headers(
                                            resp.headers, resp.url, "USAA document 1"
                                        ),
                                        index=len(all_artifacts),
                                    )
                                )
                                return all_artifacts
                        except Exception:
                            pass
                title = (await page.title()).lower()
                if "logon" in page.url.lower() or "login" in page.url.lower():
                    continue
                if "page not found" in title:
                    continue
            except Exception as e:
                log.warning("usaa: docs URL %s failed: %s", url, e)
                continue
            merge(await self._fetch_from_document_surface(page, http, ctx))
            if all_artifacts:
                return all_artifacts

            candidates = await self._collect_document_links(page)
            if candidates:
                saw_document_candidates = True
                merge(await self._fetch_document_link_candidates(http, candidates))
                if all_artifacts:
                    return all_artifacts

        if all_artifacts:
            return all_artifacts

        if not saw_document_candidates:
            await self._dump_debug(page, "docs-no-links")
            raise RuntimeError("USAA: authenticated, but no document links found yet")

        await self._dump_debug(page, "docs-fetch-failed")
        raise RuntimeError("USAA: found document links, but downloads failed")

    async def _fetch_document_link_candidates(
        self,
        http: httpx.AsyncClient,
        candidates: list[tuple[str, str]],
    ) -> list[Artifact]:
        async def fetch(name: str, href: str, idx: int) -> Artifact | None:
            try:
                r = await http.get(href)
                r.raise_for_status()
                body = r.content
                content_type = r.headers.get("content-type", "application/pdf")
                if "text/html" in content_type.lower() or body.lstrip().startswith(
                    b"<!doctype html"
                ):
                    return None
                if "pdf" not in content_type.lower() and not body.startswith(b"%PDF"):
                    return None
                display_name = name.strip() or f"usaa-document-{idx}"
                if (
                    "pdf" in content_type.lower() or body.startswith(b"%PDF")
                ) and not display_name.lower().endswith(".pdf"):
                    display_name += ".pdf"
                return Artifact(
                    id=f"usaa-doc-{idx}",
                    filename=display_name,
                    mimetype=content_type,
                    data=body,
                )
            except Exception as e:
                log.warning("usaa: failed to fetch %s: %s", href, e)
                return None

        results = await asyncio.gather(
            *[fetch(name, href, idx) for idx, (name, href) in enumerate(candidates)]
        )
        return [a for a in results if a is not None]

    async def _fetch_from_document_surface(
        self,
        page: Page,
        http: httpx.AsyncClient,
        ctx: BrowserContext,
    ) -> list[Artifact]:
        all_artifacts: list[Artifact] = []
        seen: set[str] = set()

        self._merge_artifacts(
            all_artifacts,
            seen,
            await self._fetch_document_buttons(page, http, ctx),
        )

        if await self._open_policy_documents_from_summary(page):
            self._merge_artifacts(
                all_artifacts,
                seen,
                await self._fetch_document_buttons(page, http, ctx),
            )

        self._merge_artifacts(
            all_artifacts,
            seen,
            await self._fetch_named_document_actions(page, http, ctx),
        )
        return all_artifacts

    async def _looks_like_document_center(self, page: Page) -> bool:
        deadline = time.perf_counter() + 6.0
        while time.perf_counter() < deadline:
            try:
                read_buttons = page.locator("button[data-testid^='readDocument-']")
                if await read_buttons.count() > 0:
                    return True
            except Exception:
                pass
            body = (await self._body_text(page, timeout_ms=500)).lower()
            if any(
                phrase in body
                for phrase in (
                    "my documents",
                    "search documents",
                    "search by title",
                    "filter documents",
                    "document title",
                )
            ):
                return True
            await page.wait_for_timeout(250)
        return False

    async def _wait_for_document_center_ready(self, page: Page) -> bool:
        ready = page.locator(
            "input[data-testid='search-text']:visible, "
            "button[data-testid^='readDocument-']:visible"
        )
        try:
            await ready.first.wait_for(state="visible", timeout=7000)
            return True
        except Exception:
            return False

    async def _search_document_center_by_title(self, page: Page, term: str) -> bool:
        locators = (
            page.locator("input[data-testid='search-text']:visible").first,
            page.get_by_label(re.compile(r"Search by title", re.I)).first,
            page.get_by_label(re.compile(r"Search documents", re.I)).first,
            page.locator("input[placeholder*='Search' i]:visible").first,
            page.locator("input[type='search']:visible").first,
            page.locator("input[name*='search' i]:visible").first,
        )
        search: Locator | None = None
        for locator in locators:
            try:
                await locator.wait_for(state="visible", timeout=1200)
                await locator.click(timeout=1500)
                await locator.fill("")
                await locator.type(term, delay=15)
                search = locator
                break
            except Exception:
                continue
        if search is None:
            log.info("usaa: document search input not found for %s", term)
            return False

        submitted = False
        for target in (
            page.locator("button[data-testid='search-icon']:visible").first,
            page.get_by_role(
                "button", name=re.compile(r"^Search documents$|^Search$", re.I)
            ).first,
            page.locator("button[type='submit']:visible").first,
        ):
            try:
                if await target.count() == 0:
                    continue
                await target.click(timeout=2000)
                submitted = True
                break
            except Exception:
                continue
        if not submitted:
            try:
                await search.press("Enter")
            except Exception:
                pass

        deadline = time.perf_counter() + 3.0
        while time.perf_counter() < deadline:
            try:
                if (
                    await page.locator(
                        "button[data-testid^='readDocument-']:visible"
                    ).count()
                    > 0
                ):
                    return True
            except Exception:
                pass
            try:
                body = (await self._body_text(page, timeout_ms=300)).lower()
                if any(
                    phrase in body
                    for phrase in (
                        "search results: 0",
                        "no documents",
                        "no results",
                        "didn't find any documents",
                    )
                ):
                    return True
            except Exception:
                pass
            await page.wait_for_timeout(150)
        return True

    async def _document_center_account_filter_values(self, page: Page) -> list[str]:
        filter_button = page.get_by_role(
            "button", name=re.compile(r"Filter documents|Filter", re.I)
        ).first
        try:
            if await filter_button.count() == 0:
                return []
            await filter_button.click(timeout=2500)
            account_filter = page.locator("select[data-testid='account-filter']").first
            await account_filter.wait_for(state="visible", timeout=2500)
            values: list[str] = []
            deadline = time.perf_counter() + 3.0
            while time.perf_counter() < deadline:
                values = await page.eval_on_selector_all(
                    "select[data-testid='account-filter'] option",
                    """options => options
                        .map(option => option.getAttribute('value') || option.value || '')
                        .filter(value => value.startsWith('accountName:'))""",
                )
                if values:
                    break
                await page.wait_for_timeout(150)
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass
            return values
        except Exception:
            return []

    async def _widen_document_center_date_filter(
        self,
        page: Page,
        *,
        account_filter_value: str | None = None,
    ) -> None:
        filter_button = page.get_by_role(
            "button", name=re.compile(r"Filter documents|Filter", re.I)
        ).first
        try:
            if await filter_button.count() == 0:
                return
            await filter_button.click(timeout=2500)
            await page.wait_for_timeout(300)
        except Exception:
            return

        if account_filter_value is not None:
            try:
                account_filter = page.locator(
                    "select[data-testid='account-filter']"
                ).first
                if await account_filter.count() > 0:
                    option = page.locator(
                        f"select[data-testid='account-filter'] "
                        f"option[value={json.dumps(account_filter_value)}]"
                    ).first
                    try:
                        await option.wait_for(state="attached", timeout=2500)
                    except Exception:
                        pass
                    await account_filter.select_option(value=account_filter_value)
                    await page.wait_for_timeout(150)
            except Exception:
                pass

        for pattern in (
            r"Custom range",
            r"All dates",
            r"Any time",
            r"All documents",
            r"Custom date",
            r"Date range",
        ):
            target = page.get_by_label(re.compile(pattern, re.I)).first
            try:
                if await target.count() == 0:
                    target = page.get_by_role(
                        "button", name=re.compile(pattern, re.I)
                    ).first
                if await target.count() == 0:
                    continue
                await target.click(timeout=1200)
                await page.wait_for_timeout(150)
                break
            except Exception:
                continue

        try:
            custom_range = page.locator("input[data-testid='dateFilter-3']").first
            if await custom_range.count() > 0:
                await custom_range.check(timeout=1200)
                await page.wait_for_timeout(150)
        except Exception:
            pass

        await self._fill_first_visible_date_field(
            page,
            (page.locator("input[data-testid='startDate']:visible").first,),
            "01/01/2000",
        )
        await self._fill_first_visible_date_field(
            page,
            (page.locator("input[data-testid='endDate']:visible").first,),
            time.strftime("%m/%d/%Y"),
        )
        await self._fill_first_visible_date_field(
            page,
            (
                page.get_by_label(re.compile(r"From|Start", re.I)).first,
                page.locator("input[name*='from' i]:visible").first,
                page.locator("input[id*='from' i]:visible").first,
                page.locator("input[name*='start' i]:visible").first,
                page.locator("input[id*='start' i]:visible").first,
            ),
            "01/01/2000",
        )
        await self._fill_first_visible_date_field(
            page,
            (
                page.get_by_label(re.compile(r"To|End", re.I)).first,
                page.locator("input[name*='to' i]:visible").first,
                page.locator("input[id*='to' i]:visible").first,
                page.locator("input[name*='end' i]:visible").first,
                page.locator("input[id*='end' i]:visible").first,
            ),
            time.strftime("%m/%d/%Y"),
        )

        for target in (
            page.locator("button[data-testid='filter-button']:visible").first,
            *[
                page.get_by_role("button", name=re.compile(pattern, re.I)).first
                for pattern in (r"Apply", r"Show results", r"Update", r"Done")
            ],
        ):
            try:
                if await target.count() == 0:
                    continue
                await target.click(timeout=1500)
                await page.wait_for_timeout(500)
                return
            except Exception:
                continue
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass

    async def _fill_first_visible_date_field(
        self,
        page: Page,
        locators: tuple[Locator, ...],
        value: str,
    ) -> None:
        for locator in locators:
            try:
                if await locator.count() == 0:
                    continue
                await locator.fill(value, timeout=1200)
                return
            except Exception:
                continue

    async def _rank_document_button_candidates(
        self, page: Page
    ) -> list[UsaaDocumentButtonCandidate]:
        try:
            raw_candidates = await page.eval_on_selector_all(
                "button[data-testid^='readDocument-']",
                """buttons => {
                    const normalize = value =>
                        (value || '').replace(/\\s+/g, ' ').trim();
                    const textOf = node => normalize(node && (node.innerText || node.textContent));
                    const actionish = text =>
                        /^(actions?|options?|view|download|read|open)$/i.test(text || '');
                    return buttons.map((button, index) => {
                        const row = button.closest(
                            'tr, [role="row"], [data-testid*="row"], li'
                        ) || button.parentElement;
                        let cells = [];
                        if (row) {
                            cells = Array.from(row.querySelectorAll(
                                'th, td, [role="cell"], [role="gridcell"]'
                            )).map(textOf).filter(Boolean);
                            if (!cells.length) {
                                cells = Array.from(row.children || [])
                                    .map(textOf)
                                    .filter(Boolean);
                            }
                        }
                        const rowText = textOf(row);
                        const buttonText = textOf(button);
                        const datePattern = /\\b\\d{1,2}\\/\\d{1,2}\\/\\d{4}\\b/;
                        const dateMatch = rowText.match(datePattern);
                        const accountPattern = /\\*+\\s*-?\\s*\\d{2,6}\\b/;
                        let title = buttonText;
                        if (!title || actionish(title)) {
                            title = cells.find(cell =>
                                !actionish(cell)
                                && !datePattern.test(cell)
                                && !accountPattern.test(cell)
                            ) || rowText.split(/\\n/)[0] || buttonText;
                        }
                        const account = cells.find(cell => accountPattern.test(cell))
                            || (rowText.match(accountPattern) || [''])[0];
                        return {
                            index,
                            title,
                            buttonText,
                            dateDelivered: (cells.find(cell => datePattern.test(cell))
                                || (dateMatch && dateMatch[0])
                                || ''),
                            account: account || '',
                            rowText,
                        };
                    });
                }""",
            )
        except Exception as e:
            log.info("usaa: could not inspect document button rows: %s", e)
            return []
        return self._rank_usaa_document_button_candidates(raw_candidates)

    async def _fetch_targeted_policy_documents(
        self,
        page: Page,
        http: httpx.AsyncClient,
        ctx: BrowserContext,
    ) -> list[Artifact]:
        all_artifacts: list[Artifact] = []
        seen: set[str] = set()
        selected_policy_keys: set[str] = set()

        for url in DOCUMENT_CENTER_URL_CANDIDATES:
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                if "logon" in page.url.lower() or "login" in page.url.lower():
                    continue

                if not await self._looks_like_document_center(page):
                    continue

                if not await self._wait_for_document_center_ready(page):
                    continue

                self._merge_artifacts(
                    all_artifacts,
                    seen,
                    await self._fetch_document_buttons(
                        page,
                        http,
                        ctx,
                        selected_policy_keys=selected_policy_keys,
                        fallback_all=False,
                    ),
                )
                if len(selected_policy_keys) >= 2:
                    return all_artifacts

                account_filter_values = (
                    await self._document_center_account_filter_values(page)
                )
                for account_filter_value in account_filter_values:
                    await self._widen_document_center_date_filter(
                        page, account_filter_value=account_filter_value
                    )
                    found_for_account = await self._fetch_document_buttons(
                        page,
                        http,
                        ctx,
                        selected_policy_keys=selected_policy_keys,
                        fallback_all=False,
                    )
                    self._merge_artifacts(all_artifacts, seen, found_for_account)
                    if found_for_account:
                        continue

                    for term in POLICY_DOCUMENT_SEARCH_TERMS:
                        before_keys = set(selected_policy_keys)
                        self._merge_artifacts(
                            all_artifacts,
                            seen,
                            await self._fetch_document_search_results(
                                page,
                                http,
                                ctx,
                                term,
                                selected_policy_keys=selected_policy_keys,
                            ),
                        )
                        if selected_policy_keys != before_keys:
                            break
                if all_artifacts:
                    return all_artifacts

                await self._widen_document_center_date_filter(page)
                for term in POLICY_DOCUMENT_SEARCH_TERMS:
                    before_keys = set(selected_policy_keys)
                    self._merge_artifacts(
                        all_artifacts,
                        seen,
                        await self._fetch_document_search_results(
                            page,
                            http,
                            ctx,
                            term,
                            selected_policy_keys=selected_policy_keys,
                        ),
                    )
                    if selected_policy_keys != before_keys:
                        break
                if all_artifacts:
                    return all_artifacts
            except Exception as e:
                log.warning("usaa: targeted document center path %s failed: %s", url, e)
                continue

        return all_artifacts

    async def _fetch_document_search_results(
        self,
        page: Page,
        http: httpx.AsyncClient,
        ctx: BrowserContext,
        term: str,
        *,
        selected_policy_keys: set[str],
    ) -> list[Artifact]:
        if not await self._search_document_center_by_title(page, term):
            return []
        return await self._fetch_document_buttons(
            page,
            http,
            ctx,
            selected_policy_keys=selected_policy_keys,
            fallback_all=False,
        )

    async def _fetch_document_buttons(
        self,
        page: Page,
        http: httpx.AsyncClient,
        ctx: BrowserContext,
        *,
        selected_policy_keys: set[str] | None = None,
        fallback_all: bool = True,
    ) -> list[Artifact]:
        buttons = page.locator("button[data-testid^='readDocument-']")
        count = await buttons.count()
        if count == 0:
            try:
                await buttons.first.wait_for(state="visible", timeout=2500)
                count = await buttons.count()
            except Exception:
                return []

        all_artifacts: list[Artifact] = []
        seen: set[str] = set()
        successful_policy_keys = selected_policy_keys
        if successful_policy_keys is None:
            successful_policy_keys = set()
        candidates = await self._rank_document_button_candidates(page)
        if not candidates and fallback_all:
            candidates = [
                UsaaDocumentButtonCandidate(
                    index=idx,
                    title=f"USAA document {idx + 1}",
                    date_delivered="",
                    account="",
                    policy_key=f"fallback:{idx}",
                    document_kind="fallback",
                    row_text="",
                )
                for idx in range(count)
            ]
        if not candidates:
            return []

        for candidate in candidates:
            if candidate.index >= count:
                continue
            if (
                candidate.document_kind != "fallback"
                and candidate.policy_key in successful_policy_keys
            ):
                continue
            await self._close_document_viewer(page)
            button = buttons.nth(candidate.index)
            try:
                name = (
                    await button.inner_text(timeout=2000)
                ).strip() or candidate.title or f"USAA document {candidate.index + 1}"
            except Exception:
                name = candidate.title or f"USAA document {candidate.index + 1}"
            if candidate.title and (
                not name or self._is_actionish_document_button_text(name)
            ):
                name = candidate.title

            try:
                href = await self._direct_document_href(button)
                if href:
                    direct = await self._fetch_direct_document(http, href, name)
                    if direct is not None:
                        body, content_type, display_name = direct
                        self._merge_artifacts(
                            all_artifacts,
                            seen,
                            self._single_artifact(
                                body,
                                content_type,
                                display_name,
                                index=len(all_artifacts),
                            ),
                        )
                        if candidate.document_kind != "fallback":
                            successful_policy_keys.add(candidate.policy_key)
                        continue

                payload = await self._click_for_first_document(
                    page, http, ctx, button, name
                )
                if payload is not None:
                    body, content_type, display_name = payload
                    self._merge_artifacts(
                        all_artifacts,
                        seen,
                        self._single_artifact(
                            body,
                            content_type,
                            display_name,
                            index=len(all_artifacts),
                        ),
                    )
                    if candidate.document_kind != "fallback":
                        successful_policy_keys.add(candidate.policy_key)
            except Exception as e:
                log.warning(
                    "usaa: document button %s (%s) failed: %s",
                    candidate.index,
                    candidate.title,
                    e,
                )
        return all_artifacts

    async def _fetch_named_document_actions(
        self,
        page: Page,
        http: httpx.AsyncClient,
        ctx: BrowserContext,
    ) -> list[Artifact]:
        action_patterns = (
            re.compile(r"Proof of insurance", re.I),
            re.compile(r"^(Auto )?ID card$", re.I),
        )
        all_artifacts: list[Artifact] = []
        seen: set[str] = set()
        for pattern in action_patterns:
            button = page.get_by_role("button", name=pattern).first
            if await button.count() == 0:
                continue
            try:
                name = (await button.inner_text(timeout=1000)).strip()
            except Exception:
                name = pattern.pattern.strip("^$") or "USAA document 1"
            try:
                payload = await self._click_for_first_document(
                    page, http, ctx, button, name
                )
                if payload is not None:
                    body, content_type, display_name = payload
                    self._merge_artifacts(
                        all_artifacts,
                        seen,
                        self._single_artifact(
                            body,
                            content_type,
                            display_name,
                            index=len(all_artifacts),
                        ),
                    )
            except Exception as e:
                log.warning("usaa: document action %s failed: %s", pattern.pattern, e)
        return all_artifacts

    async def _click_for_first_document(
        self,
        page: Page,
        http: httpx.AsyncClient,
        ctx: BrowserContext,
        target: Locator,
        name: str,
    ) -> tuple[bytes, str, str] | None:
        response_queue: asyncio.Queue = asyncio.Queue()

        def on_response(resp):
            if self._looks_like_document_response(resp):
                response_queue.put_nowait(resp)

        page.on("response", on_response)
        download_task = asyncio.create_task(page.wait_for_event("download", timeout=7000))
        popup_task = asyncio.create_task(ctx.wait_for_event("page", timeout=7000))
        try:
            await target.click(timeout=5000)
            payload = await self._first_document_payload(
                page, http, response_queue, download_task, popup_task, name
            )
            if payload is not None:
                await self._close_document_viewer(page)
            return payload
        finally:
            page.remove_listener("response", on_response)
            for task in (download_task, popup_task):
                if not task.done():
                    task.cancel()

    async def _close_document_viewer(self, page: Page) -> None:
        modal = page.locator(
            "[data-testid='document-view-modal'], "
            "iframe[data-testid='document-view-iframe']"
        )
        try:
            if await modal.count() == 0:
                return
        except Exception:
            return

        close_targets = (
            page.locator(
                "[data-testid='document-view-modal'] "
                "button[aria-label*='close' i]"
            ).first,
            page.locator(
                "[data-testid='document-view-modal'] "
                "button[data-testid*='close' i]"
            ).first,
            page.locator(
                "[data-testid='document-view-modal'] "
                "[role='button'][aria-label*='close' i]"
            ).first,
            page.get_by_role(
                "button", name=re.compile(r"close|dismiss|done", re.I)
            ).first,
        )
        for target in close_targets:
            try:
                if await target.count() == 0:
                    continue
                await target.click(timeout=1200)
                break
            except Exception:
                continue
        else:
            try:
                await page.keyboard.press("Escape")
            except Exception:
                return

        try:
            await page.locator("[data-testid='document-view-modal']").first.wait_for(
                state="hidden", timeout=2500
            )
        except Exception:
            try:
                await page.wait_for_timeout(300)
            except Exception:
                pass

    async def _open_policy_documents_from_summary(self, page: Page) -> bool:
        rows = page.locator("li", has_text=re.compile(r"Policy documents", re.I))
        if await rows.count() == 0:
            if not self._is_auto_policy_surface(page):
                return False
            try:
                await rows.first.wait_for(state="visible", timeout=4500)
            except Exception:
                return False
        row = rows.first
        try:
            button = row.get_by_role("button", name=re.compile(r"View", re.I)).first
            await button.click(timeout=4000)
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=5000)
            except Exception:
                pass
            try:
                await page.locator("button[data-testid^='readDocument-']").first.wait_for(
                    state="visible", timeout=5000
                )
            except Exception:
                await page.wait_for_timeout(700)
            return True
        except Exception as e:
            log.warning("usaa: policy documents action failed: %s", e)
            return False

    @staticmethod
    def _is_auto_policy_surface(page: Page) -> bool:
        url = page.url.lower()
        return "auto-insurance" in url or "insurance_auto" in url

    async def _first_document_payload(
        self,
        page: Page,
        http: httpx.AsyncClient,
        response_queue: asyncio.Queue,
        download_task: asyncio.Task,
        popup_task: asyncio.Task,
        name: str,
    ) -> tuple[bytes, str, str] | None:
        async def from_response():
            deadline = time.perf_counter() + 7.0
            while True:
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    raise TimeoutError("no document response")
                resp = await asyncio.wait_for(response_queue.get(), timeout=remaining)
                content_type = resp.headers.get("content-type", "application/pdf")
                body = await resp.body()
                if self._is_document_body(body, content_type):
                    return body, content_type, self._name_from_response(resp, name)

        async def from_download():
            download = await download_task
            path = await download.path()
            if not path:
                raise RuntimeError("download had no local path")
            body = Path(path).read_bytes()
            content_type = "application/pdf"
            if not self._is_document_body(body, content_type):
                raise RuntimeError("download was not a PDF")
            return body, content_type, download.suggested_filename or name

        async def from_popup():
            popup = await popup_task
            try:
                try:
                    await popup.wait_for_load_state("domcontentloaded", timeout=3500)
                except Exception:
                    pass
                body, content_type = await self._extract_document_from_page(popup, http)
                if body is None or not self._is_document_body(body, content_type):
                    raise RuntimeError("popup did not expose a PDF")
                return body, content_type, name
            finally:
                if not popup.is_closed():
                    await popup.close()

        async def from_current_page():
            await page.wait_for_timeout(400)
            body, content_type = await self._extract_document_from_page(page, http)
            if body is None or not self._is_document_body(body, content_type):
                raise RuntimeError("current page did not expose a PDF")
            return body, content_type, name

        tasks = [
            asyncio.create_task(from_response()),
            asyncio.create_task(from_download()),
            asyncio.create_task(from_popup()),
            asyncio.create_task(from_current_page()),
        ]
        try:
            for completed in asyncio.as_completed(tasks, timeout=7.5):
                try:
                    return await completed
                except Exception:
                    continue
        except TimeoutError:
            return None
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
        return None

    async def _direct_document_href(self, button: Locator) -> str | None:
        try:
            return await button.evaluate(
                """el => {
                    const attrs = ['href', 'data-href', 'data-url', 'data-document-url'];
                    const candidates = [el, el.closest('a[href], [data-href], [data-url], [data-document-url]')];
                    for (const node of candidates) {
                        if (!node) continue;
                        for (const attr of attrs) {
                            const value = node.getAttribute(attr);
                            if (value) return new URL(value, window.location.href).href;
                        }
                    }
                    return null;
                }"""
            )
        except Exception:
            return None

    async def _fetch_direct_document(
        self, http: httpx.AsyncClient, href: str, name: str
    ) -> tuple[bytes, str, str] | None:
        try:
            r = await http.get(href)
            content_type = r.headers.get("content-type", "application/pdf")
            if not self._is_document_body(r.content, content_type):
                return None
            return r.content, content_type, self._name_from_headers(
                r.headers, href, name
            )
        except Exception as e:
            log.warning("usaa: direct document fetch failed for %s: %s", href, e)
            return None

    def _single_artifact(
        self, body: bytes, content_type: str, name: str, *, index: int
    ) -> list[Artifact]:
        display_name = name.strip() or f"USAA document {index + 1}"
        if (
            ("pdf" in content_type.lower() or body.startswith(b"%PDF"))
            and not display_name.lower().endswith(".pdf")
        ):
            display_name += ".pdf"
        return [
            Artifact(
                id=f"usaa-doc-{index}",
                filename=display_name,
                mimetype=content_type,
                data=body,
            )
        ]

    @staticmethod
    def _merge_artifacts(
        target: list[Artifact],
        seen: set[str],
        new: list[Artifact],
    ) -> None:
        for artifact in new:
            body = artifact.data
            if not body:
                continue
            key = hashlib.sha256(body).hexdigest()
            if key in seen:
                continue
            seen.add(key)
            new_id = f"usaa-doc-{len(target)}"
            target.append(
                Artifact(
                    id=new_id,
                    filename=artifact.filename,
                    mimetype=artifact.mimetype,
                    data=body,
                )
            )

    @classmethod
    def _rank_usaa_document_button_candidates(
        cls, raw_candidates: list[dict]
    ) -> list[UsaaDocumentButtonCandidate]:
        candidates: list[UsaaDocumentButtonCandidate] = []
        for raw in raw_candidates:
            title = cls._best_usaa_document_title(raw)
            document_kind = cls._usaa_policy_document_kind(title)
            if document_kind is None:
                continue
            account = cls._normalize_usaa_text(str(raw.get("account") or ""))
            row_text = cls._normalize_usaa_text(str(raw.get("rowText") or ""))
            candidates.append(
                UsaaDocumentButtonCandidate(
                    index=int(raw.get("index") or 0),
                    title=title,
                    date_delivered=cls._normalize_usaa_text(
                        str(raw.get("dateDelivered") or "")
                    ),
                    account=account,
                    policy_key=cls._usaa_policy_key(title, account, row_text),
                    document_kind=document_kind,
                    row_text=row_text,
                )
            )

        candidates.sort(
            key=lambda candidate: (
                -cls._usaa_date_sort_value(
                    candidate.date_delivered or candidate.row_text
                ),
                cls._usaa_document_kind_priority(candidate.document_kind),
                candidate.index,
            )
        )
        return candidates

    @classmethod
    def _best_usaa_document_title(cls, raw: dict) -> str:
        title = cls._normalize_usaa_text(str(raw.get("title") or ""))
        button_text = cls._normalize_usaa_text(str(raw.get("buttonText") or ""))
        if not title or cls._is_actionish_document_button_text(title):
            title = button_text
        if not title or cls._is_actionish_document_button_text(title):
            row_text = cls._normalize_usaa_text(str(raw.get("rowText") or ""))
            pieces = re.split(r"\s{2,}|\n", row_text)
            for piece in pieces:
                piece = cls._normalize_usaa_text(piece)
                if (
                    piece
                    and not cls._is_actionish_document_button_text(piece)
                    and not re.fullmatch(r"\d{1,2}/\d{1,2}/\d{4}", piece)
                    and not re.fullmatch(r"\*+\s*-?\s*\d{2,6}", piece)
                ):
                    title = piece
                    break
        return title

    @staticmethod
    def _normalize_usaa_text(value: str) -> str:
        return re.sub(r"\s+", " ", value or "").strip()

    @staticmethod
    def _is_actionish_document_button_text(value: str) -> bool:
        return bool(
            re.fullmatch(
                r"(actions?|options?|view|download|read|open)",
                (value or "").strip(),
                flags=re.I,
            )
        )

    @staticmethod
    def _usaa_policy_document_kind(title: str) -> str | None:
        lowered = title.lower()
        if re.search(
            r"\b("
            r"declarations?|declaration page|initial|new policy|new business|"
            r"policy start|start of policy|policy packet|policy documents?"
            r")\b",
            lowered,
        ):
            if re.search(r"\b(bill|billing|statement|payment|invoice)\b", lowered):
                return None
            return "initial"
        if re.search(r"\brenew(?:al)?\b", lowered):
            return "renewal"
        if re.search(r"\bchange\b", lowered) and re.search(r"\bpolicy\b", lowered):
            return "change"
        return None

    @staticmethod
    def _usaa_document_kind_priority(kind: str) -> int:
        return {
            "initial": 0,
            "renewal": 1,
            "change": 2,
            "fallback": 3,
        }.get(kind, 4)

    @classmethod
    def _usaa_policy_key(cls, title: str, account: str, row_text: str = "") -> str:
        family = cls._usaa_policy_family(f"{title} {row_text}")
        tail = cls._usaa_masked_account_tail(account) or cls._usaa_masked_account_tail(
            title
        )
        if tail:
            return f"{family}:{tail}"

        base = f" {title.lower()} "
        base = re.sub(r"\b\d{1,2}/\d{1,2}/\d{4}\b", " ", base)
        base = re.sub(r"\*+\s*-?\s*\d{2,6}\b", " ", base)
        base = re.sub(
            r"\b("
            r"renewal|declarations?|declaration|page|initial|new|business|start|"
            r"of|policy|insurance|documents?|packet|auto|automobile|vehicle|"
            r"renters?|homeowners?|home|condo|property|id|cards?"
            r")\b",
            " ",
            base,
        )
        base = re.sub(r"[^a-z0-9]+", " ", base).strip()
        return f"{family}:{base or family}"

    @staticmethod
    def _usaa_policy_family(value: str) -> str:
        lowered = value.lower()
        if re.search(r"\b(auto|automobile|vehicle)\b", lowered):
            return "auto"
        if re.search(r"\brenters?\b", lowered):
            return "renters"
        if re.search(r"\bhomeowners?\b", lowered):
            return "homeowners"
        if re.search(r"\bcondo\b", lowered):
            return "condo"
        if re.search(r"\b(property|dwelling|home)\b", lowered):
            return "property"
        return "policy"

    @staticmethod
    def _usaa_masked_account_tail(value: str) -> str | None:
        match = re.search(r"\*+\s*-?\s*(\d{2,6})\b", value or "")
        if not match:
            return None
        return match.group(1)

    @staticmethod
    def _usaa_date_sort_value(value: str) -> int:
        match = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", value or "")
        if not match:
            return 0
        month, day, year = (int(part) for part in match.groups())
        return year * 10000 + month * 100 + day

    async def _extract_document_from_page(
        self, page: Page, http: httpx.AsyncClient
    ) -> tuple[bytes | None, str]:
        urls = await page.eval_on_selector_all(
            "embed[src], iframe[src], object[data], a[href$='.pdf'], a[href*='.pdf?']",
            """els => els.map(e => e.src || e.data || e.href).filter(Boolean)""",
        )
        for url in urls:
            try:
                r = await http.get(url)
                content_type = r.headers.get("content-type", "application/pdf")
                if self._is_document_body(r.content, content_type):
                    return r.content, content_type
            except Exception:
                continue
        return None, "application/pdf"

    @staticmethod
    def _is_document_body(body: bytes, content_type: str) -> bool:
        return is_pdf_document(body, content_type)

    @staticmethod
    def _looks_like_document_response(resp) -> bool:
        content_type = resp.headers.get("content-type", "").lower()
        url = resp.url.lower()
        return (
            "pdf" in content_type
            or "octet-stream" in content_type
            or ".pdf" in url
            or "document" in url
            or "content" in url
        )

    @staticmethod
    def _name_from_response(resp, fallback: str) -> str:
        return UsaaPolicyDocumentsRoute._name_from_headers(
            resp.headers, resp.url, fallback
        )

    @staticmethod
    def _name_from_headers(headers, url: str, fallback: str) -> str:
        return filename_from_content_disposition(
            headers.get("content-disposition", ""), url, fallback
        )

    async def _prepare_page(self, page: Page) -> None:
        return None

    async def _wait_after_mfa_submit(self, page: Page) -> None:
        deadline = time.perf_counter() + 2.0
        while time.perf_counter() < deadline:
            code_inputs = await page.locator(MFA_CODE_INPUT_SELECTOR).count()
            body = (await self._body_text(page, timeout_ms=500)).lower()
            if any(
                phrase in body
                for phrase in (
                    "invalid code",
                    "incorrect code",
                    "code you entered",
                    "expired",
                )
            ):
                raise RuntimeError("USAA MFA code was rejected")
            if code_inputs == 0:
                return
            url = page.url.lower()
            challenge_tokens = (
                "mfa",
                "otp",
                "verify",
                "security",
                "challenge",
                "logon",
            )
            if not any(k in url for k in challenge_tokens):
                return
            await page.wait_for_timeout(150)

    async def _collect_document_links(self, page: Page) -> list[tuple[str, str]]:
        links: list[tuple[str, str]] = await page.eval_on_selector_all(
            "a[href], button[data-href], [role='link'][href]",
            """els => els.map(e => {
                const rects = e.getClientRects();
                const style = window.getComputedStyle(e);
                const visible = rects.length > 0
                    && style.visibility !== 'hidden'
                    && style.display !== 'none'
                    && Number(style.opacity || '1') > 0;
                const inChrome = !!e.closest(
                    'header, footer, nav, .usaa-globalHeader, .usaa-globalFooterNav, .headerDropMenu'
                );
                const href = e.href || e.getAttribute('data-href') || '';
                const text = (e.innerText || e.textContent || '').trim().slice(0, 120);
                return { text: text || 'USAA document', href, visible, inChrome };
            }).filter(e => e.visible && !e.inChrome).map(e => [e.text, e.href])""",
        )
        doc_pattern = re.compile(
            r"pdf|document|policy|declaration|id.?card|insurance.?card|proof",
            re.I,
        )
        seen: set[str] = set()
        candidates: list[tuple[str, str]] = []
        for name, href in links:
            if not href or href in seen:
                continue
            if doc_pattern.search(name) or doc_pattern.search(href):
                seen.add(href)
                candidates.append((name, href))
        return candidates

    async def _settle(
        self,
        page: Page,
        delay_ms: int = 1000,
        networkidle_timeout_ms: int = 3000,
    ) -> None:
        try:
            await page.wait_for_load_state("networkidle", timeout=networkidle_timeout_ms)
        except Exception:
            pass
        await page.wait_for_timeout(delay_ms)

    async def _slow_fill(self, loc: Locator, value: str) -> None:
        try:
            await loc.click()
            await loc.fill("")
            await loc.type(value, delay=35)
        except Exception:
            await loc.fill(value)

    async def _wait_for_password_field(
        self, page: Page, timeout_ms: int = 45000
    ) -> Locator:
        deadline = time.perf_counter() + (timeout_ms / 1000)
        password_locators = (
            page.locator("input[name='password']:visible").first,
            page.get_by_label(re.compile(r"Password", re.I)).first,
            page.locator("input[type='password']:visible").first,
        )
        while time.perf_counter() < deadline:
            for locator in password_locators:
                try:
                    await locator.wait_for(state="visible", timeout=300)
                    return locator
                except Exception:
                    pass
            if await self._looks_blocked(page):
                raise RuntimeError(
                    "USAA blocked or returned unavailable after the Online ID step"
                )
            await page.wait_for_timeout(300)
        raise RuntimeError("USAA password field did not appear after Online ID step")

    async def _body_text(self, page: Page, timeout_ms: int = 3000) -> str:
        try:
            return await page.locator("body").inner_text(timeout=timeout_ms)
        except Exception:
            return ""

    async def _looks_blocked(self, page: Page) -> bool:
        url = page.url.lower()
        if "chrome-error" in url:
            return True
        body = (await self._body_text(page)).lower()
        return any(
            phrase in body
            for phrase in (
                "access denied",
                "unable to complete your request",
                "system is currently unavailable",
                "request unsuccessful",
                "reference #",
                "bot manager",
                "akamai",
            )
        )

    async def _dump_debug(self, page: Page, label: str) -> None:
        try:
            DEBUG_DIR.mkdir(exist_ok=True)
            png = DEBUG_DIR / f"usaa-{label}.png"
            html = DEBUG_DIR / f"usaa-{label}.html"
            html.write_text(sanitize_html_for_debug(await page.content()))
            try:
                await page.screenshot(path=str(png), full_page=True, timeout=5000)
            except Exception as e:
                log.warning("usaa: failed to capture screenshot %s: %s", png, e)
            log.warning("usaa debug dump -> %s, %s", png, html)
        except Exception as e:
            log.warning("usaa: failed to dump debug artifacts: %s", e)

    @staticmethod
    async def _first_present(*locators: Locator, timeout_ms: int = 7000) -> Locator:
        per = max(1000, timeout_ms // max(1, len(locators)))
        for loc in locators[:-1]:
            try:
                await loc.wait_for(state="visible", timeout=per)
                return loc
            except Exception:
                continue
        await locators[-1].wait_for(state="visible", timeout=per)
        return locators[-1]
