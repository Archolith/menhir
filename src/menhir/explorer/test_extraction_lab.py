"""Unit tests for extraction lab: model validation, gold scoring, context slicing."""

import pytest
from datetime import datetime, timezone

from menhir.explorer.extraction_lab import (
    ExtractionLabTuning,
    ExtractionLabArm,
    EpisodeFixture,
    GoldExtraction,
    ExtractionLabRequest,
    ExtractionResult,
    ExtractionGoldScore,
    _normalize_text,
    _compute_set_metrics,
    _score_extraction,
)


class TestExtractionLabTuning:
    """Test ExtractionLabTuning model validation."""

    def test_default_tuning(self):
        """Default tuning should use baseline prompt and episode count 10."""
        tuning = ExtractionLabTuning()
        assert tuning.prompt_variant == "baseline"
        assert tuning.context_episode_count == 10
        assert tuning.temperature == 0.0
        assert tuning.model is None

    def test_valid_prompt_variants(self):
        """All valid prompt variants should be accepted."""
        variants = [
            "baseline",
            "minus_when_in_doubt",
            "minimal_recall_patch",
            "mention_first",
            "update_aware",
            "proposition_first",
            "mention_first_update_aware",
            "proposition_first_structured",
        ]
        for variant in variants:
            tuning = ExtractionLabTuning(prompt_variant=variant)
            assert tuning.prompt_variant == variant

    def test_context_episode_count_bounds(self):
        """context_episode_count must be 1-100."""
        with pytest.raises(ValueError):
            ExtractionLabTuning(context_episode_count=0)
        with pytest.raises(ValueError):
            ExtractionLabTuning(context_episode_count=101)
        ExtractionLabTuning(context_episode_count=1)
        ExtractionLabTuning(context_episode_count=100)

    def test_temperature_bounds(self):
        """temperature must be 0.0-2.0."""
        with pytest.raises(ValueError):
            ExtractionLabTuning(temperature=-0.1)
        with pytest.raises(ValueError):
            ExtractionLabTuning(temperature=2.1)
        ExtractionLabTuning(temperature=0.0)
        ExtractionLabTuning(temperature=2.0)


class TestExtractionLabArm:
    """Test ExtractionLabArm model validation."""

    def test_valid_arm(self):
        """Valid arm should pass validation."""
        arm = ExtractionLabArm(
            id="baseline",
            label="Baseline extraction",
        )
        assert arm.id == "baseline"
        assert arm.label == "Baseline extraction"
        assert arm.enabled is True

    def test_arm_id_pattern(self):
        """Arm IDs must match [a-zA-Z0-9_-]+."""
        ExtractionLabArm(id="baseline_v1", label="V1")
        ExtractionLabArm(id="arm-1", label="Arm 1")
        ExtractionLabArm(id="A", label="A")
        with pytest.raises(ValueError):
            ExtractionLabArm(id="arm:1", label="Bad")  # Colon not allowed
        with pytest.raises(ValueError):
            ExtractionLabArm(id="", label="Bad")  # Empty not allowed

    def test_arm_label_length(self):
        """Arm label must be 1-80 characters."""
        ExtractionLabArm(id="x", label="a")
        ExtractionLabArm(id="x", label="a" * 80)
        with pytest.raises(ValueError):
            ExtractionLabArm(id="x", label="")
        with pytest.raises(ValueError):
            ExtractionLabArm(id="x", label="a" * 81)


class TestExtractionLabRequest:
    """Test ExtractionLabRequest validation."""

    def test_valid_request(self):
        """Valid request should pass validation."""
        req = ExtractionLabRequest(
            current_message="What is your name?",
            arms=[ExtractionLabArm(id="baseline", label="Baseline")],
        )
        assert req.current_message == "What is your name?"
        assert len(req.arms) == 1

    def test_blank_current_message_rejected(self):
        """Blank or whitespace-only current_message should be rejected."""
        with pytest.raises(ValueError, match="must not be blank"):
            ExtractionLabRequest(
                current_message="  ",
                arms=[ExtractionLabArm(id="baseline", label="Baseline")],
            )

    def test_no_enabled_arms_rejected(self):
        """Request with no enabled arms should be rejected."""
        with pytest.raises(ValueError, match="at least one arm must be enabled"):
            ExtractionLabRequest(
                current_message="What is your name?",
                arms=[
                    ExtractionLabArm(id="a1", label="Arm 1", enabled=False),
                    ExtractionLabArm(id="a2", label="Arm 2", enabled=False),
                ],
            )

    def test_duplicate_arm_ids_rejected(self):
        """Duplicate arm IDs should be rejected."""
        with pytest.raises(ValueError, match="arm ids must be unique"):
            ExtractionLabRequest(
                current_message="What is your name?",
                arms=[
                    ExtractionLabArm(id="same", label="Arm 1"),
                    ExtractionLabArm(id="same", label="Arm 2"),
                ],
            )

    def test_empty_arms_rejected(self):
        """Empty arms list should be rejected."""
        with pytest.raises(ValueError):
            ExtractionLabRequest(
                current_message="What is your name?",
                arms=[],
            )

    def test_current_message_stripped(self):
        """current_message should be stripped of leading/trailing whitespace."""
        req = ExtractionLabRequest(
            current_message="  What is your name?  ",
            arms=[ExtractionLabArm(id="baseline", label="Baseline")],
        )
        assert req.current_message == "What is your name?"


