"""CF-97: diagnostic URL redaction stripped only userinfo, so query secrets printed verbatim.

`redact_url_for_diagnostics` promised in its name to sanitize a URL for operator display and
delivered only `user:pass@` removal. `MENHIR_BACKEND_URL=https://backend.example/p?token=<secret>`
has no userinfo component at all, so the function returned it unchanged and `menhir diagnostics`
printed the token.

**The census matters more than the one call site.** Three separate userinfo-only redactors existed:

  * `config/settings_helpers.py::redact_uri_credentials` -- careful: IPv6-aware, degrades to
    `UNPARSEABLE_URI` rather than passing a URI it cannot prove is clean.
  * `mcp/service_access.py::redact_url_for_diagnostics` -- naive, and fails OPEN
    (`except Exception: return raw`).
  * `api/oauth_preflight.py::_redact_url_credentials` -- a byte-for-byte copy of the second.

So two of the three failed open where the third failed closed, on the same input class.

**`redact_uri_credentials` was deliberately NOT changed**, and that is the load-bearing decision
here. Its docstring promises a userinfo-free URI returned byte-for-byte, and callers depend on
that promise for more than display: `_normalize_embed_stamp_base`
(`infrastructure/view_embedder.py:107-116`) persists its output as the `embed_version` stamp, and
`backfill_assertion_embeddings` re-embeds every row whose stamped version differs. Dropping the
query there would silently change a stored identity and trigger a mass re-embed. Display-grade
reduction is therefore a SECOND function layered on top, not a tightening of the first.

**The message path had the same hole one layer over.** `build_oauth_preflight` interpolates
`metadata_url` straight into two check messages (`:238`, `:244`), and `_redact_url_in_message`
substituted only the `user:pass@` form. Fixing the structured fields alone would have left the
identical leak in the string printed beside them.
"""

from __future__ import annotations

import pytest

from menhir.api.oauth_preflight import _redact_url_credentials, _redact_url_in_message
from menhir.config import redact_uri_credentials, redact_uri_for_display
from menhir.config.settings_helpers import UNPARSEABLE_URI
from menhir.mcp.service_access import redact_url_for_diagnostics

pytestmark = pytest.mark.unit

SECRET_IN_QUERY = "https://backend.example/path?token=SECRET"


# ---------------------------------------------------------------------------
# the finding
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reduce",
    [redact_uri_for_display, redact_url_for_diagnostics, _redact_url_credentials],
    ids=["shared", "mcp_diagnostics", "oauth_preflight"],
)
def test_a_credential_in_the_query_string_does_not_survive(reduce) -> None:
    """The finding, asserted against every display-grade entry point. Each of these three
    returned the URL verbatim, secret included."""
    out = reduce(SECRET_IN_QUERY)

    assert "SECRET" not in out
    assert "token" not in out
    assert out == "https://backend.example/path"


@pytest.mark.parametrize(
    "reduce",
    [redact_uri_for_display, redact_url_for_diagnostics, _redact_url_credentials],
    ids=["shared", "mcp_diagnostics", "oauth_preflight"],
)
def test_a_credential_in_the_fragment_does_not_survive(reduce) -> None:
    """Same defect, other tail component. Nothing in the old code looked at `fragment` either."""
    assert reduce("https://h/p#access_token=SECRET") == "https://h/p"


def test_userinfo_is_still_removed_and_still_marked() -> None:
    """POSITIVE CONTROL: the guarantee the old function DID offer must not be lost while adding
    the new one. A reducer that only dropped the query would pass every test above.

    The `***:***` marker is kept deliberately -- `redact_uri_credentials` deletes userinfo
    outright, which is right for a value that may be re-read as configuration, but a human
    reading diagnostics cannot tell `http://host:8099` from a URL that never had a credential.
    Four pre-existing tests in `test_mcp_backend_client_hardening.py` pin this marker."""
    assert redact_uri_for_display("http://user:pass@host:8099/path") == (
        "http://***:***@host:8099/path"
    )
    assert redact_uri_for_display("http://user:pass@host:8099/p?k=v") == (
        "http://***:***@host:8099/p"
    )
    assert redact_uri_for_display("http://user@host:8099") == "http://***:***@host:8099"


def test_a_clean_url_is_returned_unchanged() -> None:
    """POSITIVE CONTROL: a reducer that returned a constant, or that mangled ordinary URLs,
    would satisfy every leak test above. Diagnostics have to stay useful."""
    for url in ("https://backend.example/path", "http://127.0.0.1:8099", "https://h/a/b/c"):
        assert redact_uri_for_display(url) == url


# ---------------------------------------------------------------------------
# failing closed
# ---------------------------------------------------------------------------


def test_an_unparseable_uri_degrades_instead_of_passing_through() -> None:
    """Both replaced implementations ended in `except Exception: return raw` -- they failed OPEN,
    returning the unredacted string precisely when they could not prove it was safe. The shared
    reducer inherits the credential-grade function's fail-closed behaviour instead."""
    bad = "http://u:p@h:99999/x?t=SECRET"  # port out of range; urlparse defers the error

    assert redact_uri_for_display(bad) == UNPARSEABLE_URI
    assert redact_url_for_diagnostics(bad) == UNPARSEABLE_URI
    assert _redact_url_credentials(bad) == UNPARSEABLE_URI


