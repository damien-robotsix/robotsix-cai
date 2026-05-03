"""``cai-audit`` CLI: ask the audit agent to mine signals and file issues.

The pipeline runs as a graph: RunAuditNode → CreateIssuesNode. RunAuditNode
short-circuits to End when the agent proposes nothing. CreateIssuesNode
runs the issue-deduplicator agent per proposed item to decide whether to
create a new issue, append a comment to an existing one, or discard.

Six audit modes are supported:
  --mode cost          Audit the most costly session of the last 10 issue-solving runs.
  --mode errors        Audit the 10 most recent traces that contain error-level observations.
  --mode duplication   Audit copy-paste findings from jscpd against a fresh clone of the repo.
  --mode architecture  Clone the repo and audit structural health.
  --mode security      Clone the repo and audit for common vulnerability patterns.
  --mode deps          Clone the repo and audit dependency freshness against PyPI.

In every mode all signal context is pre-fetched into the prompt so the agent
can spend its tokens on judgement rather than tool plumbing.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import typing
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, model_validator
from pydantic_ai_backends.backends.local import LocalBackend
from pydantic_graph import BaseNode, End, Graph, GraphRunContext

from pydantic_deep import create_default_deps

from cai.agents.loader import build_deep_agent, load_agent_from_md, parse_agent_md, resolve_agent_path
from cai.git import clone as git_clone
from cai.github.bot import CaiBot
from cai.log.observability import langfuse_workflow, setup_langfuse
from cai.log.traces import _TRACES, _format_failures
from cai.workflows.state import WithConfidence, _inline_refs

# Per the auditor rubrics (src/cai/agents/{audit,architecture_auditor,duplication_auditor}.md),
# 9-10 is the band where the agent claims a fix is safe to dispatch without human review.
# Below that, we tag the issue for human triage instead of auto-routing it to cai-solve.
_AUTO_RAISE_CONFIDENCE_THRESHOLD = 9


def _labels_for_confidence(confidence: int) -> list[str]:
    routing = "cai:raised" if confidence >= _AUTO_RAISE_CONFIDENCE_THRESHOLD else "cai:human-review"
    return ["cai:audit", routing]


class ProposedIssue(WithConfidence):
    title: str
    body: str
    last_detected_at: str | None = None  # ISO timestamp of the most recent relevant trace
    trace_ids: list[str] = []  # Langfuse trace IDs that motivated the issue; non-empty marks it as needing human trace investigation and disables auto-raise


class DedupeOutput(BaseModel):
    action: typing.Literal["new", "discard", "append"]
    target_issue_number: int | None
    reason: str


class AuditOutput(BaseModel):
    issues: list[ProposedIssue]

    @classmethod
    def model_json_schema(cls, **kwargs: object) -> dict:
        return _inline_refs(super().model_json_schema(**kwargs))

    @model_validator(mode="before")
    @classmethod
    def _drop_nulls(cls, v: object) -> object:
        if isinstance(v, dict) and "issues" in v:
            parsed = []
            for item in v.get("issues") or []:
                if item is None:
                    continue
                if isinstance(item, str):
                    # Gemini sometimes wraps each issue as a markdown-fenced JSON string
                    # instead of returning a proper object. Strip the fence and parse.
                    text = item.strip()
                    text = re.sub(r"^```[^\n]*\n?", "", text)
                    text = re.sub(r"\n?```$", "", text)
                    try:
                        item = json.loads(text.strip())
                    except (json.JSONDecodeError, ValueError):
                        continue
                parsed.append(item)
            v = {**v, "issues": parsed}
        return v


@dataclass
class AuditState:
    bot: CaiBot
    repo: str
    prompt: str
    agent_name: str = "audit"
    workspace: Path | None = None  # filesystem root the agent is allowed to read
    output: AuditOutput | None = field(default=None)


@lru_cache(maxsize=5)
def _audit_agent(agent_name: str = "audit"):
    config, instructions = parse_agent_md(resolve_agent_path(agent_name))
    return build_deep_agent(config, instructions, output_type=AuditOutput)


@lru_cache(maxsize=1)
def _dedupe_agent():
    return load_agent_from_md(
        resolve_agent_path("issue_deduplicator"), output_type=DedupeOutput
    )


def _trace_section(trace_ids: list[str]) -> str:
    """Render trace IDs as a markdown section to splice into an issue body or comment."""
    bullets = "\n".join(f"- `{tid}`" for tid in trace_ids)
    return (
        "## Relevant Traces\n\n"
        "Symptom drawn from the following Langfuse traces. "
        "Inspect them (`traces_show <id>`) to confirm the issue is real before acting.\n\n"
        f"{bullets}\n"
    )


def _labels_for_trace_investigation(labels: list[str]) -> list[str]:
    """Force human review and tag as trace-investigation for issues backed by trace IDs."""
    out = ["cai:human-review" if lbl == "cai:raised" else lbl for lbl in labels]
    if "cai:trace-investigation" not in out:
        out.append("cai:trace-investigation")
    return out


async def _create_issues_from_proposals(
    bot: CaiBot,
    repo_name: str,
    issues: list[ProposedIssue],
    labels_for_confidence: typing.Callable[[int], list[str]],
) -> None:
    """Deduplicate proposed issues against open issues and create, append, or discard each."""
    if not issues:
        return

    repo_obj = bot.repo(repo_name)

    open_issues = repo_obj.get_issues(state="open")
    open_issues_summary = (
        "\n".join(f"#{issue.number}: {issue.title}" for issue in open_issues)
        or "No open issues."
    )
    dedupe_agent = _dedupe_agent()

    for issue in issues:
        print(f"Evaluating proposed issue: {issue.title}")

        recent_commits_text = _recent_commits_since(repo_obj, issue.last_detected_at)

        dedupe_prompt = (
            f"Proposed issue title: {issue.title}\n"
            f"Proposed issue body: {issue.body}\n\n"
            f"Currently open issues:\n{open_issues_summary}"
            + recent_commits_text
        )
        dedupe_decision: DedupeOutput = (await dedupe_agent.run(dedupe_prompt)).output

        if dedupe_decision.action == "discard":
            print(f"Discarding issue '{issue.title}': {dedupe_decision.reason}")
            continue

        if (
            dedupe_decision.action == "append"
            and dedupe_decision.target_issue_number is not None
        ):
            target_issue = repo_obj.get_issue(dedupe_decision.target_issue_number)
            print(
                f"Appending issue '{issue.title}' to "
                f"#{target_issue.number}: {dedupe_decision.reason}"
            )
            comment = (
                "**Additional proposed issue details:**\n\n"
                f"**Title**: {issue.title}\n\n"
                f"**Body**:\n{issue.body}"
            )
            if issue.trace_ids:
                comment += "\n\n" + _trace_section(issue.trace_ids)
            target_issue.create_comment(comment)
            continue

        if dedupe_decision.action == "append":
            print(
                f"Warning: Deduplicator suggested appending '{issue.title}' "
                f"but provided no target_issue_number. "
                f"Reason: {dedupe_decision.reason}. "
                "Falling back to creating a new issue.",
                file=sys.stderr,
            )

        labels = labels_for_confidence(issue.confidence)
        body = issue.body
        if issue.trace_ids:
            labels = _labels_for_trace_investigation(labels)
            body = body.rstrip() + "\n\n" + _trace_section(issue.trace_ids)
        created = repo_obj.create_issue(
            title=issue.title,
            body=body,
            labels=labels,
        )
        print(f"Created (confidence={issue.confidence}, labels={labels}): {created.html_url}")


class RunAuditNode(BaseNode[AuditState, None, AuditOutput]):
    """Run the audit agent against pre-fetched signal context."""

    async def run(
        self, ctx: GraphRunContext[AuditState]
    ) -> "CreateIssuesNode | End[AuditOutput]":
        if ctx.state.workspace is not None:
            deps = create_default_deps(backend=LocalBackend(root_dir=ctx.state.workspace))
        else:
            deps = create_default_deps()
        result = await _audit_agent(ctx.state.agent_name).run(ctx.state.prompt, deps=deps)
        output: AuditOutput = result.output
        ctx.state.output = output
        if not output.issues:
            print("No issues proposed by the audit agent.", file=sys.stderr)
            return End(output)
        return CreateIssuesNode()


async def _create_issues_node_run(
    ctx: GraphRunContext, labels_for_confidence: typing.Callable[[int], list[str]]
) -> End:
    assert ctx.state.output is not None
    await _create_issues_from_proposals(
        bot=ctx.state.bot,
        repo_name=ctx.state.repo,
        issues=ctx.state.output.issues,
        labels_for_confidence=labels_for_confidence,
    )
    return End(ctx.state.output)


class CreateIssuesNode(BaseNode[AuditState, None, AuditOutput]):
    """Per proposed issue: check recent commits, dedupe, then create/append/discard."""

    async def run(self, ctx: GraphRunContext[AuditState]) -> End[AuditOutput]:
        return await _create_issues_node_run(ctx, _labels_for_confidence)


def _recent_commits_since(repo_obj: object, last_detected_at: str | None) -> str:
    """Return a formatted block of commits pushed after ``last_detected_at``.

    Returns an empty string when the timestamp is missing or the fetch fails.
    The block instructs the dedupe agent to discard the issue if any commit
    appears to address it.
    """
    if not last_detected_at:
        return ""
    try:
        since_dt = datetime.fromisoformat(
            last_detected_at.replace("Z", "+00:00")
        ).replace(tzinfo=timezone.utc)
        commits = list(repo_obj.get_commits(since=since_dt))[:20]  # type: ignore[attr-defined]
        if not commits:
            return ""
        lines = "\n".join(
            f"  {c.sha[:8]} {c.commit.message.splitlines()[0]}" for c in commits
        )
        return (
            f"\n\nCommits merged after the problem was last detected "
            f"({last_detected_at[:19]}):\n{lines}\n"
            f"If any of these commits appears to already address this issue, "
            f"set action to 'discard'."
        )
    except Exception as exc:
        print(f"Warning: could not fetch recent commits: {exc}", file=sys.stderr)
        return ""


audit_graph: Graph[AuditState, None, AuditOutput] = Graph(
    nodes=[RunAuditNode, CreateIssuesNode]
)


# ---------------------------------------------------------------------------
# Prompt builders — pre-fetch everything so the agent needs no listing tools
# ---------------------------------------------------------------------------

def _build_cost_prompt(unknown: list[str]) -> str:
    """Prompt for --mode cost: most expensive of last 10 issue-solving sessions."""
    session = _TRACES.most_costly_solve_session(n=10)
    if session is None:
        print("No issue-solving sessions found in Langfuse.", file=sys.stderr)
        sys.exit(1)

    traces = _TRACES.list_session_traces(session["session_id"])
    rows = [
        f"{'ID':<36} {'NAME':<25} {'TIMESTAMP':<22} {'COST':>9} {'LATENCY':>9}",
        "-" * 105,
    ]
    for t in traces:
        ts = (t["timestamp"] or "?")[:19]
        cost = f"${t['cost']:.4f}" if t["cost"] else "N/A"
        lat = f"{t['latency']:.1f}s" if t["latency"] else "N/A"
        rows.append(f"{t['id']:<36} {t['name']:<25} {ts:<22} {cost:>9} {lat:>9}")

    prompt = (
        f"Audit session {session['session_id']} — the most costly of the last 10 "
        f"issue-solving sessions (total cost: ${session['total_cost']:.4f}).\n\n"
        "Traces in this session:\n"
        + "\n".join(rows)
        + "\n\nDelegate deep inspection of interesting traces to trace_analyst. "
        "Draft improvements as proposed issues. "
        "Set last_detected_at to the ISO timestamp of the relevant trace for each issue. "
        "Populate trace_ids with every trace ID that supports the issue — this routes "
        "it to a human for trace-level confirmation and disables auto-raise."
    )
    if unknown:
        prompt += f"\n\nAdditional context: {' '.join(unknown)}"
    return prompt


def _build_errors_prompt(unknown: list[str]) -> str:
    """Prompt for --mode errors: 10 most recent traces with error-level observations."""
    failures = _TRACES.list_failures(limit=10)
    if not failures:
        print("No recent failures found in Langfuse.", file=sys.stderr)
        sys.exit(1)

    lines = _format_failures(
        failures,
        max_message_len=300,
        max_output_len=200,
        header=f"Recent failures ({len(failures)} traces with errors):",
    )

    prompt = (
        "Audit the following recent failures in Langfuse traces.\n\n"
        + "\n".join(lines)
        + "\n\nDelegate deep inspection of specific traces to trace_analyst. "
        "Draft improvements as proposed issues. "
        "Set last_detected_at to the ISO timestamp of the most recent relevant "
        "failure for each issue. "
        "Populate trace_ids with every trace ID that supports the issue — this routes "
        "it to a human for trace-level confirmation and disables auto-raise."
    )
    if unknown:
        prompt += f"\n\nAdditional context: {' '.join(unknown)}"
    return prompt


# ---------------------------------------------------------------------------
# Duplication mode — clone the target repo, run jscpd, format clones for the
# duplication_auditor agent.
# ---------------------------------------------------------------------------

# Per-language jscpd thresholds. YAML workflows are short and noisy at the
# Python default, so we drop the bar; markdown is bumped because docs naturally
# repeat phrasing.
_JSCPD_LANGUAGES: tuple[tuple[str, int], ...] = (
    ("python", 50),
    ("yaml", 20),
)


def _clone_repo_for_audit(bot: CaiBot, repo: str, dest: Path) -> None:
    """Clone ``repo`` into ``dest`` using the bot's installation token."""
    token = bot.token_for(repo)
    url = f"https://x-access-token:{token}@github.com/{repo}.git"
    git_clone(url, dest)


