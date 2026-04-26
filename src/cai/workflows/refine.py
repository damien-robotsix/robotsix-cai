from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_ai.usage import UsageLimits
from pydantic_deep import DeepAgentDeps, LocalBackend
from pydantic_graph import BaseNode, End, GraphRunContext

from cai.agents.loader import AGENT_DIR, build_deep_agent, parse_agent_md
from cai.github.issues import IssueMeta, add_sub_issue, push
from cai.workflows.state import IssueState, RefineOutput

_MAX_FILE_BYTES = 100_000

AGENT_DEFINITION = AGENT_DIR / "refine.md"


@lru_cache(maxsize=1)
def refine_agent():
    config, instructions = parse_agent_md(AGENT_DEFINITION)
    return build_deep_agent(config, instructions, output_type=RefineOutput)


def _refine_deps(body_path: Path) -> DeepAgentDeps:
    issue_dir = str(body_path.parent)
    return DeepAgentDeps(
        backend=LocalBackend(
            root_dir=issue_dir,
            allowed_directories=[issue_dir],
        )
    )


def _load_related_files(paths: list[str], repo_root: Path) -> list[str]:
    sections: list[str] = []
    for path_str in paths:
        p = Path(path_str)
        if not p.is_absolute():
            p = repo_root / p
        try:
            p = p.resolve()
            if not p.is_file():
                continue
            if p.stat().st_size > _MAX_FILE_BYTES:
                continue
            rel = p.relative_to(repo_root)
            sections.append(f"### {rel}\n\n```\n{p.read_text()}\n```")
        except (ValueError, OSError):
            pass
    return sections


class RefineNode(BaseNode[IssueState]):
    async def run(self, ctx: GraphRunContext[IssueState]) -> End[IssueMeta]:
        state = ctx.state
        assert state.findings is not None

        file_sections = _load_related_files(state.findings.related_files, state.repo_root)

        issue_dir = state.body_path.parent
        prompt = (
            f"Refine this GitHub issue.\n\n"
            f"The body file is at {state.body_path} — use Write or Edit to rewrite it in place.\n"
            f"Sub-issue bodies (if any) go in the same directory as sibling files: "
            f"{issue_dir}/sub_issue_0.md, {issue_dir}/sub_issue_1.md, …\n\n"
            f"## Metadata\n\n{state.meta_json}\n\n"
            f"## Current body\n\n{state.body}\n\n"
            f"## Codebase findings (explore agent)\n\n{state.findings.summary}"
        )
        if file_sections:
            prompt += "\n\n## Related files\n\n" + "\n\n".join(file_sections)

        result = await refine_agent().run(
            prompt,
            deps=_refine_deps(state.body_path),
            usage_limits=UsageLimits(request_limit=10),
        )
        out: RefineOutput = result.output
        new_meta = state.meta.model_copy(update={"title": out.title})
        state.new_meta = new_meta
        state.refine_output = out

        json_path = state.body_path.with_suffix(".json")
        json_path.write_text(new_meta.model_dump_json(indent=2) + "\n")
        push(state.bot, json_path)

        assert new_meta.number is not None
        for idx, sub_title in enumerate(out.sub_issues):
            sub_meta = IssueMeta(repo=new_meta.repo, title=sub_title)
            sub_base = state.body_path.parent / f"sub_issue_{idx}"
            sub_json = sub_base.with_suffix(".json")
            sub_md = sub_base.with_suffix(".md")
            sub_json.write_text(sub_meta.model_dump_json(indent=2) + "\n")
            if not sub_md.exists():
                sub_md.write_text("## Sub-task\n\nAutomatically generated sub-task from refinement.\n")
            created = push(state.bot, sub_json)
            add_sub_issue(state.bot, new_meta.repo, new_meta.number, created.id)

        return End(new_meta)
