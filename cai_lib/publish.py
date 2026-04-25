#!/usr/bin/env python3
"""Publish analyzer findings as GitHub issues via the ``gh`` CLI.

Reads findings from a JSON file (via --findings-file),
deduplicates against existing open issues, and creates one
GitHub issue per unique finding.

Phase C.2 scope — Lane 1 publish step (Lane 2 deferred).

Key behaviours:
  - Malformed JSON or missing ``findings`` list → exit 1.
  - Zero valid findings after validation → exit 1.
  - Otherwise, creates issues and returns 0 on full success,
    1 if any issue creation failed.

No third-party dependencies — only stdlib + ``gh`` CLI.

Usage::

    # Read from a JSON file:
    python publish.py --namespace audit-good-practices --findings-file /tmp/findings.json

"""

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass

from cai_lib.dup_check import check_finding_duplicate


# Lane 1 target — the backend improves itself. When Lane 2 lands, this
# will be parameterized per workspace.
REPO = os.environ.get("CAI_REPO", "damien-robotsix/robotsix-cai")

# Default category set for the legacy auto-improve namespace. Any
# finding whose category is outside this set is rejected before we touch
# GitHub.
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
    "human_needed_pipeline_jam",
    "human_needed_abandoned",
    "human_needed_loop",
    "human_needed_reason_missing",
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

AUDIT_EXTERNAL_LIBS_CATEGORIES = {
    "library_replacement",
    "vendored_dependency",
    "reinvented_stdlib",
}

AUDIT_GOOD_PRACTICES_CATEGORIES = {
    "claude_best_practice",
    "doc_drift",
    "tool_misuse",
    "model_tier_mismatch",
}

AUDIT_CODE_REDUCTION_CATEGORIES = {
    "dead_code",
    "duplicated_logic",
    "over_abstraction",
    "inline_helper",
}

AUDIT_COST_REDUCTION_CATEGORIES = {
    "model_downgrade",
    "prompt_cache_restructure",
    "read_window_reduction",
    "redundant_subagent",
    "tool_list_bloat",
    "loop_overhead",
}

AUDIT_WORKFLOW_ENHANCEMENT_CATEGORIES = {
    "redundant_call",
    "prompt_inefficiency",
    "handoff_loop",
    "deterministic_replacement",
}