def test_empty_and_none_are_the_empty_string() -> None:
    assert redact_uri_for_display("") == ""
    assert redact_uri_for_display(None) == ""


def test_an_ipv6_literal_keeps_its_brackets() -> None:
    """Inherited from the credential-grade base. Without the brackets `::1:7687` reads as another
    hextet rather than a port -- which is why the naive copies should never have existed."""
    assert redact_uri_for_display("bolt://neo4j:pw@[::1]:7687") == "bolt://***:***@[::1]:7687"


# ---------------------------------------------------------------------------
# the two functions are deliberately different, and must stay that way
# ---------------------------------------------------------------------------


def test_the_credential_grade_redactor_still_returns_queries_byte_for_byte() -> None:
    """THE REGRESSION THAT WOULD HURT MOST. `redact_uri_credentials` feeds
    `_normalize_embed_stamp_base`, whose output is persisted as `embed_version` and compared
    against on every backfill. If someone "consolidates" the two functions by making this one
    drop the query, every stamped row silently mismatches and re-embeds.

    This test exists to make that consolidation fail loudly."""
    assert redact_uri_credentials(SECRET_IN_QUERY) == SECRET_IN_QUERY
    assert redact_uri_credentials("https://h/p#f") == "https://h/p#f"


@pytest.mark.parametrize(
    "url",
    [SECRET_IN_QUERY, "http://u:p@h/x?k=v", "https://h/p", "bolt://n:p@[::1]:7687", "http://h:1"],
)
def test_display_never_discloses_more_than_credential_grade(url: str) -> None:
    """The relationship between the two, pinned as a property rather than a table: whatever the
    credential-grade function removed stays removed, and the display one additionally carries no
    query or fragment. Only the mask may be ADDED."""
    disp = redact_uri_for_display(url)

    assert "?" not in disp and "#" not in disp
    assert "u:p@" not in disp and "n:p@" not in disp
    assert disp.replace("***:***@", "") in redact_uri_credentials(url).split("?")[0]


# ---------------------------------------------------------------------------
# the message path
# ---------------------------------------------------------------------------


def test_a_url_interpolated_into_a_check_message_is_reduced() -> None:
    """`build_oauth_preflight` puts `metadata_url` straight into this sentence at `:238`.
    The old regex matched only `(https?://)[^@\\s]+@`, so this passed through untouched."""
    msg = _redact_url_in_message(
        "Protected-resource metadata available at https://b.ex/.well-known/x?token=SECRET."
    )

    assert "SECRET" not in msg
    assert msg == "Protected-resource metadata available at https://b.ex/.well-known/x."


def test_sentence_punctuation_survives_the_reduction() -> None:
    """The greedy `\\S+` match swallows a trailing period; it is peeled back before reducing.
    Without that, operator messages would lose their punctuation into the path."""
    assert _redact_url_in_message("see https://h/a, then https://h/b.") == (
        "see https://h/a, then https://h/b."
    )
    assert _redact_url_in_message("(https://h/a?k=v)") == "(https://h/a)"


def test_userinfo_in_a_message_is_still_removed() -> None:
    """POSITIVE CONTROL: the only thing the old regex did must still happen, marker included."""
    out = _redact_url_in_message("bad: http://u:p@h/x here")

    assert "u:p@" not in out
    assert out == "bad: http://***:***@h/x here"


def test_a_message_with_no_url_is_untouched() -> None:
    """POSITIVE CONTROL: a substitution that rewrote arbitrary prose would pass the leak tests."""
    text = "MENHIR_OAUTH_ISSUER is missing a trailing slash - issuer matching may be strict."
    assert _redact_url_in_message(text) == text


def test_multiple_urls_in_one_message_are_each_reduced() -> None:
    msg = _redact_url_in_message("a https://x/1?s=A and b https://y/2#s=B done")

    assert msg == "a https://x/1 and b https://y/2 done"


# ---------------------------------------------------------------------------
# end to end through the two report builders
# ---------------------------------------------------------------------------


def test_the_mcp_diagnostics_block_reports_a_reduced_backend_url(monkeypatch) -> None:
    """The actual disclosure path: `build_mcp_backend_diagnostics` -> `operator_diagnostics`
    -> `menhir diagnostics` stdout."""
    from menhir.mcp import service_access

    monkeypatch.setattr(
        service_access, "resolve_mcp_backend_url", lambda *a, **k: SECRET_IN_QUERY
    )
    monkeypatch.setattr(
        service_access, "backend_client_mode_enabled", lambda *a, **k: True
    )
    monkeypatch.setattr(
        service_access, "resolve_mcp_backend_auth_key", lambda *a, **k: "k"
    )

    class _S:
        agent_key = "k"
        api_key = ""

    block = service_access.build_mcp_backend_diagnostics(_S())

    assert block["backend_url"] == "https://backend.example/path"
    assert "SECRET" not in repr(block)
