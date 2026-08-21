"""CF-58: the past-vs-present lexicon core shared by two classifiers.

Two classifiers each carry a temporal keyword list for a DIFFERENT audience:

- ``facet_derivation`` classifies TEXT (memory content) into a historical/current
  belief bucket.
- ``temporal_intent`` classifies QUERIES into a temporal lens.

Neither used to import the other, and their lists overlapped -- ``"used to"``,
``"previously"``, ``"no longer"``, ``"now"``, ``"currently"``, ``"current"``. A
keyword added to one classifier did not reach the other.

These two constants are the single source of that overlap. Each consumer appends its
own audience-specific extras ON PURPOSE (merging them would change what the other
classifier matches); the extras live in the consumers, not here.
"""

from __future__ import annotations

# Past-vs-present markers common to BOTH the text and the query classifier.
_SHARED_HISTORICAL_MARKERS: tuple[str, ...] = ("used to", "previously", "no longer")
_SHARED_CURRENT_MARKERS: tuple[str, ...] = ("now", "currently", "current")

__all__ = ["_SHARED_HISTORICAL_MARKERS", "_SHARED_CURRENT_MARKERS"]
