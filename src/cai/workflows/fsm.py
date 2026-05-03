from __future__ import annotations

import asyncio

from pydantic_graph import Graph
from pydantic_ai.exceptions import AgentRunError

from cai.github.bot import CaiBot
from cai.github.issues import IssueMeta
from cai.github.labels import CAI_LABEL_SPECS, ensure_labels, set_label
from cai.github.pr import list_resolved_threads, list_unresolved_threads
from cai.github.repo import IssueWorkspace, PRWorkspace
from cai.log import langfuse_workflow
from cai.workflows.docs import DocsNode
from cai.workflows.explore import ExploreNode
from cai.workflows.implement import ImplementNode
from cai.workflows.merge_eval import MergeEvaluationNode
from cai.workflows.pr import PRNode
from cai.workflows.python_review import PythonReviewNode
from cai.workflows.github_workflow_review import GitHubWorkflowReviewNode
from cai.workflows.pydantic_ai_review import PydanticAIReviewNode
from cai.workflows.refine import RefineNode
from cai.workflows.state import IssueState, load_session_state
from cai.workflows.test_runner import TestNode, TestSanityNode

solve_graph: Graph[IssueState, None, IssueMeta] = Graph(
    nodes=[ExploreNode, RefineNode, ImplementNode, TestNode, PythonReviewNode, GitHubWorkflowReviewNode, PydanticAIReviewNode, TestSanityNode, DocsNode, PRNode, MergeEvaluationNode]
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
    state.session_state = load_session_state(state.body_path.parent)
    issue_ref = f"{meta.repo}#{meta.number}" if meta.number else meta.repo
    session_id = None
    if meta.number is not None:
        from cai.workflows.registry import by_slug, CliArgs
        cli_args = CliArgs(repo=meta.repo, number=meta.number)
        session_id = by_slug("solve").session_id(cli_args)
    async def _run():
        with langfuse_workflow(
            "cai-solve",
            input={"issue": issue_ref, "title": meta.title},
            metadata={"repo": meta.repo, "issue_number": meta.number},
            session_id=session_id,
        ):
            await solve_graph.run(ExploreNode(), state=state)

    try:
        asyncio.run(_run())
        assert state.new_meta is not None
        if meta.number is not None:
            ensure_labels(bot, meta.repo, CAI_LABEL_SPECS)
            issue = bot.repo(meta.repo).get_issue(meta.number)
            labels = [lbl.name for lbl in issue.labels if lbl.name != "cai:raised"]
            if not (state.refine_output and state.refine_output.sub_issues):
                outcome = "cai:pr-ready" if state.pr_url else "cai:failed"
                labels.append(outcome)
            issue.edit(labels=labels)
            state.new_meta.labels = labels
    except Exception:
        if meta.number is not None:
            ensure_labels(bot, meta.repo, CAI_LABEL_SPECS)
            issue = bot.repo(meta.repo).get_issue(meta.number)
            labels = [lbl.name for lbl in issue.labels if lbl.name != "cai:raised"]
            labels.append("cai:failed")
            issue.edit(labels=labels)
        raise
    if state.pr_number is not None:
        if not state.auto_merge_enabled:
            set_label(bot, meta.repo, state.pr_number, "cai:human-review", present=True)
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
    set_label(bot, workspace.repo, workspace.number, "cai:human-review", present=False)
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
    from cai.workflows.registry import by_slug, CliArgs
    cli_args = CliArgs(repo=workspace.repo, number=workspace.number, branch=workspace.head_branch)
    _pr_session_id = by_slug("solve-pr").session_id(cli_args)
    async def _run():
        with langfuse_workflow(
            "cai-solve",
            input={"pr": pr_ref, "title": workspace.title, "branch": workspace.head_branch},
            metadata={"repo": workspace.repo, "pr_number": workspace.number},
            session_id=_pr_session_id,
        ):
            await solve_graph.run(ImplementNode(), state=state)

    asyncio.run(_run())
    if not state.auto_merge_enabled:
        set_label(bot, workspace.repo, workspace.number, "cai:human-review", present=True)
    return meta

