# Architecture

> **Status: deprecated.** This library has been merged into
> [`discordwell/the-universal-api`](https://github.com/discordwell/the-universal-api)
> as the `universal_routes/` subpackage. This document describes the design as
> it stood when the repo was frozen, for archeology and for anyone forking the
> adapters. See [`README.md`](./README.md) for the high-level pitch.

## What an adapter is

The Universal API runtime turns "personal documents behind a login" into an
API. An **adapter** is the per-site knowledge the runtime needs to do that for
one (site, intent) pair — e.g. "fetch my auto-policy declarations from Geico".
Adapters hold only selectors, URLs, and flow logic. **No credentials or browser
sessions are ever stored here**; the runtime injects the user's credentials at
call time and discards them after.

## The two contracts

Everything in this repo hangs off two things in [`base.py`](./universal_routes/base.py):

1. **`ROUTE_META`** — a module-level dict every adapter exports. It is the
   catalog/routing metadata (`domain`, `targets`, `aliases`, `description`,
   `mfa_style`). `validate_meta()` enforces its shape.
2. **`Route`** — an ABC with four async hooks the runtime drives in a fixed
   sequence. An adapter implements only these; the runtime owns the
   orchestration, the browser, and the HTTP client.

```
            ┌──────────────── runtime (lives in the unified repo) ───────────────┐
user intent │  catalog lookup → pick Route → login() → mfa_required()? ──yes──┐  │
   ──────────▶                                    │                  submit_mfa()│ │
            │                                     └──no──┐               │       │ │
            │                          is_authenticated()?◀─────────────┘       │ │
            │                                     │ yes                          │ │
            │                                  fetch() ─────▶ list[Artifact] ────┼─▶ files to user
            └────────────────────────────────────────────────────────────────────┘
```

### `Route` hooks (what each adapter writes)

| hook                  | contract |
|-----------------------|----------|
| `context_options()`   | Optional Playwright `BrowserContext` overrides. May include runner-specific keys (e.g. `_launch_chrome_cdp`, `_init_script`) for stealth against bot-managed sites. |
| `login()`             | Submit credentials. Return on an MFA challenge or already authenticated. Raise `RuntimeError` on explicit credential rejection (so the runtime fails fast instead of hanging). |
| `mfa_required()`      | Cheap probe: is the page asking for a code right now? |
| `submit_mfa()`        | Fill + submit the code, land on the post-auth page. |
| `is_authenticated()`  | Cheap probe for the warm-context quick path. *May navigate the page* — callers treat it as mutated afterward. |
| `fetch()`             | Return `list[Artifact]`. Given an `httpx.AsyncClient` with the browser's cookies lifted in (for parallel binary downloads) and the `page` (for DOM scraping). |

`Artifact` is one in-memory file (`filename`, `mimetype`, `data`, `id`). Its
`filename` is sanitized on construction via `safe_filename()` (see below), so an
adapter can hand it a raw server-supplied name without thinking about it.

## Shared download primitives

Adapters fetch binary documents from sites that don't always cooperate, so the
fiddly "is this actually a file and what is it called" logic lives once in
[`base.py`](./universal_routes/base.py) and is reused by every carrier
(composition over copy-paste):

- **`is_pdf_document(body, content_type)`** — guards against a logged-out portal
  answering a document request with `200` + an HTML sign-in page (sometimes even
  typed `application/pdf`). Returns `False` for HTML/empty bodies so junk never
  reaches the user as `document.pdf`; accepts a `%PDF` magic prefix regardless of
  content-type.
- **`filename_from_content_disposition(disposition, url, fallback)`** — picks the
  best name: RFC 5987 extended form (`filename*=UTF-8''…`, percent-decoded) →
  plain `filename=` → URL tail → `fallback`.
- **`safe_filename(name, fallback)`** — reduces a possibly hostile name to one
  safe path segment (strips directory components, control characters, and leading
  dots). `Artifact` applies it at construction, so every naming path is covered at
  one chokepoint and no adapter can forget.

## The manifest (and why it can't drift)

The runtime must not import every adapter at boot just to know what exists, so
[`build_manifest.py`](./universal_routes/build_manifest.py) walks `adapters/`,
collects each `ROUTE_META` (validating it), finds the one `Route` subclass per
module, and writes [`manifest.json`](./universal_routes/manifest.json) — a flat,
sorted index the runtime loads instead.

Re-run it after adding/renaming/removing an adapter:

```
python -m universal_routes.build_manifest
```

**Invariant:** the committed `manifest.json` must equal `build_payload(discover())`.
`tests/test_manifest.py` enforces this, so a forgotten rebuild fails tests
rather than shipping a stale catalog.

## Conventions

- **Directory name is the domain key.** `adapters/<domain_underscored>/` —
  `geico_com/`, `usaa_com/`. The dotted domain in `ROUTE_META["domain"]` must
  match (`usaa_com` ↔ `usaa.com`); the manifest tests check this.
- **One route per file**, one `Route` subclass per file.
- **Debug dumps must be sanitized.** Any adapter that writes `page.content()`
  to disk for debugging MUST pass it through `sanitize_html_for_debug()` first,
  which strips `<input>` values (quoted and unquoted) and `<textarea>` bodies so
  usernames, member numbers, SSNs, and passwords never hit the filesystem.
- **Composition over copy-paste.** A narrow route can wrap a broader one and
  post-filter — see `usaa_com/id_cards.py`, which reuses
  `usaa_com/policy_documents.py`'s login/fetch flow and keeps only the
  ID-card / proof-of-insurance artifacts.

## Tests

`pytest`. The suite is browser-free — it exercises the pure logic that's easy
to get wrong: the meta validator, the HTML sanitizer, manifest/adapter sync,
the ID-card filename filter, and USAA's document classification/ranking
helpers. Browser flows are validated against the live sites by the runtime, not
here.
