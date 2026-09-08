"""Tests for the process lifecycle actions (wait / status)."""

from __future__ import annotations

import ctypes
from typing import Any

import pytest

from smithy.core.errors import InvalidInput, PlatformError
from smithy.windows.tools.process import ProcessTool

_STILL_ACTIVE = 259


class _FakeKernel32:
    def __init__(
        self, *, wait_result: int = 0, exit_code: int = 0, open_result: int = 1234
    ) -> None:
        self.wait_result = wait_result
        self.exit_code = exit_code
        self.open_result = open_result
        self.opened_pids: list[int] = []
        self.closed: list[int] = []
        self.waited_ms: int | None = None

    def OpenProcess(self, access: int, inherit: bool, pid: int) -> int:
        self.opened_pids.append(pid)
        return self.open_result

    def WaitForSingleObject(self, handle: int, timeout_ms: int) -> int:
        self.waited_ms = timeout_ms
        return self.wait_result

    def GetExitCodeProcess(self, handle: int, byref: Any) -> int:
        byref._obj.value = self.exit_code
        return 1

    def CloseHandle(self, handle: int) -> int:
        self.closed.append(handle)
        return 1


class _FakeWindll:
    def __init__(self, kernel32: _FakeKernel32) -> None:
        self.kernel32 = kernel32


def _patch_ctypes(monkeypatch: pytest.MonkeyPatch, kernel32: _FakeKernel32) -> None:
    monkeypatch.setattr(ctypes, "windll", _FakeWindll(kernel32), raising=False)


class TestProcessStatus:
    @pytest.mark.asyncio
    async def test_running(self, monkeypatch: pytest.MonkeyPatch) -> None:
        kernel32 = _FakeKernel32(exit_code=_STILL_ACTIVE)
        _patch_ctypes(monkeypatch, kernel32)
        result = await ProcessTool().execute({"action": "status", "pid": 4242})
        assert result == {"pid": 4242, "running": True, "exit_code": None}
        assert kernel32.opened_pids == [4242]
        assert kernel32.closed

    @pytest.mark.asyncio
    async def test_exited(self, monkeypatch: pytest.MonkeyPatch) -> None:
        kernel32 = _FakeKernel32(exit_code=3)
        _patch_ctypes(monkeypatch, kernel32)
        result = await ProcessTool().execute({"action": "status", "pid": 1})
        assert result == {"pid": 1, "running": False, "exit_code": 3}

    @pytest.mark.asyncio
    async def test_open_failed_means_not_running(self, monkeypatch: pytest.MonkeyPatch) -> None:
        kernel32 = _FakeKernel32(exit_code=0, open_result=0)
        _patch_ctypes(monkeypatch, kernel32)
        result = await ProcessTool().execute({"action": "status", "pid": 1})
        assert result == {"pid": 1, "running": False, "exit_code": None}


class TestProcessWait:
    @pytest.mark.asyncio
    async def test_exit_code_reported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        kernel32 = _FakeKernel32(wait_result=0, exit_code=0)
        _patch_ctypes(monkeypatch, kernel32)
        result = await ProcessTool().execute({"action": "wait", "pid": 7, "timeout_ms": 1500})
        assert result == {"status": "exited", "pid": 7, "exit_code": 0}
        assert kernel32.waited_ms is not None
        assert kernel32.waited_ms.value == 1500

    @pytest.mark.asyncio
    async def test_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        kernel32 = _FakeKernel32(wait_result=0x102, exit_code=_STILL_ACTIVE)
        _patch_ctypes(monkeypatch, kernel32)
        result = await ProcessTool().execute({"action": "wait", "pid": 7})
        assert result == {"status": "timeout", "pid": 7}

    @pytest.mark.asyncio
    async def test_open_failure_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        kernel32 = _FakeKernel32(wait_result=0, exit_code=0, open_result=0)
        _patch_ctypes(monkeypatch, kernel32)
        with pytest.raises(PlatformError, match="Cannot open process"):
            await ProcessTool().execute({"action": "wait", "pid": 7})

    @pytest.mark.asyncio
    async def test_validation(self) -> None:
        with pytest.raises(InvalidInput, match="pid"):
            await ProcessTool().execute({"action": "wait"})
        with pytest.raises(InvalidInput, match="timeout_ms"):
            await ProcessTool().execute({"action": "wait", "pid": 1, "timeout_ms": 0})
        with pytest.raises(InvalidInput, match="pid"):
            await ProcessTool().execute({"action": "status"})
