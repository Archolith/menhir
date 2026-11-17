# Security Policy

## Reporting a Vulnerability

Report security issues privately to **security@archolith.dev**. Please do not open a
public issue for a vulnerability.

Include what you have: affected version or commit, a description of the issue, and
whatever reproduction steps you can share. A proof of concept helps but is not required.

This is a small project, not a funded security program. Expect an acknowledgement within
about a week. There is no bounty.

## Supported Versions

Fixes land on `main` and ship in the next release; the latest 0.3.x release is the supported
version. There are no backported security releases.

## Scope

Menhir stores memory content in Neo4j and holds credentials for an LLM provider, so the
issues worth reporting are roughly:

- Authentication or authorization bypass on the HTTP or MCP surface, including anything
  that lets a caller reach a tier it was not granted.
- Anything that lets caller-controlled input override a verified OAuth identity.
- Reading or writing memory across a namespace boundary that should isolate it.
- Prompt injection through stored memory content that reaches a privileged action or
  crosses a trust boundary. See the known limitation below before reporting: the fact that
  stored content reaches the LLM unsanitized is already known.
- Credential or memory-content disclosure through logs, error responses, or telemetry.

## Known Design Limits (Not Vulnerabilities)

These are deliberate and documented, so please do not report them as findings:

- **Single-tenant by design.** Menhir assumes one operator. Multi-tenant isolation is not
  implemented, and caller-supplied identity headers are not bound to the API key that
  authenticated the request, so in a shared deployment a caller could attribute writes to
  another user. Run one instance per operator. `docs/security-posture.md` documents the
  threat model this posture sits inside.
- **Loopback no-auth mode.** With no key configured, the server serves an unauthenticated
  API on loopback. It refuses to bind a non-loopback address in that state unless
  `MENHIR_ALLOW_INSECURE_REMOTE_NO_AUTH=1` is set, which is documented as unsafe.
- **Erasure keeps extracted entities and facts (#417).** Deleting or erasing a memory does
  not remove the entities and fact edges extracted from it, which can restate its content.
  Deleting the namespace removes them. A fix is planned after 0.3.0; see the README's known
  limitations.
- **Stored memory content reaches the LLM unsanitized.** The contradiction check and the
  edge-fact repair / compression paths pass stored content into prompts without
  sanitization, so content written into memory can influence those LLM calls. This is a
  tracked open issue, not a surprise. A report that goes further -- showing injected
  content causing a privileged action, a cross-namespace read or write, or exfiltration --
  is in scope and worth sending.
- **Operator-supplied Cypher in maintenance scripts.** Scripts under `scripts/` are
  operator tools that take a database URI and run privileged queries. They are not a
  sandbox and are not intended to be exposed to untrusted callers.
