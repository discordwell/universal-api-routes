# universal-api-routes

Public adapter library for [The Universal API](https://theuniversalapi.com) — a runtime that turns any "personal docs behind a login" website into an API.

Each adapter teaches the runtime how to:

1. Log into one specific site
2. Navigate to one specific kind of data
3. Return that data as files (PDFs, CSVs, JSON, etc.)

The runtime never persists credentials or browser sessions. Adapters here only contain selectors, URLs, and flow logic — no secrets.

## Repo shape

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

## Safety bar

- Adapters are **read-only**: no destructive actions (delete, transfer, post, purchase).
- Adapters never log credentials. Only flow-relevant state (URLs, selectors, counts).
- The runtime guarantees credentials and cookies stay in memory; adapters must not write them to disk.

## Auto-generated routes

When the runtime encounters a site with no adapter, it spawns Claude inside the production container, which builds an adapter live and force-pushes it to `main` here. Each auto-built adapter's first commit message names the originating job and includes Claude's reasoning.

## License

MIT — see `LICENSE`.
