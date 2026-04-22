"""Subprocess helpers extracted from cai.py."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from claude_agent_sdk import ClaudeAgentOptions, query
    from claude_agent_sdk.types import (
        AssistantMessage,
        ResultMessage,
        TextBlock,
    )
    _SDK_AVAILABLE = True
except ImportError:  # pragma: no cover
    _SDK_AVAILABLE = False

from cai_lib.logging_utils import log_cost


# Resolve the CLI path once at import time so the SDK reuses the
# npm-installed `claude` binary audited in Dockerfile instead of the
# copy bundled with the SDK wheel.
_CLI_PATH = shutil.which("claude")


# Bounds for the stderr-capture sink wired into ClaudeAgentOptions.stderr.
# The SDK only pipes the `claude -p` subprocess's stderr when a callback is
# attached — otherwise stderr inherits the parent fd and the CLI's real
# crash reason (e.g. transient network / OOM / signal) vanishes into the
# wrapper's own log stream, leaving callers staring at the SDK's hardcoded
# placeholder "Check stderr output for details".
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


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Thin wrapper around subprocess.run with text mode; defaults check=False.

    ``check`` is overridable — callers that want the stdlib raise-on-nonzero
    semantics can pass ``check=True``. Previously we hard-coded ``check=False``
    and then also forwarded ``**kwargs`` into ``subprocess.run``, which raised
    ``TypeError: got multiple values for keyword argument 'check'`` whenever
    a caller tried to opt in (e.g. `actions/triage.py` on the body-edit path).
    """
    kwargs.setdefault("check", False)
    return subprocess.run(cmd, text=True, **kwargs)


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


async def _collect_results(
    prompt: str,
    options: ClaudeAgentOptions,
) -> tuple[list[ResultMessage], str]:
    """Drive ``query()`` to completion.

    Returns ``(result_messages, last_non_empty_assistant_text)``. Collects
    every ResultMessage (forward-compat: today the CLI emits exactly one)
    and records the final non-empty ``AssistantMessage`` TextBlock so the
    priority-4 stdout-salvage path can fall back to it when ``result`` is
    absent (e.g. ``subtype == "error_max_budget_usd"``).
    """
    results: list[ResultMessage] = []
    last_assistant = ""
    async for msg in query(prompt=prompt, options=options):
        if isinstance(msg, ResultMessage):
            results.append(msg)
        elif isinstance(msg, AssistantMessage):
            parts = [
                b.text for b in msg.content
                if isinstance(b, TextBlock) and b.text.strip()
            ]
            if parts:
                last_assistant = "".join(parts).strip()
    return results, last_assistant


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


# Maximum length of the machine-parsable marker body on a cost comment.
# The marker carries a handful of short key=value tokens; cap defensively
# so a huge ``agent`` or ``category`` string cannot blow past GitHub's
# 65 536-char comment limit.
_COST_COMMENT_MAX_CHARS = 800


