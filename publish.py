#!/usr/bin/env python3
"""
Publish analyzer findings as GitHub issues via the `gh` CLI.

Reads the analyzer's stdout from this script's own stdin, parses
`### Finding:` markdown blocks produced by `prompts/backend-auto-improve.md`,
and creates one issue per finding in the `damien-robotsix/robotsix-cai`
repository. Existing findings are deduped by a fingerprint HTML comment
embedded in the issue body (`<!-- fingerprint: <key> -->`).

Phase C.2 scope — this is the Lane 1 publish step. Lane 2 (workspace
targets) is still deferred.

No third-party Python dependencies — only stdlib plus the `gh` CLI.

Usage::

    cat analyzer-output.md | python publish.py

    # Or from cai.py, piped directly:
    # subprocess.run(["python", "/app/publish.py"], input=analyzer_stdout, ...)
"""

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass


# Lane 1 target — the backend improves itself. When Lane 2 lands, this
# will be parameterized per workspace.
REPO = "damien-robotsix/robotsix-cai"

# The set of categories declared in prompts/backend-auto-improve.md. Any
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
}

# Labels we ensure exist before creating issues. The first two are the
# state labels; the rest are the category labels. Idempotent — `gh label
# create` returns non-zero if the label already exists, which we ignore.
LABELS = [
    ("auto-improve", "ededed", "Self-improvement finding raised by the analyzer"),
    ("auto-improve:raised", "0e8a16", "Finding freshly raised; not yet triaged"),
    ("auto-improve:requested", "1d76db", "Human-requested fix (admin-only label)"),
    ("auto-improve:in-progress", "fbca04", "fix subagent is actively working on this issue"),
    ("auto-improve:pr-open", "5319e7", "fix subagent opened a PR"),
    ("auto-improve:merged", "0e8a16", "PR was merged; awaiting verify"),
    ("category:reliability", "d73a4a", "Errors, failures, flaky behavior"),
    ("category:cost_reduction", "fbca04", "Token waste, unnecessary tool calls"),
    ("category:prompt_quality", "0075ca", "Unclear or missing prompt guidance"),
    ("category:workflow_efficiency", "5319e7", "Unnecessary workflow steps or config"),
]

AUDIT_LABELS = [
    ("audit", "c5def5", "Queue/PR consistency audit finding"),
    ("audit:raised", "0e8a16", "Audit finding freshly raised; needs human triage"),
    ("audit:solved", "6f42c1", "Audit finding addressed"),
    ("category:stale_lifecycle", "d93f0b", "Issue stuck in a state longer than expected"),
    ("category:lock_corruption", "e11d48", "Mutually exclusive labels or dangling references"),
    ("category:loop_stuck", "fbca04", "Findings raised but no fixes landing"),
    ("category:prompt_contradiction", "0075ca", "Conflicting rules in prompt files"),
    ("category:topic_duplicate", "5319e7", "Two open issues about the same pattern"),
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


def _extract_field(block: str, name: str) -> str:
    """Pull a single-line `- **Name:** value` field out of a block."""
    match = re.search(
        rf"^- \*\*{re.escape(name)}:\*\*\s*(.+)$",
        block,
        flags=re.MULTILINE,
    )
    return match.group(1).strip() if match else ""


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


def ensure_labels(namespace: str = "auto-improve") -> None:
    """Create the cai label set if it doesn't exist. Idempotent."""
    label_set = AUDIT_LABELS if namespace == "audit" else LABELS
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
        source_file = "prompts/backend-audit.md"
    else:
        source_note = "cai self-analyzer"
        source_file = "prompts/backend-auto-improve.md"
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
            "audit",
            "audit:raised",
            f"category:{f.category}",
        ])
    else:
        labels = ",".join([
            "auto-improve",
            "auto-improve:raised",
            f"category:{f.category}",
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
        choices=["auto-improve", "audit"],
        help="Label namespace to use (default: auto-improve)",
    )
    args = parser.parse_args()
    namespace = args.namespace
    valid_cats = AUDIT_CATEGORIES if namespace == "audit" else VALID_CATEGORIES

    text = sys.stdin.read()
    if not text.strip():
        print("[publish] empty input; nothing to do")
        return 0

    findings = parse_findings(text, valid_categories=valid_cats)
    if not findings:
        print("[publish] no findings parsed; nothing to do")
        return 0

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
