"""Behaviour of the small but load-bearing primitives in ``base.py``."""

from __future__ import annotations

from universal_routes.base import Artifact


def test_artifact_id_defaults_to_filename():
    art = Artifact(filename="dec-page.pdf", mimetype="application/pdf", data=b"%PDF")
    assert art.id == "dec-page.pdf"


def test_artifact_explicit_id_is_kept():
    art = Artifact(
        filename="dec-page.pdf",
        mimetype="application/pdf",
        data=b"%PDF",
        id="doc-7",
    )
    assert art.id == "doc-7"


def test_artifact_holds_raw_bytes():
    art = Artifact(filename="x.pdf", mimetype="application/pdf", data=b"%PDF-1.7\n...")
    assert art.data.startswith(b"%PDF")
