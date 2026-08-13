#!/usr/bin/env python3
"""Executable evidence probe for the Menhir M2 API/OAuth compound audit.

Run from the repository root at the audited commit (or the audit branch):

    python .agent/audit/m2_functional_probe.py

The final form of this script prints every reproduction and static sweep cited by
`.agent/reviews/menhir-M2-compound-audit-results.md`. It intentionally performs no
production writes and uses temporary directories/stores for stateful checks.
"""

from __future__ import annotations

import sys

AUDITED_COMMIT = "eebf6d6dd83f15083167bf847b639d24b953fdc9"


def main() -> int:
    print(f"Menhir M2 functional probe — target {AUDITED_COMMIT}")
    print("DRAFT: evidence probes are being added during the audit.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
