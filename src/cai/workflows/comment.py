"""Analysis-flow terminal node: post the refined body as a comment.

Reached only when ``IssueState.flow_kind == "analysis"`` (set by
``cai-solve`` after reading the ``Type`` field from the issue's GitHub
Project item). The comment target is the parent issue when the source is
a sub-issue, otherwise the source issue itself. The source issue is then
closed with the ``cai:resolved`` label.
"""
from __future__ import annotations

from pydantic_graph import BaseNode, End, GraphRunContext

from cai.github.issues import IssueMeta, get_parent_issue
from cai.github.labels import CAI_LABEL_SPECS, ensure_labels
from cai.workflows.state import IssueState


class CommentNode(BaseNode[IssueState, None, IssueMeta]):
    async def run(self, ctx: GraphRunContext[IssueState]) -> End[IssueMeta]:
        state = ctx.state
        assert state.new_meta is not None
        assert state.new_meta.number is not None

        body = state.body_path.read_text()

        source_repo = state.new_meta.repo
        source_number = state.new_meta.number

        parent = get_parent_issue(state.bot, source_repo, source_number)
        target_number = parent if parent is not None else source_number

        repo_obj = state.bot.repo(source_repo)
        target_issue = repo_obj.get_issue(target_number)
        comment = target_issue.create_comment(body)
        state.comment_url = comment.html_url

        ensure_labels(state.bot, source_repo, CAI_LABEL_SPECS)
        source_issue = repo_obj.get_issue(source_number)
        labels = [
            lbl.name for lbl in source_issue.labels if lbl.name != "cai:raised"
        ]
        if "cai:resolved" not in labels:
            labels.append("cai:resolved")
        source_issue.edit(labels=labels, state="closed", state_reason="completed")
        state.new_meta.labels = labels
        state.new_meta.state = "closed"
        state.new_meta.state_reason = "completed"

        return End(state.new_meta)
