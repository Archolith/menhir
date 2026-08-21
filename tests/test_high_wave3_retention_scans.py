"""Counterexample tests for HIGH remediation wave 3 (CF-6, CF-142, CF-166, CF-167, CF-168,
CF-201, CF-202, CF-203).

Each test reproduces the scenario the register recorded, not the shape of the fix.
"""

from __future__ import annotations

import ast
import asyncio
import datetime as dt
import json
import pathlib
import re
import sqlite3
import subprocess
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "menhir"


# ---------------------------------------------------------------------------
# CF-6 -- the timestamp separator that silently widened every window
# ---------------------------------------------------------------------------


def test_cf6_a_row_past_the_window_is_excluded() -> None:
    """Rows are written with Python isoformat ('T', microseconds, offset) and compared against
    SQLite datetime('now') (space, 19 chars) AS TEXT, so the stored value sorted ABOVE a
    same-instant cutoff and a 25-hour-old row read as inside a 24-hour window."""
    from menhir.infrastructure.telemetry.helpers import _utc_now_iso

    conn = sqlite3.connect(":memory:")
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=25)).isoformat()
    fresh = _utc_now_iso()

    normalized = "substr(replace(?, 'T', ' '), 1, 19)"
    older = conn.execute(
        f"SELECT {normalized} < datetime('now','-24 hours')", (old,)
    ).fetchone()[0]
    newer = conn.execute(
        f"SELECT {normalized} < datetime('now','-24 hours')", (fresh,)
    ).fetchone()[0]

    assert bool(older) is True, "a 25h-old row must fall outside a 24h window"
    assert bool(newer) is False, "a row written now must fall inside it"

    # And the unfixed comparison is what the finding recorded.
    #
    # Pinned rather than wall-clock: the defect is that 'T' (0x54) sorts above ' ' (0x20) at
    # index 10, which only decides the comparison when the two date PREFIXES are equal. With a
    # live clock, `now - 25h` and `now - 24h` straddle midnight for one hour every day, the date
    # prefixes differ, the comparison resolves before reaching the separator, and this assertion
    # fails on a correct implementation. Fixed instants keep the control on the character the
    # finding is actually about.
    pinned_cutoff = "2026-08-19 23:00:00"  # the shape datetime('now','-24 hours') emits
    pinned_row = "2026-08-19T22:00:00.123456+00:00"  # an hour OLDER, so genuinely outside
    assert bool(
        conn.execute("SELECT ? < ?", (pinned_row, pinned_cutoff)).fetchone()[0]
    ) is False, "unnormalized compare must read an out-of-window row as inside it"
    assert bool(
        conn.execute(f"SELECT {normalized} < ?", (pinned_row, pinned_cutoff)).fetchone()[0]
    ) is True, "normalizing the row side must restore the correct ordering"


def test_cf6_no_unnormalized_window_comparison_remains() -> None:
    """The invariant, not the instance: every comparison against datetime('now') in the
    telemetry stores normalizes the column side first."""
    offenders: list[str] = []
    for path in sorted((_SRC / "infrastructure/telemetry").glob("*.py")):
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "datetime('now'" not in line:
                continue
            if re.search(r"(?:AND|WHERE|,)\s+(\w+)\s*(?:<|>=|>|<=)\s*datetime\('now'", line):
                offenders.append(f"{path.name}:{i}")
    assert offenders == []


# ---------------------------------------------------------------------------
# CF-167 -- MCP tool arguments persisted verbatim into the sidecar
# ---------------------------------------------------------------------------


def test_cf167_submitted_memory_text_is_not_persisted_verbatim() -> None:
    """`call_payload` returned the caller's kwargs verbatim and `_preview_of` json.dumps'd them
    into mcp_events.payload_preview with no redaction, so the first 500 characters of every
    memory a user submitted were written to the plaintext sidecar."""
    from menhir.infrastructure.telemetry.helpers import _preview_of

    secret = "My AWS root password is hunter2 and my therapist is Dr. Chen"
    rendered = _preview_of({"text": secret, "namespace": "work", "source": "claude-code"})

    assert secret not in rendered
    assert "hunter2" not in rendered
    # Structure survives -- the preview is still useful for debugging.
    assert '"namespace": "work"' in rendered
    assert '"source": "claude-code"' in rendered


def test_cf167_recall_queries_are_masked_too() -> None:
    from menhir.infrastructure.telemetry.helpers import _preview_of

    rendered = _preview_of({"query": "what did I say about my divorce", "limit": 5})
    assert "divorce" not in rendered
    assert '"limit": 5' in rendered


def test_cf167_unknown_argument_names_are_masked_by_default() -> None:
    """The policy is an allowlist precisely so a tool added later is private by default."""
    from menhir.infrastructure.telemetry.helpers import _preview_of

    rendered = _preview_of({"some_future_content_arg": "sensitive value"})
    assert "sensitive value" not in rendered


def test_cf167_size_still_measures_the_unredacted_payload() -> None:
    """The size is structural and is why the preview is useful; redaction must not shrink it."""
    from menhir.infrastructure.telemetry.helpers import _size_of

    small = _size_of({"text": "x"})
    large = _size_of({"text": "x" * 400})
    assert large > small + 300


