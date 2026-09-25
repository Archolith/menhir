"""Corpus definition for the L6 composition probe: what counts as a consequential helper.

Moved verbatim from ``l6_composition_probe.py``; re-exported through the parts package.
"""

from __future__ import annotations

import re

# --------------------------------------------------------------------------------------
# Corpus definition: what counts as a consequential helper.
#
# Each category is (name-or-param signal, body signal). A helper joins the corpus when its
# NAME/PARAMS match, or when its BODY match is strong enough to stand alone (Cypher guards,
# ContextVar reads, control-signal raises). Body-only matches are noisier and are marked.
# --------------------------------------------------------------------------------------

CATEGORIES: dict[str, dict[str, object]] = {
    "tenancy": {
        "params": {"namespace", "tenant", "client_id", "client_name", "group_id", "scope",
                   "project", "project_name"},
        "name": re.compile(r"namespace|tenant|scope", re.I),
        "body": re.compile(r"\$namespace|\$group_id|\$project|namespace\s*IS\s*NULL", re.I),
    },
    "ownership": {
        "params": {"worker_id", "owner", "processing_owner", "lease", "lease_id", "holder"},
        "name": re.compile(r"owner|ownership|lease|claim|heartbeat|takeover|revoke", re.I),
        "body": re.compile(r"\$worker_id|processing_owner|SagaOwnershipRevoked|_revocation", re.I),
    },
    "destructive": {
        "params": set(),
        "name": re.compile(r"^(delete|erase|purge|drop|remove|wipe|truncate|unmerge|"
                           r"rollback|restore|supersede)", re.I),
        "body": re.compile(r"DETACH\s+DELETE|\bDELETE\s+[a-z]|REMOVE\s+[a-z]", re.I),
    },
    "provenance": {
        "params": {"evidence", "admitted", "admission", "provenance", "trust", "tier"},
        "name": re.compile(r"provenance|admission|admitted|evidence|attest|trust|promote|"
                           r"demote|apex", re.I),
        "body": re.compile(r"ADMITTED_ON|EVIDENCED_BY|SUPPORTED_BY|provenance", re.I),
    },
    "saga": {
        "params": {"journal", "saga_id", "disposition"},
        "name": re.compile(r"saga|journal|prepare|commit|reconcile|compensat|quarantine", re.I),
        "body": re.compile(r"owned_mutation|mark_committed|journal\.", re.I),
    },
    "budget": {
        "params": {"budget", "max_calls", "max_tokens", "quota"},
        "name": re.compile(r"budget|quota|reserve|throttle|rate_?limit", re.I),
        "body": re.compile(r"LlmBudgetExceeded|LlmUsageControlSignal"),
    },
    "callback": {
        "params": {"callback", "on_event", "hook", "handler", "sink", "emitter"},
        "name": re.compile(r"callback|_emit|emit_|dispatch|notify|publish", re.I),
        "body": re.compile(r"callback\(|_callback\.get\(\)", re.I),
    },
    "contextvar": {
        "params": set(),
        "name": re.compile(r"^(get|current|resolve)_(request|session|tier|auth|context)", re.I),
        "body": re.compile(r"_var\.get\(\)|ContextVar|\.get\(\)\s*or\s+_default", re.I),
    },
    "normalization": {
        "params": {"raw", "user_input", "untrusted"},
        "name": re.compile(r"normali[sz]e|sanitiz|escape|redact|scrub|_safe\b|guard", re.I),
        # No body signal. `re.sub(`/`.replace(` matched 279 helpers, nearly all of them
        # formatting -- a body probe that broad buries the security-sensitive ones.
        "body": re.compile(r"(?!x)x"),
    },
}

#: Params whose ABSENCE or `None` can disable enforcement. `namespace=None` is the CF-230
#: shape exactly: the opt-in isolation contract working as designed, handed the one value
#: that turns it off.
WEAKENING_PARAMS = {
    "namespace", "tenant", "group_id", "project", "project_name", "scope",
    "worker_id", "owner", "processing_owner", "required_state", "expected_state",
    "client_id", "client_name", "budget", "limit", "callback", "revocation",
    "actor", "session", "tier", "auth_mode",
}

#: Deliberate control signals. A blanket `except Exception` between the raiser and the actor
#: turns a decision back into a fault -- CF-227, and CF-231 one frame further up.
CONTROL_SIGNALS = {"LlmUsageControlSignal", "LlmBudgetExceeded", "SagaOwnershipRevoked"}

#: Boundaries that do NOT copy the context. `asyncio.to_thread` and `create_task` do copy it;
#: a bare `threading.Thread` and a raw executor submit do not.
LOSSY_BOUNDARIES = re.compile(r"threading\.Thread\(|run_in_executor\(|"
                              r"ThreadPoolExecutor\(|\.submit\(", re.I)

SKIP_DIRS = {"__pycache__", "static", "templates"}
