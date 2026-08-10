# Menhir Graphiti 0.29 dependency probe

> **Steps 1-5 ALL EXECUTED 2026-07-12** (commits `d018477`, `89e5cce`). `_patch_graphiti_dedupe_resolutions()`
> switched to the 0.29 `duplicate_candidate_id` shape (no dual-version compat shim -- single-user
> deployment, one supported version). Pin widened to `graphiti-core>=0.29.2,<0.30`, lock
> regenerated.
>
> **Step 5 (live canary) ran and caught three real bugs the offline suite's mocks never exercised:**
> (1) `graphiti_patches.py`'s local-model retry loop read `self.MAX_RETRIES`, a Graphiti 0.28.x
> `LLMClient` class attribute removed in 0.29 (replaced by an internal tenacity retry this patch
> doesn't call) -- fixed by owning the retry budget as a local constant. (2) Menhir's
> `PatchedExtractedEntity` didn't declare `episode_indices`, a field Graphiti 0.29 added for
> multi-episode batching and now reads unconditionally -- fixed by adding the field with the same
> default as upstream (Graphiti's own empty-list fallback makes this safe for Menhir's
> single-episode path). (3) `episode_lifecycle.py::fetch_episode_processing` had a
> pre-existing (unrelated to Graphiti) duplicate-column-alias bug from an incomplete SSOT-11
> cleanup, surfaced only because the live canary actually exercises background-enrichment finalize
> against real Neo4j, which the offline suite's mocks never do.
>
> After all three fixes: full `add_episode` -> background enrichment -> semantic recall round trip
> verified live against Neo4j + LLM. Full offline suite green: 2853 passed, 32 skipped, 0 failed.
> This validates the review's own thesis below -- the live canary was exactly the gap worth closing,
> not a formality.

Date: 2026-07-12  
Project: Menhir  
Current dependency: `graphiti-core>=0.28.1,<0.29` (`0.28.2` locked)  
Candidate: `graphiti-core==0.29.2`

## Verdict

Proceed with a narrow compatibility patch and a live canary. The migration is
smaller than the version boundary initially suggested: an offline full-suite run
with Graphiti 0.29.2 produced 2,831 passes, 32 skips, and three failures, all from
one version-sensitive Menhir patch test module.

## Observed compatibility break

Graphiti changed the node-dedup response model:

| Version | Duplicate field | No-duplicate representation |
|---|---|---|
| 0.28.2 | `duplicate_name: str` | Empty string |
| 0.29.2 | `duplicate_candidate_id: int` | `-1` |

Menhir's `_patch_graphiti_dedupe_resolutions()` currently tolerates incomplete LLM
responses by defaulting `duplicate_name` to an empty string. With 0.29.2 overlaid,
three tests in `tests/test_graphiti_dedupe_resolutions_patch.py` fail because the
new required `duplicate_candidate_id` field is absent.

The safe adaptation is version-aware normalization:

- retain the `duplicate_name=""` fallback when the installed model exposes that
  field;
- default a missing or malformed `duplicate_candidate_id` to `-1` when the newer
  field is exposed;
- continue dropping entries without an integer-coercible entity `id`.

Graphiti 0.29's downstream resolver explicitly treats negative candidate IDs as
no duplicate, so `-1` preserves Menhir's fail-safe behavior instead of choosing an
arbitrary existing entity.

## Verification evidence

Focused overlay run against Graphiti 0.29.2:

```text
183 passed, 3 failed
```

Full offline overlay run against Graphiti 0.29.2:

```text
2831 passed, 32 skipped, 3 failed
```

All failures were the same `duplicate_candidate_id` model-shape mismatch. No other
constructor, import, search configuration, provider, patch, ingestion wrapper, API,
or service regression appeared.

The overlay used Graphiti 0.29.2 ahead of the synchronized environment on
`sys.path`; the repository dependency and lockfile were not changed.

## Upstream changes relevant to Menhir

The upstream 0.29 release notes describe a major internal search restructure while
stating that the public search API is unchanged. They also add combined node/edge
extraction, multi-episode batching, saga summaries, fact-triple episodes, and
episode metadata.

- Combined extraction remains opt-in, so the version bump alone will not provide
  the headline reduction in ingestion LLM calls.
- Episode indices in the new bulk path are zero-based; Menhir does not currently
  consume those indices.
- The documented database schema migration applies to Kuzu. Menhir uses Neo4j, so
  no Graphiti-driven database migration was identified.
- Source: <https://github.com/getzep/graphiti/releases>

## Remaining risk and recommended execution

1. Make `_patch_graphiti_dedupe_resolutions()` support both Graphiti model shapes.
2. Update its regression tests to assert the appropriate no-duplicate field.
3. Widen the dependency to `graphiti-core>=0.29.2,<0.30` and regenerate the lock.
4. Run the complete offline suite and dependency audit.
5. Run one real `add_episode` and search in a disposable namespace. Offline tests
   do not prove the live Neo4j plus LLM extraction path.

The live canary is the only material verification gap after the compatibility patch.
