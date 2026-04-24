"""SDK-error diagnostic summariser for non-zero agent-SDK results."""

from __future__ import annotations


def _sdk_error_summary(result) -> str:
    """Render a single-line diagnostic for a non-zero SDK result.

    Called by :func:`_run_claude_p` when the terminal
    :class:`ResultMessage` reports ``is_error=True`` (or the
    downstream ``no-ResultMessage`` fallback). The returned string
    is stuffed into the ``stderr`` field of the returned
    :class:`subprocess.CompletedProcess` so downstream callers —
    notably ``cai_lib.actions.implement.handle_implement`` — have
    *something* to log on the ``result=subagent_failed`` branch.

    Before issue #1106 both the no-results fallback and the terminal
    ``is_error=True`` return path set ``stderr=""``, which is why
    issue #910's five consecutive ``subagent_failed`` runs were
    byte-identical at the audit-log layer: the SDK subtype
    (``error_max_turns`` vs. ``error_max_structured_output_retries``
    vs. an API error) never reached the log row.

    The output is whitespace-tolerant (``_format_stderr_tail`` on
    the caller side collapses whitespace) and opaque — it is not
    classified into a fixed tag set, so new SDK subtypes land in
    the log verbatim without requiring a prompt update.
    """
    subtype = getattr(result, "subtype", None) or "none"
    is_error = bool(getattr(result, "is_error", False))
    text = getattr(result, "result", None)
    preview = ""
    if isinstance(text, str):
        preview = text.replace("\n", " ").strip()[:160]
    if preview:
        return (
            f"sdk_subtype={subtype} is_error={is_error} "
            f"result={preview!r}"
        )
    return f"sdk_subtype={subtype} is_error={is_error}"
