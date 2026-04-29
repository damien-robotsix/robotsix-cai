"""``cai-audit`` CLI: ask the audit agent to mine signals and file issues.

The pipeline runs as a graph: RunAuditNode → CreateIssuesNode. RunAuditNode
short-circuits to End when the agent proposes nothing. CreateIssuesNode
runs the issue-deduplicator agent per proposed item to decide whether to
create a new issue, append a comment to an existing one, or discard.

Three audit modes are supported:
  --mode cost         Audit the most costly session of the last 10 issue-solving runs.
  --mode errors       Audit the 10 most recent traces that contain error-level observations.
  --mode duplication  Audit copy-paste findings from jscpd against a fresh clone of the repo.

In every mode all signal context is pre-fetched into the prompt so the agent
can spend its tokens on judgement rather than tool plumbing.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import shutil
import subprocess
import sys
import tempfile
import typing
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
from cai.log.traces import _TRACES
from cai.workflows.state import WithConfidence, _inline_refs


class ProposedIssue(WithConfidence):
    title: str
    body: str
    last_detected_at: str | None = None  # ISO timestamp of the most recent relevant trace


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


@lru_cache(maxsize=2)
def _audit_agent(agent_name: str = "audit"):
    config, instructions = parse_agent_md(resolve_agent_path(agent_name))
    return build_deep_agent(config, instructions, output_type=AuditOutput)


@lru_cache(maxsize=1)
def _dedupe_agent():
    return load_agent_from_md(
        resolve_agent_path("issue_deduplicator"), output_type=DedupeOutput
    )


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


class CreateIssuesNode(BaseNode[AuditState, None, AuditOutput]):
    """Per proposed issue: check recent commits, dedupe, then create/append/discard."""

    async def run(self, ctx: GraphRunContext[AuditState]) -> End[AuditOutput]:
        assert ctx.state.output is not None
        repo_obj = ctx.state.bot.repo(ctx.state.repo)

        open_issues = repo_obj.get_issues(state="open")
        open_issues_summary = (
            "\n".join(f"#{issue.number}: {issue.title}" for issue in open_issues)
            or "No open issues."
        )
        dedupe_agent = _dedupe_agent()

        for issue in ctx.state.output.issues:
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
                target_issue.create_comment(
                    "**Additional proposed issue details:**\n\n"
                    f"**Title**: {issue.title}\n\n"
                    f"**Body**:\n{issue.body}"
                )
                continue

            if dedupe_decision.action == "append":
                print(
                    f"Warning: Deduplicator suggested appending '{issue.title}' "
                    f"but provided no target_issue_number. "
                    f"Reason: {dedupe_decision.reason}. "
                    "Falling back to creating a new issue.",
                    file=sys.stderr,
                )

            created = repo_obj.create_issue(
                title=issue.title,
                body=issue.body,
                labels=["cai:audit"],
            )
            print(f"Created: {created.html_url}")

        return End(ctx.state.output)


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
        "Set last_detected_at to the ISO timestamp of the relevant trace for each issue."
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

    lines = [f"Recent failures ({len(failures)} traces with errors):"]
    for f in failures:
        ts = (f["timestamp"] or "?")[:19]
        lines.append(f"\n[{ts}] {f['name']}  trace_id={f['id']}")
        for e in f["errors"]:
            lines.append(f"  Failed step: {e['name']}")
            if e.get("status_message"):
                lines.append(f"    Message: {e['status_message'][:300]}")
            if e.get("output"):
                lines.append(f"    Output:  {e['output'][:200]}")

    prompt = (
        "Audit the following recent failures in Langfuse traces.\n\n"
        + "\n".join(lines)
        + "\n\nDelegate deep inspection of specific traces to trace_analyst. "
        "Draft improvements as proposed issues. "
        "Set last_detected_at to the ISO timestamp of the most recent relevant "
        "failure for each issue."
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
        choices=["cost", "errors", "duplication"],
        default="cost",
        help=(
            "Audit mode: 'cost' analyses the most expensive of the last 10 "
            "issue-solving sessions; 'errors' analyses the 10 most recent "
            "traces that contain error-level observations; 'duplication' "
            "clones the repo and audits jscpd copy-paste findings."
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

        session_id = f"audit-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"

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
