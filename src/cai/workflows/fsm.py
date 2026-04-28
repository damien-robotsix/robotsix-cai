from __future__ import annotations

from pydantic_graph import Graph

from cai.github.bot import CaiBot
from cai.github.issues import IssueMeta
from cai.github.pr import list_resolved_threads, list_unresolved_threads
from cai.github.repo import IssueWorkspace, PRWorkspace
from cai.log import langfuse_workflow, session_id_for_pr
from cai.workflows.docs import DocsNode
from cai.workflows.explore import ExploreNode
from cai.workflows.implement import ImplementNode
from cai.workflows.pr import PRNode
from cai.workflows.python_review import PythonReviewNode
from cai.workflows.refine import RefineNode
from cai.workflows.state import IssueState
from cai.workflows.test_runner import TestNode, TestSanityNode

solve_graph: Graph[IssueState, None, IssueMeta] = Graph(
    nodes=[ExploreNode, RefineNode, ImplementNode, TestNode, PythonReviewNode, TestSanityNode, DocsNode, PRNode]
)


def solve_issue(bot: CaiBot, workspace: IssueWorkspace) -> tuple[IssueMeta, str | None]:
    """Refine the issue, implement the fix, and open a pull request.

    Returns the refined issue metadata and the PR URL (or None if the graph ends early).
    """
    meta = IssueMeta.model_validate_json(workspace.issue_json.read_text())
    state = IssueState(
        bot=bot,
        meta=meta,
        body_path=workspace.issue_md.resolve(),
        repo_root=workspace.repo_root.resolve(),
    )
    issue_ref = f"{meta.repo}#{meta.number}" if meta.number else meta.repo
    session_id = f"issue-{meta.number}" if meta.number else None
    with langfuse_workflow(
        "cai-solve",
        input={"issue": issue_ref, "title": meta.title},
        metadata={"repo": meta.repo, "issue_number": meta.number},
        session_id=session_id,
    ):
        solve_graph.run_sync(ExploreNode(), state=state)
    assert state.new_meta is not None
    return state.new_meta, state.pr_url


def solve_pr(bot: CaiBot, workspace: PRWorkspace) -> IssueMeta:
    """Address review threads on a PR via the same graph, entered at ImplementNode.

    The PR's head branch is already checked out by ``prepare_pr_workspace``;
    the implement agent receives the unresolved threads in its prompt and
    returns per-thread replies plus a single bundled commit. ``PRNode``
    pushes the commit and posts/resolves on the existing PR — no new PR is
    opened.
    """
    meta = IssueMeta(
        repo=workspace.repo,
        number=workspace.number,
        title=workspace.title,
    )
    threads = list_unresolved_threads(bot, workspace.repo, workspace.number)
    prior = list_resolved_threads(bot, workspace.repo, workspace.number)
    state = IssueState(
        bot=bot,
        meta=meta,
        body_path=workspace.body_path.resolve(),
        repo_root=workspace.repo_root.resolve(),
        branch_name=workspace.head_branch,
        review_threads=threads,
        prior_corrections=prior,
        pr_number=workspace.number,
    )
    state.new_meta = meta
    pr_ref = f"{workspace.repo}#{workspace.number}"
    with langfuse_workflow(
        "cai-solve",
        input={"pr": pr_ref, "title": workspace.title, "branch": workspace.head_branch},
        metadata={"repo": workspace.repo, "pr_number": workspace.number},
        session_id=session_id_for_pr(workspace.number, workspace.head_branch),
    ):
        solve_graph.run_sync(ImplementNode(), state=state)
    return meta
