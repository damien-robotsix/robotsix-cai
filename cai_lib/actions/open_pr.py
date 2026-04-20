"""Handler for brand-new PRs (PRState.OPEN).

On a bot branch (``auto-improve/<N>-...``) applies
``open_to_reviewing_code`` to tag the PR with ``pr:reviewing-code``
so the dispatcher can route it to ``handle_review_pr`` on the next
tick.

On any other branch (a human-authored PR that has no chance of
auto-merging — see issue #1065) applies ``open_to_human`` to tag
the PR with ``pr:human-needed`` immediately — before any review /
rebase / docs cycle is spent. The previous behaviour tagged every
fresh PR ``pr:reviewing-code`` and only parked at merge time via
``not_bot_branch`` in ``handle_merge``, which wasted agent time
and polluted the audit log with downstream pipeline transitions
for a PR that was never going to auto-merge.
"""
from __future__ import annotations

from cai_lib.actions.merge import _BOT_BRANCH_RE
from cai_lib.config import REPO
from cai_lib.fsm import apply_pr_transition
from cai_lib.logging_utils import log_run
from cai_lib.subprocess_utils import _run


def handle_open_to_review(pr: dict) -> int:
    """Route a brand-new PR based on its head branch.

    The PR dict is passed by the dispatcher; it has no pipeline
    label yet (``get_pr_state`` returned ``PRState.OPEN``).

    - If the head branch matches ``auto-improve/<issue>-...`` the
      PR is tagged ``pr:reviewing-code`` via the existing
      ``open_to_reviewing_code`` transition.
    - Otherwise the PR is tagged ``pr:human-needed`` via
      ``open_to_human`` and a short comment is posted explaining
      why, so the dispatcher stops routing the PR every tick and
      the audit log sees a single
      ``result=not_bot_branch_open`` entry instead of a full
      pipeline run that would eventually park at merge time.
    """
    pr_number = pr["number"]
    branch = pr.get("headRefName", "") or ""

    if not _BOT_BRANCH_RE.match(branch):
        print(
            f"[cai dispatch] PR #{pr_number}: non-bot branch "
            f"{branch!r}; parking as PR_HUMAN_NEEDED at PR-open "
            f"time",
            flush=True,
        )
        _run(
            ["gh", "pr", "comment", str(pr_number),
             "--repo", REPO, "--body",
             f"This PR is on branch `{branch}`, which is not an "
             f"`auto-improve/<issue>-…` bot branch, so the cai "
             f"auto-improve pipeline will not review or auto-"
             f"merge it. Moving to `pr:human-needed` at PR-open "
             f"time — a human admin must review and merge this "
             f"PR manually. Re-applying pipeline labels will "
             f"just re-enter this state."],
            capture_output=True,
        )
        ok = apply_pr_transition(
            pr_number, "open_to_human",
            current_pr=pr,
            log_prefix="cai dispatch",
            divert_reason=(
                f"Non-bot-branch PR (branch={branch!r}) cannot be "
                f"auto-merged; requires manual review."
            ),
        )
        log_run("dispatch", repo=REPO, pr=pr_number,
                result="not_bot_branch_open", exit=0)
        return 0 if ok else 1

    ok = apply_pr_transition(
        pr_number, "open_to_reviewing_code",
        current_pr=pr,
        log_prefix="cai dispatch",
    )
    return 0 if ok else 1
