# Merge-eligibility guardrail — structural & path-shaped nodes never merge

**Design authority:** @ctharvey | **Status:** IMPLEMENTED 2026-07-10 (48 correlation tests green; regex validated against live Neo4j) | **Related:** F4 merge audit (todo `86e4b309`), `ingest-identity-merge-gating.md`

> **Implementation note (07-10):** built by an Opus subagent, reviewed by Claude. The Cypher path-shape
> regex was validated against the LIVE Neo4j engine (not just Python `re`) — `src/components/article`,
> `.env`, `CHANGELOG.md`, `start-server.ps1` match; `Gemma 3 27B`, `Tier 1`, `UserService` do not;
> `A/B testing` matches (documented, fail-safe false positive). Forward-only; no `merged_from` touched.

**Directive (2026-07-10, @ctharvey):** ingested data structures must never be eligible for correlation
merge — guardrail merges. Forward fix (prevents recurrence); complements F4 (which measures the
historical damage). The ~2,910 historical absorptions are unrecoverable (no unmerge); this stops new ones.

## Evidence (live LME, read-only probe)

Of 2,910 total absorbed entities across 1,467 absorbers, two deterministic signals cover **51%**:

| Category | Signal | Absorbed | Note |
|---|---|---|---|
| Structural | `structure_role IS NOT NULL` (file/directory/symbol/dependency/endpoint/config/project/test/entrypoint/document) | 812 (28%) | own identity path (structural upsert); never a correlation-merge subject |
| Path-shaped (untagged) | name contains `/`, `\`, or a file-extension token | 678 (23%) | slipped in as SEMANTIC; **includes the single largest absorber** `src/components/article` (73) |
| (residual real semantic) | — | 1283 (44%) | LEAVE to the LLM judge (H1) — guardrail must not touch |

Generic/deictic tokens (87) and very-short names (50) are smaller, fuzzier categories — denylist
follow-up, not this pass.

## Design — a third deterministic veto

The merge path already runs deterministic vetoes before the LLM judge in
`CorrelationService.handle_merge_proposal` (`correlation_service.py:394-396`): `co_mention`,
`anchor_project`. Add a third, **`ineligible_node`**, that fires when *either* the survivor or the
absorbed node is merge-ineligible. `handle_merge_proposal` is the single choke point for both entry
paths (correlation service + consolidation `_check_contradictions_batch`), so one veto covers all.

**Ineligible = either party satisfies:**
1. `structure_role IS NOT NULL`, OR
2. **path-shaped name** — matches `[\\/]` or a trailing file-extension
   (`\.(py|md|txt|json|ya?ml|ps1|sh|java|ts|tsx|js|sql|env|toml|cfg|ini|gradle|html|css|png|jpg|svg)\b`,
   case-insensitive).

Veto routes to **conflict** (never merge) — fail-safe toward retention, consistent with the existing
vetoes. Because a veto's only cost is two same-entities staying separate (mild recall dilution) vs. a
false merge's irreversible loss, over-vetoing is the correct direction; path-shape false positives
(e.g. `A/B testing`) are acceptable and noted, not engineered around.

## Parts

| Item | What | File | 
|------|------|------|
| P1 | `check_ineligible_node_veto(survivor_uuid, absorbed_uuid) -> bool` | `correlation_queries.py` |
| P2 | Wire the veto into `handle_merge_proposal` (both merge-proposal branches) | `correlation_service.py` |
| P3 | Tests | `tests/` |

### P1 — repo veto query (`correlation_queries.py`, next to `check_anchor_project_veto:299`)

```python
def check_ineligible_node_veto(self, survivor_uuid: str, absorbed_uuid: str) -> bool:
    """True if EITHER node is merge-ineligible: structural (structure_role set) or path-shaped name."""
    rows = self.neo4j.execute(
        """
        MATCH (n:Entity) WHERE n.uuid IN [$a, $b]
        WITH n, (n.structure_role IS NOT NULL) AS structural,
             (n.name =~ '(?i).*([\\\\/]|\\.(py|md|txt|json|ya?ml|ps1|sh|java|ts|tsx|js|sql|env|toml|cfg|ini|gradle|html|css|png|jpg|svg))(\\b|$).*') AS pathshaped
        RETURN count(CASE WHEN structural OR pathshaped THEN 1 END) > 0 AS ineligible
        """,
        params={"a": survivor_uuid, "b": absorbed_uuid},
    )
    return bool(rows[0].get("ineligible", False)) if rows else False
```
(Final regex tuned during impl against the live names; the Python-side extension list above is the
contract. Keep the check DB-side to avoid a second fetch.)

### P2 — wire into `handle_merge_proposal` (`correlation_service.py:394`)

Add as the **first** veto (cheapest, deterministic, no name-content ambiguity), before `co_mention`:

```python
if self._repo.check_ineligible_node_veto(survivor_uuid, absorbed_uuid):
    logger.info("Merge proposal %s <-> %s VETOED: ineligible node (structural/path-shaped)",
                survivor_uuid, absorbed_uuid)
    record_mcp_event(kind="background", operation="identity_decision",
        payload={"similarity": similarity, "action": "merge_proposed",
                 "survivor_uuid": survivor_uuid, "absorbed_uuid": absorbed_uuid},
        result={"final_action": "conflict", "vetoes_fired": ["ineligible_node"],
                "judge_available": False}, success=True)
    return "conflict"
```
Mirror the exact shape of the co_mention/anchor_project blocks (`:398-420`). Both proposal branches
(`:193-219`, `:329-331`) go through `handle_merge_proposal`, so no other edit is needed.

### P3 — tests

- structural survivor → veto (conflict, `vetoes_fired == ["ineligible_node"]`, no judge call).
- structural absorbed (reverse) → veto.
- path-shaped name (`src/components/article`, `.env`, `CHANGELOG.md`) on either party → veto.
- neither structural nor path-shaped → veto does NOT fire; judge path runs as today (regression:
  existing judge/veto tests stay green).
- Follow the F2/F5 stub pattern; add `check_ineligible_node_veto` support to the correlation repo stub.

## Verification gates

1. Veto fires when either party is structural OR path-shaped; routes to conflict; audit records
   `vetoes_fired: ["ineligible_node"]`.
2. Real semantic pairs (no signal) are unaffected — judge still decides.
3. Existing correlation/merge/judge suites green.
4. No change to `merged_from` receipts (forward-only; historical merges untouched — F4 owns those).

## Notes / boundaries

- **Forward-only.** Does not unmerge the 812+678 historical absorptions (no unmerge exists). F4
  documents them.
- **Fail-safe direction.** A false veto costs recall dilution (repairable); it never causes loss.
- **Optional follow-ups (not this pass):** generic/deictic denylist (~87) and short-name (~50)
  categories; excluding ineligible nodes from correlation *candidate search* (saves judge calls).
- **Frontier:** single checkout now (menhir-frontier folded into main); land once.

## Cross-reference

- Merge path: `correlation_service.py:385-440` (`handle_merge_proposal` vetoes) ·
  `correlation_queries.py:277,299` (existing veto queries)
- Structural marker: `structure_role` (`structure_queries.py`, values file/directory/symbol/…);
  precedent — `consolidation_queries.count_persistent_edges` already excludes `structure_role IS NOT NULL`
- Family: `ingest-identity-merge-gating.md` (Part 1 vetoes) · F4 audit (todo `86e4b309`)
