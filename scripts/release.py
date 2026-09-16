"""Release gate: refuse to tag unless every check a release depends on is green.

Why this exists: v0.2.1 and v0.2.2 were tagged from a workstation whose local suite was
green while `tests.yml` was red on the same SHA (ruff F821 caught a NameError the offline
suite could not see), and `publish-pypi.yml` published anyway. The workflow now gates on
CI, but the tag was still cut blind. This script is the workstation half of that gate.

Default mode is check-only. Nothing is tagged or pushed unless `--tag` is passed, and
`--tag` refuses unless every check below passed in this same run.

Checks (all mandatory; there is no flag that skips one and still allows `--tag`):

  1. tree        working tree clean (no modified, staged, or deleted tracked files)
  2. branch      on `main`, HEAD pushed and equal to `origin/main` after a fetch
  3. version     pyproject version == requested tag, tag does not exist locally or on
                 the remote, and version is greater than the newest existing v* tag
  4. changelog   CHANGELOG.md has a heading naming the release version
  5. deps        no dependency resolved from a repository (mirrors publish-pypi.yml)
  6. lint        the exact two ruff commands tests.yml runs
  7. offline     `pytest -p no:cacheprovider` (the offline job)
  8. online      `pytest -m "online and not needs_llm" --run-online` against the throwaway
                 test Neo4j (the online job); an UNREACHABLE test instance is a failure,
                 not a skip -- an unexecuted online lane is exactly how v0.2.1 shipped
  9. ci          every `tests` workflow run for HEAD's SHA is completed and green (waits
                 while runs are in progress)

Usage:
    python scripts/release.py v0.2.4            # check only, prints a summary table
    python scripts/release.py v0.2.4 --tag      # checks, then annotated tag + push + watch
    python scripts/release.py v0.2.4 --only lint,offline    # rerun a subset while iterating

Environment: MENHIR_TEST_NEO4J_URI / _USER / _PASSWORD / _DATABASE for the online lane
(defaults match tests.yml: bolt://localhost:7688, neo4j, testpassword, neo4j).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PY = sys.executable
GH_REPO = "Archolith/menhir"
TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
PYPI_PROJECT = "archolith-menhir"

# 127.0.0.1, not localhost: the throwaway container publishes on 0.0.0.0 only, and on Windows
# every driver connect to `localhost` tries [::1] first and waits out a ~21s TCP timeout
# before falling back -- 357 online tests x 21s turned a 3-minute lane into two hours.
ONLINE_ENV_DEFAULTS = {
    "MENHIR_TEST_NEO4J_URI": "bolt://127.0.0.1:7688",
    "MENHIR_TEST_NEO4J_USER": "neo4j",
    "MENHIR_TEST_NEO4J_PASSWORD": "testpassword",
    "MENHIR_TEST_NEO4J_DATABASE": "neo4j",
}

CHECK_ORDER = (
    "tree",
    "branch",
    "version",
    "changelog",
    "deps",
    "lint",
    "offline",
    "online",
    "ci",
)


class CheckFailed(Exception):
    pass


@dataclass
class Result:
    name: str
    ok: bool
    detail: str
    seconds: float


@dataclass
class Context:
    tag: str
    version: str
    head: str = ""
    results: list[Result] = field(default_factory=list)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def run(
    cmd: list[str],
    *,
    check: bool = True,
    env: dict[str, str] | None = None,
    timeout: int | None = None,
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        cmd,
        cwd=REPO,
        text=True,
        capture_output=True,
        env={**os.environ, **(env or {})},
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
    )
    if check and proc.returncode != 0:
        out = (proc.stdout or "") + (proc.stderr or "")
        raise CheckFailed(f"`{' '.join(cmd)}` exited {proc.returncode}\n{out.strip()[-4000:]}")
    return proc


def git(*args: str) -> str:
    return run(["git", *args]).stdout.strip()


def gh_json(*args: str) -> list[dict]:
    proc = run(["gh", *args, "-R", GH_REPO])
    return json.loads(proc.stdout or "[]")


def tail(text: str, lines: int = 3) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------


def check_tree(ctx: Context) -> str:
    status = git("status", "--porcelain", "--untracked-files=no")
    if status:
        raise CheckFailed("working tree has uncommitted changes:\n" + status)
    untracked = git("status", "--porcelain", "--untracked-files=normal")
    n_untracked = sum(1 for line in untracked.splitlines() if line.startswith("??"))
    return f"clean ({n_untracked} untracked, ignored by the gate)"


def check_branch(ctx: Context) -> str:
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    if branch != "main":
        raise CheckFailed(f"on {branch!r}; releases are cut from main")
    run(["git", "fetch", "--quiet", "origin", "main", "--tags"])
    head = git("rev-parse", "HEAD")
    remote = git("rev-parse", "origin/main")
    if head != remote:
        ahead = git("rev-list", "--count", "origin/main..HEAD")
        behind = git("rev-list", "--count", "HEAD..origin/main")
        raise CheckFailed(
            f"HEAD {head[:8]} != origin/main {remote[:8]} (ahead {ahead}, behind {behind}); "
            "push or pull first -- CI must have run on exactly this commit"
        )
    ctx.head = head
    return f"main @ {head[:8]}, in sync with origin"


def check_version(ctx: Context) -> str:
    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    if project["version"] != ctx.version:
        raise CheckFailed(
            f"pyproject version is {project['version']!r}, tag {ctx.tag} needs {ctx.version!r}"
        )
    if git("tag", "-l", ctx.tag):
        raise CheckFailed(f"tag {ctx.tag} already exists locally")
    if git("ls-remote", "--tags", "origin", ctx.tag):
        raise CheckFailed(f"tag {ctx.tag} already exists on origin")
    existing = [
        tuple(int(p) for p in m.groups())
        for t in git("tag", "-l", "v*").splitlines()
        if (m := TAG_RE.match(t))
    ]
    requested = tuple(int(p) for p in TAG_RE.match(ctx.tag).groups())  # type: ignore[union-attr]
    newest = max(existing) if existing else None
    if newest is not None and requested <= newest:
        raise CheckFailed(f"{ctx.tag} is not newer than existing v{'.'.join(map(str, newest))}")
    newest_text = f"v{'.'.join(map(str, newest))}" if newest else "none"
    return f"pyproject {ctx.version} == {ctx.tag}; newest existing {newest_text}"


def changelog_heading(version: str) -> str | None:
    text = (REPO / "CHANGELOG.md").read_text(encoding="utf-8")
    m = re.search(rf"^## .*\bv{re.escape(version)}\b.*$", text, re.MULTILINE)
    return m.group(0) if m else None


def check_changelog(ctx: Context) -> str:
    heading = changelog_heading(ctx.version)
    if heading is None:
        raise CheckFailed(f"CHANGELOG.md has no '## ... v{ctx.version}' heading")
    return heading[:100]


def check_deps(ctx: Context) -> str:
    deps = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "dependencies"
    ]
    vcs = [d for d in deps if "@" in d and "://" in d]
    if vcs:
        raise CheckFailed(f"non-PyPI dependencies would ship in the wheel metadata: {vcs}")
    return f"{len(deps)} dependencies, none repository-resolved"


def check_lint(ctx: Context) -> str:
    probe = run([PY, "-m", "ruff", "--version"], check=False)
    if probe.returncode != 0:
        raise CheckFailed(f"ruff is not installed in {PY}; run `{PY} -m pip install ruff`")
    # Exactly what tests.yml runs -- keep these two lines in sync with the workflow.
    run([PY, "-m", "ruff", "check", "--select", "F811", "--output-format", "concise", "."])
    run([PY, "-m", "ruff", "check", "--select", "F821,ASYNC", "--output-format", "concise", "src"])
    return f"{probe.stdout.strip()}: F811 (.) and F821,ASYNC (src) clean"


def check_offline(ctx: Context) -> str:
    proc = run(
        [PY, "-m", "pytest", "-p", "no:cacheprovider", "-q", "--no-header", "tests/"],
        check=False,
        timeout=3600,
    )
    if proc.returncode != 0:
        raise CheckFailed("offline suite failed:\n" + tail(proc.stdout, 40))
    return tail(proc.stdout, 1)


def _test_neo4j_problem(env: dict[str, str]) -> str | None:
    try:
        from neo4j import GraphDatabase  # type: ignore[import-not-found]
    except ImportError:
        return "neo4j driver not importable"
    try:
        driver = GraphDatabase.driver(
            env["MENHIR_TEST_NEO4J_URI"],
            auth=(env["MENHIR_TEST_NEO4J_USER"], env["MENHIR_TEST_NEO4J_PASSWORD"]),
        )
        with driver.session(database=env["MENHIR_TEST_NEO4J_DATABASE"]) as session:
            session.run("RETURN 1").single()
        driver.close()
    except Exception as exc:  # noqa: BLE001 -- any failure means the lane cannot run
        return f"{type(exc).__name__}: {str(exc).splitlines()[0][:160]}"
    return None


def check_online(ctx: Context) -> str:
    env = {k: os.environ.get(k, v) for k, v in ONLINE_ENV_DEFAULTS.items()}
    problem = _test_neo4j_problem(env)
    if problem:
        raise CheckFailed(
            f"test Neo4j at {env['MENHIR_TEST_NEO4J_URI']} is not usable ({problem}). "
            "Start the throwaway instance (docker compose service menhir-neo4j-test); "
            "an online lane that did not execute is a failure, not a skip."
        )
    proc = run(
        [
            PY, "-m", "pytest", "-p", "no:cacheprovider", "-q", "--no-header",
            "-m", "online and not needs_llm", "--run-online", "-rs", "tests/",
        ],
        check=False,
        env=env,
        timeout=3600,
    )
    if proc.returncode != 0:
        raise CheckFailed("online suite failed:\n" + tail(proc.stdout, 40))
    summary = tail(proc.stdout, 1)
    m = re.search(r"\b(\d+) passed\b", summary)
    if not m or int(m.group(1)) == 0:
        raise CheckFailed("online suite executed nothing:\n" + summary)
    return summary


def check_ci(ctx: Context) -> str:
    sha = ctx.head or git("rev-parse", "HEAD")
    deadline = time.monotonic() + 45 * 60
    while True:
        runs = gh_json(
            "run", "list", "--commit", sha, "--workflow", "tests",
            "--json", "status,conclusion,url",
        )
        if not runs:
            raise CheckFailed(
                f"no `tests` workflow run exists for {sha[:8]}; push the commit and let CI run"
            )
        pending = [r for r in runs if r["status"] != "completed"]
        if pending:
            if time.monotonic() > deadline:
                raise CheckFailed(f"timed out waiting for tests.yml on {sha[:8]}")
            print(f"    tests.yml still running on {sha[:8]} ({len(pending)} pending) ...")
            time.sleep(30)
            continue
        failed = [r for r in runs if r["conclusion"] != "success"]
        if failed:
            raise CheckFailed(
                f"tests.yml is not green on {sha[:8]}: "
                + ", ".join(f"{r['conclusion']} {r['url']}" for r in failed)
            )
        return f"{len(runs)} tests.yml run(s) green on {sha[:8]}: {runs[0]['url']}"


CHECKS = {
    "tree": check_tree,
    "branch": check_branch,
    "version": check_version,
    "changelog": check_changelog,
    "deps": check_deps,
    "lint": check_lint,
    "offline": check_offline,
    "online": check_online,
    "ci": check_ci,
}


# ---------------------------------------------------------------------------
# tag + publish watch
# ---------------------------------------------------------------------------


def do_tag(ctx: Context) -> None:
    print(f"\n==> tagging {ctx.tag} at {ctx.head[:8]} and pushing")
    heading = changelog_heading(ctx.version) or ctx.tag
    run(["git", "tag", "-a", ctx.tag, "-m", heading.lstrip("# ").strip()])
    run(["git", "push", "origin", ctx.tag])
    peeled = git("rev-parse", f"{ctx.tag}^{{commit}}")
    if peeled != ctx.head:
        raise CheckFailed(f"tag {ctx.tag} points at {peeled[:8]}, expected {ctx.head[:8]}")
    print(f"    pushed; {ctx.tag} -> {peeled[:8]}")


def watch_publish(ctx: Context) -> None:
    print("==> waiting for `Publish to PyPI` on the tag")
    run_id = None
    deadline = time.monotonic() + 10 * 60
    while run_id is None:
        runs = gh_json(
            "run", "list", "--workflow", "Publish to PyPI", "--branch", ctx.tag,
            "--json", "databaseId",
        )
        if runs:
            run_id = runs[0]["databaseId"]
        elif time.monotonic() > deadline:
            raise CheckFailed("publish workflow did not start within 10 minutes")
        else:
            time.sleep(15)
    proc = run(
        ["gh", "run", "watch", str(run_id), "--exit-status", "--interval", "30", "-R", GH_REPO],
        check=False,
        timeout=3600,
    )
    view = run(["gh", "run", "view", str(run_id), "--json", "jobs", "-R", GH_REPO])
    for job in json.loads(view.stdout or "{}").get("jobs", []):
        print(f"    {(job.get('conclusion') or job.get('status')):>10}  {job['name']}")
    if proc.returncode != 0:
        raise CheckFailed(f"publish workflow failed: https://github.com/{GH_REPO}/actions/runs/{run_id}")
    print("==> confirming PyPI")
    for _ in range(20):
        try:
            with urllib.request.urlopen(f"https://pypi.org/pypi/{PYPI_PROJECT}/json", timeout=20) as resp:
                data = json.load(resp)
            if ctx.version in data["releases"]:
                print(f"    PyPI latest: {data['info']['version']} (has {ctx.version})")
                return
        except urllib.error.URLError:
            pass
        time.sleep(15)
    raise CheckFailed(f"{ctx.version} not visible on PyPI after publish")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("tag", help="release tag, e.g. v0.2.4")
    ap.add_argument(
        "--tag", action="store_true", dest="do_tag",
        help="after ALL checks pass: create the annotated tag, push it, watch publish",
    )
    ap.add_argument(
        "--only", default="",
        help="comma-separated subset of checks to run (never allowed with --tag)",
    )
    args = ap.parse_args(argv)

    if not TAG_RE.match(args.tag):
        print(f"tag must look like vX.Y.Z, got {args.tag!r}", file=sys.stderr)
        return 2
    ctx = Context(tag=args.tag, version=args.tag[1:])

    selected = [c.strip() for c in args.only.split(",") if c.strip()] or list(CHECK_ORDER)
    unknown = [c for c in selected if c not in CHECKS]
    if unknown:
        print(f"unknown check(s): {unknown}; choose from {list(CHECK_ORDER)}", file=sys.stderr)
        return 2
    if args.do_tag and args.only:
        print("--tag requires the full check set; drop --only", file=sys.stderr)
        return 2

    print(f"release gate for {ctx.tag} in {REPO}")
    for name in CHECK_ORDER:
        if name not in selected:
            continue
        started = time.monotonic()
        print(f"--> {name}", flush=True)
        try:
            detail, ok = CHECKS[name](ctx), True
        except CheckFailed as exc:
            detail, ok = str(exc), False
        except Exception as exc:  # noqa: BLE001 -- a crashed check is a failed check
            detail, ok = f"check crashed: {type(exc).__name__}: {exc}", False
        ctx.results.append(Result(name, ok, detail, time.monotonic() - started))
        if not ok:
            # Stop at the first failure: later checks are meaningless against a tree
            # that already cannot be released, and the two suites are slow.
            break

    print("\n" + "=" * 78)
    reached = {r.name for r in ctx.results}
    all_ok = all(r.ok for r in ctx.results) and reached == set(selected)
    for r in ctx.results:
        first, *rest = r.detail.splitlines() or [""]
        print(f"{'PASS' if r.ok else 'FAIL'}  {r.name:<10} {r.seconds:7.1f}s  {first}")
        for line in rest:
            print(f"{'':27}{line}")
    for name in selected:
        if name not in reached:
            print(f"SKIP  {name:<10}          not reached")
    print("=" * 78)

    if not all_ok:
        print(f"NOT READY: {ctx.tag} must not be tagged.")
        return 1
    if not args.do_tag:
        print(f"READY: every selected check passed. Re-run with --tag to release {ctx.tag}.")
        return 0
    try:
        do_tag(ctx)
        watch_publish(ctx)
    except CheckFailed as exc:
        print(f"RELEASE FAILED: {exc}", file=sys.stderr)
        return 1
    print(f"RELEASED {ctx.tag}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
