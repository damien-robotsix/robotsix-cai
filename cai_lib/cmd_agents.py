"""Agent-launch cmd_* functions extracted from cai.py.

After audit-refactor 7.2 the eight periodic/creative agent commands
(analyze, audit, propose, code-audit, agent-audit, update-check,
cost-optimize, external-scout) were removed. This module now contains
only the on-demand per-module audit dispatcher:

  cmd_audit_module  — ``cai audit-module --kind <kind>``
"""

import sys
import time

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
