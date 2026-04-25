"""cai_lib.actions.implement — handler for the implement phase of the FSM.

Derived from ``cmd_implement`` in ``cai.py``. The dispatcher hands the
handler an issue whose state is either :class:`IssueState.PLAN_APPROVED`
(fresh entry — we apply ``approved_to_in_progress`` first) or
:class:`IssueState.IN_PROGRESS` (resume — we skip the entry transition
because a prior run locked the issue and crashed / was interrupted).

On fresh entry we also check whether a branch
``auto-improve/<issue_number>-*`` already exists on origin; if so we
reuse it (resume semantics) rather than creating a fresh branch.

Direct-invoke (``args.issue``) handling and the "find oldest
:plan-approved issue" pickup are intentionally dropped here — the
dispatcher owns queue selection.
"""
from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
import sys
import traceback
import uuid

from pathlib import Path

from cai_lib.config import (
    REPO,
    LABEL_IN_PROGRESS,
    LABEL_PLAN_APPROVED,
    LABEL_PR_OPEN,
    LABEL_REFINED,
    LABEL_HUMAN_NEEDED,
    LABEL_OPUS_ATTEMPTED,
    LABEL_EXTENDED_RETRIES,
    LABEL_RAISED,
    LOG_PATH,
)

# Model ID passed to ``claude -p --model`` when the Opus-escalation
# label is present on the issue. Kept in one place so the next Opus
# release only needs to touch this constant (and the matching pin in
# claude-code's declarative agent files).
_OPUS_MODEL_ID = "claude-opus-4-7"
from cai_lib.github import (
    _gh_json,
    _set_labels,
    _build_issue_block,
    close_issue_not_planned,
)
from cai_lib.claude_argv import _run_claude_p
from cai_lib.subprocess_utils import _run
from cai_lib.utils.log import log_run
from cai_lib.cmd_helpers import (
    _work_directory_block,
    _git,
    _gh_user_identity,
    _fetch_previous_fix_attempts,
    _setup_agent_edit_staging,
    _apply_agent_edit_staging,
    _build_attempt_history_block,
    _extract_stored_plan,
)
from cai_lib.actions.plan import (
    _FILES_TO_CHANGE_SECTION_RE,
    _FILES_TO_CHANGE_PATH_RE,
)
from cai_lib.cmd_helpers_issues import _parse_files_to_change
from cai_lib.fsm import (
    fire_trigger,
    IssueState,
    get_issue_state,
)


# ---------------------------------------------------------------------------
# Handler-local helpers (moved from cai.py — only used by the implement phase).
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r"[^a-z0-9]+")

# Broader safety net (issue #1088): max consecutive implement-failure
# runs of ANY kind (subagent_failed, unexpected_error, clone_failed,
# fetch_existing_failed, push_failed, pr_create_failed) for the same
# issue before we park at :human-needed. `tests_failed` used to count
# here too, but local test failures no longer roll back to
# :plan-approved — the implement handler pushes the PR anyway and
# routes it to REVISION_PENDING so cai-revise fixes the tests on the
# PR side, which means a failing test never produces a second
# implement entry for the same issue in the first place. Catches the
# post-escalation loop observed on issue #1065 where Opus (resumed
# with LABEL_OPUS_ATTEMPTED) kept retrying on git/gh transport
# flakes.
_MAX_CONSECUTIVE_FAILED_ATTEMPTS = 3

# Extended cap used when LABEL_EXTENDED_RETRIES is present on the
# issue AND opus_escalation is False — i.e. for Sonnet-tier runs on
# plans that handle_plan_gate tagged as medium-scale refactors
# (issue #1151 — >=5 files AND >=40 edit steps). Raises the broad
# consecutive-failure cap from 3 to 5, giving Sonnet two extra
# attempts on transient infra / tooling flakes across many edit
# sites before the Opus one-shot is burned. At the Opus tier the
# cap stays at :data:`_MAX_CONSECUTIVE_FAILED_ATTEMPTS` because the
# rescue ``_issue_has_opus_attempted`` guard already refuses a
# second escalation, so more Opus retries would just loop.
_MAX_CONSECUTIVE_FAILED_ATTEMPTS_EXTENDED = 5

# Result tags that count as implement failures for
# :func:`_count_consecutive_failed_attempts`. ``tests_failed_pushed`` is
# NOT counted — it tags a successful PR open (tests failed locally but
# the PR was routed to cai-revise), not a stuck retry. Bail-outs such as
# ``no_stored_plan`` / ``bad_state`` / ``lock_failed`` and the legacy
# transition tags (``tests_failed_escalated`` / ``retries_exhausted``)
# are also excluded — they signal the pipeline already responded.
_COUNTED_IMPLEMENT_FAILURES: frozenset[str] = frozenset({
    "subagent_failed",
    "unexpected_error",
    "clone_failed",
    "fetch_existing_failed",
    "push_failed",
    "pr_create_failed",
})


# Length cap for the ``stderr_tail=`` field stamped on the
# ``result=subagent_failed`` log row (issue #1106). 120 chars keeps
# the log line readable while still carrying enough signal to tell a
# ``sdk_subtype=error_max_turns`` run apart from an
# ``sdk_subtype=error_max_structured_output_retries`` run. The token
# is always a single whitespace-free run so the
# ``_RESULT_TAG_RE = re.compile(r" result=(\S+)")`` classifier in
# :func:`_count_consecutive_failed_attempts` keeps matching
# ``subagent_failed`` unchanged.
_STDERR_TAIL_LIMIT = 120


def _format_stderr_tail(stderr: str) -> str:
    """Sanitize ``agent.stderr`` for inclusion as a key=value log field.

    Whitespace (spaces, tabs, newlines) collapses to ``_`` so the
    token is space-free; ``=`` is rewritten to ``:`` so a downstream
    key=value parser cannot accidentally split ``stderr_tail=foo=bar``
    into two fields. The result is truncated to
    :data:`_STDERR_TAIL_LIMIT` characters and coerced to at least
    ``"empty"`` when *stderr* is blank (the pre-#1106 shape, retained
    for a fresh ``stderr=""`` path the SDK may surface in future).

    Returned tokens are always non-empty, contain no whitespace and
    no ``=``, so they are safe to log between ``result=<tag>`` and
    ``exit=<code>`` without breaking either the existing
    :data:`_RESULT_TAG_RE` classifier or the audit agent's column
    layout.
    """
    text = (stderr or "").strip()
    if not text:
        return "empty"
    text = re.sub(r"\s+", "_", text)
    text = text.replace("=", ":")
    tail = text[:_STDERR_TAIL_LIMIT]
    return tail or "empty"


def _park_in_progress_at_human_needed(
    issue_number: int,
    *,
    reason: str,
    extra_remove: tuple[str, ...] = (),
    log_prefix: str = "cai implement",
) -> bool:
    """Park an :in-progress issue at :human-needed with a parseable divert-reason comment.

    Wraps :func:`fire_trigger` for ``in_progress_to_human_needed``
    so every implement-side escalation path goes through the PR #1072
    invariant (issue #1009) — which refuses silent HUMAN_NEEDED diverts
    and auto-posts a :func:`_render_human_divert_reason`-rendered
    comment carrying the ``Automation paused ``<transition>```,
    ``Required confidence: ``<val>``` and ``Reported confidence:
    ``<val>``` lines that ``_fetch_human_needed_issues`` in
    :mod:`cai_lib.cmd_agents` regex-matches for the audit agent's
    ``human_needed_reason_missing`` finder.

    Prior to issue #1083 this handler called
    ``_set_labels(add=[LABEL_HUMAN_NEEDED])`` directly in four places
    with a hand-rolled comment that only carried the marker header
    and skipped the structured fields — leaving #1044 (and any future
    opus-retry park) invisible to the audit parser.

    Returns True iff the park succeeded (labels changed + MARKER
    comment posted). Retries once on transient ``_set_labels`` failure
    — the same double-retry pattern the direct-label path had before
    the refactor. The retry re-invokes ``fire_trigger``; because
    its comment post is gated on ``ok`` from ``_set_labels``, a failed
    first attempt does not double-post the comment on the second
    attempt.
    """
    for _attempt in range(2):
        if fire_trigger(
            issue_number,
            "in_progress_to_human_needed",
            extra_remove=list(extra_remove),
            log_prefix=log_prefix,
            divert_reason=reason,
        )[0]:
            return True
    return False


