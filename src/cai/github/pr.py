"""GitHub Pull Request creation for cai-solve."""
from __future__ import annotations

from .bot import CaiBot


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
