"""Phase 1 of the canonical-self remediation: the identity SSOT and its evidence contract.

Two properties are load-bearing and each has a test that fails loudly if it regresses:

1. **One formula.** ``self_uuid_for_namespace`` is the only runtime derivation of the canonical
   self UUID, and it stays byte-identical to the contract already written into production data.
   A second copy is how the split identity in the RCA got created.
2. **The name is never authority.** Only trusted, Menhir-owned episode metadata establishes the
   human. Everything else -- including an entity literally named ``user`` -- fails closed.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest

from menhir.domain.namespace import namespace_to_group_id
from menhir.domain.self_identity import (
    GATE_APPROVED_HUMAN_SOURCES,
    SELF_ALIASES,
    SelfEvidenceKind,
    SelfIdentityContext,
    SpeakerRole,
    eligible_self_evidence,
    is_self_alias,
    normalize_logical_namespace,
    self_context_for_pending_episode,
    self_uuid_for_namespace,
)

_SRC = Path(__file__).resolve().parents[1] / "src"


def _ctx(**kw):
    """A context that is self-eligible unless a test makes it otherwise."""
    base = dict(
        namespace="default",
        speaker_role=SpeakerRole.USER,
        evidence_kind=SelfEvidenceKind.TRUSTED_USER_TURN,
        source_kind="mcp_add_memory",
        episode_uuid="ep-1",
    )
    base.update(kw)
    return SelfIdentityContext(**base)


# --------------------------------------------------------------------------- namespace normalization


@pytest.mark.unit
@pytest.mark.parametrize("value", [None, "", "   ", "default"])
def test_blank_and_default_normalize_to_the_default_silo(value):
    assert normalize_logical_namespace(value) == "default"


@pytest.mark.unit
def test_named_namespaces_are_preserved_verbatim():
    assert normalize_logical_namespace("proj-a") == "proj-a"
    assert normalize_logical_namespace("  proj-a  ") == "proj-a"


@pytest.mark.unit
def test_logical_default_is_not_the_physical_group():
    """D2: logical `default` owns identity; physical `""` is a separate mapping.

    Conflating these is the dormant `ensure_self_entity` partition bug -- it writes
    `group_id = $namespace`, so activating it would target group "default" while all
    production data lives in group "".
    """
    assert normalize_logical_namespace(None) == "default"
    assert namespace_to_group_id("default") == ""
    assert namespace_to_group_id(None) == ""
    assert namespace_to_group_id("proj-a") == "proj-a"


# --------------------------------------------------------------------------- the one formula


@pytest.mark.unit
def test_uuid_matches_the_legacy_contract_byte_for_byte():
    """Production data was written under the pre-existing formula. Changing the output would
    orphan every canonical self node that already exists."""
    for ns in ("default", "proj-a", "proj-b"):
        legacy = str(uuid.uuid5(uuid.NAMESPACE_URL, f"menhir-self:{ns}"))
        assert self_uuid_for_namespace(ns) == legacy


@pytest.mark.unit
def test_stable_uuid_vectors():
    """Frozen vectors. If these change, existing self nodes are unreachable."""
    assert self_uuid_for_namespace("default") == "7a5773c8-0d03-565f-a153-61df7316a0d5"
    assert self_uuid_for_namespace("proj-a") == "f3f02eac-d42d-5c6e-b941-bd3656ddc6d3"


@pytest.mark.unit
@pytest.mark.parametrize("value", [None, "", "   ", "default"])
def test_every_blank_spelling_resolves_to_one_default_identity(value):
    assert self_uuid_for_namespace(value) == self_uuid_for_namespace("default")


@pytest.mark.unit
def test_distinct_namespaces_are_isolated():
    seen = {self_uuid_for_namespace(ns) for ns in ("default", "proj-a", "proj-b", "proj-c")}
    assert len(seen) == 4


@pytest.mark.unit
def test_uuid_derivation_performs_no_io():
    """Recall derives the query subject on a hot path and documents itself as doing no DB read.
    A helper that ever grew a lookup would silently break that contract."""
    ctx = _ctx()
    assert ctx.self_uuid == self_uuid_for_namespace("default")
    # Context normalizes its namespace on construction, so the property is pure arithmetic.
    assert SelfIdentityContext(namespace="  proj-a  ").namespace == "proj-a"


# --------------------------------------------------------------------------- no second copy


@pytest.mark.unit
def test_no_other_runtime_module_derives_the_self_uuid():
    """Safety invariant 2. The RCA's split identity exists because two independent creators
    minted self nodes; a second formula reintroduces exactly that.

    Prose mentioning the formula is allowed -- an executable derivation is not.
    """
    derivation = re.compile(r"""uuid5\s*\(\s*[^)]*NAMESPACE_URL[^)]*menhir-self:""", re.S)
    offenders = []
    for path in _SRC.rglob("*.py"):
        if path.name == "self_identity.py":
            continue
        text = path.read_text(encoding="utf-8")
        if "menhir-self:" not in text:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if "menhir-self:" not in line:
                continue
            if line.lstrip().startswith("#") or line.lstrip().startswith("*"):
                continue  # prose
            if derivation.search(line):
                offenders.append(f"{path.relative_to(_SRC)}:{lineno}")
    assert not offenders, (
        "self UUID derived outside domain/self_identity.py: "
        + ", ".join(offenders)
        + " -- call self_uuid_for_namespace() instead"
    )


@pytest.mark.unit
def test_known_readers_and_writer_import_the_ssot():
    """The three verified derivations from the Phase 0 inventory now route through the helper."""
    for rel in ("menhir/services/recall_pipeline.py", "menhir/infrastructure/episode_lifecycle.py"):
        text = (_SRC / rel).read_text(encoding="utf-8")
        assert "from menhir.domain.self_identity import self_uuid_for_namespace" in text, rel
        assert "self_uuid_for_namespace(" in text, rel


# --------------------------------------------------------------------------- evidence contract


@pytest.mark.unit
def test_trusted_user_turn_establishes_the_human():
    assert eligible_self_evidence(_ctx()) is True


@pytest.mark.unit
def test_explicit_self_subject_is_admissible_without_a_known_role():
    """A trusted internal caller vouches for the subject, so UNKNOWN role is not fatal here --
    but a positively non-human role still is (covered below)."""
    ctx = _ctx(
        evidence_kind=SelfEvidenceKind.EXPLICIT_SELF_SUBJECT,
        speaker_role=SpeakerRole.UNKNOWN,
    )
    assert eligible_self_evidence(ctx) is True


@pytest.mark.unit
def test_no_evidence_is_not_evidence():
    assert eligible_self_evidence(_ctx(evidence_kind=None)) is False
    assert eligible_self_evidence(None) is False


@pytest.mark.unit
def test_unknown_role_alone_never_establishes_the_human():
    """TRUSTED_USER_TURN's whole content is the trusted role. Without the role it proves nothing."""
    ctx = _ctx(speaker_role=SpeakerRole.UNKNOWN)
    assert eligible_self_evidence(ctx) is False


