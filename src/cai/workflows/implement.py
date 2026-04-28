from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from git import Repo
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.usage import UsageLimits
from pydantic_deep import DeepAgentDeps, LocalBackend
from pydantic_graph import BaseNode, GraphRunContext

from cai.agents.loader import AGENT_DIR, build_deep_agent, parse_agent_md
from cai.git import checkout_branch
from cai.github.pr import ReviewThread
from cai.workflows.state import ImplementOutput, IssueState

AGENT_DEFINITION = AGENT_DIR / "implement.md"


@lru_cache(maxsize=1)
def _implement_agent():
    config, instructions = parse_agent_md(AGENT_DEFINITION)
    agent = build_deep_agent(config, instructions, output_type=ImplementOutput)

    @agent.output_validator
    async def _fix_must_edit(ctx, output: ImplementOutput) -> ImplementOutput:
        # When any reply claims action='fix', the working tree must show
        # changes — otherwise the bundled commit would carry no edits and
        # the resolve step would mark threads resolved against nothing.
        if not any(r.action == "fix" for r in output.replies):
            return output
        repo_root = Path(ctx.deps.backend.root_dir)
        if not Repo(str(repo_root)).is_dirty(
            index=True, working_tree=True, untracked_files=True
        ):
            raise ModelRetry(
                "One or more replies use action='fix' but the working tree "
                "has no changes. Either invoke write_file or hashline_edit "
                "to actually make the change, or switch those replies to "
                "action='reply_only'."
            )
        return output

    return agent


def _deps(repo_root: Path) -> DeepAgentDeps:
    return DeepAgentDeps(
        backend=LocalBackend(
            root_dir=str(repo_root),
            allowed_directories=[str(repo_root)],
        )
    )


def _branch_name(number: int) -> str:
    return f"cai/solve-{number}"


def _format_threads_section(
    threads: list[ReviewThread], prior_corrections: list[ReviewThread]
) -> str:
    sections: list[str] = ["## Review threads to address"]
    sections.append(
        "Each thread below needs a `replies` entry in your output. Set "
        "`action='fix'` when you edit code for that thread, `'reply_only'` "
        "when you push back without editing. The commit you describe via "
        "`commit_message` bundles edits for every thread you fixed."
    )
    for t in threads:
        line = t.line if t.line is not None else "(line unknown)"
        history = "\n\n".join(
            f"**{c.author}** ({c.created_at}):\n{c.body}" for c in t.comments
        )
        sections.append(
            f"### Thread `{t.id}` — `{t.path}` at line {line}\n\n"
            f"```diff\n{t.diff_hunk}\n```\n\n{history}"
        )
    if prior_corrections:
        rendered_lines: list[str] = []
        for t in prior_corrections:
            head = t.comments[0]
            bot_replies = [c for c in t.comments[1:] if c.author.endswith("[bot]")]
            line = f"- `{t.path}` — **{head.author}**: {head.body.strip()}"
            if bot_replies:
                last = bot_replies[-1]
                line += f"\n  - **{last.author}** (resolved): {last.body.strip()}"
            rendered_lines.append(line)
        sections.append(
            "## Prior corrections (resolved threads on this PR)\n\n"
            "Do not undo these — the fixes already landed.\n\n"
            + "\n\n".join(rendered_lines)
        )
    return "\n\n".join(sections)


class ImplementNode(BaseNode[IssueState]):
    async def run(self, ctx: GraphRunContext[IssueState]) -> TestNode:
        from cai.workflows.test_runner import TestNode

        state = ctx.state
        assert state.new_meta is not None
        assert state.new_meta.number is not None

        if state.branch_name is None:
            branch = _branch_name(state.new_meta.number)
            checkout_branch(state.repo_root, branch)
            state.branch_name = branch

        body = state.body_path.read_text()
        meta_json = state.new_meta.model_dump_json(indent=2)

        prompt = (
            "Implement the code changes described in this GitHub issue.\n\n"
            "Make all necessary changes to fully resolve the issue according to the plan.\n"
            "Return:\n"
            "- summary: a concise description of the changes you made\n"
            "- commit_message: a clear commit message for these changes\n"
            "- required_checks: list of checks needed for this MR (e.g. ['documentation'])\n"
            "- replies: per-thread replies — leave empty unless review threads are listed below\n\n"
            f"## Issue metadata\n\n{meta_json}\n\n"
            f"## Issue body (implementation plan)\n\n{body}"
        )
        reference_section = state.reference_files_section()
        if reference_section:
            prompt += "\n\n" + reference_section
        if state.review_threads:
            prompt += "\n\n" + _format_threads_section(
                state.review_threads, state.prior_corrections
            )
        if state.test_failure_details:
            prompt += (
                "\n\n## Test failures to fix\n\n"
                "The previous implementation attempt failed the test suite. "
                "Fix the code so these tests pass:\n\n"
                f"```\n{state.test_failure_details}\n```"
            )
        result = await _implement_agent().run(
            prompt,
            deps=_deps(state.repo_root),
            usage_limits=UsageLimits(request_limit=100),
        )
        state.implement_output = result.output
        return TestNode()
