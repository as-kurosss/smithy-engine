"""OcrTool — read text from a screenshot via Windows.Media.OCR.

The zero-dependency OCR for UIA-invisible UIs (Citrix, RDP, canvases):
Windows 10+ ships an OCR engine (the one powering Search/Magnifier),
exposed here through Windows PowerShell 5.1 WinRT interop — no
tesseract binary, no native wheels.

Flow: capture (``path`` to an existing image, or an on-screen region
grabbed with ``mss``) → recognize → ``{"text": ...}``.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from smithcore.core.blocking import run_blocking
from smithcore.core.errors import InvalidInput, PlatformError
from smithcore.core.files import confine_path
from smithcore.core.tool import AbstractTool

# WinRT interop in Windows PowerShell 5.1: activate the required WinRT
# types, bridge IAsyncOperation with AsTask, then OCR the image file.
#
# The image path and language are passed through the environment
# (SMITHCORE_OCR_PATH / SMITHCORE_OCR_LANG) — never interpolated into the
# script text — so user-controlled values cannot inject PowerShell.
_OCR_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
# Force UTF-8 on stdout so non-ASCII OCR text survives the pipe.
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Storage.StorageFile, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Storage.Streams.IRandomAccessStream,
         Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Globalization.Language, Windows.Globalization, ContentType = WindowsRuntime]

$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() |
    Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
                   $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($WinRtTask, $ResultType) {
    $asTask = $asTaskGeneric.MakeGenericMethod($ResultType)
    $netTask = $asTask.Invoke($null, @($WinRtTask))
    $netTask.Wait(-1) | Out-Null
    $netTask.Result
}

if ($env:SMITHCORE_OCR_LANG -ne '') {
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage(
        [Windows.Globalization.Language]::new($env:SMITHCORE_OCR_LANG))
} else {
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
}
if ($null -eq $engine) { throw 'OCR engine unavailable for the requested language' }

$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($env:SMITHCORE_OCR_PATH)) `
    ([Windows.Storage.StorageFile])
$stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) `
    ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) `
    ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = Await ($decoder.GetSoftwareBitmapAsync()) `
    ([Windows.Graphics.Imaging.SoftwareBitmap])
$result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
Write-Output $result.Text
"""


class OcrTool(AbstractTool):
    """Read text from an image or screen region (Windows OCR)."""

    @property
    def name(self) -> str:
        return "windows.ocr"

    @property
    def description(self) -> str:
        return (
            "Reads text from an image file or screen region using the "
            "built-in Windows OCR engine (works where UIA cannot see)"
        )

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Image file to read (or omit to capture a region)",
                },
                "x": {"type": "integer", "description": "Region left (with y/width/height)"},
                "y": {"type": "integer", "description": "Region top"},
                "width": {"type": "integer", "description": "Region width"},
                "height": {"type": "integer", "description": "Region height"},
                "language": {
                    "type": "string",
                    "description": "BCP-47 tag, e.g. 'en-US' / 'ru-RU' "
                    "(default: user profile languages)",
                },
            },
            "required": [],
        }

    async def execute(self, config: dict[str, Any]) -> Any:
        language = config.get("language", "")
        if not isinstance(language, str):
            raise InvalidInput(
                "Invalid 'language': expected a string tag like 'en-US'",
                param="language",
                input_value=language,
            )
        raw_path = config.get("path")
        region = _check_region(config)

        cleanup: Path | None = None
        if raw_path is not None:
            if not isinstance(raw_path, (str, os.PathLike)) or not str(raw_path):
                raise InvalidInput(
                    "Invalid 'path': expected an image file path",
                    param="path",
                    input_value=raw_path,
                )
            image_path = confine_path(Path(raw_path))
        else:
            if region is None:
                raise InvalidInput(
                    "Provide 'path' (an image file) or a screen region "
                    "(x, y, width, height) to capture",
                    param="path",
                    input_value=None,
                )
            cleanup = await run_blocking(_capture_region, region)
            image_path = cleanup

        try:
            text: str = await run_blocking(_recognize, image_path, language)
        except (InvalidInput, PlatformError):
            raise
        except Exception as exc:
            raise PlatformError(f"OCR failed: {exc}", source=exc) from exc
        finally:
            if cleanup is not None:
                from smithcore.core.blocking import run_blocking as _rb

                await _rb(_unlink_quiet, cleanup)
        return {"text": text, "chars": len(text)}


def _check_region(config: dict[str, Any]) -> tuple[int, int, int, int] | None:
    keys = ("x", "y", "width", "height")
    if not any(key in config for key in keys):
        return None
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
    return (values["x"], values["y"], values["width"], values["height"])


def _capture_region(region: tuple[int, int, int, int]) -> Path:
    """Grab a screen region to a temp PNG (runs in an executor)."""
    import mss
    from PIL import Image

    left, top, width, height = region
    with mss.mss() as sct:
        shot = sct.grab({"left": left, "top": top, "width": width, "height": height})
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    handle = tempfile.NamedTemporaryFile(suffix=".png", delete=False)  # noqa: SIM115
    handle.close()
    path = Path(handle.name)
    img.save(str(path), format="PNG")
    return path


def _powershell_exe() -> str:
    """Absolute path to Windows PowerShell (never a PATH lookup)."""
    root = os.environ.get("SYSTEMROOT", r"C:\Windows")
    candidate = Path(root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    return str(candidate) if candidate.is_file() else "powershell.exe"


def _recognize(image_path: Path, language: str) -> str:
    """Run the PowerShell OCR script (runs in an executor)."""
    env = dict(os.environ)
    env["SMITHCORE_OCR_PATH"] = str(image_path)
    env["SMITHCORE_OCR_LANG"] = language
    try:
        completed = subprocess.run(
            [
                _powershell_exe(),
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                _OCR_SCRIPT,
            ],
            capture_output=True,
            text=True,
            timeout=60,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise PlatformError("Windows OCR timed out after 60s") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise PlatformError(f"Windows OCR failed: {detail}")
    return completed.stdout.strip()


def _unlink_quiet(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink()
