# Belief-Gate Git-Staleness Implementation Plan

> **ARCHIVED 2026-07-11 (ctharvey-approved).** All pieces landed: producer
> `derive_structural_staleness` (`domain/git_staleness.py:138`); git adapter
> (`infrastructure/git_log.py`) with cached provider (`services/change_log_provider.py`
> `CachedGitChangeLog`); ingest-time belief-commit capture (`episode_stamping.py:35-115`,
> `memory_graph_adapter.py:352`, `cypher.py:280`); best-effort recall-time wiring
> (`recall_service.py:128,1192`, inside the belief-gate block, degrades silently on any miss).
> Runs under `frontier_belief_gate` (default-OFF, tracked in `.agent/default-off-features.md`).
> Owner approved archiving as code-complete; default-off is a deferred activation decision.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Feed deterministic git/structure staleness into the belief gate so a memory anchored to code that CHANGED after the belief was formed is treated as superseded (`LATER_CONTRADICTED`) and gated by `CurrentnessWarden` — closing deferred item #1 of the belief-gate (`belief-layer.md`).

**Architecture:** `git_staleness.derive_structural_staleness(anchors, believed_at, changes, belief_context)` is a complete, tested PURE producer that emits `LATER_CONTRADICTED` + `FILE/SYMBOL/DEPENDENCY_CHANGED` `BeliefEvidence` — which `is_superseded_belief` -> `CurrentnessWarden` already consume. It needs three data sources none of which exist in the live path: (1) the candidate's code **anchors** (file paths), (2) the **belief's commit context** (the git world it was formed against), (3) a **change log** for those anchors. This plan supplies all three: a pure git adapter that builds `GitChange`s from `git log` (behind a swappable, cached provider so capture is decoupled from query), a graph-read extension that returns anchor paths, an ingest-time capture of the belief commit, and the recall-time wiring that calls the producer and merges its evidence into `belief_evidence`. Recall-time git is best-effort: any failure or unresolvable repo degrades silently to no staleness (never breaks recall).

**Tech Stack:** Python 3.12, stdlib `subprocess` for git, pytest (`pythonpath = src`, `asyncio_mode = auto`). Tests are pure/stub or run against a throwaway git repo created in a tmp dir — never the live Neo4j graph.

## Decisions already made (do not relitigate)

