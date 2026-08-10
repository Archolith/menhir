# Menhir Neo4j Python driver 6 dependency probe

Date: 2026-07-12  
Project: Menhir  
Current dependency: `neo4j>=5.0,<6` (`5.28.4` locked)  
Candidate: `neo4j==6.2.0`

## Verdict

Proceed. No Menhir code change or database migration was identified for the Python
driver upgrade. Neo4j 6.2.0 passed the full offline suite and connected successfully
to the running Neo4j 5.26.26 server through both Menhir's direct driver pattern and
Graphiti's `Neo4jDriver` wrapper.

## Menhir API usage

Menhir's direct driver surface is small:

- `GraphDatabase.driver(...)`;
- `driver.session()`;
- `session.run(...)` and result access;
- `driver.close()`.

The code does not use the deprecated `read_transaction` or `write_transaction`
methods removed at the driver-6 boundary. Graphiti Core 0.28.2 declares
`neo4j>=5.26.0` without an upper bound, so it does not block driver 6.

Driver 6 changes some post-close error behavior and stabilizes newer transaction
APIs, but none of the changed surfaces found in the official API documentation are
load-bearing in Menhir.

## Verification evidence

Focused Neo4j/Graphiti/provider overlay against Neo4j Python driver 6.2.0:

```text
95 passed, 3 skipped
```

Full offline overlay against Neo4j Python driver 6.2.0:

```text
2834 passed, 32 skipped
```

Read-only live probes:

```text
neo4j-driver 6.2.0 connectivity ok; server 5.26.26
graphiti Neo4jDriver over neo4j 6.2.0 ok; RETURN 1 => 1
```

The official compatibility matrix lists Neo4j 5.26 LTS as compatible with driver
6.x, and the driver manual states that the current 6.x line supports Neo4j 4.4,
5.x, 2025.x, and 2026.x servers:

- <https://neo4j.com/developer/kb/neo4j-supported-versions/>
- <https://neo4j.com/docs/python-manual/current/install/>
- <https://neo4j.com/docs/api/python-driver/current/api.html>

## Scope and recommendation

The implementation should only require:

1. Change the direct range to `neo4j>=6.2,<7`.
2. Regenerate and audit `uv.lock`.
3. Run the full frozen suite.
4. Repeat the read-only direct-driver and Graphiti-driver connectivity probes.

This upgrades the Python client only. It does not require upgrading the Neo4j
database server, rewriting Cypher, or migrating stored graph data. The installed
Neo4j 5.26.26 server is an LTS release and is explicitly supported by driver 6.x.

## Implementation result

Implemented on 2026-07-12. The direct dependency is now `neo4j>=6.2,<7`, and the
lock resolves Neo4j 6.2.0. A clean frozen environment reported no known third-party
vulnerabilities and passed the full offline suite:

```text
2834 passed, 32 skipped
```

The synchronized local environment also passed direct-driver and Graphiti-driver
read-only connectivity checks against Neo4j server 5.26.26.