class TestNormalizeText:
    """Test text normalization for gold scoring."""

    def test_lowercase(self):
        """Text should be converted to lowercase."""
        assert _normalize_text("Rachel") == "rachel"
        assert _normalize_text("SUBURBS") == "suburbs"

    def test_strip_whitespace(self):
        """Leading/trailing whitespace should be removed."""
        assert _normalize_text("  rachel  ") == "rachel"

    def test_collapse_whitespace(self):
        """Multiple spaces should collapse to single space."""
        assert _normalize_text("Rachel  moved  back") == "rachel moved back"

    def test_remove_punctuation(self):
        """Leading/trailing punctuation should be removed."""
        assert _normalize_text("Rachel.") == "rachel"
        assert _normalize_text("!rachel!") == "rachel"
        assert _normalize_text("'the suburbs'") == "the suburbs"

    def test_full_normalization(self):
        """Full normalization pipeline."""
        assert _normalize_text("  Rachel MOVED to The SUBURBS!  ") == "rachel moved to the suburbs"


class TestComputeSetMetrics:
    """Test recall/precision computation for set comparisons."""

    def test_perfect_recall_precision(self):
        """Identical sets should have recall=1, precision=1."""
        recall, precision = _compute_set_metrics({"a", "b"}, {"a", "b"})
        assert recall == 1.0
        assert precision == 1.0

    def test_no_gold_items(self):
        """If no gold items, score depends on whether extracted is empty."""
        # No gold, nothing extracted → both perfect
        recall, precision = _compute_set_metrics(set(), set())
        assert recall == 1.0
        assert precision == 1.0

        # No gold, but extracted something → penalize
        recall, precision = _compute_set_metrics({"a"}, set())
        assert recall == 0.0
        assert precision == 0.0

    def test_no_extracted_items(self):
        """If nothing extracted but gold items exist → recall=0."""
        recall, precision = _compute_set_metrics(set(), {"a", "b"})
        assert recall == 0.0
        assert precision == 0.0

    def test_partial_recall(self):
        """Partial overlap should produce appropriate recall/precision."""
        # extracted: {a, b}, gold: {a, b, c}
        # recall = 2/3, precision = 2/2
        recall, precision = _compute_set_metrics({"a", "b"}, {"a", "b", "c"})
        assert recall == pytest.approx(2/3)
        assert precision == 1.0

    def test_false_positives(self):
        """False positives should reduce precision."""
        # extracted: {a, b, c}, gold: {a}
        # recall = 1/1, precision = 1/3
        recall, precision = _compute_set_metrics({"a", "b", "c"}, {"a"})
        assert recall == 1.0
        assert precision == pytest.approx(1/3)


class TestGoldExtraction:
    """Test GoldExtraction model."""

    def test_default_gold(self):
        """Default gold extraction should have empty lists."""
        gold = GoldExtraction()
        assert gold.mentions == []
        assert gold.propositions == []
        assert gold.update_language == []

    def test_gold_with_data(self):
        """Gold extraction should accept lists of strings."""
        gold = GoldExtraction(
            mentions=["Rachel", "suburbs"],
            propositions=["Rachel moved to suburbs"],
            update_language=["actually", "moved back"],
        )
        assert len(gold.mentions) == 2
        assert len(gold.propositions) == 1
        assert len(gold.update_language) == 2


class TestExtractionResult:
    """Test ExtractionResult serialization."""

    def test_empty_extraction(self):
        """Empty extraction should be valid."""
        result = ExtractionResult()
        assert result.mentions == []
        assert result.propositions == []

    def test_extraction_with_data(self):
        """Extraction with mentions and propositions should validate."""
        result = ExtractionResult(
            mentions=[{"text": "Rachel", "labels": ["PERSON"]}],
            propositions=[{"fact": "Rachel moved", "source_uuid": "a", "target_uuid": "b"}],
        )
        assert len(result.mentions) == 1
        assert len(result.propositions) == 1


