from __future__ import annotations

from pathlib import Path

from pydantic_graph import Graph

from cai.github.issues import IssueMeta
from cai.workflows.explore import ExploreNode
from cai.workflows.refine import RefineNode
from cai.workflows.state import IssueState, RefineOutput

refine_graph: Graph[IssueState, None, IssueMeta] = Graph(nodes=[ExploreNode, RefineNode])


def refine_issue(
    meta: IssueMeta,
    body_path: Path,
    *,
    repo_root: Path | None = None,
) -> tuple[IssueMeta, RefineOutput]:
    """Run explore → refine hand-off against ``meta`` and the body at ``body_path``."""
    body_path = Path(body_path).resolve()
    repo_root = (repo_root or Path.cwd()).resolve()
    state = IssueState(meta=meta, body_path=body_path, repo_root=repo_root)
    refine_graph.run_sync(ExploreNode(), state=state)
    assert state.new_meta is not None
    assert state.refine_output is not None
    return state.new_meta, state.refine_output


def refine_files(json_path: Path, *, repo_root: Path | None = None) -> IssueMeta:
    """Refine the issue at ``<n>.json`` + ``<n>.md`` in place."""
    json_path = Path(json_path)
    md_path = json_path.with_suffix(".md")
    if not md_path.exists():
        raise FileNotFoundError(f"missing issue body file: {md_path}")
    meta = IssueMeta.model_validate_json(json_path.read_text())
    new_meta, _ = refine_issue(meta, md_path, repo_root=repo_root)
    json_path.write_text(new_meta.model_dump_json(indent=2) + "\n")
    return new_meta
