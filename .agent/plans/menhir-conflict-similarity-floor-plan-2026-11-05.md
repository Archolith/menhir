---
artifact_schema: 1
artifact_uuid: 526e9676-769c-4aec-8d27-0e888d79250a
artifact_type: plan
artifact_status: APPROVED
---

# Conflict detection: similarity floor and belief-shaped fixture (PR #392)

> Moved from the maintainer workspace on 2026-11-07 with local paths and host details removed. Some cited reviews and plans were not moved; those names are historical.

Approved by the owner 2026-11-05. Implementation is open PR #392.
Date: 2026-11-05
PR: Archolith/menhir #392, branch `fix/conflict-detection-signal`.
Replaces Phases 1-2 of `menhir-conflict-detection-signal-2026-08-09.md`. RCA: `rca-conflict-detection-rrf-scale-mismatch-2026-08-09.md`.

## Decision

Route as today, and add one check in front of the expensive paths: when a pair's real name cosine is below a floor, drop it. Replace the production shadow flag with a committed labeled fixture. No feature flags.

Full cosine routing was rejected on evidence. On 16 hand-written pairs under the current thresholds it missed 3 of 6 contradictions and sent one contradiction (rate limit 100 vs 500, 0.952) to merge proposal. On 24 held-out pairs it flagged 0 of 10 contradictions. Cosine measures alike, and contradictions are alike.

## Safety property

**The floor can only turn a would-be conflict or merge proposal into `none`. It creates no new route to merge, the queue, or an edge.** Merge gates, judge and saga are untouched, so the 2026-07 auto-merge failure mode gets no new path. The cost is the opposite error: a real contradiction below the floor is dropped silently, so the floor is set from the contradictions, and every drop is recorded.

## Whole state machine

- Callers: all go through `classify_pair`, so the check lives there once (SSOT-03). `check_correlation` (enrichment, `enrichment_steps.py:1224`), `check_correlation_batch` (tests only), `_check_contradictions_batch` (consolidation Phase 3, `scan_for_conflicts`, `CandidateService.approve`).
- Enrichment builds `CorrelationService` with no flags today, so the flag-gated veto could never run there. Removing the flags closes that gap.
- Authorizing fact for a drop: both stored `name_embedding` vectors, read at classification time, give a defined cosine below the floor. Nothing else may authorize it.
- No new durable state or schema, so old and new processes can run side by side. Pairs simply route differently.

## Changes

### 1. Fixture first

`tests/fixtures/conflict_pairs.json`: the 40 hand-written pairs from the probes, plus a same-episode correction with no shared word and a same-episode correction with one. Each side is a belief:

```json
{"id": "residence-paris-london", "kind": "contradiction", "should_look": true, "same_episode": false,
 "name_cosine": 0.490,
 "a": {"name": "Paris residence", "subject": "user", "attribute": "residence", "value": "Paris", "confidence": "stated"},
 "b": {"name": "London home", "subject": "user", "attribute": "home", "value": "London", "confidence": "stated"}}
```

Store each pair's cosine and the embedder stamp, not 1536-dim vectors. The test stub returns two small vectors with exactly that cosine, so it runs through the real `pair_cosine`. `scripts/build_conflict_fixture.py` re-embeds names with `make_view_embedder` and rewrites cosines and the stamp, so an embedder change is visible. Runtime does not read the belief fields in this PR; they label the data for the follow-up.

### 2. Floor in `classify_pair`

After `_route`, only for `conflict` and `merge_proposed`:

- Read the pair's vectors with `fetch_pair_name_embeddings`, then `pair_cosine`.
- Defined and below `CORRELATION_SIMILARITY_FLOOR`: record `correlation_similarity_floor` (uuids, `rrf_score`, cosine, route avoided) and return `("none", {"reason": "below_similarity_floor"})`.
- `None` or any read error: continue exactly as today. No signal is not low similarity.

