"""Decide whether the just-pushed PR is simple enough to auto-merge.

Runs after :class:`PRNode`. Skipped (no-op) when:

* there is no PR number to act on (graph ended without a PR),
* tests did not pass — never auto-merge an unverified change,
* a human reviewer has already been requested — don't bypass review.

Otherwise the merge_evaluator agent receives the PR diff plus the
implementation summary and emits a structured yes/no decision; on yes,
GitHub's ``enablePullRequestAutoMerge`` mutation is called with method
``MERGE``.
"""
from __future__ import annotations

import sys
from functools import lru_cache

from pydantic_ai.usage import UsageLimits
from pydantic_graph import BaseNode, End, GraphRunContext

from cai.agents.loader import load_agent_from_md, resolve_agent_path
from cai.github.issues import IssueMeta
from cai.github.pr import (
    enable_auto_merge,
    get_pr_diff,
    get_pr_node_id_and_review_requests,
)
from cai.workflows.state import IssueState, MergeEvaluationOutput

_DIFF_CHAR_CAP = 80_000


@lru_cache(maxsize=1)
def _merge_evaluator_agent():
    return load_agent_from_md(
        resolve_agent_path("merge_evaluator"),
        output_type=MergeEvaluationOutput,
    )


def _truncate_diff(diff: str) -> tuple[str, bool]:
    if len(diff) <= _DIFF_CHAR_CAP:
        return diff, False
    return diff[:_DIFF_CHAR_CAP] + "\n... [diff truncated]", True


class MergeEvaluationNode(BaseNode[IssueState, None, IssueMeta]):
    async def run(self, ctx: GraphRunContext[IssueState]) -> End[IssueMeta]:
        state = ctx.state
        assert state.new_meta is not None

        if state.pr_number is None:
            return End(state.new_meta)

        if state.tests_passed is not True:
            print(
                f"[merge-eval] skipping auto-merge for {state.new_meta.repo}#{state.pr_number}: "
                f"tests did not pass",
                file=sys.stderr,
            )
            return End(state.new_meta)

        try:
            _, review_requests = get_pr_node_id_and_review_requests(
                state.bot, state.new_meta.repo, state.pr_number
            )
        except Exception as exc:
            print(f"[merge-eval] could not fetch PR metadata: {exc}", file=sys.stderr)
            return End(state.new_meta)

        if review_requests > 0:
            print(
                f"[merge-eval] skipping auto-merge for {state.new_meta.repo}#{state.pr_number}: "
                f"{review_requests} human reviewer(s) requested",
                file=sys.stderr,
            )
            return End(state.new_meta)

        try:
            diff = get_pr_diff(state.bot, state.new_meta.repo, state.pr_number)
        except Exception as exc:
            print(f"[merge-eval] could not fetch PR diff: {exc}", file=sys.stderr)
            return End(state.new_meta)

        diff_text, truncated = _truncate_diff(diff)
        impl = state.implement_output
        body = state.body_path.read_text() if state.body_path.exists() else ""

        prompt = (
            f"## Issue title\n\n{state.new_meta.title}\n\n"
            f"## Issue body\n\n{body or '(empty)'}\n\n"
            f"## Implementation summary\n\n"
            f"{impl.summary if impl else '(no implementation summary available)'}\n\n"
            f"## Bundled commit message\n\n"
            f"{impl.commit_message if impl else '(none)'}\n\n"
            f"## PR diff{' (TRUNCATED)' if truncated else ''}\n\n```diff\n{diff_text}\n```"
        )

        try:
            result = await _merge_evaluator_agent().run(
                prompt, usage_limits=UsageLimits(request_limit=10)
            )
        except Exception as exc:
            print(f"[merge-eval] evaluator agent failed: {exc}", file=sys.stderr)
            return End(state.new_meta)

        decision: MergeEvaluationOutput = result.output
        state.merge_evaluation = decision

        if not decision.auto_merge:
            print(
                f"[merge-eval] not enabling auto-merge for "
                f"{state.new_meta.repo}#{state.pr_number}: {decision.reason}",
                file=sys.stderr,
            )
            return End(state.new_meta)

        try:
            enable_auto_merge(state.bot, state.new_meta.repo, state.pr_number)
            state.auto_merge_enabled = True
            print(
                f"[merge-eval] auto-merge enabled on {state.new_meta.repo}#{state.pr_number}: "
                f"{decision.reason}",
                file=sys.stderr,
            )
        except Exception as exc:
            print(
                f"[merge-eval] enablePullRequestAutoMerge failed for "
                f"{state.new_meta.repo}#{state.pr_number}: {exc}",
                file=sys.stderr,
            )

        return End(state.new_meta)
