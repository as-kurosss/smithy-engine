"""Tests for smithcore.core.assets — EnvAssetProvider + facade asset()."""

from __future__ import annotations

import json

import pytest

from smithcore.core.assets import DEFAULT_ASSET_PREFIX, EnvAssetProvider
from smithcore.core.errors import InvalidInput
from smithcore.core.events import ToolEvent
from smithcore.core.tool import tool
from smithcore.facade import SmithCore


class TestEnvAssetProvider:
    def test_reads_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SMITHCORE_ASSET_DB_PASSWORD", "s3cret")
        assert EnvAssetProvider().get("db.password") == "s3cret"

    def test_name_normalization(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SMITHCORE_ASSET_API_KEY_2", "k")
        provider = EnvAssetProvider()
        assert provider.get("api-key-2") == "k"
        assert provider.get("API_KEY_2") == "k"

    def test_custom_prefix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VAULT_TOKEN", "t")
        assert EnvAssetProvider(prefix="VAULT_").get("token") == "t"

    def test_missing_asset_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SMITHCORE_ASSET_NOPE", raising=False)
        with pytest.raises(InvalidInput, match="SMITHCORE_ASSET_NOPE"):
            EnvAssetProvider().get("nope")

    def test_empty_name_raises(self) -> None:
        with pytest.raises(InvalidInput, match="name"):
            EnvAssetProvider().get("  ")

    def test_empty_prefix_raises(self) -> None:
        with pytest.raises(InvalidInput, match="prefix"):
            EnvAssetProvider(prefix="")

    def test_default_prefix(self) -> None:
        assert EnvAssetProvider().prefix == DEFAULT_ASSET_PREFIX


class TestFacadeAsset:
    def test_default_provider(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SMITHCORE_ASSET_TOKEN", "abc")
        assert SmithCore().asset("token") == "abc"

    def test_custom_provider(self) -> None:
        class StaticProvider:
            def get(self, name: str) -> str:
                return f"value:{name}"

        assert SmithCore(assets=StaticProvider()).asset("x") == "value:x"


class TestFacadeSecretRedaction:
    @pytest.mark.asyncio
    async def test_asset_value_redacted_from_tool_events(self) -> None:
        @tool("echo")
        async def echo(config: dict) -> dict:
            return {"echo": config.get("text")}

        events: list[ToolEvent] = []

        async def capture(event: ToolEvent) -> ToolEvent:
            events.append(event)
            return event

        class StaticProvider:
            def get(self, name: str) -> str:
                return "s3cret-token"

        bot = SmithCore(tools=[echo], assets=StaticProvider())
        bot.add_middleware(capture)
        secret = bot.asset("token")
        await bot.call("echo", text=secret)

        (event,) = events
        assert "s3cret-token" not in json.dumps(event.config)
        assert "s3cret-token" not in json.dumps(event.result)
        assert event.config["text"] == "***"
        assert event.result["echo"] == "***"
