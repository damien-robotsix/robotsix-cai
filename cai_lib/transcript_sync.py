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

# Access config attributes lazily (``config.TRANSCRIPT_SYNC_URL`` etc.)
# rather than importing them by value. Tests can then patch individual
# attributes on the config module without having to reload this module,
# and runtime behaviour picks up changes to the environment exposed via
# config without any reload dance at all.
from cai_lib import config


_SSH_OPTIONS = [
    # Accept new host keys automatically (first connection) but still fail
    # on mismatch. The known_hosts file lives under /home/cai/.ssh which
    # is persisted in the cai_home volume, so after the first run the host
    # key is pinned and subsequent runs use strict checking.
    "-o", "StrictHostKeyChecking=accept-new",
    # Short timeout so a broken network doesn't wedge a cron run.
    "-o", "ConnectTimeout=15",
]


def _is_local_url(url: str) -> bool:
    """True when the sync URL is a plain filesystem path, not SSH.

    Local mode is used when the cai container runs on the same host as
    the transcript store (e.g. a VPS that both hosts cai and acts as the
    central store for other laptops that push over SSH). The path is
    expected to be bind-mounted into the container; rsync then runs
    directly against the filesystem with no SSH transport at all.

    Heuristic: an SSH URL always contains ``:`` (``user@host:/path``),
    a local path never does.
    """
    return ":" not in url


def _ssh_command() -> str:
    """Build the ``-e`` argument for rsync — ``ssh -i <key> <opts>``."""
    parts = ["ssh", "-i", str(config.TRANSCRIPT_SYNC_SSH_KEY), *_SSH_OPTIONS]
    return " ".join(parts)


def _server_bucket() -> str:
    """Return ``<url>/<repo-slug>/<machine-id>`` — this host's push target."""
    return (
        f"{config.TRANSCRIPT_SYNC_URL.rstrip('/')}/"
        f"{config.REPO_SLUG}/{config.MACHINE_ID}"
    )


def _server_slug() -> str:
    """Return ``<url>/<repo-slug>`` — the pull source (every machine's bucket)."""
    return f"{config.TRANSCRIPT_SYNC_URL.rstrip('/')}/{config.REPO_SLUG}"


def _transport_args() -> list[str]:
    """rsync transport flags. Empty for local mode; ``-e ssh …`` for SSH."""
    if _is_local_url(config.TRANSCRIPT_SYNC_URL):
        return []
    return ["-e", _ssh_command()]


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


def _local_has_transcripts() -> bool:
    """True when there is at least one .jsonl under ``TRANSCRIPT_DIR``.

    Guards ``push()`` against wiping the server bucket on a fresh install:
    the container's TRANSCRIPT_DIR exists (pre-created in the image) but
    is empty until the first claude session runs. An `rsync --delete`
    of an empty source would drop every file from this machine's bucket
    on the server.
    """
    if not config.TRANSCRIPT_DIR.exists():
        return False
    return any(config.TRANSCRIPT_DIR.rglob("*.jsonl"))


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


def _ensure_local_bucket() -> None:
    """In local mode, create this host's bucket directory if missing.

    SSH mode uses rsync's ``--mkpath`` to auto-create remote intermediate
    directories; local mode hits the filesystem directly, so we make sure
    the target exists.
    """
    if not _is_local_url(config.TRANSCRIPT_SYNC_URL):
        return
    Path(_server_bucket()).mkdir(parents=True, exist_ok=True)


