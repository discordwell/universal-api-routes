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


def test_artifact_sanitizes_path_traversal_filename():
    # A hostile Content-Disposition / href can't smuggle a directory traversal
    # through the artifact the runtime writes to disk.
    art = Artifact(
        filename="../../etc/passwd", mimetype="application/pdf", data=b"%PDF"
    )
    assert art.filename == "passwd"
    assert art.id == "passwd"  # id defaults to the *sanitized* name


def test_artifact_sanitizes_control_characters():
    art = Artifact(
        filename="dec\npage.pdf", mimetype="application/pdf", data=b"%PDF"
    )
    assert art.filename == "decpage.pdf"


def test_artifact_blank_filename_falls_back():
    art = Artifact(filename="   ", mimetype="application/pdf", data=b"%PDF")
    assert art.filename == "document"


def test_artifact_explicit_id_survives_filename_sanitization():
    art = Artifact(
        filename="/tmp/evil.pdf",
        mimetype="application/pdf",
        data=b"%PDF",
        id="doc-7",
    )
    assert art.filename == "evil.pdf"
    assert art.id == "doc-7"
