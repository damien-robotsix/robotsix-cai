"""GitHub Pull Request helpers for cai-solve and cai-address.

REST is used where it works straightforwardly (creating PRs, replying to
review comments). Review-thread state and resolution are GraphQL-only,
so those are issued via the GraphQL endpoint with the installation token.
"""
from __future__ import annotations

from dataclasses import dataclass

import requests

from .bot import CaiBot

_GRAPHQL_URL = "https://api.github.com/graphql"


@dataclass(frozen=True)
class ReviewComment:
    author: str
    body: str
    created_at: str


@dataclass(frozen=True)
class ReviewThread:
    """A single unresolved review thread on a pull request.

    ``id`` is the GraphQL node id (used for ``resolveReviewThread``).
    ``first_comment_id`` is the REST databaseId of the head comment
    (used for the ``/comments/{id}/replies`` REST endpoint).
    """

    id: str
    path: str
    line: int | None
    diff_hunk: str
    first_comment_id: int
    comments: list[ReviewComment]


def create_pull_request(
    bot: CaiBot,
    repo: str,
    *,
    title: str,
    body: str,
    head: str,
    base: str | None = None,
) -> str:
    """Open a pull request. Returns the HTML URL of the new PR."""
    repo_obj = bot.repo(repo)
    if base is None:
        base = repo_obj.default_branch
    pr = repo_obj.create_pull(title=title, body=body, head=head, base=base)
    return pr.html_url


def get_pr_meta(bot: CaiBot, repo: str, number: int) -> tuple[str, str, str]:
    """Return ``(title, body, head_branch)`` for pull request ``number``."""
    pr = bot.repo(repo).get_pull(number)
    return pr.title, pr.body or "", pr.head.ref


def _graphql(bot: CaiBot, repo: str, query: str, variables: dict) -> dict:
    token = bot.token_for(repo)
    resp = requests.post(
        _GRAPHQL_URL,
        json={"query": query, "variables": variables},
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()
    if "errors" in payload:
        raise RuntimeError(f"GraphQL error: {payload['errors']}")
    return payload["data"]


_LIST_THREADS_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100) {
        nodes {
          id
          isResolved
          isOutdated
          path
          line
          comments(first: 50) {
            nodes {
              databaseId
              author { login }
              body
              diffHunk
              createdAt
            }
          }
        }
      }
    }
  }
}
"""


def _parse_thread_node(node: dict) -> ReviewThread | None:
    comment_nodes = node["comments"]["nodes"]
    if not comment_nodes:
        return None
    head = comment_nodes[0]
    comments = [
        ReviewComment(
            author=(c.get("author") or {}).get("login") or "ghost",
            body=c["body"],
            created_at=c["createdAt"],
        )
        for c in comment_nodes
    ]
    return ReviewThread(
        id=node["id"],
        path=node["path"],
        line=node["line"],
        diff_hunk=head["diffHunk"] or "",
        first_comment_id=head["databaseId"],
        comments=comments,
    )


def list_unresolved_threads(
    bot: CaiBot,
    repo: str,
    number: int,
) -> list[ReviewThread]:
    """List unresolved review threads on PR ``number``.

    Threads whose head comment was authored by any GitHub App bot (login ends
    with ``[bot]``) are skipped — the agent should only address human review
    comments. Outdated threads are also skipped.
    """
    owner, name = repo.split("/", 1)
    data = _graphql(
        bot,
        repo,
        _LIST_THREADS_QUERY,
        {"owner": owner, "name": name, "number": number},
    )
    nodes = data["repository"]["pullRequest"]["reviewThreads"]["nodes"]
    threads: list[ReviewThread] = []
    for node in nodes:
        if node["isResolved"] or node["isOutdated"]:
            continue
        head = (node["comments"]["nodes"] or [{}])[0]
        head_author = (head.get("author") or {}).get("login") or ""
        if head_author.endswith("[bot]"):
            continue
        thread = _parse_thread_node(node)
        if thread is not None:
            threads.append(thread)
    return threads


def list_resolved_threads(bot: CaiBot, repo: str, number: int) -> list[ReviewThread]:
    """List resolved (non-outdated) review threads on PR ``number``.

    Used as "prior corrections" context for the address agent: it shows
    what reviewers previously asked for and how cai[bot] responded, so
    the agent doesn't undo a prior fix when handling a new thread.
    """
    owner, name = repo.split("/", 1)
    data = _graphql(
        bot,
        repo,
        _LIST_THREADS_QUERY,
        {"owner": owner, "name": name, "number": number},
    )
    nodes = data["repository"]["pullRequest"]["reviewThreads"]["nodes"]
    threads: list[ReviewThread] = []
    for node in nodes:
        if not node["isResolved"] or node["isOutdated"]:
            continue
        thread = _parse_thread_node(node)
        if thread is not None:
            threads.append(thread)
    return threads


def reply_to_review_comment(
    bot: CaiBot,
    repo: str,
    pr_number: int,
    comment_id: int,
    body: str,
) -> None:
    """Post ``body`` as a reply in the thread headed by ``comment_id``."""
    token = bot.token_for(repo)
    url = (
        f"https://api.github.com/repos/{repo}/pulls/"
        f"{pr_number}/comments/{comment_id}/replies"
    )
    resp = requests.post(
        url,
        json={"body": body},
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=30,
    )
    resp.raise_for_status()


_RESOLVE_THREAD_MUTATION = """
mutation($threadId: ID!) {
  resolveReviewThread(input: {threadId: $threadId}) {
    thread { id isResolved }
  }
}
"""


def resolve_review_thread(bot: CaiBot, repo: str, thread_id: str) -> None:
    """Mark thread ``thread_id`` as resolved."""
    _graphql(bot, repo, _RESOLVE_THREAD_MUTATION, {"threadId": thread_id})
