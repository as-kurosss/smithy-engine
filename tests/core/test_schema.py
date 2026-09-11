"""Tests for smithcore.core.schema and registry-level config validation."""

from __future__ import annotations

from typing import Any

import pytest

from smithcore.core.errors import InvalidInput
from smithcore.core.registry import ToolRegistry
from smithcore.core.schema import validate_against_schema
from smithcore.core.tool import AbstractTool


class TestValidateAgainstSchema:
    def test_empty_schema_allows_anything(self) -> None:
        assert validate_against_schema({}, {"whatever": 1}) == []

    def test_valid_config(self) -> None:
        schema: dict[str, Any] = {
            "type": "object",
            "properties": {"timeout_ms": {"type": "integer", "minimum": 1}},
            "required": [],
        }
        assert validate_against_schema(schema, {"timeout_ms": 100}) == []

    def test_missing_required(self) -> None:
        schema: dict[str, Any] = {
            "type": "object",
            "properties": {"action": {"type": "string"}},
            "required": ["action"],
        }
        problems = validate_against_schema(schema, {})
        assert len(problems) == 1
        assert "action" in problems[0]

    def test_wrong_type(self) -> None:
        schema: dict[str, Any] = {
            "type": "object",
            "properties": {"timeout_ms": {"type": "integer"}},
        }
        problems = validate_against_schema(schema, {"timeout_ms": "soon"})
        assert len(problems) == 1
        assert "integer" in problems[0]

    def test_bool_is_not_integer(self) -> None:
        schema: dict[str, Any] = {
            "type": "object",
            "properties": {"duration_ms": {"type": "integer"}},
        }
        assert validate_against_schema(schema, {"duration_ms": True}) != []

    def test_enum_violation(self) -> None:
        schema: dict[str, Any] = {
            "type": "object",
            "properties": {"action": {"type": "string", "enum": ["start", "stop"]}},
            "required": ["action"],
        }
        problems = validate_against_schema(schema, {"action": "reboot"})
        assert len(problems) == 1
        assert "reboot" in problems[0]

    def test_minimum_and_maximum(self) -> None:
        schema: dict[str, Any] = {
            "type": "object",
            "properties": {"n": {"type": "integer", "minimum": 1, "maximum": 10}},
        }
        assert "minimum" in validate_against_schema(schema, {"n": 0})[0]
        assert "maximum" in validate_against_schema(schema, {"n": 11})[0]
        assert validate_against_schema(schema, {"n": 5}) == []

    def test_array_items(self) -> None:
        schema: dict[str, Any] = {
            "type": "object",
            "properties": {"args": {"type": "array", "items": {"type": "string"}}},
        }
        problems = validate_against_schema(schema, {"args": ["ok", 42]})
        assert len(problems) == 1
        assert "[1]" in problems[0]

    def test_additional_properties_false(self) -> None:
        schema: dict[str, Any] = {
            "type": "object",
            "properties": {"a": {"type": "string"}},
            "additionalProperties": False,
        }
        problems = validate_against_schema(schema, {"a": "x", "b": "y"})
        assert len(problems) == 1
        assert "b" in problems[0]

    def test_unknown_properties_allowed_by_default(self) -> None:
        schema: dict[str, Any] = {"type": "object", "properties": {}}
        assert validate_against_schema(schema, {"x": 1}) == []

    def test_unknown_keywords_ignored(self) -> None:
        schema: dict[str, Any] = {
            "type": "object",
            "properties": {"x": {"type": "string", "format": "email", "minLength": 3}},
        }
        assert validate_against_schema(schema, {"x": "y"}) == []

    def test_nested_object(self) -> None:
        schema: dict[str, Any] = {
            "type": "object",
            "properties": {
                "proxy": {
                    "type": "object",
                    "properties": {"port": {"type": "integer"}},
                    "required": ["port"],
                }
            },
        }
        problems = validate_against_schema(schema, {"proxy": {}})
        assert len(problems) == 1
        assert "port" in problems[0]


class SchemaTool(AbstractTool):
    """Tool with a constraining schema for registry tests."""

    def __init__(self) -> None:
        self.calls = 0

    @property
    def name(self) -> str:
        return "schema.tool"

    @property
    def description(self) -> str:
        return "Schema test tool."

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"action": {"type": "string", "enum": ["start", "stop"]}},
            "required": ["action"],
        }

    async def execute(self, config: dict[str, Any]) -> Any:
        self.calls += 1
        return "ran"


class TestRegistryValidation:
    @pytest.mark.asyncio
    async def test_valid_config_executes(self) -> None:
        reg = ToolRegistry()
        tool = SchemaTool()
        reg.register(tool)
        assert await reg.execute("schema.tool", {"action": "start"}) == "ran"
        assert tool.calls == 1

    @pytest.mark.asyncio
    async def test_missing_required_rejected_without_running(self) -> None:
        reg = ToolRegistry()
        tool = SchemaTool()
        reg.register(tool)
        with pytest.raises(InvalidInput, match="schema.tool"):
            await reg.execute("schema.tool", {})
        assert tool.calls == 0

    @pytest.mark.asyncio
    async def test_enum_violation_rejected_without_running(self) -> None:
        reg = ToolRegistry()
        tool = SchemaTool()
        reg.register(tool)
        with pytest.raises(InvalidInput, match="reboot"):
            await reg.execute("schema.tool", {"action": "reboot"})
        assert tool.calls == 0
