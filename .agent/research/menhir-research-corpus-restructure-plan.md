# Research-Corpus Restructure Implementation Plan

> **Status: COMPLETE (2026-06-29).** All 26 steps below shipped across commits `8729389` through
> `dab8bb9`. Checkboxes were reconciled to the landed history on 2026-08-10; command paths, branch
> names, and verification output in the body remain the historical execution record.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (inline) or superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Re-cluster `docs/research/` into themed subdirectories, index `docs/roadmap/`, triage `.agent/plans/`, and correct stale status headers — losing zero information and zero links.

**Architecture:** All file relocation via `git mv` (history preserved). Cross-links rewritten to new paths and verified by a link-resolution check after every move/edit. The before→after manifest lives in commit bodies, not a tracked file.

**Tech Stack:** git, markdown, a throwaway Python link-checker (run via the `python` on PATH).

## Global Constraints

- Retain ALL information: no deletions, no doc-body content edits except the status-header lines in Task 4.
- Every move is `git mv` (verify renames with `git diff --name-status -M`).
- Stage files by explicit path; never `git add -A`.
- Branch: `claude/menhir-chain-handoff-doc-7iuat2` (frontier worktree). `menhir` main is untouched.
- Conventional commits, `docs:` prefix. Co-Author trailer: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- Working dir for all commands: `C:/Users/you/IdeaProjects/projects/archolith/menhir-frontier`.

## The link-checker (verification tool, used by several tasks)

Throwaway script written to `/tmp/linkcheck.py` (NOT committed). Walks every `.md` under `docs/` and `.agent/`, extracts markdown link targets `](...md...)` and backtick-quoted `path/...md` references, resolves each relative to its containing file, and prints any that do not resolve to an existing file. Exit nonzero if any dangling.

```python
import re, sys, pathlib
root = pathlib.Path("C:/Users/you/IdeaProjects/projects/archolith/menhir-frontier")
scan_dirs = [root/"docs", root/".agent"]
md_link = re.compile(r"\]\(([^)]+\.md)(#[^)]*)?\)")
bad = []
for base in scan_dirs:
    for f in base.rglob("*.md"):
        text = f.read_text(encoding="utf-8", errors="replace")
        for m in md_link.finditer(text):
            target = m.group(1).strip()
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            resolved = (f.parent / target).resolve()
            if not resolved.exists():
                bad.append(f"{f.relative_to(root)} -> {target}")
if bad:
    print("DANGLING LINKS:")
    print("\n".join(sorted(set(bad))))
    sys.exit(1)
print(f"OK: all markdown links resolve ({sum(1 for b in scan_dirs for _ in b.rglob('*.md'))} files scanned)")
```

> Note: this checks relative markdown links `](...)`. Backtick-quoted bare paths (e.g. `` `facet-retrieval.md` ``) are prose references, not links — they are handled by Task 2's grep-and-rewrite, not by this resolver.

---

### Task 1: Create cluster subdirs and relocate research docs

**Files:**
- Create dirs: `docs/research/{direction,process,positioning,retrieval,schemas,belief-temporal,vision,archive}/`
- Move (git mv) 26 docs into clusters per the spec §3 (README.md stays at `docs/research/README.md`).

**Interfaces:**
- Produces: the new paths every later task and link rewrite depends on. New paths:
  - `direction/`: semantic-operating-system.md, oracle-architecture.md, llm-reviewer-seams.md
  - `process/`: research-process.md, archolith-bench-operational-model.md, research-vs-shipped-inventory.md
  - `positioning/`: positioning.md
  - `retrieval/`: retrieval-tuning-stack.md, facet-retrieval.md, facet-extraction-plan.md, oracle-amplified-retrieval.md, oracle-runtime-interfaces.md, oracle-execution-and-performance.md, retrieval-control-rails.md, intent-warden.md
  - `schemas/`: layer4-knowledge-artifacts.md, cold-start-brief.md
  - `belief-temporal/`: belief-layer.md, connected-data-substrates.md, tracehead-braidtrace.md
  - `vision/`: cognitive-replay-and-phasing.md
  - `archive/`: probabilistic-belief-layer.md, probabilistic-circuit-breakers.md, agent-experience-substrate.md, cognitive-artifacts-and-software-cognition.md, cognitive-infrastructure-platform.md

