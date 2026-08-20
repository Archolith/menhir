"""CF-35: a Neo4j/LLM URI must never reach a caller or a log with its userinfo intact.

`NEO4J_URI` is supported in the `neo4j://user:password@host:7687` form. `_neo4j_dependency_snapshot`
parsed that URI to derive host and port and then returned the ORIGINAL string anyway, and
`SystemMetadataResource` re-disclosed it through `get_provider_config`. Both are *resources*, which
under CF-16 carry no tier requirement, so the lowest authenticated caller reads them.

The invariant under test is not "resources.py redacts" but "no disclosure boundary emits userinfo",
so the payload-level tests below assert against the whole rendered payload, not one field.
"""

from __future__ import annotations

import pytest

from menhir.config import redact_uri_credentials
from menhir.config.settings_helpers import UNPARSEABLE_URI

CANARY = "s3cr3t-pw"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("neo4j://user:s3cr3t-pw@db.internal:7687", "neo4j://db.internal:7687"),
        ("bolt+s://admin:s3cr3t-pw@host", "bolt+s://host"),
        # IPv6: `hostname` strips the brackets, so a naive rebuild yields `::1:7687`, where the
        # port is indistinguishable from another hextet.
        ("neo4j://user:s3cr3t-pw@[::1]:7687", "neo4j://[::1]:7687"),
        # userinfo with no password half is still userinfo.
        ("neo4j://someuser@host:7687", "neo4j://host:7687"),
    ],
)
def test_userinfo_is_stripped(raw: str, expected: str) -> None:
    out = redact_uri_credentials(raw)
    assert out == expected
    assert CANARY not in out


@pytest.mark.unit
@pytest.mark.parametrize(
    "raw",
    [
        "bolt://localhost:7687",
        "http://127.0.0.1:8080/v1",
        "neo4j://host:7687",
        # An `@` in the PATH or QUERY is not userinfo; `netloc` covers the authority only.
        "http://host/path?contact=a@b.com",
    ],
)
def test_credential_free_uris_pass_through_byte_for_byte(raw: str) -> None:
    """POSITIVE CONTROL: without this, every assertion above would pass against a function
    that returned the empty string for everything."""
    assert redact_uri_credentials(raw) == raw


@pytest.mark.unit
def test_unparseable_authority_fails_closed_rather_than_passing_through() -> None:
    """A port urlparse refuses raises on ATTRIBUTE ACCESS, not at parse time."""
    out = redact_uri_credentials("neo4j://user:s3cr3t-pw@host:99999")
    assert out == UNPARSEABLE_URI
    assert CANARY not in out


@pytest.mark.unit
@pytest.mark.parametrize("raw", ["", None])
def test_blank_input_is_blank_output(raw: str | None) -> None:
    assert redact_uri_credentials(raw) == ""


@pytest.mark.unit
def test_redaction_is_idempotent() -> None:
    once = redact_uri_credentials("neo4j://user:s3cr3t-pw@host:7687")
    assert redact_uri_credentials(once) == once


@pytest.mark.unit
def test_dependency_snapshot_payload_carries_no_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    """CALLER BOUNDARY: the redaction has to be reached by the real resource, not merely exist."""
    from menhir.mcp import resources

    monkeypatch.setenv("NEO4J_URI", f"neo4j://neo4j:{CANARY}@127.0.0.1:7687")
    monkeypatch.setattr(resources, "_socket_reachable", lambda *a, **k: False)

    snapshot = resources._neo4j_dependency_snapshot()

    assert CANARY not in repr(snapshot), f"credential disclosed in {snapshot!r}"
    assert snapshot["uri"] == "neo4j://127.0.0.1:7687"
    # Control: the snapshot is still useful -- it did not degrade to blanks.
    assert snapshot["host"] == "127.0.0.1"
    assert snapshot["port"] == 7687


@pytest.mark.unit
def test_provider_config_redacts_every_url_it_discloses(monkeypatch: pytest.MonkeyPatch) -> None:
    """`neo4j_uri` was the reported field, but the same dict discloses three other URLs through
    SystemMetadataResource. Redacting only the reported one would leave the class of bug open.

    Built from a REAL `MemorySettings`, not a stub: `get_provider_config` constructs four
    `ProviderConfig` objects from the same settings, so a stub thin enough to isolate the URLs
    would not survive the call it is meant to exercise.
    """
    import asyncio

    from menhir.config import MemorySettings
    from menhir.core.backend_runtime_admin_ops import RuntimeProviderAdminOpsMixin

    monkeypatch.setenv("NEO4J_URI", f"neo4j://neo4j:{CANARY}@127.0.0.1:7687")
    monkeypatch.setenv("LOCAL_LLM_BASE_URL", f"http://u:{CANARY}@127.0.0.1:8080/v1")
    monkeypatch.setenv("LOCAL_LLM_EMBED_BASE_URL", f"http://u:{CANARY}@127.0.0.1:8081/v1")
    monkeypatch.setenv("MENHIR_BACKEND_URL", f"http://u:{CANARY}@127.0.0.1:8090")

    class _Built:
        settings = MemorySettings.from_env()

    class _Ops(RuntimeProviderAdminOpsMixin):
        built = _Built()

    config = asyncio.run(_Ops().get_provider_config())

    assert CANARY not in repr(config), f"credential disclosed in {config!r}"
    for key in ("neo4j_uri", "local_llm_base_url", "local_llm_embed_base_url", "backend_url"):
        assert config[key], f"{key} degraded to blank instead of being redacted"
        assert "@" not in config[key], f"{key} still carries userinfo: {config[key]!r}"
    # Control: the values are still the real endpoints, not placeholders.
    assert config["neo4j_uri"] == "neo4j://127.0.0.1:7687"


@pytest.mark.unit
def test_runtime_failure_annotation_carries_no_credential() -> None:
    """The operator-facing failure string is a log/console surface, so it is a boundary too."""
    from menhir.core.runtime_support import _annotate_runtime_failures

    class _Settings:
        neo4j_uri = f"neo4j://neo4j:{CANARY}@127.0.0.1:7687"

    out = _annotate_runtime_failures(["Neo4j connectivity check failed."], _Settings())

    assert CANARY not in out[0]
    # Control: the annotation still happened and still names the endpoint.
    assert "127.0.0.1:7687" in out[0]
    assert "Start Docker Desktop" in out[0]
