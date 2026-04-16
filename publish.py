#!/usr/bin/env python3
"""
Publish analyzer findings as GitHub issues via the `gh` CLI.

Reads the analyzer's stdout from this script's own stdin, parses
`### Finding:` markdown blocks produced by `.claude/agents/cai-analyze.md`,
and creates one issue per finding in the `damien-robotsix/robotsix-cai`
repository. Existing findings are deduped by a fingerprint HTML comment
embedded in the issue body (`<!-- fingerprint: <key> -->`).

Phase C.2 scope — this is the Lane 1 publish step. Lane 2 (workspace
targets) is still deferred.

Non-empty stdin that produces zero parsed ``### Finding:`` blocks exits 1
with a stderr diagnostic (including the first ~500 chars of input); empty
stdin still exits 0.

No third-party Python dependencies — only stdlib plus the `gh` CLI.

Usage::

    cat analyzer-output.md | python publish.py

    # Or from cai.py, piped directly:
    # subprocess.run(["python", "/app/publish.py"], input=analyzer_stdout, ...)
"""

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass


# Lane 1 target — the backend improves itself. When Lane 2 lands, this
# will be parameterized per workspace.
REPO = "damien-robotsix/robotsix-cai"

# The set of categories declared in .claude/agents/cai-analyze.md. Any
# finding whose category is outside this set is rejected before we touch
# GitHub — the analyzer is instructed not to invent new ones.
VALID_CATEGORIES = {
    "reliability",
    "cost_reduction",
    "prompt_quality",
    "workflow_efficiency",
}

AUDIT_CATEGORIES = {
    "stale_lifecycle",
    "lock_corruption",
    "loop_stuck",
    "prompt_contradiction",
    "topic_duplicate",
    "silent_failure",
    "forgotten_backlog",
    "cost_outlier",
    "workflow_anomaly",
    "fix_loop_efficiency",
}

CODE_AUDIT_CATEGORIES = {
    "cross_file_inconsistency",
    "dead_code",
    "missing_reference",
    "duplicated_logic",
    "hardcoded_drift",
    "config_mismatch",
    "registration_mismatch",
}

UPDATE_CHECK_CATEGORIES = {
    "version_update",
    "feature_adoption",
    "deprecation",
    "best_practice",
}

CHECK_WORKFLOWS_CATEGORIES = {
    "workflow_failure",
    "workflow_flake",
    "workflow_config_error",
}

AGENT_AUDIT_CATEGORIES = {
    "best_practice_violation",
    "unused_agent",
    "redundant_agents",
}

EXTERNAL_SCOUT_CATEGORIES = {
    "external_solution",
}

# Labels we ensure exist before creating issues. These include FSM/lifecycle
# state labels (auto-improve:*), PR state labels (pr:*), and kind labels (kind:*).
# Category information is now stored in the issue body, not as labels. Idempotent —
# `gh label create` returns non-zero if the label already exists, which we ignore.
LABELS = [
    ("auto-improve", "ededed", "Self-improvement finding raised by the analyzer"),
    ("auto-improve:raised", "0e8a16", "Awaiting structured refinement before implement subagent picks it up"),
    ("auto-improve:in-progress", "fbca04", "implement subagent is actively working on this issue"),
    ("auto-improve:pr-open", "5319e7", "implement subagent opened a PR"),
    ("auto-improve:merged", "0e8a16", "PR was merged; awaiting verify"),
    ("auto-improve:needs-exploration", "c2e0c6", "Issue needs autonomous exploration/benchmarking (handled by cai-explore)"),
    ("auto-improve:triaging",    "fbca04", "cai-triage is actively classifying this issue (transient)"),
    ("auto-improve:refining",    "fbca04", "cai-refine is actively running (transient)"),
    ("kind:code",                "0075ca", "Triage: issue requires a code change"),
    ("kind:maintenance",         "e4e669", "Triage: issue is a maintenance/ops task"),
    ("auto-improve:refined", "0e8a16", "Issue has been reviewed/refined and is ready for the implement subagent"),
    ("auto-improve:planning", "fbca04", "cai-plan is actively running (transient)"),
    ("auto-improve:revising", "d4c5f9", "Revise subagent is actively iterating on a PR"),
    ("auto-improve:solved", "0e8a16", "Pattern verified absent from recent transcripts"),
    ("auto-improve:planned", "e4e669", "Plan generated and stored in issue body; awaiting human approval"),
    ("auto-improve:plan-approved", "0e8a16", "Plan approved (auto via high confidence, or human resume); ready for implement subagent"),
    ("auto-improve:applying", "fbca04", "Maintenance ops actively being applied (transient)"),
    ("auto-improve:applied",  "0e8a16", "Maintenance ops applied; awaiting verification"),
    ("auto-improve:parent", "c5def5", "Parent issue with sub-issues"),
    ("auto-improve:human-needed", "e11d48", "Issue parked awaiting admin comment (cai-unblock resume)"),
    ("auto-improve:pr-human-needed", "e11d48", "PR parked awaiting admin comment (cai-unblock resume)"),
    ("merge-blocked", "e11d48", "Merge subcommand reviewed and decided not to auto-merge; awaiting human"),
    ("needs-human-review", "e11d48", "PR needs a human decision before merge"),
    ("pr:reviewing-code",   "e4e669", "PR is in code review (cai-review-pr)"),
    ("pr:revision-pending", "d93f0b", "Code review posted findings; revise needed"),
    ("pr:reviewing-docs",   "0075ca", "Code clean; in docs review (cai-review-docs)"),
    ("pr:approved",         "0e8a16", "Docs reviewed clean; ready for merge handler"),
    ("pr:rebasing",         "fbca04", "PR has merge conflicts with main; cai-rebase will attempt a rebase"),
    ("pr:ci-failing",       "e11d48", "CI is red; cai-fix-ci will attempt a repair"),
]