@pytest.mark.unit
@pytest.mark.parametrize("role", [SpeakerRole.ASSISTANT, SpeakerRole.TOOL, SpeakerRole.SYSTEM])
@pytest.mark.parametrize("kind", list(SelfEvidenceKind))
def test_non_human_roles_fail_closed_under_every_evidence_kind(role, kind):
    """The assistant self-echo case: an assistant turn asserting a fact about "the user" must
    never bind the human, no matter what evidence a caller attaches."""
    assert eligible_self_evidence(_ctx(speaker_role=role, evidence_kind=kind)) is False


@pytest.mark.unit
@pytest.mark.parametrize(
    "source",
    [
        "project-scan",       # the literal value project_ingest.py writes
        "document-ingest",    # the literal value ingest_document.py writes
        "project_scan",       # punctuation variant must not slip past
        "PROJECT-SCAN",
        "  project-scan  ",
        "structure-scan",
    ],
)
def test_project_scan_narrative_is_never_the_human(source):
    """Scan narrative discusses software users; it never speaks as the owner. Fails closed even
    when a caller wrongly attaches trusted evidence."""
    assert eligible_self_evidence(_ctx(source_kind=source)) is False


@pytest.mark.unit
def test_never_self_source_kinds_match_what_producers_actually_write():
    """The guard was originally written with underscore spellings while production writes
    hyphens, which made it decorative. Pin it to the real strings."""
    scan = (_SRC / "menhir/services/project_ingest.py").read_text(encoding="utf-8")
    doc = (_SRC / "menhir/mcp/tools/ingest/ingest_document.py").read_text(encoding="utf-8")
    assert 'source="project-scan"' in scan
    assert 'source="document-ingest"' in doc
    for literal in ("project-scan", "document-ingest"):
        assert eligible_self_evidence(_ctx(source_kind=literal)) is False


