"""Model profiles: one class per model family owns that family's model-specific behavior.

Callers resolve a profile from the configured model name (and endpoint) and ask it, instead of
matching model strings themselves:

- ``shape_request``: chat-completion kwargs the model/endpoint requires (reasoning models take
  ``max_completion_tokens``, reject ``temperature``, and may opt into an OpenAI service tier).
- ``structured_output_mode``: the response format the Graphiti fork must request.
- ``provider_extra_body``: provider request extras (DeepSeek thinking off).
- ``extraction_instructions``: extra combined-extraction instructions for this family.

Matching is on the bare model name, so OpenRouter slugs (``openai/gpt-...``) resolve to the
default profile: OpenRouter translates request parameters itself, and production runs on such a
slug, so its prompts and requests stay unchanged.
"""

from __future__ import annotations

import os
from typing import Any, ClassVar, Literal

StructuredOutputMode = Literal["json_schema", "json_object"]

SERVICE_TIER_ENV = "MENHIR_OPENAI_SERVICE_TIER"
FLEX_TIMEOUT_ENV = "MENHIR_OPENAI_FLEX_TIMEOUT_S"
DEFAULT_FLEX_TIMEOUT_S = 900.0


def _lower(value: Any) -> str:
    return str(value or "").strip().lower()


def _is_openai_endpoint(base_url: Any) -> bool:
    url = _lower(base_url)
    return not url or "api.openai.com" in url


class ModelProfile:
    """Default behavior: requests pass through unchanged and no extra instructions are added."""

    name: ClassVar[str] = "default"
    structured_output_mode: ClassVar[StructuredOutputMode] = "json_schema"

    def matches(self, model: str, endpoint: str) -> bool:
        return True

    def shape_request(self, kwargs: dict[str, Any], *, base_url: Any) -> dict[str, Any]:
        return dict(kwargs)

    def provider_extra_body(self) -> dict[str, Any]:
        return {}

    def extraction_instructions(self) -> str:
        return ""


class DeepSeekProfile(ModelProfile):
    """DeepSeek API or model: JSON-object mode, and thinking off.

    Thinking is on by default (~2.3x latency, ~3x output tokens), which overruns the ingest wait
    window; ``{"type": "disabled"}`` is the only form the DeepSeek API accepts. Matched before the
    OpenAI families because the DeepSeek endpoint only serves DeepSeek models.
    """

    name = "deepseek"
    structured_output_mode = "json_object"

    def matches(self, model: str, endpoint: str) -> bool:
        return "deepseek" in model or "deepseek" in endpoint

    def provider_extra_body(self) -> dict[str, Any]:
        return {"thinking": {"type": "disabled"}}


class OpenAIReasoningProfile(ModelProfile):
    """OpenAI reasoning models (gpt-5*, gpt-6*, o-series) called by bare name.

    They reject ``max_tokens`` and any non-default ``temperature``. ``MENHIR_OPENAI_SERVICE_TIER``
    (e.g. ``flex``) is opt-in and only sent to the OpenAI endpoint; Flex can queue, so it gets a
    longer per-request timeout (``MENHIR_OPENAI_FLEX_TIMEOUT_S``, default 900 s).
    """

    name = "openai-reasoning"
    prefixes: ClassVar[tuple[str, ...]] = ("gpt-5", "gpt-6", "o1", "o3", "o4")

    def matches(self, model: str, endpoint: str) -> bool:
        return model.startswith(self.prefixes)

    def shape_request(self, kwargs: dict[str, Any], *, base_url: Any) -> dict[str, Any]:
        out = dict(kwargs)
        if "max_tokens" in out:
            tokens = out.pop("max_tokens")
            if tokens is not None:
                out.setdefault("max_completion_tokens", tokens)
        out.pop("temperature", None)
        tier = os.getenv(SERVICE_TIER_ENV, "").strip().lower()
        if tier and _is_openai_endpoint(base_url) and "service_tier" not in out:
            out["service_tier"] = tier
            if tier == "flex" and "timeout" not in out:
                out["timeout"] = float(os.getenv(FLEX_TIMEOUT_ENV) or DEFAULT_FLEX_TIMEOUT_S)
        return out


_GPT6_EXTRACTION_INSTRUCTIONS = """\
MODEL PRECISION (these rules override any pull toward exhaustive extraction):
Entities
- No possessive or relational words in entity names. Never start a name with `user's`, `my`,
  `your` or `their`, and drop `new`, `old` and `favourite`: `brass sextant`, not
  `user's new brass sextant`. Keep identifying details: `dented green canoe`. Ownership belongs
  in the relationship.
- One entity per real-world thing. If the text names one thing several ways (`loom warping`,
  `warping the loom`), pick one name and reuse it.
Edges
- Emit one edge per distinct fact, written once. Do not restate it from the other entity's side:
  `user will trial Fernwick and compare it with Larkspur` is one edge, not two.
- Do not emit a fact that another edge already states or implies. `user moved Odile's
  harpsichord on their furniture dolly` already implies `user owns a furniture dolly` and `the
  harpsichord is Odile's`; `user's rent rose $73 a month when the lease renewed` already implies
  `the lease renewed`; `user plans to use the loom's dobby attachment` already implies `loom has
  a dobby attachment`.
- The edge with the most detail always stays. When two edges overlap, delete the one that adds
  nothing; never delete the one carrying a date, amount, reason or comparison.
- Keep a list given in one clause as one edge: `user will ask the luthier about refretting and a
  new nut`, not one edge per item.
- Fold opinions, reasons and qualifiers into the fact they describe: `user bought a brass sextant
  from Halvard and loves it` is one edge, not separate purchase, brand and opinion edges.
- Still give each new detail its own edge: a date, amount, comparison or plan that no other edge
  states.
- Before answering, compare your edges pairwise and delete any edge whose information is fully
  contained in another.
"""


class Gpt6Profile(OpenAIReasoningProfile):
    """gpt-6 family. Same request shaping; its extraction over-splits facts and prefixes owners.

    smoke6 vs gpt-4o-mini: 1.4x entities and 1.8x edges with no block; 1.36x and 1.72x with a
    first, two-rule block (b4), so gpt-6 needs the duplicate patterns named explicitly. On item
    gpt4_2655b836 the pattern-naming block took edges from 2.09x to 1.89x (b5); what remained were
    edges implied by a fuller edge, which the implication rule and pairwise check target.

    Examples in the block are invented and must not come from LongMemEval or any eval set:
    examples drawn from the benchmark inflate its score (b5/b6 used them and are not clean).
    """

    name = "gpt-6"
    prefixes = ("gpt-6",)

    def extraction_instructions(self) -> str:
        return _GPT6_EXTRACTION_INSTRUCTIONS


_DEFAULT = ModelProfile()
#: Most specific first; the default matches everything and must stay last.
_PROFILES: tuple[ModelProfile, ...] = (
    DeepSeekProfile(),
    Gpt6Profile(),
    OpenAIReasoningProfile(),
    _DEFAULT,
)


def resolve_model_profile(model: Any, *, endpoint: Any = None) -> ModelProfile:
    """Return the profile for ``model`` served at ``endpoint``."""
    name, url = _lower(model), _lower(endpoint)
    return next(profile for profile in _PROFILES if profile.matches(name, url))
