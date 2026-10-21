"""E2E-19 - fresh install, shipped defaults (#168 final-gate item 59).

Runs the installed wheel the way a user does, with no feature flag exported and no ENV_FILE:
config lives in a lane-owned temp HOME (~/.menhir/.env), written by `menhir setup`.

Steps:

1. `menhir setup --provider local` writes a .env with no feature-flag line.
2. The effective settings equal the code defaults (promoted flags on, the rest off).
3. `menhir up` starts a full-mode backend; a stock README MCP client config connects to it.
4. A memory ingests and enriches in the background with no nudge.
5. Recall runs the promoted defaults (Warden gate, source memories).
6. A correction lands; current recall serves it and the history keeps the original.
7. A restart through `menhir up` keeps state and settings.
8. Explicit opt-outs survive `menhir setup` re-runs, `menhir serve` from another cwd reads the
   same state-dir .env, and the opt-outs take effect; removing them restores the defaults.
9. Opted in to MENHIR_INFERRED_SELF_MODE=enforce (not a shipped default): a hook turn's fact
   lands on the anchored self, an agent-written memory about the user lands on the inferred
   self, the verbatim client copy of the hook turn skips extraction, no ordinary "user" node
   forms, and no marker of either prefix is persisted.

Each stock-client launch is a new MCP session, and a fresh write is SESSION-scoped until it is
promoted, so a later session gets no ranked items for it; only the source-memory section carries
it. Steps that need the Warden to rank something therefore write in the session they recall from.

Canonical self: an admitted user turn in the first person binds to the canonical self node under
the default `enforce` and does not under `off`. Binding needs the model to use the speaker marker
Menhir puts in the extraction prompt; the deterministic provider does that for this lane's fixture
sentence only. For a real-model pass, set MENHIR_E2E_CHAT_BASE_URL (and MENHIR_E2E_CHAT_MODEL)
to send chat and extraction there while embeddings stay on the deterministic provider; Atlas
criteria then depend on that model's extraction. MENHIR_E2E_EPISODE_WAIT_S raises the lane's
per-episode wait and, for that pass, the backend's add_episode timeout (default 300 s).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from uuid import uuid4

import pytest

from menhir.domain.namespace import namespace_to_group_id
from menhir.domain.self_identity import (
    INFERRED_ENDPOINT_MARKER_PREFIX,
    SUBJECT_ENDPOINT_MARKER_PREFIX,
    inferred_self_uuid_for_namespace,
    self_uuid_for_namespace,
)
from tests.e2e._harness.config import E2EConfig
from tests.e2e._harness.evidence import LaneEvidence
from tests.e2e._harness.fresh_install import (
    code_defaults,
    edit_env,
    effective_settings,
    flag_lines,
    make_fresh_home,
    projection_for_turn,
    readme_client_config,
    run_cli,
    start_server,
    stock_client_session,
    tool_json,
    tool_text,
)
from tests.e2e._harness.fresh_install import wait_episode_state as _wait_episode_state
from tests.e2e._harness.phase3 import admit_turn, close_pass, record_turn
from tests.e2e._harness.providers import (
    INFERRED_AGENT_TEXT,
    SELF_POSTCARD_TEXT,
    deterministic_provider,
    local_provider_environment,
)
from tests.e2e._harness.stack import graph_query
from tests.e2e.test_e2e_02_memory_lifecycle import (
    CORRECTED_AMOUNT,
    CORRECTION,
    EPISODE_ID,
    ORIGINAL,
    ORIGINAL_AMOUNT,
)

# Per-episode wait; raise it (MENHIR_E2E_EPISODE_WAIT_S) for a real-model pass.
EPISODE_WAIT_S = float(os.environ.get("MENHIR_E2E_EPISODE_WAIT_S", "") or 300)

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(max(2400, int(EPISODE_WAIT_S * 10)))]

CRITERIA = [
    "fresh_setup_from_wheel",
    "effective_defaults_match_code",
    "up_starts_and_reports_ready",
    "stock_client_config",
    "ingest_enriches_in_background",
    "recall_runs_promoted_defaults",
    "correction_and_history",
    "restart_via_up_keeps_state",
    "explicit_opt_out_survives_setup_rerun",
    "serve_from_other_cwd_reads_state_env",
    "opt_out_takes_effect_after_restart",
    "canonical_self_binds",
    "two_tier_self",
]

TWO_TIER = {"MENHIR_INFERRED_SELF_MODE": "enforce"}
# `add_memory` answers "Queued. episode_id=<uuid>"; the tracked write uses "episode_id: <uuid>".
ADMITTED_EPISODE_ID = re.compile(r"episode_id[=:]\s*([0-9a-f-]{36})")

OPT_OUT = {
    "MENHIR_FRONTIER_WARDEN_GATE": "false",
    "MENHIR_FRONTIER_CONTRADICTION_INTERRUPT": "false",
    "MENHIR_FRONTIER_SOURCE_MEMORIES": "false",
    "MENHIR_CANONICAL_SELF_BINDING_MODE": "off",
}

QUERY = "Atlas Lantern refund approval threshold"
SELF_TEXT = SELF_POSTCARD_TEXT


def wait_episode_state(config: E2EConfig, episode_uuid: str) -> str:
    return _wait_episode_state(config, episode_uuid, timeout=EPISODE_WAIT_S)
SELF_AT = "2026-07-01T10:00:00+00:00"


def _session(home, config: E2EConfig, installed, evidence: LaneEvidence, label: str):
    """A stock client session; client.json is rewritten because the backend port changes."""
    path = home.root / "client.json"
    path.write_text(json.dumps(readme_client_config(installed.venv_python, config.backend_url),
                               indent=2), encoding="utf-8")
    return stock_client_session(path, home, evidence, label=label)


async def _recall(client, ns: str, *, warden: bool = False,
                  invalidated: bool = False) -> tuple[dict, str]:
    args: dict = {"query": QUERY, "namespace": ns}
    if warden:
        args["include_warden_status"] = True
    if invalidated:
        args["include_invalidated"] = True
    result = await client.call_tool("recall_memories", args)
    return tool_json(result), tool_text(result)


async def _track(client, text: str, ns: str) -> str:
    receipt = tool_text(await client.call_tool(
        "call_tool",
        {"name": "add_memory_and_track", "arguments": {"text": text, "namespace": ns}}))
    match = EPISODE_ID.search(receipt)
    assert match, f"no episode_id in tracked-write receipt: {receipt[:400]}"
    return match.group(1)


async def _admit_self(config: E2EConfig, client, ns: str) -> dict | None:
    turn = record_turn(config, ns, SELF_TEXT, occurred_at=SELF_AT)
    await admit_turn(client, ns, SELF_TEXT, turn)
    deadline = time.monotonic() + 60
    proj = projection_for_turn(config, turn)
    while proj is None and time.monotonic() < deadline:
        await asyncio.sleep(2)
        proj = projection_for_turn(config, turn)
    return proj


async def _write_then_recall(config: E2EConfig, client, prefix: str) -> tuple[dict, str, str]:
    """Write ORIGINAL in a new namespace from this session, wait READY, recall it with the receipt."""
    ns = f"{prefix}-{uuid4().hex[:12]}"
    state = wait_episode_state(config, await _track(client, ORIGINAL, ns))
    r, text = await _recall(client, ns, warden=True)
    return r, text, state


def _ranked_has(r: dict, amount: str) -> bool:
    return any(amount in json.dumps(item) for item in r.get("items") or [])


def _sources_have(r: dict, text: str) -> bool:
    return any(text == (item or {}).get("content") for item in r.get("source_memories") or [])


def _self_rows(config: E2EConfig, ns: str) -> list[dict]:
    return graph_query(
        config,
        "MATCH (s:Entity {uuid:$uuid}) RETURN s.name AS name, s.group_id AS group_id",
        uuid=self_uuid_for_namespace(ns))


def _self_binding(config: E2EConfig, ns: str, state: str) -> dict:
    """What the graph holds for the namespace's canonical self, plus any leaked speaker marker."""
    uuid, group = self_uuid_for_namespace(ns), namespace_to_group_id(ns)
    edges = graph_query(
        config,
        "MATCH (s:Entity {uuid:$uuid})-[r]-(o:Entity) RETURN type(r) AS type, r.fact AS fact, "
        "o.name AS other",
        uuid=uuid)
    leaked_nodes = graph_query(
        config,
        "MATCH (n) WHERE n.group_id = $g AND (coalesce(n.name, '') CONTAINS $p "
        "OR coalesce(n.summary, '') CONTAINS $p) RETURN count(n) AS c",
        g=group, p=SUBJECT_ENDPOINT_MARKER_PREFIX)
    leaked_facts = graph_query(
        config,
        "MATCH ()-[r]->() WHERE r.group_id = $g AND (coalesce(r.fact, '') CONTAINS $p "
        "OR coalesce(r.name, '') CONTAINS $p) RETURN count(r) AS c",
        g=group, p=SUBJECT_ENDPOINT_MARKER_PREFIX)
    # Same-named "user" nodes that are not the canonical self (the #343 fork).
    forks = graph_query(
        config,
        "MATCH (n:Entity {name:'user'}) WHERE n.group_id = $g AND n.uuid <> $uuid "
        "OPTIONAL MATCH (n)-[r:RELATES_TO]-() WHERE r.fact IS NOT NULL "
        "RETURN n.uuid AS uuid, collect(r.fact) AS facts",
        g=group, uuid=uuid)
    return {
        "projection_state": state,
        "group_id": group,
        "self_rows": _self_rows(config, ns),
        "self_edges": edges,
        "fork_user_nodes": forks,
        "marker_leaks": leaked_nodes[0]["c"] + leaked_facts[0]["c"],
    }