# Labels that existed in an earlier design but are no longer active.
# Deleted idempotently on each publish run (gh label delete exits non-zero
# when the label is absent, so check=False is required).
LABELS_TO_DELETE = [
    "human:requested",                # removed — auto-improve:raised is the sole human entry point
    "auto-improve:merge-blocked",     # stale — superseded by merge-blocked
    "auto-improve:needs-refinement",  # stale — superseded by the refine agent deciding on exploration
    "auto-improve:in-pr",             # dead — FSM drift with auto-improve:pr-open; aligned on :pr-open
    # Legacy PR pipeline labels — replaced by first-class PRState labels
    # (pr:reviewing-code / pr:revision-pending / pr:reviewing-docs /
    # pr:ci-failing). Migration runs on cmd_cycle entry.
    "pr:edited",
    "pr:reviewed-reject",
    "pr:reviewed-accept",
    "pr:documented",
    # Retired audit-specific state labels — unified into auto-improve:raised + audit source tag.
    "audit:raised",
    "audit:needs-human",
    "audit:solved",
    # Retired check-workflows state label — unified into auto-improve:raised + check-workflows source tag.
    # Migration: _migrate_check_workflows_raised in cai_lib/watchdog.py relabels existing issues.
    "check-workflows:raised",
    "auto-improve:no-action",     # retired — replaced by gh issue close --reason "not planned"
    # Retired informational category labels — category is parsed from the issue body (**Category:** `...`) instead.
    "category:reliability",
    "category:cost_reduction",
    "category:prompt_quality",
    "category:workflow_efficiency",
    "category:stale_lifecycle",
    "category:lock_corruption",
    "category:loop_stuck",
    "category:prompt_contradiction",
    "category:topic_duplicate",
    "category:silent_failure",
    "category:forgotten_backlog",
    "category:cost_outlier",
    "category:workflow_anomaly",
    "category:fix_loop_efficiency",
    "category:cross_file_inconsistency",
    "category:dead_code",
    "category:missing_reference",
    "category:duplicated_logic",
    "category:hardcoded_drift",
    "category:config_mismatch",
    "category:registration_mismatch",
    "category:version_update",
    "category:feature_adoption",
    "category:deprecation",
    "category:best_practice",
    "category:workflow_failure",
    "category:workflow_flake",
    "category:workflow_config_error",
]

AUDIT_LABELS = [
    ("audit", "c5def5", "Queue/PR consistency audit finding (source tag)"),
]

CODE_AUDIT_LABELS = [
    ("auto-improve", "ededed", "Self-improvement finding raised by the analyzer"),
    ("auto-improve:raised", "0e8a16", "Awaiting structured refinement before implement subagent picks it up"),
]

UPDATE_CHECK_LABELS = [
    ("auto-improve", "ededed", "Self-improvement finding raised by the analyzer"),
    ("auto-improve:raised", "0e8a16", "Awaiting structured refinement before implement subagent picks it up"),
]

CHECK_WORKFLOWS_LABELS = [
    ("auto-improve", "ededed", "Self-improvement finding raised by the analyzer"),
    ("auto-improve:raised", "0e8a16", "Awaiting structured refinement before implement subagent picks it up"),
    ("check-workflows", "e11d48", "GitHub Actions workflow failure finding (source tag)"),
]

