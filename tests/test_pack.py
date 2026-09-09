"""Tests for smithy.pack — build/verify manifests and run_flow --pack."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from smithy import run_flow
from smithy.core.errors import InvalidInput
from smithy.pack import (
    PACK_MANIFEST,
    build_pack,
    fetch_pack,
    load_manifest,
    publish_pack,
    verify_pack,
    zip_pack,
)


def _make_pack(tmp_path: Path) -> Path:
    root = tmp_path / "pack"
    root.mkdir()
    flow = {
        "version": 2,
        "nodes": [
            {"id": "s", "kind": "start", "config": {}},
            {"id": "e", "kind": "end", "config": {}},
        ],
        "edges": [{"id": "e1", "source": "s", "source_handle": "out", "target": "e"}],
    }
    (root / "process.flow.json").write_text(json.dumps(flow), encoding="utf-8")
    (root / "selectors.json").write_text("{}", encoding="utf-8")
    (root / "robot.toml").write_text("[paths]\nworkdir = '.'\n", encoding="utf-8")
    (root / "queue.db").write_bytes(b"junk")
    return root


class TestBuildVerify:
    def test_build_and_verify_roundtrip(self, tmp_path: Path) -> None:
        root = _make_pack(tmp_path)
        build_pack(root, name="1c-invoices", version="1.0.0")
        assert verify_pack(root) == []
        manifest = load_manifest(root)
        assert manifest["schema"] == "smithy-pack-v1"
        assert manifest["name"] == "1c-invoices"
        assert manifest["entry"] == {"process": "process.flow.json"}
        listed = {item["path"] for item in manifest["files"]}
        assert listed == {"process.flow.json", "selectors.json"}

    def test_tampered_file_detected(self, tmp_path: Path) -> None:
        root = _make_pack(tmp_path)
        build_pack(root, name="p", version="1")
        (root / "selectors.json").write_text("{}", encoding="utf-8")
        (root / "selectors.json").write_text('{"tampered": true}', encoding="utf-8")
        problems = verify_pack(root)
        assert any("checksum mismatch: selectors.json" in p for p in problems)

    def test_missing_file_detected(self, tmp_path: Path) -> None:
        root = _make_pack(tmp_path)
        build_pack(root, name="p", version="1")
        (root / "selectors.json").unlink()
        problems = verify_pack(root)
        assert any("listed file is missing: selectors.json" in p for p in problems)

    def test_no_manifest(self, tmp_path: Path) -> None:
        root = tmp_path / "empty"
        root.mkdir()
        assert verify_pack(root) == [f"no {PACK_MANIFEST} in {root} — build the pack first"]

    def test_explicit_entry_validated(self, tmp_path: Path) -> None:
        root = _make_pack(tmp_path)
        with pytest.raises(Exception):  # noqa: B017, PT011 — entry missing file
            build_pack(root, name="p", version="1", entry={"process": "nope.json"})

    def test_robot_toml_and_db_not_listed(self, tmp_path: Path) -> None:
        root = _make_pack(tmp_path)
        build_pack(root, name="p", version="1")
        manifest = load_manifest(root)
        listed = {item["path"] for item in manifest["files"]}
        assert "robot.toml" not in listed
        assert "queue.db" not in listed

    def test_venv_and_caches_not_listed(self, tmp_path: Path) -> None:
        root = _make_pack(tmp_path)
        venv = root / ".venv" / "Lib" / "site-packages" / "pkg"
        venv.mkdir(parents=True)
        (venv / "module.py").write_text("x = 1", encoding="utf-8")
        (root / ".git" / "HEAD").parent.mkdir(parents=True)
        (root / ".git" / "HEAD").write_text("ref: main", encoding="utf-8")
        (root / ".pytest_cache" / "v").mkdir(parents=True)
        (root / ".pytest_cache" / "v" / "cache.json").write_text("{}", encoding="utf-8")
        build_pack(root, name="p", version="1")
        manifest = load_manifest(root)
        listed = {item["path"] for item in manifest["files"]}
        assert listed == {"process.flow.json", "selectors.json"}


class TestPackCli:
    def test_cli_build_and_verify(self, tmp_path: Path) -> None:
        root = _make_pack(tmp_path)
        build = subprocess.run(
            [
                sys.executable,
                "-m",
                "smithy.pack",
                "build",
                str(root),
                "--name",
                "p",
                "--version",
                "1.0.0",
            ],
            capture_output=True,
            text=True,
        )
        assert build.returncode == 0, build.stderr
        verify = subprocess.run(
            [sys.executable, "-m", "smithy.pack", "verify", str(root)],
            capture_output=True,
            text=True,
        )
        assert verify.returncode == 0, verify.stderr
        assert "pack verified" in verify.stdout


class TestDelivery:
    def test_zip_roundtrip(self, tmp_path: Path) -> None:
        root = _make_pack(tmp_path)
        build_pack(root, name="p", version="1.0.0")
        archive = zip_pack(root)
        assert archive.name == "p-1.0.0.zip"

        dest = tmp_path / "client"
        fetched = fetch_pack(str(archive), dest)
        assert fetched == dest
        assert verify_pack(dest) == []
        assert (dest / "process.flow.json").is_file()

    def test_fetch_rejects_tampered_archive(self, tmp_path: Path) -> None:
        import zipfile

        root = _make_pack(tmp_path)
        build_pack(root, name="p", version="1.0.0")
        archive = zip_pack(root)

        tampered_dir = tmp_path / "tampered"
        tampered_dir.mkdir()
        tampered_zip = tampered_dir / "p.zip"
        with zipfile.ZipFile(archive) as zin, zipfile.ZipFile(tampered_zip, "w") as zout:
            for item in zin.infolist():
                data = zin.read(item.filename)
                if item.filename == "selectors.json":
                    data = b'{"tampered": true}'
                zout.writestr(item, data)

        with pytest.raises(InvalidInput, match="checksum mismatch"):
            fetch_pack(str(tampered_zip), tmp_path / "client")

    def test_zip_slip_rejected(self, tmp_path: Path) -> None:
        import zipfile

        evil = tmp_path / "evil.zip"
        with zipfile.ZipFile(evil, "w") as zf:
            zf.writestr("../escape.json", "{}")
            zf.writestr(PACK_MANIFEST, json.dumps({"schema": "smithy-pack-v1"}))
        dest = tmp_path / "victim"
        dest.mkdir()
        with pytest.raises(InvalidInput, match="unsafe path"):
            fetch_pack(str(evil), dest)
        assert not (tmp_path / "escape.json").exists()

    def test_fetch_missing_source(self, tmp_path: Path) -> None:
        with pytest.raises(InvalidInput, match="not found"):
            fetch_pack(str(tmp_path / "nope.zip"), tmp_path / "out")

    def test_zip_refuses_unverified_pack(self, tmp_path: Path) -> None:
        root = _make_pack(tmp_path)
        with pytest.raises(InvalidInput, match="failed verification"):
            zip_pack(root)


class TestPublish:
    """publish/push — build + verify + zip + orchestrator upload in one step."""

    @staticmethod
    def _fake_orchestrator(responses: dict[str, tuple[int, bytes]]) -> Any:
        """Local HTTP server answering each URL path with a canned response.

        ``server.requests`` collects ``(path, authorization, body)`` tuples.
        """

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 — http.server API
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                code, extra = responses.get(self.path, (404, b'{"detail": "not found"}'))
                self.send_response(code)
                self.send_header("Content-Length", str(len(extra)))
                self.end_headers()
                self.wfile.write(extra)
                server.requests.append((self.path, self.headers.get("Authorization", ""), body))

            def log_message(self, *args: Any) -> None:
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        server.requests = []  # type: ignore[attr-defined]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    def test_publish_uploads_verified_zip(self, tmp_path: Path) -> None:
        root = _make_pack(tmp_path)
        server = self._fake_orchestrator({"/packs/p/versions/1.0.0": (201, b"")})
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            archive = publish_pack(root, name="p", version="1.0.0", api_url=base, token="secret")
        finally:
            server.shutdown()
        assert archive.name == "p-1.0.0.zip"
        assert verify_pack(root) == []
        path, auth, body = server.requests[0]
        assert path == "/packs/p/versions/1.0.0"
        assert auth == "Bearer secret"
        assert b'filename="p-1.0.0.zip"' in body
        assert PACK_MANIFEST.encode() in body

    def test_publish_rejects_http_non_loopback(self, tmp_path: Path) -> None:
        root = _make_pack(tmp_path)
        with pytest.raises(InvalidInput, match="https"):
            publish_pack(
                root,
                name="p",
                version="1.0.0",
                api_url="http://cloud.example.com/api",
                token="t",
            )

    def test_publish_http_non_loopback_with_insecure(self, tmp_path: Path) -> None:
        root = _make_pack(tmp_path)
        server = self._fake_orchestrator({"/packs/p/versions/1.0.0": (201, b"")})
        host = f"http://127.0.0.1:{server.server_port}"
        try:
            publish_pack(
                root,
                name="p",
                version="1.0.0",
                api_url=host.replace("127.0.0.1", "localhost"),
                token="t",
            )
        finally:
            server.shutdown()

    def test_publish_duplicate_version_raises(self, tmp_path: Path) -> None:
        root = _make_pack(tmp_path)
        server = self._fake_orchestrator(
            {"/packs/p/versions/1.0.0": (409, b'{"detail": "exists"}')}
        )
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with pytest.raises(InvalidInput, match="409.*exists"):
                publish_pack(root, name="p", version="1.0.0", api_url=base, token="t")
        finally:
            server.shutdown()

    def test_push_local_zip_without_api_url(self, tmp_path: Path) -> None:
        root = _make_pack(tmp_path)
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "smithy.pack",
                "push",
                str(root),
                "--name",
                "p",
                "--version",
                "1.0.0",
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1
        assert "SMITHY_API_URL" in result.stderr

    def test_push_uploads_to_orchestrator(self, tmp_path: Path) -> None:
        import os

        root = _make_pack(tmp_path)
        server = self._fake_orchestrator({"/packs/p/versions/1.0.0": (201, b"")})
        base = f"http://127.0.0.1:{server.server_port}"
        env = {**os.environ, "SMITHY_API_TOKEN": "tok"}
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "smithy.pack",
                    "push",
                    str(root),
                    "--name",
                    "p",
                    "--version",
                    "1.0.0",
                    "--api-url",
                    base,
                ],
                capture_output=True,
                text=True,
                env=env,
            )
        finally:
            server.shutdown()
        assert result.returncode == 0, result.stderr
        assert "pack published" in result.stdout
        path, auth, _body = server.requests[0]
        assert path == "/packs/p/versions/1.0.0"
        assert auth == "Bearer tok"


class TestRunFlowPack:
    def test_pack_stage_runs_verified(self, tmp_path: Path) -> None:
        root = _make_pack(tmp_path)
        build_pack(root, name="p", version="1")
        assert run_flow.main(["--pack", str(root), "--stage", "process"]) == 0

    def test_tampered_pack_refuses_to_run(self, tmp_path: Path) -> None:
        root = _make_pack(tmp_path)
        build_pack(root, name="p", version="1")
        (root / "process.flow.json").write_text(
            json.dumps({"version": 2, "nodes": [], "edges": []}), encoding="utf-8"
        )
        assert run_flow.main(["--pack", str(root), "--stage", "process"]) == 1

    def test_missing_stage_reports(self, tmp_path: Path, capsys: Any) -> None:
        root = _make_pack(tmp_path)
        build_pack(root, name="p", version="1")
        with pytest.raises(SystemExit):
            run_flow.main(["--pack", str(root), "--stage", "end"])

    def test_pack_tools_py_autoloaded(self, tmp_path: Path) -> None:
        root = _make_pack(tmp_path)
        flow = {
            "version": 2,
            "nodes": [
                {"id": "s", "kind": "start", "config": {}},
                {"id": "t", "kind": "tool", "tool": "pack.marker", "config": {}},
                {"id": "e", "kind": "end", "config": {}},
            ],
            "edges": [
                {"id": "e1", "source": "s", "source_handle": "out", "target": "t"},
                {"id": "e2", "source": "t", "source_handle": "out", "target": "e"},
            ],
        }
        (root / "process.flow.json").write_text(json.dumps(flow), encoding="utf-8")
        (root / "tools.py").write_text(
            "from smithy.core.tool import AbstractTool\n"
            "from typing import Any\n"
            "class M(AbstractTool):\n"
            "    @property\n"
            "    def name(self) -> str:\n"
            "        return 'pack.marker'\n"
            "    @property\n"
            "    def description(self) -> str:\n"
            "        return 'm'\n"
            "    def schema(self) -> dict:\n"
            "        return {'type': 'object'}\n"
            "    async def execute(self, config: dict) -> Any:\n"
            "        return {'ok': True}\n"
            "TOOLS = [M()]\n",
            encoding="utf-8",
        )
        build_pack(root, name="p", version="1")
        assert run_flow.main(["--pack", str(root), "--stage", "process"]) == 0