def _bound(probe: dict) -> bool:
    # A fact edge, not Menhir's similarity link (which has no fact).
    return (
        probe["projection_state"] == "READY"
        and probe["self_rows"] == [{"name": "user", "group_id": probe["group_id"]}]
        and any("cobalt postcards" in (e.get("fact") or "") for e in probe["self_edges"])
        and probe["marker_leaks"] == 0
    )


def _facts_of(config: E2EConfig, uuid: str) -> list[str]:
    rows = graph_query(
        config,
        "MATCH (s:Entity {uuid:$uuid})-[r:RELATES_TO]-(:Entity) WHERE r.fact IS NOT NULL "
        "RETURN r.fact AS fact",
        uuid=uuid)
    return sorted(str(r["fact"]) for r in rows)


def _two_tier_probe(config: E2EConfig, ns: str, client_uuid: str | None,
                    projection_uuid: str | None) -> dict:
    """Both self tiers, ordinary "user" forks, marker leaks of either prefix, and the client skip."""
    anchored, inferred = self_uuid_for_namespace(ns), inferred_self_uuid_for_namespace(ns)
    group = namespace_to_group_id(ns)
    inferred_rows = graph_query(
        config,
        "MATCH (i:Entity {uuid:$uuid}) RETURN i.name AS name, i.entity_role AS role, "
        "i.self_tier AS tier, coalesce(i.is_self, false) AS is_self, i.group_id AS group_id",
        uuid=inferred)
    forks = graph_query(
        config,
        "MATCH (n:Entity) WHERE n.group_id = $g AND toLower(trim(n.name)) = 'user' "
        "AND n.uuid <> $uuid RETURN n.uuid AS uuid",
        g=group, uuid=anchored)
    leaks = 0
    for prefix in (SUBJECT_ENDPOINT_MARKER_PREFIX, INFERRED_ENDPOINT_MARKER_PREFIX):
        leaks += graph_query(
            config,
            "MATCH (n) WHERE n.group_id = $g AND (coalesce(n.name, '') CONTAINS $p "
            "OR coalesce(n.summary, '') CONTAINS $p) RETURN count(n) AS c",
            g=group, p=prefix)[0]["c"]
        leaks += graph_query(
            config,
            "MATCH ()-[r]->() WHERE r.group_id = $g AND (coalesce(r.fact, '') CONTAINS $p "
            "OR coalesce(r.name, '') CONTAINS $p) RETURN count(r) AS c",
            g=group, p=prefix)[0]["c"]
    link = graph_query(
        config,
        "MATCH (:Entity {uuid:$a})-[r:INFERRED_VIEW_OF]->(:Entity {uuid:$i}) RETURN count(r) AS c",
        a=anchored, i=inferred)[0]["c"]
    client = graph_query(
        config,
        "MATCH (n:Episodic {uuid:$uuid}) RETURN n.processing_state AS state, "
        "n.extraction_skipped_for AS skipped_for",
        uuid=client_uuid or "") if client_uuid else []
    return {
        "anchored_facts": _facts_of(config, anchored),
        "inferred_facts": _facts_of(config, inferred),
        "inferred_node": inferred_rows,
        "fork_user_nodes": [r["uuid"] for r in forks],
        "marker_leaks": leaks,
        "tier_link_edges": link,
        "client": client,
        "projection_uuid": projection_uuid,
        "group_id": group,
    }


