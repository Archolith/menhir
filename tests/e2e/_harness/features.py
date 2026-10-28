"""Toggle any registered Menhir feature on or off, in any combination, per lane run.

WHY A CURATED REGISTRY AND NOT "EVERY BOOLEAN IN SETTINGS"
-----------------------------------------------------------
``settings_model.py`` reads 132 ``MENHIR_*`` variables. Most are tuning scalars, secrets
or deployment addresses, and a matrix over all of them is both meaningless and
unrunnable. What the campaign needs is the set of switches that change *observable
product behavior* on a supported path -- the ones whose combination could make an E2E
lane pass in one configuration and fail in another.

That set is curated below. To keep the curation honest, :func:`validate_registry`
asserts every declared variable is actually read by ``settings_model.py``: if a flag is
renamed or deleted upstream, the matrix fails loudly at session start instead of
silently toggling a variable nothing reads any more -- which would produce a green
"feature off" run that never turned anything off.

EXPRESSING A COMBINATION
------------------------
Three mechanisms, in increasing power. All are evidence-recorded.

1. A named preset::

       MENHIR_E2E_FEATURE_PRESET=mvp_default        # documented shipping defaults
       MENHIR_E2E_FEATURE_PRESET=all_off
       MENHIR_E2E_FEATURE_PRESET=research_on

2. An explicit override list, applied on top of the preset::

       MENHIR_E2E_FEATURES="frontier_shadow=on,explorer=off,structure_watcher=off"

3. A full powerset sweep over named flags, which parametrizes every lane over all
   2**n combinations::

       MENHIR_E2E_FEATURE_POWERSET="frontier_shadow,scalar_state,scalar_history"

   Three flags is 8 runs of every lane. This is deliberately opt-in and deliberately
   not defaulted: the combinatorics are the point, and so is the cost.
"""

from __future__ import annotations

import itertools
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

from menhir.config.feature_flags import FEATURES

__all__ = [
    "FEATURE_REGISTRY",
    "FeatureCombo",
    "FeatureFlag",
    "combos_from_environment",
    "validate_registry",
]


@dataclass(frozen=True)
class FeatureFlag:
    """One switch the campaign can drive.

    ``default`` is what an operator gets with no configuration -- recorded so evidence
    can state whether a lane ran on defaults or on a deviation. It is read from
    ``menhir.config.feature_flags`` so the harness cannot drift from the code defaults.

    ``in_mvp_surface`` marks flags that gate a capability the local-stdio MVP contract
    promises. A lane that fails with one of these OFF is a different severity from one
    that fails with a default-off research lane ON.
    """

    name: str
    env_var: str
    default: bool
    summary: str
    in_mvp_surface: bool = False


def _flag(name: str, env_var: str, summary: str, *, in_mvp_surface: bool = False) -> FeatureFlag:
    """Build a harness flag whose default is read from the product registry."""

    product = next((f for f in FEATURES.values() if f.env_var == env_var), None)
    if product is None:
        raise RuntimeError(
            f"tests/e2e feature {name!r} names {env_var}, which is not in "
            "menhir.config.feature_flags.FEATURES. Update FEATURE_REGISTRY."
        )
    if not isinstance(product.default, bool):
        raise RuntimeError(
            f"tests/e2e feature {name!r} ({env_var}) has product default "
            f"{product.default!r}, which is not a bool; it cannot be a matrix toggle."
        )
    return FeatureFlag(name, env_var, product.default, summary, in_mvp_surface)


