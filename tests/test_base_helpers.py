"""The shared download primitives in ``base.py`` — ``safe_filename``,
``is_pdf_document`` and ``filename_from_content_disposition`` — are reused by
every carrier adapter and decide what name a user's file gets and whether a
blob is even a document. They run on data the remote site controls, so they get
direct, browser-free coverage."""

from __future__ import annotations

import pytest

from universal_routes.base import (
    filename_from_content_disposition,
    is_pdf_document,
    safe_filename,
)


# -------------------------------------------------------------- safe_filename


def test_safe_filename_keeps_ordinary_names():
    assert safe_filename("Auto Dec Page.pdf") == "Auto Dec Page.pdf"
    assert safe_filename("Insurance ID — Vehicle 1.pdf") == "Insurance ID — Vehicle 1.pdf"
    assert safe_filename("card.pdf") == "card.pdf"


def test_safe_filename_strips_directory_components():
    # Path traversal smuggled via a Content-Disposition or href collapses to the
    # basename — it can never escape the directory the runtime writes into.
    assert safe_filename("../../etc/passwd") == "passwd"
    assert safe_filename("/abs/olute/secret.pdf") == "secret.pdf"
    assert safe_filename(r"..\\..\\windows\\system32\\evil.dll") == "evil.dll"


def test_safe_filename_removes_control_characters():
    assert safe_filename("dec\npage\t.pdf") == "decpage.pdf"
    assert safe_filename("name\x00.pdf") == "name.pdf"
    assert safe_filename("name\x7f.pdf") == "name.pdf"


def test_safe_filename_neutralizes_dot_names_and_dotfiles():
    assert safe_filename(".") == "document"
    assert safe_filename("..") == "document"
    assert safe_filename(".ssh") == "ssh"
    assert safe_filename("  ..hidden.pdf  ") == "hidden.pdf"


def test_safe_filename_falls_back_when_empty():
    assert safe_filename("") == "document"
    assert safe_filename("   ") == "document"
    assert safe_filename(None) == "document"  # defensive: tolerate None
    assert safe_filename("", fallback="usaa-doc-0") == "usaa-doc-0"


# ----------------------------------------------------------- is_pdf_document


def test_is_pdf_document_accepts_magic_bytes_regardless_of_type():
    assert is_pdf_document(b"%PDF-1.7\n...", "application/octet-stream")
    assert is_pdf_document(b"%PDF-1.4", "text/plain")


def test_is_pdf_document_accepts_pdf_or_octet_content_type():
    assert is_pdf_document(b"binary-without-magic", "application/pdf")
    assert is_pdf_document(b"binary-without-magic", "application/octet-stream")


def test_is_pdf_document_rejects_html_even_when_typed_pdf():
    assert not is_pdf_document(b"<!DOCTYPE html><html>...", "application/pdf")
    assert not is_pdf_document(
        b"  \n<HTML><body>access denied</body></html>", "application/octet-stream"
    )


def test_is_pdf_document_rejects_empty_and_plain_text():
    assert not is_pdf_document(b"", "application/pdf")
    assert not is_pdf_document(b"just some text", "text/plain")


# --------------------------------------------- filename_from_content_disposition


def test_cd_prefers_quoted_plain_filename():
    assert (
        filename_from_content_disposition(
            'attachment; filename="Auto Dec Page.pdf"', "https://x/y?z=1", "fallback"
        )
        == "Auto Dec Page.pdf"
    )


def test_cd_handles_unquoted_filename():
    assert (
        filename_from_content_disposition(
            "inline; filename=card.pdf", "https://x/y", "fallback"
        )
        == "card.pdf"
    )


def test_cd_decodes_rfc5987_extended_form():
    # ``filename*=UTF-8''...`` is percent-encoded; the old single-regex matcher
    # returned it verbatim (``Auto%20Dec%20Page.pdf``). It must be decoded.
    assert (
        filename_from_content_disposition(
            "attachment; filename*=UTF-8''Auto%20Dec%20Page.pdf", "https://x/y", "fb"
        )
        == "Auto Dec Page.pdf"
    )


def test_cd_extended_form_wins_over_plain_per_rfc():
    name = filename_from_content_disposition(
        "attachment; filename=\"fallback.pdf\"; filename*=UTF-8''real%20name.pdf",
        "https://x/y",
        "fb",
    )
    assert name == "real name.pdf"


def test_cd_falls_back_to_url_tail_ignoring_query_and_fragment():
    assert (
        filename_from_content_disposition(
            "", "https://www.usaa.com/docs/renewal.pdf?token=abc#page=2", "fb"
        )
        == "renewal.pdf"
    )


def test_cd_falls_back_to_default_for_directory_url():
    assert (
        filename_from_content_disposition(
            "", "https://www.usaa.com/my/documents/", "USAA document 1"
        )
        == "USAA document 1"
    )


@pytest.mark.parametrize("disposition", ['filename=""', "attachment", ""])
def test_cd_empty_filename_falls_through_to_url_tail(disposition):
    # An empty/garbage disposition must not win over a perfectly good URL tail.
    assert (
        filename_from_content_disposition(disposition, "https://x/real.pdf", "fb")
        == "real.pdf"
    )
