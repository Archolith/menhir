"""Compare two difftest runs: stats, replay misses, graph dump and the printed session report.

Usage: compare.py RUNS_DIR A B   (reads RUNS_DIR/out-A and RUNS_DIR/out-B)
"""

from __future__ import annotations

import difflib
import json
import re
import sys
from pathlib import Path

D = Path(".")


_RUN_TOKENS = re.compile(
    r"Menhir[A-Za-z]+_[0-9a-f]{12,}"
    r"|\b(?:acme|other|sess-[ab]|p)-[0-9a-f]{8}\b"
    r"|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"|\"turn_key\": \"[0-9a-f]{64}\""
    r"|20\d\d-\d\d-\d\d[T ]\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:?\d\d)?"
)
_FIXED_DATES = re.compile(r"^(2020-01-01|2025-)")


def _mask(text: str) -> str:
    def sub(m: re.Match) -> str:
        tok = m.group(0)
        if tok.startswith("Menhir"):
            return "<MARKER>"
        if tok.startswith('"turn_key"'):
            return '"turn_key": "<HASH>"'
        if re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", tok
        ):
            return "<UUID>"
        if re.fullmatch(r"(?:acme|other|sess-[ab]|p)-[0-9a-f]{8}", tok):
            return tok.rsplit("-", 1)[0] + "-<RUN>"
        return tok if _FIXED_DATES.match(tok) else "<NOW>"

    return _RUN_TOKENS.sub(sub, text)


# Environment noise from the test's temp project and wall clock, not graph behaviour.
_ENV_KEYS = {
    "turn_key",
    "canonical_root_path",
    "root_path",
    "root_key",
    "last_scan_probe",
    "last_publication_probe",
    "last_admission_probe",
    "last_structure_writer_id",
    "lock_nonce",
    "file_mtime",
    "scan_fingerprint",
}
_PROJECT = re.compile(r"\b([a-z]+)-[0-9a-f]{8}\b")


def _clean_item(item: str) -> str:
    data = json.loads(_PROJECT.sub(r"\1-<RUN>", item))
    for key in _ENV_KEYS:
        data.pop(key, None)
    for key, value in data.items():
        if isinstance(value, list) and all(isinstance(v, str) for v in value):
            data[key] = sorted(value)
    return json.dumps(data, sort_keys=True)


def load(name: str) -> dict:
    out = D / f"out-{name}"
    graph = (
        json.loads(_mask((out / "graph.json").read_text(encoding="utf-8")))
        if (out / "graph.json").exists()
        else None
    )
    if graph is not None:
        graph = {part: [_clean_item(i) for i in items] for part, items in graph.items()}
    stats = (
        json.loads((out / "stats.json").read_text(encoding="utf-8"))
        if (out / "stats.json").exists()
        else {}
    )
    log = (
        (out / "pytest.log").read_text(encoding="utf-8", errors="replace")
        if (out / "pytest.log").exists()
        else ""
    )
    misses = (
        sorted((out / "misses").glob("*.json")) if (out / "misses").exists() else []
    )
    return {"graph": graph, "stats": stats, "log": log, "misses": misses, "out": out}


_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_NUM_TIME = re.compile(
    r"\b\d+(\.\d+)?\s*(ms|s)\b|\d{4}-\d\d-\d\dT[\d:.]+(Z|[+-]\d\d:\d\d)?"
)


def report_lines(log: str) -> list[str]:
    lines = []
    for line in log.splitlines():
        line = _NUM_TIME.sub("<t>", _UUID.sub("<uuid>", _mask(line)))
        if (
            "PASSED" in line
            or "passed" in line
            or "failed" in line
            or "PydanticDeprecatedSince20" in line
        ):
            continue
        lines.append(line)
    return lines


def miss_summary(paths: list[Path]) -> list[str]:
    rows = []
    for p in paths:
        try:
            body = json.loads(p.read_bytes() or b"{}")
        except ValueError:
            rows.append(f"{p.name}: <non-json body>")
            continue
        model = body.get("model")
        text = json.dumps(body.get("messages") or body.get("input") or "")[:160]
        rows.append(f"{p.name}: model={model} {text}")
    return rows


def compare(a_name: str, b_name: str) -> None:
    a, b = load(a_name), load(b_name)
    print(f"===== {a_name} vs {b_name}")
    for n, r in ((a_name, a), (b_name, b)):
        s = r["stats"]
        print(
            f"{n}: exit={s.get('exitstatus')} hits={s.get('hits')} misses={s.get('misses')} "
            f"live_calls={s.get('live_calls')} spend=${s.get('spend_usd', 0):.4f} paths={s.get('by_path')}"
        )
    if b["misses"]:
        print(f"-- {len(b['misses'])} replay misses in {b_name} (first 15):")
        for row in miss_summary(b["misses"])[:15]:
            print("  ", row)
    if a["graph"] is None or b["graph"] is None:
        print(
            "-- graph dump missing:",
            [n for n, r in ((a_name, a), (b_name, b)) if r["graph"] is None],
        )
    else:
        for part in ("nodes", "rels"):
            sa, sb = set(a["graph"][part]), set(b["graph"][part])
            print(
                f"-- {part}: {len(sa)} vs {len(sb)}; only in {a_name}: {len(sa - sb)}; only in {b_name}: {len(sb - sa)}"
            )
            for item in sorted(sa - sb)[:20]:
                print(f"   - {item[:400]}")
            for item in sorted(sb - sa)[:20]:
                print(f"   + {item[:400]}")
    diff = list(
        difflib.unified_diff(
            report_lines(a["log"]),
            report_lines(b["log"]),
            a_name,
            b_name,
            n=0,
            lineterm="",
        )
    )
    print(f"-- session report diff lines: {len(diff)}")
    for line in diff[:80]:
        print("  ", line[:300])


if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise SystemExit("usage: compare.py RUNS_DIR A B")
    D = Path(sys.argv[1])
    compare(sys.argv[2], sys.argv[3])
