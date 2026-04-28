from __future__ import annotations

from pathlib import Path

from git import Repo
from pydantic_graph import BaseNode, End, GraphRunContext

from cai.git import commit, push_branch, stage_all
from cai.github.issues import IssueMeta
from cai.github.pr import (
    create_pull_request,
    reply_to_review_comment,
    resolve_review_thread,
)
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
    if state.docs_output and state.docs_output.commit_message:
        parts.append(state.docs_output.commit_message)
    return "\n\n".join(parts)


class PRNode(BaseNode[IssueState]):
    async def run(self, ctx: GraphRunContext[IssueState]) -> End[IssueMeta]:
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

        if state.review_threads:
            if committed:
                push_branch(
                    state.repo_root,
                    remote_url,
                    state.branch_name,
                    env={"GIT_TERMINAL_PROMPT": "0"},
                )
            _post_replies_and_resolve(state, committed)
            return End(state.new_meta)

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
            return End(state.new_meta)

        pr_body = state.body_path.read_text()
        if state.new_meta.number is not None:
            pr_body += f"\n\nCloses #{state.new_meta.number}"
        pr_url = create_pull_request(
            state.bot,
            state.new_meta.repo,
            title=state.new_meta.title,
            body=pr_body,
            head=state.branch_name,
        )
        state.pr_url = pr_url
        return End(state.new_meta)


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