def test_cf167_filesystem_paths_are_masked() -> None:
    from menhir.infrastructure.telemetry.helpers import _preview_of

    rendered = _preview_of({"repo_path": "C:/Users/thron/secret-project"})
    assert "secret-project" not in rendered


# ---------------------------------------------------------------------------
# CF-168 -- :TurnEvidence survived delete_namespace
# ---------------------------------------------------------------------------


def _namespace_delete_cypher() -> str:
    from menhir.infrastructure.memory_queries import MemoryQueryRepository

    captured: dict[str, Any] = {}

    class _Neo4j:
        def execute(self, query: str, params: dict[str, Any] | None = None):
            captured.setdefault("queries", []).append(query)
            return [{"deleted": 0, "repairs": 0}]

    repo = MemoryQueryRepository(_Neo4j())
    try:
        repo.delete_namespace_with_scalar_cascade("g", "ns", operation_id="op")
    except Exception:
        pass
    return "\n".join(captured.get("queries", []))


def test_cf168_turn_evidence_is_deleted_inside_the_namespace_cascade() -> None:
    """It holds raw user prompts plus cwd and transcript_path, and the label was omitted from
    the cascade entirely -- so two of the three deletion paths left it behind. Deleting it
    inside this query rather than after it is what makes the removal durable: the one path
    that did purge it ran as a separate unjournaled step AFTER the saga had committed."""
    query = _namespace_delete_cypher()
    assert "TurnEvidence" in query


def test_cf168_blast_radius_and_capture_name_the_same_set() -> None:
    """The pre-erasure capture's own docstring requires it to mirror what the delete destroys:
    if the predicates drift, an erasure deletes graph nodes whose sidecar content it never
    recorded a subject for. A dry_run that under-reports the blast radius is the same omission.

    Both `count_namespace` and the capture build the clause, so both must name the label.
    """
    source = (_SRC / "infrastructure/memory_graph_adapter.py").read_text(encoding="utf-8")
    clauses = [
        line
        for line in source.splitlines()
        if "ScalarConsolidationWatermark" in line and "n.namespace = $namespace" in line
    ]
    assert len(clauses) == 2, f"expected two label clauses, found {len(clauses)}"
    assert all("TurnEvidence" in line for line in clauses)


# ---------------------------------------------------------------------------
# CF-166 -- the retention control that was dead three ways over
# ---------------------------------------------------------------------------


def test_cf166_pruner_requires_the_retention_window() -> None:
    """The signature used to default to 14, DUPLICATING the setting instead of reading it, so a
    naive wiring would ignore MENHIR_REVISION_RETENTION_DAYS while appearing to honour it."""
    import inspect

    from menhir.infrastructure.telemetry.store import McpTelemetryStore

    sig = inspect.signature(McpTelemetryStore.prune_old_revisions)
    assert sig.parameters["retention_days"].default is inspect.Parameter.empty


def test_cf166_pruner_has_a_production_caller() -> None:
    """It was fully unit-tested and never invoked: the definition plus six test references, and
    nothing else in the corpus."""
    from menhir.services.maintenance_scheduler import MaintenanceScheduler

    assert hasattr(MaintenanceScheduler, "_make_prune_telemetry_revisions")

    scheduler = MaintenanceScheduler.__new__(MaintenanceScheduler)
    object.__setattr__(scheduler, "revision_retention_days", 14)
    coro = scheduler._make_prune_telemetry_revisions()
    coro.close()


def test_cf166_job_is_registered_and_disablable() -> None:
    import menhir.services.maintenance_scheduler as ms

    source = pathlib.Path(ms.__file__).read_text(encoding="utf-8")
    assert '"prune_telemetry_revisions"' in source
    assert "if self.revision_retention_days > 0:" in source
    # and the dispatch chain can actually reach it
    assert 'elif name == "prune_telemetry_revisions":' in source


def test_cf166_setting_reaches_the_scheduler() -> None:
    source = (_SRC / "core/runtime.py").read_text(encoding="utf-8")
    assert "revision_retention_days=getattr(settings" in source


# ---------------------------------------------------------------------------
# CF-201 / CF-203 -- scans that no index could back
# ---------------------------------------------------------------------------


def test_cf201_edge_stamping_is_typed_and_directed() -> None:
    """Relationship indexes are TYPE-scoped, so an untyped ()-[r]-() can use none of the five
    uuid indexes and degrades to an all-relationship scan -- and being undirected it produced
    two rows per relationship, which the count(DISTINCT r) was quietly correcting."""
    source = (_SRC / "infrastructure/episode_stamping.py").read_text(encoding="utf-8")

    assert '.match("()-[r]-()")' not in source
    assert "_EDGE_TYPE_PATTERN" in source
    assert "]->()" in source, "the edge pattern must be directed"

    from menhir.infrastructure.episode_stamping import _EDGE_TYPE_PATTERN
    from menhir.infrastructure.schema import EDGE_LABELS

    assert _EDGE_TYPE_PATTERN == "|".join(EDGE_LABELS)


