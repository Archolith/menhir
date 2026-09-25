"""Named detection rules for the Menhir production mutation census.

Moved verbatim from pipeline/census.py, which re-exports ``Rule`` and
``RULES`` so every existing import site keeps working unchanged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Rule:
    """One named detector.

    kind:     coarse category used for reporting and disposition policy
    pattern:  compiled regex applied per line
    why:      what a reviewer should understand this finding to mean
    severity: 'mutator' items MUST be retired or replaced before cutover;
              'context' items are recorded for completeness but may be preserved
    """

    name: str
    kind: str
    pattern: re.Pattern
    why: str
    severity: str


def _rx(p: str) -> re.Pattern:
    return re.compile(p, re.IGNORECASE)


RULES: tuple[Rule, ...] = (
    # --- the detector that would have caught the fourth wrapper ---------------
    Rule(
        "inline_interpreter_as_root",
        "privileged-exec",
        _rx(r"sudo[^|\n]*\b(python3?|bash|sh|perl|ruby|node)\b\s*-\s*(?:$|[\"'|)])"),
        "Pipes caller-supplied program text into an interpreter running as root. "
        "This is arbitrary root code execution and cannot be constrained by "
        "sudoers argument matching.",
        "mutator",
    ),
    Rule(
        "base64_program_transfer",
        "privileged-exec",
        _rx(r"base64\s+-d|\[Convert\]::ToBase64String|FromBase64String"),
        "Transfers program or payload text encoded, which defeats source review "
        "and argument inspection at the privilege boundary.",
        "mutator",
    ),
    # --- target-anchored detection -------------------------------------------
    # The verb-anchored rules below all failed on scripts/vps-ssh.ps1 and
    # scripts/vps-compose.ps1, which take an arbitrary command as a parameter
    # and run it on the production host. They build the command dynamically, so
    # no literal `ssh host "..."` or `docker compose ...` appears in code; in
    # vps-compose.ps1 the only occurrence of "docker compose" is a comment.
    #
    # This is a general weakness of verb matching: the MOST dangerous scripts
    # are the ones that pass any command through, and those are the LEAST
    # likely to contain a matching literal. Anchor on the target instead. A
    # file that names the production host is a mutation candidate no matter how
    # it builds what it sends there.
    Rule(
        "production_host_reference",
        "host-target",
        _rx(r"147\.93\.132\.141|YAWN_VPS_HOST|memory\.ctharvey\.me|"
            r"YAWN_VPS_SSH_KEY_PATH|menhir-prod-app"),
        "Names the production host, its host variable, or its SSH credential "
        "material. Anything that can address the box is a mutation candidate "
        "regardless of which commands it happens to contain.",
        "mutator",
    ),
    Rule(
        "arbitrary_command_passthrough",
        "host-target",
        _rx(r"ValueFromRemainingArguments|\$args\b|\"\$@\"|\bexec\s+\"?\$|"
            r"Usage:[^\n]*<remote command>|<docker compose args>"),
        "Forwards caller-supplied arguments onward as a command. Unbounded by "
        "construction: it cannot be constrained by sudoers argument matching "
        "and cannot be reasoned about from its own source.",
        "mutator",
    ),
    # --- privileged invocation ------------------------------------------------
    Rule(
        "sudo_invocation",
        "privileged-exec",
        _rx(r"\bsudo\b(?!\s*(?:-n\s+)?true\b)"),
        "Invokes a privileged command. Every one of these is a candidate "
        "mutation entry point and must be accounted for.",
        "mutator",
    ),
    Rule(
        "sudoers_definition",
        "privilege-grant",
        _rx(r"sudoers|NOPASSWD|/etc/sudoers\.d"),
        "Defines or edits a privilege grant. The set of these is the actual "
        "privilege boundary, regardless of what any document claims.",
        "mutator",
    ),
    # --- remote execution -----------------------------------------------------
    Rule(
        "remote_shell_exec",
        "remote-exec",
        _rx(r"\bssh\b\s+[^\s]+\s+[\"']|Invoke-Command|New-PSSession|\bscp\b|\bsftp\b|\brsync\b"),
        "Executes or transfers to a remote host. Combined with a privileged "
        "invocation this is a deployment path.",
        "mutator",
    ),
    # --- root filesystem mutation --------------------------------------------
    Rule(
        "root_path_write",
        "fs-mutation",
        _rx(r"\b(install|mv|cp|rm|chmod|chown|ln|mkdir|tee|truncate)\b[^\n]{0,80}"
            r"(/srv/|/etc/|/usr/local/|/var/lib/|/run/lock/|/opt/)"),
        "Writes, moves, or deletes under a root-owned production path.",
        "mutator",
    ),
    # --- orchestration --------------------------------------------------------
    Rule(
        # NOTE: an earlier version of this rule matched \.(service|timer|path|socket)\b
        # which collided with every Python attribute access ending in `.path`
        # and produced 3698 of 4555 findings. A detector that buries the signal
        # is worse than no detector: it makes the "no unclassified item" gate
        # unachievable, and an unachievable gate gets waived. Match unit files,
        # unit-file grammar, and systemctl verbs only.
        "systemd_unit",
        "service-definition",
        _rx(r"systemctl\s+(enable|disable|start|stop|restart|reload|daemon-reload|mask|unmask)\b|"
            r"^\s*\[(Unit|Service|Timer|Socket|Install)\]|"
            r"^\s*(ExecStart|ExecStop|ExecReload|WantedBy|RequiredBy|OnCalendar|Restart)\s*=|"
            r"[\w.-]+\.(service|timer|socket)\b"),
        "Defines or controls a system service, timer, or socket activation. "
        "These run without an operator present and are mutation paths.",
        "mutator",
    ),
    Rule(
        "documented_manual_procedure",
        "runbook",
        _rx(r"^\s*(?:[-*\d.]+\s+)?(?:run|execute|then)\b[^\n]{0,60}\bsudo\b|"
            r"^\s*\$?\s*sudo\s+"),
        "A documented manual mutation procedure. An operator following a runbook "
        "is a real mutation path, but it is a human process rather than an "
        "executable one, so it is recorded without blocking the cutover gate.",
        "context",
    ),
    Rule(
        "ansible_mutation",
        "orchestration",
        _rx(r"ansible\.builtin\.(file|copy|template|command|shell|systemd|service|"
            r"lineinfile|blockinfile|user|group|package|apt|unarchive|get_url)"),
        "An Ansible task that changes host state.",
        "mutator",
    ),
    Rule(
        "compose_production",
        "container-mutation",
        _rx(r"docker\s+compose[^\n]*(-f\s*[^\s]*production|up\b|down\b|restart\b)|"
            r"docker\s+(run|rm|stop|start|restart|exec|load|tag|push)\b"),
        "Starts, stops, replaces, or loads containers. Affects what is running "
        "and reachable.",
        "mutator",
    ),
    # --- concurrency control --------------------------------------------------
    Rule(
        "lock_acquisition",
        "lock",
        _rx(r"flock|/run/lock/|LOCK_EX|LOCK_NB|menhir-production\.lock"),
        "Acquires or defines a mutation lock. Two writers sharing a lock path "
        "are the same critical section and must be censused together.",
        "mutator",
    ),
    # --- data at risk ---------------------------------------------------------
    Rule(
        "database_or_backup_op",
        "data-mutation",
        _rx(r"neo4j-admin|cypher-shell|\bdump\b[^\n]{0,40}neo4j|restore[^\n]{0,40}neo4j|"
            r"backup-generation|menhir-backup"),
        "Touches the Neo4j database or its backups. Data loss here is the one "
        "unrecoverable failure in this system.",
        "mutator",
    ),
    # --- ingress (owned by yawn.deploy per ADR 0002) --------------------------
    Rule(
        "ingress_config",
        "ingress",
        _rx(r"Caddyfile|caddy-release|caddy-route|reverse_proxy|cloudflared|menhir-proxy"),
        "Ingress configuration or its writers. Per ADR 0002 yawn.deploy owns "
        "this; any Menhir-side writer is a boundary violation.",
        "context",
    ),
)