def push() -> int:
    """Push the local transcript tree into this host's server bucket.

    No-op (returns 0) when the feature is disabled or the local dir has
    no .jsonl files yet — pushing an empty tree with ``--delete`` would
    wipe this machine's server bucket on a fresh install. Uses
    ``--delete`` once there's content so the bucket mirrors the local
    window; server-side cleanup enforces age/size across machines.
    """
    if not config.transcript_sync_enabled():
        return 0
    if not _local_has_transcripts():
        return 0
    if not _ensure_rsync():
        return 0
    _ensure_local_bucket()
    return _run_rsync(
        [
            "-az",
            "--delete",
            # --mkpath creates missing intermediate dirs on the receiver
            # (e.g. the per-repo and per-host bucket). Without it the
            # first push to a fresh server fails with ENOENT.
            "--mkpath",
            *_transport_args(),
            # Trailing slashes: rsync copies the *contents* of TRANSCRIPT_DIR
            # into the server bucket, not the directory itself.
            f"{config.TRANSCRIPT_DIR}/",
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
    if not config.transcript_sync_enabled():
        return 0
    if not _ensure_rsync():
        return 0
    config.TRANSCRIPT_AGGREGATE_DIR.mkdir(parents=True, exist_ok=True)
    if _is_local_url(config.TRANSCRIPT_SYNC_URL):
        # In local mode the source may not exist yet (no machine has
        # pushed). rsync would emit "No such file or directory" and
        # return non-zero — harmless but noisy. Skip gracefully.
        if not Path(_server_slug()).exists():
            return 0
    return _run_rsync(
        [
            "-az",
            *_transport_args(),
            f"{_server_slug()}/",
            f"{config.TRANSCRIPT_AGGREGATE_DIR}/",
        ],
        label="pull",
    )


def sync() -> int:
    """Push then pull transcripts and cost logs. Returns 0 iff all succeed."""
    if not config.transcript_sync_enabled():
        print(
            "[transcript-sync] disabled (CAI_TRANSCRIPT_SYNC_URL or "
            "CAI_MACHINE_ID unset) — nothing to do",
            flush=True,
        )
        return 0
    rc = push()
    if rc != 0:
        return rc
    rc = pull()
    if rc != 0:
        return rc
    rc = push_cost()
    if rc != 0:
        return rc
    return pull_cost()


# ---------------------------------------------------------------------------
# Cost-log sync (mirrors the transcript-sync design for cai-cost.jsonl)
# ---------------------------------------------------------------------------
# Server layout:
#   <TRANSCRIPT_SYNC_URL>/
#     <repo-slug>-cost/        # separate namespace from transcripts
#       <machine-id>/
#         cai-cost.jsonl
#
# Local aggregate:
#   COST_LOG_AGGREGATE_DIR/
#     <machine-id>/
#       cai-cost.jsonl
# ---------------------------------------------------------------------------


def _cost_server_bucket() -> str:
    """Return ``<url>/<repo-slug>-cost/<machine-id>`` — this host's cost push target."""
    return (
        f"{config.TRANSCRIPT_SYNC_URL.rstrip('/')}/"
        f"{config.REPO_SLUG}-cost/{config.MACHINE_ID}"
    )


def _cost_server_slug() -> str:
    """Return ``<url>/<repo-slug>-cost`` — the cost pull source (all machines)."""
    return f"{config.TRANSCRIPT_SYNC_URL.rstrip('/')}/{config.REPO_SLUG}-cost"


def _local_has_cost_log() -> bool:
    """True when the local cost log file exists and is non-empty.

    Guards ``push_cost`` against uploading an empty file on a fresh install.
    """
    return (
        config.COST_LOG_PATH.exists()
        and config.COST_LOG_PATH.stat().st_size > 0
    )


def push_cost() -> int:
    """Push the local cost log to this host's server bucket.

    No-op (returns 0) when the feature is disabled or the local cost log is
    absent/empty. Uses rsync to copy the single file to the machine-id bucket.
    """
    if not config.transcript_sync_enabled():
        return 0
    if not _local_has_cost_log():
        return 0
    if not _ensure_rsync():
        return 0
    if _is_local_url(config.TRANSCRIPT_SYNC_URL):
        Path(_cost_server_bucket()).mkdir(parents=True, exist_ok=True)
    return _run_rsync(
        [
            "-az",
            "--mkpath",
            *_transport_args(),
            str(config.COST_LOG_PATH),
            f"{_cost_server_bucket()}/cai-cost.jsonl",
        ],
        label="cost-push",
    )


def pull_cost() -> int:
    """Pull all machines' cost logs into the local cost aggregate mirror.

    No-op (returns 0) when disabled. Creates the aggregate directory if
    missing. Does NOT use ``--delete`` for the same reason as ``pull()``.
    """
    if not config.transcript_sync_enabled():
        return 0
    if not _ensure_rsync():
        return 0
    config.COST_LOG_AGGREGATE_DIR.mkdir(parents=True, exist_ok=True)
    if _is_local_url(config.TRANSCRIPT_SYNC_URL):
        if not Path(_cost_server_slug()).exists():
            return 0
    return _run_rsync(
        [
            "-az",
            *_transport_args(),
            f"{_cost_server_slug()}/",
            f"{config.COST_LOG_AGGREGATE_DIR}/",
        ],
        label="cost-pull",
    )


def parse_source() -> Path:
    """Return the directory parse.py should walk.

    When sync is enabled AND the aggregate exists with content, use it.
    Otherwise fall back to the local-only directory so deployments without
    sync configured keep behaving as before.
    """
    if config.transcript_sync_enabled() and any(
        config.TRANSCRIPT_AGGREGATE_DIR.rglob("*.jsonl")
    ):
        return config.TRANSCRIPT_AGGREGATE_DIR
    return config.TRANSCRIPT_DIR


def cmd_transcript_sync(args) -> int:  # noqa: ARG001 - args required by dispatcher
    """CLI entrypoint: `cai transcript-sync`. Syncs transcripts and cost logs."""
    rc = sync()
    if rc != 0:
        print(f"[transcript-sync] exited with rc={rc}", file=sys.stderr)
    return rc