def _jscpd_argv() -> list[str]:
    """Resolve how to invoke jscpd. Prefer the global binary; fall back to npx."""
    binary = shutil.which("jscpd")
    if binary:
        return [binary]
    return ["npx", "--yes", "jscpd@4"]


def _run_jscpd(
    workspace: Path, language: str, min_tokens: int, *, output_dir: Path
) -> list[dict]:
    """Run jscpd on ``workspace`` for one language and return its clone records."""
    output_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        *_jscpd_argv(),
        "--reporters", "json",
        "--output", str(output_dir),
        "--silent",
        "--min-tokens", str(min_tokens),
        "--format", language,
        str(workspace),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    # jscpd exits 0 on success and may exit non-zero only when --threshold is
    # exceeded; we always pass threshold 0 implicitly via .jscpd.json so any
    # non-zero exit is a real failure worth surfacing.
    if result.returncode != 0:
        print(
            f"Warning: jscpd ({language}) exited {result.returncode}: "
            f"{result.stderr.strip()[:500]}",
            file=sys.stderr,
        )
        return []
    report = output_dir / "jscpd-report.json"
    if not report.exists():
        return []
    try:
        data = json.loads(report.read_text())
    except json.JSONDecodeError as exc:
        print(f"Warning: could not parse jscpd report ({language}): {exc}", file=sys.stderr)
        return []
    return list(data.get("duplicates") or [])


