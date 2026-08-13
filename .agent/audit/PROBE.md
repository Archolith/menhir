# Mechanical probe -- moved

The probe and its tests now live in their own project:

    projects/ctharvey/cth.auditprobe

It was extracted once it proved useful beyond Menhir. Running it against another
project immediately exposed a bug that was invisible here: three checks
hardcoded the package name `menhir`, so they silently returned zero everywhere
else -- reporting clean results for checks that never ran.

## Usage

    python ../../ctharvey/cth.auditprobe/src/auditprobe/probe.py \
        src/menhir/api --type security

Or install it (`pip install -e ../../ctharvey/cth.auditprobe`) and use the
`auditprobe` entry point.

## What Menhir keeps

- `PROBE-PROTOCOL.md` -- the rule that every report section maps to a check
- `AUDIT-FIT.md` -- which workspace audit types fit this codebase
- `MODULE-MAP.md` -- the 11-module partition
- `prompts/` -- the audit prompt templates

The ground-truth expectations that used to live in
`test_menhir_audit_probe.py` were Menhir-specific and would have rotted as those
defects get fixed. The probe's own suite now uses synthetic fixtures instead.
Menhir's confirmed findings are tracked in the workspace findings register.
