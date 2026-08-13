# Menhir M2 Compound Audit Results

**Target:** `Archolith/menhir@eebf6d6dd83f15083167bf847b639d24b953fdc9`  
**Status:** DRAFT — evidence collection in progress  
**Scope:** exactly 24 files under `src/menhir/api/` (expected 5,565 lines)

## Executive Summary

The highest-risk result found so far is an authorization-tier violation in `POST /api/phase3/reset`: the handler requires only the `agent` tier but invokes namespace deletion and purges turn evidence. The ordinary namespace-delete route and generic backend policy classify the same operation as `operator`-only.

Additional confirmed candidates under final verification:

- Explorer authorization bypass when a loopback-bound Menhir instance is exposed through a same-host reverse proxy; the bypass condition accepts `loopback_bound` independently of the request peer/forwarding-header checks and reaches candidate approve/reject writes.
- OAuth authorization codes are marked redeemed before `client_id`, `redirect_uri`, resource, and PKCE verifier validation, allowing a caller who obtains a code to invalidate it without the verifier.
- The consent-session cookie is `SameSite=Strict`, preventing the advertised one-click path on ordinary cross-site OAuth authorization navigations.

Exact line citations, executable reproductions, complete tier matrix, six bug-class sweeps, test analysis, disproved candidates, and coverage reconciliation will be filled in as the audit completes.

## Findings by Audit Type

### A1 Functional Correctness

_Draft in progress._

### A2 Security

_Draft in progress._

### A3 Architecture

_Draft in progress._

### A4 Maintainability

_Draft in progress._

### A5 Performance

_Draft in progress._

### A6 Test Coverage

_Draft in progress._

### A7 LLM/AI

_Draft in progress._

### Compliance

_Draft in progress._

## Auth Tier Enforcement Matrix

_Draft in progress._

## Bug-Class Sweep Results

_Draft in progress._

## Test Coverage Gap Analysis

_Draft in progress._

## Disproved Candidates

- The DCR rate-limit settings appeared unused in `oauth_as_register.py`, but normal server construction rewires `_register_limiter` with `build_register_limiter(settings)` in `server_support.py`. Direct module invocation remains a separate testability concern; the live `create_app` path honors the settings.

## Open Questions

_Draft in progress._

## Coverage Table

All 24 files have been read; final line-count reconciliation and per-file notes are pending.

## What Was Checked / Environment Limits

_Draft in progress._

## Review Confidence

_Draft; final score pending._