def _slugify(text: str, max_len: int = 50) -> str:
    """Branch-friendly slug — lowercase ascii, dashes, no leading/trailing."""
    slug = _SLUG_RE.sub("-", text.lower()).strip("-")
    if len(slug) > max_len:
        slug = slug[:max_len].rstrip("-")
    return slug or "fix"


def _get_plan_for_fix(issue: dict) -> str | None:
    """Retrieve the implementation plan for a fix run.

    A stored plan is now required for every fix run (see the plan gate
    in `_select_fix_target` and `cmd_implement`). This helper is only
    called after that gate, so the plan should always be present; the
    None branch is kept as a defensive WARNING rather than a hard abort.
    """
    plan = _extract_stored_plan(issue.get("body", ""))
    if plan:
        print(f"[cai implement] using stored plan from issue body ({len(plan)} chars)", flush=True)
    else:
        print("[cai implement] WARNING: plan gate bypassed — no stored plan in body", flush=True)
    return plan


def _parse_suggested_issues(agent_output: str) -> list[dict]:
    """Extract suggested issues from the subagent's stdout.

    The subagent can emit blocks like:

        ## Suggested Issue
        ### Title
        <title text>
        ### Body
        <body text>

    Returns a list of dicts with 'title' and 'body' keys.
    """
    issues: list[dict] = []
    parts = re.split(r"^## Suggested Issue\s*$", agent_output, flags=re.MULTILINE)
    for part in parts[1:]:  # skip everything before the first marker
        title = ""
        body = ""
        title_match = re.search(
            r"^### Title\s*\n(.*?)(?=^### |\Z)",
            part,
            flags=re.MULTILINE | re.DOTALL,
        )
        body_match = re.search(
            r"^### Body\s*\n(.*?)(?=^## |\Z)",
            part,
            flags=re.MULTILINE | re.DOTALL,
        )
        if title_match:
            title = title_match.group(1).strip()
        if body_match:
            body = body_match.group(1).strip()
        if title:
            issues.append({"title": title, "body": body})
    return issues


def _create_suggested_issues(
    suggested: list[dict], source_issue_number: int,
) -> int:
    """Create GitHub issues raised by the implement subagent. Returns count created."""
    created = 0
    for s in suggested:
        issue_body = (
            f"{s['body']}\n\n"
            f"---\n"
            f"_Raised by the implement subagent while working on "
            f"#{source_issue_number}._\n"
        )
        labels = ",".join(["auto-improve", LABEL_RAISED])
        result = _run(
            [
                "gh", "issue", "create",
                "--repo", REPO,
                "--title", s["title"],
                "--body", issue_body,
                "--label", labels,
            ],
            capture_output=True,
        )
        if result.returncode == 0:
            url = result.stdout.strip()
            print(f"[cai implement] created suggested issue: {url}", flush=True)
            created += 1
        else:
            print(
                f"[cai implement] failed to create suggested issue "
                f"'{s['title']}': {result.stderr}",
                file=sys.stderr,
            )
    return created


def _pre_screen_issue_actionability(issue: dict) -> tuple[str, str]:
    """Cheap Haiku pre-screen to classify an issue before running plan-select.

    Returns a tuple of (verdict, reason) where verdict is one of:
      "actionable" — proceed to clone + plan-select pipeline
      "spike"      — issue needs research before code can be written
      "ambiguous"  — issue is too vague to identify any concrete change

    On any error (network failure, JSON parse error, unexpected format),
    falls through to ("actionable", "pre-screen error: <msg>") so the
    pipeline is never blocked.
    """
    try:
        title = issue.get("title", "")
        body = issue.get("body", "") or ""
        labels = ", ".join(lbl["name"] for lbl in issue.get("labels", []))

        prompt_text = (
            "You are a triage classifier for GitHub issues in an automated "
            "code-improvement pipeline.\n\n"
            "Given the issue below, classify it as one of:\n"
            '- "actionable": A concrete code change can be identified. '
            "This is the DEFAULT — use it when in doubt.\n"
            '- "spike": The issue clearly requires research, investigation, '
            "or evaluation before any code can be written. No specific file "
            "or function is identifiable.\n"
            '- "ambiguous": The issue is so vague that no file, function, '
            "or specific change can be identified, AND it does not describe "
            "a research task.\n\n"
            "IMPORTANT: Strongly bias toward \"actionable\". Only use "
            '"spike" or "ambiguous" when you are highly confident. If the '
            "issue mentions any specific file, function, code pattern, or "
            'error, classify as "actionable".\n\n'
            "Respond with ONLY a JSON object (no markdown fencing):\n"
            '{"verdict": "actionable"|"spike"|"ambiguous", '
            '"reason": "<one sentence explanation>"}\n\n'
            "## Issue\n"
            f"**Title:** {title}\n"
            f"**Labels:** {labels}\n\n"
            f"{body}"
        )

        # NOTE: intentional deviation from the --agent convention used by other
        # _run_claude_p call sites.  The pre-screen is a pure inline prompt with
        # no tool access and no agent definition file — using --model directly
        # avoids the overhead of an agent file for a call that never needs one.
        proc = _run_claude_p(
            ["claude", "-p", "--model", "claude-haiku-4-5", prompt_text],
            category="implement.pre-screen",
        )

        raw = (proc.stdout or "").strip()
        # Strip markdown code fences if the model ignored the instruction
        if raw.startswith("```"):
            raw = raw.removeprefix("```json").removeprefix("```")
            raw = raw.removesuffix("```").strip()

        parsed = json.loads(raw)
        verdict = parsed.get("verdict", "actionable")
        reason = parsed.get("reason", "")
        if verdict not in ("actionable", "spike", "ambiguous"):
            return ("actionable", f"unexpected verdict {verdict!r}")
        return (verdict, reason)

    except Exception as e:  # noqa: BLE001
        print(
            f"[cai implement] pre-screen error (falling through to actionable): {e}",
            file=sys.stderr,
            flush=True,
        )
        return ("actionable", f"pre-screen error: {e}")


def _find_existing_branch(issue_number: int) -> str | None:
    """Return an existing origin branch ``auto-improve/<N>-*``, or None.

    Uses the GitHub matching-refs API so we don't need a local clone to
    probe.  On any failure we return None and let the caller create a
    fresh branch — the resume optimisation is best-effort.
    """
    try:
        refs = _gh_json([
            "api",
            f"repos/{REPO}/git/matching-refs/heads/auto-improve/{issue_number}-",
        ])
    except subprocess.CalledProcessError as e:
        print(
            f"[cai implement] matching-refs lookup failed (continuing with fresh branch): {e.stderr}",
            file=sys.stderr,
            flush=True,
        )
        return None
    if not isinstance(refs, list) or not refs:
        return None
    # refs[i]["ref"] looks like "refs/heads/auto-improve/42-foo-bar-abcd1234".
    for entry in refs:
        ref = entry.get("ref", "")
        prefix = "refs/heads/"
        if ref.startswith(prefix):
            return ref[len(prefix):]
    return None


