"""Stderr-capture sink wired into ``ClaudeAgentOptions.stderr``.

Bounds for the stderr-capture sink wired into ClaudeAgentOptions.stderr.
The SDK only pipes the ``claude -p`` subprocess's stderr when a callback
is attached — otherwise stderr inherits the parent fd and the CLI's real
crash reason (e.g. transient network / OOM / signal) vanishes into the
wrapper's own log stream, leaving callers staring at the SDK's hardcoded
placeholder "Check stderr output for details".
"""

from __future__ import annotations


_CAPTURED_STDERR_MAX_LINES = 200
_CAPTURED_STDERR_MAX_CHARS = 4000


def _make_stderr_sink(buf: list[str]):
    def _sink(line: str) -> None:
        if len(buf) < _CAPTURED_STDERR_MAX_LINES:
            buf.append(line)
    return _sink


def _captured_stderr_text(buf: list[str]) -> str:
    if not buf:
        return ""
    joined = "\n".join(buf)
    if len(joined) > _CAPTURED_STDERR_MAX_CHARS:
        # Keep the tail — the crash reason tends to be on the last lines.
        joined = "[truncated]\n" + joined[-_CAPTURED_STDERR_MAX_CHARS:]
    return joined
