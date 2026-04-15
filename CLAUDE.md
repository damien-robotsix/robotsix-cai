# Project Guidelines

This file is loaded by all Claude Code subagents in headless mode.

## Efficiency guidance (all agents)

0. **Consult `CODEBASE_INDEX.md` first.** Before using Glob or
   Grep for file discovery, check `CODEBASE_INDEX.md` at the repo
   root — it lists every tracked file with a one-line description
   and eliminates the need for an initial discovery round.
1. **Grep before Read.** Use Grep to locate the relevant file(s)
   and line numbers before opening them with Read. Do not
   sequentially Read files to search for content — reserve Read for
   files whose paths and relevance are already known.
2. **Verify paths with Glob before Read.** When a file path is
   constructed or inferred (not hard-coded), confirm the file exists
   using Glob before attempting to Read it. If a Read fails, do not
   retry the same path — use Glob to find the correct filename
   first.
3. **Batch independent Read calls.** When you need to read multiple
   files and the reads are independent, issue all Read calls in a
   single turn rather than one at a time.
4. **Batch Grep calls.** When searching for multiple patterns or
   across multiple paths, combine them into a single Grep call using
   regex alternation (`pat1|pat2`) or issue independent Grep calls
   in parallel rather than sequentially. Use Glob first to narrow
   the file set, then Grep the results, instead of running
   exploratory Grep calls one at a time.

## Your working directory and the canonical /app location

**Your `cwd` is `/app`, NOT the clone.** `/app` is where your declarative
agent definition lives. Treat `/app` as **read-only** — edits there land
in the container's writable layer and are lost on next restart, never
making it into git.

**Your actual work happens on a fresh clone of the repository at a
path the wrapper provides in the user message** (look for the
`## Work directory` section).

**Use absolute paths under the work directory for everything you
read or edit.** Relative paths resolve to `/app` (the canonical,
baked-in source) and any edit there is wasted.

  - GOOD: `Read("<work_dir>/cai.py")`
  - BAD:  `Read("cai.py")`         (reads /app/cai.py)
  - GOOD: `Edit("<work_dir>/parse.py", ...)`
  - BAD:  `Edit("parse.py", ...)`  (edits /app/parse.py)

**Note:** `cai.py` is ~63 k tokens — a whole-file `Read("<work_dir>/cai.py")`
will exceed the token limit. Use `Grep(pattern, path="<work_dir>")` for
symbol search and `Read("<work_dir>/cai.py", offset=N, limit=200)` for
targeted sections.

## Self-modifying `.claude/agents/*.md`, `.claude/plugins/`, and `CLAUDE.md` (staging directory)

**Claude-code's headless `-p` mode hardcodes a write block on
every `.claude/agents/*.md` path**, regardless of any permission
flag or `settings.json` rule. `Edit` or `Write` calls against
`<work_dir>/.claude/agents/<any-file>.md` WILL fail with a sensitive-file
protection error — you cannot bypass it from inside your session.

The same protection applies to **`.claude/plugins/`** — you cannot
write plugin files directly there either.

The **staging directory** at `<work_dir>/.cai-staging/` that the
wrapper pre-creates is the workaround for both cases:

**For agent definition files** (`.claude/agents/*.md`):

  1. **Read** the current agent file at its clone-side path to
     see the existing content: `Read("<work_dir>/.claude/agents/<basename>.md")`.
     (Read is allowed; only Edit/Write on that path is blocked.)
  2. **Write** the FULL new file content (YAML frontmatter +
     body, exactly what you want the final file to look like)
     to `<work_dir>/.cai-staging/agents/<same-basename>.md`
     using the Write tool.
  3. The wrapper copies `.cai-staging/agents/*.md` over
     `.claude/agents/*.md` (matching by basename) after you exit
     successfully, then deletes the staging directory so it
     doesn't land in the PR.

**For plugin files** (`.claude/plugins/<plugin-path>`):

  1. **Write** the plugin file content to
     `<work_dir>/.cai-staging/plugins/<same-relative-path>`.
     Preserve the full directory structure under `plugins/`.
     For example, to create
     `.claude/plugins/cai-skills/skills/foo/SKILL.md`, write to
     `.cai-staging/plugins/cai-skills/skills/foo/SKILL.md`.
  2. The wrapper merges `.cai-staging/plugins/` into
     `.claude/plugins/` using `shutil.copytree` with
     `dirs_exist_ok=True` after you exit, then deletes the
     staging directory.

**For `CLAUDE.md` files** (root or any subdirectory):

  1. **Write** the full `CLAUDE.md` content to
     `<work_dir>/.cai-staging/claudemd/<same-relative-path>/CLAUDE.md`.
     Preserve the directory structure. To update the root
     `CLAUDE.md`, write to `.cai-staging/claudemd/CLAUDE.md`. To
     update `subdir/CLAUDE.md`, write to
     `.cai-staging/claudemd/subdir/CLAUDE.md`.
  2. The wrapper scans `.cai-staging/claudemd/` for all files named
     exactly `CLAUDE.md` and copies each to the matching path in
     `<work_dir>/` after you exit, then deletes the staging
     directory.

Rules (apply to agents, plugins, and CLAUDE.md files):

  - Staged files are copied unconditionally — new definitions
    are created if no target exists yet.
  - Write the FULL file, not a diff. The wrapper does an
    unconditional overwrite.
  - Use the exact same relative path as the target under their
    respective subdirectory (e.g. `cai-implement.md` → `cai-implement.md`,
    `cai-skills/skills/foo/SKILL.md` → same path under plugins/).
  - Do NOT try `Edit`/`Write` on `<work_dir>/.claude/agents/...`
    or `<work_dir>/.claude/plugins/...` — it will always fail.
    Go through the staging directory.

Example of updating an agent file:

  - GOOD: `Read("<work_dir>/.claude/agents/cai-implement.md")` then
    `Write("<work_dir>/.cai-staging/agents/cai-implement.md", "<full new content>")`
  - BAD:  `Edit("<work_dir>/.claude/agents/cai-implement.md", old, new)`  (blocked)

Example of creating a plugin skill:

  - GOOD: `Write("<work_dir>/.cai-staging/plugins/cai-skills/skills/foo/SKILL.md", "<full content>")`
  - BAD:  `Write("<work_dir>/.claude/plugins/cai-skills/skills/foo/SKILL.md", ...)`  (blocked)

Example of updating root CLAUDE.md:

  - GOOD: `Write("<work_dir>/.cai-staging/claudemd/CLAUDE.md", "<full content>")`
  - BAD:  `Write("<work_dir>/CLAUDE.md", ...)`  (blocked)
