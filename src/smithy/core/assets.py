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

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Protocol, runtime_checkable

from smithy.core.errors import InvalidInput, PlatformError

DEFAULT_ASSET_PREFIX = "SMITHY_ASSET_"


def _normalize(name: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", name.strip().upper())


@runtime_checkable
class AssetProvider(Protocol):
    """Secret source contract."""

    def get(self, name: str) -> str:
        """Return the secret value for *name*; raise if unknown."""
        ...

    def credential(self, name: str) -> dict[str, str]:
        """Return a credential's fields (or ``{"value": ...}`` for text)."""
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
        self._cache: dict[str, str] = {}

    @property
    def prefix(self) -> str:
        """Env var prefix of this provider."""
        return self._prefix

    def get(self, name: str) -> str:
        """Look up the env var for *name*.

        Results are cached for the lifetime of the provider, so a bot
        that fetches the same asset repeatedly pays the environment
        lookup once.

        Raises:
            InvalidInput: If *name* is empty or the env var is not set.
        """
        if not isinstance(name, str) or not name.strip():
            raise InvalidInput(
                "asset name must be a non-empty string", param="name", input_value=name
            )
        key = self._prefix + re.sub(r"[^A-Z0-9]+", "_", name.strip().upper())
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        value = os.environ.get(key)
        if value is None:
            raise InvalidInput(
                f"Unknown asset {name!r}: environment variable {key!r} is not set",
                param="name",
                input_value=name,
            )
        self._cache[key] = value
        return value

    def credential(self, name: str) -> dict[str, str]:
        """Credential fields for *name*, or ``{"value": ...}`` for text."""
        fields = credential_fields(name, self._prefix)
        if fields:
            return fields
        return {"value": self.get(name)}


def credential_fields(name: str, prefix: str = DEFAULT_ASSET_PREFIX) -> dict[str, str]:
    """All ``<prefix><NAME>_<FIELD>`` env vars for a credential *name*.

    The agent injects a credential as one env var per field, e.g. asset
    ``crm`` with ``login``/``password`` becomes ``SMITHY_ASSET_CRM_LOGIN``
    and ``SMITHY_ASSET_CRM_PASSWORD``. Keys are returned lower-cased
    (``{"login": ..., "password": ...}``); an empty result means either a
    text asset or an unknown name.
    """
    base = prefix + _normalize(name)
    marker = base + "_"
    fields: dict[str, str] = {}
    for key, value in os.environ.items():
        if key.startswith(marker) and value != "":
            field = key[len(marker) :].lower()
            if field:
                fields[field] = value
    return fields


class HttpAssetProvider:
    """Fetch assets on demand from a smithy-cloud orchestrator.

    Resolves an asset by **id/GUID or name** (and ``name.field`` for a
    credential field), so the engine no longer needs the whole vault
    injected into the environment. Only used when the agent configures
    ``SMITHY_ORCHESTRATOR_URL`` + ``SMITHY_AGENT_ID`` + a token; otherwise
    :class:`EnvAssetProvider` is used.
    """

    def __init__(
        self,
        base_url: str,
        *,
        agent_id: str,
        token: str,
        process_id: str | None = None,
        timeout: float = 15.0,
        allow_insecure: bool = False,
    ) -> None:
        parsed = urllib.parse.urlparse(base_url)
        if parsed.scheme not in ("http", "https"):
            raise InvalidInput(
                "orchestrator URL must be http(s)", param="base_url", input_value=base_url
            )
        host = parsed.hostname or ""
        loopback = host in ("127.0.0.1", "::1", "localhost")
        if parsed.scheme == "http" and not loopback and not allow_insecure:
            raise InvalidInput(
                "plain http to a non-loopback orchestrator is refused "
                "(assets would travel in cleartext)",
                param="base_url",
                input_value=base_url,
            )
        self._base = base_url.rstrip("/")
        self._agent_id = str(agent_id)
        self._token = token
        self._process_id = process_id
        self._timeout = timeout
        self._cache: dict[str, dict[str, Any]] = {}

    def _fetch(self, ref: str) -> dict[str, Any] | None:
        """One asset by id or name; ``None`` on 404 (so ``name.field`` can split)."""
        if ref in self._cache:
            return self._cache[ref]
        url = (
            f"{self._base}/api/agents/{urllib.parse.quote(self._agent_id, safe='')}"
            f"/assets/{urllib.parse.quote(ref, safe='')}"
        )
        if self._process_id:
            url += f"?process_id={urllib.parse.quote(self._process_id, safe='')}"
        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {self._token}"})
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                data: Any = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            exc.close()
            if exc.code == 404:
                return None
            raise PlatformError(f"asset fetch failed with HTTP {exc.code}", source=exc) from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise PlatformError(f"asset fetch failed: {exc}", source=exc) from exc
        if isinstance(data, dict):
            self._cache[ref] = data
            return data
        return None

    def get(self, name: str) -> str:
        data = self._fetch(name)
        if data is not None:
            if str(data.get("kind")) == "credential":
                raise InvalidInput(
                    f'asset {name!r} is a credential — use "name.field" or asset.credential',
                    param="name",
                    input_value=name,
                )
            return str(data.get("value") or "")
        if "." in name:
            base, _, field = name.rpartition(".")
            parent = self._fetch(base)
            fields = (parent or {}).get("fields")
            if isinstance(fields, dict) and field in fields:
                return str(fields[field])
        raise InvalidInput(f"Unknown asset {name!r}", param="name", input_value=name)

    def credential(self, name: str) -> dict[str, str]:
        data = self._fetch(name)
        if data is None and "." in name:
            data = self._fetch(name.rpartition(".")[0])
        if data is None:
            raise InvalidInput(f"Unknown asset {name!r}", param="name", input_value=name)
        if str(data.get("kind")) == "credential":
            fields = data.get("fields") or {}
            if isinstance(fields, dict):
                return {str(key): str(value) for key, value in fields.items()}
            return {}
        return {"value": str(data.get("value") or "")}


def asset_provider_from_env() -> AssetProvider:
    """Orchestrator-backed provider when configured, else env vars.

    The agent sets ``SMITHY_ORCHESTRATOR_URL`` / ``SMITHY_AGENT_ID`` /
    ``SMITHY_AGENT_TOKEN``; self-host runs use ``SMITHY_ASSET_*`` directly.
    """
    url = os.environ.get("SMITHY_ORCHESTRATOR_URL")
    agent_id = os.environ.get("SMITHY_AGENT_ID")
    token = os.environ.get("SMITHY_AGENT_TOKEN") or os.environ.get("SMITHY_API_TOKEN")
    process_id = os.environ.get("SMITHY_PROCESS_ID") or None
    if url and agent_id and token:
        return HttpAssetProvider(url, agent_id=agent_id, token=token, process_id=process_id)
    return EnvAssetProvider()