# --------------------------------------------------------------------------- the name is not authority


@pytest.mark.unit
@pytest.mark.parametrize("name", ["user", "User", "  the user  ", "I", "myself"])
def test_recognized_aliases_normalize(name):
    assert is_self_alias(name) is True


@pytest.mark.unit
@pytest.mark.parametrize("name", ["users", "user account", "admin", "", None, "the users"])
def test_non_aliases_are_rejected(name):
    assert is_self_alias(name) is False


@pytest.mark.unit
def test_alias_membership_is_not_evidence():
    """D1, the core rule: an ordinary application actor named `user` stays an ordinary entity.
    `is_self_alias` answers "could this be", never "is this"."""
    ordinary = _ctx(evidence_kind=None, speaker_role=SpeakerRole.UNKNOWN, source_kind="project_scan")
    assert is_self_alias("user") is True
    assert eligible_self_evidence(ordinary) is False


# --------------------------------------------------------------------------- evidence from a pending episode


@pytest.mark.unit
@pytest.mark.parametrize("source", ["user", "manual", "  USER  ", "Manual"])
def test_gate_approved_source_reconstructs_a_trusted_human_turn(source):
    """The persisted source is a gate receipt: `evaluate_user_tier_claim` already required
    Menhir-owned turn evidence with role=user and downgraded anything ungrounded."""
    ctx = self_context_for_pending_episode(source=source, namespace="default", episode_uuid="ep-1")
    assert ctx.speaker_role is SpeakerRole.USER
    assert ctx.evidence_kind is SelfEvidenceKind.TRUSTED_USER_TURN
    assert eligible_self_evidence(ctx) is True


@pytest.mark.unit
@pytest.mark.parametrize(
    "source",
    ["agent_inference", "claude-code", "codex", "opencode", "hook", "project_scan", "", None],
)
def test_every_other_producer_is_not_the_human(source):
    """Agent-authored memories are the bulk of production. None of them may bind the human."""
    ctx = self_context_for_pending_episode(source=source, namespace="default", episode_uuid="ep-1")
    assert ctx.speaker_role is SpeakerRole.UNKNOWN
    assert ctx.evidence_kind is None
    assert eligible_self_evidence(ctx) is False


@pytest.mark.unit
def test_a_downgraded_claim_is_not_the_human():
    """The gate rewrites an ungrounded user claim to agent_inference BEFORE persistence, so the
    downgrade is what reaches this function. An ungrounded claim must not bind."""
    ctx = self_context_for_pending_episode(
        source="agent_inference", namespace="default", episode_uuid="ep-1"
    )
    assert eligible_self_evidence(ctx) is False


@pytest.mark.unit
def test_replay_cannot_strengthen_evidence():
    """Retry/repair/replay re-reads the same persisted source, so it reconstructs identical
    evidence. Evidence never grows on a second pass."""
    first = self_context_for_pending_episode(source="claude-code", namespace="p", episode_uuid="e")
    replayed = self_context_for_pending_episode(source="claude-code", namespace="p", episode_uuid="e")
    assert first == replayed
    assert eligible_self_evidence(replayed) is False


