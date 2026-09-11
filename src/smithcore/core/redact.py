"""Redact secret values before they reach logs, traces or audit files.

Asset values (``bot.asset(...)`` / ``${asset:...}``) are the only things
tracked as secrets: the facade and the flow runner remember every value
an :class:`~smithcore.core.assets.AssetProvider` returned and scrub it from
tool events, rendered configs and results. Plain literals that were never
fetched through an asset are not tracked — put secrets behind an asset
reference to get redaction.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

REDACTED = "***"


def _normalize(secrets: Iterable[str] | None) -> tuple[str, ...]:
    if not secrets:
        return ()
    # Longest first so a secret that contains another is fully removed.
    return tuple(sorted({s for s in secrets if s}, key=len, reverse=True))


def redact_text(text: str, secrets: Iterable[str] | None) -> str:
    """Replace every occurrence of a secret in *text* with ``***``."""
    for secret in _normalize(secrets):
        if secret in text:
            text = text.replace(secret, REDACTED)
    return text


def _redact_key(key: Any, secrets: tuple[str, ...]) -> Any:
    if isinstance(key, str):
        for secret in secrets:
            if secret and secret in key:
                key = key.replace(secret, REDACTED)
        return key
    return key


def _redact(value: Any, secrets: tuple[str, ...]) -> Any:
    if isinstance(value, str):
        for secret in secrets:
            if secret in value:
                value = value.replace(secret, REDACTED)
        return value
    if isinstance(value, list):
        return [_redact(item, secrets) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact(item, secrets) for item in value)
    if isinstance(value, dict):
        return {_redact_key(key, secrets): _redact(item, secrets) for key, item in value.items()}
    return value


def redact_value(value: Any, secrets: Iterable[str] | None) -> Any:
    """Recursively redact secrets from a JSON-like value.

    Both string values and dict keys are scrubbed. Non-container
    values are returned unchanged.
    """
    normalized = _normalize(secrets)
    if not normalized:
        return value
    return _redact(value, normalized)