def _extract_test_failures(output: str, max_chars: int = 3000) -> str:
    """Filter ``python -m unittest -v`` output to FAIL/ERROR sections only.

    The full unittest log can be hundreds of lines of ``... ok`` entries
    that crowd out the actual failure signal when posted into a divert
    comment. We surface three concise pieces instead:

    1. A list of failing test identifiers (``FAIL: <dotted>`` /
       ``ERROR: <dotted>``) from the per-test status lines at the top
       of the verbose output.
    2. Each detailed ``FAIL:`` / ``ERROR:`` block emitted after the
       ``===`` separators, including the traceback for that test.
    3. The final summary line (``FAILED (failures=N, errors=M)`` or
       ``OK``) so the divert consumer can see aggregate counts.

    Result is capped at *max_chars* and suffixed with ``... (truncated)``
    when clipped. If the input has no recognisable FAIL/ERROR markers
    (e.g. a crash before unittest ran), the raw output is returned
    truncated so the divert comment is never empty.
    """
    lines = output.splitlines()

    status_re = re.compile(
        r"^(\S+)\s+\(([^)]+)\)\s+\.\.\.\s+(FAIL|ERROR)\b"
    )
    block_start_re = re.compile(r"^(FAIL|ERROR):\s+")

    failing_names: list[str] = []
    for ln in lines:
        m = status_re.match(ln)
        if m:
            failing_names.append(f"{m.group(3)}: {m.group(2)}")

    blocks: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        if block_start_re.match(lines[i]):
            block_lines = [lines[i]]
            i += 1
            while i < n:
                # Next detailed block starts — stop so we don't swallow it.
                if (
                    lines[i].startswith("=====")
                    and i + 1 < n
                    and block_start_re.match(lines[i + 1])
                ):
                    break
                # End of test session — stop before the "Ran N tests" summary.
                if (
                    lines[i].startswith("-----")
                    and i + 1 < n
                    and lines[i + 1].startswith("Ran ")
                ):
                    break
                block_lines.append(lines[i])
                i += 1
            blocks.append("\n".join(block_lines).rstrip())
        else:
            i += 1

    summary = ""
    for ln in reversed(lines):
        stripped = ln.strip()
        if stripped.startswith("FAILED") or stripped == "OK":
            summary = stripped
            break

    if not failing_names and not blocks:
        truncated = output[:max_chars]
        if len(output) > max_chars:
            truncated += "\n\n... (truncated)"
        return truncated

    parts: list[str] = []
    if failing_names:
        parts.append("Failing tests:")
        parts.extend(f"  - {name}" for name in failing_names)
        parts.append("")
    if blocks:
        parts.extend(blocks)
    if summary:
        if parts and parts[-1] != "":
            parts.append("")
        parts.append(summary)

    result = "\n".join(parts).rstrip()
    if len(result) > max_chars:
        result = result[:max_chars].rstrip() + "\n\n... (truncated)"
    return result


# Matches ``  File "<path>", line <N>, in <func>`` traceback frames.
_TRACEBACK_FRAME_RE = re.compile(
    r'^\s*File "([^"]+)", line (\d+), in (\S+)',
    re.MULTILINE,
)


