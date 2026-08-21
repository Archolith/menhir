"""CF-60: the scoring floor and scale are owned by the domain, not the caller.

``MIN_SIMILARITY_THRESHOLD`` and ``GRAPHITI_RRF_DUAL_METHOD_MAX`` were defined in
``services/scoring_service.py`` while the domain module ``retrieval_tuning`` documented
them (including an explicit cross-layer pointer). The layering was backwards. The fix moves
both definitions into ``domain/retrieval_tuning.py`` and imports them in ``scoring_service``
so every existing consumer keeps resolving them, with no import cycle.
"""

from __future__ import annotations

import importlib
import inspect

import pytest

from menhir.domain import retrieval_tuning

pytestmark = pytest.mark.unit


def test_values_unchanged_from_domain() -> None:
    assert retrieval_tuning.MIN_SIMILARITY_THRESHOLD == 0.15
    assert retrieval_tuning.GRAPHITI_RRF_DUAL_METHOD_MAX == 2.0


def test_one_object_not_two_copies() -> None:
    from menhir.services import scoring_service

    # Same bound object (not two copies). NOTE: `is` on small floats is not a reliable
    # copy-detector on its own (CPython may intern), so also assert the source declares
    # no second assignment.
    assert scoring_service.GRAPHITI_RRF_DUAL_METHOD_MAX is retrieval_tuning.GRAPHITI_RRF_DUAL_METHOD_MAX
    assert scoring_service.MIN_SIMILARITY_THRESHOLD is retrieval_tuning.MIN_SIMILARITY_THRESHOLD

    src = inspect.getsource(scoring_service)
    assert "GRAPHITI_RRF_DUAL_METHOD_MAX =" not in src
    assert "MIN_SIMILARITY_THRESHOLD =" not in src


def test_importers_still_resolve_from_scoring_service() -> None:
    """POSITIVE CONTROL: every listed importer keeps resolving the names."""
    from menhir.services import scoring_service

    assert scoring_service.GRAPHITI_RRF_DUAL_METHOD_MAX == 2.0
    assert scoring_service.MIN_SIMILARITY_THRESHOLD == 0.15

    # Modules importing GRAPHITI_RRF_DUAL_METHOD_MAX from scoring_service.
    for name in ("recall_pipeline", "recall_policies", "recall_service", "recall_support", "hybrid_retrieval"):
        mod = importlib.import_module(f"menhir.services.{name}")
        assert mod.GRAPHITI_RRF_DUAL_METHOD_MAX == 2.0

    # Modules importing MIN_SIMILARITY_THRESHOLD from scoring_service (hybrid_retrieval
    # only imports GRAPHITI_RRF_DUAL_METHOD_MAX, so it is omitted here).
    for name in ("recall_pipeline", "recall_policies", "recall_service", "recall_support"):
        mod = importlib.import_module(f"menhir.services.{name}")
        assert mod.MIN_SIMILARITY_THRESHOLD == 0.15