def _format_clone(workspace: Path, clone: dict) -> str:
    """Render a single jscpd clone record as a prompt block."""
    def _rel(path: str) -> str:
        try:
            return str(Path(path).resolve().relative_to(workspace))
        except ValueError:
            return path

    first = clone.get("firstFile") or {}
    second = clone.get("secondFile") or {}
    fragment = (clone.get("fragment") or "").rstrip()
    if len(fragment) > 1500:
        fragment = fragment[:1500] + "\n... [truncated]"
    return (
        f"- format: {clone.get('format', '?')}  "
        f"lines: {clone.get('lines', '?')}  tokens: {clone.get('tokens', '?')}\n"
        f"  A: {_rel(first.get('name', '?'))} "
        f"L{first.get('start', '?')}-L{first.get('end', '?')}\n"
        f"  B: {_rel(second.get('name', '?'))} "
        f"L{second.get('start', '?')}-L{second.get('end', '?')}\n"
        f"  fragment:\n```\n{fragment}\n```"
    )


def _build_duplication_prompt(
    bot: CaiBot, repo: str, workspace: Path, unknown: list[str]
) -> str:
    """Clone ``repo`` into ``workspace`` and build a prompt of jscpd findings."""
    print(f"Cloning {repo} into {workspace} for duplication audit...", file=sys.stderr)
    _clone_repo_for_audit(bot, repo, workspace)

    all_clones: list[dict] = []
    for language, min_tokens in _JSCPD_LANGUAGES:
        report_dir = workspace.parent / f"jscpd-{language}"
        clones = _run_jscpd(workspace, language, min_tokens, output_dir=report_dir)
        print(
            f"jscpd ({language}, min-tokens={min_tokens}): {len(clones)} clones",
            file=sys.stderr,
        )
        all_clones.extend(clones)

    if not all_clones:
        print("No duplication clones found by jscpd.", file=sys.stderr)
        sys.exit(0)

    blocks = [_format_clone(workspace, c) for c in all_clones]
    prompt = (
        f"Audit the following copy-paste findings from jscpd against {repo}. "
        f"The repository is checked out at the agent's filesystem root — open "
        f"any cited file with `filesystem_read` to inspect surrounding context "
        f"before deciding whether to propose a refactor.\n\n"
        f"Found {len(all_clones)} clone groups:\n\n"
        + "\n\n".join(blocks)
        + "\n\nReturn an AuditOutput. Be conservative — only propose issues for "
        "duplications that are worth refactoring (real shared logic, not "
        "boilerplate or coincidental similarity). For multi-location clones, "
        "file ONE issue covering the whole set."
    )
    if unknown:
        prompt += f"\n\nAdditional context: {' '.join(unknown)}"
    return prompt