AGENT_AUDIT_LABELS = [
    ("auto-improve", "ededed", "Self-improvement finding raised by the analyzer"),
    ("auto-improve:raised", "0e8a16", "Awaiting structured refinement before implement subagent picks it up"),
]

EXTERNAL_SCOUT_LABELS = [
    ("auto-improve", "ededed", "Self-improvement finding raised by the analyzer"),
    ("auto-improve:raised", "0e8a16", "Awaiting structured refinement before implement subagent picks it up"),
]


@dataclass
class Finding:
    title: str
    category: str
    key: str
    confidence: str
    evidence: str
    remediation: str


def parse_findings(text: str, valid_categories: set[str] | None = None) -> list[Finding]:
    """Split analyzer output into Finding blocks.

    The prompt format is::

        ### Finding: <title>

        - **Category:** <category>
        - **Key:** <key>
        - **Confidence:** <low|medium|high>
        - **Evidence:**
          - <line>
          - <line>
        - **Remediation:** <remediation>

    Parsing is deliberately lenient: we split on `### Finding:` headers
    and then pull fields with regexes. Unknown fields are ignored;
    missing required fields cause the block to be skipped.
    """
    if valid_categories is None:
        valid_categories = VALID_CATEGORIES
    findings: list[Finding] = []

    # Split on the Finding header, keeping the header text itself.
    blocks = re.split(r"^### Finding:\s*", text, flags=re.MULTILINE)
    # blocks[0] is everything before the first header (preamble); skip it.
    for block in blocks[1:]:
        lines = block.splitlines()
        if not lines:
            continue
        title = lines[0].strip()
        body = "\n".join(lines[1:])

        category = _extract_field(body, "Category")
        key = _extract_field(body, "Key")
        confidence = _extract_field(body, "Confidence")
        evidence = _extract_multiline_field(body, "Evidence")
        remediation = _extract_multiline_field(body, "Remediation")

        if not (title and category and key):
            # Incomplete block — skip rather than post garbage.
            print(
                f"[publish] skipping incomplete finding (title={title!r})",
                file=sys.stderr,
            )
            continue

        if category not in valid_categories:
            print(
                f"[publish] skipping finding with invalid category {category!r}",
                file=sys.stderr,
            )
            continue

        findings.append(
            Finding(
                title=title,
                category=category,
                key=key,
                confidence=confidence or "unspecified",
                evidence=evidence or "(no evidence provided)",
                remediation=remediation or "(no remediation provided)",
            )
        )

    return findings


def load_findings_json(path: str, valid_categories: set[str]) -> list[Finding]:
    """Load and validate a JSON findings file.

    Schema:
        {"findings": [{"title", "category", "key",
                       "confidence", "evidence", "remediation"}, ...]}

    Validation rules (intentionally stricter than parse_findings for structured
    JSON input; parse_findings is lenient because it parses free-form markdown):
      * Malformed JSON or missing top-level ``findings`` list -> sys.exit(1).
      * Required fields (title, category, key) missing -> per-finding
        stderr error, that entry skipped (other entries keep going).
      * category outside ``valid_categories`` -> per-finding stderr error, skipped.
      * confidence not in {"low","medium","high"} -> warn and default to
        "unspecified" (JSON is structured so we validate the field; markdown is
        free-form so parse_findings accepts any confidence string as-is).
      * evidence / remediation missing -> default strings
        ("(no evidence provided)" / "(no remediation provided)").
    """
    try:
        with open(path) as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[publish] ERROR: could not load findings file {path!r}: {exc}", file=sys.stderr)
        sys.exit(1)

    if not isinstance(data, dict) or not isinstance(data.get("findings"), list):
        print(
            f"[publish] ERROR: {path!r} must be a JSON object with a top-level "
            f'"findings" list',
            file=sys.stderr,
        )
        sys.exit(1)

    findings: list[Finding] = []
    for idx, entry in enumerate(data["findings"]):
        if not isinstance(entry, dict):
            print(f"[publish] findings[{idx}]: not a dict — skipped", file=sys.stderr)
            continue

        skip = False
        for field in ("title", "category", "key"):
            if not entry.get(field):
                print(
                    f"[publish] findings[{idx}]: missing required field {field!r} — skipped",
                    file=sys.stderr,
                )
                skip = True
                break
        if skip:
            continue

        title = entry["title"]
        category = entry["category"]
        key = entry["key"]

        if category not in valid_categories:
            print(
                f"[publish] findings[{idx}]: invalid category {category!r} — skipped",
                file=sys.stderr,
            )
            continue

        confidence = entry.get("confidence", "")
        if confidence not in {"low", "medium", "high"}:
            print(
                f"[publish] findings[{idx}]: confidence {confidence!r} not in "
                f"{{low,medium,high}} — defaulting to 'unspecified'",
                file=sys.stderr,
            )
            confidence = "unspecified"

        evidence = entry.get("evidence") or "(no evidence provided)"
        remediation = entry.get("remediation") or "(no remediation provided)"

        findings.append(
            Finding(
                title=title,
                category=category,
                key=key,
                confidence=confidence,
                evidence=evidence,
                remediation=remediation,
            )
        )

    return findings