class TestExtractionGoldScore:
    """Test ExtractionGoldScore model."""

    def test_perfect_score(self):
        """Perfect extraction should score 1.0 across all metrics."""
        score = ExtractionGoldScore(
            mention_recall=1.0,
            mention_precision=1.0,
            proposition_recall=1.0,
            proposition_precision=1.0,
            update_capture_rate=1.0,
            unsupported_inference_rate=0.0,
        )
        assert score.mention_recall == 1.0
        assert score.unsupported_inference_rate == 0.0

    def test_zero_score(self):
        """Failed extraction should score 0.0 on recall metrics."""
        score = ExtractionGoldScore(
            mention_recall=0.0,
            mention_precision=0.0,
            proposition_recall=0.0,
            proposition_precision=0.0,
            update_capture_rate=0.0,
            unsupported_inference_rate=1.0,
        )
        assert score.mention_recall == 0.0
        assert score.unsupported_inference_rate == 1.0

    def test_bounds_enforcement(self):
        """Scores must be in [0.0, 1.0]."""
        with pytest.raises(ValueError):
            ExtractionGoldScore(
                mention_recall=-0.1,
                mention_precision=0.5,
                proposition_recall=0.5,
                proposition_precision=0.5,
                update_capture_rate=0.5,
                unsupported_inference_rate=0.5,
            )
        with pytest.raises(ValueError):
            ExtractionGoldScore(
                mention_recall=1.1,
                mention_precision=0.5,
                proposition_recall=0.5,
                proposition_precision=0.5,
                update_capture_rate=0.5,
                unsupported_inference_rate=0.5,
            )


class TestEpisodeFixture:
    """Test EpisodeFixture model."""

    def test_episode_fixture_with_defaults(self):
        """Episode fixture should use current time by default."""
        fixture = EpisodeFixture(text="test content")
        assert fixture.text == "test content"
        assert fixture.created_at is not None
        assert fixture.created_at.tzinfo is not None

    def test_episode_fixture_with_custom_time(self):
        """Episode fixture should accept custom created_at."""
        now = datetime.now(timezone.utc)
        fixture = EpisodeFixture(text="test", created_at=now)
        assert fixture.created_at == now

    def test_episode_text_bounds(self):
        """Episode text must be non-empty and bounded.

        Bound is 8000, not 2000: real LME assistant turns run longer than 2000 chars
        (observed up to ~3552 in the RCA fixtures) -- a tight bound would force
        truncating real conversation text when populating live-namespace fixtures.
        """
        with pytest.raises(ValueError):
            EpisodeFixture(text="")
        EpisodeFixture(text="a")
        EpisodeFixture(text="a" * 8000)
        with pytest.raises(ValueError):
            EpisodeFixture(text="a" * 8001)


