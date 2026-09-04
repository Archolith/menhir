"""Replay corpus for the canonical-self binding decision (plan Phase 6).

The existing extraction-lab fixtures supply the TRUE-POSITIVE half -- real first-person user
turns and assistant turns, LongMemEval-derived plus three production RCA cases. They cannot be
replayed against this contract directly, because they carry role as a text prefix inside content
(``"user: I'm planning..."``), which D1 explicitly refuses to trust. Parsing that prefix would
validate the contract against exactly the input it rejects.

So this corpus supplies role as out-of-band trusted metadata (legitimate: these are Menhir-owned
fixtures, not model output) and adds the four control categories the lab fixtures lack:
project-scan narrative, generic account/RBAC prose, manual semantic memories, and retry/repair.

The controls are the point. A corpus of only self-turns proves binding works; it cannot prove
binding is NARROW, and Phase 6's gate is zero false-positive self binds.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from menhir.domain.self_identity import SelfIdentityContext, self_context_for_pending_episode
from menhir.infrastructure.self_binding import SelfBindOutcome


@dataclass(frozen=True)
class CorpusCase:
    """One replayable episode: what was ingested, and what binding must decide."""

    name: str
    category: str
    #: Persisted source, i.e. the admission gate's verdict -- not the caller's request.
    source: str
    #: Entity names the extractor emits for this episode.
    entity_names: tuple[str, ...]
    expected: SelfBindOutcome
    #: True only where a human self identity legitimately exists in this payload.
    expects_self: bool = False
    namespace: str = "default"
    note: str = ""
    identity_override: SelfIdentityContext | None = field(default=None)

    def identity(self) -> SelfIdentityContext:
        if self.identity_override is not None:
            return self.identity_override
        return self_context_for_pending_episode(
            source=self.source,
            namespace=self.namespace,
            episode_uuid=f"ep-{self.name}",
            source_kind=self.source,
        )


# --------------------------------------------------------------------------- true positives

_SELF_TURNS = (
    CorpusCase(
        name="first_person_fact",
        category="trusted_user_turn",
        source="user",
        entity_names=("user", "Chicago"),
        expected=SelfBindOutcome.BOUND,
        expects_self=True,
        note="The ordinary case: a gate-approved human turn stating a fact about themselves.",
    ),
    CorpusCase(
        name="single_self_alias_with_third_party",
        category="trusted_user_turn",
        source="user",
        entity_names=("user", "Rachel"),
        expected=SelfBindOutcome.BOUND,
        expects_self=True,
        note="One self alias beside a named third party is the ordinary shape.",
    ),
    CorpusCase(
        name="manual_semantic_memory",
        category="manual_memory",
        source="manual",
        entity_names=("user", "espresso"),
        expected=SelfBindOutcome.BOUND,
        expects_self=True,
        note="`manual` is apex-tier and gated identically to `user`; a granted claim is the human.",
    ),
    CorpusCase(
        name="human_turn_about_third_parties_only",
        category="trusted_user_turn",
        source="user",
        entity_names=("Rachel", "Chicago"),
        expected=SelfBindOutcome.NO_SELF_CANDIDATE,
        note="Trusted author, but no self alias in the payload -- nothing to bind.",
    ),
)


# --------------------------------------------------------------------------- controls

_CONTROLS = (
    # --- review P1: a trusted turn that also discusses an application user ---
    CorpusCase(
        name="human_turn_mentioning_an_application_user",
        category="ambiguous_subject",
        source="user",
        entity_names=("I", "the user", "AuthService"),
        expected=SelfBindOutcome.AMBIGUOUS,
        note=(
            "'I gave the user read access.' Episode-level evidence proves the AUTHOR, not which "
            "node is the author. Collapsing both would fold an RBAC subject into the human."
        ),
    ),
    CorpusCase(
        name="human_turn_with_two_self_aliases",
        category="ambiguous_subject",
        source="user",
        entity_names=("user", "myself"),
        expected=SelfBindOutcome.AMBIGUOUS,
        note="Even two genuine aliases cannot be PROVEN identical without per-node authority.",
    ),
    # --- assistant echo ---
    CorpusCase(
        name="assistant_echo_about_the_user",
        category="assistant_echo",
        source="claude-code",
        entity_names=("user", "Chicago"),
        expected=SelfBindOutcome.NOT_ELIGIBLE,
        note="An assistant restating a user fact must not become the human.",
    ),
    # --- project-scan narrative ---
    CorpusCase(
        name="project_scan_auth_module",
        category="project_scan",
        source="project-scan",
        entity_names=("user", "AuthService"),
        expected=SelfBindOutcome.NOT_ELIGIBLE,
        note="Scan narrative discusses a software `user`; it never speaks as the owner.",
    ),
    CorpusCase(
        name="project_scan_the_user_prose",
        category="project_scan",
        source="project-scan",
        entity_names=("the user", "session token"),
        expected=SelfBindOutcome.NOT_ELIGIBLE,
        note="Doc prose about 'the user' of a system, the highest-risk false positive.",
    ),
    CorpusCase(
        name="document_ingest_manual_page",
        category="project_scan",
        source="document-ingest",
        entity_names=("user", "installation"),
        expected=SelfBindOutcome.NOT_ELIGIBLE,
        note="Imported documentation, same shape as scan narrative.",
    ),
    # --- generic account / RBAC prose ---
    CorpusCase(
        name="rbac_role_description",
        category="generic_account_user",
        source="claude-code",
        entity_names=("user", "admin", "role"),
        expected=SelfBindOutcome.NOT_ELIGIBLE,
        note="RBAC prose: `user` here is a permission tier, not a person.",
    ),
    CorpusCase(
        name="account_record_narrative",
        category="generic_account_user",
        source="claude-code",
        entity_names=("user account", "database"),
        expected=SelfBindOutcome.NOT_ELIGIBLE,
        note="'user account' is not a self alias at all; must not even be a candidate.",
    ),
    CorpusCase(
        name="plural_users_metric",
        category="generic_account_user",
        source="claude-code",
        entity_names=("users", "signup rate"),
        expected=SelfBindOutcome.NOT_ELIGIBLE,
        note="Plural `users` is a population, never the owner.",
    ),
    # --- ungated agent memory that mentions the human ---
    CorpusCase(
        name="agent_inference_about_user",
        category="downgraded_claim",
        source="agent_inference",
        entity_names=("user", "preference"),
        expected=SelfBindOutcome.NOT_ELIGIBLE,
        note=(
            "A `user` claim the admission gate DENIED arrives here already rewritten to "
            "agent_inference. The denial must survive into binding."
        ),
    ),
    CorpusCase(
        name="hook_emitted_event",
        category="untrusted_producer",
        source="hook",
        entity_names=("user", "file"),
        expected=SelfBindOutcome.NOT_ELIGIBLE,
        note="Tooling output, no human author.",
    ),
    # --- retry / repair ---
    CorpusCase(
        name="retry_of_agent_episode",
        category="retry_repair",
        source="claude-code",
        entity_names=("user", "Chicago"),
        expected=SelfBindOutcome.NOT_ELIGIBLE,
        note="A retry re-reads the same persisted source; it can never upgrade evidence.",
    ),
    CorpusCase(
        name="relationless_repair_of_user_turn",
        category="retry_repair",
        source="user",
        entity_names=("user", "Rachel"),
        expected=SelfBindOutcome.BOUND,
        expects_self=True,
        note=(
            "The repair pass re-runs extraction and replaces the payload, so binding must be "
            "applied to the repaired result -- and must still bind, since the source is unchanged."
        ),
    ),
)


CORPUS: tuple[CorpusCase, ...] = _SELF_TURNS + _CONTROLS

#: Cases where a self bind would be a FALSE POSITIVE. Phase 6's gate is zero of these binding.
NEGATIVE_CASES: tuple[CorpusCase, ...] = tuple(c for c in CORPUS if not c.expects_self)
