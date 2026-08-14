# Menhir M1 Domain Architecture Audit — External Pass 1 of 2

- Repository: `Archolith/menhir`
- Commit: `eebf6d6dd83f15083167bf847b639d24b953fdc9`
- Scope: exactly 26 named domain files (21 root files plus all five `domain/truth` files)
- Status: **DRAFT — refined continuously during the audit**

## 1. Executive Summary

Pending mechanical analysis. Source acquisition is pinned to the named commit. The local environment cannot resolve `github.com`; authenticated GitHub API reads are being used instead. Empty GitHub code-search results are not treated as absence because this repository is not code-search indexed.

## 2. Findings

Pending.

## 3. Inverted Dependency Table

Pending.

## 4. Cycles

Pending.

## 5. Blast Radius

Pending.

## 6. `artifact_reconciliation.py` Responsibility Map

Pending.

## 7. Bug-Class Sweep

Pending probe implementation and control test.

## 8. Disproved Candidates

Pending.

## 9. Open Questions

- **Environment:** direct Git clone/push cannot be used because DNS resolution for `github.com` fails. Repository reads and the final branch write use the authenticated GitHub connector.

## 10. Coverage Table

Pending independent `wc -l` reconciliation to 5,601.

## 11. Citation Self-Check

Pending.

## 12. What Was Checked, and What Could Not Be Verified in This Environment

Pending.

Temporary source-mirror acquisition link for this draft (removed before finalization): [pinned archive](https://git-downloader.com/api/zip?url=https%3A%2F%2Fgithub.com%2FArcholith%2Fmenhir&commit=eebf6d6dd83f15083167bf847b639d24b953fdc9).

## 13. Review Confidence (/100)

Pending.
