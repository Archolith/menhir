"""CF-88: concurrent Extraction Lab runs must not corrupt each other.

The prompt patch is process-global, so the patched window is a critical section.
Two in-process lab requests handled by the same event loop interleave: request A
patches, awaits its LLM call, request B patches over it, and A's arm silently runs
under B's prompt. The fix holds a module-level asyncio.Lock across the ENTIRE
patched window (patch -> awaited extraction -> restore).

This test constructs the interleaving rather than asserting a lock exists. It fakes
the process-global prompt state and the patch/restore cycle, then holds arm A inside
the patched window on a controllable await point while arm B is started, proving B
cannot apply its patch until A has restored and released.
"""

import asyncio
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from menhir.explorer import extraction_lab as lab

pytestmark = pytest.mark.unit

#: Test-local stand-in for graphiti's process-global prompt_library state.
GLOBAL_PROMPT = {"value": "original"}
#: How many times _apply_extraction_patches has run (proves which arm is patched).
APPLY_COUNT = {"value": 0}
#: What each arm observed as the current global value when it applied its patch.
APPLY_SEQ: list[tuple[str, str]] = []


def _fake_apply(variant, known_entities=None, retrieved_context=None):
    captured = GLOBAL_PROMPT["value"]
    GLOBAL_PROMPT["value"] = f"patched:{variant}"
    APPLY_COUNT["value"] += 1
    APPLY_SEQ.append((captured, GLOBAL_PROMPT["value"]))

    def restore():
        GLOBAL_PROMPT["value"] = captured

    return restore


def _make_arm(arm_id: str, variant: str) -> lab.ExtractionLabArm:
    return lab.ExtractionLabArm(
        id=arm_id,
        label=arm_id,
        tuning=lab.ExtractionLabTuning(prompt_variant=variant),
    )


def _make_request(arm: lab.ExtractionLabArm) -> lab.ExtractionLabRequest:
    return lab.ExtractionLabRequest(
        current_message="Rachel moved back to the suburbs.",
        arms=[arm],
    )


def _fake_graphiti_client() -> SimpleNamespace:
    clients = SimpleNamespace(llm_client=SimpleNamespace(temperature=0.0))
    client = SimpleNamespace(clients=clients, close=AsyncMock())
    return SimpleNamespace(client=client)


def _patch_stack(extract_side_effect) -> ExitStack:
    """Stub every external dependency of _run_extraction_arm; the extraction call
    (the awaited work inside the patched window) is the piece the tests control."""
    stack = ExitStack()
    stack.enter_context(
        patch("menhir.config.MemorySettings.from_env", return_value=None)
    )
    stack.enter_context(
        patch(
            "menhir.infrastructure.graphiti_client.GraphitiClient.from_settings",
            return_value=_fake_graphiti_client(),
        )
    )
    stack.enter_context(
        patch(
            "menhir.explorer.extraction_lab._apply_extraction_patches", new=_fake_apply
        )
    )
    stack.enter_context(
        patch(
            "graphiti_core.utils.maintenance.node_operations.extract_nodes",
            new=AsyncMock(side_effect=extract_side_effect),
        )
    )
    stack.enter_context(
        patch(
            "graphiti_core.utils.maintenance.node_operations.resolve_extracted_nodes",
            new=AsyncMock(return_value=([], {}, [])),
        )
    )
    stack.enter_context(
        patch(
            "graphiti_core.utils.maintenance.edge_operations.extract_edges",
            new=AsyncMock(return_value=[]),
        )
    )
    stack.enter_context(
        patch(
            "graphiti_core.utils.maintenance.edge_operations.resolve_extracted_edges",
            new=AsyncMock(return_value=([], [], [])),
        )
    )
    stack.enter_context(
        patch(
            "graphiti_core.utils.bulk_utils.resolve_edge_pointers",
            return_value=[],
        )
    )
    stack.enter_context(
        patch(
            "menhir.explorer.extraction_lab._score_extraction",
            new=AsyncMock(
                return_value=lab.ExtractionGoldScore(
                    mention_recall=0.0,
                    mention_precision=0.0,
                    proposition_recall=0.0,
                    proposition_precision=0.0,
                    update_capture_rate=0.0,
                    unsupported_inference_rate=0.0,
                )
            ),
        )
    )
    return stack


