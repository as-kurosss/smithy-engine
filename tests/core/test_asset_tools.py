"""Asset tools and secret handling in the flow runner."""

from __future__ import annotations

from typing import Any

import pytest

from smithy.core.asset_tools import AssetCredentialTool, AssetGetTool
from smithy.core.assets import EnvAssetProvider, HttpAssetProvider, asset_provider_from_env
from smithy.core.errors import InvalidInput
from smithy.core.registry import ToolRegistry
from smithy.flow import FlowRunner


def _doc(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> dict[str, Any]:
    return {"version": 2, "nodes": nodes, "edges": edges}


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(AssetGetTool())
    registry.register(AssetCredentialTool())
    return registry


class TestAssetTools:
    @pytest.mark.asyncio
    async def test_get_text_asset(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("SMITHY_ASSET_CRM_URL", "https://crm.example")
        result = await AssetGetTool().execute({"name": "crm-url"})
        assert result == "https://crm.example"

    @pytest.mark.asyncio
    async def test_get_unknown_asset_raises(self) -> None:
        with pytest.raises(InvalidInput, match="Unknown asset"):
            await AssetGetTool().execute({"name": "nope"})

    @pytest.mark.asyncio
    async def test_credential_fields(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("SMITHY_ASSET_CRM_LOGIN", "alice")
        monkeypatch.setenv("SMITHY_ASSET_CRM_PASSWORD", "s3cret")
        result = await AssetCredentialTool().execute({"name": "crm"})
        assert result == {"login": "alice", "password": "s3cret"}

    @pytest.mark.asyncio
    async def test_credential_missing_name(self) -> None:
        with pytest.raises(InvalidInput, match="name"):
            await AssetGetTool().execute({})


class TestSecretHandling:
    @pytest.mark.asyncio
    async def test_value_is_redacted_and_excluded_from_results(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("SMITHY_ASSET_CRM_PASSWORD", "topsecret")
        logs: list[tuple[str, str]] = []
        doc = _doc(
            [
                {"id": "s", "kind": "start", "config": {}},
                {
                    "id": "get",
                    "kind": "tool",
                    "tool": "asset.get",
                    "config": {"name": "crm.password"},
                    "save_as": "pwd",
                },
                {"id": "e", "kind": "end", "config": {}},
            ],
            [
                {"id": "e1", "source": "s", "source_handle": "out", "target": "get"},
                {"id": "e2", "source": "get", "source_handle": "out", "target": "e"},
            ],
        )
        runner = FlowRunner(_registry(), log=lambda level, msg: logs.append((level, msg)))
        assert await runner.run(doc) == "finished"

        # The secret is registered and never appears in any log line.
        assert "topsecret" in runner._secrets
        assert all("topsecret" not in msg for _level, msg in logs)
        # ...and the variable is withheld from the run result snapshot.
        assert "pwd" not in runner.public_variables()
        assert "pwd" in runner._variables


class TestProviderSelection:
    def test_env_provider_credential(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("SMITHY_ASSET_CRM_LOGIN", "alice")
        monkeypatch.setenv("SMITHY_ASSET_CRM_PASSWORD", "s3cret")
        assert EnvAssetProvider().credential("crm") == {"login": "alice", "password": "s3cret"}

    def test_factory_prefers_orchestrator(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("SMITHY_ORCHESTRATOR_URL", "http://127.0.0.1:9")
        monkeypatch.setenv("SMITHY_AGENT_ID", "a1")
        monkeypatch.setenv("SMITHY_AGENT_TOKEN", "t")
        assert isinstance(asset_provider_from_env(), HttpAssetProvider)

    def test_factory_falls_back_to_env(self, monkeypatch: Any) -> None:
        for key in ("SMITHY_ORCHESTRATOR_URL", "SMITHY_AGENT_ID", "SMITHY_AGENT_TOKEN"):
            monkeypatch.delenv(key, raising=False)
        assert isinstance(asset_provider_from_env(), EnvAssetProvider)


class TestHttpAssetProvider:
    def test_get_by_name_field_and_id(self) -> None:
        import json
        import threading
        import urllib.parse
        from http.server import BaseHTTPRequestHandler, HTTPServer

        gid = "550e8400-e29b-41d4-a716-446655440000"
        credential = {
            "id": gid,
            "name": "crm",
            "kind": "credential",
            "value": "",
            "fields": {"login": "alice", "password": "s3cret"},
        }
        assets = {
            "crm-url": {"id": gid, "name": "crm-url", "kind": "text", "value": "u", "fields": {}},
            "crm": credential,
            gid: credential,
        }

        class Handler(BaseHTTPRequestHandler):
            paths: list[str] = []

            def log_message(self, *args: Any) -> None:
                pass

            def do_GET(self) -> None:
                Handler.paths.append(self.path)
                ref = urllib.parse.unquote(self.path.rsplit("/", 1)[-1].split("?")[0])
                data = assets.get(ref)
                if data is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                body = json.dumps(data).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = HTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            provider = HttpAssetProvider(f"http://127.0.0.1:{port}", agent_id="a1", token="t")
            assert provider.get("crm-url") == "u"
            assert provider.get("crm.password") == "s3cret"
            assert provider.credential("crm") == {"login": "alice", "password": "s3cret"}
            assert provider.credential(gid) == {"login": "alice", "password": "s3cret"}
            with pytest.raises(InvalidInput, match="Unknown asset"):
                provider.get("nope")

            scoped = HttpAssetProvider(
                f"http://127.0.0.1:{port}", agent_id="a1", token="t", process_id="p1"
            )
            assert scoped.get("crm-url") == "u"
            assert any("process_id=p1" in path for path in Handler.paths)
        finally:
            server.shutdown()
