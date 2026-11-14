"""Release gate: refuse to tag unless every check a release depends on is green.

Why this exists: v0.2.1 and v0.2.2 were tagged from a workstation whose local suite was
green while `tests.yml` was red on the same SHA (ruff F821 caught a NameError the offline
suite could not see), and `publish-pypi.yml` published anyway. The workflow now gates on
CI, but the tag was still cut blind. This script is the workstation half of that gate.

Default mode is check-only. Nothing is tagged or pushed unless `--tag` is passed, and
`--tag` refuses unless every check below passed in this same run.

Checks (all mandatory; there is no flag that skips one and still allows `--tag`):

  0. preflight   in seconds, before any lane: whatever the selected checks need is present --
                 ruff, a Beacon interpreter (MENHIR_TEST_BEACON_PYTHON, pin read from
                 tests.yml), a reachable test Neo4j, an authenticated `gh` (#606)
  1. tree       working tree clean (no modified, staged, or deleted tracked files)
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
 10. image       the newest scheduled or dispatched `release image dry run` for HEAD's SHA
                 is green, with build and verify passing on every release runner (waits
                 while it runs; with no run, prints the `gh workflow run` command). A tag
                 whose image build fails has already burned the PyPI version (#614).

A full check-only run that passes writes a receipt (.release-gate/<tag>.json, gitignored).
`--tag` on the same tag, HEAD and interpreter within two hours reuses the receipt's offline
and online results and reruns every other check, so the slow lanes do not run twice (#607).
`--fresh` ignores the receipt.

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
    "preflight",
    "tree",
    "branch",
    "version",
    "changelog",
    "deps",
    "lint",
    "offline",
    "online",
    "ci",
    "image",
)

#: #607: the slow lanes a check-only run's receipt lets `--tag` reuse; every other check reruns.
REUSABLE = ("offline", "online")
RECEIPT_DIR = REPO / ".release-gate"
RECEIPT_MAX_AGE_S = 2 * 3600

DRYRUN_WORKFLOW = "release-image-dryrun.yml"
# Pull-request runs build the merge commit, not HEAD, so only these events count.
DRYRUN_EVENTS = frozenset({"schedule", "workflow_dispatch"})
DRYRUN_RUNNERS = ("ubuntu-24.04", "ubuntu-26.04")
DRYRUN_STAGES = (
    "Build and seal candidate without publishing",
    "Verify sealed candidate without publishing",
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
    #: The selected checks other than preflight, which checks only what these need.
    lanes: frozenset[str] = frozenset()
    tagging: bool = False


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


def beacon_pins() -> set[str]:
    """The Beacon commit(s) tests.yml installs; the gate reads them so the two cannot drift."""
    text = (REPO / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
    return set(re.findall(r"Archolith/beacon\.git@([0-9a-f]{7,40})", text))


def _beacon_fix_command() -> str:
    pins = sorted(beacon_pins())
    if len(pins) != 1:
        return f"(tests.yml pins Beacon at {pins or 'nothing'}; fix the workflow first)"
    pin = pins[0]
    venv = f".release-gate/beacon-{pin[:8]}"
    python = f"{venv}/Scripts/python.exe" if os.name == "nt" else f"{venv}/bin/python"
    return (
        f'python -m venv {venv} && {python} -m pip install '
        f'"git+https://github.com/Archolith/beacon.git@{pin}"\n'
        f"then set MENHIR_TEST_BEACON_PYTHON={REPO.as_posix()}/{python}"
    )


def _probe(cmd: list[str], timeout: float) -> subprocess.CompletedProcess[str] | None:
    """Run a quick probe; None when it could not start or did not finish in time."""
    try:
        return run(cmd, check=False, timeout=timeout)  # type: ignore[arg-type]
    except (OSError, subprocess.TimeoutExpired):
        return None


def _beacon_problem() -> str | None:
    python = os.environ.get("MENHIR_TEST_BEACON_PYTHON", "")
    if not python:
        return "MENHIR_TEST_BEACON_PYTHON is not set (the offline lane's Beacon tests need it)"
    if not Path(python).is_file():
        return f"MENHIR_TEST_BEACON_PYTHON={python} does not exist"
    probe = _probe([python, "-c", "import beacon"], timeout=30)
    if probe is None or probe.returncode != 0:
        return f"MENHIR_TEST_BEACON_PYTHON={python} cannot import beacon"
    return None


def check_preflight(ctx: Context) -> str:
    """#606: refuse in seconds, before any lane, when a selected lane cannot run here."""
    problems: list[str] = []
    checked: list[str] = []
    if "lint" in ctx.lanes:
        checked.append("ruff")
        probe = _probe([PY, "-m", "ruff", "--version"], timeout=20)
        if probe is None or probe.returncode != 0:
            problems.append(f"ruff is not installed in {PY}\n  fix: {PY} -m pip install ruff")
    if "offline" in ctx.lanes:
        checked.append("beacon")
        problem = _beacon_problem()
        if problem:
            problems.append(f"{problem}\n  fix: {_beacon_fix_command()}")
    if "online" in ctx.lanes:
        checked.append("test neo4j")
        env = {k: os.environ.get(k, v) for k, v in ONLINE_ENV_DEFAULTS.items()}
        problem = _test_neo4j_problem(env, connect_timeout=3.0)
        if problem:
            problems.append(
                f"test Neo4j at {env['MENHIR_TEST_NEO4J_URI']} is not usable ({problem})\n"
                "  fix: start a disposable test Neo4j and set MENHIR_TEST_NEO4J_URI to it"
            )
    if ctx.lanes & {"ci", "image"} or ctx.tagging:
        checked.append("gh auth")
        probe = _probe(["gh", "auth", "status"], timeout=20)
        if probe is None or probe.returncode != 0:
            problems.append("gh is not installed or not authenticated\n  fix: gh auth login")
    if problems:
        raise CheckFailed("\n".join(problems))
    return "ok: " + (", ".join(checked) or "nothing the selected checks need")