def _reset() -> None:
    GLOBAL_PROMPT["value"] = "original"
    APPLY_COUNT["value"] = 0
    APPLY_SEQ.clear()


async def _immediate(*args, **kwargs):
    return [], {}


class TestConcurrentInterleaving:
    async def test_two_concurrent_arms_never_observe_each_others_prompt(self):
        # The finding: while arm A is inside the patched window, arm B has NOT
        # applied its patch -- the global still holds A's value.
        _reset()
        gate = asyncio.Event()
        arm_a = _make_arm("arm-a", "mention_first")
        arm_b = _make_arm("arm-b", "update_aware")

        async def hold(*args, **kwargs):
            await gate.wait()
            return [], {}

        with _patch_stack(hold):
            task_a = asyncio.create_task(
                lab._run_extraction_arm(arm_a, _make_request(arm_a))
            )
            # A has applied its patch only after acquiring the lock, so once the
            # fake has run, A holds the lock and is awaiting hold() on the gate.
            while APPLY_COUNT["value"] < 1:
                await asyncio.sleep(0)

            task_b = asyncio.create_task(
                lab._run_extraction_arm(arm_b, _make_request(arm_b))
            )
            # Let B run far enough to reach (and block on) the lock.
            for _ in range(50):
                await asyncio.sleep(0)

            # B must not have applied: the global still holds A's value and only A
            # has patched.
            assert GLOBAL_PROMPT["value"] == "patched:mention_first"
            assert APPLY_COUNT["value"] == 1

            gate.set()
            results = await asyncio.gather(task_a, task_b)

        assert results[0].ok is True
        assert results[1].ok is True
        assert GLOBAL_PROMPT["value"] == "original"

    async def test_two_sequential_arms_each_observe_their_own_prompt(self):
        # POSITIVE CONTROL: a fix that deadlocked, or refused the second caller,
        # would pass the concurrent test above -- sequential reuse must still work.
        _reset()
        with _patch_stack(_immediate):
            r1 = await lab._run_extraction_arm(
                _make_arm("arm-1", "mention_first"), _make_request(_make_arm("arm-1", "mention_first"))
            )
            r2 = await lab._run_extraction_arm(
                _make_arm("arm-2", "update_aware"), _make_request(_make_arm("arm-2", "update_aware"))
            )

        assert r1.ok is True
        assert r2.ok is True
        assert GLOBAL_PROMPT["value"] == "original"
        # Each arm observed the ORIGINAL value before applying -- never the other
        # arm's patch -- and each restored it in turn.
        assert APPLY_SEQ == [
            ("original", "patched:mention_first"),
            ("original", "patched:update_aware"),
        ]

    async def test_raising_arm_still_restores_and_releases_the_lock(self):
        # POSITIVE CONTROL: an arm that raises inside the window must restore the
        # global AND release the lock, so a later arm can still proceed.
        _reset()

        async def boom(*args, **kwargs):
            raise RuntimeError("simulated extraction failure")

        with _patch_stack(boom):
            bad = await lab._run_extraction_arm(
                _make_arm("bad", "mention_first"), _make_request(_make_arm("bad", "mention_first"))
            )

        assert bad.ok is False
        assert GLOBAL_PROMPT["value"] == "original"

        with _patch_stack(_immediate):
            third = await lab._run_extraction_arm(
                _make_arm("third", "update_aware"), _make_request(_make_arm("third", "update_aware"))
            )

        assert third.ok is True
        assert GLOBAL_PROMPT["value"] == "original"
