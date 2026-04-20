"""Phase E entry point — subcommand dispatcher.

Subcommands:

    python cai.py init      Smoke-test claude -p only if the transcript
                            volume has no prior sessions. Used to seed
                            the self-improvement loop on a fresh
                            install; a no-op once transcripts exist.

    python cai.py analyze   Parse prior transcripts with parse.py, pipe
                            the combined analyzer prompt through
                            claude -p, and publish findings via
                            publish.py.

    python cai.py fix       Score eligible issues by age, category
                            success rate, and prior fix attempts; pick
                            the highest scorer labelled
                            `auto-improve:plan-approved` (reached
                            either automatically on a HIGH-confidence
                            plan or via admin resume from
                            `:human-needed`), run a cheap Haiku
                            pre-screen to classify the issue;
                            ambiguous issues are returned to their origin
                            label without cloning, while spike-shaped
                            issues are routed to :human-needed; if
                            actionable, lock it
                            via the `:in-progress` label, clone the repo
                            into /tmp, load the stored implementation
                            plan from the issue body (written by `cai
                            plan` between `<!-- cai-plan-start/end -->`
                            markers) if present, then run the fix
                            subagent (full tool permissions) with that
                            plan, and open a PR if the agent produced a
                            diff. Does NOT re-plan — planning is a
                            separate `cai plan` step. Rolls back the
                            label on empty diff or any failure.

    python cai.py verify    Mechanical, no-LLM. Walk issues with
                            `:pr-open`, find their linked PR by `Refs`
                            search, and transition the label:
                            merged → `:merged`,
                            closed-unmerged → `:refined`,
                            no-linked-PR → `:raised`.

    python cai.py audit     Periodic queue/PR consistency audit.
                            Deterministically rolls back stale
                            `:in-progress` (>6h), `:revising` (>1h),
                            and `:applying` (>2h) locks; unsticks stale
                            `:no-action` issues; flags stale `:merged`
                            issues; recovers `:pr-open` issues with closed
                            PRs; cleans up orphaned branches; retroactively
                            closes closed issues lacking terminal labels
                            (as 'not planned'); then runs an Opus-driven
                            semantic check for duplicates, stuck loops, label
                            corruption, and human-needed issues
                            (pipeline jams, abandoned tasks, repeated
                            diversions, missing reasons). Findings are
                            pre-screened for duplicates/resolved via
                            cai-dup-check; survivors are published as
                            `auto-improve:raised` + `audit` issues in
                            the unified label scheme.

    python cai.py audit-module  On-demand per-module audit: iterate every
                            module in docs/modules.yaml and dispatch the
                            matching on-demand audit agent for each module.
                            Takes --kind <kind> to select which audit type
                            to run (choices: good-practices, code-reduction,
                            cost-reduction, workflow-enhancement). Publishes
                            findings through the existing dedup/dup-check
                            pipeline.

    python cai.py audit-triage  Autonomously resolve `auto-improve:raised`
                            + `audit` findings without opening a PR. Calls
                            `cai-audit-triage` which classifies each finding
                            as `close_duplicate`, `close_resolved`,
                            `passthrough`, or `escalate`.

    python cai.py revise    Watch `:pr-open` PRs for new comments and
                            let the implement subagent iterate on the same
                            branch. Force-pushes revisions with
                            `--force-with-lease`.

    python cai.py confirm   Re-analyze the recent transcript window and
                            verify whether `:merged` issues are actually
                            solved. Patterns that disappeared trigger a
                            SOLVED transition, which closes the issue as
                            GitHub "completed"; patterns that persist are
                            re-queued to `:refined` (up to 3 attempts),
                            then escalated to `:needs-human-review`.

    python cai.py review-pr Walk open PRs against main, run a
                            consistency review for ripple effects. Post
                            findings as PR comments; out-of-scope findings
                            become separate GitHub issues. Skips PRs
                            already reviewed at their current HEAD SHA.

    python cai.py merge     Confidence-gated auto-merge for bot PRs.
                            Evaluates each :pr-open PR against its
                            linked issue, posts a verdict comment, and
                            merges when confidence meets the threshold.

    python cai.py refine      Pick the oldest issue labelled
                            `auto-improve:raised`, invoke the
                            cai-refine subagent (read-only) to
                            produce a structured plan, update the
                            issue body, and transition the label to
                            `auto-improve:refined`.

    python cai.py plan      Run the plan-select pipeline on the
                            oldest issue labelled `auto-improve:
                            refined`. Clones the repo into /tmp,
                            runs 2 serial plan agents followed by
                            a select agent, stores the chosen plan
                            in the issue body inside
                            `<!-- cai-plan-start/end -->` markers,
                            and transitions the label from
                            `auto-improve:refined` to
                            `auto-improve:planned`. Invoked as part
                            of `cai.py cycle`; also runnable manually.

    python cai.py code-audit  Weekly source-code consistency audit.
                            Clones the repo read-only, runs a Sonnet
                            agent that checks for cross-file
                            inconsistencies, dead code, missing
                            references, and similar concrete problems.
                            Findings are published as issues via
                            publish.py with the `code-audit` namespace.

    python cai.py agent-audit  Weekly audit of .claude/agents/**/*.md for
                            Claude Code best-practice violations, unused
                            agents (not invoked via `--agent` anywhere), and
                            near-duplicate agents. Runs on Opus. Findings are
                            published via publish.py with the `agent-audit`
                            namespace.

    python cai.py propose     Weekly creative improvement proposal.
                            Clones the repo read-only, runs a creative
                            agent to propose an ambitious improvement,
                            then a review agent to evaluate feasibility.
                            Approved proposals are filed as issues with
                            `auto-improve:raised` so they flow through
                            the refine → fix pipeline.

    python cai.py update-check  Weekly Claude Code release check.
                            Clones the repo, fetches the latest Claude
                            Code releases from GitHub, and runs a Sonnet
                            agent that compares the current pinned
                            version against the latest releases. Findings
                            (new versions, deprecated flags, best
                            practices) are published via publish.py with
                            the `update-check` namespace.

    python cai.py external-scout  Weekly scout for open-source libraries
                            that could replace in-house plumbing.
                            Clones the repo, runs an Opus agent that
                            walks the codebase, picks one category of
                            in-house utility, searches the open-source
                            ecosystem for mature alternatives, and emits
                            a single adoption proposal (or No findings.).
                            Findings are published via publish.py with
                            the `external-scout` namespace. Uses the
                            built-in `memory: project` pool to avoid
                            re-proposing the same category or library.

    python cai.py health-report  Automated pipeline health report with
                            anomaly detection. Aggregates cost trends
                            (last 7d vs prior 7d), issue queue counts,
                            pipeline stalls, and fix quality metrics.
                            Flags anomalies with 🔴/🟡/🟢 traffic-light
                            indicators and posts the report as a GitHub
                            issue with the `health-report` label. Use
                            --dry-run to print to stdout without posting.

    python cai.py check-workflows  Periodic GitHub Actions workflow failure
                            monitor. Fetches recent workflow runs, filters
                            out bot branches (auto-improve/), and runs a
                            Haiku agent to identify persistent failures.
                            Findings are published via publish.py with the
                            `check-workflows` namespace. Runs every 6 hours
                            by default (configurable via CAI_CHECK_WORKFLOWS_SCHEDULE).

    python cai.py unblock   Scan open issues/PRs parked at
                            `auto-improve:human-needed` (or
                            `auto-improve:pr-human-needed`) that an admin
                            has marked ready for resume by applying the
                            `human:solved` label. For each such item, invokes
                            the `cai-unblock` Haiku agent to classify the
                            admin's comment, fires the matching state
                            transition, strips the label, and returns the
                            issue/PR to the FSM. Requires CAI_ADMIN_LOGINS
                            to be set; without it, `human:solved` is silently
                            ignored.

    python cai.py rescue    Autonomous counterpart to `unblock`. Scans
                            `auto-improve:human-needed` issues and
                            `auto-improve:pr-human-needed` PRs that have
                            NOT yet received the `human:solved` label and
                            asks the `cai-rescue` agent whether each
                            divert can be resumed without human input. On
                            a HIGH-confidence `AUTONOMOUSLY_RESOLVABLE`
                            verdict, fires the matching state transition
                            and posts an audit comment; otherwise leaves
                            the target parked. Optionally collects
                            `prevention_finding` text from the agent and
                            publishes the survivors as
                            `auto-improve:raised` issues so recurring
                            divert patterns get fixed at the source.

Default schedules (all configurable via environment variables):

    Subcommand        Default cron       Frequency               Env var
    ─────────────     ──────────────     ─────────────────────   ──────────────────────────
    cycle             0 * * * *          Hourly at :00           CAI_CYCLE_SCHEDULE
    verify            15 * * * *         Hourly at :15           CAI_VERIFY_SCHEDULE
    analyze           0 0 * * *          Daily at midnight       CAI_ANALYZER_SCHEDULE
    audit             0 */6 * * *        Every 6 hours           CAI_AUDIT_SCHEDULE
    check-workflows   0 */6 * * *        Every 6 hours           CAI_CHECK_WORKFLOWS_SCHEDULE
    code-audit        0 3 * * 0          Weekly, Sundays 03:00   CAI_CODE_AUDIT_SCHEDULE
    propose           0 4 * * 0          Weekly, Sundays 04:00   CAI_PROPOSE_SCHEDULE
    cost-optimize     0 5 * * 0          Weekly, Sundays 05:00   CAI_COST_OPTIMIZE_SCHEDULE
    agent-audit       0 6 * * 0          Weekly, Sundays 06:00   CAI_AGENT_AUDIT_SCHEDULE
    update-check      0 4 * * 1          Weekly, Mondays 04:00   CAI_UPDATE_CHECK_SCHEDULE
    external-scout    0 6 * * 1          Weekly, Mondays 06:00   CAI_EXTERNAL_SCOUT_SCHEDULE
    health-report     0 7 * * 1          Weekly, Mondays 07:00   CAI_HEALTH_REPORT_SCHEDULE
    rescue            30 */4 * * *       Every 4 hours at :30    CAI_RESCUE_SCHEDULE

The container runs `entrypoint.sh`, which executes `cai.py cycle` once
synchronously at startup (driving the full issue-solving pipeline:
verify → confirm → drain PRs → refine → plan → implement loop), then hands
off to supercronic. Each cron tick is a fresh process. The pipeline is
driven by a single `CAI_CYCLE_SCHEDULE` cron line; cross-instance
serialization is handled GitHub-side via an `auto-improve:locked`
ownership lock (label + a `<!-- cai-lock owner=... -->` claim comment,
acquired at every dispatch entry) so two cai instances — on the same
host or across hosts — cannot advance the same issue/PR concurrently.
A stale-lock watchdog expires the lock after 1h. Orthogonal tasks
(analyze, audit, propose, update-check, health-report, cost-optimize,
check-workflows, code-audit, agent-audit, external-scout) keep their
own schedules and are not run at startup.

The gh auth check is done once per subcommand invocation. We want a
clear error message in docker logs if credentials ever disappear from
the cai_home volume.

No third-party Python dependencies — only stdlib.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import uuid

from datetime import datetime, timedelta, timezone
from pathlib import Path

from cai_lib.publish import (  # noqa: E402
    ensure_all_labels, AUDIT_CATEGORIES,
    create_issue, issue_exists, ensure_labels,
)
from cai_lib.dup_check import check_duplicate_or_resolved  # noqa: E402


from cai_lib.config import *  # noqa: E402,F403
from cai_lib.config import (  # noqa: E402
    _STALE_MERGED_DAYS,
)

from cai_lib.logging_utils import (  # noqa: E402
    log_cost,  # noqa: F401
    _get_issue_category, _log_outcome, _load_outcome_stats,
)
from cai_lib.audit.cost import (  # noqa: E402
    _load_outcome_counts, _load_cost_log, _row_ts, _build_cost_summary,
)


# ---------------------------------------------------------------------------
# Common helpers
# ---------------------------------------------------------------------------

from cai_lib.subprocess_utils import _run, _run_claude_p  # noqa: E402


from cai_lib.github import (  # noqa: E402
    check_gh_auth, check_claude_auth,
    _set_pr_labels, _issue_has_label, _build_issue_block,
    _build_implement_user_message, _fetch_linked_issue_block,
    close_issue_not_planned, _recover_stale_pr_open,
)
from cai_lib.cmd_helpers import _work_directory_block  # noqa: E402
from cai_lib.cmd_unblock import cmd_unblock  # noqa: E402
from cai_lib.cmd_rescue import cmd_rescue  # noqa: E402
from cai_lib.cmd_review_docs import cmd_review_docs  # noqa: E402
from cai_lib.cmd_misc import (  # noqa: E402
    cmd_init, cmd_verify, cmd_test,
    cmd_cost_report, cmd_health_report, cmd_check_workflows,
)
from cai_lib.cmd_agents import (  # noqa: E402
    cmd_analyze, cmd_audit, cmd_propose, cmd_code_audit,
    cmd_agent_audit, cmd_update_check, cmd_cost_optimize, cmd_external_scout,
    cmd_audit_module,
)
from cai_lib.cmd_cycle import cmd_cycle, cmd_dispatch  # noqa: E402
from cai_lib.transcript_sync import cmd_transcript_sync  # noqa: E402




# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(prog="cai")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="Smoke test if no transcripts exist")
    sub.add_parser("analyze", help="Run the analyzer + publish findings")

    dispatch_parser = sub.add_parser("dispatch", help="Dispatch FSM action (oldest actionable by default)")
    dispatch_parser.add_argument("--issue", type=int, default=None, help="Dispatch a specific issue by number")
    dispatch_parser.add_argument("--pr", type=int, default=None, help="Dispatch a specific PR by number")

    sub.add_parser("verify", help="Update labels based on PR merge state")
    sub.add_parser(
        "audit",
        help="Periodic queue/PR consistency audit with stale-lock rollback and semantic analysis",
    )
    audit_module_p = sub.add_parser(
        "audit-module",
        help=(
            "On-demand per-module audit: iterate every module in "
            "docs/modules.yaml and dispatch the matching on-demand "
            "audit agent, publishing findings via the existing "
            "dedup/dup-check pipeline."
        ),
    )
    audit_module_p.add_argument(
        "--kind",
        required=True,
        choices=["good-practices", "code-reduction", "cost-reduction", "workflow-enhancement"],
        help="Per-module audit kind to dispatch",
    )
    sub.add_parser("code-audit", help="Audit repo source code for inconsistencies")
    sub.add_parser("agent-audit", help="Weekly audit of .claude/agents/ for consistency and usage")
    sub.add_parser("propose", help="Weekly creative improvement proposal")
    sub.add_parser("update-check", help="Check Claude Code releases for workspace improvements")
    sub.add_parser("external-scout", help="Scout open-source libraries to replace in-house plumbing")
    sub.add_parser(
        "unblock",
        help="Resume :human-needed issues when an admin has commented",
    )
    sub.add_parser(
        "rescue",
        help="Autonomously resume :human-needed issues that don't actually require human input (Opus cai-rescue agent)",
    )
    sub.add_parser("cost-optimize", help="Weekly cost-reduction proposal or evaluation")
    sub.add_parser("check-workflows", help="Check GitHub Actions for recent workflow failures and raise findings")
    sub.add_parser("cycle", help="One cycle tick: verify, audit, dispatch one actionable issue/PR")
    sub.add_parser(
        "transcript-sync",
        help="Push local transcripts to the central server and pull the aggregate mirror back (no-op when CAI_TRANSCRIPT_SYNC_URL unset)",
    )
    sub.add_parser("test", help="Run the project test suite")

    review_docs_parser = sub.add_parser(
        "review-docs",
        help="CI-mode doc review: run cai-review-docs on a PR and commit any doc fixes (no FSM transitions)",
    )
    review_docs_parser.add_argument(
        "--pr", type=int, required=True,
        help="Open PR number to review",
    )

    cost_parser = sub.add_parser(
        "cost-report",
        help="Print a human-readable cost report from the cost log",
    )
    cost_parser.add_argument(
        "--days", type=int, default=7,
        help="Window in days to include (default: 7)",
    )
    cost_parser.add_argument(
        "--top", type=int, default=10,
        help="Number of most-expensive invocations to list (default: 10)",
    )
    cost_parser.add_argument(
        "--by", choices=["category", "agent", "day"], default="category",
        help="Aggregation grouping (default: category)",
    )

    health_parser = sub.add_parser(
        "health-report",
        help="Automated pipeline health report with anomaly detection",
    )
    health_parser.add_argument(
        "--dry-run", action="store_true", default=False,
        help="Print report to stdout without posting a GitHub issue",
    )

    args = parser.parse_args()

    # transcript-sync only shells out to rsync/ssh; it never touches GitHub
    # or Claude. Skipping the auth checks lets sync run independently of
    # pipeline-credential state (e.g. when gh auth hasn't been configured
    # yet on a fresh install that wants to start shipping transcripts).
    if args.command != "transcript-sync":
        auth_rc = check_gh_auth()
        if auth_rc != 0:
            return auth_rc

        auth_rc = check_claude_auth()
        if auth_rc != 0:
            return auth_rc

        ensure_all_labels()

    handlers = {
        "init": cmd_init,
        "analyze": cmd_analyze,
        "dispatch": cmd_dispatch,
        "verify": cmd_verify,
        "audit": cmd_audit,
        "audit-module": cmd_audit_module,
        "code-audit": cmd_code_audit,
        "agent-audit": cmd_agent_audit,
        "propose": cmd_propose,
        "update-check": cmd_update_check,
        "external-scout": cmd_external_scout,
        "unblock": cmd_unblock,
        "rescue": cmd_rescue,
        "review-docs": cmd_review_docs,
        "cycle": cmd_cycle,
        "cost-report": cmd_cost_report,
        "health-report": cmd_health_report,
        "cost-optimize": cmd_cost_optimize,
        "check-workflows": cmd_check_workflows,
        "transcript-sync": cmd_transcript_sync,
        "test": cmd_test,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
