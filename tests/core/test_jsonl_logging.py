"""Tests for smithcore.core.logging — JsonlEventLogger."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from smithcore.core.errors import ElementNotFound
from smithcore.core.events import ToolEvent
from smithcore.core.logging import JsonlEventLogger
from smithcore.core.transactions import current_transaction_id


def _read_lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


class TestJsonlEventLogger:
    @pytest.mark.asyncio
    async def test_logs_success_event(self, tmp_path: Path) -> None:
        path = tmp_path / "runs.jsonl"
        logger = JsonlEventLogger(path)
        event = ToolEvent(
            tool_name="windows.click",
            config={"name": "OK"},
            result={"status": "clicked"},
            duration_ms=12.5,
        )
        try:
            returned = await logger(event)
        finally:
            logger.close()
        assert returned is event
        (record,) = _read_lines(path)
        assert record["tool"] == "windows.click"
        assert record["config"] == {"name": "OK"}
        assert record["result"] == {"status": "clicked"}
        assert record["duration_ms"] == 12.5
        assert record["error"] is None
        assert record["transaction_id"] is None
        assert "ts" in record

    @pytest.mark.asyncio
    async def test_logs_error_event(self, tmp_path: Path) -> None:
        path = tmp_path / "runs.jsonl"
        logger = JsonlEventLogger(path)
        event = ToolEvent(
            tool_name="windows.click",
            config={},
            error=ElementNotFound("no button"),
        )
        try:
            await logger(event)
        finally:
            logger.close()
        (record,) = _read_lines(path)
        assert record["error"] == {"type": "ElementNotFound", "message": "no button"}
        assert record["result"] is None

    @pytest.mark.asyncio
    async def test_stamps_current_transaction_id(self, tmp_path: Path) -> None:
        path = tmp_path / "runs.jsonl"
        logger = JsonlEventLogger(path)
        token = current_transaction_id.set("tx-42")
        try:
            await logger(ToolEvent(tool_name="t", config={}))
        finally:
            current_transaction_id.reset(token)
            logger.close()
        (record,) = _read_lines(path)
        assert record["transaction_id"] == "tx-42"

    @pytest.mark.asyncio
    async def test_include_flags_drop_sensitive_fields(self, tmp_path: Path) -> None:
        path = tmp_path / "runs.jsonl"
        logger = JsonlEventLogger(path, include_config=False, include_result=False)
        try:
            await logger(ToolEvent(tool_name="t", config={"secret": 1}, result={"x": 2}))
        finally:
            logger.close()
        (record,) = _read_lines(path)
        assert "config" not in record
        assert "result" not in record
        assert record["tool"] == "t"

    @pytest.mark.asyncio
    async def test_appends_multiple_lines(self, tmp_path: Path) -> None:
        path = tmp_path / "runs.jsonl"
        logger = JsonlEventLogger(path)
        try:
            await logger(ToolEvent(tool_name="a", config={}))
            await logger(ToolEvent(tool_name="b", config={}))
        finally:
            logger.close()
        records = _read_lines(path)
        assert [record["tool"] for record in records] == ["a", "b"]

    @pytest.mark.asyncio
    async def test_redact_scrubs_config_result_and_error(self, tmp_path: Path) -> None:
        path = tmp_path / "runs.jsonl"
        logger = JsonlEventLogger(path, redact=["s3cret"])
        try:
            await logger(
                ToolEvent(
                    tool_name="t",
                    config={"password": "s3cret"},
                    result="s3cret",
                    error=ElementNotFound("bad s3cret"),
                )
            )
        finally:
            logger.close()
        (record,) = _read_lines(path)
        assert record["config"] == {"password": "***"}
        assert record["result"] == "***"
        assert record["error"]["message"] == "bad ***"
