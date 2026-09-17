---
artifact_schema: 1
artifact_type: plan
artifact_status: PROPOSED
---

# P3 extraction: threat model and mechanism

Parent plan: `.agent/plans/menhir-mcp-snapshot-ingest-2026-09-16.md` (P3)
Status: **PROPOSED — not approved, not implemented.**

## Why this document exists before any code

Every phase so far has been able to say "this code cannot reach that." P2A's gate is enforced by a
test that parses the module and fails if it imports `zipfile`. P3 deletes that guarantee on
purpose: it is the first phase that opens an archive a stranger uploaded.

Until now the worst an adversarial bundle could do was consume a quota. After P3 it is running a
parser over attacker-chosen bytes and writing attacker-chosen paths to a disk. The plan lists the
components — isolated extractor, manifest verification, managed-root containment, project lease,
bounded worker — but not what each is defending against, and a component built without its threat
named tends to defend the case its author imagined.

## What P3 may and may not do

**May:** open the archive, verify it against its manifest, materialise it under a managed root,
scan the result, report counts and a fingerprint, delete the materialised copy.

**May not:** adopt project identity, write anything to the graph, or leave a materialised root
behind. `SnapshotReceiveMode.extracts_archives` is already the gate and is already pinned by test;
`writes_graph` stays false through all of P3.

## Threats, and the mechanism for each

The frozen protocol already does more here than it appears to. `normalize_bundle_path` rejects
absolute paths, `..`, backslashes, drive letters, UNC prefixes, control characters, empty segments,
over-long segments, and trailing dot/space. **It rejects and never repairs**, which is the property
P3 depends on: a repaired path is a path that was attacker-chosen and then made to look safe.

| # | Threat | Mechanism |
| --- | --- | --- |
| 1 | **Path traversal (zip-slip).** An entry named `../../etc/authorized_keys`. | Every entry name goes through `normalize_bundle_path` BEFORE the entry is opened. Never `ZipFile.extractall` — it is the canonical zip-slip vector. After joining, re-resolve and assert the result is still inside the managed root; a check on the name is not a check on the resolved path. |
| 2 | **Symlink escape.** An entry stored as a symlink to `/` or to `../..`, followed on the next write or by the scanner. | The bundler emits regular files only and declares symlinks as omissions. The extractor refuses any entry whose external attributes are not a regular file, before creating anything. It never follows a link it did not create. |
| 3 | **Decompression bomb.** 4 GiB from a 400 KiB entry; nested archives. | Limits enforced DURING the write, not from the header: read through a counting wrapper and abort the moment the running total exceeds `max_total_bytes` or one entry exceeds `max_file_bytes`. A declared size in the header is an attacker-supplied number and must never be trusted to size an allocation. Nested archives are not opened — P3 extracts one level and treats an inner archive as an ordinary file. |
| 4 | **Entry-count exhaustion.** A million empty files. | Count as entries are enumerated and refuse past `max_file_count` before extracting any of them. |
| 5 | **Duplicate entries.** The same path twice, where the manifest describes one. ZIP permits it; readers disagree on which wins. | Refuse the bundle on the first repeated normalized path. Not "last wins" — a bundle whose meaning depends on the reader's tie-break is not a snapshot of anything. |
| 6 | **Archive/manifest disagreement.** An entry not in the manifest, or a manifest entry absent from the archive. | The manifest is the contract; the archive is the delivery. Both directions are a refusal, not a reconciliation. This is also the first point at which `tree_digest` can be verified at all — every digest before now covered chunks of the wire, not the content. |
| 7 | **Content substitution.** Entry bytes that do not match the manifest's per-file digest. | Hash each file as it is written and compare to its `FileRecord.sha256`. Hash the stream, do not re-read the file afterwards: re-reading opens a window in which the file could change. |
| 8 | **Case and unicode collision.** `README.md` and `readme.md`; NFC vs NFD spellings of one name. Distinct in the manifest, one file on macOS or Windows. | Refuse when two normalized paths collide under both case-folding and NFC normalization. Refuse rather than pick, for the reason in #5. |
| 9 | **Windows-hostile names.** `CON`, `aux.txt`, trailing dot or space. | Already refused by `normalize_bundle_path` and `_WINDOWS_RESERVED` at bundle time. The extractor re-applies the same function rather than trusting that it happened — the bundle may not have come from our bundler. |
| 10 | **Parser exposure.** A malformed archive that crashes or hangs `zipfile` itself. | The extraction runs in a bounded worker with a wall-clock cap and its own memory ceiling, and a crash is a FAILED job, not a server outage. This is the argument for a subprocess rather than a thread, and it should be settled explicitly — see open questions. |
| 11 | **Time-of-check/time-of-use.** Verified, then scanned, with a gap. | The managed root is written once and then treated as read-only; nothing outside the extractor writes into it, and the scan runs against the same materialisation that was verified. |
| 12 | **Leftover roots.** A crash mid-extraction leaves a materialised copy on disk. | The root is per-upload and named from the upload id, so it is attributable. A sweep reclaims roots with no live job, exactly as staging blobs are reclaimed today. A root is never adopted on restart: invariant 11 applies here too — a directory's existence is not state. |

