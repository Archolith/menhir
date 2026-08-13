#!/usr/bin/env python3
"""Ground-truth regression test for menhir_audit_probe.py.

Asserts the probe detects defects that were independently confirmed by hand
against commit eebf6d6dd83f15083167bf847b639d24b953fdc9, and that it does NOT
report a known false positive.

A probe nobody checks is just another unverified claim. Run this before
trusting any report that cites probe output.

    python .agent/audit/test_menhir_audit_probe.py

Exit 0 = all expectations hold. Exit 1 = the probe regressed, or the code under
audit changed and the expectations need review (which is itself a signal).
"""

from __future__ import annotations

import io
import contextlib
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(HERE))

import menhir_audit_probe as probe  # noqa: E402


def run(module: str, atype: str | None = None) -> str:
    """Capture probe output for a module without spawning a subprocess."""
    mod = REPO / module
    files = list(probe.iter_py(mod))
    # Derive package and layers exactly as main() does, so the test exercises
    # the real wiring rather than the defaults.
    pkg, pkg_dir = probe.package_root(mod, REPO)
    layers = probe.sibling_layers(pkg_dir)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        probe.check_line_reconciliation(files, REPO)
        probe.check_duplicate_bodies(files, REPO)
        probe.check_unread_constants(files, REPO, REPO)
        probe.check_private_imports(files, REPO, pkg)
        probe.check_layering(files, REPO, pkg, layers)
        probe.check_dead_symbols(files, REPO, REPO)
        probe.check_comment_claims(files, REPO)
        if atype:
            fn = probe.PLUGINS[atype]
            if fn is probe.plugin_a3_architecture:
                fn(files, REPO, pkg, pkg_dir)
            else:
                fn(files, REPO)
    return buf.getvalue()


CASES: list[tuple[str, str, str | None, list[str], list[str]]] = [
    # (label, module, plugin, must_contain, must_not_contain)
    (
        "CF-2 supersede_artifact duplicate dispatches to different repos",
        "src/menhir/infrastructure", None,
        ["MemoryGraphAdapter.supersede_artifact defined 2 times",
         "line 1362", "line 1664", "bodies DIFFER"],
        [],
    ),
    (
        "CF-7 fail_exhausted_pending_episodes duplicate drops raw-capture loop",
        "src/menhir/infrastructure", None,
        ["MemoryGraphAdapter.fail_exhausted_pending_episodes defined 2 times",
         "line 487", "line 876", "bodies DIFFER"],
        [],
    ),
    (
        "M2 line reconciliation matches hand count",
        "src/menhir/api", None,
        ["TOTAL: 5565 lines across 24 files"],
        [],
    ),
    (
        "M2 layering: 11 api -> infrastructure edges",
        "src/menhir/api", None,
        ["api          -> infrastructure   11"],
        [],
    ),
    (
        "M2 DRY: both identical-body helpers found",
        "src/menhir/api", None,
        ["_settings_for defined in 2 files", "new_client_id defined in 2 files",
         "IDENTICAL (DRY candidate)"],
        # different classes must never be reported as shadowing each other
        ["OAuthAuthenticationError.__init__ defined 2 times"],
    ),
    (
        "CF-26 unredacted request body reaches logs",
        "src/menhir/api", "a2",
        ["routes_handlers.py", "logs body", "redaction-in-file=False"],
        [],
    ),
    (
        "CF-27 sync sqlite one hop from async auth path",
        "src/menhir/api", "a2",
        ["_call_with_client_token", "resolve()", "sqlite3.connect"],
        # dict .get() must not be resolved to a store method by bare name
        ["-> get()"],
    ),
    (
        "CF-3 circuit breaker broad handler in async with no finally",
        "src/menhir/infrastructure", "a1",
        ["circuit_breaker.py", "finally=False"],
        [],
    ),
    (
        "CF-6 both timestamp forms present in telemetry",
        "src/menhir/infrastructure/telemetry", "a1",
        ["BOTH PRESENT"],
        [],
    ),
]


def main() -> int:
    print("Ground-truth regression test for menhir_audit_probe")
    print("repo: %s" % REPO)
    print("-" * 74)

    failures = 0
    for label, module, atype, must, must_not in CASES:
        try:
            out = run(module, atype)
        except Exception as exc:  # a crash is a failure, not an error to hide
            print("  FAIL  %s\n        probe raised %s: %s" % (label, type(exc).__name__, exc))
            failures += 1
            continue

        missing = [m for m in must if m not in out]
        present = [m for m in must_not if m in out]
        if missing or present:
            failures += 1
            print("  FAIL  %s" % label)
            for m in missing:
                print("        expected but absent : %r" % m)
            for m in present:
                print("        present but banned  : %r" % m)
        else:
            print("  ok    %s" % label)

    print("-" * 74)
    print("%d/%d expectations hold" % (len(CASES) - failures, len(CASES)))
    if failures:
        print("\nA failure means either the probe regressed, or the audited code")
        print("changed. Both need a human decision -- do not silently update the")
        print("expectations to match new output.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
