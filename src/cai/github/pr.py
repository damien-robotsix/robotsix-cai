"""GitHub Pull Request helpers for cai-solve.

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
) -> tuple[str, int]:
    """Open a pull request. Returns ``(html_url, number)`` of the new PR."""
    repo_obj = bot.repo(repo)
    if base is None:
        base = repo_obj.default_branch
    pr = repo_obj.create_pull(title=title, body=body, head=head, base=base)
    return pr.html_url, pr.number


def get_pr_meta(bot: CaiBot, repo: str, number: int) -> tuple[str, str, str, str]:
    """Return ``(title, body, head_branch, base_branch)`` for pull request ``number``."""
    pr = bot.repo(repo).get_pull(number)
    return pr.title, pr.body or "", pr.head.ref, pr.base.ref


def _graphql(bot: CaiBot, query: str, variables: dict) -> dict:
    repo = f"{variables['owner']}/{variables['name']}"
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


def _get_review_threads_nodes(bot: CaiBot, repo: str, number: int) -> list[dict]:
    owner, name = repo.split("/", 1)
    data = _graphql(bot, _LIST_THREADS_QUERY, {"owner": owner, "name": name, "number": number})
    return data["repository"]["pullRequest"]["reviewThreads"]["nodes"]


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
    nodes = _get_review_threads_nodes(bot, repo, number)
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

    Used as "prior corrections" context for the implement agent in
    PR-comment mode: it shows what reviewers previously asked for and
    how cai[bot] responded, so the agent doesn't undo a prior fix when
    handling a new thread.
    """
    nodes = _get_review_threads_nodes(bot, repo, number)
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
    owner, name = repo.split("/", 1)
    _graphql(bot, _RESOLVE_THREAD_MUTATION, {"threadId": thread_id, "owner": owner, "name": name})


def get_pr_diff(bot: CaiBot, repo: str, number: int) -> str:
    """Return the unified diff of PR ``number`` against its base branch."""
    token = bot.token_for(repo)
    url = f"https://api.github.com/repos/{repo}/pulls/{number}"
    resp = requests.get(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.v3.diff",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.text


_PR_NODE_ID_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      id
      reviewRequests(first: 1) { totalCount }
    }
  }
}
"""


def get_pr_node_id_and_review_requests(
    bot: CaiBot, repo: str, number: int
) -> tuple[str, int]:
    """Return ``(node_id, review_request_count)`` for PR ``number``.

    The node id is needed for the ``enablePullRequestAutoMerge`` mutation;
    the review-request count lets the caller skip auto-merge when a human
    has been pinged for review.
    """
    owner, name = repo.split("/", 1)
    data = _graphql(
        bot, _PR_NODE_ID_QUERY, {"owner": owner, "name": name, "number": number}
    )
    pr = data["repository"]["pullRequest"]
    return pr["id"], pr["reviewRequests"]["totalCount"]


_ENABLE_AUTO_MERGE_MUTATION = """
mutation($pullRequestId: ID!, $mergeMethod: PullRequestMergeMethod!) {
  enablePullRequestAutoMerge(input: {pullRequestId: $pullRequestId, mergeMethod: $mergeMethod}) {
    pullRequest { id autoMergeRequest { enabledAt mergeMethod } }
  }
}
"""


def enable_auto_merge(
    bot: CaiBot,
    repo: str,
    number: int,
    *,
    merge_method: str = "MERGE",
) -> None:
    """Enable GitHub auto-merge on PR ``number`` with the given merge method.

    ``merge_method`` is one of ``MERGE``, ``SQUASH``, ``REBASE``. The mutation
    fails when the repository does not allow auto-merge or the requested
    method, or when required checks are already passing — the caller can let
    that bubble up since a failure is informational, not fatal.
    """
    pr_node_id, _ = get_pr_node_id_and_review_requests(bot, repo, number)
    owner, name = repo.split("/", 1)
    _graphql(
        bot,
        _ENABLE_AUTO_MERGE_MUTATION,
        {
            "pullRequestId": pr_node_id,
            "mergeMethod": merge_method,
            "owner": owner,
            "name": name,
        },
    )