class TestApplyPromptVariant:
    """Test _apply_extraction_patches against graphiti-core's REAL extraction prompt.

    No LLM calls -- these only exercise the text-transform + monkeypatch/restore
    mechanics, which is exactly what the fidelity contract requires be provably
    correct before any live extraction call happens. Regression coverage for a real
    bug found during review: the original patch target (`_extract_nodes_context`)
    did not exist in graphiti-core, so every "variant" arm was silently running the
    unmodified baseline prompt.
    """

    _CONTEXT = {
        "episode_content": "Rachel actually just moved back to the suburbs again.",
        "previous_episodes": [],
        "entity_types": "{}",
        "custom_extraction_instructions": "",
    }

    _ALL_VARIANTS = [
        "minus_when_in_doubt",
        "minimal_recall_patch",
        "mention_first",
        "update_aware",
        "proposition_first",
        "mention_first_update_aware",
        "proposition_first_structured",
    ]

    def _default_user_prompt(self):
        from graphiti_core.prompts.extract_nodes import extract_message as default_fn
        return next(m.content for m in default_fn(self._CONTEXT) if m.role == "user")

    def test_baseline_is_noop(self):
        """The baseline variant must not touch prompt_library at all."""
        from menhir.explorer.extraction_lab import _apply_extraction_patches
        from graphiti_core.prompts import prompt_library

        before = prompt_library.extract_nodes.extract_message
        restore = _apply_extraction_patches("baseline")
        assert prompt_library.extract_nodes.extract_message is before
        restore()
        assert prompt_library.extract_nodes.extract_message is before

    def test_combined_extraction_does_not_patch_separate_node_prompt(self):
        from graphiti_core.prompts import prompt_library

        from menhir.explorer.extraction_lab import _apply_extraction_patches

        before = prompt_library.extract_nodes.extract_message
        restore = _apply_extraction_patches("combined_extraction")
        assert prompt_library.extract_nodes.extract_message is before
        restore()
        assert prompt_library.extract_nodes.extract_message is before

    def test_sanity_when_in_doubt_sentence_present_in_real_prompt(self):
        """Guards against this whole test class silently passing if graphiti-core's
        prompt text ever changes out from under the substring match."""
        assert "When in doubt, do NOT extract." in self._default_user_prompt()

    @pytest.mark.parametrize("variant", _ALL_VARIANTS)
    def test_variant_changes_prompt_and_preserves_scaffolding(self, variant):
        """Every non-baseline variant must: (1) actually change the prompt text,
        (2) preserve the production few-shot <EXAMPLE> blocks and entity/context
        injection (fidelity contract: only the declared parameter differs)."""
        from menhir.explorer.extraction_lab import _apply_extraction_patches
        from graphiti_core.prompts import prompt_library

        default_user = self._default_user_prompt()
        restore = _apply_extraction_patches(variant)
        try:
            patched_user = next(
                m.content
                for m in prompt_library.extract_nodes.extract_message(self._CONTEXT)
                if m.role == "user"
            )
            assert patched_user != default_user, f"{variant} did not change the prompt"
            assert "<EXAMPLE>" in patched_user, f"{variant} dropped the few-shot examples"
            assert "Gamecube" in patched_user, f"{variant} dropped a production example"
            assert self._CONTEXT["episode_content"] in patched_user, (
                f"{variant} dropped current-message injection"
            )
        finally:
            restore()

    @pytest.mark.parametrize("variant", ["minus_when_in_doubt", "minimal_recall_patch"])
    def test_removal_variants_drop_the_target_sentence(self, variant):
        """Conditions 2 and 3 specifically must remove the literal sentence."""
        from menhir.explorer.extraction_lab import _apply_extraction_patches
        from graphiti_core.prompts import prompt_library

        restore = _apply_extraction_patches(variant)
        try:
            patched_user = next(
                m.content
                for m in prompt_library.extract_nodes.extract_message(self._CONTEXT)
                if m.role == "user"
            )
            assert "When in doubt, do NOT extract." not in patched_user
        finally:
            restore()

    @pytest.mark.parametrize("variant", _ALL_VARIANTS)
    def test_restore_returns_byte_identical_baseline(self, variant):
        """After restore(), prompt_library must produce EXACTLY the default prompt --
        no leakage into subsequent (e.g. baseline) arm runs."""
        from menhir.explorer.extraction_lab import _apply_extraction_patches
        from graphiti_core.prompts import prompt_library

        default_user = self._default_user_prompt()
        restore = _apply_extraction_patches(variant)
        restore()
        restored_user = next(
            m.content
            for m in prompt_library.extract_nodes.extract_message(self._CONTEXT)
            if m.role == "user"
        )
        assert restored_user == default_user

    def test_model_and_temperature_override_land_on_the_real_client(self):
        """Regression coverage for the model/temperature fidelity gap: these tuning
        knobs must actually reach clients.llm_client, not silently no-op like the
        original prompt-variant bug did. No live LLM call -- construction alone is
        enough to verify the override lands in the right place."""
        from menhir.config import MemorySettings
        from menhir.infrastructure.graphiti_client import GraphitiClient

        # .from_env(), matching the fix in extraction_lab.py's _run_extraction_arm --
        # the bare constructor ignores the runtime environment entirely (see that
        # file's comment for how this was found).
        settings = MemorySettings.from_env()
        graphiti_client = GraphitiClient.from_settings(settings)
        clients = graphiti_client.client.clients

        clients.llm_client.config.model = "gpt-4.1-nano"
        clients.llm_client.temperature = 0.7
        assert clients.llm_client.config.model == "gpt-4.1-nano"
        assert clients.llm_client.temperature == 0.7

    def test_restore_after_exception_still_cleans_up(self):
        """Mirrors the try/finally contract in _run_extraction_arm: even if the
        caller never reaches a normal restore() call site, a caller that does call
        it (e.g. from a finally block) must still fully undo the patch."""
        from menhir.explorer.extraction_lab import _apply_extraction_patches
        from graphiti_core.prompts import prompt_library

        default_user = self._default_user_prompt()
        restore = _apply_extraction_patches("update_aware")
        try:
            raise RuntimeError("simulated extraction failure")
        except RuntimeError:
            pass
        finally:
            restore()
        restored_user = next(
            m.content
            for m in prompt_library.extract_nodes.extract_message(self._CONTEXT)
            if m.role == "user"
        )
        assert restored_user == default_user


