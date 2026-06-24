"""The USAA adapter classifies, keys, and ranks document rows with a stack of
pure helpers (no browser needed). They encode real business rules — "a billing
statement is not a policy document", "newest renewal wins", "two policies with
different last-4 are distinct" — so they deserve direct tests."""

from __future__ import annotations

import pytest

from universal_routes.adapters.usaa_com.policy_documents import (
    UsaaPolicyDocumentsRoute as R,
)


# ---------------------------------------------------------------- text helpers


def test_normalize_collapses_whitespace():
    assert R._normalize_usaa_text("  Auto\n  Policy   Renewal ") == "Auto Policy Renewal"


@pytest.mark.parametrize("text", ["View", "actions", "Download", "Open", "READ"])
def test_actionish_button_text_detected(text):
    assert R._is_actionish_document_button_text(text)


@pytest.mark.parametrize("text", ["Auto Policy", "Declarations", "", "ID Card"])
def test_non_actionish_button_text(text):
    assert not R._is_actionish_document_button_text(text)


# ----------------------------------------------------------- document kind


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Auto Policy Declarations", "initial"),
        ("New Policy Packet", "initial"),
        ("Auto Policy Renewal", "renewal"),
        ("2024 Renewal Notice", "renewal"),
        ("Policy Change Confirmation", "change"),
        ("Auto Insurance ID Card", None),
        ("Billing Statement", None),
        ("Payment Receipt", None),
    ],
)
def test_document_kind_classification(title, expected):
    assert R._usaa_policy_document_kind(title) == expected


def test_billing_declarations_is_not_a_policy_document():
    # A "declarations" row that is really a billing statement must be excluded,
    # otherwise the user gets an invoice when they asked for their dec page.
    assert R._usaa_policy_document_kind("Declarations Billing Statement") is None


def test_kind_priority_orders_initial_first():
    assert (
        R._usaa_document_kind_priority("initial")
        < R._usaa_document_kind_priority("renewal")
        < R._usaa_document_kind_priority("change")
        < R._usaa_document_kind_priority("fallback")
    )


# --------------------------------------------------------- account / dates


@pytest.mark.parametrize(
    "value,expected",
    [
        ("**** 1234", "1234"),
        ("Auto ****-5678", "5678"),
        ("****12", "12"),
        ("no mask here", None),
        ("123456", None),  # digits without a masking prefix don't count
    ],
)
def test_masked_account_tail(value, expected):
    assert R._usaa_masked_account_tail(value) == expected


@pytest.mark.parametrize(
    "value,expected",
    [
        ("Delivered 03/15/2024", 20240315),
        ("1/2/2020", 20200102),
        ("no date", 0),
    ],
)
def test_date_sort_value(value, expected):
    assert R._usaa_date_sort_value(value) == expected


@pytest.mark.parametrize(
    "value,family",
    [
        ("Auto Policy", "auto"),
        ("Renters Insurance", "renters"),
        ("Homeowners Declarations", "homeowners"),
        ("Condo Policy", "condo"),
        ("Home Dwelling Coverage", "property"),
        ("Umbrella Coverage", "policy"),
    ],
)
def test_policy_family(value, family):
    assert R._usaa_policy_family(value) == family


# --------------------------------------------------------------- policy key


def test_policy_key_uses_account_tail_when_present():
    assert R._usaa_policy_key("Auto Policy Renewal", "**** 1234", "") == "auto:1234"


def test_policy_key_distinguishes_accounts_in_same_family():
    a = R._usaa_policy_key("Auto Policy Renewal", "**** 1111", "")
    b = R._usaa_policy_key("Auto Policy Renewal", "**** 2222", "")
    assert a != b
    assert a.startswith("auto:") and b.startswith("auto:")


def test_policy_key_keeps_distinguishing_token_without_account():
    # "boat" is not one of the stripped boilerplate words, so it survives as
    # the distinguishing tail of the key.
    assert R._usaa_policy_key("Boat Policy Declarations", "", "") == "policy:boat"


# ------------------------------------------------------------------ ranking


def _row(index, title, date, account=""):
    return {
        "index": index,
        "title": title,
        "buttonText": "",
        "dateDelivered": date,
        "account": account,
        "rowText": "",
    }


def test_ranking_drops_non_policy_rows():
    ranked = R._rank_usaa_document_button_candidates(
        [
            _row(0, "Billing Statement", "01/01/2024"),
            _row(1, "Auto Policy Declarations", "06/01/2023", "**** 1111"),
            _row(2, "Auto Policy Renewal", "06/01/2024", "**** 1111"),
        ]
    )
    kinds = [c.document_kind for c in ranked]
    assert "fallback" not in kinds
    assert all(k in ("initial", "renewal", "change") for k in kinds)
    assert len(ranked) == 2  # billing statement excluded


def test_ranking_prefers_newest_then_kind():
    ranked = R._rank_usaa_document_button_candidates(
        [
            _row(0, "Auto Policy Declarations", "06/01/2023", "**** 1111"),
            _row(1, "Auto Policy Renewal", "06/01/2024", "**** 1111"),
        ]
    )
    # Newest delivered date wins regardless of kind.
    assert [c.index for c in ranked] == [1, 0]


def test_ranking_breaks_date_ties_by_kind():
    ranked = R._rank_usaa_document_button_candidates(
        [
            _row(0, "Auto Policy Renewal", "06/01/2024", "**** 1111"),
            _row(1, "Auto Policy Declarations", "06/01/2024", "**** 1111"),
        ]
    )
    # Same date: an "initial"/declarations doc outranks a renewal.
    assert [c.document_kind for c in ranked] == ["initial", "renewal"]


# ------------------------------------------------- artifact assembly helpers


def test_single_artifact_appends_pdf_suffix_for_pdf_bytes():
    route = R()
    [art] = route._single_artifact(
        b"%PDF-1.7", "application/octet-stream", "Auto ID Card", index=0
    )
    assert art.filename == "Auto ID Card.pdf"
    assert art.id == "usaa-doc-0"


def test_single_artifact_does_not_double_suffix():
    route = R()
    [art] = route._single_artifact(b"%PDF", "application/pdf", "card.pdf", index=1)
    assert art.filename == "card.pdf"


def test_single_artifact_leaves_non_pdf_alone():
    route = R()
    [art] = route._single_artifact(b"plain text", "text/plain", "notes.txt", index=2)
    assert art.filename == "notes.txt"


def test_merge_artifacts_dedupes_by_content_and_skips_empty():
    target: list = []
    seen: set = set()
    R._merge_artifacts(
        target,
        seen,
        [
            Artifact_("a.pdf", b"same"),
            Artifact_("b.pdf", b"same"),  # duplicate bytes -> dropped
            Artifact_("c.pdf", b"other"),
            Artifact_("d.pdf", b""),  # empty -> skipped
        ],
    )
    assert [a.filename for a in target] == ["a.pdf", "c.pdf"]
    assert [a.id for a in target] == ["usaa-doc-0", "usaa-doc-1"]


def Artifact_(filename: str, data: bytes):
    from universal_routes.base import Artifact

    return Artifact(filename=filename, mimetype="application/pdf", data=data)