- **Feed source = recall-time cached git query** (`git log`), reconciled via the existing `reconcile_snapshot` machinery, cached per `(repo, HEAD)`. Not ingested into the graph.
- **Full grounded path is in scope**: the ingest write to capture belief commit context (#2) is allowed.
- **Provider abstraction**: the change log is read through a `ChangeLogProvider` so the in-process cache can be swapped for a persistent sidecar later without touching callers. The persistent sidecar itself is a documented Forward item, not built here.
- **Grounding**: committed beliefs use `ANCESTRY` mode (grounded); memories with no captured commit fall back to the ungrounded `DATE_HEURISTIC`. `WORKTREE` (dirty/stash) mode is a Forward item.

## Global Constraints

- **Default OFF + warden-gated.** All new behavior is reachable only when `enable_belief_gate` is True, and (per the belief-gate fix) only APPLIED when `enable_warden_gate` is also True. With the gate off, no git is invoked, no extra query runs, recall is byte-for-byte unchanged.
- **Best-effort, never breaks recall.** Any git failure, missing repo, timeout, or unresolvable anchor -> skip staleness for that candidate, log at debug/exception, continue. Mirror the existing `_attach_frontier_metadata` / temporal-fetch degraded pattern.
- **Reuse, do not reinvent.** Use `derive_structural_staleness`, `GitChange`, `RepoSnapshot`, `reconcile_snapshot`, `FileIdentityResolver`, `BeliefCommitContext`, and `belief_evidence` as-is. The staleness decision logic and evidence shaping are already tested in `tests/domain/`.
- **No live Neo4j in tests.** Graph reads/writes are stubbed; git is exercised against a tmp `git init` repo.
- **Git invocation is sandboxed and bounded**: always `git -C <repo>`, never the process CWD; pass a timeout; scope to the anchor paths; cap the commit window. No network git.
- Run tests from repo root: `python -m pytest <path> -q`. The single `graphiti_core` Pydantic warning is third-party/benign.

## File Structure

- `src/menhir/infrastructure/git_log.py` (NEW) — pure-ish git adapter: run `git log`/`status`, build `RepoSnapshot`/`GitChange`. The only code that shells out to git.
- `src/menhir/services/change_log_provider.py` (NEW) — `ChangeLogProvider` protocol + `CachedGitChangeLog` (in-process cache keyed by repo+HEAD+since+paths). The swap seam for a future sidecar.
- `src/menhir/infrastructure/memory_queries.py` + `memory_graph_adapter.py` — extend `fetch_candidate_provenance` to also return anchor `structure_path`/`structure_project`.
- `src/menhir/services/ingest_service.py` — capture `belief_commit`/`belief_branch` on memory ingest when a project repo is resolvable.
- `src/menhir/services/recall_service.py` — gated, best-effort staleness pass that resolves repo, queries the provider, calls `derive_structural_staleness`, and merges evidence into candidate metadata for `belief_evidence`.
- `src/menhir/domain/belief_evidence.py` — accept pre-computed staleness evidence and fold it into the assembled evidence.
- `docs/research/belief-temporal/belief-layer.md` — move item #1 from deferred to implemented; record the provider/sidecar seam, the repo-availability degradation, and the WORKTREE/sidecar Forward items.

---

### Task 1: Pure git adapter — build GitChange/RepoSnapshot from `git log`

**Files:**
- Create: `src/menhir/infrastructure/git_log.py`
- Test: `tests/infrastructure/test_git_log.py`

**Interfaces:**
- Consumes: `from menhir.domain.git_staleness import GitChange`; `from menhir.domain.repo_snapshot import RepoSnapshot`.
- Produces: `capture_changes(repo_path: str, *, since_commit: str | None, paths: list[str] | None = None, timeout_s: float = 5.0, runner: Callable[[list[str]], str] | None = None) -> list[GitChange]` and `current_head(repo_path, *, runner=None) -> tuple[str, str]` returning `(head_sha, branch)`.
- A `runner` seam (defaults to a real `subprocess` git call) lets tests inject canned `git` output with no real git AND lets the dummy-repo tests use the real runner.

- [ ] **Step 1: Write the failing tests** (use a real tmp git repo so the parsing is exercised end to end)

```python
# tests/infrastructure/test_git_log.py
import subprocess
from pathlib import Path

import pytest

from menhir.infrastructure.git_log import capture_changes, current_head


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=True).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "r"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    (r / "a.py").write_text("v1\n")
    _git(r, "add", "a.py"); _git(r, "commit", "-q", "-m", "c1")
    return r


def test_no_changes_since_head_is_empty(repo: Path) -> None:
    head, _ = current_head(str(repo))
    assert capture_changes(str(repo), since_commit=head, paths=["a.py"]) == []


def test_change_after_belief_commit_is_captured(repo: Path) -> None:
    head_at_belief, _ = current_head(str(repo))
    (repo / "a.py").write_text("v2\n")
    _git(repo, "commit", "-q", "-am", "c2")
    changes = capture_changes(str(repo), since_commit=head_at_belief, paths=["a.py"])
    assert [c.target for c in changes] == ["a.py"]
    c = changes[0]
    assert c.kind == "file" and c.commit and c.changed_at
    # ANCESTRY contract: the belief commit must be in descends_from so derive_structural_
    # staleness Mode 2 marks it grounding.
    assert head_at_belief in c.descends_from


def test_unrelated_path_change_is_not_captured(repo: Path) -> None:
    head_at_belief, _ = current_head(str(repo))
    (repo / "b.py").write_text("x\n")
    _git(repo, "add", "b.py"); _git(repo, "commit", "-q", "-m", "c2")
    assert capture_changes(str(repo), since_commit=head_at_belief, paths=["a.py"]) == []


def test_rename_sets_renamed_from(repo: Path) -> None:
    head_at_belief, _ = current_head(str(repo))
    _git(repo, "mv", "a.py", "c.py"); _git(repo, "commit", "-q", "-m", "rename")
    changes = capture_changes(str(repo), since_commit=head_at_belief, paths=["a.py", "c.py"])
    assert any(c.renamed_from == "a.py" and c.target == "c.py" for c in changes)


def test_bad_repo_path_raises_or_empty_via_runner() -> None:
    # injected runner that fails -> capture_changes surfaces a CalledProcessError-like;
    # the recall caller (Task 5) is what swallows it. Here assert the runner is used.
    def fake(args: list[str]) -> str:
        return ""  # no commits
    assert capture_changes("/nonexistent", since_commit="abc", paths=["a.py"], runner=fake) == []
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/infrastructure/test_git_log.py -q`
Expected: FAIL (`ModuleNotFoundError: menhir.infrastructure.git_log`).

- [ ] **Step 3: Implement the adapter**

Implement with a `git log --name-status -M --format` parse over `since_commit..HEAD` scoped to `paths`. Concrete spec:
- `current_head(repo_path, runner)`: `git -C repo rev-parse HEAD` and `git -C repo rev-parse --abbrev-ref HEAD` -> `(sha, branch)`.
- `capture_changes(...)`:
  - rev range = `f"{since_commit}..HEAD"` when `since_commit` else `"HEAD"`.
  - cmd: `["log", rev_range, "--name-status", "-M", "--date=iso-strict", "--format=%x01%H%x1f%cI%x1f%P", "--", *paths]` (the `%x01` record sep + `%x1f` field sep make parsing unambiguous; `%P` is parents, unused but cheap).
  - For each commit record, parse the `--name-status` lines: `M\tpath` -> file change; `R100\told\tnew` -> rename (`renamed_from=old`, `target=new`); `A`/`D`/`M` -> `target=path`. `kind="file"` for all (symbol/dependency kinds are a Forward item).
  - Build `GitChange(target=path, changed_at=commit_iso, kind="file", commit=sha, branch=None, descends_from=frozenset({since_commit}) if since_commit else frozenset(), renamed_from=old_or_None)`. Setting `descends_from={since_commit}` is correct by construction: every commit in `since_commit..HEAD` descends from `since_commit`, which is exactly what `derive_structural_staleness` Mode 2 checks.
  - Default `runner` runs `subprocess.run(["git", "-C", repo_path, *cmd], capture_output=True, text=True, timeout=timeout_s, check=True).stdout`. On `CalledProcessError`/`FileNotFoundError`/`TimeoutExpired`, re-raise (Task 5 swallows; the injected-runner test shows the runner seam).

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/infrastructure/test_git_log.py -q`
Expected: PASS (5 passed). Adjust rename test if the local git emits `R\d+` differently; assert on `renamed_from`, not the score.

- [ ] **Step 5: Commit**

```bash
git add src/menhir/infrastructure/git_log.py tests/infrastructure/test_git_log.py
git commit -m "feat: pure git adapter — build GitChange list from git log over a commit range"
```

---

### Task 2: Change-log provider + in-process cache (the swap seam)

**Files:**
- Create: `src/menhir/services/change_log_provider.py`
- Test: `tests/test_change_log_provider.py`

**Interfaces:**
- Consumes: `capture_changes`, `current_head` (Task 1).
- Produces: `class ChangeLogProvider(Protocol)` with `changes(repo_path, since_commit, paths) -> list[GitChange]`; `class CachedGitChangeLog` implementing it, caching by `(repo_path, head_sha, since_commit, frozenset(paths))`. Cache invalidates implicitly because the key includes the current HEAD (a new commit -> new key).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_change_log_provider.py
from menhir.domain.git_staleness import GitChange
from menhir.services.change_log_provider import CachedGitChangeLog


def test_caches_by_repo_head_since(monkeypatch) -> None:
    calls = {"head": 0, "changes": 0}
    def head(repo, runner=None): calls["head"] += 1; return ("HEADSHA", "main")
    def cap(repo, *, since_commit, paths, **kw):
        calls["changes"] += 1
        return [GitChange(target="a.py", changed_at="2026-01-01", commit="x")]
    p = CachedGitChangeLog(head_fn=head, capture_fn=cap)
    a = p.changes("/r", since_commit="C", paths=["a.py"])
    b = p.changes("/r", since_commit="C", paths=["a.py"])
    assert a == b
    assert calls["changes"] == 1   # second call served from cache


def test_new_head_busts_cache() -> None:
    seq = iter([("H1", "main"), ("H2", "main")])
    def head(repo, runner=None): return next(seq)
    def cap(repo, *, since_commit, paths, **kw):
        return [GitChange(target="a.py", changed_at="t", commit="x")]
    calls = {"n": 0}
    def counting_cap(repo, **kw): calls["n"] += 1; return cap(repo, **kw)
    p = CachedGitChangeLog(head_fn=head, capture_fn=counting_cap)
    p.changes("/r", since_commit="C", paths=["a.py"])
    p.changes("/r", since_commit="C", paths=["a.py"])
    assert calls["n"] == 2   # HEAD moved -> recomputed
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_change_log_provider.py -q`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement the provider**

```python
# src/menhir/services/change_log_provider.py
"""Change-log provider: the swap seam between recall and the git adapter.

CachedGitChangeLog runs the git adapter at most once per (repo, HEAD, since, paths) and
memoizes in-process. The key includes the current HEAD, so a new commit transparently
busts the entry. A persistent sidecar backend (survives restarts; pre-populatable when
repos are absent at recall) is a Forward item and can replace this class behind the same
Protocol without touching callers.
"""
from __future__ import annotations

from typing import Callable, Protocol

from menhir.domain.git_staleness import GitChange
from menhir.infrastructure.git_log import capture_changes, current_head


class ChangeLogProvider(Protocol):
    def changes(self, repo_path: str, *, since_commit: str | None,
                paths: list[str]) -> list[GitChange]: ...


class CachedGitChangeLog:
    def __init__(self, *, head_fn: Callable[..., tuple[str, str]] = current_head,
                 capture_fn: Callable[..., list[GitChange]] = capture_changes) -> None:
        self._head_fn = head_fn
        self._capture_fn = capture_fn
        self._cache: dict[tuple, list[GitChange]] = {}

    def changes(self, repo_path: str, *, since_commit: str | None,
                paths: list[str]) -> list[GitChange]:
        head, _branch = self._head_fn(repo_path)
        key = (repo_path, head, since_commit, frozenset(paths))
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        out = self._capture_fn(repo_path, since_commit=since_commit, paths=paths)
        self._cache[key] = out
        return out
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_change_log_provider.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add src/menhir/services/change_log_provider.py tests/test_change_log_provider.py
git commit -m "feat: cached change-log provider (swap seam for a future sidecar backend)"
```

---

### Task 3: Return anchor paths from candidate provenance (graph read)

**Files:**
- Modify: `src/menhir/infrastructure/memory_queries.py` (`fetch_candidate_provenance`)
- Modify: `tests/` provenance test (extend the existing fetch_candidate_provenance test)

**Interfaces:**
- Produces: each provenance row gains `anchor_paths` (list of `st.structure_path`) and `anchor_project` (single project when all anchors agree, mirroring the existing `anchor_projects` -> project pick) alongside the existing fields.

- [ ] **Step 1: Write the failing test** — extend the existing `fetch_candidate_provenance` test (stub neo4j rows) to assert the row carries `anchor_paths` (e.g. `["src/x.py"]`) for a node `ANCHORED_TO` a structural file node. Mirror the existing test's stub shape.

- [ ] **Step 2: Run to verify failure.** `python -m pytest tests/ -q -k provenance`

- [ ] **Step 3: Extend the Cypher** in `fetch_candidate_provenance` — add a pattern comprehension parallel to the existing `anchor_projects`:

```
[ (n)-[:ANCHORED_TO]->(st:Entity)
  WHERE st.structure_role IS NOT NULL | st.structure_path ] AS anchor_paths,
```

Leave the service-layer policy (mapping rows to metadata) for Task 5. Do not change `fetch_candidate_metadata`.

- [ ] **Step 4: Run to verify pass.** `python -m pytest tests/ -q -k provenance`

- [ ] **Step 5: Commit**

```bash
git add src/menhir/infrastructure/memory_queries.py tests/<provenance_test>.py
git commit -m "feat: fetch_candidate_provenance returns ANCHORED_TO structure paths"
```

---

### Task 4: Capture belief commit context at ingest (graph write)

**Files:**
- Modify: `src/menhir/services/ingest_service.py`
- Modify: `src/menhir/infrastructure/paths.py` (add `repo_root_for_project(project) -> Path | None` if not already derivable)
- Test: `tests/` ingest test (stub) + a `repo_root_for_project` unit test

**Interfaces:**
- Produces: on episode ingest, when the call carries a resolvable project, the memory node stores `belief_commit` (HEAD sha) and `belief_branch`. Sourced via `current_head(repo_root_for_project(project))`, best-effort (absent on failure -> grounded mode unavailable for that memory; recall falls back to DATE_HEURISTIC).

- [ ] **Step 1: Write the failing tests** — (a) `repo_root_for_project("menhir")` returns the project dir under `projects_dir()` when it is a git repo, else None; (b) ingest with a stub `current_head` returning `("SHA","main")` writes `belief_commit="SHA"`/`belief_branch="main"` into the persisted node payload (assert on the stub graph adapter's captured write).

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement** — add `repo_root_for_project` to `paths.py` (resolve `projects_dir()/<owner>/<project>` or the structure project `root_path`; return None when not a git dir). In `ingest_episode` (beside the existing `reference_time` capture), best-effort resolve the project's repo head via an injected `head_fn` (default `current_head`) and thread `belief_commit`/`belief_branch` into the node payload. Wrap in try/except -> on any failure leave both unset. Gate the git call so it only runs when a project is provided (no project -> skip).

- [ ] **Step 4: Run to verify pass.**

- [ ] **Step 5: Commit**

```bash
git add src/menhir/services/ingest_service.py src/menhir/infrastructure/paths.py tests/<ingest_test>.py tests/<paths_test>.py
git commit -m "feat: capture belief_commit/belief_branch on episode ingest (best-effort)"
```

---

### Task 5: Recall-time staleness pass — wire the producer into the belief gate

**Files:**
- Modify: `src/menhir/domain/belief_evidence.py` (fold pre-computed staleness evidence in)
- Modify: `src/menhir/services/recall_service.py` (gated, best-effort staleness pass + repo resolution + provider)
- Test: `tests/test_belief_evidence.py` (staleness fold) + `tests/test_recall_service.py` (end-to-end gated)

**Interfaces:**
- Consumes: `CachedGitChangeLog` (Task 2), `derive_structural_staleness` + `BeliefCommitContext` (domain), the anchor paths (Task 3) and belief commit (Task 4) now present in candidate metadata, `repo_root_for_project` (Task 4).
- Produces: candidate metadata gains `staleness_evidence: tuple[BeliefEvidence, ...]`; `assemble_belief_evidence` appends it; a git-stale candidate becomes `belief_superseded` via the `LATER_CONTRADICTED` evidence and is gated by `CurrentnessWarden`.

- [ ] **Step 1 (HARD PART FIRST): fold staleness evidence into belief_evidence + test**

In `belief_evidence.assemble_belief_evidence`, after the existing temporal + provenance evidence, append any pre-computed staleness evidence carried on the metadata:

```python
    for ev_obj in metadata.get("staleness_evidence", ()) or ():
        if isinstance(ev_obj, BeliefEvidence):
            ev.append(ev_obj)
```

And in `score_candidate_belief`, treat staleness as a belief signal: a candidate carrying `staleness_evidence` is belief-bearing even without a temporal marker, and `is_superseded_belief` (which reads `LATER_CONTRADICTED`) drives the `SUPERSESSION` type:

```python
    has_signal = (metadata.get("belief_superseded") or metadata.get("belief_has_temporal")
                  or bool(metadata.get("staleness_evidence")))
    if not has_signal:
        return None
```

Test (`tests/test_belief_evidence.py`): a metadata dict carrying a `LATER_CONTRADICTED` `BeliefEvidence` in `staleness_evidence` yields a non-None score whose bucket is not `SAFE_TO_ASSERT`, and `assemble_belief_evidence` includes that evidence.

- [ ] **Step 2: Run the belief_evidence tests.** `python -m pytest tests/test_belief_evidence.py -q` — Expected PASS.

- [ ] **Step 3: Recall-time staleness pass (gated, best-effort)**

In `recall_service`, add a module-level pure helper and a gated call in `_attach_frontier_metadata` (beside the existing belief-temporal-marker fetch). The provider is a `RecallService` field defaulting to `CachedGitChangeLog()`.

```python
def _staleness_evidence_for(
    meta: dict[str, object],
    *,
    provider: ChangeLogProvider,
    repo_resolver: Callable[[str], str | None],
) -> tuple[BeliefEvidence, ...]:
    """Best-effort git staleness for one candidate's metadata. Returns () on any miss."""
    anchors = [str(p) for p in (meta.get("anchor_paths") or ())]
    project = meta.get("anchor_project")
    if not anchors or not project:
        return ()
    repo = repo_resolver(str(project))
    if not repo:
        return ()
    commit = meta.get("belief_commit")
    ctx = BeliefCommitContext(commit=str(commit)) if commit else None
    changes = provider.changes(repo, since_commit=(str(commit) if commit else None), paths=anchors)
    verdict = derive_structural_staleness(
        anchors=anchors, believed_at=(str(meta.get("created_at")) if meta.get("created_at") else None),
        changes=changes, belief_context=ctx,
    )
    return verdict.evidence
```

In `_attach_frontier_metadata`, when `tuning.enable_belief_gate`, after merging belief temporal markers, for each candidate uuid run `_staleness_evidence_for` inside a try/except (per-candidate or one wrapper), and merge `staleness_evidence` into `metadata_by_uuid[uuid]`. Resolve repos with `repo_root_for_project` wrapped to return `str | None`. Any exception -> log.exception, leave staleness absent.

- [ ] **Step 4: End-to-end test** (`tests/test_recall_service.py`, stub graph adapter + injected fake provider)

Using `_two_cands`, give entity-2 `anchor_paths=["a.py"]`, `anchor_project="p"`, `belief_commit="C"`, and inject a `RecallService` whose change-log provider returns one `GitChange(target="a.py", ... , descends_from=frozenset({"C"}))` and whose repo resolver returns a dummy path. Assert: with `enable_belief_gate=True, enable_warden_gate=True`, entity-2 is gated (dropped or `warden_label` set); with both flags off, entity-2 is kept. Provider/resolver are injected (no real git, no live graph).

- [ ] **Step 5: Run recall + belief suites.** `python -m pytest tests/test_recall_service.py tests/test_belief_evidence.py tests/test_assertion_pipeline.py -q` — Expected PASS, default path unchanged.

- [ ] **Step 6: Commit**

```bash
git add src/menhir/domain/belief_evidence.py src/menhir/services/recall_service.py tests/test_belief_evidence.py tests/test_recall_service.py
git commit -m "feat: recall-time git staleness feeds the belief gate (gated, best-effort)"
```

---

### Task 6: Documentation — flip deferred item #1 to implemented

**Files:**
- Modify: `docs/research/belief-temporal/belief-layer.md`

- [ ] **Step 1: Update the belief-gate section** — move item #1 (Git/structure staleness) from "Forward / before activation" to the implemented description: the recall-time cached `git log` feed via `ChangeLogProvider`/`CachedGitChangeLog`, `git_log.capture_changes`, the anchor-path read, ingest commit capture, and the `_staleness_evidence_for` wiring. State that committed beliefs use grounded ANCESTRY; legacy (no `belief_commit`) fall back to ungrounded DATE_HEURISTIC.

- [ ] **Step 2: Record the new Forward items** — (a) persistent sidecar backend for `ChangeLogProvider` (survives restarts; pre-populatable when repos are absent at recall); (b) WORKTREE/dirty-belief mode (capture `belief_worktree_hash` at ingest + `git status` hashes at recall); (c) SYMBOL/DEPENDENCY change kinds (currently all `file`); (d) the repo-availability caveat: staleness only fires where menhir can resolve the repo on disk at the project's root — elsewhere it degrades silently to no signal. Keep bench validation (#4) as the activation gate.

- [ ] **Step 3: Commit**

```bash
git add docs/research/belief-temporal/belief-layer.md
git commit -m "docs: belief-gate git-staleness implemented; record sidecar/worktree/symbol forward items"
```

---

## Self-Review

- **Spec coverage:** Tasks 1-2 build + cache the feed (the swap seam answers the sidecar concern); Task 3 supplies anchors; Task 4 supplies the belief commit (grounded mode); Task 5 wires the producer in and gates stale beliefs; Task 6 documents. Item #1 is closed end to end for committed, repo-resolvable beliefs.
- **Placeholder scan:** the hard parts (git adapter parse contract, provider cache, staleness fold, recall helper) carry complete code; Tasks 3/4/6 are concrete specs against named symbols and existing patterns.
- **Type consistency:** `GitChange`/`RepoSnapshot`/`BeliefCommitContext`/`StalenessVerdict.evidence` match the domain dataclasses; `ChangeLogProvider.changes(repo, since_commit, paths)` is used identically in Task 2 and Task 5; `derive_structural_staleness(*, anchors, believed_at, changes, belief_context)` matches the real signature; `descends_from=frozenset({since_commit})` satisfies Mode 2's `commit in c.descends_from` check.
- **Default-path safety:** every new path is gated by `enable_belief_gate`; git only runs when the gate is on and a repo resolves; all failures degrade to no staleness. With the gate off, no git, no extra query, recall unchanged.
- **Constraint fit:** tests use a tmp git repo or injected runner/provider and stub graph adapters — never the live Neo4j graph. Git is always `git -C <repo>` with a timeout, scoped to anchor paths.
