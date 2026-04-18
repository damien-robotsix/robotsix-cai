"""Git and worktree helpers for cai action wrappers."""

import shutil
import subprocess
import sys

from pathlib import Path


def _read_shared_memory() -> str:
    """Read all shared agent memory files and return them as a formatted
    block suitable for inclusion in the user message.

    Returns an empty string if the shared memory directory does not exist
    or contains no files.
    """
    shared_dir = Path("/app/.claude/agent-memory/shared")
    if not shared_dir.is_dir():
        return ""
    parts: list[str] = []
    # Read the index file first (if it exists), then all other .md files
    index = shared_dir / "MEMORY.md"
    files: list[Path] = []
    if index.is_file():
        files.append(index)
    for f in sorted(shared_dir.iterdir()):
        if f.is_file() and f.suffix == ".md" and f.name != "MEMORY.md":
            files.append(f)
    if not files:
        return ""
    for f in files:
        try:
            content = f.read_text()
        except OSError:
            continue
        parts.append(f"### `{f.name}`\n\n{content}")
    if not parts:
        return ""
    return (
        "\n\n## Shared agent memory (pre-loaded)\n\n"
        "The following shared memory entries have been pre-loaded from "
        "`.claude/agent-memory/shared/`. **Do NOT attempt to read these "
        "files from disk** — they are already included below. If your "
        "agent instructions say to read `.claude/agent-memory/shared/MEMORY.md`, "
        "refer to this section instead.\n\n"
        + "\n\n---\n\n".join(parts)
        + "\n"
    )


# Paths of the staging directories inside a cloned worktree, relative
# to the clone root.
AGENT_EDIT_STAGING_REL = Path(".cai-staging") / "agents"
AGENT_DELETE_STAGING_REL = Path(".cai-staging") / "agents-delete"
PLUGIN_STAGING_REL = Path(".cai-staging") / "plugins"
CLAUDEMD_STAGING_REL = Path(".cai-staging") / "claudemd"


