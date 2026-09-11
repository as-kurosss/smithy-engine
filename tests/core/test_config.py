"""Tests for the TOML config loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from smithcore.core.config import Config, load_config
from smithcore.core.errors import ConfigError

VALID = """\
[robot]
name = "invoices"
queue = "invoices"
run_id = "local-run-1"

[retry]
max_attempts = 3

[assets]
servers = ["a", "b"]
"""


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_happy_path_typed_nested_access(tmp_path: Path) -> None:
    file = _write(tmp_path / "bot.toml", VALID)
    config = load_config(file, required=["robot.queue", "retry.max_attempts"])
    assert config.robot.queue == "invoices"
    assert config["retry"]["max_attempts"] == 3
    assert isinstance(config.retry.max_attempts, int)
    assert isinstance(config.assets.servers, tuple)


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="Cannot read config"):
        load_config(tmp_path / "nope.toml")


def test_invalid_toml_raises(tmp_path: Path) -> None:
    file = _write(tmp_path / "bot.toml", "[robot\nbroken =")
    with pytest.raises(ConfigError, match="Invalid TOML"):
        load_config(file)


def test_all_problems_listed_at_once(tmp_path: Path) -> None:
    file = _write(tmp_path / "bot.toml", '[robot]\nname = "x"\n')
    with pytest.raises(ConfigError) as exc_info:
        load_config(file, required=["robot.queue", "retry.max_attempts"])
    message = str(exc_info.value)
    assert "robot.queue" in message and "retry.max_attempts" in message


def test_must_exist_paths(tmp_path: Path) -> None:
    file = _write(tmp_path / "bot.toml", f'[paths]\nworkdir = "{tmp_path.as_posix()}"\n')
    config = load_config(file, must_exist=["paths.workdir"])
    assert Path(str(config.paths.workdir)) == tmp_path

    ghost = _write(tmp_path / "bad.toml", '[paths]\nworkdir = "./nope"\n')
    with pytest.raises(ConfigError, match="does not exist"):
        load_config(ghost, must_exist=["paths.workdir"])


def test_config_is_frozen(tmp_path: Path) -> None:
    config = load_config(_write(tmp_path / "bot.toml", VALID))
    with pytest.raises(AttributeError):
        config.robot = "other"  # type: ignore[misc]
    with pytest.raises(TypeError):
        config["robot"] = {}  # type: ignore[typeddict-item]
    with pytest.raises(AttributeError):
        _ = config.no_such_key


def test_to_dict_round_trip(tmp_path: Path) -> None:
    config = load_config(_write(tmp_path / "bot.toml", VALID))
    plain = config.to_dict()
    assert plain["robot"]["queue"] == "invoices"
    assert plain["assets"]["servers"] == ["a", "b"]
    assert isinstance(config, Config)


def test_env_overlay_overrides_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    file = _write(tmp_path / "bot.toml", VALID)
    monkeypatch.setenv("SMITHCORE_ROBOT__QUEUE", "cloud-queue")
    config = load_config(file)
    assert config.robot.queue == "cloud-queue"
    # Untouched keys keep file values.
    assert config.robot.name == "invoices"


def test_env_overlay_types_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    file = _write(tmp_path / "bot.toml", VALID)
    monkeypatch.setenv("SMITHCORE_RETRY__MAX_ATTEMPTS", "5")
    monkeypatch.setenv("SMITHCORE_DEBUG", "true")
    monkeypatch.setenv("SMITHCORE_ROBOT__NAME", "plain name with spaces")
    config = load_config(file)
    assert config.retry.max_attempts == 5
    assert isinstance(config.retry.max_attempts, int)
    assert config.debug is True
    assert config.robot.name == "plain name with spaces"


def test_env_overlay_satisfies_required(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    file = _write(tmp_path / "bot.toml", '[robot]\nname = "x"\n')
    monkeypatch.setenv("SMITHCORE_ROBOT__QUEUE", "env-queue")
    config = load_config(file, required=["robot.queue"])
    assert config.robot.queue == "env-queue"


def test_env_overlay_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    file = _write(tmp_path / "bot.toml", VALID)
    monkeypatch.setenv("SMITHCORE_ROBOT__QUEUE", "cloud-queue")
    config = load_config(file, env_prefix=None)
    assert config.robot.queue == "invoices"


def test_env_overlay_single_underscore_stays_literal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file = _write(tmp_path / "bot.toml", VALID)
    monkeypatch.setenv("SMITHCORE_ROBOT_NAME", "top-level")
    config = load_config(file)
    assert config["robot_name"] == "top-level"
    assert config.robot.name == "invoices"
