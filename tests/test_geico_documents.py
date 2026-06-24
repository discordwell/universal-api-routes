"""Geico turns a downloaded blob into an Artifact with one pure helper,
``_artifact_from_download``. Its job is to keep an HTML session-timeout / error
page (which Geico's portal happily serves with status 200) from reaching the
user as a fake ``document.pdf``, and to name real PDFs sensibly. No browser
needed."""

from __future__ import annotations

from universal_routes.adapters.geico_com.policy_documents import (
    GeicoPolicyDocumentsRoute as R,
)


def test_html_error_page_is_dropped():
    # The bug this guards: a logged-out portal returns its sign-in page typed
    # application/pdf; without the guard the user gets that HTML as "document".
    assert (
        R._artifact_from_download(
            "dec-page", b"<!DOCTYPE html><html>sign in</html>", "application/pdf", 0
        )
        is None
    )


def test_empty_body_is_dropped():
    assert R._artifact_from_download("x", b"", "application/pdf", 0) is None


def test_real_pdf_becomes_artifact():
    art = R._artifact_from_download("Dec Page", b"%PDF-1.7", "application/pdf", 2)
    assert art is not None
    assert art.filename == "Dec Page.pdf"
    assert art.id == "doc-2"
    assert art.data.startswith(b"%PDF")


def test_pdf_suffix_not_doubled():
    art = R._artifact_from_download("card.pdf", b"%PDF", "application/pdf", 0)
    assert art.filename == "card.pdf"


def test_pdf_served_as_octet_stream_gets_suffix():
    # Magic bytes win over a vague content-type, and the name still gets .pdf.
    art = R._artifact_from_download(
        "Insurance Card", b"%PDF-1.4", "application/octet-stream", 1
    )
    assert art is not None
    assert art.filename == "Insurance Card.pdf"


def test_blank_link_text_falls_back_to_indexed_name():
    art = R._artifact_from_download("", b"%PDF", "application/pdf", 5)
    assert art is not None
    assert art.filename == "document-5.pdf"


def test_download_filename_is_sanitized():
    # Link text is page-controlled; a traversal-y name is neutralized via Artifact.
    art = R._artifact_from_download(
        "../../secret.pdf", b"%PDF", "application/pdf", 0
    )
    assert art is not None
    assert art.filename == "secret.pdf"