# ---------------------------------------------------------------------------
# Architecture mode — clone the target repo, walk the directory tree, and
# collect structural signals for the architecture_auditor agent.
# ---------------------------------------------------------------------------

_DIRS_TO_SKIP = frozenset({
    ".git", "__pycache__", "node_modules", ".venv", "venv", ".tox",
    ".eggs", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "dist", "build",
})


def _build_architecture_prompt(
    bot: CaiBot, repo: str, workspace: Path, unknown: list[str]
) -> str:
    """Clone ``repo`` into ``workspace`` and build a prompt of structural context."""
    print(f"Cloning {repo} into {workspace} for architecture audit...", file=sys.stderr)
    _clone_repo_for_audit(bot, repo, workspace)

    tree_lines: list[str] = []
    file_metadata: list[str] = []
    large_files: list[tuple[str, int]] = []
    package_dirs_with_init: list[str] = []
    package_dirs_without_init: list[str] = []

    for root, dirs, files in os.walk(workspace):
        dirs[:] = [
            d for d in dirs
            if d not in _DIRS_TO_SKIP and not d.startswith(".")
        ]

        rel_root = Path(root).relative_to(workspace)
        depth = 0 if rel_root == Path(".") else len(rel_root.parts)
        indent = "    " * depth

        if rel_root == Path("."):
            tree_lines.append(".")
        else:
            tree_lines.append(f"{indent}{rel_root.name}/")

        has_init = "__init__.py" in files
        py_files = [f for f in files if f.endswith(".py")]

        if rel_root != Path(".") and py_files:
            if has_init:
                package_dirs_with_init.append(str(rel_root))
            else:
                package_dirs_without_init.append(str(rel_root))

        for fname in sorted(files):
            if fname.startswith("."):
                continue
            file_indent = "    " * (depth + 1)
            tree_lines.append(f"{file_indent}{fname}")

            if fname.endswith(".py"):
                fpath = Path(root) / fname
                rel_path = str(fpath.relative_to(workspace))
                try:
                    line_count = len(fpath.read_text().splitlines())
                except (OSError, ValueError):
                    line_count = 0
                is_large = line_count > 300
                large_marker = " !LARGE!" if is_large else ""
                file_metadata.append(f"  {rel_path}: {line_count} lines{large_marker}")
                if is_large:
                    large_files.append((rel_path, line_count))

    if large_files:
        large_section = "\n".join(
            f"  - {path}: {lines} lines"
            for path, lines in sorted(large_files, key=lambda x: -x[1])
        )
    else:
        large_section = "(no Python files exceed 300 lines)"

    prompt = (
        f"Architecture audit of {repo}. The repository is checked out at the "
        f"agent's filesystem root.\n\n"
        f"## Directory Tree\n\n"
        + "\n".join(tree_lines)
        + "\n\n## Python File Metadata\n\n"
        + ("\n".join(file_metadata) if file_metadata else "(no Python files found)")
        + "\n\n## Large Python Files (>300 lines)\n\n"
        + large_section
        + "\n\n## Package Structure Summary\n\n"
    )

    if package_dirs_with_init:
        prompt += "Directories with __init__.py (proper packages):\n"
        for d in sorted(package_dirs_with_init):
            prompt += f"  - {d}\n"
    else:
        prompt += "No directories with __init__.py found.\n"

    if package_dirs_without_init:
        prompt += (
            "\nDirectories with Python files but NO __init__.py "
            "(implicit namespace or potential missing package):\n"
        )
        for d in sorted(package_dirs_without_init):
            prompt += f"  - {d}\n"

    prompt += (
        "\nUse `filesystem_read` to inspect specific files for deeper context. "
        "Delegate broad searches to the `explore` subagent."
    )

    if unknown:
        prompt += f"\n\nAdditional context: {' '.join(unknown)}"

    return prompt


