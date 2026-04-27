from __future__ import annotations

from pydantic_graph import BaseNode, End, GraphRunContext

from cai.git import commit, push_branch, stage_all
from cai.github.issues import IssueMeta
from cai.github.pr import create_pull_request
from cai.workflows.state import IssueState


class PRNode(BaseNode[IssueState]):
    async def run(self, ctx: GraphRunContext[IssueState]) -> End[IssueMeta]:
        state = ctx.state
        assert state.new_meta is not None
        assert state.implement_output is not None
        assert state.branch_name is not None

        stage_all(state.repo_root)
        commit(
            state.repo_root,
            state.implement_output.commit_message,
            author_name="cai-bot",
            author_email="cai-bot@users.noreply.github.com",
        )

        token = state.bot.token_for(state.new_meta.repo)
        remote_url = (
            f"https://x-access-token:{token}@github.com/{state.new_meta.repo}.git"
        )
        push_branch(
            state.repo_root,
            remote_url,
            state.branch_name,
            env={"GIT_TERMINAL_PROMPT": "0"},
        )

        pr_body = state.body_path.read_text()
        pr_url = create_pull_request(
            state.bot,
            state.new_meta.repo,
            title=state.new_meta.title,
            body=pr_body,
            head=state.branch_name,
        )
        state.pr_url = pr_url
        return End(state.new_meta)
