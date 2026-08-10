# Tasks: Debugging

Use this file for troubleshooting and incident-shaped work before opening the large reference docs.

## Common Tasks

### Enrichment is stuck
- read first:
  - `runtime.ops`
  - `runtime.storage`
  - `model.episode`
  - `mcp.tool.get_episode_trace`
  - `mcp.tool.list_enrichment_queue`
- then open:
  - [architecture.md](architecture.md)
  - [data_models.md](data_models.md)
  - [endpoints.md](endpoints.md)
  - [workflows/troubleshoot_enrichment_stalls.md](workflows/troubleshoot_enrichment_stalls.md)

### Startup or preflight failed
- read first:
  - `runtime.dependencies`
  - `runtime.ops`
  - `mcp.resource.system.dependency_health`
  - `mcp.resource.system.metadata`
- then open:
  - [architecture.md](architecture.md)
  - [endpoints.md](endpoints.md)

### Queue state looks wrong
- read first:
  - `model.episode`
  - `runtime.ops`
  - `mcp.tool.list_enrichment_queue`
  - `mcp.resource.system.processing_queue`
- then open:
  - [data_models.md](data_models.md)
  - [endpoints.md](endpoints.md)
  - [architecture.md](architecture.md)

### Retry or failure behavior is unclear
- read first:
  - `memory.policy.lifecycle`
  - `runtime.ops`
  - `model.episode`
- then open:
  - [retry-policy.yaml](retry-policy.yaml)
  - [memory-design.md](memory-design.md)
  - [architecture.md](architecture.md)

## Machine-Readable Helpers

- [processing-states.yaml](processing-states.yaml)
- [retry-policy.yaml](retry-policy.yaml)
- [mcp-tools.yaml](mcp-tools.yaml)
