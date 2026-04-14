"""Handler for brand-new PRs (PRState.OPEN).

Applies ``open_to_reviewing_code`` to tag the PR with ``pr:reviewing-code``
so the dispatcher can route it to ``handle_review_pr`` on the next tick.
"""
from cai_lib.fsm import apply_pr_transition


def handle_open_to_review(pr: dict) -> int:
    """Move a brand-new PR into the REVIEWING_CODE state.

    The PR dict is passed by the dispatcher; it has no pipeline label yet
    (``get_pr_state`` returned ``PRState.OPEN``). We tag it and return.
    """
    pr_number = pr["number"]
    ok = apply_pr_transition(
        pr_number, "open_to_reviewing_code",
        current_pr=pr,
        log_prefix="cai dispatch",
    )
    return 0 if ok else 1
