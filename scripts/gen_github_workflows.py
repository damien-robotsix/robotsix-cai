#!/usr/bin/env python3
"""Generate ``cai-*.yml`` workflow files from the registry via a Jinja template.

Run by the ``regen-github-workflows`` pre-commit hook so the CI YAML
always reflects the live registry entries. Exits non-zero if any
workflow file would change, which is what makes pre-commit fail and
re-stage the regenerated file in the same hook run.

The list of workflows comes from ``cai.workflows.registry.WORKFLOWS`` —
adding a workflow there automatically produces a CI YAML file on the
next run of this script.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from cai.workflows.registry import WORKFLOWS  # noqa: E402

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

# Slugs that are deliberately kept hand-written.
SKIP_SLUGS = {"audit-auto"}


def _determine_shape(spec) -> str:
    events = [e.event for e in spec.github_trigger.on]
    if "workflow_run" in events:
        return "resolve"
    if "push" in events:
        return "gate"
    return "simple"


def main() -> int:
    from jinja2 import Environment, FileSystemLoader

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        keep_trailing_newline=True,
    )
    template = env.get_template("cai_workflow.yml.j2")

    changed: list[Path] = []

    for spec in WORKFLOWS:
        if spec.slug in SKIP_SLUGS:
            continue

        shape = _determine_shape(spec)
        rendered = template.render(spec=spec, shape=shape)

        path = WORKFLOWS_DIR / f"cai-{spec.slug}.yml"
        existing = path.read_text() if path.exists() else None
        if existing != rendered:
            path.write_text(rendered)
            changed.append(path)

    if changed:
        for path in changed:
            print(f"updated {path.relative_to(REPO_ROOT)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
