"""Saga startup pieces not pinned to ``menhir.core.runtime``'s module namespace.

Moved verbatim from ``runtime.py``: the shared dispatcher wiring, the backlog observation
helper, the refusal exception, the reconciliation-gate timing constants, and the live-mode
arming check. The functions whose bodies the test suite monkeypatches through
``menhir.core.runtime`` (``_recover_saga_backlog``, the observe/recovery pair, the admission
latch) stay defined there and import these names, so ``setattr`` on the facade module keeps
working.
"""

from __future__ import annotations

import logging
from typing import Any

from menhir.config import MemorySettings

logger = logging.getLogger(__name__)


def _build_saga_dispatcher(adapter: object) -> Any:
    """The shared dispatcher wiring, re-exported so startup and the CLI cannot diverge."""
    from menhir.services.saga_preflight import build_default_dispatcher

    return build_default_dispatcher(adapter)


def _observe_saga_backlog(adapter: object) -> object:
    """Classify the PREPARED backlog without mutating anything."""
    return _build_saga_dispatcher(adapter).observe()


class SagaRecoveryNotWriteReady(RuntimeError):
    """Live recovery could not clear the backlog, so this instance must not admit saga writers.

    Raised during startup, deliberately fatal. The circuit-breaker rule is that a systemic recovery
    failure means "stop recovery and keep the writer gate closed", never "stop recovery and start
    normally" -- and the only way to keep it closed for this process is to refuse to finish booting.
    """


#: How long a starting instance waits for a peer to finish recovery before giving up.
#: Generous, because the alternative to waiting is refusing to boot: a peer draining a large
#: backlog is normal, and a short timeout would turn ordinary simultaneous startup into an outage.
SAGA_GATE_WAIT_SECONDS = 300.0

#: Poll interval while waiting for the gate.
_SAGA_GATE_POLL_SECONDS = 2.0


def _saga_recovery_is_armed() -> bool:
    """Whether this deployment asked for live recovery, so startup must wait for its verdict.

    Fails closed: a settings read that raises answers True, because the alternative is serving
    without knowing whether recovery was armed. Waiting costs startup latency; guessing wrong
    costs the invariant.
    """
    try:
        return str(MemorySettings.from_env().saga_reconcile_startup_mode or "").lower() == "live"
    except Exception:
        logger.warning("Could not read saga_reconcile_startup_mode; assuming live", exc_info=True)
        return True
