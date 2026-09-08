"""TOML robot config — load once in Init, fail fast, frozen afterwards.

The file replaces the legacy two-column Excel sheet: same idea (one config
per robot, values differ per environment), but diffable in git, typed, and
validated up front. Secrets never live here — only *references* to
orchestrator assets (names, GUIDs); values are fetched at runtime.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

from smithy.core.errors import ConfigError

_MISSING = object()

#: Framework-owned ``SMITHY_*`` settings that are consumed directly by
#: the engine (blocking.py, process.py) and must not leak into the robot
#: config document as fake keys.
_RESERVED_ENV_KEYS: frozenset[str] = frozenset(
    {"blocking_timeout", "allowed_commands", "output_root"}
)


class Config:
    """Immutable attribute-style view over a loaded TOML document.

    Nested tables become nested :class:`Config`, lists become tuples —
    nothing inside can be reassigned after loading. Access by attribute
    (``config.paths.workdir``) or by item (``config[\"paths\"][\"workdir\"]``).
    """

    __slots__ = ("_data",)

    def __init__(self, data: dict[str, Any]) -> None:
        object.__setattr__(self, "_data", data)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError(f"Config is frozen; cannot set {name!r}")

    def __getattr__(self, name: str) -> Any:
        if name == "_data":
            # Avoid infinite recursion when __getattr__ runs before
            # __init__ (copy.copy, unpickling).
            raise AttributeError(name)
        try:
            return self._data[name]
        except KeyError:
            raise AttributeError(f"Unknown config key: {name!r}") from None

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __contains__(self, key: object) -> bool:
        return key in self._data

    def __repr__(self) -> str:
        return f"Config({self._data!r})"

    def to_dict(self) -> dict[str, Any]:
        """Plain deep copy of the document (logging, debugging)."""
        return {key: _thaw(value) for key, value in self._data.items()}


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return Config({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Config):
        return value.to_dict()
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _lookup(config: Config, dotted: str) -> Any:
    current: Any = config
    for part in dotted.split("."):
        if isinstance(current, Config) and part in current:
            current = current[part]
        else:
            return _MISSING
    return current


def _parse_env_value(raw: str) -> Any:
    """Interpret an env value with TOML scalar syntax; fall back to string.

    ``"8080"`` becomes ``8080``, ``"true"`` becomes ``True``,
    ``"C:\\temp"`` (not valid TOML) stays a plain string.
    """
    try:
        return tomllib.loads(f"value = {raw}")["value"]
    except ValueError:
        return raw


def _deep_set(document: dict[str, Any], parts: list[str], value: Any) -> None:
    """Set a nested key, creating intermediate tables as needed."""
    current = document
    for part in parts[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            child = {}
            current[part] = child
        current = child
    current[parts[-1]] = value


def _apply_env_overlay(document: dict[str, Any], prefix: str) -> None:
    """Overlay ``<prefix>*`` env vars onto the TOML document (in place).

    ``SMITHY_ROBOT__QUEUE`` sets ``robot.queue``; double underscore nests,
    single underscores stay literal (``SMITHY_ROBOT_NAME`` → ``robot_name``).
    Values are typed with TOML scalar syntax (ints, bools, quoted strings).
    Framework-owned settings (``SMITHY_BLOCKING_TIMEOUT``,
    ``SMITHY_ALLOWED_COMMANDS``) are skipped — they configure the engine,
    not the robot document.
    """
    for name, raw in os.environ.items():
        if not name.startswith(prefix):
            continue
        rest = name[len(prefix) :].lower()
        if not rest or rest in _RESERVED_ENV_KEYS:
            continue
        _deep_set(document, rest.split("__"), _parse_env_value(raw))


def load_config(
    path: str | Path,
    *,
    required: tuple[str, ...] | list[str] = (),
    must_exist: tuple[str, ...] | list[str] = (),
    env_prefix: str | None = "SMITHY_",
) -> Config:
    """Load *path* as TOML and validate it.

    Raises one :class:`ConfigError` listing *every* problem at once —
    the robot fails in Init, never mid-run. *required* are dotted keys
    that must be present (``\"robot.queue\"``); *must_exist* are dotted
    keys whose values must be existing filesystem paths.

    When *env_prefix* is set (default ``\"SMITHY_\"``), matching env vars
    override file values — per-environment tweaks without editing TOML.
    ``SMITHY_ROBOT__QUEUE`` sets ``robot.queue`` (``__`` nests, values are
    TOML-typed). Checks run *after* the overlay, so env can satisfy
    *required*. Pass ``env_prefix=None`` to disable.
    """
    file = Path(path)
    try:
        raw = file.read_bytes()
    except OSError as exc:
        raise ConfigError(f"Cannot read config file: {file}", input_value=str(file)) from exc
    try:
        document = tomllib.loads(raw.decode("utf-8"))
    except ValueError as exc:
        raise ConfigError(f"Invalid TOML in {file}: {exc}", input_value=str(file)) from exc
    if env_prefix:
        _apply_env_overlay(document, env_prefix)
    frozen = _freeze(document)
    if not isinstance(frozen, Config):
        raise ConfigError("Config root must be a table", input_value=str(file))
    problems: list[str] = []
    for key in required:
        if _lookup(frozen, key) is _MISSING:
            problems.append(f"missing required key: {key!r}")
    for key in must_exist:
        value = _lookup(frozen, key)
        if value is _MISSING:
            problems.append(f"missing path key: {key!r}")
        elif not isinstance(value, (str, Path)) or not Path(value).exists():
            problems.append(f"path does not exist: {key!r} = {value!r}")
    if problems:
        raise ConfigError(
            f"Invalid config {file}:\n" + "\n".join(f"  - {item}" for item in problems),
            input_value=str(file),
        )
    return frozen