def _enclosing_function_source(
    source: str, line: int,
) -> tuple[str, str] | None:
    """Return ``(func_name, func_source)`` for the innermost ``def`` /
    ``async def`` that encloses *line* in *source*, or ``None`` when no
    such function exists.

    Uses :mod:`ast` so nested defs and class methods resolve correctly.
    ``line`` is 1-based, matching Python traceback line numbers.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    lines = source.splitlines()
    best: ast.FunctionDef | ast.AsyncFunctionDef | None = None
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        end = node.end_lineno or node.lineno
        if node.lineno <= line <= end:
            if best is None or node.lineno >= best.lineno:
                best = node
    if best is None:
        return None
    end = best.end_lineno or best.lineno
    src = "\n".join(lines[best.lineno - 1: end])
    return (best.name, src)


def _extract_referenced_helpers(
    failure_output: str,
    work_dir: Path,
    max_chars: int = 2000,
) -> str:
    """Extract source of non-test helpers referenced by test tracebacks.

    Scans *failure_output* for ``File "<path>", line <N>, in <func>``
    frames. Skips entries under ``tests/``, absolute paths, ``..``-escapes,
    and anything containing ``site-packages`` (stdlib / third-party). For
    each remaining unique ``(path, func)`` pair reads
    ``<work_dir>/<path>`` and returns the source of the function
    enclosing line ``<N>``.

    Result is Markdown suitable for inlining into a GitHub issue comment:
    one fenced ``python`` block per helper, separated by blank lines.
    Returns an empty string when no helpers can be resolved. Capped at
    *max_chars*; clipped output is suffixed with ``... (truncated)``.

    Added in issue #987 — the 3-consecutive-``tests_failed`` divert
    comment previously only carried the test traceback. Pairing each
    traceback frame with the current implementation of the helper it
    landed in gives Opus / a human the exact context needed to diagnose
    a narrow regression (path normalisation, regex edge case, etc.)
    without re-reading the whole file.
    """
    seen: set[tuple[str, str]] = set()
    helpers: list[str] = []
    for m in _TRACEBACK_FRAME_RE.finditer(failure_output):
        path, line_s, func = m.group(1), m.group(2), m.group(3)
        if (
            path.startswith("tests/")
            or path.startswith("/")
            or ".." in path.split("/")
            or "site-packages" in path
        ):
            continue
        key = (path, func)
        if key in seen:
            continue
        seen.add(key)
        try:
            source = (work_dir / path).read_text()
        except OSError:
            continue
        res = _enclosing_function_source(source, int(line_s))
        if res is None:
            continue
        name, src = res
        helpers.append(
            f"`{path}` — `{name}` (line {line_s}):\n\n"
            f"```python\n{src}\n```"
        )
    if not helpers:
        return ""
    joined = "\n\n".join(helpers)
    if len(joined) > max_chars:
        joined = joined[:max_chars].rstrip() + "\n\n... (truncated)"
    return joined


# Match ``result=<tag>`` where <tag> is a space-bounded token. Used by
# :func:`_count_consecutive_failed_attempts` to classify each log line
# without the substring pitfalls of plain ``in`` checks.
_RESULT_TAG_RE = re.compile(r" result=(\S+)")


def _count_consecutive_failed_attempts(issue_number: int) -> int:
    """Count trailing consecutive implement-failure log entries for
    *issue_number* in LOG_PATH.

    A "failure" is any ``[implement]`` entry whose ``result=`` token
    is in :data:`_COUNTED_IMPLEMENT_FAILURES`. Walks the entries for
    this issue in reverse and counts how many trailing entries match.
    The walk stops at the first non-failure entry — ``tests_failed_pushed``
    (a successful PR open with failing local tests) and plain PR-open
    entries (which have no ``result=`` field) both end the streak.

    Returns 0 on any I/O failure (the guard is best-effort — we must
    never block the implement pipeline on log read errors).

    Added in issue #1088 to catch post-escalation loops — the canonical
    failure was issue #1065, which accumulated three ``unexpected_error``
    runs in a row without any dedicated counter noticing.
    """
    try:
        if not LOG_PATH.exists():
            return 0
        lines = LOG_PATH.read_text().splitlines()
    except OSError:
        return 0
    issue_tag = f" issue={issue_number} "
    relevant = [
        ln for ln in lines
        if "[implement]" in ln and issue_tag in (ln + " ")
    ]
    count = 0
    for ln in reversed(relevant):
        m = _RESULT_TAG_RE.search(ln)
        if m and m.group(1) in _COUNTED_IMPLEMENT_FAILURES:
            count += 1
        else:
            break
    return count


# ---------------------------------------------------------------------------
# Plan-scope enforcement (issue #1074).
#
# When ``cai-implement`` writes files the stored plan did not list, those
# out-of-scope edits can silently sink the PR. Issue #1065 is the
# canonical failure: the agent wrote an unrelated test module that
# referenced real git operations, causing two consecutive
# ``tests_failed`` runs and a divert.
#
# We detect scope violations **structurally in Python** from the untruncated
# plan text and the working-tree ``git status`` — mirroring the
# wrapper-driven scope guard pattern established in
# ``merge.py::_detect_unauthorized_agent_deletions`` (and documented in
# the shared memory entry ``merge-wrapper-driven-scope-exemptions.md``).
#
# Authoritative scope is the union of:
#
#   1. Every backticked ``path/with.ext`` token inside the plan's
#      ``### Files to change`` section (parsed via the same
#      ``_FILES_TO_CHANGE_SECTION_RE`` / ``_FILES_TO_CHANGE_PATH_RE``
#      already used by ``merge.py``).
#   2. Every backticked path in a ``#### Step N — Edit <path>`` or
#      ``#### Step N — Write <path>`` header (parsed via
#      ``_STEP_HEADER_RE`` below).
#   3. Always-in-scope paths in ``_ALWAYS_IN_SCOPE`` — currently the
#      PR-context dossier ``.cai/pr-context.md`` which the agent is
#      expected to write before exiting.
#
# Path normalisation strips any ``/tmp/cai-<kind>-<N>-<uid>/`` prefix
# so plans that reference clone-absolute paths from the plan phase's
# work directory align with the implement phase's ``git status``
# output. Staging-path canonicalisation expands each
# ``.cai-staging/...`` entry to its live-path alias (so a plan listing
# ``.cai-staging/agents/foo.md`` also authorises ``.claude/agents/foo.md``
# and vice versa).
#
# Out-of-scope paths are reverted:
#   - Tracked modifications / deletions → ``git checkout HEAD -- <path>``.
#   - Untracked additions → ``os.unlink`` (or ``shutil.rmtree`` for
#     directories; untracked dirs are vanishingly rare).
# A single summary comment is posted on the issue so the reviewer can
# see what was omitted.
#
# The guard is a no-op when the stored plan is missing or contains no
# ``### Files to change`` section — we refuse to build an empty scope
# set that would nuke every legitimate diff.
# ---------------------------------------------------------------------------

_STEP_HEADER_RE = re.compile(
    r"^####\s+Step\s+\d+\s+[\u2014\u2013\-]\s+(?:Edit|Write)\s+`([^`]+)`",
    re.MULTILINE,
)

# Strip ``/tmp/cai-<kind>-<N>-<uid>/`` prefix from plan-referenced
# absolute paths so they collate with relative ``git status`` paths.
# Anchored at start of string; only strips the first matching prefix.
_WORK_DIR_PREFIX_RE = re.compile(r"^/tmp/cai-[^/]+/")

# Paths always treated as in-scope regardless of plan contents.
# The PR-context dossier is written by ``cai-implement`` as part of its
# protocol (see ``cai-implement.md`` "Before you exit: write the PR
# context dossier") and must never be reverted by the scope guard.
_ALWAYS_IN_SCOPE: frozenset[str] = frozenset({
    ".cai/pr-context.md",
})


def _normalize_plan_path(path: str) -> str:
    """Return *path* as a relative clone-side path.

    Strips any leading ``/tmp/cai-<kind>-<N>-<uid>/`` work-directory
    prefix so a plan-phase absolute path such as
    ``/tmp/cai-plan-1065-a5338f84/cai_lib/foo.py`` normalises to
    ``cai_lib/foo.py``. Purely-relative paths pass through unchanged
    (with any leading slash stripped defensively).
    """
    stripped = _WORK_DIR_PREFIX_RE.sub("", path or "")
    return stripped.lstrip("/")


def _canonical_staging_aliases(rel: str) -> list[str]:
    """Return the live-path alias(es) a staging-dir plan entry
    authorises, or ``[]`` when *rel* does not point into
    ``.cai-staging/``.

    The wrapper's ``_apply_agent_edit_staging`` copies files under
    ``.cai-staging/agents/``, ``.cai-staging/agents-delete/``,
    ``.cai-staging/plugins/``, ``.cai-staging/claudemd/``, and
    ``.cai-staging/files-delete/`` to their live counterparts, so a
    plan listing the staging form must also authorise the live form
    (and vice versa, handled by the caller adding both to the scope
    set).
    """
    if rel.startswith(".cai-staging/agents/"):
        return [".claude/agents/" + rel[len(".cai-staging/agents/"):]]
    if rel.startswith(".cai-staging/agents-delete/"):
        return [".claude/agents/" + rel[len(".cai-staging/agents-delete/"):]]
    if rel.startswith(".cai-staging/plugins/"):
        return [".claude/plugins/" + rel[len(".cai-staging/plugins/"):]]
    if rel.startswith(".cai-staging/claudemd/"):
        return [rel[len(".cai-staging/claudemd/"):]]
    if rel.startswith(".cai-staging/files-delete/"):
        return [rel[len(".cai-staging/files-delete/"):]]
    return []


def _parse_plan_scope(plan_text: str) -> set[str]:
    r"""Return the set of relative clone-paths the plan declares in
    scope.

    Parses two sections:

      * ``### Files to change`` — backticked ``path/with.ext`` tokens
        via :data:`_FILES_TO_CHANGE_PATH_RE` (imported from
        ``cai_lib.actions.plan``).
      * ``#### Step N — Edit \`<path>\``` / ``#### Step N — Write
        \`<path>\``` — the backticked path in the step header via
        :data:`_STEP_HEADER_RE`.

    Each extracted path is normalised by :func:`_normalize_plan_path`
    and expanded with :func:`_canonical_staging_aliases` so the scope
    set accepts either the staging form or the live form. Always-in-
    scope entries from :data:`_ALWAYS_IN_SCOPE` are added
    unconditionally.

    Returns an empty set on empty/None input.
    """
    scope: set[str] = set(_ALWAYS_IN_SCOPE)
    if not plan_text:
        return scope

    section = _FILES_TO_CHANGE_SECTION_RE.search(plan_text)
    if section:
        for raw in _FILES_TO_CHANGE_PATH_RE.findall(section.group(1)):
            rel = _normalize_plan_path(raw)
            if not rel:
                continue
            scope.add(rel)
            for alias in _canonical_staging_aliases(rel):
                scope.add(alias)

    for m in _STEP_HEADER_RE.finditer(plan_text):
        rel = _normalize_plan_path(m.group(1))
        if not rel:
            continue
        scope.add(rel)
        for alias in _canonical_staging_aliases(rel):
            scope.add(alias)

    return scope


def _list_changed_paths(work_dir: Path) -> list[str]:
    """Return every path that differs from ``HEAD`` in *work_dir*.

    Combines two queries so both tracked changes (modifications,
    staged additions, deletions, renames) and untracked files are
    covered without having to parse ``git status --porcelain``
    status codes:

      * ``git diff --name-only HEAD`` — tracked mutations vs. HEAD.
      * ``git ls-files --others --exclude-standard`` — untracked
        files (respecting ``.gitignore``).

    Duplicates are removed while preserving first-seen order.
    Returns ``[]`` on any subprocess failure — the scope guard must
    fail open rather than blocking legitimate diffs.
    """
    out: list[str] = []
    seen: set[str] = set()
    for args in (
        ("diff", "--name-only", "HEAD"),
        ("ls-files", "--others", "--exclude-standard"),
    ):
        proc = _git(work_dir, *args, check=False)
        if proc.returncode != 0:
            continue
        for line in (proc.stdout or "").splitlines():
            rel = line.strip()
            if not rel or rel in seen:
                continue
            seen.add(rel)
            out.append(rel)
    return out


def _enforce_plan_scope(
    work_dir: Path, plan_text: str, issue_number: int,
) -> list[str]:
    """Revert files outside the plan's declared scope and return the
    reverted paths.

    Called immediately after :func:`_apply_agent_edit_staging` and
    before the main ``git status`` capture in
    :func:`handle_implement`. Returns ``[]`` (no-op) when:

      * *plan_text* is empty or ``None``;
      * the plan has no ``### Files to change`` section — we refuse
        to build a pathologically empty scope set;
      * every changed path is in scope.

    Otherwise, each out-of-scope path is reverted:

      * Tracked modifications / deletions → ``git checkout HEAD --
        <path>`` restores the indexed version.
      * Untracked additions → :meth:`Path.unlink` (or
        :func:`shutil.rmtree` for directory-shaped entries, which are
        rare — the agent's tools emit files, not directories).

    A summary comment is posted to the issue so the reviewer can see
    what was omitted and, if the omission was legitimate, either
    add the path to the plan or re-file the work as a separate issue.
    """
    if not plan_text:
        return []
    if not _FILES_TO_CHANGE_SECTION_RE.search(plan_text):
        return []

    scope = _parse_plan_scope(plan_text)
    changed = _list_changed_paths(work_dir)
    out_of_scope = [p for p in changed if p not in scope]
    if not out_of_scope:
        return []

    reverted: list[str] = []
    for rel in out_of_scope:
        abs_path = work_dir / rel
        checkout = _git(
            work_dir, "checkout", "HEAD", "--", rel, check=False,
        )
        if checkout.returncode == 0:
            reverted.append(rel)
            continue
        try:
            if abs_path.is_dir() and not abs_path.is_symlink():
                shutil.rmtree(abs_path, ignore_errors=True)
            else:
                abs_path.unlink(missing_ok=True)
            reverted.append(rel)
        except OSError:
            continue

    if reverted:
        bullet_list = "\n".join(f"- `{p}`" for p in reverted)
        comment_body = (
            "## Implement subagent: out-of-scope files omitted\n\n"
            "The implement agent wrote the following file(s) that "
            "were NOT listed in the stored plan's "
            "`### Files to change` section or any "
            "`#### Step N — Edit/Write` header:\n\n"
            f"{bullet_list}\n\n"
            "These paths were reverted before commit to keep the PR "
            "limited to the plan's declared scope. If any of them "
            "genuinely needed editing, re-plan the issue so the path "
            "is in scope, or raise a follow-up issue.\n\n"
            "---\n"
            "_Set by `cai implement` plan-scope enforcer (issue #1074)._"
        )
        _run(
            ["gh", "issue", "comment", str(issue_number),
             "--repo", REPO,
             "--body", comment_body],
            capture_output=True,
        )

    return reverted


