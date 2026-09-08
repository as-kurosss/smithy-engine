"""Tests for smithy.core.assets — EnvAssetProvider + facade asset()."""

from __future__ import annotations

import pytest

from smithy.core.assets import DEFAULT_ASSET_PREFIX, EnvAssetProvider
from smithy.core.errors import InvalidInput
from smithy.facade import Smithy


class TestEnvAssetProvider:
    def test_reads_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SMITHY_ASSET_DB_PASSWORD", "s3cret")
        assert EnvAssetProvider().get("db.password") == "s3cret"

    def test_name_normalization(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SMITHY_ASSET_API_KEY_2", "k")
        provider = EnvAssetProvider()
        assert provider.get("api-key-2") == "k"
        assert provider.get("API_KEY_2") == "k"

    def test_custom_prefix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VAULT_TOKEN", "t")
        assert EnvAssetProvider(prefix="VAULT_").get("token") == "t"

    def test_missing_asset_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SMITHY_ASSET_NOPE", raising=False)
        with pytest.raises(InvalidInput, match="SMITHY_ASSET_NOPE"):
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
        monkeypatch.setenv("SMITHY_ASSET_TOKEN", "abc")
        assert Smithy().asset("token") == "abc"

    def test_custom_provider(self) -> None:
        class StaticProvider:
            def get(self, name: str) -> str:
                return f"value:{name}"

        assert Smithy(assets=StaticProvider()).asset("x") == "value:x"
