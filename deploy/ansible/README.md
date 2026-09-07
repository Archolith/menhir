# Menhir host prerequisites

This playbook manages only persistent, machine-level prerequisites for a Menhir
host. It creates the required root-owned directories, installs the Menhir
tmpfiles policy, installs the scaffold-audit systemd units, reloads systemd when
unit files change, and keeps the audit timer enabled and active. It also stops,
disables, and removes the retired `menhir-caddy-reconcile` units so the shared
Caddy configuration cannot become a second Menhir ingress writer.

Cloudflared is the authoritative public ingress for this deployment. This
playbook deliberately does not install or modify Cloudflared, tunnel
credentials, tunnel configuration, DNS, or public routes. Those remain under
the existing ingress authority and its separate reviewed procedure.

The role also does not deploy application files, release bundles, images,
containers, databases, durable application data, Caddy route state, or secrets.
The installed audit unit invokes a release-owned read-only verification command.

## Inventory

Copy `inventory.example.yml` to `inventory.yml` and replace the documentation
placeholders with the target SSH host and operator account. Keep passwords,
private keys, tokens, tunnel credentials, and application secrets out of both
files. Supply SSH authentication through the agent or your normal external
Ansible configuration.

The example inventory uses the reserved `.invalid` domain and cannot identify a
production machine. Review `group_vars/all.yml` before applying the role. Its
directory contract is intentionally explicit and every managed path is owned by
root with a fixed mode.

## Entry points

From `deploy/ansible`:

```text
ansible-playbook -i inventory.yml --check --diff playbook.yml
ansible-playbook -i inventory.yml --diff playbook.yml
```

The first command is the review entry point. The second applies only the host
prerequisites after the check-mode diff has been reviewed. No external Ansible
collections or roles are required; `requirements.yml` records that empty
dependency set.

Run the SSH host assertions from the repository root after an apply:

```text
pytest --hosts=ansible://menhir_hosts \
  --ansible-inventory=deploy/ansible/inventory.yml \
  deploy/ansible/tests/test_host_state.py
```

The repository-local contract suite is `tests/test_ansible_host_state.py`. It
parses the playbook and role without importing Ansible and rejects inventories
containing secret-shaped data or roles that gain deployment mutation powers.
