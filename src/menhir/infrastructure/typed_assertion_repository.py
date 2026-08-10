"""Public typed-assertion repository composed from focused operation families."""

from menhir.infrastructure.typed_assertion_models import (
    ScalarStateActivationError,
    _RECORD_CYPHER,
)
from menhir.infrastructure.typed_assertion_reconciliation import (
    TypedAssertionReconciliationMixin,
)
from menhir.infrastructure.typed_assertion_repair_repository import TypedAssertionRepairMixin
from menhir.infrastructure.typed_assertion_write_repository import TypedAssertionWriteMixin


class TypedAssertionRepository(
    TypedAssertionWriteMixin, TypedAssertionReconciliationMixin, TypedAssertionRepairMixin
):
    """Neo4j typed-assertion repository composed from focused operation families."""