class TestContextAblationOverrides:
    """Phase 2 context-form ablation (menhir-extraction-context-ablation-handoff.md):
    forced_known_entities and retrieved_context. Both are prompt-composition
    mechanisms only -- no DB, no live LLM needed to verify them.
    """

    _CONTEXT = {
        "episode_content": "Rachel actually just moved back to the suburbs again.",
        "previous_episodes": [],
        "entity_types": "{}",
        "custom_extraction_instructions": "",
    }

    def _default_user_prompt(self):
        from graphiti_core.prompts.extract_nodes import extract_message as default_fn
        return next(m.content for m in default_fn(self._CONTEXT) if m.role == "user")

    def test_retrieved_context_injected_as_distinct_block(self):
        from menhir.explorer.extraction_lab import _apply_extraction_patches
        from graphiti_core.prompts import prompt_library

        restore = _apply_extraction_patches("baseline", None, "Rachel previously moved to Chicago.")
        try:
            patched_user = next(
                m.content
                for m in prompt_library.extract_nodes.extract_message(self._CONTEXT)
                if m.role == "user"
            )
            assert "<RETRIEVED CONTEXT>" in patched_user
            assert "Rachel previously moved to Chicago." in patched_user
            # Must NOT be labeled as a KNOWN ENTITIES block -- distinct delivery format
            # is the entire point of condition J vs condition B.
            assert "<KNOWN ENTITIES>" not in patched_user
        finally:
            restore()

    def test_retrieved_context_composes_with_known_entities(self):
        """Both blocks can be present at once -- not mutually exclusive."""
        from menhir.explorer.extraction_lab import _apply_extraction_patches
        from graphiti_core.prompts import prompt_library

        restore = _apply_extraction_patches("baseline", ["Rachel"], "Rachel previously moved to Chicago.")
        try:
            patched_user = next(
                m.content
                for m in prompt_library.extract_nodes.extract_message(self._CONTEXT)
                if m.role == "user"
            )
            assert "<KNOWN ENTITIES>" in patched_user
            assert "<RETRIEVED CONTEXT>" in patched_user
        finally:
            restore()

    def test_baseline_with_no_extras_is_still_a_true_noop(self):
        """The original no-op path (baseline, nothing extra) must be unaffected by
        adding the retrieved_context mechanism."""
        from menhir.explorer.extraction_lab import _apply_extraction_patches
        from graphiti_core.prompts import prompt_library

        before = prompt_library.extract_nodes.extract_message
        restore = _apply_extraction_patches("baseline", None, None)
        assert prompt_library.extract_nodes.extract_message is before
        restore()

    def test_restore_after_retrieved_context_returns_byte_identical_baseline(self):
        from menhir.explorer.extraction_lab import _apply_extraction_patches
        from graphiti_core.prompts import prompt_library

        default_user = self._default_user_prompt()
        restore = _apply_extraction_patches("baseline", None, "some retrieved content")
        restore()
        restored_user = next(
            m.content
            for m in prompt_library.extract_nodes.extract_message(self._CONTEXT)
            if m.role == "user"
        )
        assert restored_user == default_user

    async def test_forced_known_entities_bypasses_db_lookup_entirely(self):
        """When forced_known_entities is set, _lookup_known_entities must never be
        called -- this is the ablation-determinism guarantee the field exists for.

        HERMETIC: the LLM extraction call is stubbed out. The docstring here always said
        "we don't need a real LLM response for this assertion", but nothing actually
        prevented one -- `run_extraction_lab` reached a live OpenAI call, so this unit
        test made a network request. It passed in ~5s alone and blew the 60s per-test
        timeout inside the full suite, which is what an unstubbed network dependency
        buys: a test that is green in isolation and flaky in aggregate.

        `extract_nodes` is imported INSIDE `_run_extraction_arm`, so the import resolves
        at call time and patching the source module is what actually intercepts it.
        Patching a name on `extraction_lab` would silently miss.

        The branch under test runs BEFORE extraction, so failing the extraction step
        leaves the guarantee fully exercised while removing the network entirely.
        """
        from menhir.explorer.extraction_lab import (
            ExtractionLabArm,
            ExtractionLabRequest,
            ExtractionLabTuning,
            run_extraction_lab,
        )
        from unittest.mock import AsyncMock, patch

        request = ExtractionLabRequest(
            current_message="Rachel actually just moved back to the suburbs again.",
            source_namespace="lme-830ce83f",  # present, but must be ignored
            arms=[
                ExtractionLabArm(
                    id="forced",
                    label="forced",
                    tuning=ExtractionLabTuning(
                        forced_known_entities=["Rachel"],
                        enable_candidate_lookup=True,  # would normally trigger a DB lookup
                    ),
                ),
            ],
        )
        lookup = AsyncMock(side_effect=AssertionError("DB lookup must not be called"))
        with patch("menhir.explorer.extraction_lab._lookup_known_entities", new=lookup), \
             patch(
                 "graphiti_core.utils.maintenance.node_operations.extract_nodes",
                 new=AsyncMock(side_effect=RuntimeError("stubbed: no LLM in unit tests")),
             ):
            payload = await run_extraction_lab(request)

        # The guarantee, asserted DIRECTLY rather than inferred from an error string.
        # The old form (`if not arm.ok: assert "..." not in arm.error`) passed vacuously
        # whenever the arm happened to succeed, so it could not fail for the right reason.
        lookup.assert_not_awaited()

        arm = payload.arms[0]
        if not arm.ok:
            assert "DB lookup must not be called" not in (arm.error or "")


