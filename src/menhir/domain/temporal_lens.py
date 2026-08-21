"""CF-59: the temporal-lens vocabulary (current | historical | any) as one source.

These three string values were carried bare as ``intent: str`` with the allowed values
written only in a trailing comment (``oracles.QueryContext``, ``facets.FacetedQuery``)
and branched on as string literals (``oracle_combiner._ranking_score``). A typo in a
literal silently selected the wrong branch with no error.

``StrEnum`` rather than ``class TemporalLens(str, Enum)``, and the difference is not cosmetic.
Both keep ``==`` against a plain string working and both serialize through ``json.dumps`` as the
value -- but a plain ``str``-mixin enum FORMATS as ``"TemporalLens.HISTORICAL"``:

    str(X.HISTORICAL)   -> 'TemporalLens.HISTORICAL'      # str, Enum
    f"{X.HISTORICAL}"   -> 'TemporalLens.HISTORICAL'      # str, Enum
    str(X.HISTORICAL)   -> 'historical'                   # StrEnum

so any log line, message or f-string-built payload carrying the lens would have silently changed
text while every equality test kept passing. ``StrEnum`` is a true drop-in for the bare strings
these replaced. The string values are unchanged on purpose -- they appear in stored payloads.
"""

from __future__ import annotations

from enum import StrEnum


class TemporalLens(StrEnum):
    """Which temporal lens a query selects. String values are the wire/storage form."""

    CURRENT = "current"
    HISTORICAL = "historical"
    ANY = "any"


__all__ = ["TemporalLens"]
