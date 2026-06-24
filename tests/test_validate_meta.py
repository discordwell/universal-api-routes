"""``validate_meta`` is the gate that keeps a malformed ``ROUTE_META`` out of
the manifest. Cover the happy path and every rejection branch."""

from __future__ import annotations

import pytest

from universal_routes.base import validate_meta


def _valid_meta() -> dict:
    return {
        "domain": "geico.com",
        "targets": ["policy declarations"],
        "aliases": ["dec page"],
        "description": "Auto-policy documents.",
        "mfa_style": "code_input",
    }


def test_accepts_valid_meta():
    validate_meta(_valid_meta(), "test")  # must not raise


def test_accepts_minimal_meta_without_optional_keys():
    validate_meta(
        {
            "domain": "example.com",
            "targets": ["something"],
            "description": "A thing.",
        },
        "test",
    )


@pytest.mark.parametrize("key", ["domain", "targets", "description"])
def test_rejects_missing_required_key(key):
    meta = _valid_meta()
    del meta[key]
    with pytest.raises(ValueError, match="missing keys"):
        validate_meta(meta, "test")


def test_rejects_empty_targets():
    meta = _valid_meta()
    meta["targets"] = []
    with pytest.raises(ValueError, match="non-empty list"):
        validate_meta(meta, "test")


def test_rejects_non_list_targets():
    meta = _valid_meta()
    meta["targets"] = "policy declarations"
    with pytest.raises(ValueError, match="non-empty list"):
        validate_meta(meta, "test")


def test_rejects_blank_target_entry():
    meta = _valid_meta()
    meta["targets"] = ["valid", "   "]
    with pytest.raises(ValueError, match="non-empty strings"):
        validate_meta(meta, "test")


def test_rejects_domain_without_dot():
    meta = _valid_meta()
    meta["domain"] = "localhost"
    with pytest.raises(ValueError, match="host like"):
        validate_meta(meta, "test")


def test_rejects_blank_description():
    meta = _valid_meta()
    meta["description"] = "   "
    with pytest.raises(ValueError, match="description"):
        validate_meta(meta, "test")


def test_rejects_non_list_aliases():
    meta = _valid_meta()
    meta["aliases"] = "dec page"
    with pytest.raises(ValueError, match="aliases"):
        validate_meta(meta, "test")


def test_rejects_non_string_alias_entry():
    meta = _valid_meta()
    meta["aliases"] = ["ok", 123]
    with pytest.raises(ValueError, match="aliases"):
        validate_meta(meta, "test")


def test_rejects_unknown_mfa_style():
    meta = _valid_meta()
    meta["mfa_style"] = "carrier-pigeon"
    with pytest.raises(ValueError, match="mfa_style"):
        validate_meta(meta, "test")


def test_source_appears_in_error():
    meta = _valid_meta()
    del meta["domain"]
    with pytest.raises(ValueError, match="my.adapter"):
        validate_meta(meta, "my.adapter")
