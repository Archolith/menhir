"""TypedAssertion — the durable, entity-bound, grounded record of one typed-scalar observation.

A `TypedAssertion` is the rebuild INPUT for a ScalarStateView (see
`.agent/plans/menhir-scalar-state-view-implementation-plan.md`, Piece C commit 1). It is NOT a
View and NOT recall memory: it is the persisted event log the deterministic fold replays, so a
View can be rebuilt exactly even though perception itself is probabilistic.

## Three-level identity (review C.1, rev 2)

* `claim_key` — the STABLE, SOURCE-GROUNDED locator: `subject_uuid + episode_uuid + span_start +
  span_end + claim_ordinal`. It contains NO extracted semantics (no attribute/kind/value), so a
  newer perceiver can CORRECT a misclassification (attribute owned -> sold) on the same source
  span and still supersede the wrong prior interpretation via the same head. Two distinct claims
  in one episode get different spans/ordinals, so neither collapses the other.
* `assertion_key` — the fully-interpreted assertion: `claim_key + perceiver_version + attribute +
  scope + value_kind + unit + operation + normalized value`. Exact idempotency: same version +
  same interpretation -> one node. A NEWER perceiver_version -> a new assertion that supersedes the
  claim's prior current one; a DIFFERENT value at the SAME version does NOT supersede (stochastic
  disagreement must not replace current state) — it is stored non-current for audit.
* `slot_key` — (entity, attribute, scope, value_kind, unit): what a ScalarStateView folds over.
  Note slot lives in the assertion, not the claim: correcting the slot is a supersession, not a
  new claim.

The repository keeps ONE current assertion per `claim_key` behind a `:TypedAssertionHead` so
supersession is atomic. Authority is NOT derived from the stored tier alone — C.2's fold computes
the effective tier from the assertions it actually selects (weakest required contributor).
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field
from typing import Any

#: identity-contract version stamped on every head + assertion. v2 is the source_key-anchored
#: contract (head keyed by source_key; assertion_key built from source_key). v1 (unstamped) is the
#: original claim_key-anchored contract. Activation refuses to run over any v1 node (fresh-only), so
#: the source_key/assertion_key identity spaces never mix. Bump only on a real identity change.
IDENTITY_VERSION: int = 2

#: the typed scalar families a TypedAssertion may carry (matches ScalarStateKind.VALUE_KINDS).
VALUE_KINDS: frozenset[str] = frozenset({
    "boolean", "status", "count", "duration", "frequency",
    "money", "measurement", "clock_time", "weekday",
})

#: value_kinds whose value is a number (or a [lo, hi] numeric range). Only these may take a delta.
NUMERIC_KINDS: frozenset[str] = frozenset({"count", "money", "measurement", "duration", "frequency"})

#: how the value relates to the running state: a stated absolute, a signed increment, or an EXPIRE
#: (the value ENDED with no current replacement -- "I used to X"). An expire carries the OLD value as
#: provenance (validated like an absolute of its kind) but the fold treats a latest-expire as
#: "current unknown, no View" rather than a standing value.
OPERATIONS: frozenset[str] = frozenset({"absolute", "delta", "expire"})

#: framing of an `absolute` value (relevant ONLY when operation == "absolute"). `correction` marks an
#: explicit fix of a previously-stated value ("actually, 182") and is used ONLY to break an otherwise
#: unbreakable equal-`valid_at` tie at fold time -- never to override a genuinely later value. It is
#: DETERMINISTICALLY classified from the grounded source span by code (NOT emitted by the LLM), and is
#: intentionally ABSENT from every identity key (source_key/claim_key/assertion_key), so it never forks
#: assertion identity. Legacy/missing -> `ordinary`.
ABSOLUTE_SEMANTICS: frozenset[str] = frozenset({"ordinary", "correction"})

#: trust tiers, ordered LOW -> HIGH. Only user/manual/trusted_tool may ground an authoritative View;
#: agent inference stays advisory. Order drives the monotonic never-downgrade upgrade.
EVIDENCE_TIERS: tuple[str, ...] = ("agent", "trusted_tool", "manual", "user")

#: how `valid_at` was determined — provenance for the world-time on the assertion.
TIME_BASES: frozenset[str] = frozenset({"explicit", "episode_reference", "learned_fallback"})

_WEEKDAYS: frozenset[str] = frozenset({
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
})
_CLOCK_RE = re.compile(r"^\d{1,2}:\d{2}$")


def evidence_rank(tier: str) -> int:
    """Rank of an evidence tier (higher = more trusted); unknown tiers rank lowest."""
    try:
        return EVIDENCE_TIERS.index(tier)
    except ValueError:
        return -1


#: max value of any one SemVer component; components >= this collide, so they are rejected (rank 0).
_VERSION_COMPONENT_MAX = 10 ** 6


def perceiver_rank(version: str) -> int:
    """A sortable integer for a FIXED-WIDTH SemVer perceiver version, so cross-major ordering is
    correct: "v2" > "v1.3" > "v1.2" > "v1". Each of major/minor/patch occupies a fixed 6-digit
    field (`major*10^12 + minor*10^6 + patch`); a variable-length accumulator would wrongly rank
    "v1.3" (10003) above "v2" (2). Non-SemVer, >3-component, or out-of-range versions rank 0 so they
    never spuriously supersede. Computed in Python; the store compares integers, not strings."""
    s = str(version or "").lstrip("vV")
    parts = s.split(".")
    if not parts or len(parts) > 3:
        return 0
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return 0
    if any(n < 0 or n >= _VERSION_COMPONENT_MAX for n in nums):
        return 0
    while len(nums) < 3:  # pad minor/patch so "v1" == "v1.0.0"
        nums.append(0)
    major, minor, patch = nums
    return major * (_VERSION_COMPONENT_MAX ** 2) + minor * _VERSION_COMPONENT_MAX + patch


def build_source_key(episode_uuid: str, span_start: int, span_end: int, claim_ordinal: int) -> str:
    """The BINDING-STABLE claim locator hash: episode_uuid + span offsets + ordinal, with NO
    subject_uuid, so it is invariant under merge rebinding. This is the SINGLE definition of the
    source_key algorithm; `TypedAssertion.source_key` and the perception `TypedScalarProposal` both
    call it, so the durable identity a proposal computes cannot drift from the one the assertion
    persists."""
    h = hashlib.sha256()
    for part in (str(episode_uuid).strip(), str(span_start), str(span_end), str(claim_ordinal)):
        h.update(part.encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()


def _is_number(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _is_finite_number(value: Any) -> bool:
    """True for a real, representable number. Excludes bool; for a Python int, tests representability
    via `float()` — an oversized int (e.g. 10**1000) has no float image, so `math.isfinite(float(x))`
    would raise `OverflowError`; we catch it and return False so the validator honors its documented
    `ValueError`-only failure contract instead of crashing the extraction pass. Floats must be
    finite (NaN/inf rejected)."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        try:
            return math.isfinite(float(value))
        except OverflowError:
            return False
    return isinstance(value, float) and math.isfinite(value)


