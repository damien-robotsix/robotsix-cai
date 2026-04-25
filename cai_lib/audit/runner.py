"""On-demand per-module audit runner.

For a given audit kind, iterates every module declared in
``docs/modules.yaml``, invokes the matching per-module agent, and
publishes the agent's ``findings.json`` through the existing publish
pipeline by shelling out to ``publish.py`` — the same pattern every
other audit caller (``cmd_external_scout``, ``cmd_update_check``,
etc.) already uses.

Each created issue carries a ``<!-- module: <name> -->`` body footer
(emitted by ``publish.py`` when the runner passes ``--module``) so
future audit runs can scope dedup by module + fingerprint.

Per-module failures (agent exit != 0, missing findings file, publish
failure, unexpected exception) are logged to stderr and counted but
never raised — one failing module must not abort the remaining
modules in the loop.
"""

import json
import shutil
import sys
import time
import uuid
from pathlib import Path

from cai_lib.audit.cost import _build_cost_summary
from cai_lib.audit_logging import audit_log_finish, audit_log_start
from cai_lib.config import PUBLISH_SCRIPT
from cai_lib.utils.log import log_run
from cai_lib.claude_argv import _run_claude_p
from cai_lib.subprocess_utils import _run


# Map CLI ``--kind`` argument to the on-demand audit agent that must
# run once per module. Each kind has a matching publish namespace of
# the form ``audit-<kind>`` registered in :mod:`cai_lib.publish`.
KIND_TO_AGENT = {
    "good-practices":       "cai-audit-good-practices",
    "code-reduction":       "cai-audit-code-reduction",
    "cost-reduction":       "cai-audit-cost-reduction",
    "workflow-enhancement": "cai-audit-workflow-enhancement",
}


def _build_module_prompt(entry, findings_file: Path) -> str:  # type: ignore[no-untyped-def]
    """Construct the user message for the on-demand audit agent.

    The four agents under ``.claude/agents/audit/cai-audit-*.md`` all
    expect a ``## Module`` section (name, summary, globs, optional doc
    snippet) followed by a ``## Findings file`` section pointing at
    the absolute path where they must write ``findings.json``.

    Also pulls fresh cost data (no-op when sync is disabled) and appends
    a ``## Cost summary`` block so the audit agent receives spend context.
    """
    try:
        from cai_lib.transcript_sync import pull_cost  # noqa: PLC0415
        pull_cost()
    except Exception:  # noqa: BLE001
        pass

    globs_block = "\n".join(f"- `{g}`" for g in entry.globs) if entry.globs else "- (none)"
    parts = [
        "## Module",
        "",
        f"**Name:** {entry.name}",
        f"**Summary:** {entry.summary or '(no summary)'}",
        "**File globs:**",
        globs_block,
    ]
    if entry.doc:
        parts += ["", "```", entry.doc, "```"]
    parts += [
        "",
        "## Findings file",
        "",
        f"Write your findings to: `{findings_file}`",
        "",
    ]
    cost_summary = _build_cost_summary()
    if cost_summary:
        parts += [
            "",
            "## Cost summary",
            "",
            cost_summary,
        ]
    return "\n".join(parts)


