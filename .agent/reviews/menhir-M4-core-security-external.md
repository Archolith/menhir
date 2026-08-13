# Menhir M4 — Core Runtime and Backend Security Audit (External)

**Repository:** `Archolith/menhir`  
**Pinned commit:** `eebf6d6dd83f15083167bf847b639d24b953fdc9`  
**Audit branch:** `audit/m4-core-security-external`  
**Scope:** 23 files under `src/menhir/core/` and `src/menhir/` root; declared total 5,097 lines  
**Status:** DRAFT — source review has not begun

> Resume rule: start at the first `NOT READ` row in Section 13. A row changes to `READ` only in the same commit that records the evidence obtained from that file.

## 1. Executive Summary, highest-risk result first

DRAFT — no source files read yet.

## 2. Trust Boundary Register — every caller assumption, whether each transport enforces it, with the call chain

DRAFT — no source files read yet.

## 3. Authorization Surface — privileged actions and what gates them

DRAFT — no source files read yet.

## 4. Redaction Verification — executed adversarial inputs and real output

DRAFT — not executed yet.

## 5. Diagnostics Exposure — operator_diagnostics.py reachability by tier

DRAFT — no source files read yet.

## 6. Startup and Credential Handling — preflight fail-open/closed, bootstrap file modes and logging

DRAFT — no source files read yet.

## 7. Guard and Identity Analysis — ingest_guard.py, reader_identity.py

DRAFT — no source files read yet.

## 8. Injection and Traversal Register

DRAFT — no source files read yet.

## 9. Information Disclosure Register

DRAFT — no source files read yet.

## 10. Bug-Class Sweep Results — command and output, or NOT RUN

DRAFT — all six sweeps are NOT RUN.

## 11. Disproved Candidates, with the evidence that disproved them

DRAFT — none yet.

## 12. Open Questions

DRAFT — none yet.

## 13. Coverage Table — all 23 files, measured line reconciliation against 5,097

| # | Scope file | Declared lines | Measured lines | Status | Evidence / resume note |
|---:|---|---:|---:|---|---|
| 1 | `src/menhir/core/backend_client_ops.py` | 703 | — | NOT READ | Resume here. |
| 2 | `src/menhir/core/backend_protocol.py` | 683 | — | NOT READ | — |
| 3 | `src/menhir/core/runtime.py` | 646 | — | NOT READ | — |
| 4 | `src/menhir/core/backend_runtime_admin_ops.py` | 603 | — | NOT READ | — |
| 5 | `src/menhir/core/backend_runtime_data_ops.py` | 513 | — | NOT READ | — |
| 6 | `src/menhir/core/runtime_preflight.py` | 456 | — | NOT READ | — |
| 7 | `src/menhir/core/bootstrap.py` | 316 | — | NOT READ | — |
| 8 | `src/menhir/operator_diagnostics.py` | 297 | — | NOT READ | — |
| 9 | `src/menhir/core/runtime_support.py` | 167 | — | NOT READ | — |
| 10 | `src/menhir/privacy.py` | 162 | — | NOT READ | — |
| 11 | `src/menhir/core/backend_shared.py` | 129 | — | NOT READ | — |
| 12 | `src/menhir/core/backend_client.py` | 102 | — | NOT READ | — |
| 13 | `src/menhir/core/request_context.py` | 74 | — | NOT READ | — |
| 14 | `src/menhir/core/ingest_guard.py` | 74 | — | NOT READ | — |
| 15 | `src/menhir/core/backend_runtime.py` | 41 | — | NOT READ | — |
| 16 | `src/menhir/core/backend_impl.py` | 30 | — | NOT READ | — |
| 17 | `src/menhir/core/__init__.py` | 27 | — | NOT READ | — |
| 18 | `src/menhir/core/backend_config.py` | 18 | — | NOT READ | — |
| 19 | `src/menhir/__init__.py` | 16 | — | NOT READ | — |
| 20 | `src/menhir/main.py` | 14 | — | NOT READ | — |
| 21 | `src/menhir/core/backend_runtime_ops.py` | 12 | — | NOT READ | — |
| 22 | `src/menhir/core/reader_identity.py` | 11 | — | NOT READ | — |
| 23 | `src/menhir/__main__.py` | 3 | — | NOT READ | — |
|  | **Totals** | **5,097** | **—** | **0/23 READ** | Reconcile after all files are measured. |

## 14. What Was Checked, and what could not be verified in this environment

DRAFT — only branch creation and report scaffolding completed. No source was read and no command was executed.

## 15. Review Confidence (/100). If any scope went unread, cap it well below 80.

**Current confidence: 0/100.** All 23 scope files remain unread.
