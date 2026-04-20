"""cai_lib.actions.refine — handler for IssueState.REFINING.

Invoked by the FSM dispatcher after it has fetched an open issue and
verified its state is ``IssueState.REFINING``. Runs the ``cai-refine``
agent, parses its output, and fires the appropriate transition
(``refining_to_refined``, ``refining_to_exploration``, or a
decomposition path that labels the parent ``:parent``).
"""
from __future__ import annotations

import re
import sys
import time

from cai_lib.config import (
    REPO,
    LABEL_RAISED,
    LABEL_REFINING,
    LABEL_PARENT,
    LABEL_DEPTH_PREFIX,
    MAX_DECOMPOSITION_DEPTH,
)
from cai_lib.fsm import apply_transition
from cai_lib.github import (
    _gh_json, _set_labels, _build_issue_block, _post_issue_comment,
)
from cai_lib.subprocess_utils import _run, _run_claude_p
from cai_lib.logging_utils import log_run
from cai_lib.cmd_helpers import _strip_stored_plan_block
from cai_lib.cmd_implement import _parse_decomposition
from cai_lib.issues import create_issue, link_sub_issue, list_sub_issues


_REFINE_NEXT_STEP_RE = re.compile(
    r"^\s*NextStep\s*[:=]\s*(PLAN|EXPLORE)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def _parse_refine_next_step(text: str) -> "str | None":
    """Extract ``NextStep: PLAN | EXPLORE`` from cai-refine output.

    Returns ``"PLAN"``, ``"EXPLORE"``, or ``None`` when missing. A missing
    decision is treated as PLAN by the caller — staying on :refined is
    the safe default that preserves today's behaviour.
    """
    if not text:
        return None
    m = _REFINE_NEXT_STEP_RE.search(text)
    if not m:
        return None
    return m.group(1).upper()


# ---------------------------------------------------------------------------
# Scope-guardrail contradiction lint (issue #919).
#
# When cai-refine writes a "Scope guardrails" entry that forbids editing
# a file that also appears in its own "Files to change" list, the plan
# agent produced downstream will self-flag the contradiction and drop
# confidence to MEDIUM — wasting a full plan cycle per #902. Catch the
# direct contradiction deterministically here and divert to :human-needed
# with a clear comment so the admin can either drop the guardrail or
# split the forbidden work into a predecessor issue.
# ---------------------------------------------------------------------------

# _PATH_RE only recognises paths that begin with a word char (no leading
# slash). Absolute clone-prefix paths like
# "/tmp/cai-plan-902-abcd1234/cai_lib/publish.py" are normalised by the
# _CLONE_PREFIX_RE pre-strip below before this regex runs, so the
# resulting "cai_lib/publish.py" matches identically to a bare relative
# reference.
_PATH_RE = re.compile(
    r"(?<![\w/.-])"
    r"([A-Za-z_][\w./-]*\.(?:py|md|yaml|yml|json|sh|js|ts|tsx|jsx|toml|cfg|ini|txt))"
    r"(?![\w/.-])"
)

# Matches the work-directory prefix the planner/implement subagent injects
# into its plan ("/tmp/cai-<phase>-<issue>-<hash>/"). Stripping it before
# path extraction lets a `/tmp/.../cai_lib/publish.py` reference match a
# bare `cai_lib/publish.py` reference for the contradiction check.
_CLONE_PREFIX_RE = re.compile(r"/tmp/cai-[^/\s]+/")

# Matches a leading "./" on a path reference (e.g. "./cai_lib/foo.py")
# that occurs at the start of a token — at string start or after
# whitespace / backtick / bracket / comma / etc. The lookbehind excludes
# word chars and "/" so mid-path occurrences like "foo/./bar" are NOT
# stripped. Running this after _CLONE_PREFIX_RE (which removes
# "/tmp/cai-<phase>-<issue>-<hash>/") and before _PATH_RE lets the
# word-char-anchored _PATH_RE match the bare remainder, since its own
# lookbehind (?<![\w/.-]) would otherwise block a match immediately
# after a slash.
_DOT_SLASH_RE = re.compile(r"(?<![/\w])\./")

_FILES_TO_CHANGE_HEADER = "### Files to change"
_SCOPE_GUARDRAILS_HEADER = "### Scope guardrails"


def _extract_section(refined_body: str, header: str) -> str:
    """Return the text of the named ``### <Header>`` section, or ``""``."""
    if not refined_body or header not in refined_body:
        return ""
    lines = refined_body.splitlines()
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip() == header)
    except StopIteration:
        return ""
    end = len(lines)
    for j in range(start + 1, len(lines)):
        stripped = lines[j].lstrip()
        if stripped.startswith("## ") or stripped.startswith("### "):
            end = j
            break
    return "\n".join(lines[start + 1:end]).strip()


