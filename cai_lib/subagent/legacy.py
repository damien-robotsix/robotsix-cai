"""Deprecated ``claude -p`` argv facade — kept for the 12 handlers still on it.

``_run_claude_p`` wraps the SDK ``query`` with the same cost-row +
cost-mirror + FSM-state + stderr-sink behaviour as ``run_subagent``
but accepts ``claude -p``-style argv. Do not add new call sites;
port existing ones to :func:`cai_lib.subagent.core.run_subagent`
instead. The module is a thin shim and becomes deletable once every
handler has been ported off.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions

from cai_lib.logging_utils import log_cost

from .core import _CLI_PATH, _collect_results
from .cost import _post_cost_comment
from .errors import _sdk_error_summary
from .fsm_state import _CURRENT_FSM_STATE
from .stderr_sink import _captured_stderr_text, _make_stderr_sink


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
    :func:`cai_lib.subagent.core.run_subagent` instead.

    ``cmd[0]`` must be ``"claude"`` and ``cmd[1]`` must be ``"-p"``;
    ``cmd[2:]`` is parsed into a ``ClaudeAgentOptions`` by
    ``_argv_to_options`` (recognised flags become typed fields, unknown
    flags forward via ``extra_args``). ``input`` becomes the SDK
    ``prompt=`` argument; when absent, a trailing non-flag argv token is
    used instead (the implement pre-screen pattern at
    ``actions/implement.py:229``).

    The returned ``CompletedProcess`` mirrors the pre-SDK contract:
      - ``.stdout`` carries ``structured_output`` (JSON-encoded) when
        ``--json-schema`` succeeded; ``""`` on ``subtype ==
        "error_max_structured_output_retries"`` with a diagnostic stderr
        line; ``result`` text otherwise; falling back to the last
        assistant text when ``result`` is absent.
      - ``.returncode`` is 1 on any exception or when ``is_error`` is
        True; 0 otherwise.

    Cost rows carry the following keys: ``ts``, ``category``, ``agent``,
    ``cost_usd``, ``duration_ms``, ``duration_api_ms``, ``num_turns``,
    ``session_id``, ``host``, ``exit``, ``is_error``, the four flat token
    keys (``input_tokens``, ``output_tokens``, ``cache_creation_input_tokens``,
    ``cache_read_input_tokens``), and optional fields: ``models`` (per-model
    rollup, issue #1205), ``parent_model`` (top-level agent model name),
    ``subagents`` (subagent invocation counts, issue #1205), ``fsm_state``
    (dispatcher funnel position, issue #1203), ``cache_hit_rate`` (pre-computed
    aggregate hit rate, issue #1205), ``prompt_fingerprint`` (16-char SHA256
    hash for cache-rate regression detection, issue #1207), ``module``
    (caller-supplied module name for grouping spend by module, issue #1206),
    ``scope_files`` (caller-supplied file list for grouping spend by declared
    scope, issue #1206), ``target_kind`` (``"issue"`` or ``"pr"``, issue #1210),
    ``target_number`` (numeric issue/PR ID, issue #1210), and ``fix_attempt_count``
    (count of prior closed-unmerged PRs for the linked issue — matches the
    ``_log_outcome`` semantic in ``cai-outcomes.jsonl`` so the two logs can be
    joined; stamped only by fix-retry flows: ``implement`` / ``revise`` / ``fix-ci``,
    issue #1204). Rows from non-handler call sites (rescue, unblock, dup-check,
    audit, init) typically omit ``fsm_state`` and other optional fields,
    preserving back-compat for legacy rows. Optional ``module`` and ``scope_files``
    kwargs (when set by the caller) stamp ``row["module"]`` / ``row["scope_files"]``
    onto the cost-log row so downstream cost tooling can group spend by module
    (audit runs) or declared file scope (implement runs). Both keys are omitted
    when the kwargs are unset — and ``scope_files`` is capped at the first 10
    paths when set — preserving pre-change row shape for every non-participating
    call site. ``parent_cost_usd`` is intentionally dropped — the CLI format
    emits exactly one result event so there is nothing to attribute.
    """
    if len(cmd) < 2 or cmd[0] != "claude" or cmd[1] != "-p":
        raise ValueError("_run_claude_p requires cmd[:2] == ['claude', '-p']")

    # Honour the legacy ``timeout=`` kwarg (``actions/explore.py`` uses it
    # as a 30-minute cap); silently discard other ``subprocess.run``
    # kwargs we previously inherited via ``**kwargs``.
    timeout = kwargs.pop("timeout", None)

    options, positional_prompt = _argv_to_options(cmd[2:], cwd=cwd)
    if _CLI_PATH:
        options.cli_path = _CLI_PATH

    captured_stderr: list[str] = []
    options.stderr = _make_stderr_sink(captured_stderr)

    prompt = input if input is not None else positional_prompt

    try:
        if timeout is not None:
            results, last_assistant, parent_model, subagent_counts = \
                asyncio.run(
                    asyncio.wait_for(
                        _collect_results(prompt, options), timeout=timeout,
                    )
                )
        else:
            results, last_assistant, parent_model, subagent_counts = \
                asyncio.run(_collect_results(prompt, options))
    except Exception as exc:  # noqa: BLE001
        preview = str(exc)[:200].replace("\n", " ")
        cli_stderr = _captured_stderr_text(captured_stderr)
        cli_stderr_preview = cli_stderr.replace("\n", " | ")[:400]
        msg = (
            f"[cai cost] claude-agent-sdk query failed "
            f"({category}/{agent}): {preview}"
        )
        if cli_stderr_preview:
            msg += f" | cli_stderr={cli_stderr_preview!r}"
        print(msg, file=sys.stderr, flush=True)
        combined = str(exc)
        if cli_stderr:
            combined = f"{combined}\n--- cli stderr ---\n{cli_stderr}"
        return subprocess.CompletedProcess(
            args=cmd, returncode=1, stdout="", stderr=combined,
        )

    if not results:
        preview = (last_assistant or "")[:120].replace("\n", " ")
        cli_stderr = _captured_stderr_text(captured_stderr)
        cli_stderr_preview = cli_stderr.replace("\n", " | ")[:400]
        msg = (
            f"[cai cost] no ResultMessage from claude-agent-sdk "
            f"({category}/{agent}); last assistant starts with: {preview!r}"
        )
        if cli_stderr_preview:
            msg += f" | cli_stderr={cli_stderr_preview!r}"
        print(msg, file=sys.stderr, flush=True)
        combined = f"no_ResultMessage last_assistant={preview!r}"
        if cli_stderr:
            combined = f"{combined}\n--- cli stderr ---\n{cli_stderr}"
        return subprocess.CompletedProcess(
            args=cmd, returncode=1, stdout=last_assistant or "",
            stderr=combined,
        )

    result = results[-1]
    usage = result.usage or {}
    flat_keys = (
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    )
    flat = {
        k: usage[k] for k in flat_keys
        if isinstance(usage.get(k), (int, float))
    }
    models = result.model_usage if isinstance(result.model_usage, dict) else {}
    returncode = 1 if result.is_error else 0

    row = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "category": category,
        "agent": agent,
        "cost_usd": result.total_cost_usd,
        "duration_ms": result.duration_ms,
        "duration_api_ms": result.duration_api_ms,
        "num_turns": result.num_turns,
        "session_id": result.session_id,
        "host": socket.gethostname(),
        "exit": returncode,
        "is_error": bool(result.is_error),
    }
    row.update(flat)
    # Issue #1205: pre-compute aggregate cache hit rate once at write
    # time so every downstream reader (cost-report, cost-optimize, the
    # audit/cost.py summary) cites a single authoritative number instead
    # of re-deriving it (and getting the formula subtly wrong — e.g.
    # forgetting ``cache_creation_input_tokens`` or including
    # ``output_tokens`` in the denominator). Rows whose denominator is
    # zero (no cache tokens and no input tokens observed) omit the
    # field entirely so legacy/empty-usage rows stay byte-identical to
    # the pre-#1205 shape.
    cr = flat.get("cache_read_input_tokens") or 0
    cc = flat.get("cache_creation_input_tokens") or 0
    it = flat.get("input_tokens") or 0
    denom = cr + cc + it
    if denom > 0:
        row["cache_hit_rate"] = round(cr / denom, 4)
    if models:
        # Per-model hit rate using the camelCase keys the SDK emits
        # inside ``model_usage``. Mutates ``models`` in place; skips
        # entries whose denominator is zero (same omission rule as the
        # aggregate field). A non-dict ``mu`` is defensively ignored.
        for _m, mu in models.items():
            if not isinstance(mu, dict):
                continue
            m_cr = mu.get("cacheReadInputTokens") or 0
            m_cc = mu.get("cacheCreationInputTokens") or 0
            m_it = mu.get("inputTokens") or 0
            m_denom = m_cr + m_cc + m_it
            if m_denom > 0:
                mu["cacheHitRate"] = round(m_cr / m_denom, 4)
        row["models"] = models
    if parent_model:
        row["parent_model"] = parent_model
    if subagent_counts:
        row["subagents"] = dict(subagent_counts)
    # Issue #1203: stamp the FSM funnel position when the dispatcher has
    # set it. Non-FSM call sites (cmd_rescue, cmd_unblock, dup_check,
    # audit/runner.py, cmd_misc.init) leave the contextvar unset; the
    # key is omitted in that case, preserving pre-#1203 row shape.
    fsm_state = _CURRENT_FSM_STATE.get()
    if fsm_state:
        row["fsm_state"] = fsm_state
    # Issue #1207: stamp a short SHA256 fingerprint. When ``fingerprint_payload``
    # is provided, use it as-is (stable caller-controlled key); otherwise
    # fall back to the system + user prompt concatenation.
    fp_src = (
        fingerprint_payload if fingerprint_payload is not None
        else (options.system_prompt or "") + "\n---\n" + (prompt or "")
    )
    row["prompt_fingerprint"] = hashlib.sha256(fp_src.encode()).hexdigest()[:16]
    # Issue #1206: stamp the caller-supplied module / scope_files so
    # cai-audit-cost-reduction and cai-cost-optimize can group spend by
    # module (audit runs) or declared file scope (implement runs).
    # Each key is omitted when the caller did not supply it, keeping
    # the row byte-identical to the pre-#1206 shape for every
    # non-participating call site. ``scope_files`` is capped at the
    # first 10 paths to bound row size.
    if module is not None:
        row["module"] = module
    if scope_files:
        row["scope_files"] = list(scope_files)[:10]
    # Issue #1210: stamp target kind and number for cost attribution.
    if target_kind is not None:
        row["target_kind"] = target_kind
    if target_number is not None:
        row["target_number"] = target_number
    # Issue #1204: stamp the linked issue's prior-fix-attempt count so
    # cost-log readers can join cai-cost.jsonl to cai-outcomes.jsonl
    # (which already carries the same key via _log_outcome). Only
    # fix-retry flows (implement / revise / fix-ci) pass the kwarg;
    # every other call site leaves it None and the key is omitted,
    # preserving pre-#1204 row shape. Zero must be stamped (first
    # attempt) — use ``is not None``, not truthiness.
    if fix_attempt_count is not None:
        row["fix_attempt_count"] = fix_attempt_count
    log_cost(row)

    # Post a per-target cost-attribution comment on the issue/PR the
    # agent worked on, when the caller identified a target. Marker
    # body is stripped out of agent-input comment streams by
    # ``_strip_cost_comments`` (keyed on ``CAI_COST_COMMENT_RE``) so
    # it never pollutes downstream prompts, while remaining visible
    # to humans and audit tools that read comments via ``gh``.
    # Best-effort — ``_post_cost_comment`` swallows all exceptions.
    #
    # ``extra_target_kind`` / ``extra_target_number`` let a caller mirror
    # the cost comment onto a second object — used by ``cai revise`` and
    # ``cai merge`` to surface spend on both the PR and the linked issue
    # (the issue is the unit humans track; the PR is the work product).
    if target_kind is not None and target_number is not None:
        _post_cost_comment(target_kind, target_number, row, agent)
    if extra_target_kind is not None and extra_target_number is not None:
        _post_cost_comment(extra_target_kind, extra_target_number, row, agent)

    # Priority: structured_output → error_max_structured_output_retries →
    # result text → last-assistant salvage.
    if result.structured_output is not None:
        stdout = json.dumps(result.structured_output)
    elif result.subtype == "error_max_structured_output_retries":
        print(
            f"[cai cost] structured output retries exhausted "
            f"({category}/{agent}); schema was not satisfied",
            file=sys.stderr, flush=True,
        )
        stdout = ""
    elif isinstance(result.result, str):
        stdout = result.result
    else:
        stdout = last_assistant

    stderr = ""
    if returncode != 0:
        # Issue #1106: populate stderr with a diagnostic summary so the
        # downstream implement handler can record *why* the subagent
        # exited (sdk_subtype, is_error, and the first 160 chars of
        # result text). Without this the log row is byte-identical
        # across every SDK failure mode, which is what left issue #910
        # spinning through 5 consecutive subagent_failed runs.
        stderr = _sdk_error_summary(result)
    return subprocess.CompletedProcess(
        args=cmd, returncode=returncode, stdout=stdout, stderr=stderr,
    )
