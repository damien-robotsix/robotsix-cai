"""Cai-specific subagent layer — repo-coupled subclasses of SubAgent/CostTracker.

This module owns all robotsix-cai repository specifics that have been
stripped from :mod:`cai_lib.subagent.core` and
:mod:`cai_lib.subagent.cost_tracker`:

* :class:`CaiCostTracker` — overrides :meth:`~CostTracker._emit` to stamp
  the FSM-state contextvar, call :func:`cai_lib.utils.log.log_cost`, and
  mirror the cost row as a GH comment via
  :func:`cai_lib.cost_comment._post_cost_comment`.
* :class:`CaiSubAgent` — overrides :meth:`~SubAgent._prepare_options` to
  pin the npm-installed ``claude`` CLI path and auto-inject the
  ``cai-skills`` plugin when present.
* :func:`run_subagent` — one-shot shim that constructs a
  :class:`CaiSubAgent` backed by a :class:`CaiCostTracker`.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions

from cai_lib.subagent.core import RunResult, SubAgent
from cai_lib.subagent.cost_tracker import CostRow, CostTracker
from cai_lib.cost_comment import _post_cost_comment
from cai_lib.fsm_state import _CURRENT_FSM_STATE
from cai_lib.utils.log import log_cost


class CaiCostTracker(CostTracker):
    """CostTracker subclass that logs to disk and mirrors GH comments.

    Overrides :meth:`_emit` to stamp the FSM-state contextvar and any
    caller-supplied row extras onto the :class:`CostRow` (so both the
    in-memory ``cost_rows`` reference and the serialised dict carry
    them), call :func:`~cai_lib.utils.log.log_cost`, and post the
    cost-attribution comment on the issue/PR target (and optional
    extra target) via
    :func:`~cai_lib.cost_comment._post_cost_comment`.

    The four extras (``module``, ``scope_files``, ``fingerprint_payload``,
    ``fix_attempt_count``) are the caller-per-call kwargs that
    ``_run_claude_p`` previously threaded through its inline row
    builder. Moving them onto the tracker lets the facade collapse to
    a thin shim (#1274) while keeping every production row stamp
    byte-identical. ``fingerprint_payload`` overrides the
    ``prompt_fingerprint`` that
    :meth:`~cai_lib.subagent.cost_tracker.CostRow.from_result_message`
    derived from ``system_prompt + prompt`` — callers use this to
    supply a stable cache-health key when the prompt is not by itself
    a stable identifier (issue #1207).
    """

    module: str | None = None
    scope_files: list[str] | None = None
    fingerprint_payload: str | None = None
    fix_attempt_count: int | None = None

    def _emit(self, row: CostRow, agent: str) -> None:
        """Stamp FSM state + caller extras, log cost row, and post GH comments."""
        if self.fingerprint_payload is not None:
            row.prompt_fingerprint = hashlib.sha256(
                self.fingerprint_payload.encode()
            ).hexdigest()[:16]
        fsm_state = _CURRENT_FSM_STATE.get()
        if fsm_state:
            row.fsm_state = fsm_state
        if self.module is not None:
            row.module = self.module
        if self.scope_files:
            row.scope_files = list(self.scope_files)[:10]
        if self.fix_attempt_count is not None:
            row.fix_attempt_count = self.fix_attempt_count
        if self.target_kind is not None:
            row.target_kind = self.target_kind
        if self.target_number is not None:
            row.target_number = self.target_number
        dumped = row.model_dump(exclude_none=True)
        log_cost(dumped)
        if self.target_kind is not None and self.target_number is not None:
            _post_cost_comment(
                self.target_kind, self.target_number, dumped, agent,
            )
        if (
            self.extra_target_kind is not None
            and self.extra_target_number is not None
        ):
            _post_cost_comment(
                self.extra_target_kind, self.extra_target_number,
                dumped, agent,
            )


class CaiSubAgent(SubAgent):
    """SubAgent subclass that pins the CLI path and injects the cai-skills plugin.

    Overrides :meth:`_prepare_options` to call the parent's stderr-sink
    setup, then additionally:

    * Pin ``options.cli_path`` to the npm-installed ``claude`` binary so
      the SDK reuses the binary audited in the Dockerfile.
    * Inject the ``cai-skills`` local plugin when
      ``.claude/plugins/cai-skills`` exists at the caller's cwd.
    """

    def _prepare_options(self) -> None:
        """Pin cli_path, auto-inject cai-skills plugin, attach stderr sink."""
        # Base: reset captured stderr and attach fresh stderr sink.
        super()._prepare_options()

        _cli_path = shutil.which("claude")
        if _cli_path and not getattr(self.options, "cli_path", None):
            self.options.cli_path = _cli_path

        skills_plugin = Path(".claude/plugins/cai-skills")
        if skills_plugin.is_dir():
            if self.options.plugins is None:
                self.options.plugins = []
            already = any(
                isinstance(p, dict) and p.get("path") == str(skills_plugin)
                for p in self.options.plugins
            )
            if not already:
                self.options.plugins.append(
                    {"type": "local", "path": str(skills_plugin)}
                )


def run_subagent(
    prompt: str,
    options: ClaudeAgentOptions,
    *,
    category: str,
    agent: str,
    target_kind: str | None = None,
    target_number: int | None = None,
    extra_target_kind: str | None = None,
    extra_target_number: int | None = None,
    timeout: float | None = None,
) -> RunResult:
    """Cai-specific one-shot subagent invocation over :class:`CaiSubAgent`.

    Constructs a :class:`CaiSubAgent` (with a :class:`CaiCostTracker` built
    from the optional target metadata), calls ``.run(prompt)`` once, and
    returns the :class:`~cai_lib.subagent.core.RunResult`.
    """
    tracker = CaiCostTracker(
        target_kind=target_kind,
        target_number=target_number,
        extra_target_kind=extra_target_kind,
        extra_target_number=extra_target_number,
    )
    return CaiSubAgent(
        options=options,
        category=category,
        agent=agent,
        timeout=timeout,
        cost_tracker=tracker,
    ).run(prompt)
