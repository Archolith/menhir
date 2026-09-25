"""Response-model hardening for Graphiti's combined extractor (sanitize before validate).

Owns ``_patch_graphiti_combined_extraction_models``, which wraps upstream ``CombinedExtraction``
with the receipt-scoped sanitation validator. Extracted verbatim from
``graphiti_extraction_patches``; that module re-exports it here.
"""

from __future__ import annotations

import logging
from typing import Any

from menhir.infrastructure.graphiti_extraction_patches_receipt import _extraction_receipt
from menhir.infrastructure.graphiti_extraction_patches_sanitize import _sanitize_combined_payload

logger = logging.getLogger(__name__)


def _patch_graphiti_combined_extraction_models() -> None:
    """Harden the combined-extraction response model: sanitize + close edge endpoints.

    Menhir forces single-episode ``add_episode`` through Graphiti's combined extractor
    (``extract_nodes_and_edges``), whose response model ``CombinedExtraction`` — unlike
    the separate-path ``ExtractedEntities`` that ``_patch_graphiti_entity_extraction``
    already hardens — has NO malformed-row tolerance and NO edge-endpoint closure. That
    left the path Menhir mandates with two live defects:

    1. A single malformed edge row (e.g. missing ``target_entity_name``) fails the whole
       ``CombinedExtraction(**llm_response)`` construction, zeroing the episode.
    2. An edge whose endpoint is absent from ``extracted_entities`` (e.g. ``Alice`` in
       ``Alice -OWNS-> Alice's coins`` when only the possessive was extracted) is dropped
       by Graphiti, then its now-unconnected partner is orphan-pruned — persisting zero
       entities from a content-bearing episode.

    This wraps ``CombinedExtraction`` with a ``mode="before"`` validator that drops only
    malformed rows and materializes missing edge endpoints (generic ``Entity``, gated
    against pronouns/names absent from the current and previous episode context) BEFORE
    validation and Graphiti's own resolution.

    The hardening is scoped to Menhir's forced path via the extraction-receipt ContextVar:
    when no receipt is active (any other combined-extraction caller, e.g. extraction_lab),
    the validator passes the payload through unchanged. The symbol is replaced in BOTH the
    prompts module (source of truth) and the maintenance module (which imports it directly),
    mirroring the dual-module pattern the separate-extraction patch already uses.
    """
    try:
        import graphiti_core.prompts.extract_nodes_and_edges as _ene_module
        import graphiti_core.utils.maintenance.combined_extraction as _ce_module
        from pydantic import BaseModel, Field, model_validator

        if getattr(_ce_module, "_menhir_combined_models_patched", False):
            return

        _CombinedEntity = _ene_module.CombinedEntity
        _CombinedFact = _ene_module.CombinedFact

        class PatchedCombinedExtraction(BaseModel):
            # Field declarations are copied VERBATIM from upstream CombinedExtraction: both
            # required, both described. `model_json_schema()` is what the structured-output path
            # sends as `response_format.json_schema`, so relaxing these to `default_factory=list`
            # told the model both arrays were optional and stripped their descriptions -- weakening
            # the constraint on exactly the local models this patch family exists to compensate for
            # -- and turned a `{}` or typo'd-key response from a loud upstream ValidationError into
            # a silent, successful zero-extraction. The tolerance belongs in the `mode="before"`
            # validator below, which runs ahead of required-field checking and can supply the
            # defaults without changing the schema handed to the model.
            extracted_entities: list[_CombinedEntity] = Field(  # type: ignore[valid-type]
                ..., description="List of extracted entities"
            )
            edges: list[_CombinedFact] = Field(  # type: ignore[valid-type]
                ..., description="List of extracted relationship facts"
            )

            @model_validator(mode="before")
            @classmethod
            def _menhir_sanitize(cls, data: Any) -> Any:
                receipt = _extraction_receipt.get()
                if receipt is None:
                    return data  # not Menhir's forced path — leave payload untouched
                return _sanitize_combined_payload(data, receipt, receipt.episode_text)

        _ene_module.CombinedExtraction = PatchedCombinedExtraction  # type: ignore[assignment]
        _ce_module.CombinedExtraction = PatchedCombinedExtraction  # type: ignore[assignment]
        _ce_module._menhir_combined_models_patched = True  # type: ignore[attr-defined]
        logger.debug("Graphiti combined-extraction model hardening patch applied")
    except (ImportError, AttributeError) as exc:
        logger.warning("Failed to patch Graphiti combined-extraction models: %s", exc)
