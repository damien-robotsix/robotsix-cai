# Project Guidelines

This file is loaded by all Claude Code subagents in headless mode.

## Efficiency guidance (all agents)

0. **Consult `docs/modules.yaml` first.** Before using Glob or
   Grep for file discovery, check `docs/modules.yaml` at the repo
   root — it is the primary index of every module with file locations
   and descriptions. For deeper detail on a specific module, read the
   corresponding narrative in `docs/modules/<name>.md`. These files
   are the single source of truth for codebase structure and eliminate
   the need for an initial discovery round.
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

## Project layout

`.claude/agents/` is organized into six subfolders, each holding
the agent prompt files for that category:

  - `audit/` — on-demand audit agents that write to
    `findings.json`, plus transcript-search helpers
    (cai-audit-code-reduction, cai-audit-cost-reduction,
    cai-audit-external-libs, cai-audit-good-practices,
    cai-audit-workflow-enhancement, cai-transcript-finder).
  - `implementation/` — code-editing and planning agents invoked
    by the FSM (cai-fix-ci, cai-implement, cai-plan, cai-rebase,
    cai-revise, cai-select).
  - `lifecycle/` — FSM lifecycle handlers and helpers
    (cai-confirm, cai-dup-check, cai-explore, cai-propose,
    cai-propose-review, cai-refine, cai-rescue,
    cai-split, cai-triage, cai-unblock).
  - `ops/` — operational / maintenance agents
    (cai-check-workflows, cai-maintain, cai-update-check).
  - `review/` — pre-merge code and docs review plus merge
    verdict agents (cai-comment-filter, cai-merge, cai-review-docs,
    cai-review-pr).
  - `utility/` — shared helper agents used across pipelines
    (cai-cost-optimize, cai-external-scout, cai-git, cai-memorize).

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
     see the existing content: `Read("<work_dir>/.claude/agents/<relative-path>.md")`.
     (Read is allowed; only Edit/Write on that path is blocked.)
  2. **Write** the FULL new file content (YAML frontmatter +
     body, exactly what you want the final file to look like)
     to `<work_dir>/.cai-staging/agents/<same-relative-path>.md`
     using the Write tool. Preserve any subdirectory structure.
  3. The wrapper recursively walks `.cai-staging/agents/`, preserves
     subdirectory paths, and copies each file to the matching path in
     `.claude/agents/` after you exit successfully, then deletes the
     staging directory so it doesn't land in the PR.

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
     `<work_dir>/` after you exit, then deletes the
     staging directory.

**For deleting agent files** (`.claude/agents/*.md` removals and
migrations):

  1. **Write** an empty (or any-content) `.md` tombstone file to
     `<work_dir>/.cai-staging/agents-delete/<same-relative-path>.md`.
     For example, to delete `.claude/agents/cai-triage.md`, write to
     `.cai-staging/agents-delete/cai-triage.md`. To delete
     `.claude/agents/lifecycle/cai-triage.md`, write to
     `.cai-staging/agents-delete/lifecycle/cai-triage.md`.
  2. The wrapper walks `.cai-staging/agents-delete/` with
     `rglob("*.md")` after you exit and deletes each matching file
     at `.claude/agents/<relative-path>.md`. Tombstone contents are
     ignored — only the relative path matters. Missing targets are
     silently skipped (stale tombstones are safe). Non-`.md` files
     in the tombstone tree are ignored.
  3. Do NOT try `Bash("rm ...")` on `.claude/agents/...` — it is
     blocked by the same sensitive-file protection.

**For deleting arbitrary repo files** (anywhere under `<work_dir>/`
other than `.git/` and `.cai-staging/`):

  1. **Write** an empty (or any-content) tombstone file to
     `<work_dir>/.cai-staging/files-delete/<same-relative-path>`.
     For example, to delete `cai_lib/cmd_agents.py`, write to
     `.cai-staging/files-delete/cai_lib/cmd_agents.py`. To delete
     `tests/test_retroactive_sweep.py`, write to
     `.cai-staging/files-delete/tests/test_retroactive_sweep.py`.
  2. The wrapper walks `.cai-staging/files-delete/` with
     `rglob("*")` after you exit and deletes each matching file at
     `<work_dir>/<relative-path>`. Tombstone contents are
     ignored — only the relative path matters. Targets must be
     tracked by git; untracked targets, paths under `.git/` or
     `.cai-staging/`, and symlink-escape attempts are refused with
     a stderr warning. Missing targets are silently skipped.
  3. Use this when you need to delete a file and do NOT have Bash
     (e.g. `cai-implement`). Do NOT stub the file with
     `raise ImportError(...)` as a workaround — leave the tree
     clean.

Rules (apply to agents, plugins, CLAUDE.md files, and agent deletions):

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

  - GOOD (implementation): `Read("<work_dir>/.claude/agents/implementation/cai-implement.md")` then
    `Write("<work_dir>/.cai-staging/agents/implementation/cai-implement.md", "<full new content>")`
  - GOOD (lifecycle): `Read("<work_dir>/.claude/agents/lifecycle/cai-triage.md")` then
    `Write("<work_dir>/.cai-staging/agents/lifecycle/cai-triage.md", "<full new content>")`
  - BAD:  `Edit("<work_dir>/.claude/agents/implementation/cai-implement.md", old, new)`  (blocked)

Example of creating a plugin skill:

  - GOOD: `Write("<work_dir>/.cai-staging/plugins/cai-skills/skills/foo/SKILL.md", "<full content>")`
  - BAD:  `Write("<work_dir>/.claude/plugins/cai-skills/skills/foo/SKILL.md", ...)`  (blocked)

Example of updating root CLAUDE.md:

  - GOOD: `Write("<work_dir>/.cai-staging/claudemd/CLAUDE.md", "<full content>")`
  - BAD:  `Write("<work_dir>/CLAUDE.md", ...)`  (blocked)

Example of deleting an agent file:

  - GOOD: `Write("<work_dir>/.cai-staging/agents-delete/cai-triage.md", "")`
  - BAD:  `Bash("rm <work_dir>/.claude/agents/cai-triage.md")`  (blocked)

Example of deleting an arbitrary repo file:

  - GOOD: `Write("<work_dir>/.cai-staging/files-delete/cai_lib/cmd_agents.py", "")`
  - BAD:  Stubbing the file with `raise ImportError(...)` as a workaround

## Invoking cai-test-runner

When your agent definition file directs you to "use the standard
`cai-test-runner` invocation recipe in `CLAUDE.md`", use this block:

~~~
Agent(
  subagent_type="cai-test-runner",
  description="Run regression tests",
  prompt="work_dir=<work_dir>"
)
~~~

Parse the reply's `## Test Result` header. On `PASS`, proceed to the
next step. On `FAIL`:

1. Read the `## Failures` block to identify which tests broke and why.
2. Decide which side is correct:
   - **Your change is wrong** — fix the code.
   - **The test pins obsolete behavior** — update the test.
3. Re-invoke `cai-test-runner` to confirm the fix.
4. **Cap yourself at two iterations.** If the same or a new failure is
   still present after two fix attempts, stop and exit anyway — do
   not burn the rest of your turn budget chasing a test you cannot
   reason about. The wrapper will push the PR regardless and handle
   downstream routing if tests still fail post-push.

A green run is strongly preferred but not mandatory. Your goal is to
hand off the cleanest tree possible — not to guarantee zero failures.
