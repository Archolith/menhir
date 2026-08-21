"""CF-77: two callable seam types declared ten times across eight modules, and one had drifted.

`LlmComplete` and `Embed` describe how a service receives its injected LLM/embedding collaborators.
They were re-declared per module rather than imported, and the copies did not stay identical:

    quantstate_consolidator.py:22   Embed = Callable[[str], list[float]]
    every other declaration         Embed = Callable[[str], list[float] | None]

The drifted copy dropped exactly the case a caller has to handle. Every real embedding seam here
can yield None -- a down endpoint, a circuit-breaker refusal, a short upstream response (CF-156) --
so the one module consuming that alias had a signature promising something the runtime does not.

These tests assert the property, not the absence of a name: a re-introduced local alias that
happened to be spelled correctly would still be a second declaration, and the next drift would be
invisible again.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from menhir.services.seam_types import Embed, LlmComplete

pytestmark = pytest.mark.unit

_CONSUMERS = (
    "correction_resolver", "event_fold", "event_history_perception", "perception",
    "quantstate_consolidator", "typed_scalar_proposer_reviewer", "typed_scalar_rules",
    "verifier_sync",
)


def test_the_embed_alias_admits_none() -> None:
    """THE DRIFT. This is the assertion the diverged copy would have failed."""
    assert "None" in str(Embed)


@pytest.mark.parametrize("module", _CONSUMERS)
def test_every_consumer_binds_the_shared_object(module: str) -> None:
    """Identity, not equality: two separately-declared `Callable[[str], str]` aliases compare equal,
    so equality would pass against exactly the duplication this finding is about."""
    import importlib

    mod = importlib.import_module(f"menhir.services.{module}")
    for name, shared in (("LlmComplete", LlmComplete), ("Embed", Embed)):
        bound = getattr(mod, name, None)
        if bound is not None:
            assert bound is shared, f"{module}.{name} is not the shared alias"


def test_no_module_redeclares_either_alias() -> None:
    """Structural half: a re-introduced local declaration is caught even if it is spelled
    correctly today, because the next edit to it is what drifts."""
    offenders: list[str] = []
    root = pathlib.Path(__file__).resolve().parents[2] / "src" / "menhir"
    for path in root.rglob("*.py"):
        if path.name == "seam_types.py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover
            continue
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id in ("LlmComplete", "Embed"):
                        offenders.append(f"{path.name}:{node.lineno} {t.id}")
    assert offenders == [], f"seam types re-declared instead of imported: {offenders}"


def test_the_shared_module_is_the_only_declaration_site() -> None:
    """POSITIVE CONTROL for the scan above: it must be able to FIND a declaration, or it would
    pass vacuously on a codebase where the aliases had been deleted entirely."""
    root = pathlib.Path(__file__).resolve().parents[2] / "src" / "menhir"
    src = (root / "services" / "seam_types.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    declared = {
        t.id for node in tree.body if isinstance(node, ast.Assign)
        for t in node.targets if isinstance(t, ast.Name)
    }
    assert {"LlmComplete", "Embed"} <= declared
