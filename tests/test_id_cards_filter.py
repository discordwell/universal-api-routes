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


def test_matches_camelcase_keyword_without_separator():
    """A camelCase transition is a real boundary even with no ``-``/``_``/space:
    ``InsuranceID`` and ``autoID`` end in the ``id`` keyword and must match."""
    assert _ID_CARD_FILENAME_RE.search("InsuranceID.pdf")
    assert _ID_CARD_FILENAME_RE.search("autoID.pdf")
    # All-caps runs the keyword together with the prefix but the
    # ``insurance...id`` keyword still anchors it at the start.
    assert _ID_CARD_FILENAME_RE.search("INSURANCEID.pdf")


def test_matches_concatenated_pascalcase_names():
    """A keyword that ends an acronym run and butts straight into the next
    PascalCase word (``IDCard`` → ``ID`` | ``Card``) must still match — these
    are natural no-separator spellings of the route's advertised targets."""
    assert _ID_CARD_FILENAME_RE.search("AutoIDCard.pdf")
    assert _ID_CARD_FILENAME_RE.search("IDCard.pdf")
    assert _ID_CARD_FILENAME_RE.search("InsuranceIDCard.pdf")
    assert _ID_CARD_FILENAME_RE.search("MemberIDCard.pdf")


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


def test_does_not_match_words_ending_in_id():
    """Regression: the camelCase boundary ``(?<=[a-z0-9])(?=[A-Z])`` must stay
    case-sensitive. Under the pattern's ``re.I`` flag, ``[A-Z]`` widens to any
    letter, which made ``id`` fire between any two letters — so a word ending in
    ``id`` before a separator ("Hybrid Vehicle", "Valid Documents") was wrongly
    flagged as an ID card and the user's renewal packet leaked through the
    filter as if it were their wallet card."""
    assert not _ID_CARD_FILENAME_RE.search("Hybrid Vehicle Policy.pdf")
    assert not _ID_CARD_FILENAME_RE.search("Valid Documents.pdf")
    assert not _ID_CARD_FILENAME_RE.search("rapid-response.pdf")
    assert not _ID_CARD_FILENAME_RE.search("Squid.pdf")
    assert not _ID_CARD_FILENAME_RE.search("candidate.pdf")
    # The trailing acronym→word boundary must not re-open the same hole: a
    # word merely *containing* "id" before a PascalCase word still must not
    # match (the "id" here is mid-word, with no real boundary before it).
    assert not _ID_CARD_FILENAME_RE.search("AndroidApp.pdf")
    assert not _ID_CARD_FILENAME_RE.search("GridView.pdf")
    assert not _ID_CARD_FILENAME_RE.search("MadridReport.pdf")