- [x] **Step 1: Create the subdirectories**

```bash
cd /c/Users/you/IdeaProjects/projects/archolith/menhir-frontier/docs/research
mkdir -p direction process positioning retrieval schemas belief-temporal vision archive
```

- [x] **Step 2: git mv each doc into its cluster**

```bash
cd /c/Users/you/IdeaProjects/projects/archolith/menhir-frontier/docs/research
git mv semantic-operating-system.md oracle-architecture.md llm-reviewer-seams.md direction/
git mv research-process.md archolith-bench-operational-model.md research-vs-shipped-inventory.md process/
git mv positioning.md positioning/
git mv retrieval-tuning-stack.md facet-retrieval.md facet-extraction-plan.md oracle-amplified-retrieval.md oracle-runtime-interfaces.md oracle-execution-and-performance.md retrieval-control-rails.md intent-warden.md retrieval/
git mv layer4-knowledge-artifacts.md cold-start-brief.md schemas/
git mv belief-layer.md connected-data-substrates.md tracehead-braidtrace.md belief-temporal/
git mv cognitive-replay-and-phasing.md vision/
git mv probabilistic-belief-layer.md probabilistic-circuit-breakers.md agent-experience-substrate.md cognitive-artifacts-and-software-cognition.md cognitive-infrastructure-platform.md archive/
```

- [x] **Step 3: Verify all 26 are renames, only README.md remains at top level**

```bash
cd /c/Users/you/IdeaProjects/projects/archolith/menhir-frontier
git diff --cached --name-status -M | grep -c '^R'   # expect 26
ls docs/research/*.md                                # expect only README.md
```
Expected: count `26`; `ls` shows only `docs/research/README.md`.

- [x] **Step 4: Write the 8 subdir READMEs**

One per cluster, 5-10 lines: cluster purpose + doc list with one-line summary and current status. (Content authored per spec §3; `archive/README.md` states these are superseded pointers, replacements named, never deleted.) Stage them explicitly.

- [x] **Step 5: Commit (manifest in body)**

```bash
cd /c/Users/you/IdeaProjects/projects/archolith/menhir-frontier
git add docs/research/
git commit  # body lists every old path -> new path (the manifest)
```
Commit message: `docs: re-cluster research corpus into themed subdirs (git mv only)` + manifest in body + Co-Author trailer.

---

### Task 2: Rewrite cross-links to new research paths

**Files:**
- Modify: every `.md` that references a moved research doc. Known referrers: `.agent/plans/chain-handoff.md` (50), `.agent/plans/menhir-research-execution-ladder.md` (10), `.agent/plans/{deferred-verification,menhir-intent-oracle-plan,r1-hybrid-candidate-generation,r2-facet-candidate-generation}.md`, `.agent/{memory-design,memory-futures,maintenance,...}.md`, moved research docs that link to siblings, `docs/roadmap/*.md`.

**Interfaces:**
- Consumes: new paths from Task 1.

- [x] **Step 1: Enumerate every referrer + old reference**

```bash
cd /c/Users/you/IdeaProjects/projects/archolith/menhir-frontier
grep -rIn -E '(docs/research/[a-z0-9-]+\.md|\]\([^)]*[a-z0-9-]+\.md\)|`[a-z0-9-]+\.md`)' docs .agent --include='*.md' > /tmp/refs_before.txt
wc -l /tmp/refs_before.txt
```

- [x] **Step 2: Rewrite references file-by-file**

For each referrer, update both `docs/research/<file>.md` style paths and relative `](...)` links to the new cluster path, AND backtick prose references where they imply a path. Within a moved research doc, sibling links become relative across clusters (e.g. a `retrieval/` doc linking to `belief-temporal/belief-layer.md` uses `../belief-temporal/belief-layer.md`; same-cluster stays bare). Use Edit per file (do not bulk-sed blindly — some `.md` backticks are .agent docs that did NOT move). Map of moved files -> cluster is in Task 1 Interfaces.

- [x] **Step 3: Run the link-checker — expect 0 dangling**

