"""Aggregate Recall Lab judge dimensions and expose tuning gaps for one arm.

The judge's ``average_scores`` already averages repeated blinded passes within a
run. This script gives every usable query equal weight when averaging those
scores across runs.

Examples:

    python scripts/analyze_recall_lab_scores.py --from-id 34 --to-id 75 --latest-per-query
    python scripts/analyze_recall_lab_scores.py --focus e --baseline production --worst 10
    python scripts/analyze_recall_lab_scores.py --json > recall-lab-scores.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

SCORE_DIMENSIONS = (
    "relevance",
    "completeness",
    "ranking",
    "noise_control",
    "omission_safety",
)

DIMENSION_LABELS = {
    "relevance": "relevance",
    "completeness": "complete",
    "ranking": "ranking",
    "noise_control": "noise",
    "omission_safety": "omissions",
}

LEVER_HINTS = {
    "relevance": (
        "Candidate mix: test an E variant with attributed BM25/content-vector candidates; "
        "then sweep facet_weight only if relevant candidates are already present."
    ),
    "completeness": (
        "Gate selectivity: inspect E's warden verdicts and relax the sub-warden dropping "
        "otherwise relevant candidates; keep evidence-anchor soft."
    ),
    "ranking": (
        "Ordering: sweep facet_weight and oracle ranking independently, using the same "
        "candidate pool so candidate coverage does not confound the result."
    ),
    "noise_control": (
        "Precision: tune warden/oracle thresholds, but do not tighten them while completeness "
        "or omission safety is already below Production."
    ),
    "omission_safety": (
        "Recall safety: reduce hard warden drops or convert them to score penalties; E already "
        "disables the evidence-anchor hard gate, so inspect the remaining warden chain."
    ),
}


@dataclass(frozen=True)
class Observation:
    run_id: int
    query: str
    scores: dict[str, dict[str, float]]
    hits: dict[str, int]
    labels: dict[str, str]


def _get_json(url: str, *, timeout: float) -> dict[str, Any]:
    request = Request(url, headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except HTTPError as exc:
        raise RuntimeError(f"Recall Lab returned HTTP {exc.code} for {url}") from exc
    except URLError as exc:
        raise RuntimeError(f"Recall Lab is unavailable at {url}: {exc.reason}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"Recall Lab returned a non-object payload for {url}")
    return payload


def observation_from_detail(detail: dict[str, Any]) -> tuple[Observation | None, str | None]:
    """Return one usable judged observation, or a stable skip reason."""
    result = detail.get("result")
    if not isinstance(result, dict):
        return None, "missing_result"
    judgment = result.get("judgment")
    if not isinstance(judgment, dict) or not judgment.get("ok"):
        return None, "judge_not_ok"

    arms = result.get("arms")
    if not isinstance(arms, list) or not arms:
        return None, "missing_arms"
    if any(not arm.get("ok") or arm.get("degraded") for arm in arms if isinstance(arm, dict)):
        return None, "unhealthy_arm"

    raw_scores = judgment.get("average_scores")
    if not isinstance(raw_scores, dict) or not raw_scores:
        return None, "missing_scores"

    scores: dict[str, dict[str, float]] = {}
    for arm_id, raw_dimensions in raw_scores.items():
        if not isinstance(raw_dimensions, dict):
            return None, "invalid_scores"
        dimensions: dict[str, float] = {}
        for dimension in SCORE_DIMENSIONS:
            value = raw_dimensions.get(dimension)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                return None, "invalid_scores"
            numeric = float(value)
            if not 0.0 <= numeric <= 5.0:
                return None, "invalid_scores"
            dimensions[dimension] = numeric
        scores[str(arm_id)] = dimensions

    labels: dict[str, str] = {}
    hits: dict[str, int] = {}
    for arm in arms:
        if not isinstance(arm, dict):
            continue
        arm_id = str(arm.get("id") or "")
        if not arm_id:
            continue
        labels[arm_id] = str(arm.get("label") or arm_id)
        results = arm.get("results")
        hits[arm_id] = len(results) if isinstance(results, list) else 0

    try:
        run_id = int(detail["id"])
    except (KeyError, TypeError, ValueError):
        return None, "invalid_run_id"
    query = str(detail.get("request", {}).get("query") or result.get("query") or "").strip()
    if not query:
        return None, "missing_query"
    return Observation(run_id=run_id, query=query, scores=scores, hits=hits, labels=labels), None


def latest_observation_per_query(observations: list[Observation]) -> list[Observation]:
    """Collapse retries while retaining the newest usable grade for each exact query."""
    latest = {observation.query: observation for observation in sorted(observations, key=lambda row: row.run_id)}
    return sorted(latest.values(), key=lambda row: row.run_id)


def build_report(
    observations: list[Observation],
    *,
    focus: str = "e",
    baseline: str = "production",
    worst: int = 5,
) -> dict[str, Any]:
    if not observations:
        raise ValueError("no usable Recall Lab observations")

    dimension_values: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    hit_values: dict[str, list[int]] = defaultdict(list)
    labels: dict[str, str] = {}
    winner_counts: Counter[str] = Counter()
    focus_query_gaps: list[dict[str, Any]] = []

    for observation in observations:
        labels.update(observation.labels)
        for arm_id, dimensions in observation.scores.items():
            for dimension, value in dimensions.items():
                dimension_values[arm_id][dimension].append(value)
            if arm_id in observation.hits:
                hit_values[arm_id].append(observation.hits[arm_id])

        if focus in observation.scores and baseline in observation.scores:
            focus_composite = sum(observation.scores[focus].values()) / len(SCORE_DIMENSIONS)
            baseline_composite = sum(observation.scores[baseline].values()) / len(SCORE_DIMENSIONS)
            dimension_gaps = {
                dimension: observation.scores[focus][dimension] - observation.scores[baseline][dimension]
                for dimension in SCORE_DIMENSIONS
            }
            focus_query_gaps.append(
                {
                    "run_id": observation.run_id,
                    "query": observation.query,
                    "focus_composite": round(focus_composite, 3),
                    "baseline_composite": round(baseline_composite, 3),
                    "delta": round(focus_composite - baseline_composite, 3),
                    "dimension_gaps": {key: round(value, 3) for key, value in dimension_gaps.items()},
                }
            )

        composites = {
            arm_id: sum(dimensions.values()) / len(SCORE_DIMENSIONS)
            for arm_id, dimensions in observation.scores.items()
        }
        if composites:
            best = max(composites.values())
            for arm_id, value in composites.items():
                if abs(value - best) < 1e-9:
                    winner_counts[arm_id] += 1

    arms: dict[str, dict[str, Any]] = {}
    for arm_id, values_by_dimension in dimension_values.items():
        averages = {
            dimension: sum(values_by_dimension[dimension]) / len(values_by_dimension[dimension])
            for dimension in SCORE_DIMENSIONS
        }
        arms[arm_id] = {
            "label": labels.get(arm_id, arm_id),
            "samples": len(values_by_dimension[SCORE_DIMENSIONS[0]]),
            "scores": {key: round(value, 3) for key, value in averages.items()},
            "composite": round(sum(averages.values()) / len(SCORE_DIMENSIONS), 3),
            "average_hits": round(sum(hit_values[arm_id]) / len(hit_values[arm_id]), 3)
            if hit_values[arm_id]
            else None,
            "best_composite_queries": winner_counts[arm_id],
        }

    comparison: dict[str, Any] | None = None
    if focus in arms and baseline in arms:
        gaps = {
            dimension: arms[focus]["scores"][dimension] - arms[baseline]["scores"][dimension]
            for dimension in SCORE_DIMENSIONS
        }
        ordered_dimensions = sorted(
            SCORE_DIMENSIONS,
            key=lambda dimension: (gaps[dimension], arms[focus]["scores"][dimension]),
        )
        comparison = {
            "focus": focus,
            "baseline": baseline,
            "dimension_gaps": {key: round(gaps[key], 3) for key in SCORE_DIMENSIONS},
            "priority_order": ordered_dimensions,
            "lever_hints": {dimension: LEVER_HINTS[dimension] for dimension in ordered_dimensions},
            "worst_queries": sorted(focus_query_gaps, key=lambda row: (row["delta"], row["run_id"]))[
                : max(0, worst)
            ],
        }

    return {
        "usable_runs": len(observations),
        "run_ids": [row.run_id for row in observations],
        "arms": arms,
        "comparison": comparison,
    }


def fetch_observations(
    *,
    base_url: str,
    limit: int,
    from_id: int | None,
    to_id: int | None,
    timeout: float,
) -> tuple[list[Observation], Counter[str]]:
    root = base_url.rstrip("/") + "/"
    history = _get_json(urljoin(root, f"explorer/api/recall-lab/history?limit={limit}"), timeout=timeout)
    rows = history.get("runs")
    if not isinstance(rows, list):
        raise RuntimeError("Recall Lab history response did not contain a runs list")

    observations: list[Observation] = []
    skipped: Counter[str] = Counter()
    for row in sorted(rows, key=lambda item: int(item.get("id", 0))):
        try:
            run_id = int(row["id"])
        except (KeyError, TypeError, ValueError):
            skipped["invalid_run_id"] += 1
            continue
        if from_id is not None and run_id < from_id:
            continue
        if to_id is not None and run_id > to_id:
            continue
        detail = _get_json(urljoin(root, f"explorer/api/recall-lab/history/{run_id}"), timeout=timeout)
        observation, reason = observation_from_detail(detail)
        if observation is None:
            skipped[reason or "unknown"] += 1
        else:
            observations.append(observation)
    return observations, skipped


def _format_text(report: dict[str, Any], skipped: Counter[str]) -> str:
    lines = [
        f"Recall Lab score averages: {report['usable_runs']} usable query grades",
        "",
        "arm          n    relevance  complete  ranking  noise  omissions  composite  avg hits  query-best",
    ]
    preferred_order = ["production", "a", "b", "c", "d", "e"]
    arm_ids = sorted(report["arms"], key=lambda arm_id: (preferred_order.index(arm_id) if arm_id in preferred_order else 99, arm_id))
    for arm_id in arm_ids:
        arm = report["arms"][arm_id]
        scores = arm["scores"]
        lines.append(
            f"{arm_id:<11} {arm['samples']:>3}  {scores['relevance']:>9.3f}  {scores['completeness']:>8.3f}  "
            f"{scores['ranking']:>7.3f}  {scores['noise_control']:>5.3f}  {scores['omission_safety']:>9.3f}  "
            f"{arm['composite']:>9.3f}  {arm['average_hits']:>8.2f}  {arm['best_composite_queries']:>10}"
        )

    comparison = report.get("comparison")
    if comparison:
        focus = comparison["focus"]
        baseline = comparison["baseline"]
        lines.extend(["", f"{focus.upper()} lever view (delta vs {baseline}; negative means {focus.upper()} is worse):"])
        for dimension in comparison["priority_order"]:
            focus_score = report["arms"][focus]["scores"][dimension]
            baseline_score = report["arms"][baseline]["scores"][dimension]
            gap = comparison["dimension_gaps"][dimension]
            lines.append(
                f"  {DIMENSION_LABELS[dimension]:<10} {focus_score:.3f} vs {baseline_score:.3f} "
                f"({gap:+.3f}) - {comparison['lever_hints'][dimension]}"
            )
        if comparison["worst_queries"]:
            lines.extend(["", f"Worst {focus.upper()} query gaps:"])
            for row in comparison["worst_queries"]:
                lines.append(f"  run {row['run_id']:>4}  {row['delta']:+.3f}  {row['query']}")

    if skipped:
        rendered = ", ".join(f"{key}={value}" for key, value in sorted(skipped.items()))
        lines.extend(["", f"Skipped runs: {rendered}"])
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--base-url",
        default=os.getenv("MENHIR_RECALL_LAB_URL", "http://127.0.0.1:8090"),
        help="Menhir server root URL (default: %(default)s)",
    )
    parser.add_argument("--limit", type=int, default=500, help="Maximum history rows to request")
    parser.add_argument("--from-id", type=int, help="Include runs at or after this id")
    parser.add_argument("--to-id", type=int, help="Include runs at or before this id")
    parser.add_argument(
        "--latest-per-query",
        action="store_true",
        help="Keep the newest usable grade for each exact query, collapsing judge retries",
    )
    parser.add_argument("--focus", default="e", help="Arm to improve (default: e)")
    parser.add_argument("--baseline", default="production", help="Comparison arm (default: production)")
    parser.add_argument("--worst", type=int, default=5, help="Number of worst focus-arm query gaps to show")
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout per request")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.limit < 1 or args.limit > 500:
        raise SystemExit("--limit must be between 1 and 500")
    if args.from_id is not None and args.to_id is not None and args.from_id > args.to_id:
        raise SystemExit("--from-id must be less than or equal to --to-id")

    try:
        observations, skipped = fetch_observations(
            base_url=args.base_url,
            limit=args.limit,
            from_id=args.from_id,
            to_id=args.to_id,
            timeout=args.timeout,
        )
        if args.latest_per_query:
            observations = latest_observation_per_query(observations)
        report = build_report(observations, focus=args.focus, baseline=args.baseline, worst=args.worst)
    except (RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps({**report, "skipped": dict(skipped)}, indent=2, sort_keys=True))
    else:
        print(_format_text(report, skipped))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
