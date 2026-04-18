#!/usr/bin/env bash
#
# robotsix-cai — server-side transcript cleanup.
#
# Runs on the host that receives transcript uploads from one or more
# cai containers (see cai_lib/transcript_sync.py). This script is NOT
# run inside the container: `scp` or `rsync` it onto your OVH box and
# wire it into a cron on the server.
#
# It enforces two caps on the transcript root:
#
#   * Age  — deletes *.jsonl older than CAI_SERVER_MAX_AGE_DAYS
#     (default 30) and removes empty directories it leaves behind.
#   * Size — if the remaining tree is still larger than
#     CAI_SERVER_MAX_SIZE_MB (default 2000 MB), deletes the oldest
#     files one at a time until the total is under the cap.
#
# Environment variables (all optional):
#
#   CAI_SERVER_TRANSCRIPT_ROOT  Directory to clean up. Default:
#                               /srv/cai-transcripts.
#   CAI_SERVER_MAX_AGE_DAYS     Files older than this are deleted.
#                               Default: 30.
#   CAI_SERVER_MAX_SIZE_MB      Total size cap (in megabytes).
#                               Default: 2000.
#   CAI_SERVER_DRY_RUN          If set to any non-empty value, prints
#                               what would be deleted without actually
#                               deleting anything.
#
# Example cron entry on the server (daily at 03:30):
#
#   30 3 * * * /srv/cai-transcripts-cleanup.sh >> /var/log/cai-cleanup.log 2>&1

set -euo pipefail

ROOT="${CAI_SERVER_TRANSCRIPT_ROOT:-/srv/cai-transcripts}"
MAX_AGE_DAYS="${CAI_SERVER_MAX_AGE_DAYS:-30}"
MAX_SIZE_MB="${CAI_SERVER_MAX_SIZE_MB:-2000}"
DRY_RUN="${CAI_SERVER_DRY_RUN:-}"

if [ ! -d "$ROOT" ]; then
  echo "[cai-cleanup] $ROOT does not exist; nothing to do"
  exit 0
fi

log() { echo "[cai-cleanup] $*"; }

log "root=$ROOT max_age=${MAX_AGE_DAYS}d max_size=${MAX_SIZE_MB}MB dry_run=${DRY_RUN:-no}"

# --- Age cap -----------------------------------------------------------------
# `find -mtime +N` matches files whose mtime is > N*24h ago.
if [ -n "$DRY_RUN" ]; then
  aged=$(find "$ROOT" -type f -name '*.jsonl' -mtime "+$MAX_AGE_DAYS" -print | wc -l)
  log "would delete $aged file(s) older than ${MAX_AGE_DAYS}d"
else
  deleted=$(find "$ROOT" -type f -name '*.jsonl' -mtime "+$MAX_AGE_DAYS" -print -delete | wc -l)
  log "age pass: deleted $deleted file(s) older than ${MAX_AGE_DAYS}d"
  # Clean up empty directories left behind (ignoring errors on non-empty).
  find "$ROOT" -mindepth 1 -type d -empty -delete 2>/dev/null || true
fi

# --- Size cap ----------------------------------------------------------------
# Convert the cap to bytes for exact comparison.
cap_bytes=$((MAX_SIZE_MB * 1024 * 1024))

current_bytes() {
  # `du -sb` gives bytes; strip the trailing path column.
  du -sb "$ROOT" 2>/dev/null | awk '{print $1}'
}

total=$(current_bytes)
log "size pass start: total=${total}B cap=${cap_bytes}B"

if [ "$total" -le "$cap_bytes" ]; then
  log "under cap, done"
  exit 0
fi

# Stream oldest-first (sorted by mtime ascending) and delete until under cap.
# find -printf prints the mtime (seconds since epoch, %T@) + size (%s) + path (%p)
# tab-separated. sort -n on the first field gives oldest first.
removed=0
freed=0
while IFS=$'\t' read -r _mtime size path; do
  [ -z "$path" ] && continue
  if [ -n "$DRY_RUN" ]; then
    log "would delete: $path ($size B)"
  else
    rm -f -- "$path"
  fi
  freed=$((freed + size))
  removed=$((removed + 1))
  if [ $((total - freed)) -le "$cap_bytes" ]; then
    break
  fi
done < <(find "$ROOT" -type f -name '*.jsonl' -printf '%T@\t%s\t%p\n' | sort -n)

log "size pass: removed=$removed freed=${freed}B"

if [ -z "$DRY_RUN" ]; then
  find "$ROOT" -mindepth 1 -type d -empty -delete 2>/dev/null || true
fi

log "done: total=$(current_bytes)B"