```bash
python /tmp/linkcheck.py
```
Expected: `OK: all markdown links resolve (...)`. If DANGLING printed, fix each listed path and re-run until clean.

- [x] **Step 4: Commit**

```bash
cd /c/Users/you/IdeaProjects/projects/archolith/menhir-frontier
git add -- <explicit list of edited .md paths>
git commit -m "docs: rewrite cross-links for research-corpus restructure" # + Co-Author trailer
```

---

### Task 3: Rewrite the master research index

**Files:**
- Modify: `docs/research/README.md`

**Interfaces:**
- Consumes: new cluster paths (Task 1); registers the two orphans.

- [x] **Step 1: Replace the flat Canonical/Speculative tables with a cluster-indexed view**

Keep ALL governance sections verbatim (status vocabulary, anti-sprawl rules, promotion ladder, durable save list, parked concepts, superseded section). Replace the reading-order + the two doc tables so every doc links to its new subdir path. ADD rows for `retrieval/intent-warden.md` (status `supported-by-eval`) and `direction/llm-reviewer-seams.md` (status `speculative`). Update the corpus map + reading-order clusters to name both new docs.

- [x] **Step 2: Link-check + confirm both orphans now appear**

```bash
cd /c/Users/you/IdeaProjects/projects/archolith/menhir-frontier
python /tmp/linkcheck.py
grep -c 'intent-warden.md\|llm-reviewer-seams.md' docs/research/README.md   # expect >= 2
```
Expected: link-check OK; grep count >= 2.

- [x] **Step 3: Commit**

```bash
git add docs/research/README.md
git commit -m "docs: cluster-index research README + register intent-warden, llm-reviewer-seams" # + trailer
```

---

### Task 4: Normalize status headers (spec §6)

**Files:**
- Modify (header line only): `retrieval/intent-warden.md`, `retrieval/oracle-amplified-retrieval.md`, `retrieval/oracle-runtime-interfaces.md`, `process/archolith-bench-operational-model.md`, `direction/oracle-architecture.md`, `process/research-process.md`, `direction/semantic-operating-system.md`, `process/research-vs-shipped-inventory.md`

**Interfaces:**
- Consumes: new paths (Task 1).

- [x] **Step 1: Apply each correction from the spec §6 table**

For each doc, set the `## Status` value to the controlled label (keep any descriptive note as a following line; do NOT touch the rest of the body):
- intent-warden.md -> `supported-by-eval`
- oracle-amplified-retrieval.md -> `supported-by-spike`
- oracle-runtime-interfaces.md -> `supported-by-spike`
- archolith-bench-operational-model.md -> add `## Status\n\ncanonical`
- oracle-architecture.md -> add `## Status\n\nactive`
- research-process.md -> `canonical` (above the taxonomy list, not replacing it)
- semantic-operating-system.md -> `active`
- research-vs-shipped-inventory.md -> `canonical (snapshot)`

- [x] **Step 2: Verify each header parses to a controlled label**

```bash
cd /c/Users/you/IdeaProjects/projects/archolith/menhir-frontier/docs/research
for f in retrieval/intent-warden.md retrieval/oracle-amplified-retrieval.md retrieval/oracle-runtime-interfaces.md process/archolith-bench-operational-model.md direction/oracle-architecture.md process/research-process.md direction/semantic-operating-system.md process/research-vs-shipped-inventory.md; do
  printf "%-45s " "$f"; awk '/^## *Status/{getline; while($0 ~ /^[[:space:]]*$/) getline; print; exit}' "$f"
done
```
Expected: each prints a controlled-vocabulary label.

- [x] **Step 3: Confirm only header lines changed (no body churn)**

```bash
git diff --stat docs/research/   # small line counts per file
```

- [x] **Step 4: Commit**

```bash
git add -- <the 8 paths>
git commit -m "docs: normalize research-doc status headers to controlled vocabulary" # + trailer
```

---

### Task 5: Add the roadmap index

**Files:**
- Create: `docs/roadmap/README.md`

- [x] **Step 1: Write the altitude-grouped index (spec §4)**