## The lease is the part most likely to be got wrong

The parent plan says "project lease" without saying what it protects. P2B already produced one
lesson worth carrying: the quota check was a check-then-act, and it was fixed by making the
reservation durable BEFORE the decision. The same failure is available here in a worse form.

What a lease must satisfy, stated as properties rather than as a design:

- **It is bound to the snapshot, the owner, and a generation** — not to a project name alone. A
  lease that outlives its holder and is then interpreted as belonging to the next holder is how two
  extractions of different snapshots interleave into one root.
- **Liveness is durable evidence, not inference.** A PID is not proof: PIDs are reused, and a PID
  in another namespace is a different process entirely. "The process must be dead by now" is not a
  fact about a distributed system.
- **It expires, and expiry is re-checked during the work, not only at the start.** An extraction
  that outruns its lease must stop, because by then another worker may legitimately hold it.
- **A crashed holder must not block the phase forever.** This is the argument against a bare lock
  file, and it is the same reasoning that kept a lock out of the P2B quota fix.

I do not think the lease should be designed in this document. It should be designed against
written counterexamples — holder A attested dead then B claims and A resumes; a lease expiring
mid-extraction; two workers on one project with different snapshots — in the way the P2B
counterexamples were written first and found two real bugs.

## Open questions for the owner

1. **Subprocess or in-process?** A subprocess bounds memory and survives a parser crash, and costs
   startup time per extraction plus a way to report failure back. In-process is simpler and makes a
   `zipfile` hang the server's problem. My inclination is subprocess, and it is a real cost, so it
   should be chosen rather than assumed.
2. **Where does the managed root live?** Under `MENHIR_STATE_DIR` alongside staging is the obvious
   answer, and it means extraction and staging share a disk budget that was measured for staging
   alone. The 64 MiB compressed / 256 MiB expanded pilot quota was approved for bytes on the wire;
   a materialised copy is a second, larger, simultaneous cost.
3. **What does `shadow` report?** The plan says counts, fingerprint and partial status. The
   fingerprint's definition matters: if it is `tree_digest`, it is verifiable against the manifest
   and worth little as a scan result; if it is over scan OUTPUT, it is the thing that makes the
   comparison with a direct local scan meaningful.
4. **Does P3 need the bundle to have come from our bundler?** Nothing today proves it did. Every
   rule above is applied to the archive as received, which is the right posture — but it is worth
   being explicit that "the bundler would never emit that" is not a defence P3 may rely on.

## What I would build first

Not the extractor. The counterexample corpus: a directory of small, deliberately hostile archives —
traversal, symlink, bomb, duplicate, collision, manifest mismatch, malformed — with a test that
asserts each is refused with a specific stable code. That corpus is what makes the extractor
reviewable, and it is the artifact P2B's experience argues for most strongly: both bugs found this
phase were found by writing the adversarial case first.
