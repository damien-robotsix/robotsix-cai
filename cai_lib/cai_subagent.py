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

import shutil
import subprocess
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions

from cai_lib.subagent.core import SubAgent
from cai_lib.subagent.cost_tracker import CostRow, CostTracker
from cai_lib.cost_comment import _post_cost_comment
from cai_lib.fsm_state import _CURRENT_FSM_STATE
from cai_lib.utils.log import log_cost


class CaiCostTracker(CostTracker):
    """CostTracker subclass that logs to disk and mirrors GH comments.

    Overrides :meth:`_emit` to stamp the FSM state contextvar onto the
    row, call :func:`~cai_lib.utils.log.log_cost`, and post the
    cost-attribution comment on the issue/PR target (and optional extra
    target) via :func:`~cai_lib.cost_comment._post_cost_comment`.
    """

    def _emit(self, row: CostRow, agent: str) -> None:
        """Stamp FSM state, log cost row, and post GH cost-attribution comment."""
        fsm_state = _CURRENT_FSM_STATE.get()
        if fsm_state:
            row.fsm_state = fsm_state
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
) -> subprocess.CompletedProcess:
    """Cai-specific one-shot subagent invocation over :class:`CaiSubAgent`.

    Constructs a :class:`CaiSubAgent` (with a :class:`CaiCostTracker` built
    from the optional target metadata), calls ``.run(prompt)`` once, and
    returns the :class:`subprocess.CompletedProcess`.

    This is the cai-repository-specific counterpart to
    :func:`cai_lib.subagent.core.run_subagent`. Callers that need CLI-pin,
    plugin-inject, cost-log, and GH-comment behaviors should use this
    function; the base :func:`cai_lib.subagent.run_subagent` is a stripped
    version without those behaviors.
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
