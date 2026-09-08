"""Static contracts for the bounded Menhir Ansible host role."""

import hashlib
import json
import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
ANSIBLE = ROOT / "deploy" / "ansible"

EXPECTED_DIRECTORIES = {
    "/srv/menhir": "0755",
    "/srv/menhir/production": "0755",
    "/srv/menhir/backups": "0755",
    "/srv/menhir/backups/encrypted": "0700",
    "/srv/menhir/scaffold": "0755",
    "/srv/menhir/scaffold/bin": "0755",
    "/srv/menhir/staging-transactions": "0700",
    "/srv/menhir/install-transactions": "0700",
    "/srv/menhir/scaffold-transactions": "0700",
    "/var/lib/menhir-production": "0755",
    "/var/log/menhir-production": "0755",
    "/etc/menhir": "0700",
    "/etc/yawn-vps": "0755",
}

EXPECTED_TEMPLATES = {
    "menhir-scaffold-audit.service": "menhir-scaffold-audit.service.j2",
    "menhir-scaffold-audit.timer": "menhir-scaffold-audit.timer.j2",
}

RETIRED_CADDY_WRITER_UNITS = {
    "menhir-caddy-reconcile.path",
    "menhir-caddy-reconcile.service",
}

RETIRED_CADDY_WRITER_SCRIPTS = {
    "/srv/menhir/production/bin/caddy-release.sh",
    "/srv/menhir/production/bin/caddy-route-apply",
    "/srv/menhir/production/bin/caddy-route-rollback",
}

SCAFFOLD_AUDIT_SHA256 = (
    "33b746f26fed20a6f1ea5081f7b3f8b0aafeb6821f6dec7315ce54f5bca1c012"
)

FORBIDDEN_ACTIONS = {
    "command",
    "raw",
    "script",
    "shell",
    "ansible.builtin.command",
    "ansible.builtin.shell",
    "ansible.builtin.raw",
    "ansible.builtin.script",
    "community.docker.docker_compose",
    "community.docker.docker_compose_v2",
    "community.docker.docker_container",
    "community.docker.docker_image",
    "containers.podman.podman_container",
    "containers.podman.podman_image",
    "community.general.postgresql_db",
    "community.mysql.mysql_db",
}

TASK_METADATA = {
    "become",
    "changed_when",
    "check_mode",
    "delegate_to",
    "failed_when",
    "loop",
    "loop_control",
    "name",
    "notify",
    "register",
    "run_once",
    "tags",
    "vars",
    "when",
}

SECRET_KEY = re.compile(
    r"(^|_)(password|passwd|private_key|secret|token|credential)(_|$)",
    re.IGNORECASE,
)
SECRET_VALUE = re.compile(
    r"BEGIN [A-Z ]*PRIVATE KEY|(?:secret|token|password)\s*[:=]",
    re.IGNORECASE,
)


def load_yaml(path):
    return yaml.safe_load(path.read_text(encoding="ascii"))


def task_actions(tasks):
    for task in tasks:
        for key in task:
            if key not in TASK_METADATA:
                yield key, task[key]


