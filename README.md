# universal-api-routes — DEPRECATED

This library has been **merged into**
[github.com/discordwell/the-universal-api](https://github.com/discordwell/the-universal-api)
as a top-level `universal_routes/` subpackage. Install / fork / read
adapters from there.

The history of this repo is preserved for archeology — none of the
code below is loaded by the runtime any more. New auto-built adapters
land in the unified repo (`universal_routes/adapters/`) of
`discordwell/the-universal-api` instead.

---

## What this used to be

Public adapter library for [The Universal API](https://theuniversalapi.com) — a runtime that turns any "personal docs behind a login" website into an API.

Each adapter teaches the runtime how to:

1. Log into one specific site
2. Navigate to one specific kind of data
3. Return that data as files (PDFs, CSVs, JSON, etc.)

The runtime never persists credentials or browser sessions. Adapters here only contain selectors, URLs, and flow logic — no secrets.

## Repo shape (historical)

```
universal_routes/
├── base.py                       # Route ABC + Artifact dataclass
├── build_manifest.py             # python -m universal_routes.build_manifest
├── manifest.json                 # generated; the runtime's fast lookup index
└── adapters/
    └── <domain_underscored>/
        └── <flow_name>.py        # one route per file
```

Each adapter declares `ROUTE_META` at module level:

```python
ROUTE_META = {
    "domain": "geico.com",
    "targets": ["policy declarations", "auto insurance documents", "policy docs"],
    "aliases": ["dec page", "declarations page", "auto policy"],
    "description": "Fetches auto-policy documents from ecams.geico.com.",
    "mfa_style": "code_input",   # "code_input" | "none" | "novnc"
}
```

The directory name under `adapters/` is the load-bearing domain key — `usaa_com/`, `geico_com/`, `amazon_com/`.

## License

MIT — see `LICENSE`.