# ---------------------------------------------------------------------------
# Handler.
# ---------------------------------------------------------------------------


def handle_implement(issue: dict) -> int:
    """Run the implement subagent against one eligible issue.

    The dispatcher supplies an issue whose state is either
    :class:`IssueState.PLAN_APPROVED` (fresh entry — we apply
    ``approved_to_in_progress`` first) or :class:`IssueState.IN_PROGRESS`
    (resume — we skip the entry transition). On normal success we apply
    ``in_progress_to_pr``; on pre-screen ``spike`` / subagent "Needs
    Spike" marker we divert to ``:human-needed``.
    """
    issue_number = issue["number"]
    title = issue["title"]
    label_names = [lbl["name"] for lbl in issue.get("labels", [])]
    state = get_issue_state(label_names)
    print(f"[cai implement] picked #{issue_number}: {title}", flush=True)

    # Hard plan gate — every fix run requires a stored plan.
    if _extract_stored_plan(issue.get("body", "")) is None:
        print(
            f"[cai implement] #{issue_number} has no stored plan; "
            f"demoting to {LABEL_REFINED}",
            flush=True,
        )
        _set_labels(
            issue_number,
            add=[LABEL_REFINED],
            remove=[LABEL_PLAN_APPROVED],
            log_prefix="cai implement",
        )
        log_run("implement", repo=REPO, issue=issue_number, result="no_stored_plan", exit=0)
        return 0

    # 1. :plan-approved → :in-progress entry is now fired by ``drive_issue``
    # before this handler runs (see ``cai_lib/dispatcher.py``). By the time
    # we get here the issue is always at :in-progress; any other state is a
    # label corruption we refuse to process.
    if state != IssueState.IN_PROGRESS:
        print(
            f"[cai implement] unexpected state {state} for #{issue_number}; skipping",
            file=sys.stderr,
            flush=True,
        )
        log_run("implement", repo=REPO, issue=issue_number, result="bad_state", exit=0)
        return 0

    # Make sure git can authenticate over HTTPS via the gh token. This
    # is also done in entrypoint.sh, but redoing it here is cheap and
    # idempotent and lets ad-hoc `docker run` invocations work too.
    _run(["gh", "auth", "setup-git"], capture_output=True)

    opus_escalation = LABEL_OPUS_ATTEMPTED in label_names

    # Note: the dedicated "N consecutive `tests_failed` runs" early-abort
    # guard was removed when the test-failure handler stopped rolling
    # back to :plan-approved (issues open a PR even on failing local
    # tests and route to cai-revise via the PR pipeline), so consecutive
    # tests_failed streaks no longer accumulate. The broader
    # :data:`_MAX_CONSECUTIVE_FAILED_ATTEMPTS` cap below still catches
    # loops across other failure kinds.

    # General consecutive-failure cap (issue #1088 / #1151). Fires for
    # both tiers as a broader safety net than the tests-only counter
    # above. If this issue has already burned through the *effective*
    # cap of implement failures of any kind (tests_failed,
    # subagent_failed, unexpected_error, and the git/gh transport
    # failures), park at :human-needed for admin review.
    #
    # The effective cap is
    # :data:`_MAX_CONSECUTIVE_FAILED_ATTEMPTS_EXTENDED` (5) when
    # ``opus_escalation`` is False AND the issue carries
    # ``LABEL_EXTENDED_RETRIES`` (stamped by ``handle_plan_gate`` on
    # plans meeting the #1151 structural thresholds — >=5 files AND
    # >=40 edit steps). Otherwise the cap is
    # :data:`_MAX_CONSECUTIVE_FAILED_ATTEMPTS` (3). The extension
    # gives Sonnet two extra attempts on transient infra / tooling
    # flakes for plans whose blast radius is large enough that a
    # flaky run is more likely over many edit sites, without
    # inviting further Opus retries (the rescue
    # ``_issue_has_opus_attempted`` guard refuses a second Opus
    # escalation anyway, so raising the Opus cap would just loop).
    #
    # At the Opus tier the LABEL_OPUS_ATTEMPTED label will naturally
    # prevent rescue from re-escalating (see cmd_rescue's
    # _issue_has_opus_attempted guard); at the Sonnet tier rescue may
    # still escalate to Opus, which is desirable (different model may
    # handle it).
    extended_retries = LABEL_EXTENDED_RETRIES in label_names
    if not opus_escalation and extended_retries:
        effective_cap = _MAX_CONSECUTIVE_FAILED_ATTEMPTS_EXTENDED
    else:
        effective_cap = _MAX_CONSECUTIVE_FAILED_ATTEMPTS
    consecutive_any = _count_consecutive_failed_attempts(issue_number)
    if consecutive_any >= effective_cap:
        tier = "Opus" if opus_escalation else "Sonnet"
        print(
            f"[cai implement] #{issue_number} has {consecutive_any} "
            f"consecutive failed implement attempts at the {tier} "
            f"tier (>= {effective_cap}); skipping "
            f"subagent and parking at auto-improve:human-needed",
            flush=True,
        )
        reason = (
            "## Implement subagent: retries exhausted\n\n"
            f"This issue has {consecutive_any} consecutive failed "
            f"implement attempts in the run log at the {tier} tier "
            "(counting `tests_failed`, `subagent_failed`, "
            "`unexpected_error`, and git/gh transport failures). "
            "Further retries would loop indefinitely without "
            "progress — pre-empting the subagent call and "
            "escalating to human review.\n\n"
            "---\n"
            f"_Pre-empted by `cai implement` retries-exhausted guard "
            f"after {effective_cap} consecutive "
            f"failures. Re-label to `{LABEL_PLAN_APPROVED}` once the "
            "underlying problem is resolved to retry. Issue #1088 / #1151._"
        )
        if not _park_in_progress_at_human_needed(
            issue_number, reason=reason,
        ):
            print(
                f"[cai implement] WARNING: label transition "
                f"to auto-improve:human-needed failed twice "
                f"for #{issue_number} — issue may be stuck",
                file=sys.stderr, flush=True,
            )
            log_run("implement", repo=REPO,
                    issue=issue_number,
                    result="label_transition_failed", exit=1)
            return 1
        log_run("implement", repo=REPO, issue=issue_number,
                result="retries_exhausted", exit=0)
        return 0

    # Pre-screen: cheap Haiku call to triage obvious non-actionable issues
    # before the expensive clone + plan-select pipeline.
    ps_verdict, ps_reason = _pre_screen_issue_actionability(issue)
    print(f"[cai implement] pre-screen: verdict={ps_verdict} reason={ps_reason}", flush=True)

    if ps_verdict == "spike":
        reason = (
            f"## Pre-screen: spike-shaped\n\n"
            f"{ps_reason}\n\n---\n"
            f"_Flagged by `cai implement` pre-screen (Haiku) as "
            f"spike-shaped (needs research, not code). No spike agent "
            f"exists — routed to human review. Re-label to "
            f"`{LABEL_PLAN_APPROVED}` to retry._"
        )
        _park_in_progress_at_human_needed(issue_number, reason=reason)
        log_run("implement", repo=REPO, issue=issue_number, result="pre_screen_human", exit=0)
        return 0

    if ps_verdict == "ambiguous":
        # Bounce to :refined (not :plan-approved) so the issue re-flows
        # through refine → plan → confidence-gated approval. Returning to
        # :plan-approved would make _select_fix_target pick it up again
        # immediately and loop on the same pre-screen verdict.
        _set_labels(
            issue_number,
            add=[LABEL_REFINED],
            remove=[LABEL_IN_PROGRESS],
        )
        _run(
            ["gh", "issue", "comment", str(issue_number),
             "--repo", REPO,
             "--body",
             f"## Pre-screen: ambiguous issue\n\n"
             f"{ps_reason}\n\n---\n"
             f"_Flagged by `cai implement` pre-screen (Haiku). The issue "
             f"was returned to `{LABEL_REFINED}` for refinement._"],
            capture_output=True,
        )
        log_run("implement", repo=REPO, issue=issue_number, result="pre_screen_ambiguous", exit=0)
        return 0

    _uid = uuid.uuid4().hex[:8]
    work_dir = Path(f"/tmp/cai-implement-{issue_number}-{_uid}")
    locked = True

    def rollback() -> None:
        nonlocal locked
        if not locked:
            return
        _set_labels(
            issue_number,
            add=[LABEL_PLAN_APPROVED],
            remove=[LABEL_IN_PROGRESS],
        )
        locked = False

    try:
        if work_dir.exists():
            shutil.rmtree(work_dir)

        # 2. Clone.
        clone = _run(
            ["git", "clone", "--depth", "1", f"https://github.com/{REPO}.git", str(work_dir)],
            capture_output=True,
        )
        if clone.returncode != 0:
            print(f"[cai implement] git clone failed:\n{clone.stderr}", file=sys.stderr)
            rollback()
            log_run("implement", repo=REPO, issue=issue_number, result="clone_failed", exit=1)
            return 1

        # 3. Configure git identity from the gh token's owner.
        name, email = _gh_user_identity()
        _git(work_dir, "config", "user.name", name)
        _git(work_dir, "config", "user.email", email)
        print(f"[cai implement] git identity: {name} <{email}>", flush=True)

        # 4. Branch — reuse an existing auto-improve/<N>-* branch on origin
        #    if present (resume), otherwise cut a fresh one off main.
        existing = _find_existing_branch(issue_number)
        if existing:
            print(
                f"[cai implement] reusing existing branch {existing} for #{issue_number}",
                flush=True,
            )
            fetch = _run(
                ["git", "-C", str(work_dir), "fetch", "origin",
                 f"{existing}:{existing}"],
                capture_output=True,
            )
            if fetch.returncode != 0:
                print(
                    f"[cai implement] fetch of existing branch failed:\n{fetch.stderr}",
                    file=sys.stderr,
                )
                rollback()
                log_run("implement", repo=REPO, issue=issue_number,
                        result="fetch_existing_failed", exit=1)
                return 1
            _git(work_dir, "checkout", existing)
            branch = existing
        else:
            branch = f"auto-improve/{issue_number}-{_slugify(title)}"
            _git(work_dir, "checkout", "-b", branch)

        # 4b. Fetch previous fix attempts (closed, unmerged PRs) and
        #     build a history block so the fix agent doesn't repeat
        #     rejected approaches.
        attempts = _fetch_previous_fix_attempts(issue_number)
        attempt_history_block = _build_attempt_history_block(attempts)
        if attempt_history_block:
            print(
                f"[cai implement] injecting {len(attempts)} previous fix attempt(s) for #{issue_number}",
                flush=True,
            )

        # 4c. Retrieve the pre-computed plan from the issue body.
        #     `cai plan` stored the plan; it was auto-approved on HIGH
        #     confidence, or an admin resumed it via cai-unblock. Either
        #     way it now carries `:plan-approved`.
        selected_plan = _get_plan_for_fix(issue)

        # 4d. Pre-create the `.cai-staging/agents/` directory so the
        #     agent has somewhere to write proposed updates to its
        #     own `.claude/agents/*.md` file(s). Claude-code's
        #     headless `-p` mode hardcodes a protection on every
        #     `.claude/agents/*.md` path that no permission flag
        #     bypasses, so we route self-modifications through a
        #     non-protected scratch path and copy them back after
        #     the agent exits successfully. See
        #     `_setup_agent_edit_staging` / `_apply_agent_edit_staging`.
        _setup_agent_edit_staging(work_dir)

        # 5. Run the cai-implement declarative subagent.
        user_message = (
            _work_directory_block(work_dir, issue.get("body") or "")
            + "\n"
            + _build_issue_block(issue) + attempt_history_block
        )
        if selected_plan:
            user_message = (
                _work_directory_block(work_dir, issue.get("body") or "")
                + "\n"
                + "## Selected Implementation Plan\n\n"
                + "The following plan was pre-computed by `cai plan` and "
                + "approved by a human reviewer. "
                + "Follow this plan to implement the fix.\n\n"
                + f"{selected_plan}\n\n"
                + "---\n\n"
                + _build_issue_block(issue) + attempt_history_block
            )
        claude_cmd = ["claude", "-p", "--agent", "cai-implement"]
        if opus_escalation:
            claude_cmd += ["--model", _OPUS_MODEL_ID]
            print(
                f"[cai implement] #{issue_number} carries "
                f"{LABEL_OPUS_ATTEMPTED}; invoking cai-implement with "
                f"--model {_OPUS_MODEL_ID}",
                flush=True,
            )
        else:
            # Sonnet-tier caps: 60-turn (issue #934 / PR #1003) +
            # $2.50 per-invocation cost ceiling (issue #1107). The
            # budget cap catches runaway invocations that stay under
            # 60 turns but rack up cost via large inputs or cache
            # churn. On exhaustion, claude-agent-sdk returns
            # ResultMessage with subtype="error_max_budget_usd" and
            # is_error=True; _run_claude_p surfaces that as
            # returncode=1 with the last-assistant salvage text,
            # which flows through the existing `subagent_failed`
            # rollback path below. Three consecutive failures (any
            # kind) then trip _MAX_CONSECUTIVE_FAILED_ATTEMPTS and
            # park the issue at :human-needed with
            # result=retries_exhausted. Opus one-shot runs are
            # deliberately uncapped (shared memory:
            # implement-sonnet-turn-cap.md).
            claude_cmd += ["--max-turns", "60",
                           "--max-budget-usd", "2.50"]
        claude_cmd += ["--dangerously-skip-permissions",
                       "--add-dir", str(work_dir)]
        # Issue #1206: stamp the plan's declared file scope onto the
        # cost-log row so cai-audit-cost-reduction and cai-cost-optimize
        # can group implement spend by declared scope. Parsed from the
        # issue body's ``### Files to change`` section; an empty list
        # (section absent or contains no paths) is converted to None so
        # the key is omitted, preserving pre-change row shape.
        scope_files = _parse_files_to_change(issue.get("body") or "")
        print(f"[cai implement] running cai-implement subagent for {work_dir}", flush=True)
        _plan_scope_files: list[str] | None = None
        if selected_plan:
            _sec = _FILES_TO_CHANGE_SECTION_RE.search(selected_plan)
            if _sec:
                _plan_scope_files = _FILES_TO_CHANGE_PATH_RE.findall(_sec.group(1))
        agent = _run_claude_p(
            claude_cmd,
            category="implement",
            agent="cai-implement",
            input=user_message,
            cwd="/app",
            target_kind="issue",
            target_number=issue_number,
            scope_files=_plan_scope_files or scope_files or None,
            fingerprint_payload=user_message,
            fix_attempt_count=len(attempts),
        )
        if agent.stdout:
            print(agent.stdout, flush=True)
        if agent.returncode != 0:
            print(
                f"[cai implement] subagent claude -p failed (exit {agent.returncode}):\n"
                f"{agent.stderr}",
                file=sys.stderr,
            )
            rollback()
            log_run(
                "implement",
                repo=REPO,
                issue=issue_number,
                result="subagent_failed",
                stderr_tail=_format_stderr_tail(agent.stderr or ""),
                exit=agent.returncode,
            )
            return agent.returncode

        # 5b. Create any suggested issues the subagent raised.
        agent_text = agent.stdout or ""
        suggested = _parse_suggested_issues(agent_text)
        if suggested:
            n = _create_suggested_issues(suggested, issue_number)
            print(f"[cai implement] created {n}/{len(suggested)} suggested issue(s)", flush=True)

        # 5c. Apply any `.claude/agents/**/*.md` updates the agent staged.
        applied = _apply_agent_edit_staging(work_dir)
        if applied:
            print(
                f"[cai implement] applied {applied} staged "
                f".claude/agents/**/*.md update(s)",
                flush=True,
            )

        # 5d. Plan-scope gate (issue #1074) — revert any files the
        #     agent wrote that are not listed in the stored plan's
        #     declared scope (`### Files to change` + `#### Step N —
        #     Edit/Write` headers). Prevents out-of-scope files from
        #     causing wasted test runs (#1065 wrote an unrelated test
        #     that referenced real git operations, triggering two
        #     consecutive `tests_failed` diverts).
        if selected_plan:
            reverted = _enforce_plan_scope(
                work_dir, selected_plan, issue_number,
            )
            if reverted:
                print(
                    f"[cai implement] scope gate reverted "
                    f"{len(reverted)} out-of-scope file(s): "
                    f"{', '.join(reverted)}",
                    flush=True,
                )

        # 6. Inspect the working tree. Empty diff = deliberate
        #    no-action OR a spike-shaped bail-out.
        status = _git(work_dir, "status", "--porcelain", check=False)
        if not status.stdout.strip():
            agent_text = agent.stdout or ""
            reasoning = agent_text.strip()[:2000]

            # Detect the spike marker. The cai-implement agent emits a
            # `## Needs Spike` block when bailing on a spike-shaped
            # issue. No spike agent exists, so route to :human-needed.
            is_spike = re.search(
                r"^##\s*Needs Spike\b",
                agent_text,
                flags=re.MULTILINE,
            ) is not None

            if is_spike:
                reason = (
                    "## Implement subagent: needs human review\n\n"
                    f"{reasoning}\n\n"
                    "---\n"
                    "_Set by `cai implement` after the subagent identified "
                    "this issue as needing research / verification / "
                    "evaluation rather than a code change. No spike agent "
                    "exists — a human must decide how to proceed. Re-label "
                    f"to `{LABEL_PLAN_APPROVED}` to retry as a routine "
                    "fix instead._"
                )
                print(
                    f"[cai implement] subagent produced no changes for "
                    f"#{issue_number}; marking auto-improve:human-needed",
                    flush=True,
                )
                if not _park_in_progress_at_human_needed(
                    issue_number,
                    reason=reason,
                    extra_remove=(LABEL_PLAN_APPROVED,),
                ):
                    print(
                        f"[cai implement] WARNING: label transition to "
                        f"auto-improve:human-needed failed twice for "
                        f"#{issue_number} — issue may be stuck without "
                        "a lifecycle label",
                        file=sys.stderr, flush=True,
                    )
                    rollback()
                    log_run("implement", repo=REPO, issue=issue_number,
                            result="label_transition_failed", exit=1)
                    return 1
                locked = False
                log_run("implement", repo=REPO, issue=issue_number,
                        result="human_needed", exit=0)
                return 0
            else:
                reasoning_msg = (
                    "## Implement subagent: no action needed\n\n"
                    f"{reasoning}\n\n"
                    "---\n"
                    "_Closed as **not planned** — the implement subagent reviewed "
                    "and decided no code change was needed. Re-open and re-label "
                    f"to `{LABEL_PLAN_APPROVED}` to retry._"
                )
                print(
                    f"[cai implement] subagent produced no changes for "
                    f"#{issue_number}; closing as not planned",
                    flush=True,
                )
                if not close_issue_not_planned(
                    issue_number, reasoning_msg, log_prefix="cai implement"
                ):
                    rollback()
                    log_run("implement", repo=REPO, issue=issue_number,
                            result="close_failed", exit=1)
                    return 1
                # Strip in-progress labels even after close (allowed on closed issues).
                _set_labels(
                    issue_number,
                    remove=[LABEL_IN_PROGRESS, LABEL_PLAN_APPROVED],
                )
                locked = False
                log_run("implement", repo=REPO, issue=issue_number,
                        result="dismissed_resolved", exit=0)
                return 0

        # Count changed files for the log line.
        diff_files = len(status.stdout.strip().splitlines())

        # 7. Commit.
        _git(work_dir, "add", "-A")
        commit_msg = (
            f"auto-improve: {title}\n\n"
            f"Generated by `cai implement` against issue #{issue_number}.\n\n"
            f"Refs {REPO}#{issue_number}"
        )
        _git(work_dir, "commit", "-m", commit_msg)

        # 7b. Run regression tests against the clone's working tree for
        # observability. A local failure is no longer terminal: cai-implement
        # already invokes cai-test-runner in-session and may have iterated
        # on failures, and the PR pipeline has cai-revise as a safety net
        # for any residual breakage. So we push the PR anyway and route
        # it to REVISION_PENDING with a failure-summary comment, letting
        # cai-revise fix the tests on the PR side instead of rolling the
        # issue back to :plan-approved for another full implement cycle.
        test_result = _run(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
            cwd=str(work_dir),
            capture_output=True,
        )
        tests_failed_locally = test_result.returncode != 0
        failure_summary = ""
        helpers_section = ""
        if tests_failed_locally:
            failure_output = (
                f"{test_result.stdout or ''}\n"
                f"{test_result.stderr or ''}"
            ).strip()
            failure_summary = _extract_test_failures(failure_output)
            helpers_block = _extract_referenced_helpers(
                failure_output, work_dir,
            )
            if helpers_block:
                helpers_section = (
                    "### Helper implementations referenced by failures\n\n"
                    f"{helpers_block}\n\n"
                )
            print(
                f"[cai implement] regression tests failed — pushing PR "
                f"anyway; cai-revise will address the failures\n"
                f"{failure_summary}",
                file=sys.stderr,
            )

        # 8. Push.
        push = _run(
            ["git", "-C", str(work_dir), "push",
             "--force", "-u", "origin", branch],
            capture_output=True,
        )
        if push.returncode != 0:
            print(f"[cai implement] git push failed:\n{push.stderr}", file=sys.stderr)
            rollback()
            log_run("implement", repo=REPO, issue=issue_number,
                    result="push_failed", exit=1)
            return 1

        # 9. Open the PR.
        agent_output = (agent.stdout or "").strip()
        pr_summary = ""
        _marker = "## PR Summary"
        if _marker in agent_output:
            pr_summary = agent_output[agent_output.index(_marker):]
            pr_summary = re.split(
                r"^## Suggested Issue\s*$", pr_summary, flags=re.MULTILINE,
            )[0].rstrip()
            for fence in ("```", "~~~"):
                if pr_summary.rstrip().endswith(fence):
                    pr_summary = pr_summary.rstrip()[: -len(fence)].rstrip()
        if not pr_summary.strip():
            pr_summary = (
                f"## PR Summary\n\n"
                f"{agent_output[:4000]}"
            )
        pr_body = (
            f"Refs {REPO}#{issue_number}\n\n"
            f"**Issue:** #{issue_number} — {title}\n\n"
            f"{pr_summary}\n\n"
            f"---\n"
            f"_Auto-generated by `cai implement`. The implement subagent runs autonomously "
            f"with full tool permissions — please review the diff carefully._\n"
        )
        pr = _run(
            [
                "gh", "pr", "create",
                "--repo", REPO,
                "--title", f"auto-improve: {title}",
                "--body", pr_body,
                "--head", branch,
                "--base", "main",
            ],
            capture_output=True,
        )
        if pr.returncode != 0:
            print(f"[cai implement] gh pr create failed:\n{pr.stderr}", file=sys.stderr)
            rollback()
            log_run("implement", repo=REPO, issue=issue_number,
                    result="pr_create_failed", exit=1)
            return 1

        pr_url = pr.stdout.strip()
        print(f"[cai implement] opened PR: {pr_url}", flush=True)

        pr_number = pr_url.rstrip("/").rsplit("/", 1)[-1]

        # 10. Transition label :in-progress -> :pr-open via the FSM.
        if not fire_trigger(
            issue_number, "in_progress_to_pr",
            log_prefix="cai implement",
        )[0]:
            print(
                f"[cai implement] label transition to :pr-open failed for #{issue_number}; retrying",
                flush=True,
            )
            if not fire_trigger(
                issue_number, "in_progress_to_pr",
                log_prefix="cai implement",
            )[0]:
                print(
                    f"[cai implement] WARNING: label transition to :pr-open failed twice for "
                    f"#{issue_number} — issue may be orphaned from PR {pr_url}",
                    file=sys.stderr, flush=True,
                )
        locked = False

        # If the local regression run failed, post a top-level PR
        # comment with the failure summary and route the PR to
        # REVISION_PENDING so cai-revise picks up the failures as an
        # unaddressed reviewer finding. This replaces the former
        # rollback-to-:plan-approved path: instead of throwing away a
        # PR's worth of work and re-running the whole implement cycle,
        # we let cai-revise patch the tests on the PR side.
        if tests_failed_locally:
            comment_body = (
                "## Local regression tests failed\n\n"
                "The implement subagent opened this PR even though the "
                "local `python -m unittest` run failed after commit. "
                "The failures below are posted here as a reviewer "
                "finding so `cai-revise` picks them up and addresses "
                "them on the PR side rather than rolling the issue "
                "back through another full implement cycle.\n\n"
                "### Failing tests\n\n"
                f"```\n{failure_summary}\n```\n\n"
                f"{helpers_section}"
                "---\n"
                "_Posted by `cai implement`. Addressing this comment "
                "clears the finding; the PR continues through review → "
                "merge as usual._"
            )
            _run(
                ["gh", "pr", "comment", pr_number,
                 "--repo", REPO, "--body", comment_body],
                capture_output=True,
            )
            # Walk the PR FSM: OPEN → REVIEWING_CODE → REVISION_PENDING
            # so cai-revise becomes the selector for this PR.
            fire_trigger(
                int(pr_number), "open_to_reviewing_code",
                is_pr=True, log_prefix="cai implement",
            )
            fire_trigger(
                int(pr_number), "reviewing_code_to_revision_pending",
                is_pr=True, log_prefix="cai implement",
            )
            log_run("implement", repo=REPO, issue=issue_number, branch=branch,
                    pr=pr_number, diff_files=diff_files,
                    result="tests_failed_pushed", exit=0)
            return 0

        log_run("implement", repo=REPO, issue=issue_number, branch=branch,
                pr=pr_number, diff_files=diff_files, exit=0)
        return 0

    except Exception as e:
        print(f"[cai implement] unexpected failure: {e!r}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        rollback()
        log_run("implement", repo=REPO, issue=issue_number,
                result="unexpected_error", exit=1)
        return 1
    finally:
        if work_dir.exists():
            shutil.rmtree(work_dir, ignore_errors=True)
