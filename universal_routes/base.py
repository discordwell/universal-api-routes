"""Route ABC + Artifact dataclass + ROUTE_META schema."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Literal, TypedDict

import httpx
from playwright.async_api import BrowserContext, Page

MfaStyle = Literal["code_input", "none", "novnc"]


class RouteMeta(TypedDict, total=False):
    """Module-level metadata each adapter exposes as ``ROUTE_META``.

    The runtime reads this without importing the adapter — the manifest
    builder collects every ``ROUTE_META`` into a single JSON file at the
    repo root, and the runtime's catalog loads that file at boot.
    """

    domain: str
    """Bare host like ``geico.com`` — used as the catalog's primary key."""

    targets: list[str]
    """Human-readable phrases describing what this route returns. Matched
    against the user's NL intent during catalog lookup. E.g.,
    ``["policy declarations", "auto insurance documents", "dec page"]``."""

    aliases: list[str]
    """Lower-priority match terms — common shorthand, abbreviations."""

    description: str
    """One-sentence description shown in ``GET /api/sites``."""

    mfa_style: MfaStyle
    """``"code_input"`` (SMS/email code), ``"none"`` (no MFA), or ``"novnc"``
    (live-browser handoff — stretch; raises NotImplementedError for now)."""


REQUIRED_META_KEYS = ("domain", "targets", "description")


@dataclass
class Artifact:
    """One file the route produces. Lives in memory until the user downloads."""

    filename: str
    mimetype: str
    data: bytes
    id: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            self.id = self.filename


class Route(ABC):
    """Contract for one (site, intent) pair.

    The runtime drives login → MFA → fetch in a fixed sequence. Each adapter
    only writes the four hooks below.
    """

    def context_options(self) -> dict:
        """Optional Playwright BrowserContext overrides (user_agent, locale,
        viewport, proxy, plus runner-specific keys like ``_launch_chrome_cdp``
        for Akamai-stealth sites)."""
        return {}

    @abstractmethod
    async def login(self, page: Page, username: str, password: str) -> None:
        """Navigate to the site's login page and submit credentials.

        On return the page is either on an MFA challenge or already
        authenticated. Raise ``RuntimeError`` if the site explicitly rejects
        the credentials (so the runtime stops instead of timing out)."""

    @abstractmethod
    async def mfa_required(self, page: Page) -> bool:
        """Return True if the page is currently asking for an MFA code."""

    @abstractmethod
    async def submit_mfa(self, page: Page, code: str) -> None:
        """Fill and submit the MFA code; land on the post-auth page."""

    @abstractmethod
    async def is_authenticated(self, page: Page) -> bool:
        """Cheap probe: are we logged in right now? Used by the quick-path
        when the runtime has a still-warm context.

        Implementations MAY navigate ``page`` (e.g., to a dashboard URL) to
        get a reliable signal. Callers should treat the page as potentially
        mutated after this returns.
        """

    @abstractmethod
    async def fetch(
        self,
        page: Page,
        http: httpx.AsyncClient,
        ctx: BrowserContext,
    ) -> list[Artifact]:
        """Return the artifacts the user asked for.

        ``http`` is an httpx.AsyncClient with the BrowserContext's cookies
        already lifted in — use it for parallel binary downloads. Use
        ``page`` for navigation / DOM scraping."""


@dataclass
class ManifestEntry:
    """One row in ``manifest.json``."""

    route_id: str  # ``geico_com/policy_documents``
    module_path: str  # ``universal_routes.adapters.geico_com.policy_documents``
    class_name: str  # ``GeicoPolicyDocumentsRoute``
    meta: dict = field(default_factory=dict)


_INPUT_TAG_RE = re.compile(r"<input\b[^>]*>", re.I)
_VALUE_ATTR_RE = re.compile(r"""\bvalue=(['"])(.*?)\1""", re.I | re.S)


def sanitize_html_for_debug(html: str) -> str:
    """Redact credential-bearing data from an HTML page dump before disk write.

    Replaces every ``<input ... value="...">`` with ``value="[redacted]"`` —
    we err on the side of redacting more, since visible text inputs may
    carry usernames, SSNs, member numbers, or other identifiers we never
    want persisted, alongside the obvious password fields. Adapters MUST
    call this on every ``page.content()`` they write to disk.
    """

    def _redact(match: re.Match) -> str:
        tag = match.group(0)
        return _VALUE_ATTR_RE.sub(r'value=\1[redacted]\1', tag)

    return _INPUT_TAG_RE.sub(_redact, html)


def validate_meta(meta: dict, source: str) -> None:
    """Raise ValueError if ROUTE_META is missing required keys."""
    missing = [k for k in REQUIRED_META_KEYS if k not in meta]
    if missing:
        raise ValueError(f"{source}: ROUTE_META missing keys {missing}")
    if not isinstance(meta["targets"], list) or not meta["targets"]:
        raise ValueError(f"{source}: ROUTE_META['targets'] must be a non-empty list")
    if not isinstance(meta["domain"], str) or "." not in meta["domain"]:
        raise ValueError(f"{source}: ROUTE_META['domain'] must be a host like 'geico.com'")
    mfa = meta.get("mfa_style", "none")
    if mfa not in ("code_input", "none", "novnc"):
        raise ValueError(f"{source}: ROUTE_META['mfa_style']={mfa!r} not recognized")