def walk(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key, item
            yield from walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk(item)


def test_playbook_targets_only_the_host_prerequisite_role():
    plays = load_yaml(ANSIBLE / "playbook.yml")
    assert len(plays) == 1
    play = plays[0]
    assert play["hosts"] == "menhir_hosts"
    assert play["become"] is True
    assert play["gather_facts"] is False
    assert "roles" not in play
    assert not ({"pre_tasks", "post_tasks"} & set(play))
    tasks = play["tasks"]
    assert tasks[0]["name"] == (
        "Require an immutable infrastructure operation id for convergence"
    )
    fenced = tasks[1]
    assert fenced["name"] == "Converge while holding both Menhir production locks"
    included = next(
        task for task in fenced["block"]
        if "ansible.builtin.include_role" in task
    )
    assert included["ansible.builtin.include_role"] == {
        "name": "menhir_host",
        "apply": {"tags": ["menhir_host"]},
    }
    assert fenced["always"][0]["name"] == (
        "Release the root infrastructure admission holder"
    )


def test_configuration_uses_local_inventory_and_has_no_dependencies():
    configuration = (ANSIBLE / "ansible.cfg").read_text(encoding="ascii")
    requirements = load_yaml(ANSIBLE / "requirements.yml")
    assert "inventory = inventory.yml" in configuration
    assert "roles_path = roles" in configuration
    assert "host_key_checking = True" in configuration
    assert requirements == {"roles": [], "collections": []}


def test_operator_toolchain_manifest_is_exactly_pinned():
    manifest = json.loads(
        (ANSIBLE / "operator-toolchain.json").read_text(encoding="ascii")
    )
    assert manifest == {
        "schema": 1,
        "python": "3.12",
        "tools": {
            "ansible-core": "2.19.3",
            "pytest-testinfra": "10.2.2",
        },
    }


def test_ansible_convergence_holds_shared_locks_in_global_order():
    play = load_yaml(ANSIBLE / "playbook.yml")[0]
    fenced = play["tasks"][1]
    block = fenced["block"]
    holder = next(
        task for task in block
        if task["name"] == "Start the root infrastructure admission holder"
    )
    argv = holder["ansible.builtin.command"]["argv"]
    admission = argv.index("/run/lock/menhir-production-admission.lock")
    mutation = argv.index("/run/lock/menhir-production.lock")
    assert admission < mutation
    assert argv[admission - 4:admission] == ["/usr/bin/flock", "-n", "-E", "75"]
    assert argv[mutation - 4:mutation] == ["/usr/bin/flock", "-n", "-E", "76"]

    mutation_probe = next(
        index for index, task in enumerate(block)
        if task["name"] == "Verify the shared mutation lock is held"
    )
    include_role = next(
        index for index, task in enumerate(block)
        if "ansible.builtin.include_role" in task
    )
    assert mutation_probe < include_role
    assert fenced["always"][0]["when"] == "not ansible_check_mode"


def test_ansible_check_mode_skips_lock_mutation_and_post_mutation_observation():
    play = load_yaml(ANSIBLE / "playbook.yml")[0]
    fenced = play["tasks"][1]
    control_tasks = [
        task for task in fenced["block"]
        if task["name"] != "Apply the bounded Menhir host role"
    ] + fenced["always"]
    assert control_tasks
    assert all(
        task.get("when") == "not ansible_check_mode"
        for task in control_tasks
    )

    role_tasks = load_yaml(
        ANSIBLE / "roles" / "menhir_host" / "tasks" / "main.yml"
    )
    post_mutation_observations = {
        "Inspect stopped Caddy route writers",
        "Verify retired Caddy route writers are stopped",
        "Apply retired systemd definition removal",
        "Inspect systemd services after retired definition removal",
        "Verify retired Caddy route writer units are absent",
        "Inspect retired Caddy route writer scripts",
        "Verify retired Caddy route writer scripts are absent",
    }
    selected = [
        task for task in role_tasks
        if task["name"] in post_mutation_observations
    ]
    assert {task["name"] for task in selected} == post_mutation_observations
    assert all("not ansible_check_mode" in str(task["when"]) for task in selected)


def test_group_vars_define_exact_root_directory_and_unit_contracts():
    variables = load_yaml(ANSIBLE / "group_vars" / "all.yml")
    directories = {
        item["path"]: item["mode"]
        for item in variables["menhir_host_directories"]
    }
    units = {
        item["name"]: item["template"]
        for item in variables["menhir_host_units"]
    }
    assert variables["menhir_ingress_authority"] == "cloudflared"
    assert directories == EXPECTED_DIRECTORIES
    assert units == EXPECTED_TEMPLATES
    assert variables["menhir_host_active_units"] == ["menhir-scaffold-audit.timer"]
    assert variables["menhir_host_retired_units"] == [
        "menhir-caddy-reconcile.path",
        "menhir-caddy-reconcile.service",
    ]
    assert variables["menhir_host_tmpfiles"] == [
        "f /run/lock/menhir-production.lock 0600 root root - -",
        "f /run/lock/menhir-production-admission.lock 0600 root root - -",
        "d /var/lib/menhir-production 0755 root root - -",
        "d /var/lib/menhir-production/jobs 0755 root root - -",
        "d /var/lib/menhir-production/backups 0755 root root - -",
        "d /var/lib/menhir-production/receipts 0755 root root - -",
        "d /var/log/menhir-production 0755 root root - -",
    ]


def test_role_uses_idempotent_modules_and_never_recurses_permissions():
    tasks = load_yaml(
        ANSIBLE / "roles" / "menhir_host" / "tasks" / "main.yml"
    )
    actions = list(task_actions(tasks))
    names = {name for name, _ in actions}
    assert not (names & FORBIDDEN_ACTIONS)
    assert names == {
        "ansible.builtin.assert",
        "ansible.builtin.file",
        "ansible.builtin.template",
        "ansible.builtin.meta",
        "ansible.builtin.service_facts",
        "ansible.builtin.stat",
        "ansible.builtin.systemd_service",
    }

    directory_tasks = [
        value for name, value in actions if name == "ansible.builtin.file"
    ]
    assert directory_tasks == [
        {
            "path": "{{ item }}",
            "state": "absent",
        },
        {
            "path": "/etc/systemd/system/{{ item }}",
            "state": "absent",
        },
        {
            "path": "{{ item.path }}",
            "state": "directory",
            "owner": "root",
            "group": "root",
            "mode": "{{ item.mode }}",
            "recurse": False,
            "follow": False,
        }
    ]

    templates = [
        value for name, value in actions if name == "ansible.builtin.template"
    ]
    assert templates == [
        {
            "src": "menhir-production.conf.j2",
            "dest": "/etc/tmpfiles.d/menhir-production.conf",
            "owner": "root",
            "group": "root",
            "mode": "0644",
        },
        {
            "src": "{{ item.template }}",
            "dest": "/etc/systemd/system/{{ item.name }}",
            "owner": "root",
            "group": "root",
            "mode": "0644",
        },
    ]

    activations = [
        value
        for name, value in actions
        if name == "ansible.builtin.systemd_service"
    ]
    assert activations == [
        {
            "name": "{{ item }}",
            "enabled": False,
            "state": "stopped",
        },
        {
            "name": "{{ item }}",
        },
        {
            "daemon_reload": True,
        },
        {
            "name": "{{ item }}",
            "enabled": True,
            "state": "started",
        }
    ]


def test_systemd_reload_is_handler_driven():
    tasks = load_yaml(
        ANSIBLE / "roles" / "menhir_host" / "tasks" / "main.yml"
    )
    handlers = load_yaml(
        ANSIBLE / "roles" / "menhir_host" / "handlers" / "main.yml"
    )
    assert handlers == [
        {
            "name": "Reload systemd",
            "ansible.builtin.systemd_service": {"daemon_reload": True},
        }
    ]
    unit_task = next(
        task
        for task in tasks
        if task.get("name") == "Install Menhir audit units"
    )
    assert unit_task["notify"] == "Reload systemd"
    assert any(
        task.get("ansible.builtin.meta") == "flush_handlers" for task in tasks
    )


def test_retired_caddy_writers_fail_closed_and_are_verified_absent():
    tasks = load_yaml(
        ANSIBLE / "roles" / "menhir_host" / "tasks" / "main.yml"
    )
    stop = next(
        task for task in tasks
        if task.get("name") == "Disable retired Caddy route writers"
    )
    assert stop["failed_when"] is not False
    assert "Could not find the requested service" in str(stop["failed_when"])
    inspect = next(
        task for task in tasks
        if task.get("name") == "Inspect stopped Caddy route writers"
    )
    assert inspect["changed_when"] is False
    assert "Could not find the requested service" in str(inspect["failed_when"])

    names = [task["name"] for task in tasks]
    assert names.index("Verify retired Caddy route writers are stopped") \
        < names.index("Remove retired Caddy route writer definitions")
    assert names.index("Remove retired Caddy route writer definitions") \
        < names.index("Apply retired systemd definition removal")
    assert names.index("Apply retired systemd definition removal") \
        < names.index("Verify retired Caddy route writer units are absent")
    assert any(
        "ansible.builtin.service_facts" in task for task in tasks
    )

    script_removal = next(
        task for task in tasks
        if task.get("name") == "Remove retired Caddy route writer scripts"
    )
    assert set(script_removal["loop"]) == RETIRED_CADDY_WRITER_SCRIPTS


def test_scaffold_timer_requires_the_exact_safe_executable():
    tasks = load_yaml(
        ANSIBLE / "roles" / "menhir_host" / "tasks" / "main.yml"
    )
    names = [task["name"] for task in tasks]
    inspection = next(
        task for task in tasks
        if task.get("name") == "Inspect the scaffold audit executable"
    )
    assert inspection["ansible.builtin.stat"] == {
        "path": "/srv/menhir/scaffold/bin/menhir_scaffold.py",
        "follow": False,
        "checksum_algorithm": "sha256",
    }
    precondition = next(
        task for task in tasks
        if task.get("name") == "Require the exact safe scaffold audit executable"
    )
    conditions = " ".join(precondition["ansible.builtin.assert"]["that"])
    required_terms = (
        "isreg", "islnk", "uid", "gid", "0755", SCAFFOLD_AUDIT_SHA256,
    )
    for required in required_terms:
        assert required in conditions
    assert names.index("Require the exact safe scaffold audit executable") \
        < names.index("Install Menhir audit units")
    assert names.index("Require the exact safe scaffold audit executable") \
        < names.index("Enable and start Menhir host monitors")
    scaffold = ROOT / "deploy" / "scaffold" / "menhir_scaffold.py"
    assert hashlib.sha256(scaffold.read_bytes()).hexdigest() \
        == SCAFFOLD_AUDIT_SHA256

    template = (
        ANSIBLE / "roles" / "menhir_host" / "templates"
        / "menhir-scaffold-audit.service.j2"
    ).read_text(encoding="ascii")
    assert (
        "ConditionPathIsExecutable=/srv/menhir/scaffold/bin/menhir_scaffold.py"
        in template
    )


def test_release_installer_converges_retired_caddy_writers_only():
    root = ROOT / "deploy"
    installer = (root / "release-install.sh").read_text(encoding="ascii")
    census = load_yaml(root / "installed-artifacts.json")
    retired_destinations = RETIRED_CADDY_WRITER_SCRIPTS | {
        f"/etc/systemd/system/{unit}" for unit in RETIRED_CADDY_WRITER_UNITS
    }
    assert all(
        path.rsplit("/", 1)[-1] in installer
        for path in retired_destinations
    )
    assert all(
        path not in census["destinations"] for path in retired_destinations
    )
    assert "systemctl disable --now" in installer
    assert "--property=ActiveState" in installer
    assert "--property=SubState" in installer
    assert "systemctl daemon-reload" in installer
    assert "/srv/yawn/releases/menhir-route-candidate" in installer
    assert "/srv/yawn/projects/yawn.deploy" not in installer
    assert installer.index("create_snapshot\n") \
        < installer.index("journal_action phase retiring-caddy")
    assert installer.index("journal_action phase retiring-caddy") \
        < installer.index("retire_caddy_writers\n")
    assert installer.index("--property=ActiveState") \
        < installer.index('rm -f -- "/etc/systemd/system/${unit}"')


def test_inventory_is_placeholder_only_and_contains_no_secret_material():
    inventory_path = ANSIBLE / "inventory.example.yml"
    inventory_text = inventory_path.read_text(encoding="ascii")
    inventory = yaml.safe_load(inventory_text)
    hosts = inventory["all"]["children"]["menhir_hosts"]["hosts"]
    assert hosts == {
        "menhir-example": {
            "ansible_host": "menhir.example.invalid",
            "ansible_user": "example-operator",
        }
    }
    assert not SECRET_VALUE.search(inventory_text)
    for key, _ in walk(inventory):
        assert not SECRET_KEY.search(str(key))


def test_role_has_no_application_or_infrastructure_mutation_actions():
    role_text = (
        ANSIBLE / "roles" / "menhir_host" / "tasks" / "main.yml"
    ).read_text(encoding="ascii")
    lowered = role_text.lower()
    forbidden_terms = (
        "docker_container",
        "docker_image",
        "docker_compose",
        "podman_container",
        "podman_image",
        "postgresql_db",
        "mysql_db",
        "neo4j",
        "kubernetes.core",
    )
    assert all(term not in lowered for term in forbidden_terms)


def test_units_delegate_only_to_existing_bounded_entry_points():
    template_root = ANSIBLE / "roles" / "menhir_host" / "templates"
    audit = (template_root / "menhir-scaffold-audit.service.j2").read_text(
        encoding="ascii"
    )
    assert (
        "ExecStart=/srv/menhir/scaffold/bin/menhir_scaffold.py verify --app-only"
        in audit
    )
    assert not (template_root / "menhir-caddy-reconcile.service.j2").exists()
    assert not (template_root / "menhir-caddy-reconcile.path.j2").exists()


def test_readme_declares_cloudflared_authority_and_bounded_scope():
    readme = (ANSIBLE / "README.md").read_text(encoding="ascii").lower()
    assert "cloudflared is the authoritative public ingress" in readme
    assert "does not install or modify cloudflared" in readme
    for term in (
        "tunnel credentials",
        "application files",
        "images",
        "containers",
        "databases",
        "caddy route state",
        "secrets",
    ):
        assert term in readme
