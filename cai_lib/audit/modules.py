"""YAML loader + coverage helper for docs/modules.yaml.

Audit-refactor step 1.1. Pure library module — no subprocess, no
git calls. Callers (loop driver, doc agent, coverage script) feed
the tracked-file list they obtained from `git ls-files` and receive
structured results.
"""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import yaml


REQUIRED_FIELDS = ("name", "summary", "doc", "globs")


@dataclass
class ModuleEntry:
    """One entry from docs/modules.yaml."""
    name: str
    summary: str
    doc: str
    globs: list[str] = field(default_factory=list)


def load_modules(path: Path, *, check_doc_exists: bool = False) -> list[ModuleEntry]:
    """Parse and validate a modules.yaml file.

    Raises ``ValueError`` on any schema violation:
      - file does not parse as YAML with a top-level ``modules:`` list,
      - any entry is not a mapping or is missing a required field,
      - ``name`` is not a non-empty string or is duplicated,
      - ``summary`` / ``doc`` is not a non-empty string,
      - ``globs`` is not a non-empty list of strings,
      - ``check_doc_exists`` is true and an entry's ``doc`` path does
        not exist relative to ``path.parent``.

    ``check_doc_exists`` defaults to False because the skeleton
    shipped with step 1.1 points at narrative pages under
    ``docs/modules/`` that a later step of the audit-refactor will
    create.
    """
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise ValueError(f"{path}: not valid YAML: {exc}") from exc
    if not isinstance(raw, dict) or "modules" not in raw:
        raise ValueError(f"{path}: top-level key 'modules:' is required")
    items = raw["modules"]
    if not isinstance(items, list):
        raise ValueError(f"{path}: 'modules:' must be a list")

    result: list[ModuleEntry] = []
    seen_names: set[str] = set()
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"{path}: modules[{i}] is not a mapping")
        for fld in REQUIRED_FIELDS:
            if fld not in item:
                raise ValueError(
                    f"{path}: modules[{i}] missing required field '{fld}'"
                )
        name = item["name"]
        if not isinstance(name, str) or not name:
            raise ValueError(
                f"{path}: modules[{i}].name must be a non-empty string"
            )
        if name in seen_names:
            raise ValueError(f"{path}: duplicate module name '{name}'")
        seen_names.add(name)
        summary = item["summary"]
        if not isinstance(summary, str) or not summary:
            raise ValueError(
                f"{path}: modules[{i}].summary must be a non-empty string"
            )
        doc = item["doc"]
        if not isinstance(doc, str) or not doc:
            raise ValueError(
                f"{path}: modules[{i}].doc must be a non-empty string"
            )
        if check_doc_exists and not (path.parent / doc).exists():
            raise ValueError(
                f"{path}: modules[{i}].doc path does not exist: {doc}"
            )
        globs = item["globs"]
        if not isinstance(globs, list) or not globs:
            raise ValueError(
                f"{path}: modules[{i}].globs must be a non-empty list"
            )
        for g in globs:
            if not isinstance(g, str):
                raise ValueError(
                    f"{path}: modules[{i}].globs entries must be strings"
                )
        result.append(ModuleEntry(
            name=name,
            summary=summary,
            doc=doc,
            globs=list(globs),
        ))
    return result


def coverage_check(
    modules: list[ModuleEntry],
    file_list: Iterable[str],
) -> list[str]:
    """Return coverage error strings for a tracked-file list.

    For every path in ``file_list``, count how many module globs it
    matches (any glob within a module counts the module once). A
    file is "covered" iff exactly one module matches; zero or 2+
    matches produce a human-readable error string. Returns an empty
    list when coverage is perfect.
    """
    errors: list[str] = []
    for fpath in file_list:
        matched = [
            m.name for m in modules
            if any(fnmatch.fnmatch(fpath, g) for g in m.globs)
        ]
        if len(matched) == 0:
            errors.append(f"{fpath}: matches no module")
        elif len(matched) > 1:
            errors.append(
                f"{fpath}: matches multiple modules: {', '.join(matched)}"
            )
    return errors