Three groups with one-line purpose each: Active build sequencing (weekend-oracle-runtime-roadmap.md, oracle-integration-plan.md); L3/L4 GAP decision-support — proposals not rungs (l3l4-overlay-sequencing-options.md, l3l4-hybrid-sketch.md); Strategic notes — not rungs (org-scale-menhir.md, doc-drift-watch-mvp.md). Link each file.

- [x] **Step 2: Link-check**

```bash
python /tmp/linkcheck.py
```
Expected: OK.

- [x] **Step 3: Commit**

```bash
git add docs/roadmap/README.md
git commit -m "docs: add roadmap index grouped by altitude" # + trailer
```

---

### Task 6: Triage `.agent/plans/`

**Files:**
- Move (git mv) to `.agent/archive/plans/`: `session-handoff-2026-06-28-live-verification.md`, `menhir-query-profile-evaluation.md`

- [x] **Step 1: Confirm archive dir exists, then git mv**

```bash
cd /c/Users/you/IdeaProjects/projects/archolith/menhir-frontier
ls .agent/archive/plans/ >/dev/null 2>&1 || mkdir -p .agent/archive/plans
git mv .agent/plans/session-handoff-2026-06-28-live-verification.md .agent/archive/plans/
git mv .agent/plans/menhir-query-profile-evaluation.md .agent/archive/plans/
```

- [x] **Step 2: Rewrite any inbound links to the two moved plans, then link-check**

```bash
cd /c/Users/you/IdeaProjects/projects/archolith/menhir-frontier
grep -rIn 'session-handoff-2026-06-28-live-verification.md\|menhir-query-profile-evaluation.md' docs .agent --include='*.md'
# Edit each referrer to the .agent/archive/plans/ path, then:
python /tmp/linkcheck.py
```
Expected: link-check OK after edits.

- [x] **Step 3: Commit**

```bash
git add .agent/plans/ .agent/archive/plans/ -- <plus any edited referrers>
git commit -m "docs: archive consumed session-handoff + query-profile evaluation plans" # + trailer
```

---

### Task 7: Final verification + CHANGELOG

**Files:**
- Modify: `.agent/CHANGELOG.md`

- [x] **Step 1: Full corpus link-check (the acceptance gate)**

```bash
cd /c/Users/you/IdeaProjects/projects/archolith/menhir-frontier
python /tmp/linkcheck.py
```
Expected: `OK: all markdown links resolve`.

- [x] **Step 2: Confirm the move accounting**

```bash
cd /c/Users/you/IdeaProjects/projects/archolith/menhir-frontier
git log --oneline -7
git diff --name-status -M HEAD~6..HEAD | grep -E '^R' | wc -l   # 26 research + 2 plans = 28 renames
ls docs/research/*/ | wc -l   # files distributed across 8 subdirs
```
Expected: 28 renames total across the restructure commits; 0 research .md left at top level except README.md.

- [x] **Step 3: Add CHANGELOG entry (10 most recent dated entries only)**

`## 2026-06-29 - research corpus restructure` with bullets: clustered docs/research into 8 subdirs; registered intent-warden + llm-reviewer-seams; normalized 8 status headers; added roadmap index; archived 2 consumed plans; 0 dangling links.

- [x] **Step 4: Commit**

```bash
git add .agent/CHANGELOG.md
git commit -m "docs: changelog for research-corpus restructure" # + trailer
```

---

## Self-Review

- **Spec coverage:** §3 layout -> Task 1; §3 OD-2 (llm-reviewer-seams in direction/) -> Task 1 + 3; §4 roadmap index -> Task 5; §5 plans triage -> Task 6; §6 status table -> Task 4; §7 safety (git mv, link rewrite, link-check, manifest-in-body) -> Tasks 1,2,5,6,7; §8 verification -> Task 7. All covered.
- **Placeholder scan:** subdir-README and index bodies are authored at execution from spec §3/§4 (content rules given, not "TBD"); status labels are enumerated exactly. No "handle edge cases" placeholders.
- **Type/path consistency:** the new-path map in Task 1 Interfaces is the single source the link rewrites (Task 2) and index (Task 3) consume; intent-warden -> `retrieval/`, llm-reviewer-seams -> `direction/` consistent across Tasks 1/3/4.
