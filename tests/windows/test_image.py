"""Tests for smithy.windows.tools.image — find/click image fallback."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from smithy.core.errors import ElementNotFound, InvalidInput
from smithy.windows.tools.image import ClickImageTool, FindImageTool, _find_on_screen


class TestValidation:
    @pytest.mark.asyncio
    async def test_template_required(self) -> None:
        with pytest.raises(InvalidInput, match="template"):
            await FindImageTool().execute({})

    @pytest.mark.asyncio
    async def test_confidence_range(self, tmp_path: Path) -> None:
        template = tmp_path / "t.png"
        template.write_bytes(b"png")
        with pytest.raises(InvalidInput, match="confidence"):
            await FindImageTool().execute({"template": str(template), "confidence": 1.5})

    @pytest.mark.asyncio
    async def test_region_validation(self, tmp_path: Path) -> None:
        template = tmp_path / "t.png"
        template.write_bytes(b"png")
        with pytest.raises(InvalidInput, match="width"):
            await FindImageTool().execute(
                {"template": str(template), "x": 0, "y": 0, "width": 0, "height": 5}
            )

    @pytest.mark.asyncio
    async def test_clicks_validation(self, tmp_path: Path) -> None:
        template = tmp_path / "t.png"
        template.write_bytes(b"png")
        with pytest.raises(InvalidInput, match="clicks"):
            await ClickImageTool().execute({"template": str(template), "clicks": 3})


class TestFindImage:
    @pytest.mark.asyncio
    async def test_found(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        template = tmp_path / "t.png"
        template.write_bytes(b"png")
        monkeypatch.setattr(
            "smithy.windows.tools.image._find_on_screen",
            lambda *args: (True, (150, 250), 0.93),
        )
        result = await FindImageTool().execute({"template": str(template)})
        assert result == {"found": True, "x": 150, "y": 250, "confidence": 0.93}

    @pytest.mark.asyncio
    async def test_not_found(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        template = tmp_path / "t.png"
        template.write_bytes(b"png")
        monkeypatch.setattr(
            "smithy.windows.tools.image._find_on_screen",
            lambda *args: (False, (0, 0), 0.42),
        )
        with pytest.raises(ElementNotFound, match="not found on screen"):
            await FindImageTool().execute({"template": str(template)})

    @pytest.mark.asyncio
    async def test_click_image(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        template = tmp_path / "t.png"
        template.write_bytes(b"png")
        monkeypatch.setattr(
            "smithy.windows.tools.image._find_on_screen",
            lambda *args: (True, (60, 80), 0.9),
        )
        clicks: list[tuple[Any, ...]] = []

        import smithy.windows.tools.click as click_module

        def fake_click_at(x: int, y: int, button: str, count: int) -> None:
            clicks.append((x, y, button, count))

        monkeypatch.setattr(click_module, "_click_at", fake_click_at)
        result = await ClickImageTool().execute(
            {"template": str(template), "button": "right", "clicks": 2}
        )
        assert result["status"] == "clicked"
        assert clicks == [(60, 80, "right", 2)]

    @pytest.mark.asyncio
    async def test_sandbox_escape(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SMITHY_FILE_ROOT", str(tmp_path / "root"))
        (tmp_path / "root").mkdir()
        outside = tmp_path / "elsewhere.png"
        outside.write_bytes(b"png")
        with pytest.raises(InvalidInput, match="escapes"):
            await FindImageTool().execute({"template": str(outside)})


class TestFindOnScreen:
    def test_template_bigger_than_region(self, tmp_path: Path) -> None:
        pytest.importorskip("cv2")
        pytest.importorskip("mss")

        template = tmp_path / "big.png"
        template.write_bytes(b"png")
        with pytest.MonkeyPatch.context() as mp:
            import cv2
            import mss as mss_module
            import numpy

            fake_tpl = numpy.zeros((50, 50, 3), dtype=numpy.uint8)
            mp.setattr(cv2, "imread", lambda *a, **k: fake_tpl)

            class _FakeShot:
                bgra = b"\x00" * (10 * 10 * 4)
                height = 10
                width = 10

            class _FakeSct:
                monitors: list[dict[str, int]] = [
                    {},
                    {"left": 0, "top": 0, "width": 10, "height": 10},
                ]

                def __enter__(self) -> _FakeSct:
                    return self

                def __exit__(self, *args: Any) -> None:
                    pass

                def grab(self, monitor: Any) -> _FakeShot:
                    return _FakeShot()

            mp.setattr(mss_module, "mss", lambda: _FakeSct())
            with pytest.raises(InvalidInput, match="larger"):
                _find_on_screen(template, None, 0.8)
