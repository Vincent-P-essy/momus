from __future__ import annotations

import json

import httpx
import pytest
import respx

from momus.errors import GitHubError
from momus.github import GitHubClient

BASE = "https://api.github.com"
PR_JSON = {
    "number": 7,
    "title": "Add feature",
    "body": "Because reasons.",
    "draft": False,
    "user": {"login": "octocat"},
    "head": {"sha": "abc123", "ref": "feat/x"},
    "base": {"ref": "main"},
}


def _client() -> tuple[GitHubClient, list[float]]:
    sleeps: list[float] = []
    return GitHubClient("tok", "octo/demo", sleep=sleeps.append), sleeps


def test_repo_slug_is_validated() -> None:
    with pytest.raises(GitHubError, match="owner/name"):
        GitHubClient("tok", "not-a-slug")


@respx.mock
def test_get_pr_maps_fields() -> None:
    route = respx.get(f"{BASE}/repos/octo/demo/pulls/7").mock(
        return_value=httpx.Response(200, json=PR_JSON)
    )
    client, _ = _client()
    pr = client.get_pr(7)
    assert (pr.number, pr.title, pr.head_sha) == (7, "Add feature", "abc123")
    assert (pr.base_ref, pr.head_ref, pr.author, pr.draft) == ("main", "feat/x", "octocat", False)
    request = route.calls[0].request
    assert request.headers["authorization"] == "Bearer tok"
    assert request.headers["x-github-api-version"] == "2022-11-28"


@respx.mock
def test_get_pr_diff_uses_diff_media_type() -> None:
    route = respx.get(f"{BASE}/repos/octo/demo/pulls/7").mock(
        return_value=httpx.Response(200, text="diff --git a/f b/f\n")
    )
    client, _ = _client()
    assert client.get_pr_diff(7).startswith("diff --git")
    assert route.calls[0].request.headers["accept"] == "application/vnd.github.v3.diff"


@respx.mock
def test_pagination_follows_full_pages() -> None:
    full_page = [{"id": i} for i in range(100)]
    respx.get(f"{BASE}/repos/octo/demo/pulls/7/comments").mock(
        side_effect=[
            httpx.Response(200, json=full_page),
            httpx.Response(200, json=[{"id": "last"}]),
        ]
    )
    client, _ = _client()
    comments = client.list_review_comments(7)
    assert len(comments) == 101


@respx.mock
def test_server_errors_are_retried() -> None:
    respx.get(f"{BASE}/repos/octo/demo/pulls/7").mock(
        side_effect=[
            httpx.Response(502, json={"message": "bad gateway"}),
            httpx.Response(200, json=PR_JSON),
        ]
    )
    client, sleeps = _client()
    assert client.get_pr(7).number == 7
    assert len(sleeps) == 1


@respx.mock
def test_rate_limit_honours_retry_after() -> None:
    respx.get(f"{BASE}/repos/octo/demo/pulls/7").mock(
        side_effect=[
            httpx.Response(403, headers={"retry-after": "7"}, json={"message": "slow down"}),
            httpx.Response(200, json=PR_JSON),
        ]
    )
    client, sleeps = _client()
    client.get_pr(7)
    assert sleeps == [7.0]


@respx.mock
def test_client_errors_do_not_retry() -> None:
    respx.get(f"{BASE}/repos/octo/demo/pulls/404").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"})
    )
    client, sleeps = _client()
    with pytest.raises(GitHubError, match="Not Found") as excinfo:
        client.get_pr(404)
    assert excinfo.value.status_code == 404
    assert sleeps == []


@respx.mock
def test_create_review_payload() -> None:
    route = respx.post(f"{BASE}/repos/octo/demo/pulls/7/reviews").mock(
        return_value=httpx.Response(200, json={"id": 1})
    )
    client, _ = _client()
    comments = [{"path": "f.py", "line": 3, "side": "RIGHT", "body": "hm"}]
    client.create_review(7, "abc123", "summary", comments)
    payload = json.loads(route.calls[0].request.content)
    assert payload["commit_id"] == "abc123"
    assert payload["event"] == "COMMENT"
    assert payload["comments"] == comments
