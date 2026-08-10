"""Unit tests for the feature-dashboard taxonomy (operation -> parent group)."""

from menhir.explorer.feature_taxonomy import (
    PARENTS,
    classify,
    parent_order,
)


def test_known_operations_map_to_declared_parents():
    assert classify("recall_memories", "tool") == "retrieve"
    assert classify("add_memory", "tool") == "write"
    assert classify("query_structure", "tool") == "structure"
    assert classify("resolve_conflict", "tool") == "conflicts"
    assert classify("delete_memory", "tool") == "lifecycle"
    assert classify("rate_recall", "tool") == "stats"


def test_resource_reads_collapse_to_resources_parent():
    assert classify("memory://search/{term}", "resource_template") == "resources"
    assert classify("memory://recent", "resource") == "resources"
    # A memory:// operation still routes to resources even if kind is odd.
    assert classify("memory://node/{node_uuid}", "resource_template") == "resources"


def test_unmapped_operation_falls_back_to_other():
    assert classify("brand_new_tool", "tool") == "other"


def test_parent_order_includes_synthetic_groups_last():
    order = list(parent_order())
    assert order[-2:] == ["resources", "other"]
    for parent in PARENTS:
        assert parent in order


def test_every_registered_tool_is_classified():
    """Regression (SSOT-10): the taxonomy previously omitted 8 registered tools
    (add_candidate, get_provenance, list_clients, mint_client, pause_scheduler,
    resume_scheduler, revoke_client, view_entropy) -- they silently fell back to
    the "other" bucket on the dashboard instead of their real group. Every tool
    menhir.mcp.tools.ALL_TOOLS actually registers must classify() to something
    other than "other"."""
    from menhir.mcp.tools import ALL_TOOLS

    registered_names = {tool_cls.name for tool_cls in ALL_TOOLS}
    unclassified = {name for name in registered_names if classify(name, "tool") == "other"}
    assert not unclassified, f"Registered tools missing from feature_taxonomy: {unclassified}"


def test_no_phantom_tools_in_taxonomy():
    """Regression (SSOT-10): the taxonomy previously listed memory_gateway and
    recover_memory, neither of which menhir.mcp.tools.ALL_TOOLS ever registers.
    Every operation named in PARENTS must correspond to a real registered tool."""
    from menhir.mcp.tools import ALL_TOOLS

    registered_names = {tool_cls.name for tool_cls in ALL_TOOLS}
    taxonomy_ops = {op for ops in PARENTS.values() for op in ops}
    phantom = taxonomy_ops - registered_names
    assert not phantom, f"feature_taxonomy references tools that don't exist: {phantom}"
