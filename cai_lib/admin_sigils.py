"""Admin comment sigil detection for the auto-improve FSM.

Supports the ``<!-- cai-resplit -->`` sigil — an admin drops this
literal marker in a comment on a ``:plan-approved`` issue to signal
that the refined-and-planned scope is too large and should be
re-evaluated by ``cai-split``. The sigil is processed deterministically
at the start of each ``cai cycle`` tick (Phase 0.7) by firing the
``plan_approved_to_refined`` FSM transition; on the next tick the
dispatcher routes the issue to ``handle_split``.

Detection is a literal-string check — no Haiku / Claude invocation —
so the sweep is free to run on every cycle without cost impact.

Authority model: the ``<!-- cai-resplit -->`` token only counts when
it appears in the *most recent* admin-authored comment on the issue.
If a later admin comment exists without the sigil, the admin has
moved past the re-split intent and the scan ignores the issue. Admin
identity is the standard ``CAI_ADMIN_LOGINS`` set via
``cai_lib.config.is_admin_login``; a non-admin commenter echoing the
sigil string never triggers the rollback.
"""
from __future__ import annotations

import sys
from typing import Callable, Optional

from cai_lib.config import REPO, LABEL_PLAN_APPROVED, is_admin_login


# Literal-string sigil the admin drops in a comment on a
# :plan-approved issue to request a scope re-evaluation by cai-split.
RESPLIT_SIGIL = "<!-- cai-resplit -->"


def _latest_admin_comment(comments: list[dict]) -> Optional[dict]:
    """Return the most recent admin-authored comment dict, or ``None``.

    ``gh issue view --json comments`` returns comments in ascending
    ``createdAt`` order, so the last admin entry encountered during a
    linear scan is the most recent. When no admin has commented yet,
    returns ``None`` so the caller can short-circuit.
    """
    latest: Optional[dict] = None
    for c in comments:
        login = (c.get("author") or {}).get("login") or ""
        if is_admin_login(login):
            latest = c
    return latest


def scan_resplit_sigil(
    *,
    gh_json: Optional[Callable] = None,
) -> list[int]:
    """Return issue numbers whose latest admin comment carries
    :data:`RESPLIT_SIGIL` and whose current label is
    :data:`~cai_lib.config.LABEL_PLAN_APPROVED`.

    Args:
        gh_json: Injectable ``_gh_json`` for tests. When ``None`` the
            real ``cai_lib.github._gh_json`` is imported at call time
            to avoid a hard dependency cycle at import time.

    Returns:
        Sorted-by-GH-listing (usually recent-first) list of candidate
        issue numbers. An empty list is returned on any GH failure so
        the cycle never aborts on transient network errors.
    """
    if gh_json is None:
        from cai_lib.github import _gh_json as gh_json
    try:
        issues = gh_json([
            "issue", "list",
            "--repo", REPO,
            "--label", LABEL_PLAN_APPROVED,
            "--state", "open",
            "--json", "number,comments",
            "--limit", "100",
        ]) or []
    except Exception as exc:
        print(
            f"[cai cycle] scan_resplit_sigil: gh issue list failed: {exc}",
            file=sys.stderr,
        )
        return []

    out: list[int] = []
    for it in issues:
        number = it.get("number")
        if number is None:
            continue
        latest = _latest_admin_comment(it.get("comments") or [])
        if latest is None:
            continue
        if RESPLIT_SIGIL in (latest.get("body") or ""):
            out.append(number)
    return out


def process_resplit_sigil(
    issue_number: int,
    *,
    fire_trigger_fn: Optional[Callable] = None,
    post_comment_fn: Optional[Callable] = None,
) -> bool:
    """Fire ``plan_approved_to_refined`` on *issue_number* and post an ack.

    Returns the ``ok`` element of :func:`cai_lib.fsm.fire_trigger`'s
    ``(ok, diverted)`` tuple (``True`` on successful label move,
    ``False`` on FSM refusal / error). The ack comment is posted only
    after a successful transition; a failed comment post is logged
    by the poster itself and does not flip the return value.

    Args:
        issue_number: GitHub issue number to roll back.
        fire_trigger_fn: Injectable ``fire_trigger`` for tests; when
            ``None`` the real ``cai_lib.fsm.fire_trigger`` is used.
        post_comment_fn: Injectable comment poster for tests; when
            ``None`` the real ``cai_lib.github._post_issue_comment``
            is used.
    """
    if fire_trigger_fn is None:
        from cai_lib.fsm import fire_trigger as fire_trigger_fn
    if post_comment_fn is None:
        from cai_lib.github import _post_issue_comment as post_comment_fn

    ok, _ = fire_trigger_fn(
        issue_number, "plan_approved_to_refined",
        log_prefix="cai cycle",
    )
    if not ok:
        return False

    post_comment_fn(
        issue_number,
        (
            "Admin re-split sigil detected — rolling back to "
            "`:refined` so cai-split can re-evaluate scope."
        ),
        log_prefix="cai cycle",
    )
    return True