def _num_norm(x: float) -> str:
    """Stable string for one number. An arbitrary-precision int is stringified directly (never routed
    through `float()`/`math.isfinite`, which would `OverflowError` on a huge int); a non-finite float
    (NaN/inf) is stringified defensively rather than run through `int()` (which raises on NaN/inf).
    `validate_value` rejects both non-finite and non-representable numbers upstream, so this is a
    belt-and-suspenders guard that never itself crashes the key builder."""
    if isinstance(x, int) and not isinstance(x, bool):
        return str(x)
    if not math.isfinite(x):
        return str(x)
    return str(int(x)) if float(x) == int(x) else str(x)


def normalize_scalar(value: Any) -> str:
    """Stable normalized string for a typed scalar value — the persisted value AND part of the
    assertion_key. Mirrors ScalarStateKind._scalar_norm so the assertion and the View agree."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return _num_norm(value)
    if isinstance(value, (list, tuple)):
        parts = [_num_norm(x) if _is_number(x) else str(x).strip() for x in value]
        # A DEGENERATE range [N, N] is not a range: "between N and N" IS the point N, so it carries
        # no interval information and must normalize identically to the scalar N. Without this, one
        # k-sample rendering a hedged value as [1300, 1300] and another as 1300 produce two DIFFERENT
        # `_interpretation_label`s for the same reading of the same span, splitting the vote and
        # vetoing the claim at threshold=1.0 (measured: LME ns a2f3aa27, followers 1300 lost 2/3 vs
        # 1/3 on this alone while the stale 1250 committed unanimously). Endpoints are compared AFTER
        # normalization so mixed-typed pairs like [1300, "1300"] also collapse. A GENUINE range with
        # lo != hi is untouched -- interval semantics are preserved wherever they carry information.
        if len(parts) == 2 and parts[0] == parts[1]:
            return parts[0]
        return "-".join(parts)
    return str(value).strip()


def validate_value(value_kind: str, operation: str, value: Any) -> None:
    """Kind-specific value validation + delta compatibility. Raises ValueError on a mismatch."""
    if operation == "delta":
        if value_kind not in NUMERIC_KINDS:
            raise ValueError(f"delta operation requires a numeric value_kind, got {value_kind!r}")
        if not _is_finite_number(value):
            raise ValueError("a delta value must be a single finite number")
        return
    if value_kind == "boolean":
        if not isinstance(value, bool):
            raise ValueError("boolean value_kind requires a bool value")
    elif value_kind == "weekday":
        if not (isinstance(value, str) and value.strip().lower() in _WEEKDAYS):
            raise ValueError(f"weekday value must be a day name, got {value!r}")
    elif value_kind == "clock_time":
        if not (isinstance(value, str) and _CLOCK_RE.match(value.strip())):
            raise ValueError(f"clock_time value must be HH:MM, got {value!r}")
        hh, mm = value.strip().split(":")
        if not (0 <= int(hh) <= 23 and 0 <= int(mm) <= 59):
            raise ValueError(f"clock_time must be a real time 00:00-23:59, got {value!r}")
    elif value_kind == "status":
        if not (isinstance(value, str) and value.strip()):
            raise ValueError("status value must be a non-empty string")
    elif value_kind in NUMERIC_KINDS:
        ok_single = _is_finite_number(value)
        ok_range = (
            isinstance(value, (list, tuple)) and len(value) == 2
            and all(_is_finite_number(x) for x in value)
        )
        if not (ok_single or ok_range):
            raise ValueError(
                f"{value_kind} value must be a finite number or a [lo, hi] finite range, got {value!r}")


@dataclass(frozen=True)
class TypedAssertion:
    """One durable typed-scalar observation. See module docstring."""

    subject_uuid: str          # resolved entity identity (post-correlation)
    subject_display: str       # human-readable subject (display surface, never the key)
    attribute: str             # state family (owned, watched, wake, ...)
    scope: str                 # differentiating modifier (mcu, pre-1920, ""), may be blank
    value_kind: str            # one of VALUE_KINDS
    unit: str                  # measurement unit ("", hours, usd, ...), may be blank
    operation: str             # one of OPERATIONS
    value: Any                 # kind-typed: number, [lo, hi], bool, or string state
    stated_span: str           # source quote (readable grounding)
    episode_uuid: str          # provenance / Law-2 replay ledger key
    valid_at: str              # world-valid time (ISO)
    learned_at: str            # ingest time (ISO)
    span_start: int = -1       # source char offset (>=0 when known); with span_end locates the claim
    span_end: int = -1         # source char offset (>= span_start when known)
    claim_ordinal: int = 0     # disambiguates same-slot claims in one episode when offsets are absent
    time_basis: str = "explicit"
    evidence_tier: str = "agent"
    perceiver_version: str = "v1"
    namespace: str | None = None
    absolute_semantics: str = "ordinary"   # ordinary | correction (framing of an absolute; NOT in any key)
    metadata: dict[str, Any] = field(default_factory=dict)
    # 4a.1 write-time observation embedding (optional): the cosine surface for the recall observation
    # lane, computed from stated_span at persist time. None -> no embedding written (the resumable
    # backfill fills it later). embed_version stamps the model, so it agrees with the backfill's
    # re-embed-on-change predicate. NOT part of any identity key.
    name_embedding: list[float] | None = None
    embed_version: str | None = None

    def __post_init__(self) -> None:
        # Fail-closed identity + grounding + typing: an assertion missing any of these cannot
        # anchor state, so it is a hard error rather than a silently weak record.
        if not (self.subject_uuid or "").strip():
            raise ValueError("TypedAssertion requires a non-blank subject_uuid (resolved identity)")
        if not (self.attribute or "").strip():
            raise ValueError("TypedAssertion requires a non-empty attribute (state family)")
        if self.value_kind not in VALUE_KINDS:
            raise ValueError(f"value_kind {self.value_kind!r} not in {sorted(VALUE_KINDS)}")
        if self.operation not in OPERATIONS:
            raise ValueError(f"operation {self.operation!r} not in {sorted(OPERATIONS)}")
        if not (self.stated_span or "").strip():
            raise ValueError("TypedAssertion requires a non-empty stated_span (source grounding)")
        if not (self.episode_uuid or "").strip():
            raise ValueError("TypedAssertion requires an episode_uuid (provenance)")
        if self.evidence_tier not in EVIDENCE_TIERS:
            raise ValueError(f"evidence_tier {self.evidence_tier!r} not in {EVIDENCE_TIERS}")
        if self.time_basis not in TIME_BASES:
            raise ValueError(f"time_basis {self.time_basis!r} not in {sorted(TIME_BASES)}")
        if self.absolute_semantics not in ABSOLUTE_SEMANTICS:
            raise ValueError(
                f"absolute_semantics {self.absolute_semantics!r} not in {sorted(ABSOLUTE_SEMANTICS)}")
        if not (isinstance(self.span_start, int) and isinstance(self.span_end, int)):
            raise ValueError("span_start/span_end must be ints")
        # Offsets are either both known (>=0, end>=start) or both absent (-1); a half-known span is
        # a bug (the claim locator would be neither offset-grounded nor cleanly ordinal-grounded).
        both_known = self.span_start >= 0 and self.span_end >= 0
        both_absent = self.span_start == -1 and self.span_end == -1
        if not (both_known or both_absent):
            raise ValueError("span_start/span_end must both be known (>=0) or both -1")
        if both_known and self.span_end < self.span_start:
            raise ValueError("span_end must be >= span_start")
        if self.claim_ordinal < 0:
            raise ValueError("claim_ordinal must be >= 0")
        validate_value(self.value_kind, self.operation, self.value)

    @property
    def slot_key(self) -> tuple[str, str, str, str, str]:
        """The (entity, attribute, scope, value_kind, unit) tuple a ScalarStateView folds over."""
        return (
            self.subject_uuid.strip(),
            self.attribute.strip().lower(),
            self.scope.strip().lower(),
            self.value_kind.strip().lower(),
            (self.unit or "").strip().lower(),
        )

    @property
    def source_key(self) -> str:
        """BINDING-STABLE claim locator: episode_uuid + span + ordinal, with NO subject_uuid. It is
        invariant under merge rebinding (which moves subject_uuid), so re-perceiving the same
        episode/span after a merge resolves to the SAME head instead of forking a duplicate. The
        head stores this and C.4's record path resolves by it (Piece C.3 blocker 5)."""
        return build_source_key(
            self.episode_uuid, self.span_start, self.span_end, self.claim_ordinal)

    @property
    def claim_key(self) -> str:
        """STABLE, SOURCE-GROUNDED locator: subject_uuid + source_key (episode + span + ordinal).
        Contains NO extracted semantics, so a newer perceiver can correct the attribute/kind/value
        on the same source span and still supersede the wrong prior interpretation (same head). Two
        distinct claims in one episode differ in span/ordinal, so neither collapses the other. Note:
        this includes subject_uuid, so it is NOT binding-stable; ``source_key`` is the identity that
        survives rebinding, and the head persists both."""
        h = hashlib.sha256()
        parts = (
            self.subject_uuid.strip(),
            self.episode_uuid.strip(),
            str(self.span_start), str(self.span_end), str(self.claim_ordinal),
        )
        for part in parts:
            h.update(str(part).encode("utf-8", "replace"))
            h.update(b"\x00")
        return h.hexdigest()

    @property
    def assertion_key(self) -> str:
        """Exact idempotency over the FULLY-INTERPRETED assertion, built on the BINDING-STABLE
        ``source_key`` (NOT claim_key): source_key + perceiver_version + attribute + scope +
        value_kind + unit + operation + normalized value. Because it omits subject_uuid, re-perceiving
        the same source span + same interpretation after a merge produces the SAME assertion_key, so
        a user-tier re-confirmation upgrades the CURRENT node instead of forking a non-current sibling
        (C.3 blocker 2). Same version + same interpretation -> one node; a newer perceiver_version ->
        a new assertion that supersedes the current one; a different value at the SAME version does
        NOT supersede (stored non-current for audit)."""
        h = hashlib.sha256()
        parts = (
            self.source_key,
            str(self.perceiver_version),
            self.attribute.strip().lower(),
            self.scope.strip().lower(),
            self.value_kind.strip().lower(),
            (self.unit or "").strip().lower(),
            self.operation.strip().lower(),
            normalize_scalar(self.value),
        )
        for part in parts:
            h.update(str(part).encode("utf-8", "replace"))
            h.update(b"\x00")
        return h.hexdigest()

    @property
    def perceiver_rank(self) -> int:
        return perceiver_rank(self.perceiver_version)

    @property
    def evidence_rank(self) -> int:
        return evidence_rank(self.evidence_tier)
