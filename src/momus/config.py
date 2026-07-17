"""Configuration, layered lowest to highest precedence:

1. built-in defaults
2. `.momus.toml` in the reviewed repository (or an explicit --config path)
3. `MOMUS_*` environment variables
4. CLI flags
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

from pydantic import BaseModel, Field, ValidationError, field_validator

from momus.errors import ConfigError
from momus.models import Severity

_EFFORT_LEVELS = {"low", "medium", "high", "xhigh", "max"}
_ENV_PREFIX = "MOMUS_"
_LIST_FIELDS = {"ignore"}

_DEFAULT_IGNORE = [
    "**/*.lock",
    "**/package-lock.json",
    "**/pnpm-lock.yaml",
    "**/*.min.js",
    "**/*.min.css",
    "**/*.map",
    "**/*.snap",
    "**/dist/**",
    "**/vendor/**",
]


class MomusConfig(BaseModel):
    model: str = "claude-opus-4-8"
    verifier_model: str | None = None
    effort: str | None = "high"
    language: str = "en"
    verify: bool = True
    max_findings: int = 12
    min_severity: Severity = Severity.NIT
    ignore: list[str] = Field(default_factory=lambda: list(_DEFAULT_IGNORE))
    guidelines: str | None = None
    budget_usd: float = 2.0
    cost_footer: bool = True
    max_iterations: int = 30
    verifier_max_iterations: int = 12
    max_tokens: int = 16_000
    max_diff_chars: int = 240_000

    @field_validator("effort")
    @classmethod
    def _known_effort(cls, value: str | None) -> str | None:
        if value is not None and value not in _EFFORT_LEVELS:
            raise ValueError(f"effort must be one of {sorted(_EFFORT_LEVELS)}")
        return value

    @field_validator("max_findings", "max_iterations", "verifier_max_iterations", "max_tokens")
    @classmethod
    def _positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("must be >= 1")
        return value

    @property
    def effective_verifier_model(self) -> str:
        return self.verifier_model or self.model

    @classmethod
    def load(
        cls,
        repo_root: Path,
        config_path: Path | None = None,
        **overrides: Any,
    ) -> MomusConfig:
        data: dict[str, Any] = {}
        path = config_path or repo_root / ".momus.toml"
        if path.is_file():
            try:
                raw = tomllib.loads(path.read_text(encoding="utf-8"))
            except (tomllib.TOMLDecodeError, OSError) as exc:
                raise ConfigError(f"could not read {path}: {exc}") from exc
            section = raw.get("momus", raw)
            if not isinstance(section, dict):
                raise ConfigError(f"{path}: [momus] must be a table")
            unknown = set(section) - set(cls.model_fields)
            if unknown:
                raise ConfigError(f"{path}: unknown option(s): {', '.join(sorted(unknown))}")
            data.update(section)
        elif config_path is not None:
            raise ConfigError(f"config file not found: {config_path}")

        data.update(_env_overrides())
        data.update({key: value for key, value in overrides.items() if value is not None})
        try:
            return cls.model_validate(data)
        except ValidationError as exc:
            first = exc.errors()[0]
            location = ".".join(str(piece) for piece in first["loc"])
            raise ConfigError(f"invalid configuration: {location}: {first['msg']}") from exc


def _env_overrides() -> dict[str, Any]:
    values: dict[str, Any] = {}
    for name in MomusConfig.model_fields:
        raw = os.environ.get(_ENV_PREFIX + name.upper())
        if raw is None:
            continue
        if name in _LIST_FIELDS:
            values[name] = [item.strip() for item in raw.split(",") if item.strip()]
        else:
            values[name] = raw
    return values
