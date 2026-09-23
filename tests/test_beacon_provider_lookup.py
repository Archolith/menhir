"""Finding a project's Menhir id without a file in the checkout.

Identity lives only in Menhir's graph, so a caller learns the id from Menhir: ``ingest_project``
reports it, and ``get_beacon_evidence`` finds the project by the repository origin its last scan
recorded. A lookup must never guess: several checkouts of one repository are listed, not chosen.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from menhir.services.beacon_evidence import (
    BeaconEvidenceError,
    find_projects_by_repository,
    normalize_repository,
)


@pytest.mark.parametrize(
    ("spelling", "same"),
    [
        ("https://github.com/Org/Shop.git", True),
        ("https://github.com/org/shop/", True),
        ("git@github.com:org/shop.git", True),
        ("ssh://git@GitHub.com/org/shop", True),
        ("https://github.com:8443/org/shop", False),
        ("https://github.com/org/shop-two", False),
        ("https://gitlab.com/org/shop", False),
    ],
)
def test_repository_spellings_match_as_beacon_compares_them(spelling: str, same: bool) -> None:
    assert (normalize_repository(spelling) == normalize_repository("https://github.com/org/shop")) is same


class _Reader:
    def __init__(self, rows: list[dict[str, str]]) -> None:
        self.rows = rows

    def list_indexed_repositories(self) -> list[dict[str, str]]:
        return self.rows


def _row(pid: str, name: str, repository: str) -> dict[str, str]:
    return {"project_id": pid, "name": name, "root_path": f"/src/{name}", "repository": repository}


def test_lookup_returns_every_checkout_of_the_repository() -> None:
    reader = _Reader(
        [
            _row("p2", "shop-copy", "git@github.com:org/shop.git"),
            _row("p1", "shop", "https://github.com/org/shop.git"),
            _row("p3", "other", "https://github.com/org/other.git"),
        ]
    )
    found = find_projects_by_repository(reader, "https://github.com/org/shop")
    assert [p["project_id"] for p in found] == ["p1", "p2"]
    assert set(found[0]) == {"project_id", "name", "root_path"}


def test_lookup_requires_a_repository() -> None:
    with pytest.raises(BeaconEvidenceError, match="required"):
        find_projects_by_repository(_Reader([]), "  ")


class _Backend:
    def __init__(self, projects: list[dict[str, str]]) -> None:
        self.projects = projects
        self.calls: list[tuple[str, str]] = []

    async def query_structure(self, project: str, query_type: str) -> Any:
        self.calls.append((project, query_type))
        if query_type == "beacon_projects_for_repository":
            return {"projects": self.projects}
        return {"evidence_version": "1.1", "binding": {"project_id": project}}


def _endpoint(monkeypatch: pytest.MonkeyPatch, backend: _Backend, **kwargs: str) -> str:
    from menhir.mcp.tools.ops.get_beacon_evidence import GetBeaconEvidenceTool

    tool = GetBeaconEvidenceTool()
    monkeypatch.setattr(tool, "get_backend", lambda: backend)
    return asyncio.run(tool.endpoint(**kwargs))


def test_one_matching_project_is_served_by_repository(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _Backend([{"project_id": "p1", "name": "shop", "root_path": "/src/shop"}])
    text = _endpoint(monkeypatch, backend, repository=" https://github.com/org/shop ")
    assert '"project_id": "p1"' in text
    assert backend.calls[-1] == ("p1", "beacon_evidence")


def test_several_checkouts_are_listed_never_chosen(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _Backend(
        [
            {"project_id": "p1", "name": "shop", "root_path": "/src/shop"},
            {"project_id": "p2", "name": "shop-copy", "root_path": "/src/shop-copy"},
        ]
    )
    with pytest.raises(ValueError, match="2 indexed projects") as refused:
        _endpoint(monkeypatch, backend, repository="https://github.com/org/shop")
    assert "project_id=p1" in str(refused.value) and "project_id=p2" in str(refused.value)
    assert all(kind != "beacon_evidence" for _, kind in backend.calls)


def test_an_unknown_repository_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="no indexed project recorded"):
        _endpoint(monkeypatch, _Backend([]), repository="https://github.com/org/none")


@pytest.mark.parametrize("kwargs", [{}, {"project_id": "p1", "repository": "https://x/y"}])
def test_exactly_one_selector_is_required(
    monkeypatch: pytest.MonkeyPatch, kwargs: dict[str, str]
) -> None:
    with pytest.raises(ValueError, match="exactly one"):
        _endpoint(monkeypatch, _Backend([]), **kwargs)


def test_ingest_result_names_the_project_id() -> None:
    from menhir.mcp.tools.ingest.ingest_project import _format_project_ingest_outcome
    from menhir.services.project_ingest import ProjectIngestOutcome

    scanned = _format_project_ingest_outcome(
        ProjectIngestOutcome(project_name="shop", background=True, project_id="p1")
    )
    skipped = _format_project_ingest_outcome(
        ProjectIngestOutcome(project_name="shop", skipped=True, project_id="p1")
    )
    assert scanned.startswith("Scanned shop (project_id=p1):")
    assert skipped.startswith("Skipped shop (project_id=p1):")
