from __future__ import annotations

from functools import lru_cache

from pydantic_ai.usage import UsageLimits
from pydantic_graph import BaseNode, End, GraphRunContext

from cai.agents.loader import build_deep_agent, parse_agent_md, resolve_agent_path

AGENT_DEFINITION = resolve_agent_path("refine")


@lru_cache(maxsize=1)
def refine_agent():
    config, instructions = parse_agent_md(AGENT_DEFINITION)
    return build_deep_agent(config, instructions, output_type=RefineOutput)


class RefineNode(BaseNode[IssueState]):
    async def run(self, ctx: GraphRunContext[IssueState]) -> ImplementNode | End[IssueMeta]:
        state = ctx.state
        assert state.findings is not None

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
        reference_section = state.reference_files_section()
        if reference_section:
            prompt += "\n\n" + reference_section

        # Refine writes the body file (and any sub_issue_*.md/.json
        # siblings) — nothing else. Globbing top-level files in the
        # issue dir excludes the cloned ``repo/`` and the spike scratch
        # dir, both of which sit under the same parent.
        result = await refine_agent().run(
            prompt,
            deps=repo_deps(
                state.repo_root,
                write_globs=[
                    f"{issue_dir}/*.md",
                    f"{issue_dir}/*.json",
                ],
            ),
            usage_limits=UsageLimits(request_limit=50),
        )
        out: RefineOutput = result.output
        new_meta = state.meta.model_copy(update={"title": out.title})
        state.new_meta = new_meta
        state.refine_output = out
        state.reference_files = list(out.reference_files)

        json_path = state.body_path.with_suffix(".json")
        json_path.write_text(new_meta.model_dump_json(indent=2) + "\n")
        push(state.bot, json_path)

        assert new_meta.number is not None
        for idx, sub_title in enumerate(out.sub_issues):
            labels = ["cai:raised"] if idx == 0 else []
            sub_meta = IssueMeta(repo=new_meta.repo, title=sub_title, labels=labels)
            sub_base = state.body_path.parent / f"sub_issue_{idx}"
            sub_json = sub_base.with_suffix(".json")
            sub_md = sub_base.with_suffix(".md")
            sub_json.write_text(sub_meta.model_dump_json(indent=2) + "\n")
            if not sub_md.exists():
                sub_md.write_text("## Sub-task\n\nAutomatically generated sub-task from refinement.\n")
            created = push(state.bot, sub_json)
            add_sub_issue(state.bot, new_meta.repo, new_meta.number, created.id)

        if out.sub_issues:
            return End(new_meta)

        return ImplementNode()