# ---------------------------------------------------------------------------
# Security mode — clone the target repo and let the security_auditor agent
# scan for vulnerability patterns.
# ---------------------------------------------------------------------------


def _build_security_prompt(
    bot: CaiBot, repo: str, workspace: Path, unknown: list[str]
) -> str:
    """Clone ``repo`` into ``workspace`` and build a prompt for security scanning."""
    print(f"Cloning {repo} into {workspace} for security audit...", file=sys.stderr)
    _clone_repo_for_audit(bot, repo, workspace)

    prompt = (
        f"Security audit of {repo}. The repository is checked out at the "
        f"agent's filesystem root — use `filesystem_read` to inspect files "
        f"and delegate broad searches (e.g. 'find all uses of shell=True', "
        f"'locate every call to pickle.load', 'search for hardcoded API keys "
        f"or tokens') to the `explore` subagent.\n\n"
        f"Scan for: hardcoded credentials/tokens, unsafe subprocess with "
        f"shell=True, path traversal, command/SQL injection, use of "
        f"eval/exec, insecure tempfile usage, missing TLS cert verification, "
        f"overly permissive file permissions, unsafe deserialization via "
        f"pickle/yaml.load.\n\n"
        f"Return an AuditOutput. Be conservative — only propose issues for "
        f"vulnerabilities that are real and reachable, not for test-only "
        f"patterns or code that is already properly sanitised."
    )
    if unknown:
        prompt += f"\n\nAdditional context: {' '.join(unknown)}"
    return prompt


