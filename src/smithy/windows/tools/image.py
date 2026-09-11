"""Image-based automation fallback (Citrix, RDP, Java apps, canvases).

UIA cannot see pixels: remote desktops, legacy Java/Qt windows and
canvas renderings expose no accessibility tree. These tools locate a
template image (saved from :class:`ScreenshotTool`) on screen via
OpenCV template matching and optionally click it.

Requires the ``image`` extra::

    pip install "smithy-engine[image]"   # numpy + opencv-python
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from smithy.core.blocking import run_blocking
from smithy.core.errors import ElementNotFound, InvalidInput, PlatformError
from smithy.core.files import confine_path
from smithy.core.tool import AbstractTool

_DEFAULT_CONFIDENCE = 0.8

#: Search-region DoS guard: 8000x8000 ≈ 64M pixels ≈ 256MB RGBA.
_MAX_REGION_PIXELS = 64_000_000
_MAX_REGION_SIDE = 8000


class FindImageTool(AbstractTool):
    """Locate a template image on screen; returns the match center."""

    @property
    def name(self) -> str:
        return "windows.find_image"

    @property
    def description(self) -> str:
        return (
            "Finds a template image on screen (OpenCV template matching) "
            "and returns the match center; the UIA-free fallback for "
            "Citrix/RDP/canvas UIs"
        )

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "template": {"type": "string", "description": "Path to the template PNG"},
                "confidence": {
                    "type": "number",
                    "minimum": 0.0,
                    "maximum": 1.0,
                    "default": _DEFAULT_CONFIDENCE,
                    "description": "Match threshold (normalized correlation)",
                },
                "x": {"type": "integer", "description": "Search region left"},
                "y": {"type": "integer", "description": "Search region top"},
                "width": {"type": "integer", "description": "Search region width"},
                "height": {"type": "integer", "description": "Search region height"},
            },
            "required": ["template"],
        }

    async def execute(self, config: dict[str, Any]) -> Any:
        import asyncio as _asyncio

        template, region, confidence = _validate(config)
        try:
            # CV work (mss/cv2) must not hog the single COM/UIA worker thread.
            found, center, score = await _asyncio.to_thread(
                _find_on_screen, template, region, confidence
            )
        except (InvalidInput, PlatformError):
            raise
        except Exception as exc:
            raise PlatformError("Image search failed", source=exc) from exc
        if not found:
            raise ElementNotFound(
                f"Template {str(template)!r} not found on screen "
                f"(best score {score:.3f} < confidence {confidence})",
                selector={"template": str(template), "confidence": confidence},
            )
        return {
            "found": True,
            "x": center[0],
            "y": center[1],
            "confidence": round(score, 4),
        }


class ClickImageTool(AbstractTool):
    """Locate a template image on screen and click its center."""

    @property
    def name(self) -> str:
        return "windows.click_image"

    @property
    def description(self) -> str:
        return "Clicks the center of a template image found on screen (UIA-free fallback)"

    def schema(self) -> dict[str, Any]:
        schema = FindImageTool().schema()
        assert isinstance(schema["properties"], dict)
        schema["properties"]["button"] = {
            "type": "string",
            "enum": ["left", "right"],
            "default": "left",
            "description": "Mouse button",
        }
        schema["properties"]["clicks"] = {
            "type": "integer",
            "enum": [1, 2],
            "default": 1,
            "description": "Single (1) or double (2) click",
        }
        schema["required"] = ["template"]
        return schema

    async def execute(self, config: dict[str, Any]) -> Any:
        import asyncio as _asyncio

        template, region, confidence = _validate(config)
        button = config.get("button", "left")
        if not isinstance(button, str) or button not in ("left", "right"):
            raise InvalidInput(
                "Invalid 'button': expected 'left' or 'right'",
                param="button",
                input_value=button,
            )
        clicks = config.get("clicks", 1)
        if isinstance(clicks, bool) or clicks not in (1, 2):
            raise InvalidInput(
                "Invalid 'clicks': expected 1 or 2",
                param="clicks",
                input_value=clicks,
            )
        try:
            found, center, score = await _asyncio.to_thread(
                _find_on_screen, template, region, confidence
            )
        except (InvalidInput, PlatformError):
            raise
        except Exception as exc:
            raise PlatformError("Image search failed", source=exc) from exc
        if not found:
            raise ElementNotFound(
                f"Template {str(template)!r} not found on screen "
                f"(best score {score:.3f} < confidence {confidence})",
                selector={"template": str(template), "confidence": confidence},
            )
        from smithy.windows.tools.click import _click_at

        try:
            await run_blocking(_click_at, center[0], center[1], button, clicks)
        except Exception as exc:
            raise PlatformError("Image click failed", source=exc) from exc
        return {
            "status": "clicked",
            "x": center[0],
            "y": center[1],
            "confidence": round(score, 4),
            "button": button,
            "clicks": clicks,
        }


def _validate(config: dict[str, Any]) -> tuple[Any, tuple[int, int, int, int] | None, float]:
    """Validate common config; returns (template_path, region, confidence)."""
    raw_template = config.get("template")
    if not isinstance(raw_template, (str, os.PathLike)) or not str(raw_template):
        raise InvalidInput(
            "Missing required parameter: template (expected an image path)",
            param="template",
            input_value=raw_template,
        )
    template = confine_path(Path(raw_template))

    confidence = config.get("confidence", _DEFAULT_CONFIDENCE)
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise InvalidInput(
            "Invalid 'confidence': expected a number in [0, 1]",
            param="confidence",
            input_value=confidence,
        )
    confidence = float(confidence)
    if not 0.0 <= confidence <= 1.0:
        raise InvalidInput(
            "Invalid 'confidence': expected a number in [0, 1]",
            param="confidence",
            input_value=confidence,
        )

    keys = ("x", "y", "width", "height")
    if not any(key in config for key in keys):
        return template, None, confidence
    values: dict[str, int] = {}
    for key in keys:
        value = config.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            raise InvalidInput(
                f"Invalid '{key}': expected an integer",
                param=key,
                input_value=value,
            )
        values[key] = value
    if values["width"] < 1 or values["height"] < 1:
        raise InvalidInput(
            "Invalid region: width/height must be >= 1",
            param="width",
            input_value=values,
        )
    if (
        values["width"] > _MAX_REGION_SIDE
        or values["height"] > _MAX_REGION_SIDE
        or values["width"] * values["height"] > _MAX_REGION_PIXELS
    ):
        raise InvalidInput(
            f"Invalid region: {values['width']}x{values['height']} too large "
            f"(max {_MAX_REGION_SIDE}px side, {_MAX_REGION_PIXELS} pixels)",
            param="width",
            input_value={"width": values["width"], "height": values["height"]},
        )
    return template, (values["x"], values["y"], values["width"], values["height"]), confidence


def _find_on_screen(
    template: Any,
    region: tuple[int, int, int, int] | None,
    confidence: float,
) -> tuple[bool, tuple[int, int], float]:
    """Grab the screen and template-match (runs in an executor)."""
    import cv2
    import mss
    import numpy

    tpl = cv2.imread(str(template), cv2.IMREAD_COLOR)
    if tpl is None:
        raise InvalidInput(
            f"Cannot read template image: {template}",
            param="template",
            input_value=str(template),
        )

    monitor = (
        {"left": region[0], "top": region[1], "width": region[2], "height": region[3]}
        if region is not None
        else None
    )
    with mss.mss() as sct:
        grab_area = monitor or sct.monitors[1]
        origin_x = int(grab_area.get("left", 0))
        origin_y = int(grab_area.get("top", 0))
        shot = sct.grab(grab_area)
        # Copy out of the mss buffer before the context closes: frombuffer
        # would otherwise alias freed memory.
        frame = numpy.frombuffer(shot.bgra, dtype=numpy.uint8).copy()
        frame = frame.reshape(shot.height, shot.width, 4)[:, :, :3]

    tpl_height, tpl_width = tpl.shape[:2]
    if tpl_height > frame.shape[0] or tpl_width > frame.shape[1]:
        raise InvalidInput(
            "Template is larger than the search region",
            param="template",
            input_value=str(template),
        )

    result = cv2.matchTemplate(frame, tpl, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(result)
    # max_loc is relative to the grabbed frame; add the region's screen
    # origin so callers get absolute screen coordinates.
    center = (
        int(origin_x + max_loc[0] + tpl_width / 2),
        int(origin_y + max_loc[1] + tpl_height / 2),
    )
    return bool(max_val >= confidence), center, float(max_val)