def _git(work_dir: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    cmd = ["git", "-C", str(work_dir)] + list(args)
    return subprocess.run(cmd, text=True, check=check, capture_output=True)


def _work_directory_block(work_dir: Path) -> str:
    """Return the standard "## Work directory" user-message section
    that informs a cloned-worktree subagent where its actual work
    happens, and how to update protected `.claude/agents/*.md`
    files via the staging directory.

    All cloned-worktree subagents (cai-implement, cai-revise, cai-rebase,
    cai-review-pr, cai-review-docs, cai-code-audit, cai-propose, cai-propose-review,
    cai-update-check, cai-plan, cai-select, cai-git, cai-agent-audit, cai-external-scout) are invoked with `cwd=/app`
    rather than `cwd=<clone>`. This makes their canonical agent
    definition (`/app/.claude/agents/<name>.md`) and per-agent memory
    (`/app/.claude/agent-memory/<name>/`) directly available via
    cwd-relative paths.

    The trade-off: the agent must use ABSOLUTE paths to read/edit
    files in the actual clone, since the clone is no longer the
    cwd. This block tells the agent where the clone is and reminds
    it to use absolute paths.

    Self-modification of `.claude/agents/*.md`: claude-code's
    headless `-p` mode hardcodes a protection that blocks
    Edit/Write on any `.claude/agents/*.md` file, regardless of
    `--dangerously-skip-permissions`, `--permission-mode`, or
    `permissions.allow` rules. We work around it with a staging
    directory — see `_setup_agent_edit_staging` /
    `_apply_agent_edit_staging`. The block below tells the agent
    how to use it.
    """
    staging_rel = AGENT_EDIT_STAGING_REL.as_posix()
    staging_abs = (work_dir / AGENT_EDIT_STAGING_REL).as_posix()
    delete_staging_abs = (work_dir / AGENT_DELETE_STAGING_REL).as_posix()
    claudemd_abs = (work_dir / CLAUDEMD_STAGING_REL).as_posix()
    return (
        "## Work directory\n\n"
        "You are running with cwd `/app` so your declarative agent "
        "definition and per-agent memory are read from the canonical "
        "image / volume locations. Your actual work happens on a "
        "fresh clone of the repository at:\n\n"
        f"    {work_dir}\n\n"
        "**You MUST use absolute paths under that directory for all "
        "Read/Edit/Write/Glob/Grep calls that target the work.** "
        "Relative paths resolve to `/app` (the canonical, baked-in "
        "version of the repo) which you should treat as read-only. "
        "Edits to `/app/...` would land in the container's writable "
        "layer and be lost on next restart — they would NOT make it "
        "into git.\n\n"
        "Examples:\n"
        f"  - GOOD: `Read(\"{work_dir}/cai.py\")`\n"
        "  - BAD:  `Read(\"cai.py\")`               (reads /app/cai.py)\n"
        f"  - GOOD: `Edit(\"{work_dir}/parse.py\", ...)`\n"
        "  - BAD:  `Edit(\"parse.py\", ...)`        (edits /app/parse.py)\n\n"
        "If you have Bash in your tool allowlist, the same rule "
        "applies: use `git -C` (or absolute paths) for any git "
        "operation that should target the clone, NOT the cwd.\n\n"
        "  - GOOD: `git -C "
        f"{work_dir} status`\n"
        "  - BAD:  `git status`        (reports /app status, not "
        "the clone's)\n\n"
        "## Updating `.claude/agents/*.md` (self-modification)\n\n"
        "Claude-code's headless `-p` mode hardcodes a write block "
        "on every `.claude/agents/*.md` path, regardless of any "
        "permission flag or settings rule. Edit/Write calls against "
        f"`{work_dir}/.claude/agents/<name>.md` WILL fail with a "
        "sensitive-file protection error — this is not something "
        "you can bypass from inside your session.\n\n"
        "The wrapper provides a **staging directory** at:\n\n"
        f"    {staging_abs}\n\n"
        "To update an `.claude/agents/<name>.md` file, use the "
        "Write tool to write the COMPLETE new file content "
        "(frontmatter + body) to the corresponding path under "
        f"`{staging_abs}/`. After your session exits "
        "successfully the wrapper recursively walks every file it finds "
        f"in `{staging_rel}/`, preserving subdirectory paths, and copies "
        "each one to the matching `.claude/agents/<relative-path>.md` in "
        "the clone, then deletes the staging directory so it never lands "
        "in the PR.\n\n"
        "Rules:\n"
        "  - Staged files are copied unconditionally — new agent "
        "definitions are created if no target exists yet.\n"
        "  - Write the FULL file, not a diff or patch. The wrapper "
        "does an unconditional full-file overwrite.\n"
        "  - Preserve the relative path from the staging root "
        f"(e.g. `{staging_abs}/lifecycle/cai-triage.md` → "
        f"`{work_dir}/.claude/agents/lifecycle/cai-triage.md`).\n"
        "  - Do NOT attempt `Edit` or `Write` on the protected "
        f"`{work_dir}/.claude/agents/...` path — it will always "
        "fail. Go through the staging dir.\n\n"
        "Example:\n"
        f"  - GOOD: `Write(\"{staging_abs}/lifecycle/cai-triage.md\", "
        "\"<full new file content>\")`\n"
        f"  - BAD:  `Edit(\"{work_dir}/.claude/agents/lifecycle/cai-triage.md\", "
        "...)`  (blocked by claude-code)\n"
        "\n"
        "## Deleting `.claude/agents/*.md` files (self-modification)\n\n"
        "The same `-p` protection blocks `Bash(\"rm ...\")` on any "
        f"`{work_dir}/.claude/agents/...` path. To request deletion of "
        "one or more agent files (e.g. when migrating a flat agent "
        "into a subfolder layout), drop a **tombstone file** at:\n\n"
        f"    {delete_staging_abs}/<same-relative-path>.md\n\n"
        "The wrapper walks that directory with `rglob(\"*.md\")` after "
        "your session exits, and for each tombstone found deletes the "
        "matching file at `.claude/agents/<relative-path>.md`. The "
        "tombstone's contents are ignored — only the relative path "
        "matters; an empty string is fine. Missing targets are silently "
        "skipped (stale tombstones are safe). Non-`.md` files in the "
        "tombstone tree are ignored.\n\n"
        "Example — delete a flat agent after migrating it to a subfolder:\n"
        f"  1. `Write(\"{staging_abs}/lifecycle/cai-triage.md\", "
        "\"<full new content>\")`  (write new subfolder copy)\n"
        f"  2. `Write(\"{delete_staging_abs}/cai-triage.md\", \"\")`  "
        "(tombstone the old flat copy)\n\n"
        "Do NOT try `Bash(\"rm ...\")` on `.claude/agents/` — it is "
        "blocked by the same sensitive-file protection.\n\n"
        "## Updating `CLAUDE.md` files (self-modification)\n\n"
        "Claude-code's headless `-p` mode also hardcodes a write block "
        "on `CLAUDE.md` files (project-level context files). Edit/Write "
        f"calls against `{work_dir}/CLAUDE.md` or any subdirectory "
        "`CLAUDE.md` WILL fail with a sensitive-file protection error.\n\n"
        "The wrapper provides a **staging directory** at:\n\n"
        f"    {claudemd_abs}\n\n"
        "To update a `CLAUDE.md` file at any path (root or subdirectory), "
        f"write it to `{claudemd_abs}/<same-relative-path>/CLAUDE.md`. "
        "The wrapper scans for all files named exactly `CLAUDE.md` under "
        f"the staging tree and copies each to the matching path in "
        f"`{work_dir}/`, creating parent directories as needed. The "
        "staging directory is then deleted so it never lands in the PR.\n\n"
        "Rules:\n"
        "  - Only files literally named `CLAUDE.md` are copied — other "
        "files in the staging tree are ignored.\n"
        "  - Preserve the full relative path (e.g., to update "
        f"`{work_dir}/CLAUDE.md`, write to "
        f"`{claudemd_abs}/CLAUDE.md`; to update "
        f"`{work_dir}/subdir/CLAUDE.md`, write to "
        f"`{claudemd_abs}/subdir/CLAUDE.md`).\n"
        "  - Write the FULL file, not a diff or patch.\n"
        f"  - Do NOT attempt `Edit` or `Write` on `{work_dir}/CLAUDE.md` "
        "directly — it will always fail. Go through the staging dir.\n\n"
        "Example:\n"
        f"  - GOOD: `Write(\"{claudemd_abs}/CLAUDE.md\", "
        "\"<full new file content>\")`\n"
        f"  - BAD:  `Edit(\"{work_dir}/CLAUDE.md\", ...)`  "
        "(blocked by claude-code)\n"
    ) + _read_shared_memory()


def _setup_agent_edit_staging(work_dir: Path) -> Path:
    """Create the staging directories where agents write proposed
    `.claude/agents/*.md` and `.claude/plugins/` updates. Idempotent.

    Returns the absolute agent-staging directory path so the caller can
    pass it to the agent via the user message.
    """
    staging = work_dir / AGENT_EDIT_STAGING_REL
    staging.mkdir(parents=True, exist_ok=True)
    delete_staging = work_dir / AGENT_DELETE_STAGING_REL
    delete_staging.mkdir(parents=True, exist_ok=True)
    plugin_staging = work_dir / PLUGIN_STAGING_REL
    plugin_staging.mkdir(parents=True, exist_ok=True)
    claudemd_staging = work_dir / CLAUDEMD_STAGING_REL
    claudemd_staging.mkdir(parents=True, exist_ok=True)
    return staging


def _apply_agent_edit_staging(work_dir: Path) -> int:
    """Copy any files staged at `<work_dir>/.cai-staging/agents/`
    back to `<work_dir>/.claude/agents/`, copy any plugin tree staged
    at `<work_dir>/.cai-staging/plugins/` to `<work_dir>/.claude/plugins/`,
    then remove the staging directory so it doesn't land in the PR.

    Security boundaries:

      1. Each staged agent file is copied to `<work_dir>/.claude/agents/`
         preserving its subdirectory path relative to the staging root.
         If no target exists a new file is created; if one exists it is
         overwritten. Parent directories are created as needed.
      2. Staged plugin trees are merged into `<work_dir>/.claude/plugins/`
         using shutil.copytree with dirs_exist_ok=True.
      3. The staging dir lives entirely inside `work_dir` so escapes
         via `..` are not possible (the wrapper recursively walks the
         staging directory via `rglob("*.md")` and copies whole files
         preserving subdirectory paths).
      4. The staging dir is removed before commit if all staging
         operations succeeded. If plugin staging fails, the staging
         dir is preserved for inspection and the function returns
         early so staged content is not silently lost.

    Returns the count of files successfully applied. If the staging
    dir doesn't exist or is empty, returns 0 with no side effects.
    """
    staging = work_dir / AGENT_EDIT_STAGING_REL
    applied = 0

    if staging.exists() and staging.is_dir():
        target_dir = work_dir / ".claude" / "agents"
        for staged_file in sorted(staging.rglob("*.md")):
            rel = staged_file.relative_to(staging)
            target = target_dir / rel
            if not target.exists():
                print(
                    f"[cai] agent edit staging: creating new agent file "
                    f".claude/agents/{rel}",
                    flush=True,
                )

            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                content = staged_file.read_text()
                target.write_text(content)
                print(
                    f"[cai] applied staged agent file: "
                    f".claude/agents/{rel} "
                    f"({len(content)} bytes)",
                    flush=True,
                )
                applied += 1
            except OSError as exc:
                print(
                    f"[cai] agent edit staging: failed to apply "
                    f"{rel}: {exc}",
                    file=sys.stderr,
                )
                continue

    # Apply any plugin staging: .cai-staging/plugins/ → .claude/plugins/
    plugin_staging = work_dir / PLUGIN_STAGING_REL
    if plugin_staging.exists() and plugin_staging.is_dir():
        plugin_target = work_dir / ".claude" / "plugins"
        try:
            shutil.copytree(str(plugin_staging), str(plugin_target),
                            dirs_exist_ok=True)
            print(
                "[cai] applied staged plugin tree: .claude/plugins/ "
                "(merged from .cai-staging/plugins/)",
                flush=True,
            )
            applied += 1
        except OSError as exc:
            print(
                f"[cai] agent edit staging: failed to apply plugin tree: {exc}",
                file=sys.stderr,
            )
            # Preserve .cai-staging so staged plugin files are not
            # silently lost when the copy fails — caller can inspect
            # or retry. Do not fall through to shutil.rmtree below.
            return applied

    # Apply any CLAUDE.md staging: .cai-staging/claudemd/ → <work_dir>/.
    # Use rglob("CLAUDE.md") so only files literally named CLAUDE.md
    # are copied — stray files in the staging tree are ignored.
    claudemd_staging = work_dir / CLAUDEMD_STAGING_REL
    if claudemd_staging.exists() and claudemd_staging.is_dir():
        for staged_file in sorted(claudemd_staging.rglob("CLAUDE.md")):
            rel = staged_file.relative_to(claudemd_staging)
            target = work_dir / rel
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                content = staged_file.read_text()
                target.write_text(content)
                print(
                    f"[cai] applied staged CLAUDE.md: {rel} "
                    f"({len(content)} bytes)",
                    flush=True,
                )
                applied += 1
            except OSError as exc:
                print(
                    f"[cai] agent edit staging: failed to apply "
                    f"CLAUDE.md at {rel}: {exc}",
                    file=sys.stderr,
                )
                # Preserve .cai-staging so staged files are not
                # silently lost when the copy fails.
                return applied

    # Apply any agent-file deletions requested via tombstone markers at
    # .cai-staging/agents-delete/<relative-path>.md. A file's presence in
    # that tree signals "delete the matching path under .claude/agents/".
    # Contents are ignored — only the relative path matters. Non-.md
    # files are skipped. Path traversal is structurally impossible:
    # rglob() only yields descendants of the staging root.
    delete_staging = work_dir / AGENT_DELETE_STAGING_REL
    if delete_staging.exists() and delete_staging.is_dir():
        agents_dir = work_dir / ".claude" / "agents"
        for tombstone in sorted(delete_staging.rglob("*.md")):
            rel = tombstone.relative_to(delete_staging)
            target = agents_dir / rel
            try:
                if target.is_file():
                    target.unlink()
                    print(
                        f"[cai] applied staged agent deletion: "
                        f".claude/agents/{rel}",
                        flush=True,
                    )
                    applied += 1
                else:
                    print(
                        f"[cai] agent edit staging: tombstone for "
                        f".claude/agents/{rel} has no target file "
                        f"(already absent; skipping)",
                        flush=True,
                    )
            except OSError as exc:
                print(
                    f"[cai] agent edit staging: failed to delete "
                    f".claude/agents/{rel}: {exc}",
                    file=sys.stderr,
                )
                continue

    # Clean up the entire .cai-staging tree (one level above the
    # agents/ subdir) so nothing leaks into the PR.
    cai_staging_root = work_dir / ".cai-staging"
    if cai_staging_root.exists():
        try:
            shutil.rmtree(cai_staging_root)
        except OSError as exc:
            print(
                f"[cai] agent edit staging: cleanup of "
                f"{cai_staging_root} failed: {exc}",
                file=sys.stderr,
            )

    return applied
