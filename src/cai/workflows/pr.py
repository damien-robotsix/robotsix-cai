from __future__ import annotations

from pathlib import Path

from git import Repo
from pydantic_graph import BaseNode, GraphRunContext

from cai.git import commit, fetch, push_branch, rebase_abort, rebase_onto, stage_all
from cai.github.pr import (
    create_pull_request,
    reply_to_review_comment,
    resolve_review_thread,
)
from cai.workflows.merge_eval import MergeEvaluationNode
from cai.workflows.state import IssueState


def _has_staged_changes(repo_root: Path) -> bool:
    return Repo(str(repo_root)).is_dirty(
        index=True, working_tree=False, untracked_files=False
    )


def _bundled_commit_message(state: IssueState) -> str:
    assert state.implement_output is not None
    parts = [state.implement_output.commit_message]
    if state.test_output and state.test_output.commit_message:
        parts.append(state.test_output.commit_message)
    if state.python_review_output and state.python_review_output.commit_message:
        parts.append(state.python_review_output.commit_message)
    if state.github_workflow_review_output and state.github_workflow_review_output.commit_message:
        parts.append(state.github_workflow_review_output.commit_message)
    if state.pydantic_ai_review_output and state.pydantic_ai_review_output.commit_message:
        parts.append(state.pydantic_ai_review_output.commit_message)
    if state.docs_output and state.docs_output.commit_message:
        parts.append(state.docs_output.commit_message)
    return "\n\n".join(parts)


class PRNode(BaseNode[IssueState]):
    async def run(self, ctx: GraphRunContext[IssueState]) -> MergeEvaluationNode:
        state = ctx.state
        assert state.new_meta is not None
        assert state.implement_output is not None
        assert state.branch_name is not None

        stage_all(state.repo_root)
        # In PR-comment mode the agent may have produced reply_only across
        # the board, leaving nothing to commit. Skip the commit then so we
        # don't create an empty one — replies are still posted below.
        committed = _has_staged_changes(state.repo_root)
        if committed:
            commit(
                state.repo_root,
                _bundled_commit_message(state),
                author_name="cai-bot",
                author_email="cai-bot@users.noreply.github.com",
            )

        token = state.bot.token_for(state.new_meta.repo)
        remote_url = (
            f"https://x-access-token:{token}@github.com/{state.new_meta.repo}.git"
        )

        # Fetch latest main and rebase this branch onto it.
        fetch(state.repo_root)
        finished = rebase_onto(state.repo_root, "origin/main")
        if not finished:
            # Late imports to avoid circular dependency at module level.
            from cai.github.repo import PRWorkspace
            from cai.workflows.conflicts import _rebase_loop

            # Construct a minimal PRWorkspace for _rebase_loop (it only uses
            # repo_root, base_branch, title, body).
            ws = PRWorkspace(
                root=state.repo_root.parent,
                repo_root=state.repo_root,
                body_path=state.body_path,
                repo=state.new_meta.repo,
                number=0,
                head_branch=state.branch_name,
                base_branch="main",
                title=state.new_meta.title,
                body=state.body_path.read_text(),
            )
            ok, _touched = _rebase_loop(ws)
            if not ok:
                rebase_abort(state.repo_root)
                raise RuntimeError(
                    f"Rebase of {state.branch_name} onto origin/main failed. "
                    "The resolve_step agent could not clear all conflict markers."
                )

        if state.review_threads:
            if committed:
                push_branch(
                    state.repo_root,
                    remote_url,
                    state.branch_name,
                    env={"GIT_TERMINAL_PROMPT": "0"},
                )
            _post_replies_and_resolve(state, committed)
            return MergeEvaluationNode()

        # New-issue path with nothing committed: the implement agent decided
        # no code change was needed (e.g. issue already fixed by a prior PR).
        # Pushing an empty branch and opening a no-diff PR both fail at GitHub
        # ("No commits between main and …"), so close the issue as not_planned
        # with the agent's reasoning instead.
        if not committed and state.pr_number is None:
            if state.new_meta.number is not None:
                issue = state.bot.repo(state.new_meta.repo).get_issue(
                    state.new_meta.number
                )
                issue.create_comment(
                    "Closing as not planned — no code change is needed.\n\n"
                    f"{state.implement_output.summary}"
                )
                issue.edit(state="closed", state_reason="not_planned")
            return MergeEvaluationNode()

        push_branch(
            state.repo_root,
            remote_url,
            state.branch_name,
            env={"GIT_TERMINAL_PROMPT": "0"},
        )

        # PR mode without review threads (e.g. merge-conflict resolution):
        # the branch already belongs to an existing PR — push and stop, do
        # not open a duplicate PR.
        if state.pr_number is not None:
            return MergeEvaluationNode()

        pr_body = state.body_path.read_text()
        if state.new_meta.number is not None:
            pr_body += f"\n\nCloses #{state.new_meta.number}"
        pr_url, pr_number = create_pull_request(
            state.bot,
            state.new_meta.repo,
            title=state.new_meta.title,
            body=pr_body,
            head=state.branch_name,
        )
        state.pr_url = pr_url
        state.pr_number = pr_number
        return MergeEvaluationNode()


def _post_replies_and_resolve(state: IssueState, committed: bool) -> None:
    """Post each per-thread reply; resolve threads where action=fix landed.

    A thread is resolved only when the agent claimed action='fix' AND a
    commit actually landed — the validator guards against fix-without-edit
    on the agent side, but `committed=False` (e.g. all replies were
    reply_only) still needs to skip resolution here.
    """
    assert state.implement_output is not None
    assert state.new_meta is not None
    assert state.pr_number is not None

    threads_by_id = {t.id: t for t in state.review_threads}
    for r in state.implement_output.replies:
        thread = threads_by_id.get(r.thread_id)
        if thread is None:
            continue
        reply_to_review_comment(
            state.bot,
            state.new_meta.repo,
            state.pr_number,
            thread.first_comment_id,
            r.reply,
        )
        if r.action == "fix" and committed:
            resolve_review_thread(state.bot, state.new_meta.repo, thread.id)
