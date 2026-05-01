"""Parent-check workflow — runs when a sub-issue is closed and checks parent completion."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from pydantic_deep import DeepAgentDeps, LocalBackend
from pydantic_graph import BaseNode, End, Graph, GraphRunContext

from cai.agents.loader import build_deep_agent, parse_agent_md, resolve_agent_path
from cai.github.bot import CaiBot
from cai.github.issues import (
    IssueMeta,
    add_sub_issue,
    get_parent_issue,
    list_sub_issues,
    push,
)
from cai.github.labels import set_label
from cai.github.repo import parse_issue_ref
from cai.log.observability import langfuse_workflow
from cai.workflows.state import _inline_refs


class ParentCheckOutput(BaseModel):
    """Structured output from the parent-verifier agent."""

    all_fulfilled: bool = Field(
        description=(
            "Whether every requirement in the parent issue has been addressed "
            "by the closed sub-issues."
        ),
    )
    reason: str = Field(
        description="Explanation of why the parent is or is not complete."
    )
    new_sub_issues: list[str] = Field(
        default_factory=list,
        description=(
            "Titles of new sub-issues to create for any remaining work not yet "
            "covered. Empty when all_fulfilled is True."
        ),
    )

    @classmethod
    def model_json_schema(cls, **kwargs: object) -> dict:
        return _inline_refs(super().model_json_schema(**kwargs))


@dataclass
class ParentCheckState:
    bot: CaiBot
    repo: str
    sub_issue_number: int
    parent_number: int | None = None
    parent_body: str = ""
    sub_issues_summary: str = ""
    output: ParentCheckOutput | None = None


@lru_cache(maxsize=1)
def parent_verifier_agent() -> Any:
    config, instructions = parse_agent_md(resolve_agent_path("parent_verifier"))
    return build_deep_agent(
        config, instructions, output_type=ParentCheckOutput
    )


def _scratch_deps() -> DeepAgentDeps:
    """Minimal deps for the parent-verifier agent (no repo clone needed)."""
    scratch = Path(tempfile.mkdtemp(prefix="parent-check-"))
    return DeepAgentDeps(
        backend=LocalBackend(
            root_dir=str(scratch),
            allowed_directories=[str(scratch)],
        )
    )


class FetchParentNode(BaseNode[ParentCheckState]):
    async def run(
        self, ctx: GraphRunContext[ParentCheckState],
    ) -> End[None] | "VerifyParentNode":
        state = ctx.state

        parent_number = get_parent_issue(
            state.bot, state.repo, state.sub_issue_number
        )
        if parent_number is None:
            return End(None)

        siblings = list_sub_issues(state.bot, state.repo, parent_number)
        if any(sib.get("state") != "closed" for sib in siblings):
            return End(None)

        parent_body = (
            state.bot.repo(state.repo).get_issue(parent_number).body or ""
        )

        lines: list[str] = []
        for sib in siblings:
            title = sib.get("title", "(untitled)")
            state_reason = sib.get("state_reason", "")
            state_str = sib.get("state", "")
            extra = f" ({state_reason})" if state_reason else ""
            lines.append(
                f"- #{sib.get('number', '?')} **{title}** — {state_str}{extra}"
            )
        sub_issues_summary = "\n".join(lines)

        state.parent_number = parent_number
        state.parent_body = parent_body
        state.sub_issues_summary = sub_issues_summary

        return VerifyParentNode()


class VerifyParentNode(BaseNode[ParentCheckState]):
    async def run(
        self, ctx: GraphRunContext[ParentCheckState],
    ) -> End[None]:
        state = ctx.state
        assert state.parent_number is not None

        prompt = (
            f"Verify whether the following parent issue's requirements have "
            f"been fulfilled by its closed sub-issues.\n\n"
            f"## Parent issue body\n\n{state.parent_body}\n\n"
            f"## Closed sub-issues\n\n{state.sub_issues_summary}\n\n"
            f"Determine if every plan step in the parent issue has been "
            f"addressed by the closed sub-issues. If any steps remain "
            f"unaddressed, provide titles for new sub-issues that would "
            f"cover the gap."
        )

        result = await parent_verifier_agent().run(prompt, deps=_scratch_deps())
        out: ParentCheckOutput = result.output
        state.output = out

        if out.all_fulfilled:
            issue = state.bot.repo(state.repo).get_issue(state.parent_number)
            issue.edit(state="closed", state_reason="completed")
            set_label(
                state.bot, state.repo, state.parent_number, "cai:raised", False
            )
            set_label(
                state.bot, state.repo, state.parent_number, "cai:pr-ready", True
            )
        else:
            for idx, sub_title in enumerate(out.new_sub_issues):
                sub_labels = ["cai:sub-issue"]
                if idx == 0:
                    sub_labels.append("cai:raised")
                sub_meta = IssueMeta(
                    repo=state.repo, title=sub_title, labels=sub_labels
                )
                with tempfile.TemporaryDirectory() as td:
                    td_path = Path(td)
                    sub_json = td_path / f"sub_issue_{idx}.json"
                    sub_md = td_path / f"sub_issue_{idx}.md"
                    sub_json.write_text(
                        sub_meta.model_dump_json(indent=2) + "\n"
                    )
                    sub_md.write_text(
                        "## Sub-task\n\n"
                        "Automatically generated sub-task from parent "
                        "verification.\n"
                    )
                    created = push(state.bot, sub_json)
                    add_sub_issue(
                        state.bot,
                        state.repo,
                        state.parent_number,
                        created.id,
                    )

        return End(None)


parent_check_graph = Graph(nodes=[FetchParentNode, VerifyParentNode])


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="cai-parent-check",
        description=(
            "Check whether a parent issue's requirements have been "
            "fulfilled by its closed sub-issues, closing the parent or "
            "creating new sub-issues as needed."
        ),
    )
    parser.add_argument(
        "ref",
        help="Issue reference, formatted as owner/repo#number.",
    )
    args = parser.parse_args()

    parsed = parse_issue_ref(args.ref)
    if parsed is None:
        parser.error(f"expected owner/repo#number, got {args.ref!r}")
    repo, number = parsed

    bot = CaiBot()
    state = ParentCheckState(bot=bot, repo=repo, sub_issue_number=number)

    from cai.workflows.registry import CliArgs, by_slug  # local — avoids circular import

    session_id = by_slug("parent-check").session_id(
        CliArgs(repo=repo, number=number)
    )

    with langfuse_workflow(
        "cai-parent-check",
        session_id=session_id,
        input={"ref": args.ref},
    ):
        asyncio.run(parent_check_graph.run(FetchParentNode(), state=state))

    output_summary: dict[str, Any] = {
        "parent_check": True,
        "repo": repo,
        "sub_issue": number,
    }
    if state.output is not None:
        output_summary["output"] = state.output.model_dump()
    json.dump(output_summary, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
