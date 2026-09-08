"""Asset provider — runtime secrets for bots.

Config files deliberately store only *references* to secrets (asset
names, GUIDs) — never values. Values are fetched at runtime through an
:class:`AssetProvider`, called from bot code (``bot.asset("db_password")``)
instead of flowing through tool configs/results — so they can never
leak into the JSONL audit log or flow files.

The default provider reads environment variables (``SMITHY_ASSET_*``);
wire an orchestrator-backed implementation behind the same protocol.
"""

from __future__ import annotations

import os
import re
from typing import Protocol, runtime_checkable

from smithy.core.errors import InvalidInput

DEFAULT_ASSET_PREFIX = "SMITHY_ASSET_"


@runtime_checkable
class AssetProvider(Protocol):
    """Secret source contract."""

    def get(self, name: str) -> str:
        """Return the secret value for *name*; raise if unknown."""
        ...


class EnvAssetProvider:
    """Asset provider over ``SMITHY_ASSET_*`` environment variables.

    ``bot.asset("db.password")`` reads ``SMITHY_ASSET_DB_PASSWORD``:
    the name is upper-cased and every non-alphanumeric run becomes a
    single underscore.
    """

    def __init__(self, prefix: str = DEFAULT_ASSET_PREFIX) -> None:
        if not isinstance(prefix, str) or not prefix:
            raise InvalidInput(
                "prefix must be a non-empty string", param="prefix", input_value=prefix
            )
        self._prefix = prefix

    @property
    def prefix(self) -> str:
        """Env var prefix of this provider."""
        return self._prefix

    def get(self, name: str) -> str:
        """Look up the env var for *name*.

        Raises:
            InvalidInput: If *name* is empty or the env var is not set.
        """
        if not isinstance(name, str) or not name.strip():
            raise InvalidInput(
                "asset name must be a non-empty string", param="name", input_value=name
            )
        key = self._prefix + re.sub(r"[^A-Z0-9]+", "_", name.strip().upper())
        value = os.environ.get(key)
        if value is None:
            raise InvalidInput(
                f"Unknown asset {name!r}: environment variable {key!r} is not set",
                param="name",
                input_value=name,
            )
        return value