def _two_tier_ok(p: dict) -> bool:
    anchored, inferred = " ".join(p["anchored_facts"]), " ".join(p["inferred_facts"])
    return (
        "cobalt postcards" in anchored and "oolong tea" not in anchored
        and "oolong tea" in inferred and "cobalt postcards" not in inferred
        and p["inferred_node"] == [{"name": "user-inferred", "role": "inferred_self",
                                    "tier": "inferred", "is_self": False,
                                    "group_id": p["group_id"]}]
        and p["fork_user_nodes"] == []
        and p["marker_leaks"] == 0
        and p["tier_link_edges"] == 1
        and p["client"] == [{"state": "READY", "skipped_for": p["projection_uuid"]}]
    )


def _provider_env(base_url: str) -> dict[str, str]:
    """Deterministic provider, or chat on MENHIR_E2E_CHAT_BASE_URL with embeddings kept here."""
    env = local_provider_environment(base_url)
    chat_url = os.environ.get("MENHIR_E2E_CHAT_BASE_URL", "").strip()
    if chat_url:
        env["LOCAL_LLM_BASE_URL"] = chat_url
        env["LOCAL_LLM_CHAT_MODEL"] = os.environ.get("MENHIR_E2E_CHAT_MODEL", "").strip() or "local"
        # A slow local model needs more than the 300 s default per episode, as a user would set.
        env["MEMORY_GRAPHITI_ADD_EPISODE_TIMEOUT_SECONDS"] = str(int(EPISODE_WAIT_S))
    return env


