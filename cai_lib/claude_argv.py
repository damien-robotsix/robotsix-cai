"""``claude -p`` argv facade relocated from ``cai_lib.subagent.legacy``.

``_run_claude_p`` is now a thin delegating shim (#1274): it parses
``claude -p``-style argv into a :class:`ClaudeAgentOptions`, constructs
a :class:`~cai_lib.cai_subagent.CaiSubAgent` backed by a
:class:`~cai_lib.cai_subagent.CaiCostTracker` (carrying the optional
caller-supplied row extras), calls ``.run(prompt)``, and adapts the
returned :class:`~cai_lib.subagent.core.RunResult` to a
:class:`subprocess.CompletedProcess` with ``args=cmd`` so existing
callers that inspect the argv see their original command.

This facade also owns the stderr-capture sink — it allocates a
local buffer and wires it onto ``options.stderr`` before driving the
SDK so the back-compat ``CompletedProcess.stderr`` carries the real
``claude -p`` subprocess crash tail. SDK-native callers (which read
:class:`~cai_lib.subagent.core.RunResult` directly) do not need it,
so :class:`~cai_lib.subagent.core.SubAgent` itself does no stderr
capture.

Do not add new call sites; port existing ones to
:func:`cai_lib.cai_subagent.run_subagent` instead.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions

from cai_lib.cai_subagent import CaiCostTracker, CaiSubAgent
from cai_lib.subagent.core import RunResult, RunStatus
from cai_lib.subagent.stderr_sink import _captured_stderr_text, _make_stderr_sink


def _argv_to_options(
    argv: list[str],
    cwd: str | None,
) -> tuple[ClaudeAgentOptions, str]:
    """Parse `claude -p`-style argv (``cmd[2:]``) into a ClaudeAgentOptions
    plus a positional prompt. Recognised flags become typed fields; unknown
    flags forward via ``extra_args`` (so ``--agent cai-dup-check`` still
    works even though there is no typed ``agent`` field). A trailing
    non-flag token is returned as the prompt so pre-screen call sites that
    pass the prompt in argv (e.g. ``actions/implement.py:229``) keep working.
    """
    opts = ClaudeAgentOptions()
    opts.add_dirs = []
    opts.plugins = []
    opts.allowed_tools = []
    extra_args: dict[str, str | None] = {}
    positional: list[str] = []

    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--dangerously-skip-permissions":
            opts.permission_mode = "bypassPermissions"
            i += 1
        elif tok == "--model":
            opts.model = argv[i + 1]
            i += 2
        elif tok == "--max-turns":
            opts.max_turns = int(argv[i + 1])
            i += 2
        elif tok == "--max-budget-usd":
            opts.max_budget_usd = float(argv[i + 1])
            i += 2
        elif tok == "--permission-mode":
            opts.permission_mode = argv[i + 1]  # type: ignore[assignment]
            i += 2
        elif tok == "--allowedTools":
            opts.allowed_tools = [t for t in argv[i + 1].split(",") if t]
            i += 2
        elif tok == "--add-dir":
            opts.add_dirs.append(argv[i + 1])
            i += 2
        elif tok == "--plugin-dir":
            opts.plugins.append({"type": "local", "path": argv[i + 1]})
            i += 2
        elif tok == "--json-schema":
            opts.output_format = {
                "type": "json_schema",
                "schema": json.loads(argv[i + 1]),
            }
            i += 2
        elif tok.startswith("--"):
            flag_name = tok[2:]
            if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                extra_args[flag_name] = argv[i + 1]
                i += 2
            else:
                extra_args[flag_name] = None
                i += 1
        else:
            positional.append(tok)
            i += 1

    opts.extra_args = extra_args
    if cwd is not None:
        opts.cwd = cwd

    # Preserve the pre-SDK auto-inject of the cai-skills plugin when the
    # directory exists at the caller's cwd (subprocess_utils.py:59-62).
    skills_plugin = Path(".claude/plugins/cai-skills")
    if skills_plugin.is_dir():
        opts.plugins.append({"type": "local", "path": str(skills_plugin)})

    return opts, " ".join(positional)


def _to_completed_process(
    rr: RunResult, cmd: list[str], cli_stderr_buf: list[str],
) -> subprocess.CompletedProcess:
    """Adapt a :class:`RunResult` to a :class:`subprocess.CompletedProcess`.

    Preserves the pre-#1274 argv-facade contract:
      - ``args=cmd`` (not the SDK sentinel).
      - ``returncode`` is 1 on EXCEPTION / NO_RESULT / SDK_ERROR, 0 on OK.
      - ``stdout`` comes from ``rr.stdout`` unchanged (the priority
        ladder is already owned by :meth:`SubAgent._extract_stdout`).
      - ``stderr`` is:
          * ``""`` on OK,
          * ``rr.error_summary`` on SDK_ERROR (the pre-refactor
            ``_sdk_error_summary(result)`` string),
          * ``error_summary + "\\n--- cli stderr ---\\n" + cli_stderr``
            on EXCEPTION and NO_RESULT, matching the pre-refactor
            ``combined`` string byte-for-byte.

    ``cli_stderr_buf`` is the buffer wired into ``options.stderr`` by
    :func:`_run_claude_p` for this run — its captured CLI tail is
    appended to ``stderr`` on the EXCEPTION/NO_RESULT paths so the
    real subprocess crash reason still surfaces to back-compat callers.
    """
    if rr.status == RunStatus.OK:
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout=rr.stdout, stderr="",
        )
    if rr.status == RunStatus.SDK_ERROR:
        return subprocess.CompletedProcess(
            args=cmd, returncode=1, stdout=rr.stdout,
            stderr=rr.error_summary or "",
        )
    combined = rr.error_summary or ""
    cli_stderr = _captured_stderr_text(cli_stderr_buf)
    if cli_stderr:
        combined = f"{combined}\n--- cli stderr ---\n{cli_stderr}"
    return subprocess.CompletedProcess(
        args=cmd, returncode=1, stdout=rr.stdout, stderr=combined,
    )


def _run_claude_p(
    cmd: list[str],
    *,
    category: str,
    agent: str = "",
    input: str | None = None,
    cwd: str | None = None,
    target_kind: str | None = None,
    target_number: int | None = None,
    extra_target_kind: str | None = None,
    extra_target_number: int | None = None,
    module: str | None = None,
    scope_files: list[str] | None = None,
    fingerprint_payload: str | None = None,
    fix_attempt_count: int | None = None,
    **kwargs,
) -> subprocess.CompletedProcess:
    """Run a ``claude -p`` command via the Claude Agent SDK and record its cost.

    Deprecated argv facade — port new call sites to
    :func:`cai_lib.cai_subagent.run_subagent` instead.

    ``cmd[0]`` must be ``"claude"`` and ``cmd[1]`` must be ``"-p"``;
    ``cmd[2:]`` is parsed into a ``ClaudeAgentOptions`` by
    ``_argv_to_options`` (recognised flags become typed fields, unknown
    flags forward via ``extra_args``). ``input`` becomes the SDK
    ``prompt=`` argument; when absent, a trailing non-flag argv token is
    used instead (the implement pre-screen pattern at
    ``actions/implement.py:229``).

    Cost-row extras (``module``, ``scope_files``, ``fingerprint_payload``,
    ``fix_attempt_count``, ``target_kind``/``target_number`` for the row
    stamp) are carried on the
    :class:`~cai_lib.cai_subagent.CaiCostTracker` and stamped onto the
    row inside :meth:`CaiCostTracker._emit`, so the on-disk
    ``cai-cost.jsonl`` schema is identical whether a caller arrives
    through this facade or constructs a
    :class:`~cai_lib.cai_subagent.CaiSubAgent` directly.

    The returned ``CompletedProcess`` mirrors the pre-#1274 contract:
      - ``.args`` is the original ``cmd`` list (not the SDK sentinel).
      - ``.stdout`` follows the priority ladder owned by
        :meth:`SubAgent._extract_stdout`
        (``structured_output → error_max_structured_output_retries →
        result text → last-assistant salvage``).
      - ``.returncode`` is 1 on any exception, no-ResultMessage, or
        ``is_error`` response; 0 otherwise.
      - ``.stderr`` is ``""`` on success, ``_sdk_error_summary(result)``
        on an SDK-reported error, or ``str(exc) + cli_stderr`` on an
        exception / no-result path — byte-for-byte identical to the
        pre-#1274 ``combined`` string.
    """
    if len(cmd) < 2 or cmd[0] != "claude" or cmd[1] != "-p":
        raise ValueError("_run_claude_p requires cmd[:2] == ['claude', '-p']")

    # Honour the legacy ``timeout=`` kwarg (``actions/explore.py`` uses it
    # as a 30-minute cap); silently discard other ``subprocess.run``
    # kwargs we previously inherited via ``**kwargs``.
    timeout = kwargs.pop("timeout", None)

    options, positional_prompt = _argv_to_options(cmd[2:], cwd=cwd)
    prompt = input if input is not None else positional_prompt

    # Wire a stderr-capture sink onto options before driving the SDK.
    # SDK-native callers do not need this — only the back-compat
    # ``CompletedProcess.stderr`` adapter consumes the buffer so the real
    # ``claude -p`` subprocess crash reason still surfaces.
    cli_stderr_buf: list[str] = []
    options.stderr = _make_stderr_sink(cli_stderr_buf)

    tracker = CaiCostTracker(
        target_kind=target_kind,
        target_number=target_number,
        extra_target_kind=extra_target_kind,
        extra_target_number=extra_target_number,
        module=module,
        scope_files=scope_files,
        fingerprint_payload=fingerprint_payload,
        fix_attempt_count=fix_attempt_count,
    )
    rr = CaiSubAgent(
        options=options,
        category=category,
        agent=agent,
        timeout=timeout,
        cost_tracker=tracker,
    ).run(prompt)
    return _to_completed_process(rr, cmd, cli_stderr_buf)
