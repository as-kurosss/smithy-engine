"""Asset tools — fetch secrets at run time without storing them in the flow.

The agent injects assets as environment variables (``SMITHCORE_ASSET_*`` for a
text asset, ``SMITHCORE_ASSET_<NAME>_<FIELD>`` for credential fields); these
tools read them back.

Both tools set :attr:`AbstractTool.produces_secrets`, so the flow runner
redacts the returned values from logs/errors and keeps the variables they
land in out of the run-result snapshot sent back to the orchestrator.
"""

from __future__ import annotations

from typing import Any

from smithcore.core.assets import asset_provider_from_env
from smithcore.core.errors import InvalidInput
from smithcore.core.tool import AbstractTool


def _asset_name(config: dict[str, Any]) -> str:
    name = config.get("name")
    if not isinstance(name, str) or not name.strip():
        raise InvalidInput(
            "Missing required parameter: name (asset name)",
            param="name",
            input_value=name,
        )
    return name.strip()


class AssetGetTool(AbstractTool):
    """Fetch a text asset (secret) by name."""

    produces_secrets = True

    @property
    def name(self) -> str:
        return "asset.get"

    @property
    def description(self) -> str:
        return "Fetches a text asset (secret) from the agent environment by name"

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Asset name (e.g. crm-url) or a credential field (crm.login)",
                },
            },
            "required": ["name"],
        }

    async def execute(self, config: dict[str, Any]) -> Any:
        return asset_provider_from_env().get(_asset_name(config))


class AssetCredentialTool(AbstractTool):
    """Fetch a credential asset's fields (login / password / …)."""

    produces_secrets = True

    @property
    def name(self) -> str:
        return "asset.credential"

    @property
    def description(self) -> str:
        return "Fetches a credential asset's fields (login, password, …) by name"

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Credential asset name (e.g. crm)"},
            },
            "required": ["name"],
        }

    async def execute(self, config: dict[str, Any]) -> Any:
        return asset_provider_from_env().credential(_asset_name(config))
