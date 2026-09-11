"""Hand-rolled JSON Schema subset validator for tool configs.

: meth:`ToolRegistry.execute` validates the ``config`` dict against the
tool's ``schema()`` *before* execution so typos and type errors fail fast
instead of surfacing as obscure mid-run failures.

Only the keywords actually used by smithcore tools are supported (``type``,
``required``, ``properties``, ``items``, ``enum``, ``minimum``,
``maximum``, ``additionalProperties``); every unknown keyword is ignored
so schemas stay forward-compatible with the full JSON Schema spec.
"""

from __future__ import annotations

from typing import Any


def _check_type(expected: str, value: Any) -> bool:
    """Check *value* against a JSON Schema type name."""
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        # bool is an int subclass — True is never a valid integer param.
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "null":
        return value is None
    return True  # Unknown type name — ignore, don't invent errors.


def validate_against_schema(
    schema: dict[str, Any],
    config: dict[str, Any],
) -> list[str]:
    """Return human-readable problems of *config* against *schema*.

    An empty list means valid. An empty (or non-dict) schema constrains
    nothing and always validates.
    """
    problems: list[str] = []
    _validate(schema, config, "$", problems)
    return problems


def _validate(schema: Any, value: Any, path: str, problems: list[str]) -> None:
    if not isinstance(schema, dict) or not schema:
        return
    expected = schema.get("type")
    if isinstance(expected, str) and not _check_type(expected, value):
        problems.append(f"{path}: expected {expected}, got {type(value).__name__}")
        return  # Further checks on a mistyped value would only add noise.

    if isinstance(value, dict):
        required = schema.get("required")
        if isinstance(required, list):
            for name in required:
                if name not in value:
                    problems.append(f"{path}: missing required property {name!r}")
        properties = schema.get("properties")
        if isinstance(properties, dict):
            for name, subschema in properties.items():
                if name in value:
                    _validate(subschema, value[name], f"{path}.{name}", problems)
            if schema.get("additionalProperties") is False:
                for name in value:
                    if name not in properties:
                        problems.append(f"{path}: unexpected property {name!r}")

    if isinstance(value, list):
        items = schema.get("items")
        if isinstance(items, dict):
            for index, item in enumerate(value):
                _validate(items, item, f"{path}[{index}]", problems)

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        if isinstance(minimum, (int, float)) and not isinstance(minimum, bool) and value < minimum:
            problems.append(f"{path}: {value!r} is less than minimum {minimum!r}")
        maximum = schema.get("maximum")
        if isinstance(maximum, (int, float)) and not isinstance(maximum, bool) and value > maximum:
            problems.append(f"{path}: {value!r} is greater than maximum {maximum!r}")

    enum = schema.get("enum")
    if isinstance(enum, list) and value not in enum:
        problems.append(f"{path}: {value!r} is not one of {enum!r}")
