"""Before USAA hands a downloaded blob to the user it decides three things with
pure helpers: *is this actually a document* (not an HTML error/login page),
*what is it called*, and *which network responses are even worth inspecting*.
These guards are what stop a member from receiving an access-denied page saved
as ``document.pdf``, so they get direct tests (no browser needed)."""

from __future__ import annotations

import pytest

from universal_routes.adapters.usaa_com.policy_documents import (
    UsaaPolicyDocumentsRoute as R,
)


# --------------------------------------------------------- is this a document?


def test_pdf_magic_bytes_win_over_content_type():
    # A real PDF served with a wrong/missing content-type is still a document.
    assert R._is_document_body(b"%PDF-1.7\n...", "application/octet-stream")
    assert R._is_document_body(b"%PDF-1.4", "text/plain")


def test_pdf_or_octet_content_type_is_a_document():
    assert R._is_document_body(b"binary-without-magic", "application/pdf")
    assert R._is_document_body(b"binary-without-magic", "application/octet-stream")


def test_html_body_is_never_a_document():
    # The whole point of the guard: an error/login page must not be returned as
    # a PDF just because the response was typed application/pdf or octet-stream.
    assert not R._is_document_body(b"<!DOCTYPE html><html>...", "application/pdf")
    assert not R._is_document_body(
        b"  \n<html><body>access denied</body></html>", "application/octet-stream"
    )


def test_empty_body_is_not_a_document():
    assert not R._is_document_body(b"", "application/pdf")


def test_plain_text_is_not_a_document():
    assert not R._is_document_body(b"just some text", "text/plain")


# ------------------------------------------------------------- naming the file


def test_name_from_quoted_content_disposition():
    name = R._name_from_headers(
        {"content-disposition": 'attachment; filename="Auto Dec Page.pdf"'},
        "https://www.usaa.com/x?y=1",
        "fallback",
    )
    assert name == "Auto Dec Page.pdf"


def test_name_from_unquoted_content_disposition():
    name = R._name_from_headers(
        {"content-disposition": "inline; filename=card.pdf"},
        "https://www.usaa.com/x",
        "fallback",
    )
    assert name == "card.pdf"


def test_name_falls_back_to_url_tail_ignoring_query():
    name = R._name_from_headers(
        {},
        "https://www.usaa.com/docs/renewal.pdf?token=abc",
        "fallback",
    )
    assert name == "renewal.pdf"


def test_name_falls_back_to_default_when_url_has_no_filename():
    # A directory-style URL with no file component yields the caller's default.
    name = R._name_from_headers(
        {},
        "https://www.usaa.com/my/documents/",
        "USAA document 1",
    )
    assert name == "USAA document 1"


# ----------------------------------------------- which responses to inspect


class _Resp:
    """Minimal stand-in for a Playwright response: just ``url`` + ``headers``."""

    def __init__(self, url: str, content_type: str = "") -> None:
        self.url = url
        self.headers = {"content-type": content_type}


@pytest.mark.parametrize(
    "resp",
    [
        _Resp("https://www.usaa.com/x", "application/pdf"),
        _Resp("https://www.usaa.com/x", "application/octet-stream"),
        _Resp("https://www.usaa.com/file.pdf"),
        _Resp("https://www.usaa.com/inet/.../document/123"),
        _Resp("https://www.usaa.com/inet/.../content/abc"),
    ],
)
def test_document_responses_are_flagged(resp):
    assert R._looks_like_document_response(resp)


def test_ordinary_html_navigation_is_not_flagged():
    assert not R._looks_like_document_response(
        _Resp("https://www.usaa.com/my/usaa", "text/html")
    )
