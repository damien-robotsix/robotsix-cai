"""Agent-launch cmd_* functions extracted from cai.py.

After audit-refactor 7.2 the eight periodic/creative agent commands
(analyze, audit, propose, code-audit, agent-audit, update-check,
cost-optimize, external-scout) were removed. This module now contains
the on-demand per-module audit dispatcher and the audit-health runner:

  cmd_audit_module  — ``cai audit-module --kind <kind>``
  cmd_audit_health  — ``cai audit-health``
"""

import shutil
import sys
import time
import uuid
from pathlib import Path

from cai_lib.config import *  # noqa: F403,F401
from cai_lib.logging_utils import log_run


# ---------------------------------------------------------------------------
# audit-module — on-demand per-module audit dispatcher
# ---------------------------------------------------------------------------

def cmd_audit_module(args) -> int:
    """Dispatch ``cai audit-module --kind <kind>``.

    Delegates to :func:`cai_lib.audit.runner.run_module_audit`, which
    owns all kind validation, manifest loading, per-module
    invocation, and publish-via-subprocess plumbing. This wrapper
    only handles CLI-level error reporting and ``log_run`` telemetry.
    Returns 0 when every module published cleanly, 1 when at least
    one module failed (or the whole run was refused up-front).
    """
    # Deferred import so that a top-of-file import of cai_lib.audit.runner
    # doesn't drag PyYAML into cmd_agents.py on every cai invocation
    # (load_modules pulls in yaml lazily inside runner.py).
    from cai_lib.audit.runner import run_module_audit

    kind = args.kind
    t0 = time.monotonic()
    try:
        total_run, total_failed = run_module_audit(kind)
    except ValueError as exc:
        # unknown kind — argparse choices should have caught this,
        # but guard anyway so a direct call with a bad kind reports
        # cleanly instead of crashing.
        print(f"[cai audit-module] {exc}", file=sys.stderr, flush=True)
        log_run(f"audit-{kind}", result="unknown_kind", exit=1)
        return 1
    except FileNotFoundError as exc:
        print(
            f"[cai audit-module] docs/modules.yaml missing: {exc}",
            file=sys.stderr, flush=True,
        )
        log_run(f"audit-{kind}", result="no_manifest", exit=1)
        return 1
    except (OSError,) as exc:
        print(
            f"[cai audit-module] failed to load modules manifest: {exc}",
            file=sys.stderr, flush=True,
        )
        log_run(f"audit-{kind}", result="manifest_error", exit=1)
        return 1

    dur = f"{int(time.monotonic() - t0)}s"
    exit_code = 0 if total_failed == 0 else 1
    log_run(
        f"audit-{kind}",
        modules=total_run,
        failures=total_failed,
        duration=dur,
        exit=exit_code,
    )
    return exit_code


# ---------------------------------------------------------------------------
# audit-health — on-demand audit-health runner
# ---------------------------------------------------------------------------

def cmd_audit_health(args) -> int:
    """Dispatch ``cai audit-health``.

    Runs the ``cai-audit-audit-health`` agent, which reads
    ``/var/log/cai/audit/*/*.jsonl`` for the last 30 days and raises
    findings for error conditions or anomalies (stale audits, cost
    spikes, degenerate zero-findings runs, etc.).  Findings are
    published via ``publish.py --namespace audit-health``.
    """
    from cai_lib.subagent import _run_claude_p
    from cai_lib.subprocess_utils import _run

    agent = "cai-audit-audit-health"
    work_dir = Path(f"/tmp/cai-audit-health-{uuid.uuid4().hex[:8]}")
    work_dir.mkdir(parents=True, exist_ok=True)
    findings_file = work_dir / "findings.json"

    t0 = time.monotonic()
    try:
        user_message = (
            "## Audit Log Directory\n\n"
            f"Read audit logs from: `{AUDIT_LOG_DIR}`\n\n"
            "## Findings file\n\n"
            f"Write your findings to: `{findings_file}`\n"
        )
        proc = _run_claude_p(
            [
                "claude", "-p",
                "--agent", agent,
                "--permission-mode", "acceptEdits",
                "--allowedTools", "Read,Grep,Glob,Write",
                "--add-dir", str(work_dir),
            ],
            category="audit-health",
            agent=agent,
            input=user_message,
            cwd="/app",
        )
        if proc.stdout:
            print(proc.stdout)
        if proc.returncode != 0:
            print(
                f"[cai audit-health] ERROR: agent {agent} exited {proc.returncode}",
                file=sys.stderr, flush=True,
            )
            log_run("audit-health", result="agent_failed", exit=1)
            return 1
        if not findings_file.exists():
            print("[cai audit-health] agent wrote no findings", flush=True)
            log_run("audit-health", result="no_findings", exit=0)
            return 0
        published = _run([
            "python", str(PUBLISH_SCRIPT),
            "--namespace", "audit-health",
            "--findings-file", str(findings_file),
        ])
        if published.returncode != 0:
            print(
                f"[cai audit-health] ERROR: publish failed with {published.returncode}",
                file=sys.stderr, flush=True,
            )
            log_run("audit-health", result="publish_failed", exit=1)
            return 1
        dur = f"{int(time.monotonic() - t0)}s"
        log_run("audit-health", result="ok", duration=dur, exit=0)
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"[cai audit-health] ERROR: {exc}", file=sys.stderr, flush=True)
        log_run("audit-health", result="unexpected_error", exit=1)
        return 1
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
