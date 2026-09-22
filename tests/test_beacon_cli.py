"""`menhir beacon generate` boundary: expected refusals are one line and a stable exit code.

Before PR #125 F7 a stale-index refusal ("re-ingest first"), the normal operator outcome,
printed a Typer/Rich traceback. Unexpected errors must still propagate so they are not
mistaken for refusals.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from menhir.cli import beacon as cli_module
from menhir.cli.beacon import beacon_app
from menhir.services import beacon_generation as generation_module
from menhir.services.beacon_compat import BeaconCompatError
from menhir.services.beacon_generation import BeaconGenerationError

# A single-command Typer app runs its command at the root, so no "generate" token here;
# through `menhir` it is `menhir beacon generate ...`.
#: The stable refusal exit code (`menhir.cli.beacon.EXIT_REFUSED`), spelled out so a change
#: to the contract fails this test rather than following it.
EXIT_REFUSED = 2

_ARGS = ["proj", "--repo", "C:/repo", "--beacon-python", "C:/venv/python.exe"]


@pytest.fixture
def cli(monkeypatch: pytest.MonkeyPatch):
    """A generate command whose graph connection is a closed-tracking stub."""
    neo4j = SimpleNamespace(closed=False)
    neo4j.close = lambda: setattr(neo4j, "closed", True)
    monkeypatch.setattr(cli_module, "_reader", lambda: (object(), neo4j))

    def run(raises: BaseException | None = None):
        def fake_generate(*args, **kwargs):
            if raises is not None:
                raise raises
            return generation_module.GenerationOutcome(
                output_path=kwargs.get("repo_root", "x"), sha256="ab" * 32, created=True,
                scan_fingerprint="fp", git_head=None,
            )

        monkeypatch.setattr(generation_module, "generate_beacon", fake_generate)
        return CliRunner().invoke(beacon_app, _ARGS), neo4j

    return run


@pytest.mark.parametrize(
    "error",
    [
        BeaconGenerationError("project index is stale for the current checkout; re-ingest first: proj"),
        BeaconCompatError("beacon subprocess failed (2): build_no_canonical_docs"),
        OSError(30, "Read-only file system"),
    ],
    ids=["stale-index", "beacon-failure", "os-error"],
)
def test_expected_refusal_is_one_line_and_exit_2(cli, error) -> None:
    result, neo4j = cli(error)

    assert result.exit_code == EXIT_REFUSED == cli_module.EXIT_REFUSED
    assert "Traceback" not in result.output
    lines = [line for line in result.output.splitlines() if line.strip()]
    assert len(lines) == 1
    assert lines[0].startswith("beacon generate refused: ")
    assert str(error).split(":")[-1].strip()[:20] in lines[0]
    assert neo4j.closed, "the graph connection is closed on refusal"


def test_refusal_message_is_capped(cli) -> None:
    result, _ = cli(BeaconCompatError("x" * 20_000))

    assert result.exit_code == EXIT_REFUSED
    assert len(result.output) < 5_000
    assert "[truncated]" in result.output


def test_unexpected_error_still_propagates(cli) -> None:
    result, neo4j = cli(RuntimeError("bug"))

    assert result.exit_code != EXIT_REFUSED
    assert isinstance(result.exception, RuntimeError)
    assert neo4j.closed


def test_success_reports_outcome(cli) -> None:
    result, _ = cli()

    assert result.exit_code == 0, result.output
    assert "sha256: " + "ab" * 32 in result.output
    assert "git_head: none" in result.output
