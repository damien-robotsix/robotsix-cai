"""APPLYING / APPLIED state handlers for the maintenance pipeline.

``handle_maintain``  — runs cai-maintain against an ``:applying`` issue and
                       advances it to ``:applied`` (HIGH confidence) or
                       ``:human-needed`` (MEDIUM / LOW / missing). When the
                       agent emits an ``Ops-source: inferred`` marker
                       (issue #986 — Ops synthesised from a plan block
                       because no explicit ``Ops:`` header was present),
                       the handler picks the relaxed sibling transition
                       ``applying_to_applied_inferred_ops`` whose gate
                       accepts MEDIUM confidence.

``handle_applied``   — advances an ``:applied`` issue deterministically to
                       ``:solved`` and closes it on GitHub.  No agent needed;
                       the ops have already been verified by cai-maintain.
"""
from __future__ import annotations

import re
import shutil
import time
import uuid
from pathlib import Path

from cai_lib.config import REPO
from cai_lib.cmd_helpers import _work_directory_block
from cai_lib.fsm import (
    fire_trigger,
    parse_confidence,
    parse_confidence_reason,
)
from cai_lib.github import _build_issue_block, close_issue_completed
from cai_lib.logging_utils import log_run
from cai_lib.subagent import _run_claude_p
from cai_lib.subprocess_utils import _run


# Matches the marker emitted by cai-maintain when the op list was
# synthesised from a stored plan block because no explicit ``Ops:``
# header was present on the issue body (issue #986). When present,
# :func:`handle_maintain` selects the relaxed
# ``applying_to_applied_inferred_ops`` transition (MEDIUM gate).
_OPS_SOURCE_INFERRED_RE = re.compile(
    r"^\s*Ops-source:\s*inferred\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def _ops_source_is_inferred(text: str) -> bool:
    """Return True iff *text* contains a well-formed ``Ops-source: inferred`` line."""
    if not text:
        return False
    return _OPS_SOURCE_INFERRED_RE.search(text) is not None


def handle_maintain(issue: dict) -> int:
    """Dispatcher handler for ``IssueState.APPLYING``.

    Clones the repo, runs the cai-maintain agent against the issue's Ops
    block, and applies the FSM transition:
    - ``applying_to_applied``   on HIGH confidence.
    - ``applying_to_human``     on MEDIUM / LOW / missing confidence (via
      :func:`fire_trigger` divert path).
    """
    t0 = time.monotonic()
    issue_number = issue["number"]
    title = issue["title"]
    issue_labels = [lbl["name"] for lbl in issue.get("labels", [])]

    print(f"[cai maintain] picked #{issue_number}: {title}", flush=True)

    # Clone the repo so the agent can read / edit workflow files if needed.
    _uid = uuid.uuid4().hex[:8]
    work_dir = Path(f"/tmp/cai-maintain-{issue_number}-{_uid}")
    if work_dir.exists():
        shutil.rmtree(work_dir)

    _run(["gh", "auth", "setup-git"], capture_output=True)
    clone = _run(
        ["git", "clone", "--depth", "1",
         f"https://github.com/{REPO}.git", str(work_dir)],
        capture_output=True,
    )
    if clone.returncode != 0:
        import sys
        print(f"[cai maintain] git clone failed:\n{clone.stderr}",
              file=sys.stderr)
        log_run("maintain", repo=REPO, issue=issue_number,
                result="clone_failed", exit=1)
        return 1

    # Run the cai-maintain agent.
    user_message = _work_directory_block(work_dir) + "\n" + _build_issue_block(issue)
    print(f"[cai maintain] running cai-maintain agent for #{issue_number}",
          flush=True)
    result = _run_claude_p(
        ["claude", "-p", "--agent", "cai-maintain",
         "--dangerously-skip-permissions",
         "--add-dir", str(work_dir)],
        category="maintain",
        agent="cai-maintain",
        input=user_message,
        cwd="/app",
        timeout=1800,
        target_kind="issue",
        target_number=issue_number,
    )
    if result.stdout:
        print(result.stdout, flush=True)

    shutil.rmtree(work_dir, ignore_errors=True)

    if result.returncode != 0:
        import sys
        print(
            f"[cai maintain] agent failed (exit {result.returncode}):\n"
            f"{result.stderr}",
            file=sys.stderr, flush=True,
        )
        dur = f"{int(time.monotonic() - t0)}s"
        log_run("maintain", repo=REPO, issue=issue_number,
                result="agent_failed", duration=dur, exit=result.returncode)
        return result.returncode

    # Parse confidence and apply FSM transition.
    confidence = parse_confidence(result.stdout)
    confidence_reason = parse_confidence_reason(result.stdout)

    # Pick the relaxed sibling transition when the agent signalled that
    # it synthesised the Ops list from a plan block (issue #986). The
    # sibling's only difference from the default is its MEDIUM confidence
    # gate — labels move identically. Plans with an explicit ``Ops:``
    # header stay on the default HIGH-threshold transition.
    ops_inferred = _ops_source_is_inferred(result.stdout)
    transition_name = (
        "applying_to_applied_inferred_ops" if ops_inferred
        else "applying_to_applied"
    )
    if ops_inferred:
        print(
            f"[cai maintain] #{issue_number}: Ops-source: inferred — "
            f"using relaxed transition {transition_name!r} (MEDIUM gate)",
            flush=True,
        )

    ok, diverted = fire_trigger(
        issue_number,
        transition_name,
        confidence=confidence,
        _confidence_gated=True,
        current_labels=issue_labels,
        log_prefix="cai maintain",
        reason_extra=confidence_reason or "",
    )

    dur = f"{int(time.monotonic() - t0)}s"
    outcome = "diverted_to_human" if diverted else ("applied" if ok else "failed")
    log_run("maintain", repo=REPO, issue=issue_number,
            confidence=str(confidence), result=outcome, duration=dur,
            ops_source=("inferred" if ops_inferred else "explicit"),
            exit=0 if ok else 1)
    return 0 if ok else 1


def handle_applied(issue: dict) -> int:
    """Dispatcher handler for ``IssueState.APPLIED``.

    Deterministic bookkeeping step: advances ``:applied`` → ``:solved`` and
    closes the GitHub issue as completed.  No agent is needed — the ops
    have already been executed and verified by :func:`handle_maintain`.
    """
    issue_number = issue["number"]
    issue_labels = [lbl["name"] for lbl in issue.get("labels", [])]

    print(f"[cai maintain] advancing #{issue_number} :applied → :solved",
          flush=True)
    fire_trigger(
        issue_number, "applied_to_solved",
        current_labels=issue_labels,
        log_prefix="cai maintain",
    )
    close_issue_completed(
        issue_number,
        "Maintenance ops applied and verified "
        "(auto-improve:applied → :solved). Closing as completed.",
        log_prefix="cai maintain",
    )
    log_run("maintain", repo=REPO, issue=issue_number,
            result="applied_to_solved", exit=0)
    return 0