def _extract_paths(section_text: str) -> set[str]:
    # Pre-strip clone-prefix paths so the path regex (which is anchored on
    # word chars and cannot start with `/`) matches the relative remainder.
    text = _CLONE_PREFIX_RE.sub("", section_text or "")
    # Pre-strip a leading "./" so references like "./cai_lib/foo.py" also
    # match _PATH_RE on the bare remainder "cai_lib/foo.py". Without this,
    # _PATH_RE's lookbehind (?<![\w/.-]) blocks matching after a slash.
    text = _DOT_SLASH_RE.sub("", text)
    paths: set[str] = set()
    for m in _PATH_RE.finditer(text):
        raw = m.group(1).strip("`()[],.;: ")
        if not raw or any(ch.isspace() for ch in raw):
            continue
        paths.add(raw)
    return paths


def _extract_files_to_change(refined_body: str) -> set[str]:
    return _extract_paths(_extract_section(refined_body, _FILES_TO_CHANGE_HEADER))


def _extract_scope_guardrails_paths(refined_body: str) -> set[str]:
    return _extract_paths(_extract_section(refined_body, _SCOPE_GUARDRAILS_HEADER))


def _detect_guardrail_contradictions(refined_body: str) -> list[str]:
    files = _extract_files_to_change(refined_body)
    guards = _extract_scope_guardrails_paths(refined_body)
    ignorable = {"CODEBASE_INDEX.md"}
    both = sorted((files & guards) - ignorable)
    return [p for p in both if not p.startswith("docs/")]


def _issue_depth(issue: dict) -> int:
    """Return the decomposition depth of *issue* from its ``depth:N`` label.

    Returns 0 if no ``depth:`` label is present (top-level issue).
    """
    for label in issue.get("labels", []):
        name = label if isinstance(label, str) else label.get("name", "")
        if name.startswith(LABEL_DEPTH_PREFIX):
            try:
                return int(name[len(LABEL_DEPTH_PREFIX):])
            except ValueError:
                pass
    return 0


def _find_sub_issue(parent_number: int, step: int) -> "int | None":
    """Return the issue number of an existing sub-issue for *parent_number*
    / *step* (open or closed), or None if none exists.

    Uses GitHub's native sub-issues API and matches by step number
    in the title.
    """
    subs = list_sub_issues(parent_number)
    pattern = re.compile(rf"\[#{parent_number}\s+Step\s+{step}/\d+\]")
    issues = [s for s in subs if pattern.search(s.get("title", ""))]
    if not issues:
        return None
    # Return the lowest (earliest-created) matching number for stability.
    return min(int(i["number"]) for i in issues)


def _create_sub_issues(
    steps: list[dict], parent_number: int, parent_title: str,
    depth: int = 0,
) -> list[int]:
    """Create GitHub sub-issues for a multi-step decomposition.

    Each sub-issue gets:
    - A title formatted as `[#{parent_number} Step {step}/{total}] {title}`
      (e.g. `[#123 Step 1/3] Add schema migration`)
    - A body with a back-reference to the parent issue
    - A native sub-issue link to the parent via the GitHub sub-issues API

    Returns list of created issue numbers (may be shorter than *steps*
    if some creations fail).
    """
    total = len(steps)
    created: list[int] = []
    for s in steps:
        # Guard against duplicate creation: if a sub-issue for this
        # parent+step already exists (open or closed), reuse it instead
        # of spawning another. This makes refine idempotent across
        # re-runs (e.g. after rollback from :no-action to :raised).
        existing = _find_sub_issue(parent_number, s["step"])
        if existing is not None:
            print(
                f"[cai refine] sub-issue for parent #{parent_number} "
                f"step {s['step']} already exists as #{existing}; "
                f"skipping creation",
                flush=True,
            )
            created.append(existing)
            continue
        body = (
            f"{s['body']}\n\n"
            f"---\n"
            f"_Sub-issue of #{parent_number} ({parent_title}). "
            f"Step {s['step']} of {total}._\n"
        )
        title = f"[#{parent_number} Step {s['step']}/{total}] {s['title']}"
        labels = ["auto-improve", LABEL_RAISED, f"{LABEL_DEPTH_PREFIX}{depth}"]
        # Use create_issue() (REST API) instead of gh issue create so we
        # get back the internal `id` needed for link_sub_issue().
        meta = create_issue(title, body, labels)
        if meta:
            num = meta.get("number", 0)
            url = meta.get("html_url", "")
            if num:
                created.append(num)
                # Establish native GitHub sub-issue relationship so
                # all_sub_issues_closed() in the verify sweep can detect
                # parent completion without falling back to checklist regex.
                link_sub_issue(parent_number, meta["id"])
            print(f"[cai refine] created sub-issue: {url}", flush=True)
        else:
            print(
                f"[cai refine] failed to create sub-issue "
                f"'Step {s['step']}'",
                file=sys.stderr,
            )
    return created


