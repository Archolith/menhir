#!/usr/bin/env python3
"""Benchmark token cost for core .agent docs vs user-readable docs."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_ENCODING = "o200k_base"


@dataclass(frozen=True)
class CounterInfo:
    method: str
    encoding: str


@dataclass(frozen=True)
class ScenarioPaths:
    route: list[str]
    followup: list[str]


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    description: str
    prompt: str
    agent: ScenarioPaths
    user: ScenarioPaths


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Compare token cost for core .agent routing versus .agent/user routing "
            "across task-shaped documentation scenarios."
        )
    )
    parser.add_argument(
        "--scenarios",
        default=str(Path(__file__).resolve().parents[1] / "doc_token_scenarios.json"),
        help="Path to the scenario JSON file.",
    )
    parser.add_argument(
        "--scenario",
        action="append",
        dest="scenario_ids",
        help="Benchmark only the named scenario id. Repeat to select more than one.",
    )
    parser.add_argument(
        "--encoding",
        default=DEFAULT_ENCODING,
        help="Tokenizer encoding to use with tiktoken. Default: %(default)s.",
    )
    parser.add_argument(
        "--tokenizer",
        choices=("auto", "tiktoken", "estimate"),
        default="auto",
        help="Tokenizer mode. Default: %(default)s.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of the table view.",
    )
    parser.add_argument(
        "--warm",
        action="store_true",
        help=(
            "Assume common bootstrap docs are already loaded and exclude them from "
            "scenario route totals."
        ),
    )
    parser.add_argument(
        "--show-prompts",
        action="store_true",
        help="Include the scenario prompt text in the table output.",
    )
    return parser.parse_args()


def _load_scenarios(path: Path) -> tuple[dict[str, list[str]], list[Scenario]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    common = {
        "agent": raw.get("common", {}).get("agent", []),
        "user": raw.get("common", {}).get("user", []),
    }
    scenarios: list[Scenario] = []
    for item in raw["scenarios"]:
        scenarios.append(
            Scenario(
                scenario_id=item["id"],
                description=item["description"],
                prompt=item["prompt"],
                agent=ScenarioPaths(
                    route=item["agent"]["route"],
                    followup=item["agent"].get("followup", []),
                ),
                user=ScenarioPaths(
                    route=item["user"]["route"],
                    followup=item["user"].get("followup", []),
                ),
            )
        )
    return common, scenarios


def _prepare_local_temp(base_dir: Path) -> Path:
    temp_dir = base_dir / "test_tmp" / "tiktoken"
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_value = str(temp_dir)
    os.environ["TMPDIR"] = temp_value
    os.environ["TEMP"] = temp_value
    os.environ["TMP"] = temp_value
    tempfile.tempdir = temp_value
    return temp_dir


def _build_counter(mode: str, encoding_name: str, base_dir: Path) -> tuple[CounterInfo, Any]:
    if mode in {"auto", "tiktoken"}:
        _prepare_local_temp(base_dir)
        try:
            import tiktoken  # type: ignore
        except ImportError:
            if mode == "tiktoken":
                raise SystemExit(
                    "tiktoken is not installed. Install it or use --tokenizer estimate."
                ) from None
        else:
            try:
                encoding = tiktoken.get_encoding(encoding_name)
            except Exception as exc:
                if mode == "tiktoken":
                    raise SystemExit(
                        f"tiktoken could not initialize encoding {encoding_name!r}: {exc}"
                    ) from exc
            else:
                return CounterInfo(method="tiktoken", encoding=encoding_name), encoding.encode

    return (
        CounterInfo(method="estimate", encoding="chars_div_4"),
        lambda text: [None] * max(1, round(len(text) / 4)),
    )


def _count_tokens(file_path: Path, encode: Any) -> int:
    text = file_path.read_text(encoding="utf-8")
    return len(encode(text))


def _totals(base_dir: Path, files: list[str], encode: Any) -> dict[str, Any]:
    expanded: list[dict[str, Any]] = []
    total = 0
    for relative in files:
        path = base_dir / relative
        if not path.exists():
            raise SystemExit(f"Missing benchmark file: {path}")
        tokens = _count_tokens(path, encode)
        expanded.append({"path": relative, "tokens": tokens})
        total += tokens
    return {"files": expanded, "total_tokens": total}


def _diff(agent_total: int, user_total: int) -> dict[str, Any]:
    diff = agent_total - user_total
    percent = None if user_total == 0 else (diff / user_total) * 100.0
    return {"agent_minus_user_tokens": diff, "agent_minus_user_percent": percent}


def _strip_common(files: list[str], common_files: set[str]) -> list[str]:
    return [path for path in files if path not in common_files]


def _format_diff(delta: dict[str, Any]) -> str:
    percent = delta["agent_minus_user_percent"]
    if percent is None:
        return f"{delta['agent_minus_user_tokens']} (n/a)"
    return f"{delta['agent_minus_user_tokens']} ({percent:.1f}%)"


def _render_table(
    rows: list[dict[str, Any]],
    counter: CounterInfo,
    warm: bool,
    show_prompts: bool,
) -> str:
    headers = []
    if show_prompts:
        headers.append("prompt")
    headers.extend([
        "scenario",
        "agent_route",
        "user_route",
        "route_diff",
        "agent_full",
        "user_full",
        "full_diff",
    ])
    widths = {header: len(header) for header in headers}
    for row in rows:
        for header in headers:
            widths[header] = max(widths[header], len(str(row[header])))

    def render_row(row: dict[str, Any]) -> str:
        return "  ".join(str(row[header]).ljust(widths[header]) for header in headers)

    lines = [
        f"tokenizer={counter.method} encoding={counter.encoding} mode={'warm' if warm else 'cold'}",
        render_row({header: header for header in headers}),
        render_row({header: "-" * widths[header] for header in headers}),
    ]
    lines.extend(render_row(row) for row in rows)
    return "\n".join(lines)


def main() -> int:
    args = _parse_args()
    scenario_path = Path(args.scenarios).resolve()
    base_dir = scenario_path.parent
    common, scenarios = _load_scenarios(scenario_path)
    if args.scenario_ids:
        wanted = set(args.scenario_ids)
        scenarios = [scenario for scenario in scenarios if scenario.scenario_id in wanted]
        if not scenarios:
            raise SystemExit("No matching scenarios.")

    counter, encode = _build_counter(args.tokenizer, args.encoding, base_dir)
    common_agent = set(common["agent"]) if args.warm else set()
    common_user = set(common["user"]) if args.warm else set()

    report_rows: list[dict[str, Any]] = []
    report_json: dict[str, Any] = {
        "tokenizer": counter.method,
        "encoding": counter.encoding,
        "mode": "warm" if args.warm else "cold",
        "common": common if args.warm else {"agent": [], "user": []},
        "scenarios": [],
    }

    for scenario in scenarios:
        agent_route = _totals(
            base_dir,
            _strip_common(scenario.agent.route, common_agent),
            encode,
        )
        agent_followup = _totals(base_dir, scenario.agent.followup, encode)
        user_route = _totals(
            base_dir,
            _strip_common(scenario.user.route, common_user),
            encode,
        )
        user_followup = _totals(base_dir, scenario.user.followup, encode)

        agent_full = agent_route["total_tokens"] + agent_followup["total_tokens"]
        user_full = user_route["total_tokens"] + user_followup["total_tokens"]
        route_delta = _diff(agent_route["total_tokens"], user_route["total_tokens"])
        full_delta = _diff(agent_full, user_full)

        report_rows.append(
            {
                "prompt": scenario.prompt,
                "scenario": scenario.scenario_id,
                "agent_route": agent_route["total_tokens"],
                "user_route": user_route["total_tokens"],
                "route_diff": _format_diff(route_delta),
                "agent_full": agent_full,
                "user_full": user_full,
                "full_diff": _format_diff(full_delta),
            }
        )
        report_json["scenarios"].append(
            {
                "id": scenario.scenario_id,
                "description": scenario.description,
                "prompt": scenario.prompt,
                "agent": {
                    "route": agent_route,
                    "followup": agent_followup,
                    "full_total_tokens": agent_full,
                },
                "user": {
                    "route": user_route,
                    "followup": user_followup,
                    "full_total_tokens": user_full,
                },
                "route_delta": route_delta,
                "full_delta": full_delta,
            }
        )

    if args.json:
        print(json.dumps(report_json, indent=2))
    else:
        print(_render_table(report_rows, counter, args.warm, args.show_prompts))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
