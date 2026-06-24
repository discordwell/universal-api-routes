"""The id_cards route advertises ID-card / proof-of-insurance only —
verify the filename filter actually narrows down the upstream policy
docs."""

from __future__ import annotations

from universal_routes.adapters.usaa_com.id_cards import _ID_CARD_FILENAME_RE


def test_matches_obvious_id_card_names():
    assert _ID_CARD_FILENAME_RE.search("Auto ID Card.pdf")
    assert _ID_CARD_FILENAME_RE.search("auto_id_card.pdf")
    assert _ID_CARD_FILENAME_RE.search("Proof of Insurance.pdf")
    assert _ID_CARD_FILENAME_RE.search("proof-of-insurance.pdf")
    assert _ID_CARD_FILENAME_RE.search("Insurance ID — Vehicle 1.pdf")
    assert _ID_CARD_FILENAME_RE.search("wallet-card.pdf")


def test_does_not_match_dec_pages():
    assert not _ID_CARD_FILENAME_RE.search("Policy Declarations.pdf")
    assert not _ID_CARD_FILENAME_RE.search("declarations-page.pdf")
    assert not _ID_CARD_FILENAME_RE.search("Renewal Packet.pdf")
    assert not _ID_CARD_FILENAME_RE.search("auto-policy.pdf")


def test_does_not_match_substring_only():
    """``Identification`` contains the substring ``id`` but the regex
    uses word boundaries so it doesn't accidentally match."""
    assert not _ID_CARD_FILENAME_RE.search("Identification.pdf")
    assert not _ID_CARD_FILENAME_RE.search("Provider.pdf")
