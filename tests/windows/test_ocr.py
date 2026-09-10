"""Tests for smithy.windows.tools.ocr — Windows OCR tool."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from smithy.core.errors import InvalidInput, PlatformError
from smithy.windows.tools import ocr as ocr_module
from smithy.windows.tools.ocr import OcrTool


class TestOcrValidation:
    @pytest.mark.asyncio
    async def test_requires_path_or_region(self) -> None:
        with pytest.raises(InvalidInput, match="region"):
            await OcrTool().execute({})

    @pytest.mark.asyncio
    async def test_region_validation(self, tmp_path: Path) -> None:
        with pytest.raises(InvalidInput, match="width"):
            await OcrTool().execute({"x": 0, "y": 0, "width": 0, "height": 10})
        with pytest.raises(InvalidInput, match="'x'"):
            await OcrTool().execute({"x": "0", "y": 0, "width": 10, "height": 10})

    @pytest.mark.asyncio
    async def test_language_validation(self) -> None:
        with pytest.raises(InvalidInput, match="language"):
            await OcrTool().execute({"path": "x.png", "language": 5})


class TestOcrExecution:
    @pytest.mark.asyncio
    async def test_reads_text_from_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        image = tmp_path / "shot.png"
        image.write_bytes(b"png")

        captured: dict[str, Any] = {}

        def fake_run(args: Any, **kwargs: Any) -> MagicMock:
            captured["script"] = args[-1]
            captured["env"] = kwargs.get("env", {})
            done = MagicMock()
            done.returncode = 0
            done.stdout = "ИНН 7701234567\nИтого: 1500\n"
            done.stderr = ""
            return done

        monkeypatch.setattr(subprocess, "run", fake_run)
        result = await OcrTool().execute({"path": str(image)})
        assert result["text"] == "ИНН 7701234567\nИтого: 1500"
        assert result["chars"] == len(result["text"])
        assert "__PATH__" not in captured["script"]
        assert captured["env"]["SMITHY_OCR_PATH"] == str(image)
        assert captured["env"]["SMITHY_OCR_LANG"] == ""

    @pytest.mark.asyncio
    async def test_language_forwarded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        image = tmp_path / "shot.png"
        image.write_bytes(b"png")

        captured: dict[str, Any] = {}

        def fake_run(args: Any, **kwargs: Any) -> MagicMock:
            captured["script"] = args[-1]
            captured["env"] = kwargs.get("env", {})
            done = MagicMock()
            done.returncode = 0
            done.stdout = "ok"
            done.stderr = ""
            return done

        monkeypatch.setattr(subprocess, "run", fake_run)
        await OcrTool().execute({"path": str(image), "language": "ru-RU"})
        assert captured["env"]["SMITHY_OCR_LANG"] == "ru-RU"
        assert "__LANG__" not in captured["script"]

    @pytest.mark.asyncio
    async def test_failure_wrapped(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        image = tmp_path / "shot.png"
        image.write_bytes(b"png")

        def fake_run(args: Any, **kwargs: Any) -> MagicMock:
            done = MagicMock()
            done.returncode = 1
            done.stdout = ""
            done.stderr = "OCR engine unavailable"
            return done

        monkeypatch.setattr(subprocess, "run", fake_run)
        with pytest.raises(PlatformError, match="OCR engine unavailable"):
            await OcrTool().execute({"path": str(image)})

    @pytest.mark.asyncio
    async def test_region_capture_cleans_temp(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import os
        import tempfile
        from pathlib import Path

        created: list[Path] = []

        def fake_capture(region: tuple[int, int, int, int]) -> Path:
            fd, name = tempfile.mkstemp(suffix=".png")
            os.close(fd)
            handle = Path(name)
            handle.write_bytes(b"png")
            created.append(handle)
            return handle

        monkeypatch.setattr(ocr_module, "_capture_region", fake_capture)
        monkeypatch.setattr(ocr_module, "_recognize", lambda path, lang: "text")

        result = await OcrTool().execute({"x": 0, "y": 0, "width": 100, "height": 50})
        assert result["text"] == "text"
        assert not created[0].exists()