The `related` route is left alone, so RELATES_TO edges and their `similarity`/`weight` values do not change.

**Floor value: 0.40, not the 0.45 discussed earlier.** 0.45 would drop `Acme employer | Globex job` (0.409, a real contradiction). Rule: the highest round value that keeps every fixture contradiction, because an extra look costs one review and a miss is silent. At 0.40 all 16 contradictions and all 8 duplicates pass, 6 of 8 both-can-be-true pairs still go to review, and all 4 unrelated pairs drop. It is a constant with a constructor override, not a setting.

### 3. Veto always on

Remove `correlation_shared_episode_veto_enabled`; the veto runs on both conflict outcomes as written. Fonts scores 0.475, above the floor, so this is what removes it.

**Named risk:** `attributes_overlap` is word-based, so a same-episode correction with no shared word (`Paris residence` / `London home`) is dropped. The fixture pins this as a known limitation and each veto is recorded. If the owner rejects that, the veto stays out of this PR and fonts remains a queue item.

### 4. Remove the shadow

Delete `correlation_cosine_shadow_enabled`, `_record_cosine_shadow`, the `cosine_shadow` and `shared_episode_veto` constructor arguments, and their entries in `settings_model.py`, `feature_flags.py`, `.env.example` and `.agent/default-off-features.md`. Keep `pair_cosine`, `fetch_pair_name_embeddings`, the `rrf_score` name and the `list_conflicts` scope/origin output. Keep `scripts/conflict_group_signal_survey.py` unchanged.

### 5. Docs

Rewrite the CHANGELOG entry and PR title and description; update the 2026-08-09 plan's status to point here; fix the docstrings that still describe cosine bands (`correlation_service.py`, `lifecycle_consolidation.py:369`, `lifecycle_conflicts.py:61`).

## Tests

- New, fixture-driven: every contradiction and duplicate survives the floor, unrelated pairs drop, and each pair's outcome is pinned through `classify_pair`.
- Counterexamples: missing vector, dimension mismatch, zero vector and repository error all give today's route; a pair below the floor never reaches `_handle_merge_proposal`, `set_conflict` or the judge; `related` is unaffected; the enrichment path gets the floor and veto with no flags.
- Existing tests keep their assertions. Their repo stubs have no real vectors, so they take the fail-open path; if a stub needs an explicit `None` return, only the stub setup changes. Confirm against `test_correlation_service.py`, `test_lifecycle_service.py`, `test_regression_conflicts.py` and the unread files `test_candidate_service.py`, `test_namespace_isolation.py`, `test_conflict_namespace_scope.py`, `test_edge_cases.py`, `test_conflict_history.py`.
- `tests/test_conflict_detection_signal.py` is this PR's own file and is rewritten to match.
- Focused tests, CI's lint commands, one `code-reviewer` pass, then CI green at head. Push only then.

## Assumptions and limits

- **One embedder per graph.** Entity `name_embedding` has no model stamp. A same-dimension model change makes the cosine meaningless and could drop pairs wrongly. A dimension change fails open.
- **Name-only signal.** Names embed alone, and consolidation searches on content, so a pair can match on content with dissimilar names. That is the main miss risk, which is why the floor is low.
- **Fixture is 42 hand-written pairs, one model.** It pins behaviour; it is not a precision or recall measurement. Real graphs have more same-topic pairs, so queue reduction is unmeasured. The `correlation_similarity_floor` event gives the first real numbers after deploy.
- **`index.html` collisions still reach the queue** (cosine 1.000).
- **Recall shifts slightly:** `conflict_status` feeds scoring, so fewer new conflicts changes ranking. No bench run is planned here.
- **Rollback is a revert.** There is no flag, by owner decision.

## Out of scope

Merge thresholds and disarmed merge gates; repairing existing conflict groups; a name-collision veto; the companion suggestion plan.

## Follow-up: belief-shaped contradiction detection (separate issue, not filed)

