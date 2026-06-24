"""USAA — auto insurance ID cards (proof-of-insurance) route.

Reuses the policy_documents adapter's login + document-center flow,
then **filters** the returned PDFs down to those whose filename looks
like an ID card / proof of insurance — so this route honors its
advertised ``targets`` instead of dumping the full 40-page declarations
packet on a user who only wanted their wallet card.

If no card-shaped artifact is found, returns an empty list (and a
warning log) — Phase 7's judge will then surface the empty result to
the user rather than misleading them with the dec packet.
"""

from __future__ import annotations

import logging
import re

import httpx
from playwright.async_api import BrowserContext, Page

from ...base import Artifact, Route
from .policy_documents import UsaaPolicyDocumentsRoute

log = logging.getLogger(__name__)

# Match filenames like "Auto ID Card.pdf", "Proof of Insurance.pdf",
# "wallet-card.pdf", "Insurance ID — Vehicle 1.pdf". Case-insensitive,
# uses a custom boundary that treats ``_`` and ``-`` as separators so
# ``auto_id_card.pdf`` matches but ``Identification.pdf`` does not.
#
# The third boundary alternative catches a camelCase transition (``InsuranceID``,
# ``autoID``) with no separator before the keyword. It MUST stay case-sensitive:
# the pattern as a whole is compiled with ``re.I``, which would otherwise widen
# ``[A-Z]`` to match any letter and make ``(?<=[a-z0-9])(?=[A-Z])`` fire between
# any two letters — so ``id`` would spuriously match inside "Hybrid", "Valid",
# "rapid", etc. ``(?-i:...)`` scopes ignore-case off for just that lookaround.
_BOUNDARY = r"(?:^|[^A-Za-z0-9]|(?-i:(?<=[a-z0-9])(?=[A-Z])))"
_ID_CARD_FILENAME_RE = re.compile(
    _BOUNDARY
    + r"(?:id|i\.d\.|card|wallet|proof[\s_-]*of[\s_-]*insurance|insurance[\s_-]*id)"
    + r"(?:$|[^A-Za-z0-9])",
    re.I,
)

ROUTE_META = {
    "domain": "usaa.com",
    "targets": [
        "auto insurance id card",
        "proof of insurance",
        "insurance id card",
        "auto id card",
    ],
    "aliases": [
        "id card",
        "wallet card",
        "proof",
    ],
    "description": "USAA auto-insurance proof-of-insurance / wallet ID cards.",
    "mfa_style": "code_input",
}


class UsaaIdCardsRoute(Route):
    """Reuses the policy_documents login/fetch flow but post-filters to
    just the ID-card / proof-of-insurance PDFs so the route honors its
    advertised ``targets``."""

    def __init__(self) -> None:
        self._inner = UsaaPolicyDocumentsRoute()

    def context_options(self) -> dict:
        return self._inner.context_options()

    async def login(self, page: Page, username: str, password: str) -> None:
        await self._inner.login(page, username, password)

    async def mfa_required(self, page: Page) -> bool:
        return await self._inner.mfa_required(page)

    async def submit_mfa(self, page: Page, code: str) -> None:
        await self._inner.submit_mfa(page, code)

    async def is_authenticated(self, page: Page) -> bool:
        return await self._inner.is_authenticated(page)

    async def fetch(
        self,
        page: Page,
        http: httpx.AsyncClient,
        ctx: BrowserContext,
    ) -> list[Artifact]:
        all_docs = await self._inner.fetch(page, http, ctx)
        cards = [a for a in all_docs if _ID_CARD_FILENAME_RE.search(a.filename)]
        if not cards:
            log.warning(
                "usaa id_cards: returned %d docs, none matched the "
                "ID-card filename pattern — returning empty list so the "
                "judge surfaces the miss to the user",
                len(all_docs),
            )
        return cards