#: The switches the campaign drives. Grouped by what they gate.
FEATURE_REGISTRY: tuple[FeatureFlag, ...] = (
    # --- surfaces the MVP contract promises -------------------------------------
    _flag(
        "structure_watcher",
        "MENHIR_STRUCTURE_WATCHER_ENABLED",
        summary="Background rescan that keeps code structure fresh.",
        in_mvp_surface=True,
    ),
    _flag(
        "privacy_redact",
        "MENHIR_PRIVACY_REDACT",
        summary="Redact memory content in telemetry and operator surfaces.",
        in_mvp_surface=True,
    ),
    _flag(
        "detailed_revisions",
        "MENHIR_RECORD_DETAILED_REVISIONS",
        summary="Record per-field revision history for memory updates.",
        in_mvp_surface=True,
    ),
    _flag(
        "experience_counter",
        "MENHIR_EXPERIENCE_COUNTER_ENABLED",
        summary="QuantState-style agent-memory counters.",
        in_mvp_surface=True,
    ),
    # --- surfaces outside the local-stdio MVP contract ---------------------------
    _flag(
        "explorer",
        "MENHIR_EXPLORER_ENABLED",
        summary="Explorer web UI. Ships on; outside the local-stdio MVP contract.",
    ),
    _flag(
        "oauth",
        "MENHIR_OAUTH_ENABLED",
        summary="Remote OAuth resource-server mode. Outside the local-stdio MVP.",
    ),
    _flag(
        "client_tokens",
        "MENHIR_CLIENT_TOKENS_ENABLED",
        summary="Per-client token auth. Outside the local-stdio MVP.",
    ),
    # --- default-off research lanes; the plan requires they stay preserved and off
    _flag(
        "frontier_shadow",
        "MENHIR_FRONTIER_SHADOW",
        summary="Shadow-mode frontier scoring (computes, does not serve).",
    ),
    _flag(
        "scalar_state",
        "MENHIR_PERSONAL_MEMORY_SCALAR_STATE_ENABLED",
        summary="Typed scalar state extraction. Default on since the #168 freeze (cost switch).",
    ),
    _flag(
        "scalar_history",
        "MENHIR_PERSONAL_MEMORY_SCALAR_HISTORY_ENABLED",
        summary="Scalar history projection. Default on since 2026-10-28 (#399 bucket 3).",
    ),
    _flag(
        "recall_audit",
        "MENHIR_PERSONAL_MEMORY_RECALL_AUDIT_ENABLED",
        summary="Recall audit trail.",
    ),
    _flag(
        "verifier_sync",
        "MENHIR_VERIFIER_SYNC_ENABLED",
        summary="Background verifier synchronization.",
    ),
)

_BY_NAME: Mapping[str, FeatureFlag] = {flag.name: flag for flag in FEATURE_REGISTRY}


def validate_registry(settings_model_path: Path) -> None:
    """Assert every registry entry names a variable ``settings_model.py`` actually reads.

    This is the guard that keeps the matrix from becoming decorative. A flag renamed
    upstream would otherwise leave the campaign exporting a dead variable and reporting
    that it had tested the feature disabled.
    """

    source = settings_model_path.read_text(encoding="utf-8")
    known = set(re.findall(r'"(MENHIR_[A-Z0-9_]+)"', source))
    missing = sorted(flag.env_var for flag in FEATURE_REGISTRY if flag.env_var not in known)
    if missing:
        raise RuntimeError(
            "tests/e2e feature registry names variables that "
            f"{settings_model_path.name} no longer reads: {missing}. Update "
            "FEATURE_REGISTRY -- until then the matrix would silently toggle nothing."
        )

    seen: set[str] = set()
    duplicates: set[str] = set()
    for flag in FEATURE_REGISTRY:
        if flag.env_var in seen:
            duplicates.add(flag.env_var)
        seen.add(flag.env_var)
    if duplicates:
        raise RuntimeError(
            f"duplicate env vars in FEATURE_REGISTRY: {sorted(duplicates)}. Two flags "
            "writing one variable means the matrix cannot set them independently."
        )


@dataclass(frozen=True)
class FeatureCombo:
    """One resolved on/off assignment across the registry."""

    label: str
    states: tuple[tuple[str, bool], ...]

    @property
    def mapping(self) -> dict[str, bool]:
        return dict(self.states)

    def env(self) -> dict[str, str]:
        """Environment for the child processes.

        Every registry flag is exported explicitly, including ones left at their
        default. An unexported flag would fall through to whatever the child's env file
        or built-in default says, and the evidence would claim a state the run did not
        actually pin.
        """

        return {_BY_NAME[name].env_var: ("true" if value else "false") for name, value in self.states}

    def deviations(self) -> dict[str, bool]:
        """Flags that differ from their shipped default -- the interesting part."""

        return {name: value for name, value in self.states if value != _BY_NAME[name].default}

    def as_evidence(self) -> dict[str, object]:
        return {
            "label": self.label,
            "flags": self.mapping,
            "deviations_from_default": self.deviations(),
            "env": self.env(),
        }

    def __str__(self) -> str:  # shows up in pytest's test id
        return self.label


def _baseline(**overrides: bool) -> dict[str, bool]:
    states = {flag.name: flag.default for flag in FEATURE_REGISTRY}
    states.update(overrides)
    return states