Owner direction 2026-11-05: use the scalar shape without being bound to the scalar lane. Represent facts as concrete beliefs (subject, attribute, value) with a confidence label, so a contradiction is two beliefs on one key and the labels decide what happens.

- Why not scalars directly: the lane reads only `role=user` TurnEvidence, Views keep one current value per slot, and neither Views nor assertions can be conflict members.
- Open questions: what produces beliefs for project facts; the label scale and what evidence earns each label (candidates: stated vs inferred, independent restatement, judge votes, existing agreement and tier data); how a key survives attribute drift, since embeddings did not separate same from different attributes (`home_city | birth_city` 0.776) and extraction may need to reuse the subject's existing attributes; telling updates from contradictions, since most same-key changes are probably updates; which attributes hold one value, possibly learned from history.
- First step: a read-only count on a bench graph of ties, flip-backs and plain updates, then a small capped paid test of attribute reuse.

### Mechanism: reuse the fold/View system

Owner direction 2026-11-05: same fold and View system, consistent with decision 7.A (one event-sourced log, multiple agreeing projections).

- **Log:** `:TypedAssertion` stays the single evidence log, append-only and keyed by source. No parallel belief store.
- **Projection:** add a belief projection beside `scalar_state` and `scalar_history`: a new `ViewKind` plus a pure function over the same materializable rows, so live and replay match by construction, as `domain/scalar_history.py` does. Where `fold_assertions` picks a winner, this one keeps each slot's competing values with their labels.
- **Advisory first:** like `scalar_history`, it never enters the authority lane or suppresses evidence, so `scalar_state` behaviour does not change.
- **Confidence label:** derived in the projection, not stored on assertions. Inputs that exist today: `EVIDENCE_TIERS` (agent, trusted_tool, manual, user), the TurnEvidence `FOUNDS` user link, restatement across distinct sources (observations are per source, so this is countable), and `absolute_semantics=correction`. Perception currently forces every assertion to tier `agent`, so tier alone will not separate beliefs until the `FOUNDS` link is folded in.
- **New work this does not remove:** a producer for project facts (`typed_scalar_persistence.py` is the only production writer, and it reads user turns only); `VALUE_KINDS` has no general text kind beyond `status`; attribute reuse at extraction; a way to surface disagreements, since Views and assertions cannot be `set_conflict` members. A list read from the belief View avoids changing the entity conflict queue.

## Status update 2026-11-05

Approved by the owner 2026-11-05 (0.40 floor, fixture, no flags; shared-episode veto always on; belief follow-up filed as #511, label research).

One addition made during implementation: PROMOTED members are exempt from the floor (SSOT-08), and a member missing from the metadata read also keeps today's route. Operator-curated nodes are never silently dropped, matching the shared-episode veto.

The fixture has 42 pairs and adds `expected_action` and `known_limit` to the fields shown above.

## Review correction 2026-11-05

The code review disproved the Safety property as written. It held per pair but not per scan: `_check_contradictions_batch` stops after a conflict and moves on after `none`, so a drop reached later hits the scan never classified on main, including new merges and RELATES_TO edges.

Fixed by ending the node's scan at a drop, in `_check_contradictions_batch` and `check_correlation_batch`. For one scan, a drop now produces a subset of what main does. `check_correlation` has no stop and needs no change.

Corrected claim: within a scan, the floor and the veto add no new route. Across scans it is weaker: a dropped pair writes no conflict state, so the node stays scannable and merge-eligible where main would have flagged it and blocked it from later scans and merges. A later merge still passes the vetoes, unanimous judge and saga. A rescan records the drop again, so `correlation_similarity_floor` counts pair-scans, not pairs.

Also found: real two-lane RRF scores never land in [0.85, 0.95), so production reaches review only through the merge route. Tests and the fixture now run on both routes.

Open option for the owner: looking past a dropped hit would examine more pairs, but it opens the new routes above, so it was not done.
