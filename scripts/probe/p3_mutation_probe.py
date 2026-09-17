"""Mutation probe: which snapshot guards can be removed without a test noticing?

    python scripts/probe/p3_mutation_probe.py [--limit N] [--module NAME]

A passing suite says the code does what the tests check. It does not say the tests check anything.
This breaks one thing at a time and reports what survives -- a mutation the suite still passes is a
line no test is actually holding.

Two mutation kinds, chosen because they are the ones that matter for safety code rather than for
coverage in general:

* **`raise` removed.** Every guard in these modules is a refusal. Deleting one and seeing green
  means the refusal is decorative.
* **Comparison loosened or inverted.** `>` becomes `>=`, `==` becomes `!=`, and so on. Boundary
  conditions are where limits and leases are actually decided, and an off-by-one in a limit is a
  real class of bug rather than a hypothetical one.

**Nothing is left mutated.** Each mutation is written to the real module path (imports must resolve
there), with the original restored in a `finally`, and the run ends by asserting the working tree
is clean.
"""

from __future__ import annotations

import argparse
import ast
import copy
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

#: The whole snapshot surface. The first run covered only the P3 modules and found twenty gaps in
#: five files, which is the argument for pointing it at the rest: `receive.py` is the durable
#: upload state machine with the most branches and the most adversarial input, and its tests were
#: written before this technique had shown what it catches.
TARGETS = [
    "archive_plan.py",
    "extraction_lease.py",
    "extraction_writer.py",
    "extract_worker.py",
    "shadow_scan.py",
    "receive.py",
    "bundler.py",
    "policy.py",
    "upload_client.py",
    "protocol.py",
]

_FLIP = {
    ast.Gt: ast.GtE,
    ast.GtE: ast.Gt,
    ast.Lt: ast.LtE,
    ast.LtE: ast.Lt,
    ast.Eq: ast.NotEq,
    ast.NotEq: ast.Eq,
    ast.In: ast.NotIn,
    ast.NotIn: ast.In,
    ast.Is: ast.IsNot,
    ast.IsNot: ast.Is,
}


@dataclass
class Mutation:
    module: str
    kind: str
    line: int
    detail: str


def _mutants(tree: ast.AST) -> list[tuple[ast.AST, Mutation]]:
    """Every single-node mutation of `tree`, each as a fresh copy."""
    out: list[tuple[ast.AST, Mutation]] = []

    targets: list[tuple[str, int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Raise):
            targets.append(("raise-removed", node.lineno, "guard deleted"))
        elif isinstance(node, ast.Compare) and node.ops and type(node.ops[0]) in _FLIP:
            op = type(node.ops[0])
            targets.append(
                ("compare-flipped", node.lineno, f"{op.__name__} -> {_FLIP[op].__name__}")
            )

    for kind, line, detail in targets:
        clone = copy.deepcopy(tree)
        applied = False
        for node in ast.walk(clone):
            if applied:
                break
            if kind == "raise-removed" and isinstance(node, ast.Raise) and node.lineno == line:
                _replace_raise(clone, node)
                applied = True
            elif (
                kind == "compare-flipped"
                and isinstance(node, ast.Compare)
                and node.lineno == line
                and node.ops
                and type(node.ops[0]) in _FLIP
            ):
                node.ops[0] = _FLIP[type(node.ops[0])]()
                applied = True
        if applied:
            out.append((clone, Mutation("", kind, line, detail)))
    return out


def _replace_raise(tree: ast.AST, target: ast.Raise) -> None:
    """Swap a `raise` for `pass` in whatever body holds it."""
    for node in ast.walk(tree):
        for field in ("body", "orelse", "finalbody"):
            body = getattr(node, field, None)
            if isinstance(body, list):
                for index, statement in enumerate(body):
                    if statement is target:
                        body[index] = ast.Pass()
                        return


def _run_suite(repo: Path) -> bool:
    """True when the suite passes, i.e. the mutation SURVIVED."""
    result = subprocess.run(
        [
            str(repo / ".venv/Scripts/python.exe"),
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-x",
            "-q",
            "tests/snapshot",
        ],
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    return result.returncode == 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--limit", type=int, default=0, help="Stop after N mutations.")
    parser.add_argument("--module", default="", help="Only mutate this file.")
    args = parser.parse_args(argv)

    repo = Path(__file__).resolve().parents[2]

    # One run at a time, enforced with the same primitive the lease uses, and for the same reason.
    # Two concurrent runs each mutate and restore the SAME files: they overwrite each other's
    # "original", and whichever finishes second restores a mutant. That happened -- a second run
    # was started while the first was still going, and `archive_plan.py` was left with 120 lines
    # deleted. The clean-tree check caught it and nothing reached a commit, but the tool had no
    # business allowing it.
    lock = repo / ".mutation-probe.lock"
    try:
        handle = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        print(
            f"  another mutation run holds {lock.name}. Two runs mutate the same files and will\n"
            "  restore each other's mutants. Wait for it, or delete the lock if it is stale."
        )
        return 2
    os.close(handle)

    survivors: list[Mutation] = []
    killed = 0
    started = time.perf_counter()

    for name in TARGETS:
        if args.module and args.module not in name:
            continue
        path = repo / "src" / "menhir" / "snapshot" / name
        # Bytes, not text. `write_text` translates newlines to os.linesep, so restoring a file
        # read as text rewrote every line ending on Windows and left three modules "modified"
        # in git with byte-identical content. The clean-tree check caught it, which is the
        # check earning its place -- but a tool whose job is to break things must restore what
        # it found, exactly.
        original = path.read_bytes()
        tree = ast.parse(original.decode("utf-8"))

        for clone, mutation in _mutants(tree):
            if args.limit and (killed + len(survivors)) >= args.limit:
                break
            mutation.module = name
            try:
                path.write_bytes(ast.unparse(clone).encode("utf-8"))
            except Exception:  # noqa: BLE001 -- an unparseable mutant is not a finding
                path.write_bytes(original)
                continue
            try:
                survived = _run_suite(repo)
            finally:
                # Restored here, always. A mutated module left in the tree would be the worst
                # possible outcome of a tool whose entire job is to break things on purpose.
                path.write_bytes(original)

            if survived:
                survivors.append(mutation)
                print(f"  SURVIVED  {name}:{mutation.line}  {mutation.kind}  {mutation.detail}")
            else:
                killed += 1

    lock.unlink(missing_ok=True)

    total = killed + len(survivors)
    elapsed = time.perf_counter() - started
    print()
    print(f"  {total} mutations in {elapsed:.0f}s: {killed} killed, {len(survivors)} survived")
    if survivors:
        print("\n  Survivors are lines no test is holding:")
        for m in survivors:
            print(f"    {m.module}:{m.line}  {m.kind}  {m.detail}")

    if any(m.module == "upload_client.py" for m in survivors):
        print(
            "\n  NOTE: `upload_client.py` is exercised by `tests/remote_sim`, which needs Docker\n"
            "  and is NOT in the suite this probe runs. Its survivors mean 'not covered by the\n"
            "  offline suite', which is worth knowing and is NOT the same as 'untested anywhere'."
        )

    dirty = subprocess.run(
        ["git", "status", "--porcelain", "src/menhir/snapshot"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    if dirty:
        print(f"\n  ERROR: mutated files left in the tree:\n{dirty}")
        return 2
    print("  Working tree clean: every mutation was reverted.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
