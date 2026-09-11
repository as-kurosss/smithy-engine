"""Config loader demo — load, use, try to break. No UI needed, just run it.

Requires: pip install smithcore (pure stdlib otherwise)

    python examples/config_demo.py
"""

from __future__ import annotations

from pathlib import Path

from smithcore import ConfigError, load_config

HERE = Path(__file__).resolve().parent


def main() -> None:
    # 1. Load once in Init. Missing keys would fail HERE, never mid-run.
    config = load_config(
        HERE / "config_demo.toml",
        required=["robot.queue", "retry.max_attempts"],
    )

    # 2. Old habit, new spelling: attribute access instead of string keys.
    #    Was:  $Global_Config["work_path"]  (typo = KeyError on night run)
    #    Now:  config.paths.workdir        (typo = error in your editor)
    print(f"robot : {config.robot.name} ({config.robot.queue})")
    print(f"work  : {config.paths.workdir}")
    print(f"retry : {config.retry.max_attempts}")
    print(f"items : {config['assets']['servers']}")  # item access works too

    # 3. Frozen: reassignment is refused, config stays config.
    try:
        config.robot = "other"  # type: ignore[misc]
    except AttributeError as exc:
        print(f"frozen: {exc}")

    # 4. Broken file: ONE error listing EVERY problem at once.
    try:
        load_config(
            HERE / "config_demo_broken.toml",
            required=["robot.queue", "retry.max_attempts"],
        )
    except ConfigError as exc:
        print(f"broken:\n{exc}")


if __name__ == "__main__":
    main()
