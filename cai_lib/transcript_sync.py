"""Cross-host transcript synchronisation.

When ``CAI_TRANSCRIPT_SYNC_URL`` is set (see :mod:`cai_lib.config`), this
module pushes the local container's Claude Code session transcripts to a
central server over SSH/rsync and pulls the union of every machine's
transcripts back into a local aggregate directory. The analyzer and
confirm handlers then parse that aggregate instead of the local-only
``TRANSCRIPT_DIR`` so the self-improvement signal sees what *all*
machines have done, not just this one.

Feature is opt-in: if ``CAI_TRANSCRIPT_SYNC_URL`` is unset the module's
public functions are no-ops and the caller falls back to the existing
single-host behaviour.

Server layout::

    <TRANSCRIPT_SYNC_URL>/
      <repo-slug>/               # e.g. damien-robotsix_robotsix-cai
        <machine-id>/            # stable per-host identifier
          <encoded-cwd>/
            <session-id>.jsonl
          ...

Age and size enforcement happen server-side via ``scripts/server-cleanup.sh``
(run from a cron on the remote host). This module intentionally only deals
with transport.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from cai_lib.config import (
    MACHINE_ID,
    REPO_SLUG,
    TRANSCRIPT_AGGREGATE_DIR,
    TRANSCRIPT_DIR,
    TRANSCRIPT_SYNC_SSH_KEY,
    TRANSCRIPT_SYNC_URL,
    transcript_sync_enabled,
)


_SSH_OPTIONS = [
    # Accept new host keys automatically (first connection) but still fail
    # on mismatch. The known_hosts file lives under /home/cai/.ssh which
    # is persisted in the cai_home volume, so after the first run the host
    # key is pinned and subsequent runs use strict checking.
    "-o", "StrictHostKeyChecking=accept-new",
    # Short timeout so a broken network doesn't wedge a cron run.
    "-o", "ConnectTimeout=15",
]


def _ssh_command() -> str:
    """Build the ``-e`` argument for rsync — ``ssh -i <key> <opts>``."""
    parts = ["ssh", "-i", str(TRANSCRIPT_SYNC_SSH_KEY), *_SSH_OPTIONS]
    return " ".join(parts)


def _server_bucket() -> str:
    """Return ``<url>/<repo-slug>/<machine-id>`` — this host's push target."""
    return f"{TRANSCRIPT_SYNC_URL.rstrip('/')}/{REPO_SLUG}/{MACHINE_ID}"


def _server_slug() -> str:
    """Return ``<url>/<repo-slug>`` — the pull source (every machine's bucket)."""
    return f"{TRANSCRIPT_SYNC_URL.rstrip('/')}/{REPO_SLUG}"


def _ensure_rsync() -> bool:
    """True iff rsync is on PATH. Logs a single clear message when not."""
    if shutil.which("rsync") is None:
        print(
            "[transcript-sync] rsync not installed — skipping (install rsync "
            "in the image to enable cross-host transcript sync)",
            flush=True,
        )
        return False
    return True


def _run_rsync(args: list[str], *, label: str) -> int:
    """Run rsync with a consistent log prefix. Returns the exit code."""
    cmd = ["rsync", *args]
    try:
        result = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        print(f"[transcript-sync] {label}: rsync not found", flush=True)
        return 127
    if result.returncode != 0:
        # Truncate to keep the log usable when a server is misconfigured.
        err = (result.stderr or "").strip().splitlines()[-5:]
        print(
            f"[transcript-sync] {label} failed (exit {result.returncode}): "
            f"{' | '.join(err) or '(no stderr)'}",
            flush=True,
        )
    return result.returncode


def push() -> int:
    """Push the local transcript tree into this host's server bucket.

    No-op (returns 0) when the feature is disabled or the local dir is
    missing. Uses ``--delete`` so the server bucket mirrors the local
    window — per-machine history beyond the local window is NOT preserved
    (server-side cleanup enforces age/size instead).
    """
    if not transcript_sync_enabled():
        return 0
    if not TRANSCRIPT_DIR.exists():
        return 0
    if not _ensure_rsync():
        return 0
    return _run_rsync(
        [
            "-az",
            "--delete",
            "-e", _ssh_command(),
            # Trailing slashes: rsync copies the *contents* of TRANSCRIPT_DIR
            # into the server bucket, not the directory itself.
            f"{TRANSCRIPT_DIR}/",
            f"{_server_bucket()}/",
        ],
        label="push",
    )


def pull() -> int:
    """Pull every machine's bucket into the local aggregate mirror.

    No-op (returns 0) when disabled. Creates the aggregate directory if
    missing. Does NOT use ``--delete`` so a transient server outage can't
    empty the local mirror mid-run.
    """
    if not transcript_sync_enabled():
        return 0
    if not _ensure_rsync():
        return 0
    TRANSCRIPT_AGGREGATE_DIR.mkdir(parents=True, exist_ok=True)
    return _run_rsync(
        [
            "-az",
            "-e", _ssh_command(),
            f"{_server_slug()}/",
            f"{TRANSCRIPT_AGGREGATE_DIR}/",
        ],
        label="pull",
    )


def sync() -> int:
    """Push then pull. Returns 0 iff both succeed; otherwise the first failure."""
    if not transcript_sync_enabled():
        print(
            "[transcript-sync] disabled (CAI_TRANSCRIPT_SYNC_URL or "
            "CAI_MACHINE_ID unset) — nothing to do",
            flush=True,
        )
        return 0
    rc = push()
    if rc != 0:
        return rc
    return pull()


def parse_source() -> Path:
    """Return the directory parse.py should walk.

    When sync is enabled AND the aggregate exists with content, use it.
    Otherwise fall back to the local-only directory so deployments without
    sync configured keep behaving as before.
    """
    if transcript_sync_enabled() and any(TRANSCRIPT_AGGREGATE_DIR.rglob("*.jsonl")):
        return TRANSCRIPT_AGGREGATE_DIR
    return TRANSCRIPT_DIR


def cmd_transcript_sync(args) -> int:  # noqa: ARG001 - args required by dispatcher
    """CLI entrypoint: `cai transcript-sync`. Runs push + pull."""
    rc = sync()
    if rc != 0:
        print(f"[transcript-sync] exited with rc={rc}", file=sys.stderr)
    return rc
