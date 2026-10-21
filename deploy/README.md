# Menhir self-host and image verification

The supported public MVP path is the local install and server described in the
[root README](../README.md). `menhir up --compose-neo4j` starts a local,
disposable Neo4j instance; `menhir check`, `menhir diagnostics`, and
`menhir serve` verify the package and API. These commands do not require the
operator deployment repository.

The files here also retain the generic, sealed-image build and disposable
Docker test stacks. `Dockerfile` deliberately installs only from a prepared,
hash-verified `deploy/wheelhouse` and requires a digest-pinned `PYTHON_BASE`.
It cannot build from a plain clone. CI prepares that wheelhouse and uses
`build_release_image.py` to bind the image to its source and wheel digests.
The Docker stacks expect an already built and verified image tagged
`menhir:test`; they do not build or publish one.

## Disposable image checks

After preparing or loading a verified image as `menhir:test`, create an
operator credential and start the auth-only test stack:

```bash
export MENHIR_OPERATOR_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
docker compose -f deploy/docker-compose.test.yml up -d
curl -fsS http://127.0.0.1:8099/api/health
```

This stack binds Menhir only to loopback, uses an isolated state volume, and
does not start Neo4j by default. See comments in `docker-compose.test.yml` to
enable its throwaway Neo4j service for full-mode checks. The separate
`docker-compose.full.yml` stack requires a configured model provider and a
disposable database; its example uses paid OpenAI models. Neither stack is a
production template.

With the network-bound container, credential-free bootstrap is disabled.
Use `MENHIR_OPERATOR_KEY` to create the first client token, for example:

```bash
curl -fsS -X POST http://127.0.0.1:8099/api/admin/clients \
  -H "authorization: Bearer $MENHIR_OPERATOR_KEY" \
  -H 'content-type: application/json' \
  -d '{"client_name":"my-agent","tier":"operator"}'
```

The local backup helper, example environment file, and image verification
tools remain in this public directory. Operator-specific host configuration,
production runbooks, release receipts, staging, and promotion live in the
private `Archolith/menhir-deploy` repository. Removing them from the current
public tree does not remove previously published Git history.