def _configured(ws: dict) -> set[str]:
    return set(ws.get("configured_guards") or [])


async def test_e2e_19_fresh_install_shipped_defaults(
    e2e_config: E2EConfig, e2e_installed, fresh_graph, lane_evidence: LaneEvidence, tmp_path,
) -> None:
    cfg, inst, ev = e2e_config, e2e_installed, lane_evidence
    ns = f"e2e19-{uuid4().hex[:12]}"
    live: list = []

    def start(command: str, cwd, name: str):
        backend = start_server(cfg, inst, home, ev, command=command, cwd=cwd, name=name)
        live.append(backend)
        return backend

    def stop(backend) -> None:
        backend.terminate()

    # 1. setup from the wheel
    home = make_fresh_home(tmp_path / "fresh")
    assert not home.env_path.exists()
    setup = run_cli(cfg, inst, home, "setup", "--provider", "local")
    env_exists = home.env_path.exists()
    ev.attach("setup-stdout.txt", setup.stdout or "")
    ev.attach("setup-stderr.txt", setup.stderr or "")
    if env_exists:
        ev.attach("generated.env", home.env_path.read_text(encoding="utf-8"))
    mode = (home.env_path.stat().st_mode & 0o777) if env_exists else None
    mode_ok = os.name == "nt" or mode == 0o600
    setup_ok = (setup.returncode == 0 and env_exists and flag_lines(home.env_path) == []
                and mode_ok)
    ev.record("fresh_setup_from_wheel", passed=setup_ok, detail={
        "returncode": setup.returncode, "env_exists": env_exists,
        "mode": oct(mode) if mode is not None else None,
        "flag_lines": flag_lines(home.env_path) if env_exists else None})
    assert setup_ok, f"setup failed: rc={setup.returncode} {setup.stderr[-1000:]}"

    with deterministic_provider() as base_url:
        try:
            # 2. effective defaults (after the user fills in provider + graph settings)
            provider_env = _provider_env(base_url)
            ev.record_stack(chat_base_url=provider_env["LOCAL_LLM_BASE_URL"],
                            chat_model=provider_env["LOCAL_LLM_CHAT_MODEL"])
            edit_env(home.env_path, set_values={
                **provider_env,
                "NEO4J_URI": cfg.neo4j_uri, "NEO4J_USER": cfg.neo4j_user,
                "NEO4J_PASSWORD": cfg.neo4j_password, "NEO4J_DATABASE": cfg.neo4j_database})
            eff = effective_settings(cfg, inst, home)
            values, expected = eff["values"], code_defaults()
            mismatch = sorted(k for k in set(values) | set(expected)
                              if values.get(k) != expected.get(k))
            promoted = (values.get("MENHIR_FRONTIER_WARDEN_GATE") is True
                        and values.get("MENHIR_FRONTIER_CONTRADICTION_INTERRUPT") is True
                        and values.get("MENHIR_FRONTIER_SOURCE_MEMORIES") is True
                        and values.get("MENHIR_CANONICAL_SELF_BINDING_MODE") == "enforce"
                        and values.get("MENHIR_FRONTIER_BELIEF_GATE") is False
                        and values.get("MENHIR_FRONTIER_EVIDENCE_ANCHOR") is False
                        and values.get("MENHIR_BENCHMARK_MODE") is False)
            env_file_ok = eff.get("env_file") == str(home.env_path)
            ev.record("effective_defaults_match_code",
                      passed=not mismatch and promoted and env_file_ok,
                      detail={"mismatching_keys": mismatch, "promoted_ok": promoted,
                              "env_file": eff.get("env_file")})

            # 3. menhir up
            backend = start("up", home.cwd, "up-1")
            payload = backend.ready_payload or {}
            up_log = backend.log_path.read_text(encoding="utf-8", errors="replace")
            up_ok = (payload.get("status") in {"ready", "ok"}
                     and bool((payload.get("capabilities") or {}).get("enrichment_ready"))
                     and "startup mode: full" in up_log)
            ev.record("up_starts_and_reports_ready", passed=up_ok, detail={
                "status": payload.get("status"), "capabilities": payload.get("capabilities"),
                "startup_mode_full": "startup mode: full" in up_log})
            assert up_ok, "menhir up did not report ready in full mode"

            # 4. stock client config
            async with _session(home, cfg, inst, ev, "client-1") as client:
                names = {t.name for t in (await client.list_tools()).tools}
                need = {"recall_memories", "build_context", "add_memory", "call_tool"}
                ev.record("stock_client_config", passed=need <= names,
                          detail={"missing": sorted(need - names), "tool_count": len(names)})

                # 5. ingest, no nudge
                first_episode = await _track(client, ORIGINAL, ns)
                state = wait_episode_state(cfg, first_episode)
                ev.record("ingest_enriches_in_background", passed=state == "READY",
                          detail={"episode": first_episode, "state": state})

                # 6. promoted defaults on recall
                r, text = await _recall(client, ns, warden=True)
                ws = r.get("warden_status") or {}
                configured = _configured(ws)
                built = tool_text(await client.call_tool(
                    "build_context", {"query": "Atlas Lantern refunds", "namespace": ns}))
                sources = r.get("source_memories")
                ev.record("recall_runs_promoted_defaults", passed=(
                    ORIGINAL_AMOUNT in text and ws.get("state") == "applied"
                    and {"scope", "oracle_admission", "contradiction"} <= configured
                    and {"scope", "oracle_admission"} <= set(ws.get("applied_guards") or [])
                    and "evidence_anchor" not in configured
                    and "currentness" not in configured
                    and bool(sources) and ORIGINAL_AMOUNT in built),
                    detail={"warden_status": ws,
                            "source_memories": len(sources) if sources else 0,
                            "keys": sorted(r), "build_context_has_amount": ORIGINAL_AMOUNT in built})

                # 7. canonical self under the default enforce (criterion recorded after step 12)
                ns_self = f"e2e19-self-{uuid4().hex[:12]}"
                proj = await _admit_self(cfg, client, ns_self)
                self_state = wait_episode_state(cfg, proj["uuid"]) if proj else "NO_PROJECTION"
                probe_enforce = _self_binding(cfg, ns_self, self_state)

                # 8. correction and history
                correction_episode = await _track(client, CORRECTION, ns)
                corr_state = wait_episode_state(cfg, correction_episode)
                _, current = await _recall(client, ns)
                _, history = await _recall(client, ns, invalidated=True)
                has_current, has_history = CORRECTED_AMOUNT in current, ORIGINAL_AMOUNT in history
                ev.record("correction_and_history",
                          passed=corr_state == "READY" and has_current and has_history,
                          detail={"state": corr_state, "current_has_corrected": has_current,
                                  "history_has_original": has_history,
                                  "current_head": current[:300], "history_head": history[:300]})

            # 9. restart through up
            settings_before = effective_settings(cfg, inst, home)["values"]
            stop(backend)
            backend = start("up", home.cwd, "up-2")
            async with _session(home, cfg, inst, ev, "client-2") as client:
                after, _ = await _recall(client, ns)
            first_state = wait_episode_state(cfg, first_episode)
            settings_after = effective_settings(cfg, inst, home)["values"]
            # A new session: the unpromoted SESSION-scoped entities rank nothing; both memories
            # must come back through the default-on source-memory section.
            sources_ok = _sources_have(after, ORIGINAL) and _sources_have(after, CORRECTION)
            ev.record("restart_via_up_keeps_state", passed=(
                sources_ok and first_state == "READY" and settings_after == settings_before),
                detail={"source_memories_carry_both": sources_ok,
                        "ranked_count_new_session": len(after.get("items") or []),
                        "first_episode_state": first_state,
                        "settings_equal": settings_after == settings_before})

            # 10. opt-out survives setup re-runs
            stop(backend)
            edit_env(home.env_path, set_values=OPT_OUT)
            lines_before = flag_lines(home.env_path)
            bytes_before = home.env_path.read_bytes()
            diffs: list = []
            bytes_after: list[bytes] = []
            rcs: list[int] = []
            for i in range(2):
                again = run_cli(cfg, inst, home, "setup", "--provider", "local")
                rcs.append(again.returncode)
                lines_now = flag_lines(home.env_path)
                bytes_after.append(home.env_path.read_bytes())
                if lines_now != lines_before:
                    diffs.append({"run": i + 1, "before": lines_before, "after": lines_now})
            ev.record("explicit_opt_out_survives_setup_rerun", passed=(
                rcs == [0, 0] and not diffs and bytes_after[1] == bytes_after[0]),
                detail={"returncodes": rcs, "lines_before": lines_before, "diffs": diffs,
                        "second_run_stable": bytes_after[1] == bytes_after[0],
                        "first_run_changed_file": bytes_after[0] != bytes_before})

            # 11. serve from another cwd reads the state-dir .env
            backend = start("serve", home.other_cwd, "serve-other-cwd")
            async with _session(home, cfg, inst, ev, "client-serve") as client:
                r, text, state_c = await _write_then_recall(cfg, client, "e2e19-serve")
            ws = r.get("warden_status") or {}
            # The opt-outs exist only in the state-dir .env, so "disabled" proves serve read it.
            ev.record("serve_from_other_cwd_reads_state_env", passed=(
                state_c == "READY" and ws.get("state") == "disabled"
                and not ws.get("configured_guards") and not r.get("source_memories")
                and _ranked_has(r, ORIGINAL_AMOUNT)),
                detail={"episode_state": state_c, "warden_status": ws,
                        "source_memories": bool(r.get("source_memories")),
                        "ranked_has_amount": _ranked_has(r, ORIGINAL_AMOUNT)})
            stop(backend)

            # 12. opt-out takes effect after restart; removing it restores defaults
            backend = start("up", home.cwd, "up-opted-out")
            ns_off = f"e2e19-off-{uuid4().hex[:12]}"
            async with _session(home, cfg, inst, ev, "client-opted-out") as client:
                r_off, _, state_d = await _write_then_recall(cfg, client, "e2e19-off")
                ws_off = r_off.get("warden_status") or {}
                proj_off = await _admit_self(cfg, client, ns_off)
                off_state = wait_episode_state(cfg, proj_off["uuid"]) if proj_off else "NO_PROJECTION"
                probe_off = _self_binding(cfg, ns_off, off_state)
            ev.record("canonical_self_binds", passed=(
                _bound(probe_enforce) and off_state == "READY" and probe_off["self_rows"] == []),
                detail={"enforce": probe_enforce, "off": probe_off})
            opted_out = effective_settings(cfg, inst, home)["values"]
            part_a = (state_d == "READY" and ws_off.get("state") == "disabled"
                      and not r_off.get("source_memories") and _ranked_has(r_off, ORIGINAL_AMOUNT)
                      and opted_out.get("MENHIR_CANONICAL_SELF_BINDING_MODE") == "off")
            stop(backend)

            edit_env(home.env_path, remove=OPT_OUT.keys())
            assert flag_lines(home.env_path) == []
            backend = start("up", home.cwd, "up-restored")
            async with _session(home, cfg, inst, ev, "client-restored") as client:
                r, _, state_e = await _write_then_recall(cfg, client, "e2e19-restored")
            ws_on = r.get("warden_status") or {}
            restored = effective_settings(cfg, inst, home)["values"]
            part_b = (state_e == "READY" and ws_on.get("state") == "applied"
                      and {"scope", "oracle_admission", "contradiction"} <= _configured(ws_on)
                      and _sources_have(r, ORIGINAL) and restored == code_defaults())
            ev.record("opt_out_takes_effect_after_restart", passed=part_a and part_b, detail={
                "opted_out": {"ok": part_a, "episode_state": state_d, "warden_status": ws_off,
                              "source_memories": bool(r_off.get("source_memories")),
                              "binding_mode": opted_out.get("MENHIR_CANONICAL_SELF_BINDING_MODE")},
                "restored": {"ok": part_b, "episode_state": state_e, "warden_status": ws_on,
                             "settings_equal_defaults": restored == code_defaults()}})
            stop(backend)

            # 13. two-tier self, opted in (not a shipped default; removed again afterwards)
            edit_env(home.env_path, set_values=TWO_TIER)
            backend = start("up", home.cwd, "up-two-tier")
            ns_tier = f"e2e19-tier-{uuid4().hex[:12]}"
            async with _session(home, cfg, inst, ev, "client-two-tier") as client:
                turn = record_turn(cfg, ns_tier, SELF_TEXT, occurred_at=SELF_AT)
                admitted = ADMITTED_EPISODE_ID.search(
                    await admit_turn(client, ns_tier, SELF_TEXT, turn))
                client_uuid = admitted.group(1) if admitted else None
                deadline = time.monotonic() + 60
                proj_t = projection_for_turn(cfg, turn)
                while proj_t is None and time.monotonic() < deadline:
                    await asyncio.sleep(2)
                    proj_t = projection_for_turn(cfg, turn)
                proj_state = wait_episode_state(cfg, proj_t["uuid"]) if proj_t else "NO_PROJECTION"
                client_state = wait_episode_state(cfg, client_uuid) if client_uuid else "NO_EPISODE"
                agent_episode = await _track(client, INFERRED_AGENT_TEXT, ns_tier)
                agent_state = wait_episode_state(cfg, agent_episode)
            probe_tier = _two_tier_probe(cfg, ns_tier, client_uuid,
                                         proj_t["uuid"] if proj_t else None)
            ev.record("two_tier_self", passed=(
                proj_state == "READY" and client_state == "READY" and agent_state == "READY"
                and _two_tier_ok(probe_tier)),
                detail={"projection_state": proj_state, "client_state": client_state,
                        "agent_state": agent_state, "probe": probe_tier})
            stop(backend)
            edit_env(home.env_path, remove=TWO_TIER.keys())
        finally:
            for backend_proc in live:
                try:
                    stop(backend_proc)
                except Exception:
                    pass

    close_pass(lane_evidence, CRITERIA)
