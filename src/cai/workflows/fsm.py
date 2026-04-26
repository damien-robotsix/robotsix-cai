from __future__ import annotations

from pathlib import Path

from pydantic_graph import Graph

from cai.github.bot import CaiBot
from cai.github.issues import IssueMeta
from cai.log import langfuse_workflow
from cai.workflows.explore import ExploreNode
from cai.workflows.refine import RefineNode
from cai.workflows.state import IssueState

refine_graph: Graph[IssueState, None, IssueMeta] = Graph(nodes=[ExploreNode, RefineNode])


def refine_files(
    bot: CaiBot,
    json_path: Path,
    *,
    repo_root: Path | None = None,
) -> IssueMeta:
    """Refine the issue at ``<n>.json`` + ``<n>.md`` and push the result back.

    The refine node writes the refined ``<n>.json`` and pushes both files
    to GitHub via ``bot`` once the agent run succeeds.
    """
    json_path = Path(json_path)
    md_path = json_path.with_suffix(".md")
    if not md_path.exists():
        raise FileNotFoundError(f"missing issue body file: {md_path}")
    meta = IssueMeta.model_validate_json(json_path.read_text())
    state = IssueState(
        bot=bot,
        meta=meta,
        body_path=md_path.resolve(),
        repo_root=(repo_root or Path.cwd()).resolve(),
    )
    issue_ref = f"{meta.repo}#{meta.number}" if meta.number else meta.repo
    with langfuse_workflow(
        "cai-solve",
        input={"issue": issue_ref, "title": meta.title},
        metadata={"repo": meta.repo, "issue_number": meta.number},
    ):
        refine_graph.run_sync(ExploreNode(), state=state)
    assert state.new_meta is not None
    return state.new_meta
