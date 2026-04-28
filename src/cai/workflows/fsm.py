from __future__ import annotations

from pydantic_graph import Graph

from cai.github.bot import CaiBot
from cai.github.issues import IssueMeta
from cai.github.repo import IssueWorkspace
from cai.log import langfuse_workflow
from cai.workflows.explore import ExploreNode
from cai.workflows.implement import ImplementNode
from cai.workflows.pr import PRNode
from cai.workflows.refine import RefineNode
from cai.workflows.state import IssueState

solve_graph: Graph[IssueState, None, IssueMeta] = Graph(
    nodes=[ExploreNode, RefineNode, ImplementNode, DocsNode, PRNode]
)


def solve_issue(bot: CaiBot, workspace: IssueWorkspace) -> tuple[IssueMeta, str]:
    """Refine the issue, implement the fix, and open a pull request.

    Returns the refined issue metadata and the PR URL.
    """
    meta = IssueMeta.model_validate_json(workspace.issue_json.read_text())
    state = IssueState(
        bot=bot,
        meta=meta,
        body_path=workspace.issue_md.resolve(),
        repo_root=workspace.repo_root.resolve(),
    )
    issue_ref = f"{meta.repo}#{meta.number}" if meta.number else meta.repo
    with langfuse_workflow(
        "cai-solve",
        input={"issue": issue_ref, "title": meta.title},
        metadata={"repo": meta.repo, "issue_number": meta.number},
    ):
        solve_graph.run_sync(ExploreNode(), state=state)
    assert state.new_meta is not None
    assert state.pr_url is not None
    return state.new_meta, state.pr_url
 state.new_meta, state.pr_url
