"""Reusable pre/post-recall operations shared by the recall coordinator."""

from __future__ import annotations

from menhir.services.recall_support_candidates import RecallSupportCandidateMixin
from menhir.services.recall_support_frontier import RecallSupportFrontierMixin
from menhir.services.recall_support_rehydration import RecallSupportRehydrationMixin
from menhir.services.recall_support_view_authority import RecallSupportViewAuthorityMixin


class RecallSupportMixin(
    RecallSupportRehydrationMixin,
    RecallSupportCandidateMixin,
    RecallSupportFrontierMixin,
    RecallSupportViewAuthorityMixin,
):
    """Compose the recall support operation families behind the historical import surface.

    Method owners live in the ``recall_support_<aspect>`` sibling modules; this class
    keeps ``menhir.services.recall_support.RecallSupportMixin`` resolving unchanged.
    """
