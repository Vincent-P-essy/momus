"""Hand-rolled GitHub REST client.

The API surface a review needs is four endpoints; owning the client keeps the
dependency tree small and makes retry semantics (5xx, rate limits with
Retry-After) explicit and testable.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from momus.errors import GitHubError

_JSON_ACCEPT = "application/vnd.github+json"
_DIFF_ACCEPT = "application/vnd.github.v3.diff"
_MAX_ATTEMPTS = 3
_MAX_RETRY_DELAY = 60.0


@dataclass(slots=True)
class PRInfo:
    number: int
    title: str
    body: str
    head_sha: str
    base_ref: str
    head_ref: str
    author: str
    draft: bool


class GitHubClient:
    def __init__(
        self,
        token: str,
        repo: str,
        base_url: str = "https://api.github.com",
        http: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if repo.count("/") != 1:
            raise GitHubError(f"repository must be owner/name, got {repo!r}")
        self.repo = repo
        self._sleep = sleep
        self._http = http or httpx.Client(
            base_url=base_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": _JSON_ACCEPT,
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "momus-review",
            },
            timeout=30.0,
            follow_redirects=True,
        )

    # -- endpoints --------------------------------------------------------------

    def get_pr(self, number: int) -> PRInfo:
        data = self._request("GET", f"/repos/{self.repo}/pulls/{number}").json()
        return PRInfo(
            number=int(data["number"]),
            title=str(data.get("title") or ""),
            body=str(data.get("body") or ""),
            head_sha=str(data["head"]["sha"]),
            base_ref=str(data["base"]["ref"]),
            head_ref=str(data["head"]["ref"]),
            author=str((data.get("user") or {}).get("login") or ""),
            draft=bool(data.get("draft")),
        )

    def get_pr_diff(self, number: int) -> str:
        response = self._request("GET", f"/repos/{self.repo}/pulls/{number}", accept=_DIFF_ACCEPT)
        return response.text

    def list_review_comments(self, number: int) -> list[dict[str, Any]]:
        return self._paginate(f"/repos/{self.repo}/pulls/{number}/comments")

    def list_reviews(self, number: int) -> list[dict[str, Any]]:
        return self._paginate(f"/repos/{self.repo}/pulls/{number}/reviews")

    def create_review(
        self,
        number: int,
        commit_id: str,
        body: str,
        comments: list[dict[str, Any]],
        event: str = "COMMENT",
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"body": body, "event": event, "comments": comments}
        if commit_id:
            payload["commit_id"] = commit_id
        response = self._request(
            "POST", f"/repos/{self.repo}/pulls/{number}/reviews", payload=payload
        )
        result = response.json()
        return dict(result) if isinstance(result, dict) else {}

    # -- plumbing ---------------------------------------------------------------

    def _paginate(self, path: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page = 1
        while True:
            batch = self._request("GET", path, params={"per_page": 100, "page": page}).json()
            if not isinstance(batch, list):
                raise GitHubError(f"unexpected response shape from {path}")
            items.extend(batch)
            if len(batch) < 100:
                return items
            page += 1

    def _request(
        self,
        method: str,
        path: str,
        accept: str = _JSON_ACCEPT,
        payload: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        last_error = ""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                response = self._http.request(
                    method, path, headers={"Accept": accept}, json=payload, params=params
                )
            except httpx.HTTPError as exc:
                last_error = str(exc)
                if attempt < _MAX_ATTEMPTS:
                    self._sleep(float(attempt))
                    continue
                raise GitHubError(f"GitHub request failed: {exc}") from exc

            if response.status_code < 400:
                return response
            if attempt < _MAX_ATTEMPTS and _retryable(response):
                self._sleep(_retry_delay(response, attempt))
                continue
            raise GitHubError(
                f"GitHub API {method} {path} failed "
                f"({response.status_code}): {_error_message(response)}",
                status_code=response.status_code,
            )
        raise GitHubError(f"GitHub request failed after retries: {last_error}")


def _retryable(response: httpx.Response) -> bool:
    if response.status_code >= 500 or response.status_code == 429:
        return True
    # Secondary rate limits surface as 403 with Retry-After or an empty quota.
    return response.status_code == 403 and (
        "retry-after" in response.headers or response.headers.get("x-ratelimit-remaining") == "0"
    )


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    header = response.headers.get("retry-after")
    if header is not None:
        try:
            return min(float(header), _MAX_RETRY_DELAY)
        except ValueError:
            pass
    return float(attempt)


def _error_message(response: httpx.Response) -> str:
    try:
        message = response.json().get("message")
        if message:
            return str(message)
    except ValueError:
        pass
    return response.text[:200] or "no details"