PRESETS: dict[str, dict[str, bool]] = {
    #: Shipped defaults. The configuration the MVP release actually claims to support.
    "mvp_default": _baseline(),
    #: Everything the registry knows about, off. Proves nothing silently depends on a
    #: default-on background worker.
    "all_off": {flag.name: False for flag in FEATURE_REGISTRY},
    #: Everything on. Not a supported configuration; it is a stress shape that surfaces
    #: interactions between lanes that are never exercised together.
    "all_on": {flag.name: True for flag in FEATURE_REGISTRY},
    #: MVP surfaces on, every research lane off -- the configuration Phase A's
    #: "preserve default-off research lanes" freeze is meant to describe.
    "mvp_surface_only": {flag.name: flag.in_mvp_surface for flag in FEATURE_REGISTRY},
    #: Research lanes on, to check they remain non-destructive when an operator opts in.
    "research_on": _baseline(
        scalar_state=True,
        scalar_history=True,
    ),
}


def _parse_overrides(raw: str) -> dict[str, bool]:
    truthy = {"1", "on", "true", "yes", "enabled"}
    falsy = {"0", "off", "false", "no", "disabled"}
    states: dict[str, bool] = {}
    for chunk in raw.split(","):
        item = chunk.strip()
        if not item:
            continue
        if "=" not in item:
            raise ValueError(f"MENHIR_E2E_FEATURES entry {item!r} is not name=on/off")
        name, _, value = item.partition("=")
        name = name.strip()
        value = value.strip().lower()
        if name not in _BY_NAME:
            raise ValueError(
                f"unknown feature {name!r}; known: {sorted(_BY_NAME)}"
            )
        if value in truthy:
            states[name] = True
        elif value in falsy:
            states[name] = False
        else:
            raise ValueError(f"feature {name!r} value {value!r} is not on/off")
    return states


def combos_from_environment(env: Mapping[str, str] | None = None) -> list[FeatureCombo]:
    """Resolve the combinations this session should run.

    Returns at least one combo. With no configuration that is ``mvp_default``, so the
    ordinary run tests the configuration the release actually ships.
    """

    source = os.environ if env is None else env

    preset_name = (source.get("MENHIR_E2E_FEATURE_PRESET") or "mvp_default").strip()
    if preset_name not in PRESETS:
        raise ValueError(f"unknown preset {preset_name!r}; known: {sorted(PRESETS)}")
    base = dict(PRESETS[preset_name])

    overrides = _parse_overrides(source.get("MENHIR_E2E_FEATURES", ""))
    base.update(overrides)

    powerset_raw = (source.get("MENHIR_E2E_FEATURE_POWERSET") or "").strip()
    if not powerset_raw:
        label = preset_name
        if overrides:
            label += "+" + ",".join(f"{k}={'on' if v else 'off'}" for k, v in sorted(overrides.items()))
        return [FeatureCombo(label=label, states=tuple(sorted(base.items())))]

    names = [item.strip() for item in powerset_raw.split(",") if item.strip()]
    unknown = [name for name in names if name not in _BY_NAME]
    if unknown:
        raise ValueError(f"unknown features in powerset: {unknown}; known: {sorted(_BY_NAME)}")
    if len(names) > 8:
        raise ValueError(
            f"powerset over {len(names)} flags is {2 ** len(names)} runs of every lane. "
            "Refusing above 8; narrow the sweep or run it deliberately in slices."
        )

    combos: list[FeatureCombo] = []
    for assignment in itertools.product([False, True], repeat=len(names)):
        states = dict(base)
        states.update(dict(zip(names, assignment)))
        label = ",".join(
            f"{name}={'on' if value else 'off'}" for name, value in zip(names, assignment)
        )
        combos.append(FeatureCombo(label=label, states=tuple(sorted(states.items()))))
    return combos


def combo_slug(combo: FeatureCombo) -> str:
    """Filesystem-safe form of the label, for per-combo evidence directories."""

    return re.sub(r"[^A-Za-z0-9._=+-]", "_", combo.label)[:120] or "default"


def requires(combo: FeatureCombo, *names: str) -> Iterable[str]:
    """Return the requested flags that are OFF in this combo.

    A lane that needs a feature enabled calls this and skips when it is off, rather than
    failing -- a powerset sweep is expected to include combinations a given lane cannot
    exercise, and those must read as SKIPPED, never as a product failure.
    """

    return [name for name in names if not combo.mapping.get(name, False)]
