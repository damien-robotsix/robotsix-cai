"""Deterministic refinement workflow built on pydantic-deep.

A ``cai-refine`` deep agent reads the issue (metadata + body in the prompt,
codebase via its built-in filesystem/grep/glob tools, optional research
subagent), edits the body file in place via the deep-agent filesystem
toolset, and returns ``RefineOutput`` (metadata changes only). The wrapper
owns the JSON file write.
"""
from __future__ import annotations

import argparse
import json
import sys
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field
from pydantic_deep import DeepAgentDeps, LocalBackend, create_deep_agent

from cai.agents.loader import AGENT_DIR, build_model, parse_agent_md
from cai.github.issues import IssueMeta
from cai.observability import setup_langfuse

AGENT_DEFINITION = AGENT_DIR / "cai-refine.md"


class RefineOutput(BaseModel):
    """Structured metadata changes. The body is mutated on disk by the agent."""

    title: str = Field(description="Refined title (or the original if already clear).")
    labels: list[str] = Field(
        default_factory=list,
        description="Labels the issue should carry after refinement (full set, not a delta).",
    )


@lru_cache(maxsize=1)
def _agent():
    setup_langfuse()
    config, instructions = parse_agent_md(AGENT_DEFINITION)
    return create_deep_agent(
        build_model(config),
        instructions=instructions,
        output_type=RefineOutput,
        web_search=False,
        web_fetch=False,
        include_skills=False,
    )


def _deps(body_path: Path, repo_root: Path) -> DeepAgentDeps:
    """Build per-run deps with a LocalBackend rooted at the repo, plus body access."""
    backend = LocalBackend(
        root_dir=str(repo_root),
        allowed_directories=[str(body_path.parent.resolve())],
    )
    return DeepAgentDeps(backend=backend)


def refine_issue(
    meta: IssueMeta,
    body_path: Path,
    *,
    repo_root: Path | None = None,
) -> tuple[IssueMeta, RefineOutput]:
    """Run the refine agent against ``meta`` and the body at ``body_path``.

    The agent edits ``body_path`` via its built-in filesystem tools and may
    grep/read files under ``repo_root`` (defaults to ``Path.cwd()``).
    Returns the updated ``IssueMeta`` (``title`` + ``labels`` overwritten;
    other fields preserved) and the raw ``RefineOutput``.
    """
    body_path = Path(body_path).resolve()
    repo_root = (repo_root or Path.cwd()).resolve()
    body = body_path.read_text()
    prompt = (
        "Refine this GitHub issue. The body file is on disk — use your "
        "filesystem tools (Edit, Write) to rewrite it; use Read, Grep, "
        f"and Glob to investigate the codebase as needed.\n\n"
        f"Body file path: {body_path}\n"
        f"Repository root: {repo_root}\n\n"
        f"## Metadata\n\n{meta.model_dump_json(indent=2)}\n\n"
        f"## Current body\n\n{body}"
    )
    result = _agent().run_sync(prompt, deps=_deps(body_path, repo_root))
    out: RefineOutput = result.output
    new_meta = meta.model_copy(update={"title": out.title, "labels": out.labels})
    return new_meta, out


def refine_files(json_path: Path, *, repo_root: Path | None = None) -> IssueMeta:
    """Refine the issue at ``<n>.json`` + ``<n>.md`` in place.

    The agent's tool calls mutate ``<n>.md``; this wrapper writes the
    updated metadata back to ``<n>.json`` and returns it.
    """
    json_path = Path(json_path)
    md_path = json_path.with_suffix(".md")
    if not md_path.exists():
        raise FileNotFoundError(f"missing issue body file: {md_path}")

    meta = IssueMeta.model_validate_json(json_path.read_text())
    new_meta, _ = refine_issue(meta, md_path, repo_root=repo_root)
    json_path.write_text(new_meta.model_dump_json(indent=2) + "\n")
    return new_meta


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="cai-refine",
        description="Refine a cai-issue JSON+MD pair in place. Prints the updated metadata as JSON.",
    )
    parser.add_argument("path", type=Path, help="Path to the issue <n>.json file.")
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="Repository root to expose to the agent for grep/read (default: cwd).",
    )
    args = parser.parse_args()

    new_meta = refine_files(args.path, repo_root=args.repo_root)
    json.dump(new_meta.model_dump(), sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