def _post_cost_comment(
    target_kind: str,
    target_number: int,
    row: dict,
    agent: str,
) -> None:
    """Best-effort post of a cost-attribution comment on an issue or PR.

    Runs immediately after ``log_cost(row)`` when ``_run_claude_p`` is
    called with both ``target_kind`` and ``target_number`` set. The
    comment body has a machine-parsable ``<!-- cai-cost … -->`` HTML
    marker (matched by ``CAI_COST_COMMENT_RE`` in ``cai_lib.config``
    and stripped out of agent-input comment streams by
    ``_strip_cost_comments``) followed by a short human-readable
    summary line so humans scanning the issue/PR see the cost in the
    GitHub UI without the marker leaking back into subsequent agent
    prompts.

    Swallows every exception: a failed ``gh issue comment`` / ``gh pr
    comment`` must never change the returned ``CompletedProcess`` or
    the wrapped agent's behaviour — the cost comment is informational
    context, not a gating signal.
    """
    try:
        from cai_lib.github import _post_issue_comment, _post_pr_comment
    except Exception as exc:  # noqa: BLE001 — defensive import guard
        print(
            f"[cai cost] failed to import comment helpers: {exc}",
            file=sys.stderr, flush=True,
        )
        return

    try:
        cost_usd = float(row.get("cost_usd") or 0.0)
        turns = int(row.get("num_turns") or 0)
        duration_ms = int(row.get("duration_ms") or 0)
        in_tokens = int(row.get("input_tokens") or 0)
        out_tokens = int(row.get("output_tokens") or 0)
        is_error = bool(row.get("is_error"))
        category = str(row.get("category") or "")
        ts = str(row.get("ts") or "")
        models_field = row.get("models") or {}
        primary_model = ""
        if isinstance(models_field, dict) and models_field:
            primary_model = next(iter(models_field.keys()))
        marker = (
            f"<!-- cai-cost agent={agent} category={category} "
            f"model={primary_model} cost_usd={cost_usd:.4f} "
            f"turns={turns} duration_ms={duration_ms} "
            f"input_tokens={in_tokens} output_tokens={out_tokens} "
            f"is_error={is_error} ts={ts} -->"
        )
        if len(marker) > _COST_COMMENT_MAX_CHARS:
            marker = marker[: _COST_COMMENT_MAX_CHARS - 4] + " -->"
        seconds = duration_ms / 1000.0
        summary = (
            f"**Agent cost:** `{agent or '(no agent)'}` on "
            f"`{primary_model or 'unknown'}` — "
            f"${cost_usd:.4f} / {turns} turn(s) / {seconds:.1f}s "
            f"(category=`{category}`)"
        )
        body = f"{marker}\n{summary}"
    except Exception as exc:  # noqa: BLE001
        print(
            f"[cai cost] failed to format cost comment for "
            f"{target_kind} #{target_number}: {exc}",
            file=sys.stderr, flush=True,
        )
        return

    try:
        if target_kind == "issue":
            _post_issue_comment(target_number, body, log_prefix="cai cost")
        elif target_kind == "pr":
            _post_pr_comment(target_number, body, log_prefix="cai cost")
        else:
            print(
                f"[cai cost] unknown target_kind={target_kind!r}; "
                f"skipping cost comment",
                file=sys.stderr, flush=True,
            )
    except Exception as exc:  # noqa: BLE001
        print(
            f"[cai cost] failed to post cost comment on "
            f"{target_kind} #{target_number}: {exc}",
            file=sys.stderr, flush=True,
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
    **kwargs,
) -> subprocess.CompletedProcess:
    """Run a ``claude -p`` command via the Claude Agent SDK and record its cost.

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

    Cost rows carry exactly the same keys as the pre-SDK version (``ts``,
    ``category``, ``agent``, ``cost_usd``, ``duration_ms``,
    ``duration_api_ms``, ``num_turns``, ``session_id``, ``exit``,
    ``is_error``, the four flat token keys, and an optional ``models``
    per-model rollup). ``subagents`` / ``parent_cost_usd`` are
    intentionally dropped — the CLI format emits exactly one result event
    so there is nothing to attribute, and those pre-SDK code paths were
    dead in production (0/628 rows).
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
            results, last_assistant = asyncio.run(
                asyncio.wait_for(
                    _collect_results(prompt, options), timeout=timeout,
                )
            )
        else:
            results, last_assistant = asyncio.run(
                _collect_results(prompt, options)
            )
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
        "exit": returncode,
        "is_error": bool(result.is_error),
    }
    row.update(flat)
    if models:
        row["models"] = models
    log_cost(row)

    # Post a per-target cost-attribution comment on the issue/PR the
    # agent worked on, when the caller identified a target. Marker
    # body is stripped out of agent-input comment streams by
    # ``_strip_cost_comments`` (keyed on ``CAI_COST_COMMENT_RE``) so
    # it never pollutes downstream prompts, while remaining visible
    # to humans and audit tools that read comments via ``gh``.
    # Best-effort — ``_post_cost_comment`` swallows all exceptions.
    if target_kind is not None and target_number is not None:
        _post_cost_comment(target_kind, target_number, row, agent)

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
