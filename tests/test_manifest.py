"""Guard the committed ``manifest.json`` against drift.

The runtime loads ``manifest.json`` at boot rather than importing adapters, so
a manifest that lags behind the adapters (someone edited ``ROUTE_META`` but
forgot to re-run the builder) ships broken routing silently. This is the
"safety net" the builder's docstring promises."""

from __future__ import annotations

import json

from universal_routes import build_manifest


def test_committed_manifest_matches_adapters():
    expected = build_manifest.build_payload(build_manifest.discover())
    actual = json.loads(build_manifest.MANIFEST_PATH.read_text())
    assert actual == expected, (
        "manifest.json is stale — run `python -m universal_routes.build_manifest`"
    )


def test_discover_finds_known_adapters():
    route_ids = {e.route_id for e in build_manifest.discover()}
    assert {
        "geico_com/policy_documents",
        "usaa_com/policy_documents",
        "usaa_com/id_cards",
    } <= route_ids


def test_discover_entries_are_internally_consistent():
    for entry in build_manifest.discover():
        # route_id is the module path under adapters/, slash-separated.
        assert entry.module_path.endswith(entry.route_id.replace("/", "."))
        assert entry.module_path.startswith(build_manifest.ADAPTERS_PKG + ".")
        # The directory under adapters/ is the load-bearing domain key, and the
        # meta's declared domain must agree with it (geico_com -> geico.com).
        dir_key = entry.route_id.split("/", 1)[0]
        assert entry.meta["domain"].replace(".", "_") == dir_key
        assert entry.class_name


def test_manifest_is_sorted_by_route_id():
    entries = build_manifest.discover()
    route_ids = [e.route_id for e in entries]
    assert route_ids == sorted(route_ids)
