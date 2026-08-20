"""Safety tests for the combined recall-hygiene migration."""

from __future__ import annotations

import importlib.util
import json
from argparse import Namespace
from pathlib import Path
from typing import Any


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "migrate_flagged_bootstrap_scope.py"
SPEC = importlib.util.spec_from_file_location("migrate_flagged_bootstrap_scope", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _header(rows: list[dict[str, Any]], *, uri: str = "bolt://test") -> dict[str, Any]:
    return {
        "kind": MODULE.MANIFEST_KIND,
        "version": MODULE.MANIFEST_VERSION,
        "source_uri": uri,
        "candidate_count": len(rows),
        "expected_graph_fingerprint": MODULE.hashlib.sha256(
            "|".join(str(row.get("fingerprint") or "") for row in rows).encode("utf-8")
        ).hexdigest(),
    }


def _base_row(uuid: str, **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "uuid": uuid,
        "labels": ["Entity"],
        "name": uuid,
        "content": "semantic content",
        "source": "codex",
        "group_id": "default",
        "namespace": "default",
        "user_flagged": True,
        "bootstrap_scope": None,
        "structure_role": None,
        "anchor_projects": [],
        "anchor_paths": [],
    }
    row.update(overrides)
    return row


def _manifest_row(row: dict[str, Any], **targets: Any) -> dict[str, Any]:
    classified = MODULE._classify_candidate(row)
    manifest = MODULE._manifest_candidate(classified)
    manifest.update(targets)
    return manifest


def test_fingerprint_covers_role_scope_and_partition_state() -> None:
    row = _base_row("u1")

    assert MODULE.fingerprint(row) != MODULE.fingerprint({**row, "structure_role": "file"})
    assert MODULE.fingerprint(row) != MODULE.fingerprint({**row, "bootstrap_scope": "general"})
    assert MODULE.fingerprint(row) != MODULE.fingerprint({**row, "namespace": "archolith"})
    assert MODULE.fingerprint(row) != MODULE.fingerprint({**row, "group_id": "archolith"})


def test_candidate_classification_separates_structure_from_bootstrap_scope() -> None:
    legacy = MODULE._classify_candidate(
        _base_row(
            "s1",
            content="Directory: src/legacy",
            source="codex,project-scan",
            user_flagged=False,
        )
    )
    bootstrap = MODULE._classify_candidate(_base_row("b1"))

    assert legacy["candidate_kind"] == MODULE.LEGACY_STRUCTURE_CANDIDATE
    assert legacy["proposed_structure_role"] == "directory"
    assert bootstrap["candidate_kind"] == MODULE.BOOTSTRAP_CANDIDATE
    assert bootstrap["proposed_structure_role"] is None


def test_scoped_entity_is_its_own_kind_not_a_bootstrap_candidate() -> None:
    """A flagged entity that already has a scope is the cleanup population.

    BOOTSTRAP_CANDIDATE is scope IS NULL. Mixing the two would drag the large
    retention-only propagated population into a review that does not need it.
    """
    scoped = MODULE._classify_candidate(_base_row("e1", bootstrap_scope="general"))
    unscoped = MODULE._classify_candidate(_base_row("b1"))

    assert scoped["candidate_kind"] == MODULE.SCOPED_ENTITY_CLEANUP_CANDIDATE
    assert scoped["proposed_structure_role"] is None
    assert unscoped["candidate_kind"] == MODULE.BOOTSTRAP_CANDIDATE

    # An unflagged scoped entity is not a candidate at all.
    try:
        MODULE._classify_candidate(
            _base_row("e2", bootstrap_scope="general", user_flagged=False)
        )
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("unflagged scoped entity must not classify as a candidate")


def test_scoped_entity_cleanup_defaults_preserve_retention() -> None:
    manifest = MODULE._manifest_candidate(
        MODULE._classify_candidate(_base_row("e1", bootstrap_scope="general"))
    )

    assert manifest["target_user_flagged"] is True
    assert manifest["target_bootstrap_scope"] == MODULE.UNREVIEWED
    assert manifest["target_structure_role"] is None


def test_scoped_entity_cleanup_clears_pin_without_unflagging() -> None:
    """`none` nulls bootstrap_scope while user_flagged stays true.

    The parent flagged episode intended the retention; only the startup pin was
    inherited. Clearing the flag here would make the entity decay- and merge-eligible.
    """
    row = _manifest_row(
        _base_row("e1", bootstrap_scope="general"), target_bootstrap_scope="none"
    )

    assert MODULE._normalize_manifest_candidate(row) == []
    assert row["target_bootstrap_scope"] is None
    assert MODULE._target_state(row) == {
        "user_flagged": True,
        "bootstrap_scope": None,
        "structure_role": None,
    }


def test_scoped_entity_cleanup_can_keep_a_deliberate_pin() -> None:
    row = _manifest_row(
        _base_row("e1", bootstrap_scope="workspace:archolith"),
        target_bootstrap_scope=" Workspace: Archolith ",
    )

    assert MODULE._normalize_manifest_candidate(row) == []
    assert row["target_bootstrap_scope"] == "workspace:archolith"
    assert row["target_user_flagged"] is True


def test_scoped_entity_cleanup_rejects_unreviewed_and_unflagging() -> None:
    unreviewed = _manifest_row(_base_row("e1", bootstrap_scope="general"))
    assert any(
        "explicitly reviewed" in problem
        for problem in MODULE._normalize_manifest_candidate(unreviewed)
    )

    unflagging = _manifest_row(
        _base_row("e2", bootstrap_scope="general"),
        target_bootstrap_scope="none",
        target_user_flagged=False,
    )
    assert any(
        "target_user_flagged=true" in problem
        for problem in MODULE._normalize_manifest_candidate(unflagging)
    )


def test_manifest_validation_normalizes_reviewed_targets() -> None:
    structure = _manifest_row(
        _base_row(
            "s1",
            content="File: src/example.py",
            source="project-scan",
            user_flagged=True,
        ),
        target_structure_role=" FILE ",
    )
    bootstrap = _manifest_row(
        _base_row("b1"), target_bootstrap_scope=" Workspace: Archolith "
    )
    rows = [structure, bootstrap]

    problems = MODULE.validate_manifest(_header(rows), rows, uri="bolt://test", max_rows=2)

    assert problems == []
    assert structure["target_structure_role"] == "file"
    assert structure["target_bootstrap_scope"] is None
    assert bootstrap["target_bootstrap_scope"] == "workspace:archolith"


def test_manifest_requires_explicit_review_for_both_candidate_kinds() -> None:
    structure = _manifest_row(
        _base_row(
            "s1",
            content="Project: legacy",
            source="project-scan",
            user_flagged=False,
        )
    )
    bootstrap = _manifest_row(_base_row("b1"))
    rows = [structure, bootstrap]

    problems = MODULE.validate_manifest(_header(rows), rows, uri="bolt://test", max_rows=2)

    assert any("s1: target_structure_role must be explicitly reviewed" in item for item in problems)
    assert any("b1: target_bootstrap_scope must be explicitly reviewed" in item for item in problems)


def test_candidate_kind_filter_narrows_without_unbounding_the_scan() -> None:
    class _Repo:
        def __init__(self) -> None:
            self.limits: list[int] = []

        def execute(self, _query: str, params: dict[str, Any]) -> list[dict[str, Any]]:
            self.limits.append(int(params["limit"]))
            return [
                _base_row("e1", bootstrap_scope="general"),
                _base_row("b1"),
            ]

    repo = _Repo()
    scoped = MODULE._candidates(
        repo, limit=10, kinds=frozenset({MODULE.SCOPED_ENTITY_CLEANUP_CANDIDATE})
    )
    assert [row["uuid"] for row in scoped] == ["e1"]
    assert MODULE._candidates(repo, limit=10, kinds=None).__len__() == 2
    # The scan stays bounded by --limit even when the result is filtered down.
    assert repo.limits == [11, 11]


def test_narrowed_manifest_declares_its_coverage() -> None:
    args = Namespace(
        limit=10, out="unused", candidate_kind=[MODULE.SCOPED_ENTITY_CLEANUP_CANDIDATE]
    )
    assert MODULE.selected_kinds(args) == frozenset({MODULE.SCOPED_ENTITY_CLEANUP_CANDIDATE})
    assert MODULE.selected_kinds(Namespace(candidate_kind=None)) == frozenset(
        MODULE.ALL_CANDIDATE_KINDS
    )


def test_validation_rejects_manifest_outside_its_declared_coverage() -> None:
    """A narrowed manifest must not silently carry rows it never reviewed."""
    scoped = _manifest_row(
        _base_row("e1", bootstrap_scope="general"), target_bootstrap_scope="none"
    )
    stray = _manifest_row(_base_row("b1"), target_bootstrap_scope="none")
    rows = [scoped, stray]
    header = {
        **_header(rows),
        "candidate_kinds": [MODULE.SCOPED_ENTITY_CLEANUP_CANDIDATE],
    }

    problems = MODULE.validate_manifest(header, rows, uri="bolt://test", max_rows=10)

    assert any("outside its declared coverage" in problem for problem in problems)


def test_validation_rejects_mismatched_and_legacy_coverage() -> None:
    # validate_manifest normalizes target_bootstrap_scope in place ("none" -> None),
    # so each call needs its own rows.
    def _rows() -> list[dict[str, Any]]:
        return [
            _manifest_row(
                _base_row("e1", bootstrap_scope="general"), target_bootstrap_scope="none"
            )
        ]

    rows = _rows()
    header = {
        **_header(rows),
        "candidate_kinds": [MODULE.SCOPED_ENTITY_CLEANUP_CANDIDATE],
    }

    mismatch = MODULE.validate_manifest(
        header,
        rows,
        uri="bolt://test",
        max_rows=10,
        kinds=frozenset({MODULE.BOOTSTRAP_CANDIDATE}),
    )
    assert any("does not match this manifest's coverage" in p for p in mismatch)

    legacy_header = {k: v for k, v in header.items() if k != "candidate_kinds"}
    legacy = MODULE.validate_manifest(
        legacy_header,
        _rows(),
        uri="bolt://test",
        max_rows=10,
        kinds=frozenset({MODULE.SCOPED_ENTITY_CLEANUP_CANDIDATE}),
    )
    assert any("predates candidate_kinds coverage" in p for p in legacy)

    # Unnarrowed use of a pre-coverage manifest stays valid.
    assert MODULE.validate_manifest(
        legacy_header, _rows(), uri="bolt://test", max_rows=10
    ) == []


def test_cleaned_scoped_entity_leaves_its_kind_after_apply() -> None:
    """Nulling the pin moves the row into the scope-IS-NULL population by design.

    verify sweeps only the manifest's declared coverage, so a cleaned row does not
    read as an unreviewed leftover of the kind that was just migrated.
    """
    cleaned = MODULE._classify_candidate(_base_row("e1", bootstrap_scope=None))

    assert cleaned["candidate_kind"] == MODULE.BOOTSTRAP_CANDIDATE
    assert cleaned["candidate_kind"] != MODULE.SCOPED_ENTITY_CLEANUP_CANDIDATE


def test_manifest_rejects_partition_mismatch_duplicate_and_wrong_version() -> None:
    row = _manifest_row(_base_row("b1"), target_bootstrap_scope="general")
    duplicate = dict(row)
    header = _header([row, duplicate], uri="bolt://other")
    header["version"] = 1

    problems = MODULE.validate_manifest(
        header, [row, duplicate], uri="bolt://test", max_rows=2
    )

    assert any("version" in item for item in problems)
    assert any("source_uri" in item for item in problems)
    assert any("unique" in item for item in problems)


def test_default_bound_covers_planned_legacy_corpus() -> None:
    assert MODULE.DEFAULT_LIMIT >= 751


def test_candidate_query_covers_legacy_structure_cleanup_and_semantic_pins() -> None:
    class Repo:
        query = ""

        def execute(self, query, params):  # noqa: ANN001
            self.query = query
            assert params == {"limit": 11}
            return []

    repo = Repo()

    assert MODULE._candidates(repo, limit=10) == []
    assert "n.structure_role IS NULL" in repo.query
    assert "n.structure_role IS NOT NULL" in repo.query
    assert "coalesce(n.user_flagged, false)" in repo.query
    assert "coalesce(n.source, '') CONTAINS 'project-scan'" in repo.query
    assert "'directory:'" in repo.query
    assert "'file:'" in repo.query
    assert "'project:'" in repo.query


def test_evaluation_rejects_namespace_or_group_drift_after_target_is_applied() -> None:
    row = _manifest_row(_base_row("b1"), target_bootstrap_scope="general")
    assert MODULE.validate_manifest(_header([row]), [row], uri="bolt://test", max_rows=1) == []
    applied = {**_base_row("b1"), "bootstrap_scope": "general"}
    applied["namespace"] = "changed"

    pending, unchanged, missing, drift = MODULE._evaluate_manifest({"b1": applied}, [row])

    assert pending == []
    assert unchanged == []
    assert missing == []
    assert drift == ["b1: preserved fields changed since plan"]


class _StatefulRepo:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = {str(row["uuid"]): dict(row) for row in rows}
        self.write_calls = 0

    def _candidate_rows(self) -> list[dict[str, Any]]:
        result = []
        for row in self.rows.values():
            role = MODULE.infer_legacy_structure_role(row)
            if role is not None:
                if row.get("structure_role") is None or bool(row.get("user_flagged")) or row.get(
                    "bootstrap_scope"
                ) is not None:
                    result.append(dict(row))
            elif bool(row.get("user_flagged")) and row.get("bootstrap_scope") is None:
                result.append(dict(row))
        return sorted(result, key=lambda item: str(item["uuid"]))

    def execute(self, query, params):  # noqa: ANN001
        if "MATCH (n) WHERE n.uuid IN $uuids" in query:
            return [dict(self.rows[uuid]) for uuid in params["uuids"] if uuid in self.rows]
        if "UNWIND $rows AS row" in query:
            self.write_calls += 1
            for target in params["rows"]:
                self.rows[str(target["uuid"])].update(target)
            return [{"updated": len(params["rows"])}]
        rows = self._candidate_rows()
        return rows[: int(params["limit"])]


def test_combined_plan_apply_verify_preserves_partitions(tmp_path: Path) -> None:
    structure = _base_row(
        "s1",
        content="Directory: src/legacy",
        source="project-scan",
        user_flagged=True,
        bootstrap_scope="general",
        namespace="archolith",
        group_id="archolith",
    )
    bootstrap = _base_row(
        "b1", namespace="default", group_id="default", user_flagged=True
    )
    repo = _StatefulRepo([structure, bootstrap])
    manifest_path = tmp_path / "recall-hygiene.jsonl"
    backup_path = tmp_path / "backup.jsonl.gz"
    backup_path.write_bytes(b"logical-backup")

    assert MODULE.cmd_plan(
        repo,
        "bolt://test",
        Namespace(limit=10, out=str(manifest_path)),
    ) == 0
    payload = [json.loads(line) for line in manifest_path.read_text(encoding="utf-8").splitlines()]
    for row in payload[1:]:
        if row["candidate_kind"] == MODULE.LEGACY_STRUCTURE_CANDIDATE:
            row["target_structure_role"] = "directory"
        else:
            row["target_bootstrap_scope"] = "workspace:archolith"
    manifest_path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in payload),
        encoding="utf-8",
    )
    args = Namespace(
        manifest=str(manifest_path),
        backup=str(backup_path),
        max_rows=10,
        batch_size=10,
        yes=False,
    )

    assert MODULE.cmd_apply(repo, "bolt://test", args) == 0
    assert repo.write_calls == 0
    args.yes = True
    assert MODULE.cmd_apply(repo, "bolt://test", args) == 0
    assert repo.write_calls == 1
    assert repo.rows["s1"]["structure_role"] == "directory"
    assert repo.rows["s1"]["user_flagged"] is False
    assert repo.rows["s1"]["bootstrap_scope"] is None
    assert repo.rows["s1"]["namespace"] == "archolith"
    assert repo.rows["s1"]["group_id"] == "archolith"
    assert repo.rows["b1"]["bootstrap_scope"] == "workspace:archolith"
    assert repo.rows["b1"]["namespace"] == "default"
    assert repo.rows["b1"]["group_id"] == "default"
    assert MODULE.cmd_verify(
        repo,
        "bolt://test",
        Namespace(manifest=str(manifest_path), limit=10),
    ) == 0