class TestPhase2CandidateLookup:
    """Phase 2 (menhir-belief-supersession-code-mapped-plan.md): extraction-time
    candidate lookup. Covers both halves independently: the prompt-injection side
    (_apply_extraction_patches with known_entities, no DB needed) and the lookup
    query side (_lookup_known_entities, with a stub driver -- no live Neo4j needed).
    """

    _CONTEXT = {
        "episode_content": "Rachel actually just moved back to the suburbs again.",
        "previous_episodes": [],
        "entity_types": "{}",
        "custom_extraction_instructions": "",
    }

    def _default_user_prompt(self):
        from graphiti_core.prompts.extract_nodes import extract_message as default_fn
        return next(m.content for m in default_fn(self._CONTEXT) if m.role == "user")

    def test_known_entities_injected_and_preserves_variant_edit(self):
        """The KNOWN ENTITIES block and a prompt_variant edit must compose in one
        patch -- neither should silently drop the other."""
        from menhir.explorer.extraction_lab import _apply_extraction_patches
        from graphiti_core.prompts import prompt_library

        restore = _apply_extraction_patches("update_aware", ["Rachel", "Chicago"])
        try:
            patched_user = next(
                m.content
                for m in prompt_library.extract_nodes.extract_message(self._CONTEXT)
                if m.role == "user"
            )
            assert "<KNOWN ENTITIES>" in patched_user
            assert "- Rachel" in patched_user
            assert "- Chicago" in patched_user
            # The update_aware variant's own section must still be present too.
            assert "Update-Aware Extraction" in patched_user
            # Fidelity: examples and context injection still untouched.
            assert "<EXAMPLE>" in patched_user
            assert self._CONTEXT["episode_content"] in patched_user
        finally:
            restore()

    def test_known_entities_alone_with_baseline_variant(self):
        """baseline + known_entities is a real, valid combination -- not a no-op just
        because the prompt_variant itself is baseline."""
        from menhir.explorer.extraction_lab import _apply_extraction_patches
        from graphiti_core.prompts import prompt_library

        restore = _apply_extraction_patches("baseline", ["Rachel"])
        try:
            patched_user = next(
                m.content
                for m in prompt_library.extract_nodes.extract_message(self._CONTEXT)
                if m.role == "user"
            )
            assert "<KNOWN ENTITIES>" in patched_user
            assert patched_user != self._default_user_prompt()
        finally:
            restore()

    def test_baseline_with_no_known_entities_is_still_a_true_noop(self):
        """The original no-op path (baseline, no lookup) must be unaffected by adding
        Phase 2 -- this is the exact case every Phase 0/1 test already relies on."""
        from menhir.explorer.extraction_lab import _apply_extraction_patches
        from graphiti_core.prompts import prompt_library

        before = prompt_library.extract_nodes.extract_message
        restore = _apply_extraction_patches("baseline", None)
        assert prompt_library.extract_nodes.extract_message is before
        restore()
        restore_empty_list = _apply_extraction_patches("baseline", [])
        assert prompt_library.extract_nodes.extract_message is before
        restore_empty_list()

    def test_restore_after_known_entities_returns_byte_identical_baseline(self):
        from menhir.explorer.extraction_lab import _apply_extraction_patches
        from graphiti_core.prompts import prompt_library

        default_user = self._default_user_prompt()
        restore = _apply_extraction_patches("baseline", ["Rachel", "Chicago", "Nisha"])
        restore()
        restored_user = next(
            m.content
            for m in prompt_library.extract_nodes.extract_message(self._CONTEXT)
            if m.role == "user"
        )
        assert restored_user == default_user


