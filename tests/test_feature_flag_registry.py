"""Enforce the P1–P3 feature inventory using independent code and documentation evidence."""
from __future__ import annotations

from dataclasses import fields, make_dataclass, replace
from pathlib import Path
import re

import pytest

from menhir.config import MemorySettings
from menhir.config.feature_flags import EXEMPT, FEATURES, RETIRED
from menhir.config import feature_flags
from tests._feature_flag_contract import DYNAMIC_READS, inspect_source, read_sources

pytestmark = pytest.mark.unit
ROOT = Path(__file__).parents[1]
MODE_SUFFIXES = ("_mode", "_scope", "_policy", "_scale")


@pytest.fixture(scope="module")
def evidence():
    return inspect_source(read_sources(ROOT / "src/menhir"))


def inventory_errors(features, evidence, model_type=MemorySettings) -> list[str]:
    model = {f.name: f for f in fields(model_type)}
    required = {name for name, f in model.items() if f.type in (bool, "bool") or
                f.type in (str, "str") and name.endswith(MODE_SUFFIXES)}
    errors = [f"missing setting {name}" for name in required - features.keys()]
    defaults = model_type()
    envs = {}
    for key, entry in features.items():
        if entry.setting:
            if key != entry.setting or entry.setting not in model:
                errors.append(f"unknown setting {key}")
                continue
            expected = getattr(defaults, entry.setting)
            if type(expected) is not type(entry.default) or expected != entry.default:
                errors.append(f"default drift {key}")
            if (entry.env_var, *entry.env_var_aliases) != evidence.bindings.get(entry.setting):
                errors.append(f"environment binding drift {key}")
        elif key != entry.env_var:
            errors.append(f"invalid env key {key}")
        for env in (entry.env_var, *entry.env_var_aliases):
            if env in envs:
                errors.append(f"duplicate environment binding {env}")
            envs[env] = key
        for prerequisite in entry.requires:
            if prerequisite not in features or prerequisite == key:
                errors.append(f"invalid requirement {key}")
        if entry.requires and not entry.requirement_scope:
            errors.append(f"unscoped requirement {key}")
        for prerequisite in entry.emission_requires:
            if prerequisite not in features:
                errors.append(f"invalid emission requirement {key}")
        if not entry.description or not entry.category:
            errors.append(f"missing metadata {key}")
    outside = {key for key, paths in evidence.reads.items() if paths - {"config/settings_model.py"}}
    errors += [f"unregistered env read {key}" for key in outside - envs.keys()]
    for key, entry in features.items():
        if entry.setting and entry.setting not in required and not any(
            name in outside for name in (entry.env_var, *entry.env_var_aliases)
        ):
            errors.append(f"dead legacy setting {key}")
        if not entry.setting and entry.env_var not in outside:
            errors.append(f"dead env control {key}")
        if not entry.setting and repr(entry.default) not in evidence.raw_defaults.get(entry.env_var, set()):
            errors.append(f"env-only absent-value drift {key}")
    for setting, names in evidence.legacy_bindings.items():
        if any(binding != evidence.bindings.get(setting) for binding in names):
            errors.append(f"legacy environment binding drift {setting}")
    errors += [f"unresolved env read {item}" for item in sorted(evidence.unresolved)]
    return errors


def documentation_errors(features, text: str) -> list[str]:
    documented = set(re.findall(r"^\s*(?:#\s*)?(MENHIR_[A-Z0-9_]+)\s*=", text, re.M))
    errors = []
    for key, entry in features.items():
        if not entry.documented and key not in EXEMPT:
            errors.append(f"unapproved documentation exemption {key}")
        if entry.documented:
            for env in (entry.env_var, *entry.env_var_aliases):
                if env not in documented:
                    errors.append(f"missing documentation {env}")
    return errors


def test_registry_matches_settings_and_environment_readers(evidence) -> None:
    assert len(feature_flags._SETTINGS) + len(feature_flags._ENV_ONLY) == len(FEATURES)
    assert inventory_errors(FEATURES, evidence) == []
    assert evidence.dynamic == DYNAMIC_READS.keys()


def test_every_control_is_documented_with_no_exemptions() -> None:
    assert EXEMPT == {}
    assert documentation_errors(FEATURES, (ROOT / ".env.example").read_text()) == []


def test_boolean_controls_have_consumer_references(evidence) -> None:
    missing = []
    for key, entry in FEATURES.items():
        if type(entry.default) is bool and entry.setting:
            alias = evidence.bridges.get(key)
            if key not in evidence.consumers and alias not in evidence.consumers:
                missing.append(key)
    assert missing == [], f"Missing consumer references: {missing}"


def test_retired_controls_have_no_live_settings_or_readers(evidence) -> None:
    model = {f.name for f in fields(MemorySettings)}
    for key, reason in RETIRED.items():
        assert re.match(r"\d{4}-\d{2}-\d{2}: .+", reason)
        assert key not in model | evidence.reads.keys() | evidence.consumers
        assert key not in FEATURES


