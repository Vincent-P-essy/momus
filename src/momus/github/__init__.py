"""Minimal GitHub REST client — only the endpoints a review needs."""

from momus.github.client import GitHubClient, PRInfo

__all__ = ["GitHubClient", "PRInfo"]