AUDIT_HEALTH_CATEGORIES = {
    "audit-health",
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
    ("needs-workflow-review", "e11d48", "PR touches `.github/workflows/` and was held at medium confidence; awaiting admin workflow review"),
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
    # cai-update-check findings are 100% source-file edits
    # (Dockerfile/settings.json/cai.py/agent-prompt) — never
    # declarative gh-CLI ops expressible in a cai-maintain block.
    # We attach kind:code at create_issue time so triage never
    # mis-routes an update-check issue to kind:maintenance /
    # cai-maintain (issue #991; prevents the #980 divert class).
    ("kind:code", "0075ca", "Triage: issue requires a code change"),
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

AUDIT_EXTERNAL_LIBS_LABELS = [
    ("auto-improve", "ededed", "Self-improvement finding raised by the analyzer"),
    ("auto-improve:raised", "0e8a16", "Awaiting structured refinement before implement subagent picks it up"),
]

# Shared label list for the per-module on-demand audit namespaces
# (good-practices, code-reduction, cost-reduction, workflow-enhancement).
# Each of the four namespaces only needs the generic auto-improve entry
# points — no source tag — so they all alias this one list to avoid
# copy-pasting the same two rows four times.
_AUTO_IMPROVE_RAISED_ONLY = [
    ("auto-improve", "ededed", "Self-improvement finding raised by the analyzer"),
    ("auto-improve:raised", "0e8a16", "Awaiting structured refinement before implement subagent picks it up"),
]

AUDIT_GOOD_PRACTICES_LABELS = _AUTO_IMPROVE_RAISED_ONLY
AUDIT_CODE_REDUCTION_LABELS = _AUTO_IMPROVE_RAISED_ONLY
AUDIT_COST_REDUCTION_LABELS = _AUTO_IMPROVE_RAISED_ONLY
AUDIT_WORKFLOW_ENHANCEMENT_LABELS = _AUTO_IMPROVE_RAISED_ONLY
AUDIT_HEALTH_LABELS = _AUTO_IMPROVE_RAISED_ONLY


@dataclass
class Finding:
    title: str
    category: str
    key: str
    confidence: str
    evidence: str
    remediation: str


def load_findings_json(path: str, valid_categories: set[str]) -> list[Finding]:
    """Load and validate a JSON findings file.

    Schema:
        {"findings": [{"title", "category", "key",
                       "confidence", "evidence", "remediation"}, ...]}

    Validation rules:
      * Malformed JSON or missing top-level ``findings`` list -> sys.exit(1).
      * Required fields (title, category, key) missing -> per-finding
        stderr error, that entry skipped (other entries keep going).
      * category outside ``valid_categories`` -> per-finding stderr error, skipped.
      * confidence not in {"low","medium","high"} -> warn and default to
        "unspecified".
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


def _finding_body_for_dupcheck(f: Finding) -> str:
    """Render a staged finding as the ``body`` text fed to cai-dup-check.

    The haiku agent only sees this text plus the title when deciding
    whether a finding duplicates an already-open issue, so include the
    two sections that carry the semantic content — evidence and
    remediation.
    """
    return (
        f"**Category:** `{f.category}`\n\n"
        f"## Evidence\n\n{f.evidence}\n\n"
        f"## Remediation\n\n{f.remediation}\n"
    )


_NAMESPACE_REGISTRY: dict[str, tuple] = {
    "audit": (AUDIT_LABELS, AUDIT_CATEGORIES),
    "code-audit": (CODE_AUDIT_LABELS, CODE_AUDIT_CATEGORIES),
    "update-check": (UPDATE_CHECK_LABELS, UPDATE_CHECK_CATEGORIES),
    "check-workflows": (CHECK_WORKFLOWS_LABELS, CHECK_WORKFLOWS_CATEGORIES),
    "agent-audit": (AGENT_AUDIT_LABELS, AGENT_AUDIT_CATEGORIES),
    "external-scout": (EXTERNAL_SCOUT_LABELS, EXTERNAL_SCOUT_CATEGORIES),
    "audit-external-libs": (AUDIT_EXTERNAL_LIBS_LABELS, AUDIT_EXTERNAL_LIBS_CATEGORIES),
    "audit-good-practices": (AUDIT_GOOD_PRACTICES_LABELS, AUDIT_GOOD_PRACTICES_CATEGORIES),
    "audit-code-reduction": (AUDIT_CODE_REDUCTION_LABELS, AUDIT_CODE_REDUCTION_CATEGORIES),
    "audit-cost-reduction": (AUDIT_COST_REDUCTION_LABELS, AUDIT_COST_REDUCTION_CATEGORIES),
    "audit-workflow-enhancement": (AUDIT_WORKFLOW_ENHANCEMENT_LABELS, AUDIT_WORKFLOW_ENHANCEMENT_CATEGORIES),
    "audit-health": (AUDIT_HEALTH_LABELS, AUDIT_HEALTH_CATEGORIES),
}


def _label_set_for(namespace: str):
    """Return the label set for the given namespace."""
    return _NAMESPACE_REGISTRY.get(namespace, (LABELS, VALID_CATEGORIES))[0]


def _category_set_for(namespace: str) -> set[str]:
    """Return the valid-category set for the given namespace."""
    return _NAMESPACE_REGISTRY.get(namespace, (LABELS, VALID_CATEGORIES))[1]


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
    for label_set in (LABELS, AUDIT_LABELS, CODE_AUDIT_LABELS, UPDATE_CHECK_LABELS, CHECK_WORKFLOWS_LABELS, AGENT_AUDIT_LABELS, EXTERNAL_SCOUT_LABELS, AUDIT_EXTERNAL_LIBS_LABELS, AUDIT_GOOD_PRACTICES_LABELS, AUDIT_CODE_REDUCTION_LABELS, AUDIT_COST_REDUCTION_LABELS, AUDIT_WORKFLOW_ENHANCEMENT_LABELS, AUDIT_HEALTH_LABELS):
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


def create_issue(
    f: Finding,
    namespace: str = "auto-improve",
    *,
    module_name: str | None = None,
) -> int:
    """Create one issue. Returns gh's exit code.

    When ``module_name`` is set, a ``<!-- module: {name} -->`` HTML
    comment is appended on its own line just before the closing
    ``---`` separator. Future per-module audit runs can scope their
    dedup search by both fingerprint and module footer.
    """
    if namespace == "update-check":
        source_note = "cai update-check agent"
        source_file = ".claude/agents/ops/cai-update-check.md"
    elif namespace == "check-workflows":
        source_note = "cai check-workflows agent"
        source_file = ".claude/agents/cai-check-workflows.md"
    elif namespace == "external-scout":
        source_note = "cai external-scout agent"
        source_file = ".claude/agents/cai-external-scout.md"
    elif namespace == "audit-external-libs":
        source_note = "cai external-libs audit agent"
        source_file = ".claude/agents/audit/cai-audit-external-libs.md"
    elif namespace == "audit-good-practices":
        source_note = "cai good-practices audit agent"
        source_file = ".claude/agents/audit/cai-audit-good-practices.md"
    elif namespace == "audit-code-reduction":
        source_note = "cai code-reduction audit agent"
        source_file = ".claude/agents/audit/cai-audit-code-reduction.md"
    elif namespace == "audit-cost-reduction":
        source_note = "cai cost-reduction audit agent"
        source_file = ".claude/agents/audit/cai-audit-cost-reduction.md"
    elif namespace == "audit-workflow-enhancement":
        source_note = "cai workflow-enhancement audit agent"
        source_file = ".claude/agents/audit/cai-audit-workflow-enhancement.md"
    elif namespace == "audit-health":
        source_note = "cai audit-health monitor agent"
        source_file = ".claude/agents/audit/cai-audit-audit-health.md"
    else:
        source_note = "cai self-analyzer"
        source_file = ".claude/agents/cai-refine.md"
    module_footer = f"<!-- module: {module_name} -->\n" if module_name else ""
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
        f"{module_footer}"
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
    elif namespace == "update-check":
        # cai-update-check findings always require a source-file
        # edit (Dockerfile bump, .claude/settings.json flag,
        # cai.py/cai_lib invocation change, agent-prompt update).
        # Pre-apply kind:code at creation time so cai-triage honors
        # it as authoritative and never flips the issue to
        # kind:maintenance / cai-maintain (issue #991).
        labels = ",".join([
            "auto-improve",
            "auto-improve:raised",
            "kind:code",
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


def publish_findings(
    findings_path: str,
    namespace: str = "auto-improve",
    module_name: str | None = None,
) -> int:
    """Load, dup-check, and publish findings as GitHub issues.

    This is the shared implementation used by both the ``python
    publish.py`` CLI (via :func:`main`) and the in-process callers
    (e.g. the per-module audit runner which shells out with
    ``--module`` and may also invoke this directly in future).

    Parameters
    ----------
    findings_path:
        Path to a JSON file shaped ``{"findings": [...]}``.
    namespace:
        Label / category namespace. Must be registered in
        :func:`_category_set_for` and :func:`_label_set_for`.
    module_name:
        Optional module identifier. When set, each created issue
        carries a ``<!-- module: {name} -->`` footer so future audit
        runs can scope their dedup search by module.

    Returns
    -------
    int
        0 on full success (including zero valid findings — an empty
        findings file from a per-module audit run is not an error).
        1 if any ``gh issue create`` call returned non-zero.
    """
    valid_cats = _category_set_for(namespace)
    findings = load_findings_json(findings_path, valid_cats)

    if not findings:
        # Empty findings is a valid per-module outcome (module looks
        # healthy). main() historically treated this as an error;
        # keep the message but return 0 so per-module loops don't
        # spuriously count "clean" modules as failures.
        print(
            f"[publish] no valid findings in {findings_path!r} for namespace "
            f"{namespace!r} (module={module_name!r})"
        )
        return 0

    print(f"[publish] parsed {len(findings)} finding(s)")
    ensure_labels(namespace)

    # Setting CAI_SKIP_DUPCHECK_ON_PUBLISH=1 disables the semantic
    # pre-publish dup-check — useful for offline test runs or when the
    # haiku agent is unavailable.
    semantic_dupcheck_enabled = (
        os.environ.get("CAI_SKIP_DUPCHECK_ON_PUBLISH", "").strip() not in ("1", "true", "yes")
    )

    created = 0
    skipped = 0
    skipped_duplicate = 0
    failed = 0
    for f in findings:
        if issue_exists(f.key):
            print(f"[publish] skip (already exists): {f.key}")
            skipped += 1
            continue
        if semantic_dupcheck_enabled:
            verdict = check_finding_duplicate(
                title=f.title,
                body=_finding_body_for_dupcheck(f),
            )
            if verdict is not None and verdict.should_close:
                ref = (
                    f"#{verdict.target}" if verdict.verdict == "DUPLICATE"
                    else (verdict.commit_sha or "(unspecified)")
                )
                print(
                    f"[publish] skip (semantic duplicate of {ref}): {f.key} "
                    f"— {verdict.reasoning}"
                )
                skipped_duplicate += 1
                continue
        rc = create_issue(f, namespace, module_name=module_name)
        if rc == 0:
            print(f"[publish] created: {f.key}")
            created += 1
        else:
            print(f"[publish] FAILED ({rc}): {f.key}", file=sys.stderr)
            failed += 1

    print(
        f"[publish] done. namespace={namespace} module={module_name} "
        f"created={created} skipped={skipped} "
        f"skipped_duplicate={skipped_duplicate} failed={failed}"
    )
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish findings as GitHub issues")
    parser.add_argument(
        "--namespace", default="auto-improve",
        choices=["auto-improve", "audit", "code-audit", "update-check", "check-workflows", "agent-audit", "external-scout", "audit-external-libs", "audit-good-practices", "audit-code-reduction", "audit-cost-reduction", "audit-workflow-enhancement", "audit-health"],
        help="Label namespace to use (default: auto-improve)",
    )
    parser.add_argument(
        "--findings-file",
        required=True,
        help="Path to a JSON file with {\"findings\": [...]}.",
    )
    parser.add_argument(
        "--module", default=None,
        help=(
            "Optional module name. When set, every created issue "
            "carries a <!-- module: NAME --> body footer so future "
            "audit runs can scope dedup by module."
        ),
    )
    args = parser.parse_args()
    return publish_findings(
        findings_path=args.findings_file,
        namespace=args.namespace,
        module_name=args.module,
    )


if __name__ == "__main__":
    sys.exit(main())
