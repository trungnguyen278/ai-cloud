# SPDX-License-Identifier: MIT
"""Configuration: ``base.toml``, then an environment's file, then an optional local one
(docs/decisions/0002-stack.md, decision 6).

A configuration directory holds ``base.toml`` (shared by every environment),
``environments/<name>.toml`` (development, production) and an optional, git-ignored
``local.toml`` (settings for one machine). They are merged in that order, then checked
against the settings' models when a process starts: every value is required and of its
type, and an unknown key is a mistake. Secrets never come from these files.
"""

import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, ValidationError

CONFIG_DIR_ENV = "AI_CLOUD_CONFIG_DIR"


class ConfigError(Exception):
    """The configuration is missing, malformed or inconsistent."""


class Settings(BaseModel):
    """A table of the configuration, checked when it is read."""

    # strict: TOML has types, so a value of the wrong one is a mistake, not a string to
    # convert.
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    @classmethod
    def from_config(cls, data: Mapping[str, Any]) -> Self:
        """``data`` checked against these settings; a ``ConfigError`` names every
        problem, never the values, which may be secret one day."""
        try:
            return cls.model_validate(data)
        except ValidationError as error:
            problems = "; ".join(
                f"{'.'.join(str(part) for part in problem['loc'])}: {problem['msg']}"
                for problem in error.errors(include_url=False, include_input=False)
            )
            raise ConfigError(problems) from None


def default_config_dir() -> Path:
    """``$AI_CLOUD_CONFIG_DIR`` if set, otherwise the ``config`` folder of this checkout."""
    if value := os.environ.get(CONFIG_DIR_ENV):
        return Path(value)
    # src/ai_cloud/shared/config.py -> config
    return Path(__file__).resolve().parents[3] / "config"


def available_environments(config_dir: Path) -> list[str]:
    """Names of the environments in ``config_dir``."""
    return sorted(path.stem for path in (config_dir / "environments").glob("*.toml"))


def load_config(environment: str, config_dir: Path | None = None) -> dict[str, Any]:
    """Load ``base.toml``, and merge ``environments/<environment>.toml`` and
    ``local.toml`` on top."""
    config_dir = default_config_dir() if config_dir is None else config_dir
    base = config_dir / "base.toml"
    if not base.is_file():
        raise ConfigError(f"{base} not found (use --config-dir or {CONFIG_DIR_ENV})")
    available = available_environments(config_dir)
    if environment not in available:
        names = ", ".join(available) or "none"
        raise ConfigError(f"unknown environment {environment!r} (available: {names})")
    config = deep_merge(_read(base), _read(config_dir / "environments" / f"{environment}.toml"))
    local = config_dir / "local.toml"
    if local.is_file():
        config = deep_merge(config, _read(local))
    return config


def deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """Return ``base`` updated with ``override``.

    Tables present in both are merged key by key; any other value, lists included, is
    replaced. Neither argument is modified.
    """
    merged = dict(base)
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            merged[key] = deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def _read(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as file:
            return tomllib.load(file)
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
        raise ConfigError(f"{path}: {error}") from None
