"""cai_lib.cmd_review_docs — CI-mode entry point for cai-review-docs.

Invoked by the ``cai review-docs --pr N`` subcommand. Unlike the
FSM-driven ``handle_review_docs``, this entry point skips the
``PRState.REVIEWING_DOCS`` state gate and the idempotency check on
prior-SHA docs-review comments: in CI we always want a fresh run so
the module-coverage retry step in ``.github/workflows/regenerate-docs.yml``
has a chance to auto-fix a freshly-stale ``docs/modules.yaml``.

Applies no FSM transitions; if the agent pushes doc fixes, the
:pr-reviewing-docs label (if present) is left alone. The workflow
that invokes this subcommand is responsible for its own success /
failure signalling (via subsequent ``scripts/check-modules-coverage.py``
calls).
"""
from __future__ import annotations

import subprocess
import sys

from cai_lib.actions.review_docs import run_review_docs_ci
from cai_lib.config import REPO
from cai_lib.github import _gh_json


def cmd_review_docs(args) -> int:
    """Run cai-review-docs on PR #``args.pr`` and exit with its return code."""
    pr_number = args.pr
    try:
        pr = _gh_json([
            "pr", "view", str(pr_number),
            "--repo", REPO,
            "--json",
            "number,title,headRefName,headRefOid,comments,body,author",
        ])
    except subprocess.CalledProcessError as e:
        print(
            f"[cai review-docs] gh pr view #{pr_number} failed:\n{e.stderr}",
            file=sys.stderr,
        )
        return 1
    return run_review_docs_ci(pr)
