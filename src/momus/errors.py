"""Exception hierarchy. Everything user-facing derives from MomusError."""

from __future__ import annotations


class MomusError(Exception):
    """Base class for all Momus failures that should surface as a clean CLI error."""


class ConfigError(MomusError):
    """Invalid or unreadable configuration."""


class DiffError(MomusError):
    """The diff could not be parsed or obtained."""


class GitHubError(MomusError):
    """A GitHub API call failed after retries."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class ModelRefusalError(MomusError):
    """The model declined the request (stop_reason == "refusal")."""


class BudgetExceededError(MomusError):
    """The configured USD budget would be exceeded by another model call."""


class AgentError(MomusError):
    """The agent loop ended without producing a usable result."""
