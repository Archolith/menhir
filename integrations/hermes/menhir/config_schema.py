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
        ProviderField("write_namespace", "Write namespace", KIND_TEXT, default="hermes", inline=True,
                      description="Where Hermes writes. Reads search every namespace the credential can see."),
        ProviderField("client_name", "Client name", KIND_TEXT, default="hermes", inline=True,
                      description="Sent as X-Menhir-Client-Name; must match a registered client if the server "
                                  "restricts clients."),
        ProviderField("api_key", "Agent-tier bearer token", KIND_SECRET, env_key="HERMES_MENHIR_TOKEN",
                      description="Optional for loopback no-auth Menhir; never stored in provider JSON."),
        ProviderField("write_enabled", "Enable explicit write tool", KIND_BOOL, default="false", inline=True,
                      description="Lets menhir_remember stage proposals; does not ingest ordinary turns."),
        ProviderField("max_tokens", "Automatic context token budget", KIND_NUMBER, default="800"),
        ProviderField("timeout_seconds", "REST timeout in seconds", KIND_NUMBER, default="2.5"),
    ),
)
