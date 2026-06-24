"""``sanitize_html_for_debug`` is the only thing standing between a user's
credentials / SSN / member number and a plaintext HTML dump on disk, so it
gets exercised hard: quoted *and* unquoted values, password fields, textarea
bodies, the ">"-inside-a-value truncation trap, and proof that ordinary
(non-secret-bearing) markup survives untouched."""

from __future__ import annotations

from universal_routes.base import sanitize_html_for_debug


def test_redacts_quoted_value():
    out = sanitize_html_for_debug('<input type="text" value="hunter2">')
    assert "hunter2" not in out
    assert 'value="[redacted]"' in out
    # Non-value attributes survive so the dump stays debuggable.
    assert 'type="text"' in out


def test_redacts_single_quoted_value():
    out = sanitize_html_for_debug("<input value='secret-member-id'>")
    assert "secret-member-id" not in out
    assert "value='[redacted]'" in out


def test_redacts_password_field():
    out = sanitize_html_for_debug('<input type="password" value="p@ssw0rd">')
    assert "p@ssw0rd" not in out
    assert 'type="password"' in out


def test_redacts_unquoted_value():
    """The original implementation only handled quoted values; an unquoted
    ``value=secret`` would leak straight through."""
    out = sanitize_html_for_debug("<input type=text value=topsecret>")
    assert "topsecret" not in out
    assert "[redacted]" in out


def test_value_containing_gt_does_not_leak_tail():
    """A naive ``<input[^>]*>`` match stops at the first ">" inside the value
    and leaves the rest exposed; the quote-aware matcher must not."""
    out = sanitize_html_for_debug('<input value="a > b > c" name="ssn">')
    assert "a > b > c" not in out
    assert "[redacted]" in out
    # The trailing attribute (and nothing secret) remains.
    assert 'name="ssn"' in out


def test_redacts_every_input_in_document():
    html = (
        '<form><input name="user" value="alice">'
        '<input name="pass" type="password" value="s3cr3t"></form>'
    )
    out = sanitize_html_for_debug(html)
    assert "alice" not in out
    assert "s3cr3t" not in out
    assert out.count("[redacted]") == 2


def test_redacts_textarea_body():
    out = sanitize_html_for_debug(
        "<textarea name='note'>my SSN is 123-45-6789</textarea>"
    )
    assert "123-45-6789" not in out
    assert "[redacted]" in out
    # Opening tag attributes are preserved.
    assert "name='note'" in out


def test_input_without_value_is_untouched():
    html = '<input type="checkbox" name="remember">'
    assert sanitize_html_for_debug(html) == html


def test_non_input_markup_untouched():
    html = "<div class='balance'>Account value: $1,234</div>"
    # 'value' appears as page text, not an input attribute — must survive.
    assert sanitize_html_for_debug(html) == html


def test_empty_string():
    assert sanitize_html_for_debug("") == ""
