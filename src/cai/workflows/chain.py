"""``cai-chain-sub-issue`` CLI: when a sub-issue is closed, apply ``cai:raised``
to the next open sibling so the solve workflow picks it up.
"""

from __future__ import annotations

import sys

from cai.github.bot import CaiBot
from cai.github.issues import get_parent_issue, list_sub_issues
from cai.github.labels import set_label
from cai.github.repo import parse_ref_and_bot


def orchestrate(bot: CaiBot, repo: str, closed_number: int) -> str | None:
    """Find the next open sibling sub-issue and apply ``cai:raised``.

    Returns a human-readable summary string on success, or ``None``
    when the closed issue is not a sub-issue or no open sibling follows
    it.
    """
    parent_number = get_parent_issue(bot, repo, closed_number)
    if parent_number is None:
        return None

    siblings = list_sub_issues(bot, repo, parent_number)
    open_siblings = [s for s in siblings if s["state"] == "open"]
    open_siblings.sort(key=lambda s: s["number"])

    for sibling in open_siblings:
        if sibling["number"] > closed_number:
            set_label(bot, repo, sibling["number"], "cai:raised", present=True)
            return f"applied cai:raised to {repo}#{sibling['number']}"

    return None


def main() -> None:
    bot, repo, number = parse_ref_and_bot(
        "cai-chain-sub-issue",
        "When a sub-issue is closed on GitHub, locate its parent, "
        "find the next open sibling, and apply cai:raised so the "
        "cai-solve workflow picks it up automatically.",
    )
    result = orchestrate(bot, repo, number)
    if result is not None:
        print(result)
    else:
        print("no next sub-issue")
    sys.exit(0)


if __name__ == "__main__":
    main()