def _extract_field(block: str, name: str) -> str:
    """Pull a single-line `- **Name:** value` field out of a block.

    Strips surrounding whitespace and backticks from the value — the
    model sometimes wraps short identifier-like values (categories,
    keys, confidence levels) in backticks for code formatting, which
    would otherwise break exact-string validation against the
    `VALID_CATEGORIES` / `AUDIT_CATEGORIES` sets.
    """
    match = re.search(
        rf"^- \*\*{re.escape(name)}:\*\*\s*(.+)$",
        block,
        flags=re.MULTILINE,
    )
    if not match:
        return ""
    return match.group(1).strip().strip("`").strip()


def _extract_multiline_field(block: str, name: str) -> str:
    """Pull a multi-line `- **Name:**` field (value may span lines).

    The value is terminated by any of:
      * the next top-level bullet field (`- **Next:**`)
      * a blank line (paragraph break — trailing narrative that is not
        part of the finding)
      * end of block
    """
    pattern = (
        rf"^- \*\*{re.escape(name)}:\*\*\s*(.*?)"
        r"(?=\n\n|^- \*\*|\Z)"
    )
    match = re.search(pattern, block, flags=re.MULTILINE | re.DOTALL)
    if not match:
        return ""
    return match.group(1).strip()


def _label_set_for(namespace: str):
    """Return the label set for the given namespace."""
    if namespace == "audit":
        return AUDIT_LABELS
    if namespace == "code-audit":
        return CODE_AUDIT_LABELS
    if namespace == "update-check":
        return UPDATE_CHECK_LABELS
    if namespace == "check-workflows":
        return CHECK_WORKFLOWS_LABELS
    if namespace == "agent-audit":
        return AGENT_AUDIT_LABELS
    if namespace == "external-scout":
        return EXTERNAL_SCOUT_LABELS
    return LABELS


def ensure_labels(namespace: str = "auto-improve") -> None:
    """Create the cai label set if it doesn't exist. Idempotent."""
    label_set = _label_set_for(namespace)
    for name, color, description in label_set:
        subprocess.run(
            [
                "gh", "label", "create", name,
                "--color", color,
                "--description", description,
                "--repo", REPO,
            ],
            check=False,
            capture_output=True,
        )


def ensure_all_labels() -> None:
    """Create labels for ALL namespaces. Idempotent.

    Deduplicates labels that appear in multiple sets (e.g.
    auto-improve and auto-improve:raised appear in both LABELS
    and CODE_AUDIT_LABELS).
    """
    seen: set[str] = set()
    for label_set in (LABELS, AUDIT_LABELS, CODE_AUDIT_LABELS, UPDATE_CHECK_LABELS, CHECK_WORKFLOWS_LABELS, AGENT_AUDIT_LABELS, EXTERNAL_SCOUT_LABELS):
        for name, color, description in label_set:
            if name in seen:
                continue
            seen.add(name)
            subprocess.run(
                [
                    "gh", "label", "create", name,
                    "--color", color,
                    "--description", description,
                    "--repo", REPO,
                ],
                check=False,
                capture_output=True,
            )
    for name in LABELS_TO_DELETE:
        subprocess.run(
            [
                "gh", "label", "delete", name,
                "--yes",
                "--repo", REPO,
            ],
            check=False,
            capture_output=True,
        )