# ---------------------------------------------------------------------------
# Deps mode — clone the target repo, parse pyproject.toml dependencies,
# query PyPI for upgrade-worthy versions, and feed the deps_auditor agent
# pre-fetched changelog/usage context.
# ---------------------------------------------------------------------------


def _build_deps_prompt(
    bot: CaiBot, repo: str, workspace: Path, unknown: list[str]
) -> str:
    """Clone ``repo``, audit its dependencies against PyPI, and build a prompt."""
    print(f"Cloning {repo} into {workspace} for dependency audit...", file=sys.stderr)
    _clone_repo_for_audit(bot, repo, workspace)

    pyproject_path = workspace / "pyproject.toml"
    if not pyproject_path.exists():
        print("No pyproject.toml dependencies found.", file=sys.stderr)
        sys.exit(0)

    try:
        pyproject = tomllib.loads(pyproject_path.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        print("No pyproject.toml dependencies found.", file=sys.stderr)
        sys.exit(0)

    project = pyproject.get("project") or {}
    raw_deps = project.get("dependencies") or []
    if not raw_deps:
        print("No pyproject.toml dependencies found.", file=sys.stderr)
        sys.exit(0)

    dep_re = re.compile(r"^\s*([A-Za-z0-9_.\-]+)\s*>=\s*([0-9]+(?:\.[0-9]+)*)\s*$")
    parsed: list[tuple[str, str]] = []
    for entry in raw_deps:
        if not isinstance(entry, str):
            continue
        match = dep_re.match(entry)
        if match:
            parsed.append((match.group(1), match.group(2)))

    def _version_tuple(v: str) -> tuple[int, ...]:
        try:
            return tuple(int(part) for part in v.split("."))
        except ValueError:
            return ()

    def _delta(lower_t: tuple[int, ...], latest_t: tuple[int, ...]) -> str:
        width = max(len(lower_t), len(latest_t))
        a = lower_t + (0,) * (width - len(lower_t))
        b = latest_t + (0,) * (width - len(latest_t))
        return ".".join(str(b[i] - a[i]) for i in range(width))

    def _fetch_pypi(name: str) -> dict | None:
        url = f"https://pypi.org/pypi/{name}/json"
        try:
            with urllib.request.urlopen(url, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            print(
                f"Warning: PyPI HTTP {exc.code} for {name}: {exc.reason}",
                file=sys.stderr,
            )
        except urllib.error.URLError as exc:
            print(f"Warning: PyPI fetch failed for {name}: {exc.reason}", file=sys.stderr)
        except json.JSONDecodeError as exc:
            print(f"Warning: PyPI JSON decode failed for {name}: {exc}", file=sys.stderr)
        return None

    outdated: list[dict] = []
    for name, lower in parsed:
        info = _fetch_pypi(name)
        if info is None:
            continue
        latest = (info.get("info") or {}).get("version")
        if not latest:
            continue
        lower_t = _version_tuple(lower)
        latest_t = _version_tuple(latest)
        if not lower_t or not latest_t or latest_t <= lower_t:
            continue
        outdated.append({
            "name": name,
            "lower": lower,
            "latest": latest,
            "lower_t": lower_t,
            "latest_t": latest_t,
            "info": info.get("info") or {},
            "releases": info.get("releases") or {},
        })

    if not outdated:
        print("No outdated dependencies found.", file=sys.stderr)
        sys.exit(0)

    table_rows = [
        f"{'Package':<25} {'Lower bound':<14} {'Latest':<14} {'Δ':<10}",
        "-" * 65,
    ]
    for pkg in outdated:
        table_rows.append(
            f"{pkg['name']:<25} {pkg['lower']:<14} {pkg['latest']:<14} "
            f"{_delta(pkg['lower_t'], pkg['latest_t']):<10}"
        )
    outdated_section = "\n".join(table_rows)

    diff_blocks: list[str] = []
    for pkg in outdated:
        intermediate: list[tuple[tuple[int, ...], str]] = []
        for ver in pkg["releases"]:
            ver_t = _version_tuple(ver)
            if not ver_t:
                continue
            if ver_t <= pkg["lower_t"] or ver_t > pkg["latest_t"]:
                continue
            # Major/minor only: patch component is 0, or fewer than 3 parts.
            if len(ver_t) < 3 or ver_t[2] == 0:
                intermediate.append((ver_t, ver))
        intermediate.sort()
        if len(intermediate) > 10:
            intermediate = intermediate[-10:]

        if intermediate:
            version_lines = "\n".join(f"    - {v}" for _, v in intermediate)
        else:
            version_lines = "    (no intermediate major/minor releases found)"

        project_urls = pkg["info"].get("project_urls") or {}
        changelog_url: str | None = None
        for key in ("Changelog", "Release notes", "Changes", "ChangeLog"):
            if key in project_urls:
                changelog_url = project_urls[key]
                break
        if changelog_url is None:
            for key, value in project_urls.items():
                if "changelog" in key.lower() or "release" in key.lower():
                    changelog_url = value
                    break

        changelog_line = (
            f"  changelog: {changelog_url}"
            if changelog_url
            else "  changelog: (no changelog URL declared in PyPI metadata)"
        )

        diff_blocks.append(
            f"- {pkg['name']} {pkg['lower']} → {pkg['latest']}\n"
            f"  intermediate major/minor releases:\n{version_lines}\n"
            f"{changelog_line}"
        )

    usage_blocks: list[str] = []
    for pkg in outdated:
        names_to_search = [pkg["name"]]
        underscore_variant = pkg["name"].replace("-", "_")
        if underscore_variant != pkg["name"]:
            names_to_search.append(underscore_variant)

        hits: list[str] = []
        for needle in names_to_search:
            try:
                result = subprocess.run(
                    ["grep", "-rn", "--include=*.py", needle, "."],
                    cwd=workspace,
                    capture_output=True,
                    text=True,
                )
            except OSError as exc:
                print(f"Warning: grep failed for {needle}: {exc}", file=sys.stderr)
                continue
            for line in result.stdout.splitlines():
                if line and line not in hits:
                    hits.append(line)
                    if len(hits) >= 3:
                        break
            if len(hits) >= 3:
                break

        if hits:
            usage_lines = "\n".join(f"    {h}" for h in hits[:3])
            usage_blocks.append(f"- {pkg['name']}:\n{usage_lines}")
        else:
            usage_blocks.append(f"- {pkg['name']}: No codebase usage found.")

    prompt = (
        f"Dependency audit of {repo}. The repository is checked out at the "
        f"agent's filesystem root.\n\n"
        f"## Outdated Dependencies\n\n"
        + outdated_section
        + "\n\n## Version Diffs\n\n"
        + "\n\n".join(diff_blocks)
        + "\n\n## Codebase Usage\n\n"
        + "\n\n".join(usage_blocks)
        + "\n\n## Instructions\n\n"
        "Use `filesystem_read` to inspect specific files for deeper context, "
        "`web_fetch` to read changelog URLs and release notes, and the "
        "`explore` subagent for broad searches across the codebase. Return "
        "an `AuditOutput` and be conservative — only propose upgrades that "
        "materially impact the codebase (breaking changes, deprecations, "
        "security fixes, or significant new capabilities)."
    )
    if unknown:
        prompt += f"\n\nAdditional context: {' '.join(unknown)}"
    return prompt


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="cai-audit",
        description="Run the audit agent against traces or code, and open GitHub issues.",
    )
    parser.add_argument(
        "--repo",
        required=True,
        help="Target GitHub repository for creating issues, e.g., owner/repo.",
    )
    parser.add_argument(
        "--mode",
        choices=["cost", "errors", "duplication", "architecture", "security", "deps"],
        default="cost",
        help=(
            "Audit mode: 'cost' analyses the most expensive of the last 10 "
            "issue-solving sessions; 'errors' analyses the 10 most recent "
            "traces that contain error-level observations; 'duplication' "
            "clones the repo and audits jscpd copy-paste findings; "
            "'architecture' clones the repo and audits structural health; "
            "'security' clones the repo and audits for common vulnerability "
            "patterns; 'deps' clones the repo, checks PyPI for outdated "
            "dependencies, and audits upgrade-worthiness."
        ),
    )
    args, unknown = parser.parse_known_args()

    setup_langfuse()

    bot = CaiBot()
    workspace: Path | None = None
    workspace_root: Path | None = None
    agent_name = "audit"

    try:
        if args.mode == "cost":
            prompt = _build_cost_prompt(unknown)
        elif args.mode == "errors":
            prompt = _build_errors_prompt(unknown)
        elif args.mode == "architecture":
            workspace_root = Path(tempfile.mkdtemp(prefix="cai-audit-arch-"))
            workspace = workspace_root / "repo"
            agent_name = "architecture_auditor"
            prompt = _build_architecture_prompt(bot, args.repo, workspace, unknown)
        elif args.mode == "security":
            workspace_root = Path(tempfile.mkdtemp(prefix="cai-audit-sec-"))
            workspace = workspace_root / "repo"
            agent_name = "security_auditor"
            prompt = _build_security_prompt(bot, args.repo, workspace, unknown)
        elif args.mode == "deps":
            workspace_root = Path(tempfile.mkdtemp(prefix="cai-audit-deps-"))
            workspace = workspace_root / "repo"
            agent_name = "deps_auditor"
            prompt = _build_deps_prompt(bot, args.repo, workspace, unknown)
        else:
            workspace_root = Path(tempfile.mkdtemp(prefix="cai-audit-dup-"))
            workspace = workspace_root / "repo"
            agent_name = "duplication_auditor"
            prompt = _build_duplication_prompt(bot, args.repo, workspace, unknown)

        state = AuditState(
            bot=bot,
            repo=args.repo,
            prompt=prompt,
            agent_name=agent_name,
            workspace=workspace,
        )

        from cai.workflows.registry import by_slug, CliArgs
        cli_args = CliArgs(repo=args.repo)
        session_id = by_slug("audit").session_id(cli_args)

        async def _run() -> None:
            with langfuse_workflow(
                "cai-audit",
                metadata={"repo": args.repo, "mode": args.mode},
                session_id=session_id,
            ):
                await audit_graph.run(RunAuditNode(), state=state)

        asyncio.run(_run())
    finally:
        if workspace_root is not None:
            shutil.rmtree(workspace_root, ignore_errors=True)


if __name__ == "__main__":
    main()