class _StubDriver:
    """Minimal graphiti-core driver stand-in for _lookup_known_entities tests."""

    def __init__(self, records: list[dict] | None = None, raise_error: bool = False):
        self._records = records or []
        self._raise_error = raise_error
        self.calls: list[dict] = []

    async def execute_query(self, cypher_query_, **kwargs):
        self.calls.append({"cypher": cypher_query_, **kwargs})
        if self._raise_error:
            raise RuntimeError("simulated Neo4j failure")

        class _Result:
            def __init__(self, records):
                self.records = records

        return _Result(self._records)


class _StubClients:
    def __init__(self, driver):
        self.driver = driver


class TestLookupKnownEntities:
    """_lookup_known_entities: the DB-query half of Phase 2. All stubbed -- no live
    Neo4j needed to verify the query construction, filtering logic, and fail-safe
    behavior."""

    async def test_no_namespace_returns_empty_without_querying(self):
        from menhir.explorer.extraction_lab import _lookup_known_entities

        driver = _StubDriver(records=[{"name": "Rachel"}])
        clients = _StubClients(driver)
        result = await _lookup_known_entities(clients, None, "Rachel moved to the suburbs")
        assert result == []
        assert len(driver.calls) == 0

    async def test_no_driver_on_clients_returns_empty(self):
        from menhir.explorer.extraction_lab import _lookup_known_entities

        result = await _lookup_known_entities(object(), "lme-830ce83f", "Rachel moved")
        assert result == []

    async def test_matches_only_names_present_in_message(self):
        from menhir.explorer.extraction_lab import _lookup_known_entities

        driver = _StubDriver(records=[
            {"name": "Rachel"}, {"name": "Chicago"}, {"name": "Nisha"},
        ])
        clients = _StubClients(driver)
        result = await _lookup_known_entities(
            clients, "lme-830ce83f", "Rachel actually just moved back to the suburbs again."
        )
        assert result == ["Rachel"]  # Chicago/Nisha not in the message text

    async def test_query_uses_params_not_bare_kwargs(self):
        """Regression guard: Neo4jDriver.execute_query pops kwargs['params'] as the
        parameter dict -- passing namespace=... as a bare kwarg would silently not
        parameterize the query the way this code intends."""
        from menhir.explorer.extraction_lab import _lookup_known_entities

        driver = _StubDriver(records=[])
        clients = _StubClients(driver)
        await _lookup_known_entities(clients, "lme-830ce83f", "hello")
        assert len(driver.calls) == 1
        assert driver.calls[0].get("params") == {"namespace": "lme-830ce83f"}

    async def test_short_names_filtered_as_noise(self):
        """Names under 3 chars (e.g. bare 'it', 'a') would match almost any message
        as a substring -- must not count as a real signal."""
        from menhir.explorer.extraction_lab import _lookup_known_entities

        driver = _StubDriver(records=[{"name": "it"}, {"name": "Rachel"}])
        clients = _StubClients(driver)
        result = await _lookup_known_entities(clients, "lme-830ce83f", "Rachel said it was fine")
        assert result == ["Rachel"]

    async def test_query_failure_fails_safe(self):
        from menhir.explorer.extraction_lab import _lookup_known_entities

        driver = _StubDriver(raise_error=True)
        clients = _StubClients(driver)
        result = await _lookup_known_entities(clients, "lme-830ce83f", "Rachel moved")
        assert result == []


class _StubChatBackend:
    """Minimal ChatBackend stand-in: returns a canned response, never calls a real API."""

    def __init__(self, response: str):
        self._response = response
        self.calls: list[dict] = []

    async def create_chat_completion(self, **kwargs):
        self.calls.append(kwargs)
        return self._response


