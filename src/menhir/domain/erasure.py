"""The marker written in place of content erased on request (CF-165).

Some content columns are declared NOT NULL -- ``merge_audit.snapshot_json`` is the one that
matters -- so an erasure cannot blank them by writing NULL. Writing an empty string instead
was the first approach and it is ambiguous in the worst way: the consumer parses it, fails,
and reports the record as MALFORMED. "Someone exercised their right to erasure" and "this row
is corrupt" then look identical, and only one of them is a bug worth chasing.

The marker is therefore valid JSON carrying no erased content, so a JSON consumer gets a
structured answer it can recognise rather than a parse error. The CF-165 plan requires exactly
this: an erasure-driven loss of recoverability "must be distinguishable from unexpected
corruption and from ordinary retention expiry".

Lives in the domain layer because both the infrastructure that writes it and the domain parser
that reads it need the same definition, and duplicating the key is how the two drift apart.
"""

from __future__ import annotations

import json
from typing import Any

#: Key identifying an erasure marker. Dunder-ish so it cannot collide with real payload fields.
ERASED_MARKER_KEY = "__erased__"

#: What a NOT NULL content column holds after erasure. Deliberately carries no subject, no
#: timestamp and no operation id: the marker replaces content, and must not reintroduce a
#: detail about who was erased.
ERASED_MARKER = json.dumps({ERASED_MARKER_KEY: True, "by": "CF-165"}, sort_keys=True)


def is_erased_marker(value: Any) -> bool:
    """Whether ``value`` is content that was erased on request rather than lost or corrupt.

    Accepts the empty string too: rows redacted before the marker existed hold ``""``, and
    reporting those as corrupt would recreate the ambiguity this exists to remove.
    """
    if value is None:
        return False
    if isinstance(value, str):
        if value == "":
            return True
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return False
    else:
        parsed = value
    return isinstance(parsed, dict) and parsed.get(ERASED_MARKER_KEY) is True


__all__ = ["ERASED_MARKER", "ERASED_MARKER_KEY", "is_erased_marker"]
