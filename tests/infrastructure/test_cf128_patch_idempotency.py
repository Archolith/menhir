"""CF-128: `_patch_graphiti_none_replace` must not accumulate one wrapper per call.

`explorer/extraction_lab.py` constructs one ``GraphitiClient`` per experiment arm, and
``from_settings_with_capabilities`` applies every patch unconditionally. Before the guard,
each ``generate_*_embedding`` method was re-wrapped once per call -- N arms left the
embedding methods N-deep on the hottest path in the system (latency growth with no signal,
``RecursionError`` at ~1,000 arms). These tests assert the wrapper does not grow across five
applications, that all three classes are guarded, that the coercion still WORKS after being
applied twice (the positive control a naive early-return guard would break), and that the
process-global class mutation is restored on teardown.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

pytest.importorskip("graphiti_core")

from graphiti_core.edges import EntityEdge  # noqa: E402
from graphiti_core.nodes import CommunityNode, EntityNode  # noqa: E402

from menhir.infrastructure.graphiti_patches import _patch_graphiti_none_replace  # noqa: E402

#: Each patched class guards on the same distinct flag, set only after the assignment succeeds.
FLAG = "_menhir_none_replace_patched"

#: (class, method) pairs wrapped by the patch, in the same order as the patch applies them.
_PATCHED = [
    (EntityEdge, "generate_embedding"),
    (EntityNode, "generate_name_embedding"),
    (CommunityNode, "generate_name_embedding"),
]


@pytest.fixture(autouse=True)
def _restore_patches():
    """Save every patched method (and its flag) and restore them after each test.

    These patches mutate ``graphiti_core`` classes process-globally, so without this a test
    would leak a patched class into the rest of the suite. The flag may or may not have existed
    before this test ran; restore to exactly the prior state.
    """
    saved = {
        cls: (getattr(cls, method), getattr(cls, FLAG, None), hasattr(cls, FLAG))
        for cls, method in _PATCHED
    }
    try:
        yield
    finally:
        for cls, method in _PATCHED:
            original, original_flag, had_flag = saved[cls]
            setattr(cls, method, original)
            if had_flag:
                setattr(cls, FLAG, original_flag)
            elif hasattr(cls, FLAG):
                delattr(cls, FLAG)


class _StubEmbedder:
    """Minimal ``EmbedderClient`` stand-in that records what it was asked to embed."""

    def __init__(self) -> None:
        self.received: list[list[str]] = []

    async def create(self, input_data: list[str]) -> list[float]:
        self.received.append(input_data)
        return [0.1, 0.2, 0.3]


def _wrapper_depth(func) -> int:
    """Count wrapper layers by walking the closure chain of callable cells.

    The chain terminates at the original (unclosed) method, so the number of wrapper layers is
    the number of callables in the chain minus that terminal base. An unpatched method (no
    closure) therefore has depth 0; one application adds exactly one wrapper (depth 1).
    """
    chain = [func]
    seen: set[int] = set()
    while True:
        current = chain[-1]
        if id(current) in seen:
            break
        seen.add(id(current))
        closure = getattr(current, "__closure__", None)
        if not closure:
            break
        try:
            next_callable = next(
                cell.cell_contents for cell in closure if callable(cell.cell_contents)
            )
        except StopIteration:
            break
        chain.append(next_callable)
    return len(chain) - 1


@pytest.mark.parametrize("cls,method", _PATCHED, ids=lambda v: v.__name__ if hasattr(v, "__name__") else str(v))
def test_wrapper_depth_does_not_grow_across_five_applications(cls, method) -> None:
    _patch_graphiti_none_replace()
    first = getattr(cls, method)
    _patch_graphiti_none_replace()
    _patch_graphiti_none_replace()
    _patch_graphiti_none_replace()
    _patch_graphiti_none_replace()
    fifth = getattr(cls, method)
    # Five applications must leave exactly one wrapper layer (depth 1), and the bound
    # method object must be identical from the first application onward.
    assert _wrapper_depth(fifth) == 1
    assert fifth is first


def test_flag_set_on_each_class_after_patch() -> None:
    _patch_graphiti_none_replace()
    for cls, _ in _PATCHED:
        assert getattr(cls, FLAG, False) is True


@pytest.mark.asyncio
async def test_positive_control_edge_still_coerces_none_fact_after_reapply() -> None:
    _patch_graphiti_none_replace()
    _patch_graphiti_none_replace()

    embedder = _StubEmbedder()
    edge = EntityEdge(
        uuid="cf128-edge",
        group_id="g",
        name="WORKS_ON",
        fact="initial",
        source_node_uuid="src",
        target_node_uuid="dst",
        episodes=[],
        created_at=datetime.now(timezone.utc),
    )
    object.__setattr__(edge, "fact", None)  # simulate the LLM returning null
    await edge.generate_embedding(embedder)

    assert edge.fact == ""  # coerced by the (still installed) wrapper
    assert embedder.received == [[""]]


@pytest.mark.asyncio
async def test_positive_control_nodes_still_coerce_none_name_after_reapply() -> None:
    _patch_graphiti_none_replace()
    _patch_graphiti_none_replace()

    for cls in (EntityNode, CommunityNode):
        embedder = _StubEmbedder()
        node = cls(name="initial", group_id="g", summary="")
        object.__setattr__(node, "name", None)  # simulate the LLM returning null
        await node.generate_name_embedding(embedder)

        assert node.name == ""  # coerced by the (still installed) wrapper
        assert embedder.received == [[""]]