def test_cf203_the_unindexed_predicates_now_have_indexes() -> None:
    from menhir.infrastructure.schema import get_phase1_bootstrap_queries

    joined = " ".join(get_phase1_bootstrap_queries())
    for prop in ("structure_role", "structure_path", "structure_project", "raw_capture_for"):
        assert prop in joined, prop
    assert "FOR (n:Episodic) ON (n.content)" in joined


# ---------------------------------------------------------------------------
# CF-202 -- `capture_changes` walked the entire repository history
# ---------------------------------------------------------------------------


def test_cf202_default_path_logs_one_commit_not_every_ancestor() -> None:
    """`git log HEAD` is a REVISION, not a commit: it selects every ancestor. MEASURED at 1,715
    lines vs 3 on a 64-commit repo, growing without bound as the repo ages."""
    from menhir.infrastructure.git_log import capture_changes

    captured: dict[str, Any] = {}

    def _runner(args: list[str]) -> str:
        captured["args"] = args
        return ""

    capture_changes(repo_path=".", since_commit=None, runner=_runner)
    args = captured["args"]

    assert "-1" in args
    # After the "--" separator git reads -1 as a PATHSPEC, not a flag.
    assert args.index("-1") < args.index("--")


def test_cf202_since_commit_path_is_unchanged() -> None:
    from menhir.infrastructure.git_log import capture_changes

    captured: dict[str, Any] = {}

    def _runner(args: list[str]) -> str:
        captured["args"] = args
        return ""

    capture_changes(repo_path=".", since_commit="abc123", runner=_runner)
    args = captured["args"]

    assert "abc123..HEAD" in args
    assert "-1" not in args, "an explicit range must not be truncated to one commit"


def test_cf202_default_path_actually_returns_one_commit(tmp_path) -> None:
    """Executed against a real repository, including the single-root-commit case that
    HEAD~1..HEAD would have failed on."""
    from menhir.infrastructure.git_log import capture_changes

    repo = tmp_path / "r"
    repo.mkdir()

    def _git(*args: str) -> None:
        subprocess.run(
            ["git", *args], cwd=repo, check=True, capture_output=True, text=True
        )

    _git("init", "-q")
    _git("config", "user.email", "t@example.com")
    _git("config", "user.name", "t")
    for i in range(3):
        (repo / f"f{i}.txt").write_text(str(i), encoding="utf-8")
        _git("add", f"f{i}.txt")
        _git("commit", "-q", "-m", f"c{i}")

    def _runner(args: list[str]) -> str:
        return subprocess.run(
            ["git", *args], cwd=repo, check=True, capture_output=True, text=True
        ).stdout

    result = capture_changes(repo_path=str(repo), since_commit=None, runner=_runner)
    rendered = json.dumps(result, default=str)
    assert rendered.count("f2.txt") >= 1
    assert "f0.txt" not in rendered, "the default path logged an ancestor commit"


# ---------------------------------------------------------------------------
# CF-142 -- the unanchored code_ref resolver
# ---------------------------------------------------------------------------


def test_cf142_predicate_anchors_a_basename_on_a_path_boundary() -> None:
    """`ENDS WITH 'utils.py'` also matched `my_utils.py` and `test_utils.py`, and the query ends
    in LIMIT 1, so the todo got an edge to an arbitrarily chosen WRONG file."""
    from menhir.domain.todo_location import code_ref_file_predicate

    predicate = code_ref_file_predicate("f", "$file_path")
    assert "f.structure_path = $file_path" in predicate
    assert "NOT $file_path CONTAINS '/'" in predicate
    assert "ENDS WITH ('/' + $file_path)" in predicate


def test_cf142_no_unanchored_ends_with_survives_in_the_resolver() -> None:
    """The invariant: one decision, five implementations, and it was the odd one out that wrote
    the edge a user-facing tool reads back."""
    source = (_SRC / "infrastructure/todo_repository.py").read_text(encoding="utf-8")
    assert "structure_path ENDS WITH $file_path" not in source


def test_cf142_create_todo_still_issues_the_same_calls() -> None:
    """The narrower fix was chosen deliberately: existing tests pin the number and order of the
    neo4j calls create_todo makes, so the predicate is corrected in place rather than the writer
    being deleted in favour of deriving from :TodoLocation."""
    tree = ast.parse((_SRC / "infrastructure/todo_repository.py").read_text(encoding="utf-8"))
    fn = next(
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "create_todo"
    )
    executes = [
        c for c in ast.walk(fn)
        if isinstance(c, ast.Call) and getattr(c.func, "attr", None) == "execute"
    ]
    assert len(executes) >= 2


def test_cf142_every_copy_routes_through_the_shared_predicate() -> None:
    inline = re.compile(
        r"structure_path\s*=\s*l\.path\s*\n\s*OR\s*\(NOT\s+l\.path\s+CONTAINS", re.MULTILINE
    )
    offenders = []
    for rel in (
        "infrastructure/todo_repository.py",
        "infrastructure/work_artifact_repository.py",
    ):
        if inline.search((_SRC / rel).read_text(encoding="utf-8")):
            offenders.append(rel)
    assert offenders == []
