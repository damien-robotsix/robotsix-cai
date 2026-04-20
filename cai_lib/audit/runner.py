"""On-demand per-module audit runner."""

import json
import shutil
import sys
import time
import uuid
from pathlib import Path

from cai_lib.config import PUBLISH_SCRIPT
from cai_lib.logging_utils import log_run
from cai_lib.subprocess_utils import _run, _run_claude_p

MODULES_YAML_REL = Path("docs/modules.yaml")

KIND_TO_AGENT = {
    "cost": "cai-audit-cost-reduction",
    # Remaining kinds (code, workflow, best-practices, external-libs) are
    # added here as their on-demand agents ship in the #3.x issue series.
}
AUDIT_KINDS = tuple(KIND_TO_AGENT.keys())


def _resolve_manifest_path() -> Path:
    """Return the absolute path to docs/modules.yaml in the repo root.

    ``cai_lib/audit/runner.py`` → parents[0]=audit, parents[1]=cai_lib,
    parents[2]=repo root.
    """
    return Path(__file__).resolve().parents[2] / MODULES_YAML_REL


def _build_module_message(entry, findings_file: Path) -> str:  # type: ignore[no-untyped-def]
    """Construct the user message for the on-demand audit agent.

    Format matches the schema declared in cai-audit-cost-reduction.md §What you receive.
    """
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
    return "\n".join(parts)


def _run_one_module(kind: str, agent: str, entry) -> tuple[int, list]:  # type: ignore[no-untyped-def]
    """Run the audit agent for a single module entry.

    Returns ``(returncode, findings_list)``.  Always cleans up the temp dir.
    """
    work_dir = Path(f"/tmp/cai-audit-{kind}-{uuid.uuid4().hex[:8]}")
    work_dir.mkdir(parents=True, exist_ok=True)
    findings_file = work_dir / "findings.json"
    user_message = _build_module_message(entry, findings_file)

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
    )

    if proc.stdout:
        print(proc.stdout)

    if proc.returncode != 0:
        print(
            f"[cai audit] agent {agent} exited {proc.returncode} for module {entry.name}",
            file=sys.stderr,
            flush=True,
        )
        shutil.rmtree(work_dir, ignore_errors=True)
        return (proc.returncode, [])

    findings_list: list = []
    if findings_file.exists():
        try:
            data = json.loads(findings_file.read_text())
            raw = data.get("findings")
            if isinstance(raw, list):
                findings_list = raw
            else:
                print(
                    f"[cai audit] findings.json for {entry.name} missing 'findings' list",
                    file=sys.stderr,
                    flush=True,
                )
        except (json.JSONDecodeError, OSError) as exc:
            print(
                f"[cai audit] could not read findings.json for {entry.name}: {exc}",
                file=sys.stderr,
                flush=True,
            )

    shutil.rmtree(work_dir, ignore_errors=True)
    return (0, findings_list)


def cmd_audit_run(args) -> int:  # type: ignore[no-untyped-def]
    """Dispatcher for ``cai audit <kind> [--module <name>] [--all]``."""
    # Deferred import so that cai.py can load even before #886 ships
    # (modules.py is a stub until that PR merges).
    try:
        from cai_lib.audit.modules import load_modules
    except ImportError as exc:
        print(
            f"[cai audit] cannot import load_modules — is issue #886 merged? ({exc})",
            file=sys.stderr,
            flush=True,
        )
        return 1

    agent = KIND_TO_AGENT.get(args.kind)
    if not agent:
        print(f"[cai audit] unknown kind {args.kind!r}; choices: {list(KIND_TO_AGENT)}", file=sys.stderr, flush=True)
        return 1

    if not args.all and not args.module:
        print(
            "[cai audit] supply --module <name> to audit a single module or --all to audit every module",
            file=sys.stderr,
            flush=True,
        )
        return 1

    t0 = time.monotonic()
    manifest = _resolve_manifest_path()

    try:
        modules = load_modules(manifest)
    except FileNotFoundError as exc:
        print(
            f"[cai audit] {exc} — create docs/modules.yaml (tracked in #886)",
            file=sys.stderr,
            flush=True,
        )
        log_run(f"audit-{args.kind}", result="no_manifest", exit=1)
        return 1
    except (ValueError, OSError) as exc:
        print(f"[cai audit] failed to parse modules manifest: {exc}", file=sys.stderr, flush=True)
        log_run(f"audit-{args.kind}", result="parse_error", exit=1)
        return 1

    if args.module:
        targets = [m for m in modules if m.name == args.module]
        if not targets:
            available = ", ".join(m.name for m in modules)
            print(
                f"[cai audit] module {args.module!r} not found; available: {available}",
                file=sys.stderr,
                flush=True,
            )
            return 1
    else:
        targets = modules
        if not targets:
            print("[cai audit] docs/modules.yaml has no module entries", file=sys.stderr, flush=True)
            return 1

    all_findings: list = []
    failures = 0
    for entry in targets:
        rc, found = _run_one_module(args.kind, agent, entry)
        all_findings.extend(found)
        if rc != 0:
            failures += 1

    elapsed = time.monotonic() - t0

    if not all_findings:
        log_run(
            f"audit-{args.kind}",
            modules=len(targets),
            findings=0,
            failures=failures,
            elapsed=f"{elapsed:.1f}s",
        )
        return 0 if failures == 0 else 1

    # Merge and publish
    merged_dir = Path(f"/tmp/cai-audit-{args.kind}-merged-{uuid.uuid4().hex[:8]}")
    merged_dir.mkdir(parents=True, exist_ok=True)
    merged_file = merged_dir / "findings.json"
    merged_file.write_text(json.dumps({"findings": all_findings}))

    published = _run(
        ["python", str(PUBLISH_SCRIPT), "--namespace", "audit", "--findings-file", str(merged_file)]
    )
    shutil.rmtree(merged_dir, ignore_errors=True)

    if published.returncode != 0 and all_findings:
        print(
            f"[cai audit] runner produced {len(all_findings)} finding(s) but publish returned "
            f"{published.returncode} — categories may not be in AUDIT_CATEGORIES (see #903)",
            file=sys.stderr,
            flush=True,
        )

    log_run(
        f"audit-{args.kind}",
        modules=len(targets),
        findings=len(all_findings),
        failures=failures,
        published=published.returncode,
        elapsed=f"{elapsed:.1f}s",
    )
    return published.returncode if failures == 0 else max(published.returncode, 1)
