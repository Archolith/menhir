"""Event-history perception boundary — the LLM extraction/admission seam for grounded categorical
occurrences (Phase 3 of the approved event-history work).

This is a *generic, offline-only* boundary. It turns prose episodes into unbound, source-grounded
``EventPerceptionProposal`` objects via a single LLM pass, then a pure builder turns a proposal
into a durable ``TypedEventAssertion``. It wires up NOTHING: no settings/flag, no runtime/scheduler/
endpoint, no repository/View write, no recall authority, and no telemetry beyond the injected
``on_drop`` observability seam. Perceiving and admitting are pure given the injected ``llm_complete``;
the caller owns persistence, projection, and authority.

Only *completed acquisition* semantics are recognized: a small conservative predicate registry maps
surface verbs to the single canonical predicate ``acquired``. There is deliberately NO ownership
supersession — a newer acquisition records an occurrence and never invalidates an older one (that is
the event-history invariant the domain contract already enforces).

The parser is fail-closed and mirrors the typed-scalar boundary's conventions (same required-field
helpers, unique substring grounding, and drop-reason codes) so the two perception seams behave
consistently. All parsing/grounding helpers are imported from ``typed_scalar_rules`` rather than
re-derived, so this admission rule set cannot drift from the established one.

Implementation lives in sibling modules (``event_history_perception_registry`` for the predicate
registry/veto patterns/object canonicalization, ``event_history_perception_parse`` for the LLM
extraction pass and admission parser, ``event_history_perception_build`` for the assertion builder);
this module re-exports the public surface so existing import sites keep working unchanged.
"""

from __future__ import annotations

from menhir.services.event_history_perception_build import (
    EventAssertionBuildResult,
    build_event_assertion,
)
from menhir.services.event_history_perception_parse import (
    EVENT_SYSTEM_PROMPT,
    EventPerceptionProposal,
    extract_events_once,
    parse_event_row,
)
from menhir.services.event_history_perception_registry import (
    CANONICAL_PREDICATE_ACQUIRED,
    canonicalize_predicate,
)
from menhir.services.seam_types import LlmComplete
