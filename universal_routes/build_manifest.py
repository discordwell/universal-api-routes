"""Walk ``adapters/``, collect ``ROUTE_META`` from each module, emit ``manifest.json``.

Run:

    python -m universal_routes.build_manifest

The runtime reads ``manifest.json`` at boot for catalog lookup. Re-run this
after adding, renaming, or deleting an adapter. A GitHub Actions workflow
also re-runs it on every push to main as a safety net.
"""

from __future__ import annotations

import importlib
import inspect
import json
import pkgutil
import sys
from pathlib import Path

from .base import ManifestEntry, Route, validate_meta

ADAPTERS_PKG = "universal_routes.adapters"
MANIFEST_PATH = Path(__file__).parent / "manifest.json"


def discover() -> list[ManifestEntry]:
    entries: list[ManifestEntry] = []
    pkg = importlib.import_module(ADAPTERS_PKG)
    for _, mod_name, is_pkg in pkgutil.walk_packages(pkg.__path__, prefix=f"{ADAPTERS_PKG}."):
        if is_pkg:
            continue
        mod = importlib.import_module(mod_name)
        meta = getattr(mod, "ROUTE_META", None)
        if meta is None:
            continue
        validate_meta(meta, mod_name)
        route_cls = _find_route_class(mod)
        if route_cls is None:
            raise ValueError(f"{mod_name}: declares ROUTE_META but no Route subclass found")
        suffix = mod_name[len(ADAPTERS_PKG) + 1 :].replace(".", "/")
        entries.append(
            ManifestEntry(
                route_id=suffix,
                module_path=mod_name,
                class_name=route_cls.__name__,
                meta=dict(meta),
            )
        )
    entries.sort(key=lambda e: e.route_id)
    return entries


def _find_route_class(mod) -> type[Route] | None:
    for _, obj in inspect.getmembers(mod, inspect.isclass):
        if obj is Route or not issubclass(obj, Route):
            continue
        if obj.__module__ == mod.__name__:
            return obj
    return None


def write_manifest(entries: list[ManifestEntry]) -> Path:
    payload = {
        "version": 1,
        "routes": [
            {
                "route_id": e.route_id,
                "module_path": e.module_path,
                "class_name": e.class_name,
                "meta": e.meta,
            }
            for e in entries
        ],
    }
    MANIFEST_PATH.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n")
    return MANIFEST_PATH


def main() -> int:
    try:
        entries = discover()
    except ValueError as e:
        print(f"manifest build failed: {e}", file=sys.stderr)
        return 1
    path = write_manifest(entries)
    print(f"wrote {len(entries)} routes → {path}")
    for e in entries:
        print(f"  {e.route_id}  ({e.meta.get('domain')})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
