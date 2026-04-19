---
title: Staging Protocol
nav_order: 10
---

# .cai-staging/ Protocol Reference

## Overview

`claude-code -p` mode hardcodes write-blocks on `.claude/agents/*.md`,
`.claude/plugins/`, and `CLAUDE.md` files inside the clone. To allow
subagents to self-modify these paths, the wrapper pre-creates a
`.cai-staging/` directory inside the clone before the agent session
starts, and applies its contents after the agent exits. The
implementation lives in
[`cai_lib/cmd_helpers_git.py`](../cai_lib/cmd_helpers_git.py) —
specifically `_setup_agent_edit_staging` and `_apply_agent_edit_staging`.

## The Four Subdirectories

| Subdir | Semantic | Filter | Target |
|---|---|---|---|
| `.cai-staging/agents/` | write/overwrite agent `.md` files | `rglob("*.md")` | `.claude/agents/<rel>` |
| `.cai-staging/agents-delete/` | tombstone — delete matching agent file | `rglob("*.md")` | `.claude/agents/<rel>` |
| `.cai-staging/plugins/` | overwrite/merge plugin tree | `shutil.copytree(dirs_exist_ok=True)` | `.claude/plugins/` |
| `.cai-staging/claudemd/` | write/overwrite `CLAUDE.md` files | `rglob("CLAUDE.md")` | `<work_dir>/<rel>` |

### `.cai-staging/agents/`

Handled by the first block in `_apply_agent_edit_staging`. Every `.md`
file found by `rglob("*.md")` under this subdir is copied — relative
path preserved — to `.claude/agents/<rel>`. If the target already
exists it is unconditionally overwritten. If it does not exist, the
parent directory is created first.

Example: `.cai-staging/agents/lifecycle/cai-triage.md` → `.claude/agents/lifecycle/cai-triage.md`

### `.cai-staging/agents-delete/`

Handled by the fourth block in `_apply_agent_edit_staging` (after
CLAUDE.md writes). Every `.md` file found by `rglob("*.md")` is
treated as a **tombstone**: only its relative path matters; file
contents are ignored. If `.claude/agents/<rel>` exists as a regular
file it is deleted. If the target is already absent, the tombstone is
silently skipped (stale tombstones are safe). Non-`.md` files in this
subdir are ignored.

Example: `.cai-staging/agents-delete/cai-triage.md` → deletes `.claude/agents/cai-triage.md`

### `.cai-staging/plugins/`

Handled by the second block in `_apply_agent_edit_staging`. The entire
subtree is merged into `.claude/plugins/` via `shutil.copytree(src,
dst, dirs_exist_ok=True)`. This means existing files are overwritten,
but files in `.claude/plugins/` that have no counterpart in the
staging tree are left untouched. Any file type is supported (no `.md`
filter).

Example: `.cai-staging/plugins/cai-skills/skills/foo/SKILL.md` → `.claude/plugins/cai-skills/skills/foo/SKILL.md`

### `.cai-staging/claudemd/`

Handled by the third block in `_apply_agent_edit_staging`. Files named
**exactly** `CLAUDE.md` found by `rglob("CLAUDE.md")` are copied —
relative path preserved — to `<work_dir>/<rel>`. Non-`CLAUDE.md`
files in the staging tree are silently ignored.

Example: `.cai-staging/claudemd/CLAUDE.md` → `<work_dir>/CLAUDE.md`
Example: `.cai-staging/claudemd/subdir/CLAUDE.md` → `<work_dir>/subdir/CLAUDE.md`

## Apply Order

The wrapper applies staged content in this fixed order:

1. **Agent writes** (`.cai-staging/agents/`)
2. **Plugin copytree** (`.cai-staging/plugins/`)
3. **CLAUDE.md writes** (`.cai-staging/claudemd/`)
4. **Agent deletions** (`.cai-staging/agents-delete/`)
5. **Cleanup** (`shutil.rmtree(.cai-staging)`)

**Why this order matters:** writes-before-deletes means a rename
(write-new + tombstone-old) works correctly in a single apply pass,
even when the new and old paths share a common ancestor directory.
CLAUDE.md writes come before deletions so that a CLAUDE.md write
failure triggers early-return without also losing pending deletions
(the staged content is preserved for inspection). Cleanup is last so
that any preceding failure leaves the staging tree intact.

## Security Invariant

The `.cai-staging/` directory lives entirely **inside `work_dir`**, so
path traversal via `..` is structurally impossible. The wrapper never
follows user-supplied absolute paths — it only iterates descendants of
the staging root via `rglob()`. Tombstone file contents are ignored;
only the relative path is used.

## Failure Semantics

Each step has distinct failure behavior:

- **Agent `.md` write failure** — logged to stderr, loop **continues**
  to the next file. Other staged files are not affected.
  (Implementation: `except OSError: continue` at `cmd_helpers_git.py` ~line 279–285.)

- **Plugin copytree failure** — logged to stderr, function **returns
  early**. `.cai-staging/` is preserved for inspection. CLAUDE.md
  writes and agent deletions do NOT run.
  (Implementation: `return applied` at ~line 308.)

- **CLAUDE.md write failure** — logged to stderr, function **returns
  early**. `.cai-staging/` is preserved. Agent deletions do NOT run.
  (Implementation: `return applied` at ~line 336.)

- **Tombstone deletion failure** — logged to stderr, loop **continues**
  to the next tombstone. A missing target file is not an error — it is
  silently skipped with a log message.
  (Implementation: `except OSError: continue` at ~line 366–372.)

- **Cleanup (`rmtree`) failure** — logged to stderr, **non-fatal**.
  (Implementation: `except OSError: print(...)` at ~line 380–385.)

## Audit: Missing / Known-Limited Cases

### Plugin deletions

Currently **unsupported**. The plugin staging subdir supports
overwrite/merge via `shutil.copytree` but there is no equivalent of
`agents-delete/` for plugins. File a new issue if plugin deletion
support is needed.

### CLAUDE.md deletions

Not applicable. `CLAUDE.md` files are a fixed set; there is no use
case for deleting them via staging.

### Renames

A rename is implemented as the idiom **write-new + tombstone-old** in
one staging pass. The apply order (writes before deletions) guarantees
the new file is created before the old is removed, even when both
paths share a common parent directory.

### Binary files

Agent tombstone-delete and agent-write both use `rglob("*.md")` — only
`.md` files are processed; stray binaries in `.cai-staging/agents/`
or `.cai-staging/agents-delete/` are silently ignored. Plugin staging
uses `shutil.copytree` with no filter, so any file type is supported
under `.cai-staging/plugins/`.

## Example: Canonical Flat-to-Subfolder Agent Migration

Stage two operations in a single pass:

```python
# Step 1 — write the new subfolder copy via staging
Write("<work_dir>/.cai-staging/agents/lifecycle/cai-triage.md", "<full new content>")

# Step 2 — tombstone the old flat copy
Write("<work_dir>/.cai-staging/agents-delete/cai-triage.md", "")
```

After `_apply_agent_edit_staging` runs:
- `.claude/agents/lifecycle/cai-triage.md` exists with the new content.
- `.claude/agents/cai-triage.md` has been deleted.
- `.cai-staging/` has been cleaned up.

## Cross-References

- Implementation: [`cai_lib/cmd_helpers_git.py`](../cai_lib/cmd_helpers_git.py)
  — `_setup_agent_edit_staging` and `_apply_agent_edit_staging`
- Matrix test: [`tests/test_agent_staging.py`](../tests/test_agent_staging.py)
- Subagent-facing rules: [root `CLAUDE.md`](../CLAUDE.md)
  (the "Self-modifying" section)