@pytest.mark.unit
def test_pending_episode_has_exactly_one_production_writer():
    """The trust in `GATE_APPROVED_HUMAN_SOURCES` rests entirely on this.

    A persisted source of `user` is only a gate receipt while the single production writer of
    `create_pending_episode` is the gated intake. A second writer persisting a raw caller-supplied
    source would restore name-only authority -- the defect this whole change removes. If this test
    fails, do NOT relax it: re-derive the evidence contract for the new writer first.
    """
    callers = set()
    for path in _SRC.rglob("*.py"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if "create_pending_episode(" not in line or "def create_pending_episode" in line:
                continue
            rel = path.relative_to(_SRC).as_posix()
            # The adapter delegate forwards to the repository; it originates nothing.
            if rel == "menhir/infrastructure/memory_graph_adapter.py":
                continue
            callers.add(rel)
    assert callers == {"menhir/services/ingest_intake.py"}, (
        f"unexpected pending-episode writers: {sorted(callers)}"
    )


@pytest.mark.unit
def test_gate_approved_sources_are_exactly_the_apex_tier():
    """Keep this set aligned with the admission gate's own `("user", "manual")` branch. Adding a
    source here without a corresponding gate grants self-authority to an ungated producer."""
    assert GATE_APPROVED_HUMAN_SOURCES == frozenset({"user", "manual"})
    gate = (_SRC / "menhir/domain/truth/admission_gate.py").read_text(encoding="utf-8")
    assert '("user", "manual")' in gate


# --------------------------------------------------------------------------- propagation seam


@pytest.mark.unit
def test_receipt_carries_identity_from_the_parent_task():
    """Phase 2 seam. The receipt is created in the parent task so both the wait_for child and
    Graphiti's own child task inherit the same object; identity must ride along with it."""
    from menhir.infrastructure.graphiti_extraction_patches import (
        begin_extraction_receipt,
        clear_extraction_receipt,
        get_extraction_receipt,
    )

    try:
        ctx = self_context_for_pending_episode(
            source="user", namespace="default", episode_uuid="ep-1"
        )
        begin_extraction_receipt("ep-1", "body", self_identity=ctx)
        active = get_extraction_receipt()
        assert active is not None
        assert active.self_identity == ctx
        assert eligible_self_evidence(active.self_identity) is True
    finally:
        clear_extraction_receipt()


@pytest.mark.unit
def test_receipt_without_identity_fails_closed():
    """A producer that supplies no evidence must not become the human by omission."""
    from menhir.infrastructure.graphiti_extraction_patches import (
        begin_extraction_receipt,
        clear_extraction_receipt,
    )

    try:
        receipt = begin_extraction_receipt("ep-2", "body")
        assert receipt.self_identity is None
        assert eligible_self_evidence(receipt.self_identity) is False
    finally:
        clear_extraction_receipt()


@pytest.mark.unit
def test_logical_namespace_is_carried_not_inferred_from_group_id():
    """D2: logical `default` maps to physical `""`. The receipt must carry the logical value, so
    the binding seam never has to reverse that mapping -- which is ambiguous."""
    ctx = self_context_for_pending_episode(source="user", namespace="default", episode_uuid="e")
    assert ctx.namespace == "default"
    assert namespace_to_group_id(ctx.namespace) == ""
    named = self_context_for_pending_episode(source="user", namespace="proj-a", episode_uuid="e")
    assert named.self_uuid != ctx.self_uuid


@pytest.mark.unit
def test_alias_set_covers_the_extraction_time_spelling():
    """Extraction rewrites first-person endpoints to `user`; binding must recognize what
    extraction actually emits."""
    assert "user" in SELF_ALIASES and "the user" in SELF_ALIASES
    assert {"i", "me", "my", "mine", "myself"} <= SELF_ALIASES