def issue_exists(key: str) -> bool:
    """Return True if an issue already carries this fingerprint."""
    fingerprint = f"<!-- fingerprint: {key} -->"
    result = subprocess.run(
        [
            "gh", "issue", "list",
            "--repo", REPO,
            "--search", f'"{fingerprint}" in:body',
            "--state", "all",
            "--json", "number",
            "--limit", "1",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(
            f"[publish] gh issue list failed ({result.returncode}):\n"
            f"{result.stderr}",
            file=sys.stderr,
        )
        return False
    # Empty list == "[]"
    return bool(result.stdout.strip() and result.stdout.strip() != "[]")


def create_issue(f: Finding, namespace: str = "auto-improve") -> int:
    """Create one issue. Returns gh's exit code."""
    if namespace == "audit":
        source_note = "cai audit agent"
        source_file = ".claude/agents/cai-audit.md"
    elif namespace == "code-audit":
        source_note = "cai code-audit agent"
        source_file = ".claude/agents/cai-code-audit.md"
    elif namespace == "update-check":
        source_note = "cai update-check agent"
        source_file = ".claude/agents/cai-update-check.md"
    elif namespace == "check-workflows":
        source_note = "cai check-workflows agent"
        source_file = ".claude/agents/cai-check-workflows.md"
    elif namespace == "agent-audit":
        source_note = "cai agent-audit agent"
        source_file = ".claude/agents/cai-agent-audit.md"
    elif namespace == "external-scout":
        source_note = "cai external-scout agent"
        source_file = ".claude/agents/cai-external-scout.md"
    else:
        source_note = "cai self-analyzer"
        source_file = ".claude/agents/cai-analyze.md"
    body = (
        f"<!-- fingerprint: {f.key} -->\n"
        f"**Category:** `{f.category}`  \n"
        f"**Confidence:** `{f.confidence}`\n"
        f"\n"
        f"## Evidence\n"
        f"\n"
        f"{f.evidence}\n"
        f"\n"
        f"## Remediation\n"
        f"\n"
        f"{f.remediation}\n"
        f"\n"
        f"---\n"
        f"_Raised automatically by the {source_note}. "
        f"See `{source_file}`._\n"
    )
    if namespace == "audit":
        labels = ",".join([
            "auto-improve",
            "auto-improve:raised",
            "audit",
        ])
    elif namespace == "check-workflows":
        labels = ",".join([
            "auto-improve",
            "auto-improve:raised",
            "check-workflows",
        ])
    else:
        labels = ",".join([
            "auto-improve",
            "auto-improve:raised",
        ])

    result = subprocess.run(
        [
            "gh", "issue", "create",
            "--repo", REPO,
            "--title", f.title,
            "--body", body,
            "--label", labels,
        ],
        check=False,
    )
    return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish findings as GitHub issues")
    parser.add_argument(
        "--namespace", default="auto-improve",
        choices=["auto-improve", "audit", "code-audit", "update-check", "check-workflows", "agent-audit", "external-scout"],
        help="Label namespace to use (default: auto-improve)",
    )
    parser.add_argument(
        "--findings-file",
        default=None,
        help="Path to a JSON file with {\"findings\": [...]}; "
             "when provided, replaces stdin/markdown parsing.",
    )
    args = parser.parse_args()
    namespace = args.namespace
    if namespace == "audit":
        valid_cats = AUDIT_CATEGORIES
    elif namespace == "code-audit":
        valid_cats = CODE_AUDIT_CATEGORIES
    elif namespace == "update-check":
        valid_cats = UPDATE_CHECK_CATEGORIES
    elif namespace == "check-workflows":
        valid_cats = CHECK_WORKFLOWS_CATEGORIES
    elif namespace == "agent-audit":
        valid_cats = AGENT_AUDIT_CATEGORIES
    elif namespace == "external-scout":
        valid_cats = EXTERNAL_SCOUT_CATEGORIES
    else:
        valid_cats = VALID_CATEGORIES

    text = ""
    if args.findings_file:
        findings = load_findings_json(args.findings_file, valid_cats)
    else:
        text = sys.stdin.read()
        if not text.strip():
            print("[publish] empty input; nothing to do")
            return 0
        findings = parse_findings(text, valid_categories=valid_cats)

    if not findings:
        if text:
            snippet = text[:500].replace("\n", "↵")
            print(
                f"[publish] ERROR: non-empty input produced 0 findings — "
                f"agent may have used prose instead of ### Finding: blocks.\n"
                f"Input snippet: {snippet!r}",
                file=sys.stderr,
            )
        else:
            print(
                "[publish] ERROR: findings file produced 0 valid findings — "
                "all entries were rejected due to missing or invalid fields.",
                file=sys.stderr,
            )
        return 1

    print(f"[publish] parsed {len(findings)} finding(s)")
    ensure_labels(namespace)

    created = 0
    skipped = 0
    failed = 0
    for f in findings:
        if issue_exists(f.key):
            print(f"[publish] skip (already exists): {f.key}")
            skipped += 1
            continue
        rc = create_issue(f, namespace)
        if rc == 0:
            print(f"[publish] created: {f.key}")
            created += 1
        else:
            print(f"[publish] FAILED ({rc}): {f.key}", file=sys.stderr)
            failed += 1

    print(
        f"[publish] done. created={created} skipped={skipped} failed={failed}"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
