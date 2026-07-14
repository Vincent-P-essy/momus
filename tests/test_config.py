from __future__ import annotations

from pathlib import Path

import pytest

from momus.config import MomusConfig
from momus.errors import ConfigError
from momus.models import Severity


def test_defaults(tmp_path: Path) -> None:
    cfg = MomusConfig.load(tmp_path)
    assert cfg.model == "claude-opus-4-8"
    assert cfg.effective_verifier_model == "claude-opus-4-8"
    assert cfg.verify is True
    assert cfg.min_severity is Severity.NIT
    assert "**/*.lock" in cfg.ignore


def test_toml_with_momus_table(tmp_path: Path) -> None:
    (tmp_path / ".momus.toml").write_text(
        '[momus]\nmodel = "claude-sonnet-5"\nlanguage = "fr"\nignore = ["docs/**"]\n'
    )
    cfg = MomusConfig.load(tmp_path)
    assert cfg.model == "claude-sonnet-5"
    assert cfg.language == "fr"
    assert cfg.ignore == ["docs/**"]


def test_toml_bare_keys(tmp_path: Path) -> None:
    (tmp_path / ".momus.toml").write_text("budget_usd = 0.5\nverify = false\n")
    cfg = MomusConfig.load(tmp_path)
    assert cfg.budget_usd == 0.5
    assert cfg.verify is False


def test_unknown_toml_key_is_rejected(tmp_path: Path) -> None:
    (tmp_path / ".momus.toml").write_text('[momus]\nmodle = "typo"\n')
    with pytest.raises(ConfigError, match="unknown option"):
        MomusConfig.load(tmp_path)


def test_env_overrides_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".momus.toml").write_text('[momus]\nmodel = "claude-sonnet-5"\n')
    monkeypatch.setenv("MOMUS_MODEL", "claude-opus-4-8")
    monkeypatch.setenv("MOMUS_VERIFY", "false")
    monkeypatch.setenv("MOMUS_IGNORE", "a/**, b/**")
    cfg = MomusConfig.load(tmp_path)
    assert cfg.model == "claude-opus-4-8"
    assert cfg.verify is False
    assert cfg.ignore == ["a/**", "b/**"]


def test_kwargs_override_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOMUS_MODEL", "claude-sonnet-5")
    cfg = MomusConfig.load(tmp_path, model="claude-fable-5")
    assert cfg.model == "claude-fable-5"


def test_none_kwargs_are_ignored(tmp_path: Path) -> None:
    cfg = MomusConfig.load(tmp_path, model=None, budget_usd=None)
    assert cfg.model == "claude-opus-4-8"


def test_invalid_effort(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="effort"):
        MomusConfig.load(tmp_path, effort="ultra")


def test_min_severity_coercion(tmp_path: Path) -> None:
    cfg = MomusConfig.load(tmp_path, min_severity="major")
    assert cfg.min_severity is Severity.MAJOR


def test_missing_explicit_config_path(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        MomusConfig.load(tmp_path, tmp_path / "nope.toml")


def test_broken_toml(tmp_path: Path) -> None:
    (tmp_path / ".momus.toml").write_text("model = [unclosed")
    with pytest.raises(ConfigError, match="could not read"):
        MomusConfig.load(tmp_path)