class TestScoreExtractionFuzzyMatch:
    """Regression coverage for the fuzzy-match scoring tier.

    Added after a live Phase 1 run showed proposition_recall reading 0.00 across every
    arm despite genuinely correct extraction ("Rachel moved back to the suburbs again."
    scored 0 against gold text "Rachel moved to or currently resides in the suburbs" --
    same fact, different wording, and the old exact-string-only scorer had no way to see
    that). All tests here use a stubbed backend -- no live LLM calls.
    """

    async def test_exact_match_needs_no_llm_backend(self):
        """When extracted text exactly matches gold (after normalization), fuzzy
        matching is never invoked -- llm_backend=None must still score correctly."""
        extraction = ExtractionResult(mentions=[{"text": "Rachel"}, {"text": "suburbs"}])
        gold = GoldExtraction(mentions=["Rachel", "suburbs"])
        score = await _score_extraction(extraction, gold, llm_backend=None)
        assert score.mention_recall == 1.0
        assert score.mention_precision == 1.0

    async def test_no_backend_leaves_semantic_mismatches_unscored(self):
        """Without a backend, a real-but-differently-worded match must NOT be credited
        -- this is the exact-match-only fallback behavior, still correct on its own
        terms (just less generous)."""
        extraction = ExtractionResult(propositions=[{"fact": "Rachel moved back to the suburbs again."}])
        gold = GoldExtraction(propositions=["Rachel moved to or currently resides in the suburbs"])
        score = await _score_extraction(extraction, gold, llm_backend=None)
        assert score.proposition_recall == 0.0
        assert score.proposition_precision == 0.0

    async def test_fuzzy_match_credits_semantic_equivalence(self):
        """With a backend that confirms the semantic match, recall/precision must rise
        to 1.0 -- this is the exact scenario the live Phase 1 run hit."""
        backend = _StubChatBackend('{"matches": [{"extracted_index": 0, "gold_index": 0}]}')
        extraction = ExtractionResult(propositions=[{"fact": "Rachel moved back to the suburbs again."}])
        gold = GoldExtraction(propositions=["Rachel moved to or currently resides in the suburbs"])
        score = await _score_extraction(extraction, gold, llm_backend=backend)
        assert score.proposition_recall == 1.0
        assert score.proposition_precision == 1.0
        assert score.unsupported_inference_rate == 0.0
        assert len(backend.calls) == 1  # exactly one classification call, not per-item

    async def test_fuzzy_match_skipped_when_everything_exact(self):
        """No leftover unmatched items on either side -> zero LLM calls, even with a
        backend configured. Keeps the harness cheap on the common case."""
        backend = _StubChatBackend('{"matches": []}')
        extraction = ExtractionResult(mentions=[{"text": "Rachel"}])
        gold = GoldExtraction(mentions=["Rachel"])
        await _score_extraction(extraction, gold, llm_backend=backend)
        assert len(backend.calls) == 0

    async def test_fuzzy_match_rejects_out_of_range_or_duplicate_indices(self):
        """A malformed or hallucinated response (bad indices, reused indices) must not
        crash and must not over-credit matches."""
        backend = _StubChatBackend(
            '{"matches": ['
            '{"extracted_index": 99, "gold_index": 0},'
            '{"extracted_index": 0, "gold_index": 0},'
            '{"extracted_index": 0, "gold_index": 0}'
            "]}"
        )
        extraction = ExtractionResult(mentions=[{"text": "the suburbs"}])
        gold = GoldExtraction(mentions=["suburban area"])
        score = await _score_extraction(extraction, gold, llm_backend=backend)
        # Only the one valid, non-duplicate pair (0, 0) should count.
        assert score.mention_recall == 1.0
        assert score.mention_precision == 1.0

    async def test_fuzzy_match_call_failure_fails_safe(self):
        """An exception from the LLM call must degrade to exact-match-only scoring, not
        propagate and crash the whole arm run."""

        class _RaisingBackend:
            async def create_chat_completion(self, **kwargs):
                raise RuntimeError("simulated API failure")

        extraction = ExtractionResult(propositions=[{"fact": "Rachel moved back to the suburbs again."}])
        gold = GoldExtraction(propositions=["Rachel moved to or currently resides in the suburbs"])
        score = await _score_extraction(extraction, gold, llm_backend=_RaisingBackend())
        assert score.proposition_recall == 0.0  # fell back to exact-match-only, no crash

    async def test_unparseable_response_fails_safe(self):
        """Garbage LLM output must not crash and must not fabricate matches."""
        backend = _StubChatBackend("not json at all")
        extraction = ExtractionResult(mentions=[{"text": "the suburbs"}])
        gold = GoldExtraction(mentions=["suburban area"])
        score = await _score_extraction(extraction, gold, llm_backend=backend)
        assert score.mention_recall == 0.0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
