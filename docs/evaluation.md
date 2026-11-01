# Evaluation posture

Menhir uses benchmarks to decide what earns authority in the default runtime. A result
is useful when its dataset variant, code revision, graph state, baseline, metric, and
run scope are recorded. Fixture output and an isolated score are not release claims.

## LongMemEval-derived evidence

Archolith maintains a LongMemEval-derived evaluation path for broad memory sanity
checks, regressions, and controlled retrieval comparisons. The available corpus includes
temporal-reasoning and knowledge-update questions, which are useful for exercising time
ordering, updates, and supersession behavior.

The 0.3.0 release gate ran on a fresh graph of the 500-question official Oracle corpus at the
frozen release candidate `44933985`. It recorded two different things, which must not be confused:
retrieval support against a same-graph vector-only Graphiti baseline, and answer quality from
recall-only answers scored by an LLM judge. Results are in
[tracker #123](https://github.com/Archolith/menhir/issues/123) (Gate E). It did not run the
full-haystack LME-S protocol, so it does not support a general claim that Menhir outperforms
vector retrieval.

LongMemEval also cannot establish Menhir-specific structural properties by itself. It
does not test repository coverage, blast-radius completeness, Git-to-graph time joins,
artifact governance, or whether a stale code anchor is handled correctly. Those require
separate acceptance tests and evidence.

## What the evidence changed

Before release, the read-side experiments were either adopted or removed. Oracle ranking,
the intent lens and the warden gate are always on. The belief gate, the diversity gate,
source-memory pools and the other entries in `feature_flags.RETIRED` were removed; each entry
records its date and reason (for example, the belief gate had a quality delta of 0 and added
12.6% to p95 latency). The scalar, scalar-history and counting lanes ship on, and scalar
authority applies while the scalar lane is on; see
[personal-memory readiness](personal-memory-readiness.md) for their known limitation.

This is the intended governance loop:

```text
recorded evidence -> bounded comparison -> activation decision -> default state
```

The repository's [default-off feature ledger](../.agent/default-off-features.md) records
activation decisions; `src/menhir/config/feature_flags.py` is the authority for current
defaults. The
[research-versus-shipped inventory](research/process/research-vs-shipped-inventory.md)
separates runtime behavior from prototypes and research. LongMemEval attribution is
recorded in the repository [`NOTICE`](../NOTICE).

## Publication rule

Menhir publishes a benchmark number only when a current benchmark authority names the
exact dataset variant, command, source commit, date, method, baseline, and evidence
artifact. This page quotes no headline number; the release-gate record is in tracker #123.
