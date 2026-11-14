"""Hermes Dashboard configuration for this directory memory provider (pure data)."""

from plugins.memory.config_schema import (
    KIND_BOOL,
    KIND_NUMBER,
    KIND_SECRET,
    KIND_TEXT,
    STORAGE_PROVIDER,
    ProviderConfigSchema,
    ProviderField,
)

CONFIG_SCHEMA = ProviderConfigSchema(
    name="menhir",
    label="Menhir",
    storage=STORAGE_PROVIDER,
    fields=(
        ProviderField("base_url", "Menhir REST URL", KIND_TEXT,
                      default="http://127.0.0.1:8100/api", inline=True),
        ProviderField("namespace", "Namespace", KIND_TEXT, default="", inline=True,
                      description="Blank uses the server's default namespace."),
        ProviderField("api_key", "Agent-tier bearer token", KIND_SECRET, env_key="HERMES_MENHIR_TOKEN",
                      description="Optional for loopback no-auth Menhir; never stored in provider JSON."),
        ProviderField("write_enabled", "Enable explicit write tool", KIND_BOOL, default="false", inline=True,
                      description="Adds menhir_remember; does not ingest ordinary turns."),
        ProviderField("max_tokens", "Automatic context token budget", KIND_NUMBER, default="800"),
        ProviderField("timeout_seconds", "REST timeout in seconds", KIND_NUMBER, default="2.5"),
    ),
)