# ---------------------------------------------------------------------------
# #607: a passing check-only run's slow lanes are reused by --tag on the same commit
# ---------------------------------------------------------------------------


def receipt_path(tag: str) -> Path:
    return RECEIPT_DIR / f"{tag}.json"


def write_receipt(ctx: Context) -> Path:
    path = receipt_path(ctx.tag)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "tag": ctx.tag,
        "head": ctx.head,
        "python": PY,
        "created": time.time(),
        "results": {r.name: r.detail for r in ctx.results if r.name in REUSABLE and r.ok},
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def load_receipt(ctx: Context, *, now: float | None = None) -> tuple[dict[str, str] | None, str]:
    """The reusable lane results, or None and the reason they cannot be reused.

    Only valid for the same tag, the same HEAD (which `branch` has just proven equals
    origin/main, with `tree` proving it clean), the same interpreter, and a recent run.
    """
    path = receipt_path(ctx.tag)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, "no receipt from a check-only run"
    except (OSError, ValueError):
        return None, f"receipt {path} is unreadable"
    age = (time.time() if now is None else now) - float(data.get("created") or 0)
    results = data.get("results") or {}
    if data.get("tag") != ctx.tag:
        return None, "receipt is for another tag"
    if not ctx.head or data.get("head") != ctx.head:
        return None, f"receipt is for {str(data.get('head'))[:8]}, HEAD is {ctx.head[:8]}"
    if data.get("python") != PY:
        return None, "receipt was written by another interpreter"
    if not 0 <= age <= RECEIPT_MAX_AGE_S:
        return None, f"receipt is {age / 60:.0f} min old (limit {RECEIPT_MAX_AGE_S // 60:.0f})"
    if set(results) != set(REUSABLE):
        return None, "receipt does not cover every slow lane"
    return {name: str(results[name]) for name in REUSABLE}, f"{age / 60:.0f} min old"


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


