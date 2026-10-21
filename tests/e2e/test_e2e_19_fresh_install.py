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

Each stock-client launch is a new MCP session, and a fresh write is SESSION-scoped until it is
promoted, so a later session gets no ranked items for it; only the source-memory section carries
it. Steps that need the Warden to rank something therefore write in the session they recall from.

Canonical-self binding is probed but is not a criterion: the deterministic provider never emits
the self-subject marker, so no binding can happen through it. Proving it needs a real model.
"""

from __future__ import annotations

import json
import os
import time
from uuid import uuid4

import pytest

from menhir.domain.self_identity import self_uuid_for_namespace
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
    wait_episode_state,
)
from tests.e2e._harness.phase3 import admit_turn, close_pass, record_turn
from tests.e2e._harness.providers import deterministic_provider, local_provider_environment
from tests.e2e._harness.stack import graph_query
from tests.e2e.test_e2e_02_memory_lifecycle import (
    CORRECTED_AMOUNT,
    CORRECTION,
    EPISODE_ID,
    ORIGINAL,
    ORIGINAL_AMOUNT,
)

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(2400)]

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
]

OPT_OUT = {
    "MENHIR_FRONTIER_WARDEN_GATE": "false",
    "MENHIR_FRONTIER_CONTRADICTION_INTERRUPT": "false",
    "MENHIR_FRONTIER_SOURCE_MEMORIES": "false",
    "MENHIR_CANONICAL_SELF_BINDING_MODE": "off",
}

QUERY = "Atlas Lantern refund approval threshold"
SELF_TEXT = "I own exactly 37 cobalt postcards in the north cabinet."
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
        time.sleep(2)
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
            edit_env(home.env_path, set_values={
                **local_provider_environment(base_url),
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

                # 7. canonical self: probe only (see module docstring)
                ns_self = f"e2e19-self-{uuid4().hex[:12]}"
                proj = await _admit_self(cfg, client, ns_self)
                self_state = wait_episode_state(cfg, proj["uuid"]) if proj else "NO_PROJECTION"
                ev.record_stack(canonical_self_probe_enforce={
                    "projection_state": self_state, "self_rows": _self_rows(cfg, ns_self)})

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
                r, _, state_d = await _write_then_recall(cfg, client, "e2e19-off")
                ws_off = r.get("warden_status") or {}
                proj_off = await _admit_self(cfg, client, ns_off)
                off_state = wait_episode_state(cfg, proj_off["uuid"]) if proj_off else "NO_PROJECTION"
                ev.record_stack(canonical_self_probe_off={
                    "projection_state": off_state, "self_rows": _self_rows(cfg, ns_off)})
            opted_out = effective_settings(cfg, inst, home)["values"]
            part_a = (state_d == "READY" and ws_off.get("state") == "disabled"
                      and not r.get("source_memories") and _ranked_has(r, ORIGINAL_AMOUNT)
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
                              "source_memories": bool(r.get("source_memories")),
                              "binding_mode": opted_out.get("MENHIR_CANONICAL_SELF_BINDING_MODE")},
                "restored": {"ok": part_b, "episode_state": state_e, "warden_status": ws_on,
                             "settings_equal_defaults": restored == code_defaults()}})
        finally:
            for backend_proc in live:
                try:
                    stop(backend_proc)
                except Exception:
                    pass

    close_pass(lane_evidence, CRITERIA)
