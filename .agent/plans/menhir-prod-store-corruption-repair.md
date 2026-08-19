# Plan: menhir prod Neo4j store corruption - diagnosis and validated repair

Status: DIAGNOSIS COMPLETE / REPAIR VALIDATED ON COPY / NOT YET APPLIED TO PROD
**Last verified:** 2026-08-18 — NOT VERIFIABLE OFFLINE — treat as LIVE. Status says the repair is validated on a copy but NOT YET APPLIED TO PROD, and nothing in this workspace can confirm prod Neo4j state (bolt://192.168.86.33:7687). The only mentions of `property record id:964518 / owner NODE:-1` are inside this plan. Someone must check prod directly before this is closed.

Date: 2026-07-25
Prod: bolt://192.168.86.33:7687 - Neo4j 5.26.26 Community
Evidence source: `C:\Users\thron\IdeaProjects\backups\prod-neo4j-data-20260725-CORRUPT.tar.gz` (raw data dir, 15:08 2026-07-25)
Forensic host: ubuntu-server (192.168.86.56), `~/neo4j-check`, fully userspace, localhost-only, auth disabled

## 1. Why this exists

A prior session reported `property record id:964518, owner NODE:-1` on prod and noted it would
break any full `:Entity` scan. That single error line was the entire evidence base. This plan
replaces it with a measured diagnosis produced by restoring the store on isolated hardware.

**Prod was not touched during diagnosis.** The only prod contact was one read-only
`CALL dbms.components()` to pin the version.

## 2. What is actually wrong - three independent fault classes

The original single-error framing was wrong. There are three distinct problems.

### F1 - Dangling property-key token (6 nodes)

Runtime symptom, reproduced exactly on the copy:

    MATCH (n:Entity) RETURN count(properties(n))  ->  Property key with id=2137 not found
    MATCH ()-[r]->() RETURN count(properties(r))  ->  107733   (relationships fully clean)

The store holds **207** property-key tokens. A property record pointing at key id **2137** is a
wild pointer, not an off-by-one.

**It is not one fault repeated - it is FIVE distinct corruption signatures**, revealed by the
tolerant exporter reading each node individually (the consistency checker gave only one opaque
`2 > 1`). This is the strongest evidence for root cause:

| node | error |
|---|---|
| 28127 | `Property key with id=2137 not found` |
| 45115 | `chain cycle, starting at property record id:964518 from owner NODE:-1` |
| 45117 | `Unable to read property value in record:789480, starting at 964520` |
| 45118 | `Invalid type or encoding of property block: 8095881760446873631 (SHORT_STRING)` |
| 45119 | `Property key with id=65549 not found` |
| 52805 | `Property key with id=290 not found` |

**Node 45115 is the origin of the original prod error** - `property record id:964518, owner
NODE:-1` appears verbatim, and it is a *chain cycle*, not a dangling pointer as first assumed.

Dangling key tokens, a cycle, an unreadable value, and an invalid block encoding are four
different ways a property chain can break, all within one id neighbourhood. A logical bug
produces one signature repeatedly; **physical damage produces exactly this scatter**. Check
dmesg / SMART on the host serving .33 before assuming this will not recur.

Affected nodes (58,921 total nodes -> **6 corrupt**, 0.01%):

| id | labels | degree | identity (via relationships) |
|---|---|---|---|
| 28127 | :Entity | 1 | DEFINES conftest.py |
| 45115 | :Entity | 22 | ANCHORED_TO archolith-filter, pyproject, dist/ |
| 45117 | :Entity | 4 | DEFINES main, _run_audit, _resolve_sources |
| 45118 | :Entity | 13 | ANCHORED_TO archolith-context, remediation session 2026-06-09 |
| 45119 | :Entity | 4 | DEFINES main, _run_audit, _resolve_sources (duplicate of 45117) |
| 52805 | :Entity | 1 | DEFINES conftest.py (duplicate of 28127) |

All six are `source='project-scan'` structural nodes (code files/symbols), **not user memories**.
Two of the three groups are exact duplicate pairs. `structure_queries.py` writes these with
`MERGE ... ON CREATE SET n.source='project-scan'`, driven by the `ingest_project` MCP tool, so
the content is idempotently re-derivable by re-running project ingest for `archolith`.

Damage is clustered in three tight id neighbourhoods (28127, 45115-45119, 52805) with healthy
nodes interleaved (45113 reads perfectly). This reads as a localized write fault, **not
progressive rot** - the earlier "corruption compounds, act fast" framing is retracted.

### F2 - Structurally corrupt index reporting ONLINE (most dangerous)

`mentions_source_idx` (id 85, RANGE, `()-[r:MENTIONS]-() ON (r.source)`) has B-tree damage:
keys out of order, tree-node range violations. **`SHOW INDEXES` reports it `ONLINE`.**

This is the most consequential finding. A corrupt index the server believes is healthy returns
*silently wrong query results* rather than erroring. F1 is loud; F2 is not.

### F3 - Residual record damage, including 2 UNWRITABLE nodes

7 inconsistent PROPERTY records + 4 inconsistent NODE records across **four** nodes -
**45121, 45131, 45133, 52802** - being dangling property references, an unused string block,
and a duplicate property key id. Same id neighbourhoods as F1.

This was initially recorded as cosmetic ("runtime-tolerated"). That was wrong, and the
correction is the most operationally important part of this plan. All four **read** fine
(25/25/25/28 properties), but the four split on **write**:

| id | name | path | write |
|---|---|---|---|
| 45121 | cli.py | archolith-mcp-audit/plugins/gemini/archolith_mcp_audit/cli.py | **FAILS** |
| 45131 | conftest.py | archolith-bench/tests/conftest.py | **FAILS** |
| 45133 | - | - | succeeds |
| 52802 | - | - | succeeds |

`MATCH (n) WHERE id(n)=45121 SET n._probe = 1` returns a row and then fails at commit with
`Unable to complete transaction.` **Any menhir write touching 45121 or 45131 fails**, so this
is a live functional defect, not a reporting artifact. It was invisible to every read-only
probe - only a write attempt exposes it.

Both are `source='project-scan'` structural nodes, both re-derivable, and both `DETACH DELETE`
cleanly. Treat them exactly like the F1 six: **8 nodes to delete in total.**

How this was nearly missed: `SET n = properties(n)` assigns identical values, Neo4j skips the
write, and the operation reports success while changing nothing. The failure only surfaced by
forcing a real mutation (`SET n._chainfix = timestamp()`).

## 3. Instrument note: neo4j-admin cannot see any of this unaided

`neo4j-admin database check` **crashes** on the untouched store, in both `IndexChecker` and
`NodeChecker`:

    java.lang.IllegalArgumentException: 2 > 1
      at PropertyRecord.ensureBlocksLoaded(PropertyRecord.java:292)
      at SafePropertyChainReader.read(SafePropertyChainReader.java:144)
    exit 70

Note the crash is inside `SafePropertyChainReader` - the class whose purpose is reading damaged
chains without failing. Its guard does not cover this record shape.

Consequence: **the checker yields no blast radius on the unrepaired store**, and there is no
pre-repair baseline. Blast radius had to be measured by running the copy and probing node id
ranges with Cypher.

`neo4j-admin database copy` - the canonical "rebuild the store" remedy - is **Enterprise-only**
and unavailable on Community 5.26.26. `dump`/`load` archive store files verbatim and carry the
corruption with them. Cypher-level repair is the only path.

## 4. Repair validated on the restored copy

| Stage | Errors | PROPERTY | NODE | INDEX |
|---|---|---|---|---|
| Original store | **checker crash** | - | - | - |
| After deleting the 6 F1 nodes | 543 | 7 | 4 | 532 |
| After rebuilding `mentions_source_idx` | 55 | 7 | 4 | 44 |
| After rebuilding 13 affected indexes | 11 | 7 | 4 | **0** |
| After deleting the 2 unwritable F3 nodes | **7** | 5 | 2 | **0** |

Floor reached: **7 errors** on nodes 45133 and 52802 - 3x "next property record does not have
this record as its previous record", 2x "string block is not in use", 2x duplicate property key.
Both nodes read (25/28 properties) and write successfully, so these are genuinely tolerable.
No Cypher-level repair removes them; only a logical export/reimport would.

Note the second delete produced **zero** orphaned index entries, unlike the first. That confirms
the 532 index errors at stage 2 were specifically caused by deleting nodes whose property keys
could not be read - Neo4j cleans indexes correctly when the keys are readable.

After step 1 the previously-failing scans complete: 55,007 entities, 58,915 nodes
(58,921 - 6, exactly as expected).

**Honest attribution:** most of the 532 index errors at stage 2 were *caused by the delete*.
Deleting a node whose property chain is unreadable leaves index entries orphaned, because Neo4j
cannot compute the keys to remove. They are fully cleared by the index rebuild. A subset
("index entry does not have the same values as the referred node") pre-dates the repair - that
exact class was reported against node 45113 in the first crash, before anything was deleted.

## 5. Prod procedure (NOT YET RUN - requires explicit approval)

No downtime required; all steps are online Cypher except the optional verification.

### Step 0 - fresh backup (use the TOLERANT exporter)

The existing tarball is a 15:08 point-in-time copy and prod has been serving since. Take a new
one before any write.

**`scripts/export_graph_backup.py` CANNOT back up this store.** It pages `properties(n)` 5000
rows at a time, so one unreadable record kills the batch. Worse, it does not fail cleanly: it
leaves a valid gzip with a `meta` header advertising 58,921 nodes that actually contains
**20,000 nodes and zero relationships**, with no in-file marker of incompleteness. That artifact
would pass for a backup.

Use `scripts/export_graph_backup_tolerant.py` (commit `11a21ea`) instead. Verified against this
exact corrupt store: 58,915 nodes + 107,733 relationships exported, exactly 6 skipped,
58,915 + 6 = 58,921 fully accounted, exit code 1 (partial), summary trailer written.

    python scripts/export_graph_backup_tolerant.py --out backups/prod-pre-repair.jsonl.gz

Its `skip_node` records also double as the F1 identification step below, since it reports each
unreadable node's id, labels, and specific error.

### Step 1 - re-identify corrupt nodes by PROBING, never by hardcoded id

**Do not paste `[28127,45115,...]` into prod.** Internal node ids are stable only within the
restored copy; prod ids should match (same store files) but "should" is not a basis for a
destructive delete. Prod has also been written to since 15:08, so the corrupt set may be larger.

Probe procedure - **a read probe alone is insufficient**. It finds F1 but silently misses the
unwritable F3 nodes, which read perfectly and only fail at commit. Both probes are required.

Read probe (finds F1):
1. `MATCH (n) WHERE id(n) >= $lo AND id(n) < $hi RETURN count(properties(n))` in chunks of 2000
   to find failing ranges.
2. Within failing ranges, probe each existing id individually; successful probes echo their id.
3. The set difference is the corrupt id list.

Write probe (finds the unwritable F3 nodes) - run against the neighbourhoods around every id
found above, since damage clusters:

    MATCH (n) WHERE id(n) = $id SET n._probe = 1     // commit failure => unwritable
    MATCH (n) WHERE id(n) = $id REMOVE n._probe      // cleanup on success

Do **not** use `SET n = properties(n)` as the probe. It assigns identical values, Neo4j skips
the write entirely, and it reports success on a node that cannot actually be written.
Afterwards confirm no probe property leaked: `MATCH (n) WHERE n._probe IS NOT NULL RETURN count(*)`.

### Step 2 - review before deleting

For each identified node dump `labels(n)`, degree, and neighbour names (properties are
unreadable by definition). Confirm each is `project-scan` structural before deleting.
**Node 45118 warrants a human look** - its anchors include
`archolith remediation session 2026-06-09`, and its properties cannot be read to confirm it
carried nothing beyond structure.

### Step 3 - delete, then rebuild indexes

    MATCH (n) WHERE id(n) IN $probed_ids DETACH DELETE n

Then drop/recreate the affected indexes using `SHOW INDEXES YIELD name, createStatement`.
On the copy this was 13 indexes plus `mentions_source_idx`; all were plain indexes with
`owningConstraint = NULL`. **Re-check `owningConstraint` on prod** - a constraint-backed index
must be rebuilt via its constraint, not dropped directly.

Rebuild `mentions_source_idx` regardless of whether prod reports it ONLINE (it will).

### Step 4 - regenerate the deleted structural nodes

Re-run `ingest_project` for `archolith`.

### Step 5 - verify

Re-run the range probe; it must complete with zero failing ranges. A full offline
`neo4j-admin database check` requires stopping prod - schedule separately if wanted.

## 6. Open questions

- ~~Root cause is unknown.~~ **RESOLVED - failing non-ECC RAM on the host.** See section 8.
- **The final 7 errors are not repairable via Cypher.** Both chain-rewrite approaches failed
  (identical-value assignment is skipped; forced mutation commits fine on these two nodes but
  does not rebuild the chain). Only a logical export/reimport would clear them. Both nodes read
  and write correctly, so accepting them is the recommended course.
- **How many unwritable nodes exist on prod is unknown.** Two were found here only because the
  consistency report happened to name their neighbourhood. There is no cheap global scan for
  "nodes that fail on write" short of attempting a write to every node. If menhir logs
  unexplained `Unable to complete transaction` errors, this is the likely cause.
- **No pre-repair baseline exists** (the checker crashed), so pre-existing vs repair-induced
  attribution for part of the index errors is inference, not measurement.

## 8. ROOT CAUSE CONFIRMED - failing non-ECC RAM (2026-07-25)

Prod ran on **192.168.86.33, the OpenMediaVault home media server**, which has a **known-bad
8 GB DDR4 non-ECC DIMM** already diagnosed on 2026-07-12 (`projects/ctharvey/home-media/.agent/
workflows/troubleshooting.md` -> "ROOT CAUSE - Failing System RAM"). It corrupts writes in flight;
it had been destroying the JFS journal and segfaulting unrelated binaries at single-high-bit
addresses (`0x800000000`, `0x2000000000`).

**Our data confirms it independently.** The store has 207 property-key tokens, so any id above
that is invalid. Each observed wild id is exactly one bit away from a valid one:

    2137  - 2048  (2^11) = 89     valid key id
    65549 - 65536 (2^16) = 13     valid key id
    290   - 256   (2^8)  = 34     valid key id

A single high bit set that should not be - the same signature as the segfault addresses, reached
via a completely different subsystem. Node 45118's `Invalid type or encoding of property block:
8095881760446873631` is the same fault landing in a block header rather than a key id.

This retroactively explains the five distinct corruption signatures in section 2: random bit
flips produce scatter, a logical bug repeats one signature.

**Consequence: the in-place repair in section 5 was never the right move.** Repairing means tens
of thousands of writes, all through the failing DIMM, on hardware with no ECC to detect it.

## 9. RESOLUTION - migrated off the faulty host (2026-07-25)

Rather than repair in place, the graph was moved:

| | before | after |
|---|---|---|
| host | 192.168.86.33 (media server, bad DIMM) | 192.168.86.56 (ubuntu-server) |
| process | `neo4j-memory` Docker container | `menhir-neo4j.service` (systemd, enabled+active) |
| store | checker crashes, full scans fail | full scans pass, 142 indexes ONLINE |
| access | - | ufw limited to 192.168.86.0/24, auth enforced |
| counts | 58,921 nodes | 58,913 nodes / 107,680 rels |

- Source: the **repaired** forensic snapshot (all 8 corrupt nodes removed, indexes rebuilt),
  i.e. the validated section 4 procedure applied on healthy hardware.
- menhir `.env` `NEO4J_URI` -> `bolt://192.168.86.56:7687` (backup `.env.bak-premigrate`).
- `neo4j-memory` on .33 **stopped with `--restart=no`**; `/srv/neo4j/data` preserved, not deleted.
- Verified end-to-end over the LAN: reads, writes, probe cleanup, and menhir's own
  `MemorySettings` path all resolve to the new instance.

### Known gaps

- The migrated copy is the **15:08 2026-07-25 snapshot**; writes to prod between then and cutover
  (~8.5 h) are not included. **DECISION 2026-07-25: gap ACCEPTED, no re-sync.** Options weighed
  were (A) re-migrate from the newer store and re-run the repair, (B) merge the delta by uuid,
  (C) accept. C was chosen; the window is believed to be low-traffic.
- `/srv/neo4j/data` on .33 remains the **only** record of that window. Do not delete it. If it is
  ever wanted, **copy it off AFTER the DIMM is replaced** - reading 1.3 GB through the failing
  RAM is itself a corruption risk, and the data is safe at rest while nothing is writing to it.
- The **7 benign record errors** (nodes 45133, 52802) came along with the store. Both read and
  write correctly; only a logical export/reimport would clear them, and no importer exists.
- **`ScalarStateView` / `TypedAssertion` labels do not exist in this store.** Confirmed expected -
  the LME scalar measurements ran against the separate `menhir-neo4j-recall-bench` instance, not
  prod. Do not read prod as evidence about that feature's behaviour.
- The DIMM is still bad and everything else on .33 still writes through it.

## 7. Teardown

Forensic environment is disposable: `rm -rf ~/neo4j-check` on ubuntu-server. No sudo was used,
no system packages installed, no services registered, nothing bound beyond 127.0.0.1.