def handle_refine(issue: dict) -> int:
    """Invoke the cai-refine agent on *issue* (already at :refining)."""
    t0 = time.monotonic()

    issue_number = issue["number"]
    title = issue["title"]
    print(f"[cai refine] targeting #{issue_number}: {title}", flush=True)

    # Move :raised → :refining before the agent runs so observers see
    # the transient working state (useful for audits + the unified
    # driver). :refining issues picked up from the second pool are
    # already in the working state.
    issue_label_names_initial = [l["name"] for l in issue.get("labels", [])]  # noqa: E741
    if LABEL_RAISED in issue_label_names_initial:
        apply_transition(
            issue_number, "raise_to_refining",
            current_labels=issue_label_names_initial,
            log_prefix="cai refine",
        )

    # Build user message. Every invocation is treated as a fresh pass —
    # the agent may rewrite the body to incorporate exploration findings
    # and re-decide NextStep.
    user_message = _build_issue_block(issue)
    current_depth = _issue_depth(issue)
    if current_depth >= MAX_DECOMPOSITION_DEPTH:
        user_message += (
            f"\n\nIMPORTANT: This issue is at decomposition depth {current_depth} "
            f"(max {MAX_DECOMPOSITION_DEPTH}). Do NOT produce a "
            f"`## Multi-Step Decomposition` section. Refine this issue as a single "
            f"unit of work."
        )
    result = _run_claude_p(
        ["claude", "-p", "--agent", "cai-refine",
         "--dangerously-skip-permissions"],
        category="refine",
        agent="cai-refine",
        input=user_message,
    )
    print(result.stdout, flush=True)

    if result.returncode != 0:
        print(
            f"[cai refine] claude -p failed (exit {result.returncode}):\n"
            f"{result.stderr}",
            flush=True,
        )
        dur = f"{int(time.monotonic() - t0)}s"
        log_run("refine", repo=REPO, issue=issue_number,
                duration=dur, result="agent_failed", exit=result.returncode)
        return result.returncode

    stdout = result.stdout

    # Check for early-exit (already structured).
    if "## No Refinement Needed" in stdout:
        print(
            f"[cai refine] #{issue_number} already structured; "
            f"advancing :refining → :refined",
            flush=True,
        )
        apply_transition(
            issue_number, "refining_to_refined",
            current_labels=[LABEL_REFINING],
            log_prefix="cai refine",
        )
        dur = f"{int(time.monotonic() - t0)}s"
        log_run("refine", repo=REPO, issue=issue_number,
                duration=dur, result="already_structured", exit=0)
        return 0

    # Check for multi-step decomposition. Parent issues take on
    # :parent and drop out of the normal FSM; sub-issues become the
    # new units of work at :raised.
    if "## Multi-Step Decomposition" in stdout:
        steps = _parse_decomposition(stdout)
        if steps and len(steps) >= 2:
            print(
                f"[cai refine] #{issue_number} decomposed into "
                f"{len(steps)} steps",
                flush=True,
            )
            sub_nums = _create_sub_issues(steps, issue_number, title, depth=current_depth + 1)
            _set_labels(
                issue_number,
                add=[LABEL_PARENT],
                remove=[LABEL_REFINING],
                log_prefix="cai refine",
            )
            dur = f"{int(time.monotonic() - t0)}s"
            log_run(
                "refine", repo=REPO, issue=issue_number,
                duration=dur, result="decomposed",
                sub_issues=len(sub_nums), steps=len(steps), exit=0,
            )
            return 0
        # Malformed decomposition (< 2 steps) — fall through to normal
        # refinement.
        print(
            f"[cai refine] #{issue_number} decomposition had "
            f"{len(steps)} step(s); falling through to normal refinement",
            flush=True,
        )

    # Parse the refined issue block.
    marker = "## Refined Issue"
    marker_pos = stdout.find(marker)
    if marker_pos == -1:
        print(
            f"[cai refine] agent output missing '{marker}' marker; "
            f"leaving #{issue_number} as-is",
            flush=True,
        )
        dur = f"{int(time.monotonic() - t0)}s"
        log_run("refine", repo=REPO, issue=issue_number,
                duration=dur, result="no_marker", exit=0)
        return 0

    refined_body = stdout[marker_pos:].strip()

    # Scope-guardrail contradiction lint (issue #919).
    contradictions = _detect_guardrail_contradictions(refined_body)
    if contradictions:
        bullet_list = "\n".join(f"- `{p}`" for p in contradictions)
        divert_reason = (
            "Refinement was paused because the proposed scope "
            "contradicts itself: the following file(s) are listed under "
            "**Files to change** *and* under **Scope guardrails**.\n\n"
            f"{bullet_list}\n\n"
            "Either drop the guardrail (the file genuinely needs "
            "editing) or split the forbidden work into a predecessor "
            "issue and drop it from **Files to change** here."
        )
        apply_transition(
            issue_number, "refining_to_human",
            current_labels=[LABEL_REFINING],
            log_prefix="cai refine",
            divert_reason=divert_reason,
        )
        dur = f"{int(time.monotonic() - t0)}s"
        print(
            f"[cai refine] #{issue_number} diverted :refining → :human-needed "
            f"in {dur} (guardrail contradicts Files-to-change: "
            f"{', '.join(contradictions)})",
            flush=True,
        )
        log_run(
            "refine", repo=REPO, issue=issue_number,
            duration=dur, result="guardrail_contradiction",
            contradictions=",".join(contradictions), exit=0,
        )
        return 0

    # Build the new issue body: refined content + original text quoted.
    next_step = _parse_refine_next_step(stdout)
    original_body = _strip_stored_plan_block(issue.get("body") or "(no body)")
    quoted_original = "\n".join(f"> {line}" for line in original_body.splitlines())
    new_body = (
        f"{refined_body}\n\n"
        f"---\n\n"
        f"> **Original issue text:**\n>\n"
        f"{quoted_original}\n"
    )

    # Update the issue body.
    update = _run(
        ["gh", "issue", "edit", str(issue_number),
         "--repo", REPO, "--body", new_body],
        capture_output=True,
    )
    if update.returncode != 0:
        print(
            f"[cai refine] gh issue edit failed:\n{update.stderr}",
            file=sys.stderr,
        )
        dur = f"{int(time.monotonic() - t0)}s"
        log_run("refine", repo=REPO, issue=issue_number,
                duration=dur, result="edit_failed", exit=1)
        return 1

    # Transition out of :refining. NextStep: EXPLORE routes the
    # issue to :needs-exploration; anything else advances to :refined
    # for cmd_plan to pick up.
    dur = f"{int(time.monotonic() - t0)}s"
    if next_step == "EXPLORE":
        apply_transition(
            issue_number, "refining_to_exploration",
            current_labels=[LABEL_REFINING],
            log_prefix="cai refine",
        )
        print(
            f"[cai refine] #{issue_number} refined and routed to "
            f":needs-exploration in {dur}",
            flush=True,
        )
        log_run("refine", repo=REPO, issue=issue_number,
                duration=dur, result="refined_explore", exit=0)
        return 0

    apply_transition(
        issue_number, "refining_to_refined",
        current_labels=[LABEL_REFINING],
        log_prefix="cai refine",
    )

    print(
        f"[cai refine] #{issue_number} refined and advanced :refining → :refined "
        f"in {dur}",
        flush=True,
    )
    log_run("refine", repo=REPO, issue=issue_number,
            duration=dur, result="refined", exit=0)
    return 0