def test_known_interactions_and_observation_switches_are_recorded() -> None:
    for key in ("frontier_belief_gate", "frontier_evidence_anchor", "frontier_contradiction_interrupt"):
        assert FEATURES[key].requires == ("frontier_warden_gate",)
    assert FEATURES["personal_memory_scalar_view_authority_enabled"].requires == (
        "personal_memory_scalar_state_enabled",)
    assert FEATURES["personal_memory_scalar_deterministic_shadow"].emission_requires == (
        "personal_memory_consolidation_audit_enabled",)
    for suffix in ("attribute", "scope", "subject"):
        assert FEATURES[f"personal_memory_scalar_reconcile_{suffix}"].version_bump is True
    for key in ("frontier_shadow", "personal_memory_recall_audit_enabled",
                "personal_memory_consolidation_audit_enabled", "MENHIR_FRONTIER_TRACE"):
        assert FEATURES[key].observation_only is True
        assert FEATURES[key].recall_affecting is False


@pytest.mark.parametrize("fault", ["missing", "default", "env", "alias", "undocumented"])
def test_detectors_reject_inventory_and_documentation_regressions(evidence, fault: str) -> None:
    changed = dict(FEATURES)
    key = "frontier_source_memories"
    if fault == "missing":
        del changed[key]
    else:
        changes = {"default": {"default": False}, "env": {"env_var": "MENHIR_TYPO"},
                   "alias": {"env_var_aliases": ("MENHIR_INVENTED_ALIAS",)},
                   "undocumented": {"documented": False}}[fault]
        changed[key] = replace(changed[key], **changes)
    assert inventory_errors(changed, evidence) or documentation_errors(changed, (ROOT / ".env.example").read_text())


def test_scanner_detects_literals_constants_import_aliases_and_new_dynamic_reads() -> None:
    evidence = inspect_source({"synthetic.py": '''import os as system
from os import getenv as read_env
KEY = "MENHIR_CONSTANT"
a = system.getenv("MENHIR_LITERAL", "")
b = read_env(KEY)
c = system.environ["MENHIR_SUBSCRIPT"]
d = system.getenv(compute_key())
def nested():
    from os import getenv as nested_read
    return nested_read(key="MENHIR_KEYWORD", default="")
'''})
    assert evidence.reads.keys() == {"MENHIR_CONSTANT", "MENHIR_LITERAL", "MENHIR_SUBSCRIPT", "MENHIR_KEYWORD"}
    assert evidence.unresolved == {("synthetic.py", "<module>", "compute_key()")}


def test_new_boolean_or_mode_fields_cannot_bypass_the_inventory(evidence) -> None:
    for name, annotation, default in (("new_enabled", bool, False), ("new_mode", str, "off")):
        changed_model = make_dataclass("ChangedSettings", [(name, annotation, default)],
                                       bases=(MemorySettings,), frozen=True)
        assert f"missing setting {name}" in inventory_errors(FEATURES, evidence, changed_model)


def test_new_environment_reads_and_removed_readers_are_detected(evidence) -> None:
    changed = inspect_source({"synthetic.py": 'import os\nx = os.getenv("MENHIR_NEW_FLAG")'})
    assert "unregistered env read MENHIR_NEW_FLAG" in inventory_errors(FEATURES, changed)
    changed.reads = {k: v for k, v in evidence.reads.items() if k != "MENHIR_FRONTIER_TRACE"}
    assert "dead env control MENHIR_FRONTIER_TRACE" in inventory_errors(FEATURES, changed)


def test_env_only_absent_default_drift_is_detected(evidence) -> None:
    changed = dict(FEATURES)
    changed["MENHIR_MCP_TIMEOUT"] = replace(changed["MENHIR_MCP_TIMEOUT"], default="999")
    assert "env-only absent-value drift MENHIR_MCP_TIMEOUT" in inventory_errors(changed, evidence)


def test_legacy_wrapper_cannot_silently_read_another_registered_control(evidence) -> None:
    changed = inspect_source({"synthetic.py":
        '_get_setting(settings, "oauth_enabled", "MENHIR_TRUSTED_PROXY", False)'})
    changed.bindings = evidence.bindings
    assert "legacy environment binding drift oauth_enabled" in inventory_errors(FEATURES, changed)


def test_documentation_detector_rejects_prose_only_mentions() -> None:
    entry = FEATURES["frontier_source_memories"]
    assert documentation_errors({entry.setting: entry}, "Use MENHIR_FRONTIER_SOURCE_MEMORIES to opt out")
    assert documentation_errors({entry.setting: entry}, "# MENHIR_FRONTIER_SOURCE_MEMORIES=false") == []