def _test_neo4j_problem(env: dict[str, str], *, connect_timeout: float | None = None) -> str | None:
    try:
        from neo4j import GraphDatabase  # type: ignore[import-not-found]
    except ImportError:
        return "neo4j driver not importable"
    try:
        driver = GraphDatabase.driver(
            env["MENHIR_TEST_NEO4J_URI"],
            auth=(env["MENHIR_TEST_NEO4J_USER"], env["MENHIR_TEST_NEO4J_PASSWORD"]),
            **({"connection_timeout": connect_timeout} if connect_timeout else {}),
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


def _dryrun_job_problems(run_id: int) -> list[str]:
    proc = run(["gh", "run", "view", str(run_id), "--json", "jobs", "-R", GH_REPO])
    jobs = json.loads(proc.stdout or "{}").get("jobs", [])
    problems = []
    for runner in DRYRUN_RUNNERS:
        for stage in DRYRUN_STAGES:
            name = f"dry run {runner} / {stage}"
            conclusions = [j.get("conclusion") for j in jobs if j.get("name") == name]
            if not conclusions:
                problems.append(f"{name}: missing")
            elif any(c != "success" for c in conclusions):
                problems.append(f"{name}: {', '.join(str(c) for c in conclusions)}")
    return problems


def check_image(ctx: Context) -> str:
    sha = ctx.head or git("rev-parse", "HEAD")
    deadline = time.monotonic() + 60 * 60
    while True:
        runs = [
            r for r in gh_json(
                "run", "list", "--commit", sha, "--workflow", DRYRUN_WORKFLOW,
                "--json", "databaseId,status,conclusion,url,event,createdAt",
            )
            if r.get("event") in DRYRUN_EVENTS
        ]
        if not runs:
            raise CheckFailed(
                f"no release image dry run exists for {sha[:8]}; start one with\n"
                f"`gh workflow run {DRYRUN_WORKFLOW} --ref main -R {GH_REPO}`\n"
                "and re-run this gate once it is green"
            )
        # The newest run decides: a later red run is newer evidence than an older green one.
        latest = max(runs, key=lambda r: r["createdAt"])
        if latest["status"] != "completed":
            if time.monotonic() > deadline:
                raise CheckFailed(f"timed out waiting for the image dry run on {sha[:8]}")
            print(f"    image dry run still running on {sha[:8]}: {latest['url']} ...")
            time.sleep(30)
            continue
        if latest["conclusion"] != "success":
            raise CheckFailed(
                f"the image dry run is not green on {sha[:8]}: "
                f"{latest['conclusion']} {latest['url']}"
            )
        problems = _dryrun_job_problems(latest["databaseId"])
        if problems:
            raise CheckFailed(
                f"the image dry run on {sha[:8]} did not build and verify on every runner "
                f"({latest['url']}):\n" + "\n".join(problems)
            )
        return f"image dry run green on {', '.join(DRYRUN_RUNNERS)} at {sha[:8]}: {latest['url']}"


CHECKS = {
    "preflight": check_preflight,
    "tree": check_tree,
    "branch": check_branch,
    "version": check_version,
    "changelog": check_changelog,
    "deps": check_deps,
    "lint": check_lint,
    "offline": check_offline,
    "online": check_online,
    "ci": check_ci,
    "image": check_image,
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
        # The run now includes the image build; its job timeouts add up to over two hours.
        timeout=3 * 3600,
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
    ap.add_argument(
        "--fresh", action="store_true",
        help="with --tag: rerun the offline and online lanes even if a check-only receipt covers them",
    )
    args = ap.parse_args(argv)

    if not TAG_RE.match(args.tag):
        print(f"tag must look like vX.Y.Z, got {args.tag!r}", file=sys.stderr)
        return 2

    selected = [c.strip() for c in args.only.split(",") if c.strip()] or list(CHECK_ORDER)
    unknown = [c for c in selected if c not in CHECKS]
    if unknown:
        print(f"unknown check(s): {unknown}; choose from {list(CHECK_ORDER)}", file=sys.stderr)
        return 2
    if args.do_tag and args.only:
        print("--tag requires the full check set; drop --only", file=sys.stderr)
        return 2
    # Preflight always runs, scoped to what the selected checks need.
    if "preflight" not in selected:
        selected.insert(0, "preflight")
    ctx = Context(
        tag=args.tag,
        version=args.tag[1:],
        lanes=frozenset(selected) - {"preflight"},
        tagging=args.do_tag,
    )
    full_set = set(selected) == set(CHECK_ORDER)

    print(f"release gate for {ctx.tag} in {REPO}")
    reuse: dict[str, str] | None = None
    reuse_note = "--fresh" if args.fresh else ""
    for name in CHECK_ORDER:
        if name not in selected:
            continue
        started = time.monotonic()
        print(f"--> {name}", flush=True)
        if name in REUSABLE and args.do_tag and not args.fresh:
            if reuse is None and not reuse_note:
                # Loaded only here, after `tree` and `branch` passed: HEAD is clean and pushed.
                reuse, reuse_note = load_receipt(ctx)
                print(f"    receipt: {'reusing, ' if reuse else 'not reused: '}{reuse_note}")
            if reuse is not None:
                ctx.results.append(Result(
                    name, True, f"reused ({reuse_note}): {reuse[name]}",
                    time.monotonic() - started,
                ))
                continue
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
        if full_set:
            path = write_receipt(ctx)
            print(
                f"READY: every check passed. Receipt {path.as_posix()}: "
                f"`--tag` within {RECEIPT_MAX_AGE_S // 3600:.0f}h on this commit reuses the "
                "offline and online lanes and reruns the rest."
            )
        else:
            print(f"READY: every selected check passed. Re-run with --tag to release {ctx.tag}.")
        return 0
    try:
        do_tag(ctx)
        watch_publish(ctx)
    except CheckFailed as exc:
        print(f"RELEASE FAILED: {exc}", file=sys.stderr)
        return 1
    try:
        receipt_path(ctx.tag).unlink()
    except OSError:
        pass
    print(f"RELEASED {ctx.tag}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