def _run_one_module(kind: str, agent: str, entry) -> int:  # type: ignore[no-untyped-def]
    """Run the audit agent for one module and publish any findings.

    Returns 0 on success (agent ran and publish succeeded, or the
    agent wrote no findings), 1 on any failure. Per-module failures
    must never propagate — the caller uses the return code only to
    count failures.
    """
    work_dir = Path(f"/tmp/cai-audit-{kind}-{uuid.uuid4().hex[:8]}")
    work_dir.mkdir(parents=True, exist_ok=True)
    findings_file = work_dir / "findings.json"

    audit_log_start(kind, entry.name, agent)
    proc = None  # set inside try block; kept for exception-handler visibility

    try:
        user_message = _build_module_prompt(entry, findings_file)
        if kind == "cost-reduction" and sys.stderr.isatty():
            _banner = _build_cost_summary()
            if _banner:
                print(
                    f"\n--- cost summary (module={entry.name}) ---\n"
                    f"{_banner}"
                    f"--- end cost summary ---\n",
                    file=sys.stderr,
                    flush=True,
                )
        proc = _run_claude_p(
            [
                "claude", "-p",
                "--agent", agent,
                "--permission-mode", "acceptEdits",
                "--allowedTools", "Read,Grep,Glob,Agent,Write",
                "--add-dir", str(work_dir),
            ],
            category=f"audit-{kind}",
            agent=agent,
            input=user_message,
            cwd="/app",
            module=entry.name,
        )

        if proc.stdout:
            print(proc.stdout)

        if proc.returncode != 0:
            print(
                f"[cai audit-{kind}] ERROR: module {entry.name} failed — "
                f"agent {agent} exited {proc.returncode}",
                file=sys.stderr,
                flush=True,
            )
            audit_log_finish(
                kind, entry.name, agent,
                proc=proc,
                findings_count=None,
                exit_code=1,
                error_class="agent_nonzero",
                message=f"agent {agent} exited {proc.returncode}",
            )
            return 1

        if not findings_file.exists():
            # No findings is a valid outcome — nothing to publish.
            audit_log_finish(
                kind, entry.name, agent,
                proc=proc,
                findings_count=0,
                exit_code=0,
                message="no findings written",
            )
            return 0

        # Quick shape check before invoking publish.py. A malformed
        # findings.json would cause publish.py to sys.exit(1) inside
        # load_findings_json; catching it here lets the loop count
        # this module as failed and carry on.
        try:
            data = json.loads(findings_file.read_text())
            if not isinstance(data, dict) or not isinstance(data.get("findings"), list):
                print(
                    f"[cai audit-{kind}] ERROR: module {entry.name} failed — "
                    f"findings.json missing top-level 'findings' list",
                    file=sys.stderr,
                    flush=True,
                )
                audit_log_finish(
                    kind, entry.name, agent,
                    proc=proc,
                    findings_count=None,
                    exit_code=1,
                    error_class="findings_missing_list",
                    message="findings.json missing top-level 'findings' list",
                )
                return 1
        except (json.JSONDecodeError, OSError) as exc:
            print(
                f"[cai audit-{kind}] ERROR: module {entry.name} failed — "
                f"could not read findings.json: {exc}",
                file=sys.stderr,
                flush=True,
            )
            audit_log_finish(
                kind, entry.name, agent,
                proc=proc,
                findings_count=None,
                exit_code=1,
                error_class="findings_parse_error",
                message=f"could not read findings.json: {exc}",
            )
            return 1

        namespace = f"audit-{kind}"
        published = _run(
            [
                "python", str(PUBLISH_SCRIPT),
                "--namespace", namespace,
                "--module", entry.name,
                "--findings-file", str(findings_file),
            ],
        )
        if published.returncode != 0:
            print(
                f"[cai audit-{kind}] ERROR: module {entry.name} failed — "
                f"publish.py returned {published.returncode}",
                file=sys.stderr,
                flush=True,
            )
            audit_log_finish(
                kind, entry.name, agent,
                proc=proc,
                findings_count=len(data.get("findings", [])),
                exit_code=1,
                error_class="publish_failed",
                message=f"publish.py returned {published.returncode}",
            )
            return 1

        audit_log_finish(
            kind, entry.name, agent,
            proc=proc,
            findings_count=len(data.get("findings", [])),
            exit_code=0,
        )
        return 0
    except Exception as exc:  # noqa: BLE001
        print(
            f"[cai audit-{kind}] ERROR: module {entry.name} failed — "
            f"unexpected exception: {exc}",
            file=sys.stderr,
            flush=True,
        )
        audit_log_finish(
            kind, entry.name, agent,
            proc=proc,
            findings_count=None,
            exit_code=1,
            error_class="unexpected_exception",
            message=f"unexpected exception: {exc}",
        )
        return 1
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def run_module_audit(kind: str) -> tuple[int, int]:
    """Iterate every module from ``docs/modules.yaml`` for the given kind.

    Returns ``(total_modules_run, total_modules_failed)``. Raises
    ``ValueError`` for an unknown kind and ``FileNotFoundError`` when
    ``docs/modules.yaml`` is missing — both caught by the CLI
    wrapper. Per-module failures are logged to stderr, counted, and
    never propagate out of the loop.
    """
    # Deferred import: modules.py pulls in PyYAML, which is always
    # available in the container, but keeping the import local keeps
    # cai.py's import chain light when audit-module is never used.
    from cai_lib.audit.modules import load_modules

    if kind not in KIND_TO_AGENT:
        raise ValueError(
            f"unknown audit kind {kind!r}; choices: {list(KIND_TO_AGENT)}"
        )

    agent = KIND_TO_AGENT[kind]
    manifest = Path(__file__).resolve().parents[2] / "docs/modules.yaml"
    modules = load_modules(manifest)

    t0 = time.monotonic()
    total_run = 0
    total_failed = 0
    for entry in modules:
        rc = _run_one_module(kind, agent, entry)
        total_run += 1
        if rc != 0:
            total_failed += 1

    elapsed = time.monotonic() - t0
    print(
        f"[cai audit-{kind}] done: modules={total_run} "
        f"failures={total_failed} elapsed={elapsed:.1f}s",
        flush=True,
    )
    return (total_run, total_failed)
