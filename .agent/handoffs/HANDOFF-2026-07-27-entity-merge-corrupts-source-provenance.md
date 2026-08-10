# HANDOFF — entity merge corrupts `source` and inflates `source_confidence`

- **Status:** ROOT-CAUSED, NOT FIXED. No code changed. Investigation was a side-finding while
  answering a provenance question; scope was not this.
- **Found:** 2026-07-27, read-only on the LME multismoke graph (`:7704`, container
  `menhir-lme-multismoke`, neo4j/`lmedata123`).
- **Severity:** the trust ladder is decorative for merged entities. Any governance decision keying on
  `Entity.source` or `Entity.source_confidence` is reading a corrupted value, and merging is the
  common case for entities.
- **Task:** #14.
- **Related:** `.agent/artifacts-provenance-governance-status.md` (the governance ledger this
  undercuts), `.agent/adr/0001-conversation-turn-capture-surface.md`.

## The defect

`src/menhir/infrastructure/correlation_queries.py:511-523`, in the entity merge:

```cypher
survivor.source_confidence = CASE
    WHEN survivor.source_confidence IS NULL THEN 0.5
    WHEN toFloat(survivor.source_confidence) + 0.1 > 1.0 THEN 1.0
    ELSE toFloat(survivor.source_confidence) + 0.1
END,
survivor.source = CASE
    WHEN survivor.source IS NULL OR survivor.source = ''
    THEN coalesce(absorbed.source, 'merged')
    WHEN absorbed.source IS NULL OR absorbed.source = ''
         OR survivor.source = absorbed.source
    THEN survivor.source
    ELSE survivor.source + ',' + absorbed.source
END,
```

Two distinct problems in one statement.

**1. `source` becomes an append log, not a value.** Every merge between differing sources
comma-joins them. The result is no longer in the tier ladder's domain:
`source_confidence_for("user,remote-api")` (`src/menhir/domain/utils.py:61`) matches no branch and
falls through to the default. The field is typed as provenance and read as provenance everywhere, but
after one merge it is a history string that nothing parses.

**2. `source_confidence` is incremented by 0.1 per merge, capped at 1.0.** It is arithmetic on a
trust score, not a tier selection. Consequences:
- Confidence stops being a function of `source`. Two nodes with the identical source string carry
  different confidences because they were merged a different number of times.
- Merge count raises trust. Absorbing N low-trust duplicates walks an `agent` node up to `user` tier.
  **This is the security-relevant half**: trust is inflated by repetition, which is exactly what an
  adversarial or merely noisy writer produces.
- Float drift: `0.7 + 0.1` yields `0.7999999999999999`, so values are not even comparable to the
  ladder constants by equality.

## Evidence (measured, `:7704`)

```
MATCH (n:Entity) WHERE n.source CONTAINS ','
RETURN n.source, n.source_confidence, count(*)
```

| `source` | `source_confidence` | n |
|---|---|---|
| `user,remote-api` | 1.0 | 12 |
| `remote-api,user` | 0.6 | 2 |
| `remote-api,user,remote-api` | **0.7999999999999999** | 1 |
| `remote-api,user,remote-api` | **0.7** | 1 |
| `user,remote-api,user,remote-api` | 1.0 | 1 |

Rows 3 and 4 are the proof that confidence is not derived from source. Rows 1 and 2 show it is
write-order dependent.

This is a benchmark graph with modest merge pressure. **Production merges far more, so expect worse
there** -- that check has NOT been run and is the first thing a fix should measure.

## A second, separate trap found in the same pass

`Episodic.source` is **overloaded across the episode twins**. Menhir's pending node carries provenance
(`'user'`, `'remote-api'`); graphiti's resolved node carries the episode TYPE (`'message'`, n=21, all
with `has_lifecycle=0`). Reading `.source` off an `:Episodic` returns a different KIND of value
depending on which twin you land on. Not caused by this bug, but it will mislead anyone auditing
provenance and belongs in the same fix's test coverage.

Also confirmed: `Entity.user_id` is `remote-api` on every node -- the derived transport default,
because `.mcp.json` sends no `X-Yawn-User-Id` (`src/menhir/api/auth.py:186-190`). It names the
channel, never a person. Do not treat it as an identity.

## What a fix has to decide (not decided here)

1. **What should `source` mean on a merged node?** Candidates: keep the HIGHEST-tier contributing
   source; keep the survivor's and record contributors in `merge_audit` (which already exists and
   already carries lineage, `correlation_queries.py:530-533`); or introduce a `sources` LIST and
   leave `source` single-valued. The list option is the only one that loses no information, but it
   changes the property's type and every reader.
2. **Should confidence move on merge at all?** Defensible: recompute from the resulting source via
   `source_confidence_for`, so it stays a pure function of tier. The current +0.1 encodes
   "corroboration raises trust", which may even be intended -- but if so it needs to be a separate,
   named, bounded field, not smeared onto the tier score. **Do not preserve the current behavior by
   default; make it an explicit choice.**
3. **Backfill.** Existing corrupted nodes cannot be repaired from `source` alone, but `merge_audit`
   holds per-absorption snapshots and may be sufficient to recompute. Verify before assuming.

## Verification for whoever picks this up

- Reproduce: query above against `:7704` (container is stopped; `docker start menhir-lme-multismoke`).
- The merge path has existing coverage -- find it before editing:
  `grep -rn "merge" tests/ | grep -i correlation`.
- **NOT RUN:** production measurement, any fix, any test. Nothing in this handoff has been
  implemented.

## Explicitly out of scope

Do not fold this into the evidence-projection work
(`IdeaProjects/projects/archolith/.agent/plans/menhir-evidence-projection-episodes.md`). That plan
consumes provenance; this bug is in how provenance is written. They should be fixed and reviewed
separately.
