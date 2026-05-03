"""Build pydantic-ai Agents from Claude-Code-style ``.md`` definition files.

Each file starts with a YAML frontmatter block delimited by ``---`` lines,
followed by the markdown that becomes the agent's system prompt::

    ---
    name: cai-refine
    model: deepseek/deepseek-v4-pro
    ---

    # Refinement Agent
    ...
"""
from __future__ import annotations

__all__ = [
    "AGENT_DIR",
    "ConsecutiveFailureGuardrail",
    "EditFileGuardrailAsRetry",
    "GlobPatternSanitizer",
    "GrepGuardrailAsRetry",
    "HistoryCompactorCapability",
    "MicroReadGuardCapability",
    "ModelRequestErrorAsRetry",
    "TOOL_FACTORIES",
    "TOOL_FLAGS",
    "ToolErrorAsRetry",
    "UnknownToolRetry",
    "WriteFileGuardrailAsRetry",
    "build_deep_agent",
    "build_deep_agent_kwargs",
    "build_model",
    "build_model_settings",
    "load_agent_from_md",
    "parse_agent_md",
    "resolve_agent_path",
]

import dataclasses
import difflib
import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import httpx
import yaml
from openai import AsyncOpenAI
from pydantic_ai import Agent, NativeOutput, PromptedOutput, RunContext, TextOutput, ToolOutput
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import ModelRetry, UnexpectedModelBehavior
from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.models import ModelRequestContext, ModelRequestParameters
from pydantic_ai.models.openrouter import OpenRouterModel
from pydantic_ai.providers.openrouter import OpenRouterProvider

AGENT_DIR = Path(__file__).resolve().parent

_OUTPUT_MARKERS = (NativeOutput, PromptedOutput, TextOutput, ToolOutput)


def _wrap_output(output_type: Any) -> Any:
    # PromptedOutput injects the schema into the system prompt and parses
    # the assistant's text as JSON, so the structured result lands without
    # a `final_result` tool call. Weaker models terminate more reliably
    # when they don't have to fake that tool call. NativeOutput would be
    # stronger but DeepSeek doesn't honour json_schema response_format.
    if output_type is None or isinstance(output_type, _OUTPUT_MARKERS):
        return output_type
    return PromptedOutput(output_type)


def resolve_agent_path(name: str) -> Path:
    matches = list(AGENT_DIR.rglob(f"{name}.md"))
    if not matches:
        raise FileNotFoundError(f"agent definition not found: {name}")
    if len(matches) > 1:
        raise ValueError(f"ambiguous agent name: {name}")
    return matches[0]


class ToolErrorAsRetry(AbstractCapability):
    """Turn uncaught tool exceptions into ``ModelRetry`` so the agent can recover.

    Without this, an invalid glob pattern, a malformed regex, a transient
    permission error, or any other Python exception raised inside a tool
    implementation crashes the entire ``agent.run()`` — wasting the run's
    cost and leaving no chance for the model to fix its arguments. With it,
    the model gets the exception type + message back as a retry prompt and
    can correct the call.

    ``ModelRetry`` itself is re-raised untouched so the existing retry
    machinery still works.
    """

    async def on_tool_execute_error(
        self, ctx: Any, *, call: Any, tool_def: Any, args: Any, error: Exception
    ) -> None:
        if isinstance(error, ModelRetry):
            raise error
        raise ModelRetry(
            f"Tool {call.tool_name!r} raised {type(error).__name__}: {error}. "
            f"Adjust the arguments and try again."
        )


class GlobPatternSanitizer(AbstractCapability):
    """Sanitize ``glob`` patterns so ``**`` in a non-pure segment doesn't crash the tool.

    Python's ``pathlib.Path.glob`` rejects patterns where ``**`` is mixed
    with other characters in a path component (e.g. ``**/.github/issues**``)
    with ``ValueError: '**' can only be an entire path component``. Models —
    especially DeepSeek V4 — repeatedly trip this and then keep retrying the
    same broken pattern, burning the request budget. Rewriting offending
    segments at the boundary (``issues**`` → ``issues*``) preserves the
    model's intent (recursive match in that segment) while keeping the call
    valid, so the run keeps making progress instead of looping.
    """

    @staticmethod
    def _sanitize(pattern: str) -> str:
        segments = pattern.split("/")
        changed = False
        for i, seg in enumerate(segments):
            if "**" in seg and seg != "**":
                segments[i] = seg.replace("**", "*")
                changed = True
        return "/".join(segments) if changed else pattern

    async def before_tool_execute(
        self, ctx: Any, *, call: Any, tool_def: Any, args: Any,
    ) -> Any:
        if call.tool_name == "glob":
            pattern = args.get("pattern") if isinstance(args, dict) else None
            if isinstance(pattern, str):
                fixed = self._sanitize(pattern)
                if fixed != pattern:
                    args["pattern"] = fixed
        elif call.tool_name == "grep":
            glob_pattern = args.get("glob_pattern") if isinstance(args, dict) else None
            if isinstance(glob_pattern, str):
                fixed = self._sanitize(glob_pattern)
                if fixed != glob_pattern:
                    args["glob_pattern"] = fixed
        return args


def _get_arg(args: Any, name: str) -> Any:
    """Extract a named argument from tool ``args``, which may be a dict or object."""
    if isinstance(args, dict):
        return args.get(name)
    return getattr(args, name, None)


class GrepGuardrailAsRetry(AbstractCapability):
    """Nudge the model away from repeated zero-result ``grep`` queries.

    Models sometimes spiral on minor regex variations that all return
    zero matches, burning context without progress. After
    ``_THRESHOLD`` consecutive empty grep results, the result is
    returned prefixed with a warning suggesting ``read_file()``
    instead of raising ``ModelRetry`` — the grep runs normally and
    its output is preserved. Any non-empty grep result resets the
    counter, so a single productive search clears the streak.

    State lives on the instance, but ``for_run`` returns a fresh
    instance per run so concurrent runs of the same agent don't share
    the counter.
    """

    _THRESHOLD = 8
    _NO_MATCH_PREFIX = "No matches for"

    def __init__(self) -> None:
        super().__init__()
        self._empty_grep_count = 0
        self._recently_removed: set[str] = set()
        self._last_nonempty_grep: tuple | None = None

    async def for_run(self, ctx: Any) -> "GrepGuardrailAsRetry":
        return GrepGuardrailAsRetry()

    async def after_tool_execute(
        self,
        ctx: Any,
        *,
        call: Any,
        tool_def: Any,
        args: Any,
        result: Any,
    ) -> Any:
        # Track edit_file old_string values so we can later exempt
        # verification greps from the empty-result counter.
        if call.tool_name == "edit_file":
            old_string = _get_arg(args, "old_string")
            if isinstance(old_string, str) and old_string:
                self._recently_removed.add(old_string)
            self._last_nonempty_grep = None
            return result

        if call.tool_name != "grep":
            return result

        text = result if isinstance(result, str) else ""
        stripped = text.strip()
        is_empty = not stripped or stripped.startswith(self._NO_MATCH_PREFIX)
        if not is_empty:
            self._empty_grep_count = 0
            # Detect identical-argument non-empty grep loops.
            # pydantic_deep's grep tool truncates output at 50-150 lines
            # with messages like "showing first 50 of 67 matches". Agents
            # re-call with the same arguments expecting pagination, but
            # grep output is idempotent — they get the same truncated
            # lines every time. Catch that here.
            current_key = (
                _get_arg(args, "pattern"),
                _get_arg(args, "path"),
                _get_arg(args, "glob_pattern"),
            )
            if self._last_nonempty_grep == current_key:
                self._last_nonempty_grep = None
                raise ModelRetry(
                    "You just called grep with the same arguments as your last "
                    "non-empty grep call. grep output is truncated by the tool "
                    "framework — calling grep with identical arguments returns "
                    "the same truncated top-N lines, not paginated results. "
                    "Instead, use file_info to discover the file's total line "
                    "count, then use grep with a narrower pattern, or use "
                    "read_file with specific offsets to get the content you "
                    "need."
                )
            self._last_nonempty_grep = current_key
            return result

        # Exempt verification greps: when the grep pattern contains
        # an old_string that was recently removed via edit_file, the
        # zero-result grep is confirming the edit — not a wild goose
        # chase.  Don't count it or reset the streak.
        if self._recently_removed:
            pattern = _get_arg(args, "pattern")
            if isinstance(pattern, str) and pattern:
                if any(
                    removed in pattern or re.escape(removed) in pattern
                    for removed in self._recently_removed
                ):
                    return result
                # re.escape escaping can differ between Python versions
                # (e.g. whether space is escaped).  Fall back to testing
                # whether the grep regex itself matches a removed string.
                try:
                    if any(
                        re.search(pattern, removed)
                        for removed in self._recently_removed
                    ):
                        return result
                except re.error:
                    pass

        self._empty_grep_count += 1
        if self._empty_grep_count >= self._THRESHOLD:
            self._empty_grep_count = 0
            return (
                f"Warning: you have made {self._THRESHOLD} consecutive zero-result grep queries. "
                f"Consider switching to read_file() instead. "
                f"(The grep was executed normally — results below.)\n\n"
                f"{text}"
            )
        return result


class EditFileGuardrailAsRetry(AbstractCapability):
    """Break the model out of a repeated ``edit_file`` "same result" loop.

    When ``old_string`` matches multiple locations in the file,
    ``str_replace`` replaces the **first** match — which may be the
    wrong one. Subsequent retries find the edit already been applied
    (in the wrong place) or the file unchanged at the intended spot,
    producing the error "``edit_file`` returned the same result 3
    times in a row." The "same result" error can also mean the edit
    was already successfully applied (the text is already present).
    This capability catches that ``ModelRetry`` and enriches the
    message with hints for both possible causes: include more
    surrounding context so the model can disambiguate the target
    location, or check whether the intended change is already there
    before retrying.

    Before the tool executes, it also pre-verifies that ``old_string``
    exists in the target file and is not ambiguous (matching multiple
    locations). Agents sometimes reconstruct ``old_string`` from memory
    rather than copying verbatim from ``read_file`` output, wasting the
    retry budget on strings that can never match.  When ``old_string``
    appears more than once the guardrail rejects the call with the
    match count and disambiguation guidance — catching the ambiguity
    before the edit lands on the wrong location.

    The first pre-verify failure on a given path raises ``ModelRetry``
    with re-read guidance.  Subsequent consecutive failures on the same
    path **return a warning string with the file's actual content
    embedded** instead of raising.  This is critical: pydantic-ai's
    per-tool ``max_retries=3`` cap aborts the entire workflow with
    ``UnexpectedModelBehavior`` after the third ``ModelRetry``, so a
    model that ignores the "re-read the file" instruction (a common
    failure mode of large reasoning models that reconstruct file state
    from memory) would otherwise crash the run.  Returning a warning
    short-circuits the tool execution without consuming the retry
    budget and forces the file's truth into context, breaking the
    guess-and-retry cycle.  See trace
    ``24990cf37f1d20be844807964fd87951`` for the failure mode this
    addresses.
    """

    _ESCALATE_AT = 2  # 1st failure: ModelRetry; 2nd+: return warning string

    def __init__(self) -> None:
        # Per-path consecutive pre-verify failure count. Reset on a
        # successful edit_file call (any path-keyed entry is cleared
        # when its path is edited successfully).
        self._fail_count: dict[str, int] = {}

    async def for_run(self, ctx: Any) -> "EditFileGuardrailAsRetry":
        return EditFileGuardrailAsRetry()

    async def wrap_tool_execute(
        self, ctx: Any, *, call: Any, tool_def: Any, args: Any, handler: Any,
    ) -> Any:
        if call.tool_name != "edit_file":
            return await handler(args)
        old_string = _get_arg(args, "old_string")
        path = _get_arg(args, "path")
        if not (isinstance(old_string, str) and old_string and isinstance(path, str) and path):
            return await handler(args)
        try:
            content = Path(path).read_text()
        except (FileNotFoundError, PermissionError, OSError):
            return await handler(args)

        not_found = old_string not in content
        match_count = 0 if not_found else content.count(old_string)
        ambiguous = match_count > 1

        if not (not_found or ambiguous):
            # Pre-verify passes; run the actual tool. Clear the
            # counter on success so a future unrelated failure starts
            # a fresh streak.
            result = await handler(args)
            self._fail_count.pop(path, None)
            return result

        count = self._fail_count.get(path, 0) + 1
        self._fail_count[path] = count

        if not_found:
            if count < self._ESCALATE_AT:
                hint = self._closest_match_hint(old_string, content)
                raise ModelRetry(
                    f"old_string not found in {path}. "
                    f"Re-read the file with read_file and copy the exact target "
                    f"lines — including all whitespace, blank lines, and surrounding "
                    f"content — into old_string. Do not reconstruct from memory."
                    + (f"\n\n{hint}" if hint else "")
                )
            hint = self._closest_match_hint(old_string, content)
            preview = self._render_preview(path, content)
            return (
                f"Warning: edit_file failed {count} consecutive times on {path} "
                f"because old_string was not found. You appear to be reconstructing "
                f"old_string from memory across retries instead of re-reading the "
                f"file. The actual file content is included below — copy your "
                f"old_string verbatim from this text (preserving every space, tab, "
                f"and blank line) before calling edit_file again.\n\n"
                + (f"{hint}\n\n" if hint else "")
                + f"{preview}"
            )

        # ambiguous (match_count > 1)
        if count < self._ESCALATE_AT:
            raise ModelRetry(
                f"old_string appears {match_count} times in {path}. "
                f"Include more surrounding context — at minimum one unique line "
                f"above AND below the target location — to disambiguate."
            )
        preview = self._render_preview(path, content)
        return (
            f"Warning: edit_file failed {count} consecutive times on {path} "
            f"because old_string still matches {match_count} locations. Use a "
            f"wider old_string with a unique anchor (function name, comment, or "
            f"unique identifier) above AND below the target. The actual file "
            f"content is included below.\n\n{preview}"
        )

    @staticmethod
    def _render_preview(path: str, content: str) -> str:
        """Format file content for inclusion in an escalation warning,
        capping size to keep the warning readable in conversation history."""
        max_chars = 8000
        if len(content) <= max_chars:
            body = content
            footer = ""
        else:
            body = content[:max_chars]
            footer = (
                f"\n...[file truncated at {max_chars} chars; total "
                f"{len(content)} chars. Use read_file with offset/limit "
                f"to inspect later regions.]"
            )
        return f"--- {path} ---\n{body}{footer}"

    @staticmethod
    def _closest_match_hint(old_string: str, content: str) -> str:
        """Find the closest-matching window in *content* for *old_string*.

        Returns a hint string with line numbers, similarity ratio, and a
        unified diff, or an empty string when no close match is found.
        Searches are skipped when *old_string* has <= 1 line or the file
        exceeds 20 000 lines.
        """
        old_lines = old_string.splitlines(keepends=True)
        if len(old_lines) <= 1:
            return ""

        file_lines = content.splitlines(keepends=True)
        if len(file_lines) > 20000:
            return ""

        n = len(old_lines)
        best_ratio = 0.0
        best_start = 0

        for i in range(len(file_lines) - n + 1):
            window = "".join(file_lines[i : i + n])
            ratio = difflib.SequenceMatcher(None, old_string, window).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_start = i

        if best_ratio < 0.5:
            return ""

        closest = "".join(file_lines[best_start : best_start + n])
        diff = "".join(
            difflib.unified_diff(
                closest.splitlines(keepends=True),
                old_string.splitlines(keepends=True),
                fromfile=f"file lines {best_start + 1}-{best_start + n}",
                tofile="old_string",
                lineterm="",
            )
        )

        return (
            f"Closest match at lines {best_start + 1}-{best_start + n} "
            f"({best_ratio:.0%} similar). Diff:\n{diff}"
        )

    async def on_tool_execute_error(
        self, ctx: Any, *, call: Any, tool_def: Any, args: Any, error: Exception
    ) -> None:
        if not isinstance(error, ModelRetry):
            return
        if call.tool_name != "edit_file":
            return
        message = str(error)
        if "same result" not in message:
            return
        raise ModelRetry(
            f"{message} The old_string may match multiple locations in "
            f"the file, or the edit may have already been applied (the text "
            f"is already present). If you successfully edited this file "
            f"earlier, check whether the intended change is already there "
            f"before retrying. Include more surrounding context — at minimum "
            f"one unique line above or below (e.g., a slug, title, or "
            f"function name) — to disambiguate the target location."
        )


class WriteFileGuardrailAsRetry(AbstractCapability):
    """Nudge the model away from ``write_file`` when a targeted ``edit_file`` would suffice.

    Models sometimes rewrite entire files via ``write_file`` for small
    fixes (e.g. a 2-line change in a 270-line file).  The full file
    content lives in the ``ToolCallPart`` arguments and bloats
    conversation context, forcing downstream LLM calls to process it
    repeatedly.

    This capability intercepts ``write_file`` calls in
    :meth:`before_tool_execute`: when the target file already exists,
    it reads the disk content and compares it against the proposed
    ``content`` via ``difflib.SequenceMatcher.ratio()``.  If similarity
    is at least 80 %, it raises ``ModelRetry`` telling the model to use
    ``edit_file`` instead and explaining the context-cost impact.  When
    the file doesn't exist, or the content differs substantially, the
    call passes through unchanged.
    """

    _SIMILARITY_THRESHOLD = 0.80

    async def before_tool_execute(
        self, ctx: Any, *, call: Any, tool_def: Any, args: Any,
    ) -> Any:
        if call.tool_name != "write_file":
            return args
        path = _get_arg(args, "path")
        new_content = _get_arg(args, "content")
        if not (isinstance(path, str) and path and isinstance(new_content, str)):
            return args
        try:
            existing = Path(path).read_text()
        except (FileNotFoundError, PermissionError, OSError):
            # File doesn't exist — creating a new file is fine.
            return args

        # Count lines for a human-scale description in the retry message.
        new_lines = new_content.count("\n") + (0 if new_content.endswith("\n") else 1)
        existing_lines = existing.count("\n") + (0 if existing.endswith("\n") else 1)

        similarity = difflib.SequenceMatcher(None, existing, new_content).ratio()
        if similarity >= self._SIMILARITY_THRESHOLD:
            raise ModelRetry(
                f"write_file on {path!r} is {similarity:.0%} identical to the "
                f"existing file ({existing_lines} lines).  The proposed content "
                f"would replace {existing_lines} lines with {new_lines} — a "
                f"near-duplicate rewrite that bloats conversation context "
                f"(each write_file carries the full file content in "
                f"ToolCallPart arguments) and inflates downstream costs.  Use "
                f"edit_file instead with a targeted old_string/new_string pair "
                f"to change only the lines that differ."
            )
        return args


class UnknownToolRetry(AbstractCapability):
    """Enrich ``Unknown tool name`` ModelRetry errors with concrete guidance.

    When a model hallucinates a non-existent tool like ``execute``,
    pydantic_ai raises ``ModelRetry("Unknown tool name: 'execute'. ...")``
    which gets the vague suffix ``"Fix the errors and try again."`` from
    ``RetryPromptPart.model_response()``. DeepSeek models interpret this
    as "fix your command syntax" and retry with different commands
    instead of switching to a valid tool. This capability enriches the
    message with explicit anti-hallucination guidance.
    """

    async def on_tool_execute_error(
        self, ctx: Any, *, call: Any, tool_def: Any, args: Any, error: Exception
    ) -> None:
        if not isinstance(error, ModelRetry):
            return
        message = str(error)
        if not message.startswith("Unknown tool name:"):
            raise error
        match = re.match(
            r"Unknown tool name: '(\w+)'\. Available tools: (.+)\.",
            message,
        )
        if match:
            tool_name = match.group(1)
            available = match.group(2)
            raise ModelRetry(
                f"Unknown tool name: '{tool_name}'. "
                f"The tool you called does not exist.\n"
                f"Available tools: {available}.\n"
                f"You cannot run shell commands. "
                f"Use read_file or grep to inspect files instead."
            )
        raise error


class ModelRequestErrorAsRetry(AbstractCapability):
    """Turn transient model-call errors into ``ModelRetry`` so the run survives.

    OpenRouter occasionally returns a malformed HTTP 200 (all-None fields in the
    ChatCompletion schema -> ``UnexpectedModelBehavior``; non-JSON or truncated
    body -> ``JSONDecodeError`` / ``httpx.RemoteProtocolError``), especially on
    DeepSeek V4 routing. The OpenAI SDK's ``max_retries`` only covers transport
    errors and 5xx — body-parse failures land here untouched, and without this
    one bad response aborts the entire agent run. pydantic_ai's built-in
    ``request_limit`` still caps total retries, so this does not loop forever.
    """

    _RETRYABLE = (
        UnexpectedModelBehavior,
        json.JSONDecodeError,
        httpx.RemoteProtocolError,
    )

    async def on_model_request_error(self, ctx: Any, *, request_context: Any, error: Exception) -> None:
        if isinstance(error, self._RETRYABLE):
            raise ModelRetry(
                f"Model returned a malformed response "
                f"({type(error).__name__}: {error}). Retrying..."
            )
        raise error


class HistoryCompactorCapability(AbstractCapability):
    """Replace stale tool outputs in message history before each model request,
    and short-circuit duplicate ``read_file`` calls at execute time.

    Repeated ``read_file`` on large files and ``ls`` / ``glob`` / ``grep``
    produce stale outputs that stay in the message history and are re-sent
    to the model every turn, causing quadratic cost growth.  Identical
    sequential ``read_file`` calls without intervening edits waste both
    latency and tokens.

    This capability:

    * Compacts older redundant tool outputs into short summary strings
      in :meth:`before_model_request`, before they reach the model.
    * Short-circuits duplicate ``read_file`` calls in
      :meth:`wrap_tool_execute` when no filesystem edits have occurred
      since the prior identical call.
    """

    _COMPACTABLE_TOOLS = frozenset({"read_file", "ls", "glob", "grep"})
    _FILE_MODIFYING_TOOLS = frozenset({
        "write_file", "edit_file", "move_file", "delete_file",
        "batch_move", "batch_delete",
    })

    async def for_run(self, ctx: Any) -> "HistoryCompactorCapability":
        return HistoryCompactorCapability()

    # ------------------------------------------------------------------
    # before_model_request — compact superseded tool outputs
    # ------------------------------------------------------------------

    async def before_model_request(
        self, ctx: RunContext, request_context: ModelRequestContext,
    ) -> ModelRequestContext:
        messages: list = list(request_context.messages)

        # First pass: collect ToolCallPart args dicts keyed by tool_call_id.
        call_args: dict[str, dict] = {}
        for msg in messages:
            if isinstance(msg, ModelResponse):
                for part in msg.parts:
                    if isinstance(part, ToolCallPart):
                        call_args[part.tool_call_id] = part.args_as_dict()

        # Second pass: find the index of the latest ToolReturnPart for each
        # compaction key.  Display/pagination args (offset, limit) are ignored
        # so that successive pagination reads on the same file are compacted.
        latest: dict[tuple, int] = {}  # key → message index
        for i, msg in enumerate(messages):
            if not isinstance(msg, ModelRequest):
                continue
            for part in msg.parts:
                if not isinstance(part, ToolReturnPart):
                    continue
                if part.tool_name not in self._COMPACTABLE_TOOLS:
                    continue
                k = self._compaction_key(part, call_args)
                if k is not None:
                    latest[k] = i

        # Third pass: rebuild messages, replacing content on superseded returns.
        for i, msg in enumerate(messages):
            if not isinstance(msg, ModelRequest):
                continue
            new_parts: list | None = None
            for j, part in enumerate(msg.parts):
                if not isinstance(part, ToolReturnPart):
                    continue
                if part.tool_name not in self._COMPACTABLE_TOOLS:
                    continue
                k = self._compaction_key(part, call_args)
                if k is None:
                    continue
                latest_idx = latest.get(k)
                if latest_idx is not None and i < latest_idx:
                    if new_parts is None:
                        new_parts = list(msg.parts)
                    target_desc = self._target_desc(part.tool_name, k)
                    new_parts[j] = dataclasses.replace(
                        part,
                        content=(
                            f"[Content omitted — superseded by a newer call "
                            f"to `{part.tool_name}` on {target_desc}]"
                        ),
                    )
            if new_parts is not None:
                messages[i] = dataclasses.replace(msg, parts=new_parts)

        return dataclasses.replace(request_context, messages=messages)

    @staticmethod
    def _compaction_key(
        return_part: ToolReturnPart, call_args: dict[str, dict],
    ) -> tuple | None:
        """Build a stable key for compaction from a tool return and its call args."""
        tid = return_part.tool_call_id
        args = call_args.get(tid, {})
        tool_name = return_part.tool_name
        if tool_name in ("read_file", "ls"):
            return (tool_name, args.get("path"))
        elif tool_name == "glob":
            return (tool_name, args.get("pattern"), args.get("path"))
        elif tool_name == "grep":
            return (
                tool_name,
                args.get("pattern"),
                args.get("path"),
                args.get("glob_pattern"),
            )
        return None

    @staticmethod
    def _target_desc(tool_name: str, key: tuple) -> str:
        """Human-readable target description for the omission placeholder."""
        if tool_name in ("read_file", "ls"):
            return str(key[1])
        if tool_name == "glob":
            return f"pattern={key[1]!r}, path={key[2]!r}"
        if tool_name == "grep":
            return f"pattern={key[1]!r}, path={key[2]!r}, glob_pattern={key[3]!r}"
        return ""

    # ------------------------------------------------------------------
    # wrap_tool_execute — short-circuit duplicate read_file
    # ------------------------------------------------------------------

    async def wrap_tool_execute(
        self, ctx: RunContext, *, call: ToolCallPart, tool_def: Any, args: Any, handler: Any,
    ) -> Any:
        if call.tool_name != "read_file":
            return await handler(args)

        current_args = call.args_as_dict()

        # Scan ctx.messages backward for a prior read_file call whose range
        # contains (or exactly matches) the current call's range.
        # Skip the current call itself: the latest ModelResponse already
        # contains this ToolCallPart by the time wrap_tool_execute fires,
        # and parallel read_file calls share that ModelResponse.
        prior_msg_idx: int | None = None
        matched_prior_args: dict | None = None
        matched_by_overlap = False
        for i in range(len(ctx.messages) - 1, -1, -1):
            msg = ctx.messages[i]
            if not isinstance(msg, ModelResponse):
                continue
            for part in msg.parts:
                if not isinstance(part, ToolCallPart):
                    continue
                if part.tool_name != "read_file":
                    continue
                if part.tool_call_id == call.tool_call_id:
                    continue
                prior_args = part.args_as_dict()
                # Different path → not a match.
                if prior_args.get("path") != current_args.get("path"):
                    continue
                # Check whether the prior range fully contains the
                # current range.
                prior_offset = prior_args.get("offset", 0)
                current_offset = current_args.get("offset", 0)
                prior_limit = prior_args.get("limit")
                current_limit = current_args.get("limit")
                if prior_limit is None:
                    # Prior read covered from prior_offset to EOF — any
                    # current_offset >= prior_offset is fully contained.
                    if current_offset >= prior_offset:
                        prior_msg_idx = i
                        matched_prior_args = prior_args
                        matched_by_overlap = True
                        break
                elif current_limit is not None:
                    # Both have explicit limits — compare end positions.
                    prior_end = prior_offset + prior_limit
                    current_end = current_offset + current_limit
                    if prior_offset <= current_offset and prior_end >= current_end:
                        prior_msg_idx = i
                        matched_prior_args = prior_args
                        matched_by_overlap = True
                        break
                # current_limit is None but prior_limit is not None:
                # we can't confirm the prior range covers to EOF →
                # fall through to exact-args below.
                # Exact-args fast-path: catches identical calls the
                # overlap logic above also covers; kept as a safety net.
                if prior_args == current_args:
                    prior_msg_idx = i
                    matched_prior_args = prior_args
                    break
            if prior_msg_idx is not None:
                break

        if prior_msg_idx is None:
            return await handler(args)

        # Scan forward from that prior call for any file-modifying tool calls.
        for i in range(prior_msg_idx + 1, len(ctx.messages)):
            msg = ctx.messages[i]
            if not isinstance(msg, ModelResponse):
                continue
            for part in msg.parts:
                if not isinstance(part, ToolCallPart):
                    continue
                if part.tool_name in self._FILE_MODIFYING_TOOLS:
                    return await handler(args)

        # No intervening file-modifying tool found — short-circuit to a
        # warning so the model doesn't waste a round-trip re-reading.
        path = current_args.get("path", "unknown")
        if matched_by_overlap:
            current_offset = current_args.get("offset", 0)
            current_limit = current_args.get("limit")
            prior_offset = matched_prior_args.get("offset", 0)
            prior_limit = matched_prior_args.get("limit")
            current_limit_str = f", limit={current_limit}" if current_limit is not None else ""
            prior_limit_str = f", limit={prior_limit}" if prior_limit is not None else ", limit=EOF"
            warning = (
                f"Warning: read_file({path!r}, offset={current_offset}"
                f"{current_limit_str}) is covered by a prior "
                f"read_file({path!r}, offset={prior_offset}{prior_limit_str}) "
                f"at message index {prior_msg_idx} — file content has "
                f"not changed; review your previous messages for the content."
            )
        else:
            warning = (
                f"Warning: identical read_file({path!r}) call at message "
                f"index {prior_msg_idx} — file content has not changed; "
                f"review your previous messages for the content."
            )
        return warning


class MicroReadGuardCapability(AbstractCapability):
    """Auto-extend tiny ``limit`` values on ``read_file`` to prevent micro-reading.

    Agents sometimes call ``read_file`` with ``limit=15`` or ``limit=60``,
    producing a wasteful read→think→read→think loop. This capability bumps
    any ``limit`` below 200 up to 200, so the agent gets a meaningful chunk
    on every call. An absent ``limit`` (whole-file read) is left alone.
    """

    _MIN_LIMIT = 200

    async def before_tool_execute(
        self, ctx: Any, *, call: Any, tool_def: Any, args: Any,
    ) -> Any:
        if call.tool_name == "read_file":
            limit = _get_arg(args, "limit")
            if limit is not None and isinstance(limit, int) and limit < self._MIN_LIMIT:
                if isinstance(args, dict):
                    args["limit"] = self._MIN_LIMIT
                else:
                    setattr(args, "limit", self._MIN_LIMIT)
        return args


class ConsecutiveFailureGuardrail(AbstractCapability):
    """Detect persistent tool failure across parameter variations.

    When the same tool produces errors or warnings 5 consecutive times
    (across any parameter variations), this guardrail raises
    ``ModelRetry`` naming the failing tool and instructing the agent to
    abandon it entirely. A successful tool execution resets the counter.

    This catches the pattern that individual tool-specific guardrails
    miss: the model retrying the same tool over and over with slightly
    tweaked parameters, never recognizing the tool is blocked.

    State lives on the instance, but ``for_run`` returns a fresh
    instance per run so concurrent sessions don't share the counter.
    """

    _THRESHOLD = 5

    def __init__(self) -> None:
        super().__init__()
        self._error_count: dict[str, int] = {}
        self._warning_count: dict[str, int] = {}

    async def for_run(self, ctx: Any) -> "ConsecutiveFailureGuardrail":
        return ConsecutiveFailureGuardrail()

    async def on_tool_execute_error(
        self, ctx: Any, *, call: Any, tool_def: Any, args: Any, error: Exception
    ) -> None:
        tool_name = call.tool_name
        count = self._error_count.get(tool_name, 0) + 1
        self._error_count[tool_name] = count
        if count >= self._THRESHOLD:
            self._error_count[tool_name] = 0
            raise ModelRetry(
                f"Tool {tool_name!r} has failed {count} consecutive times "
                f"with varying arguments. Stop using {tool_name!r} entirely "
                f"and switch to a fundamentally different approach — "
                f"e.g. read a file instead of grepping, use glob instead "
                f"of ls, or report partial findings rather than burning "
                f"more calls."
            )

    async def after_tool_execute(
        self,
        ctx: Any,
        *,
        call: Any,
        tool_def: Any,
        args: Any,
        result: Any,
    ) -> Any:
        tool_name = call.tool_name
        if isinstance(result, str) and result.startswith("Warning:"):
            count = self._warning_count.get(tool_name, 0) + 1
            self._warning_count[tool_name] = count
            if count >= self._THRESHOLD:
                self._warning_count[tool_name] = 0
                raise ModelRetry(
                    f"Tool {tool_name!r} has produced {count} consecutive "
                    f"warning results with varying arguments. Stop using "
                    f"{tool_name!r} entirely and switch to a fundamentally "
                    f"different approach — e.g. read a file instead of "
                    f"grepping, use glob instead of ls, or report partial "
                    f"findings rather than burning more calls."
                )
        else:
            # Successful tool execution resets counters for that tool.
            self._error_count.pop(tool_name, None)
            self._warning_count.pop(tool_name, None)
        return result


# httpx defaults read/write/pool to None (infinite). Without these, a silently
# dropped OpenRouter request will hang the agent indefinitely instead of
# surfacing as a retryable error.
_HTTP_TIMEOUT = httpx.Timeout(connect=10.0, read=180.0, write=30.0, pool=10.0)
_MAX_RETRIES = 3


async def _capture_openrouter_cost(response: httpx.Response) -> None:
    try:
        await response.aread()
        data = response.json()

        usage = data.get("usage") or {}
        cost = usage.get("cost")
        if cost is None:
            model_extra = usage.get("model_extra") or {}
            cost = model_extra.get("cost")
        if cost is None:
            cost = data.get("cost")

        if cost is not None:
            from langfuse import get_client

            get_client().update_current_generation(cost_details={"total": float(cost)})
    except Exception:
        pass


@lru_cache(maxsize=None)
def _provider() -> OpenRouterProvider:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY is not set")

    client = httpx.AsyncClient(
        timeout=_HTTP_TIMEOUT,
        event_hooks={"response": [_capture_openrouter_cost]},
    )

    return OpenRouterProvider(
        openai_client=AsyncOpenAI(
            api_key=key,
            base_url="https://openrouter.ai/api/v1",
            max_retries=_MAX_RETRIES,
            http_client=client,
        )
    )


class _OpenRouterServerToolsModel(OpenRouterModel):
    """``OpenRouterModel`` that prepends OpenRouter server-side tools (e.g.
    ``{"type": "openrouter:web_search"}``) to the wire-level ``tools`` list.

    pydantic-ai's ``WebSearchTool`` capability translates to a top-level
    ``web_search_options`` field, which OpenRouter's Google provider rejects
    with HTTP 404 under ``provider.require_parameters: True`` — Google does
    not declare ``web_search_options`` as supported. The server-tool shape
    routes through OpenRouter's own search infra instead, returns OpenAI-
    format ``url_citation`` annotations, and survives ``require_parameters``.
    """

    def __init__(
        self,
        model_name: str,
        *,
        server_tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(model_name, **kwargs)
        self._server_tools = list(server_tools or [])

    def _get_tools(self, model_request_parameters: ModelRequestParameters) -> list[Any]:
        return [*self._server_tools, *super()._get_tools(model_request_parameters)]


def parse_agent_md(path: str | Path) -> tuple[dict, str]:
    """Return ``(config_dict, system_prompt_text)`` from a frontmatter ``.md`` file."""
    text = Path(path).read_text()
    if not text.startswith("---"):
        raise ValueError(f"{path}: missing YAML frontmatter")
    # Find the closing '---' delimiter — it must be on a line by itself.
    lines = text.splitlines()
    close_idx = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            close_idx = i
            break
    if close_idx is None:
        raise ValueError(f"{path}: malformed frontmatter (expected closing '---' delimiter)")
    frontmatter = "\n".join(lines[1:close_idx])
    body = "\n".join(lines[close_idx + 1:])
    config = yaml.safe_load(frontmatter) or {}
    if "name" not in config:
        raise ValueError(f"{path}: frontmatter missing required 'name' field")
    return config, body.strip()


def build_model(config: dict) -> OpenRouterModel:
    """Build a pydantic-ai model from the ``model`` frontmatter key.

    The value is the full OpenRouter model ID, e.g. ``deepseek/deepseek-v4-pro``
    or ``deepseek/deepseek-v4-flash``.

    All requests go through OpenRouter, so the returned model is always an
    ``_OpenRouterServerToolsModel``. When ``web_search`` is in
    ``config['tools']``, ``{"type": "openrouter:web_search"}`` is injected
    into the wire-level tools list so OpenRouter performs search server-side
    and returns ``url_citation`` annotations.
    """
    model_id = config.get("model")
    if not model_id:
        raise ValueError("frontmatter missing required 'model' field")
    server_tools: list[dict[str, Any]] = []
    if "web_search" in (config.get("tools") or []):
        server_tools.append({"type": "openrouter:web_search"})
    return _OpenRouterServerToolsModel(
        model_id,
        provider=_provider(),
        server_tools=server_tools,
    )


def build_model_settings(config: dict) -> dict[str, Any] | None:
    """Translate optional model knobs from frontmatter to a ``model_settings`` dict.

    Supported keys: ``max_tokens``, ``temperature``, ``frequency_penalty``,
    ``presence_penalty``, ``reasoning``. All are optional; returns
    ``None`` when none are set.

    ``frequency_penalty`` / ``presence_penalty`` are the primary defence against
    token-repetition loops (the "ataata"/[PAD] meltdown): they add a per-token
    cost proportional to how often the token has already appeared, making it
    mathematically harder for the model to cycle forever.

    ``reasoning: false`` opts out of OpenRouter's extended-reasoning
    pass-through for models like ``moonshotai/kimi-k2.6`` that emit a
    long invisible reasoning stream by default. The stream bills as
    output tokens but isn't surfaced as a visible message part, so it
    silently eats the response budget and triggers
    ``UnexpectedModelBehavior`` when ``finish_reason=length`` lands
    before any tool call or text is emitted.
    """
    settings: dict[str, Any] = {}

    max_tokens = config.get("max_tokens")
    if max_tokens is not None:
        if not isinstance(max_tokens, int) or max_tokens <= 0:
            raise ValueError(f"'max_tokens' must be a positive int, got {max_tokens!r}")
        settings["max_tokens"] = max_tokens

    temperature = config.get("temperature")
    if temperature is not None:
        if not isinstance(temperature, (int, float)) or not (0.0 <= float(temperature) <= 2.0):
            raise ValueError(f"'temperature' must be a float in [0, 2], got {temperature!r}")
        settings["temperature"] = float(temperature)

    for penalty_key in ("frequency_penalty", "presence_penalty"):
        value = config.get(penalty_key)
        if value is not None:
            if not isinstance(value, (int, float)) or not (-2.0 <= float(value) <= 2.0):
                raise ValueError(
                    f"'{penalty_key}' must be a float in [-2, 2], got {value!r}"
                )
            settings[penalty_key] = float(value)

    reasoning = config.get("reasoning")
    if reasoning is not None:
        if not isinstance(reasoning, bool):
            raise ValueError(f"'reasoning' must be a bool, got {reasoning!r}")
        settings["extra_body"] = {"reasoning": {"enabled": reasoning}}

    return settings or None


# Maps a ``tools:`` frontmatter entry to the ``create_deep_agent`` flags it enables.
# Anything not listed in an agent's ``tools:`` is forced off — the full surface is
# explicit, so adding a new toolset to pydantic-deep won't silently leak in.
TOOL_FLAGS: dict[str, dict[str, Any]] = {
    "filesystem": {"include_filesystem": True},
    "filesystem_read": {"include_filesystem": True},
    "filesystem_write": {"include_filesystem": True},
    "todo": {"include_todo": True},
    "subagents": {
        "include_subagents": True,
    },
    # Opt-in flag for pydantic_deep's built-in research + planner subagents.
    # Keep separate from "subagents" because the research subagent has execute
    # tools that trigger a DeferredToolRequests bug in pydantic_deep when
    # interrupt_on is not set.  Only add this to agents that explicitly need
    # open-ended research or plan-mode capability.
    "subagents_builtin": {
        "include_builtin_subagents": True,
        "include_plan": True,
    },
    "memory": {"include_memory": True},
    "skills": {"include_skills": True},
    "history_archive": {"include_history_archive": True},
    "context_manager": {"context_manager": True},
    "checkpoints": {"include_checkpoints": True},
    "teams": {"include_teams": True},
    "improve": {"include_improve": True},
    "liteparse": {"include_liteparse": True},
    # web_search keeps pydantic_deep's flag off; ``build_model`` injects
    # ``{"type": "openrouter:web_search"}`` into the wire-level ``tools`` so
    # OpenRouter handles search server-side. Pydantic_deep's default
    # ``WebSearch()`` adds a DuckDuckGo local fallback that loops on identical
    # results and aborts the run with ``UnexpectedModelBehavior: Tool
    # 'duckduckgo_search' exceeded max retries``.
    "web_search": {"web_search": False},
    "web_fetch": {"web_fetch": True},
}

# Pairs of tool keys that map to the same backing toolset and so cannot coexist.
_TOOL_CONFLICTS: list[frozenset[str]] = [
    frozenset({"filesystem", "filesystem_read", "filesystem_write"}),
]

# Pruning rules applied after ``create_deep_agent`` for tool keys that share
# a backing toolset with another, broader key. Each entry is keyed on the
# tool name in ``tools:`` and maps a toolset id to the set of tool names to
# keep on that toolset.
_TOOL_PRUNE: dict[str, dict[str, frozenset[str]]] = {
    "filesystem_read": {
        "deep-console": frozenset({"read_file", "ls", "glob", "grep"}),
    },
    "filesystem_write": {
        "deep-console": frozenset({"write_file", "edit_file"}),
    },
}

# Tool names that resolve to a code-registered tool rather than a
# pydantic-deep ``include_*`` flag. They are looked up lazily so this
# module stays import-light. Each factory returns a ``Tool`` (or an
# object pydantic-ai accepts in ``tools=``) and is appended to the
# agent's ``tools=`` kwarg in :func:`build_deep_agent`.
TOOL_FACTORIES: dict[str, str] = {
    # name → "module:attr" import target
    "spike_run": "cai.agents.spike_tool:SPIKE_RUN_TOOL",
    "move_file": "cai.agents.fs_ops:MOVE_FILE_TOOL",
    "delete_file": "cai.agents.fs_ops:DELETE_FILE_TOOL",
    "batch_move": "cai.agents.fs_ops:BATCH_MOVE_TOOL",
    "batch_delete": "cai.agents.fs_ops:BATCH_DELETE_TOOL",
    "conflict_list": "cai.agents.conflict_tools:CONFLICT_LIST_TOOL",
    "conflict_resolve": "cai.agents.conflict_tools:CONFLICT_RESOLVE_TOOL",
    "conflict_cleanup": "cai.agents.conflict_tools:CONFLICT_CLEANUP_TOOL",
    "raise_issue": "cai.agents.issue_tool:RAISE_ISSUE_TOOL",
    "traces_list": "cai.log.traces:TRACES_LIST_TOOL",
    "traces_show": "cai.log.traces:TRACES_SHOW_TOOL",
    "traces_failures": "cai.log.traces:TRACES_FAILURES_TOOL",
    "traces_session_cost": "cai.log.traces:TRACES_SESSION_COST_TOOL",
    "traces_session": "cai.log.traces:TRACES_SESSION_TOOL",
    "traces_solve_sessions": "cai.log.traces:TRACES_SOLVE_SESSIONS_TOOL",
    "git_log": "cai.tools.git_tools:GIT_LOG_TOOL",
    "git_diff": "cai.tools.git_tools:GIT_DIFF_TOOL",
    "git_blame": "cai.tools.git_tools:GIT_BLAME_TOOL",
    "git_show": "cai.tools.git_tools:GIT_SHOW_TOOL",
    "file_info": "cai.tools.file_tools:FILE_INFO_TOOL",
}

_DEEP_FLAG_DEFAULTS: dict[str, bool] = {
    flag: False for kwargs in TOOL_FLAGS.values() for flag in kwargs
}

# Common prompt fragments keyed by the ``common:`` frontmatter key.
# Each value is inserted verbatim after the title heading of the agent.
_COMMON_FRAGMENTS: dict[str, str] = {
    "anti_hallucination_guard": (
        "> **You do NOT have an `execute`, `bash`, `shell`, or `run` tool. "
        "You cannot run commands, tests, or scripts. "
        "Only the tools listed above are available to you.**\n"
        ">\n"
        "> **Parameter bleed warning:** Each tool accepts only its own documented "
        "parameters. Do not carry a parameter from one tool (e.g., `limit` from "
        "`read_file`) to another tool (e.g., `grep`). If a parameter isn't listed "
        "in the tool's documentation, it won't be accepted."
    ),
    "antipattern_examples": (
        "> **Anti-pattern examples:**\n"
        "> - **BAD:** `execute('git log')` or `bash('ls')` — you do not have these tools.\n"
        "> - **GOOD:** use `read_file`, `grep`, `glob`, or `ls` to discover what changed."
    ),
}

# Task-tool-note auto-injected when ``subagents`` appears in ``tools:``.
_TASK_TOOL_NOTE = (
    "**Important:** When calling the `task` tool, pass the subagent instructions "
    "as `description=`, not `prompt=`. The `task` tool has no `prompt` parameter."
)


def _inject_common_fragments(config: dict, instructions: str) -> str:
    """Inject common prompt fragments into instructions after the title heading.

    Resolves ``config['common']`` names against :data:`_COMMON_FRAGMENTS` and
    auto-includes the task-tool-note when ``subagents`` is listed in
    ``config['tools']``.  Fragments are inserted in order, each separated by
    a blank line, immediately after the first ``# Title`` line.
    """
    fragments: list[str] = []

    common_names = config.get("common", [])
    if isinstance(common_names, list):
        for name in common_names:
            if name in _COMMON_FRAGMENTS:
                fragments.append(_COMMON_FRAGMENTS[name])

    tools = config.get("tools", [])
    if isinstance(tools, list) and "subagents" in tools:
        fragments.append(_TASK_TOOL_NOTE)

    if not fragments:
        return instructions

    lines = instructions.splitlines()
    heading_idx = None
    for i, line in enumerate(lines):
        if line.startswith("# "):
            heading_idx = i
            break

    if heading_idx is None:
        return "\n\n".join(fragments) + "\n\n" + instructions

    insert_point = heading_idx + 1
    frag_text = "\n\n".join(fragments)
    result_lines = lines[:insert_point] + [""] + [frag_text] + [""] + lines[insert_point:]
    return "\n".join(result_lines)


def _import_factory(target: str) -> Any:
    """Resolve a ``"module.path:attr"`` string from :data:`TOOL_FACTORIES`."""
    module_path, _, attr = target.partition(":")
    if not attr:
        raise ValueError(f"factory target must be 'module:attr', got {target!r}")
    from importlib import import_module
    return getattr(import_module(module_path), attr)


def build_deep_agent_kwargs(config: dict) -> dict[str, Any]:
    """Translate the ``tools:`` list in agent frontmatter into ``create_deep_agent`` kwargs.

    Default surface is **off** for every toggleable toolset; an agent must
    opt in by listing the toolset name in its ``tools:`` frontmatter.
    Unknown names raise ``ValueError`` so a typo doesn't silently disable
    a toolset the agent expected.
    """
    requested = config.get("tools", [])
    if not isinstance(requested, list):
        raise ValueError(
            f"'tools' must be a list, got {type(requested).__name__}"
        )
    known = TOOL_FLAGS.keys() | TOOL_FACTORIES.keys()
    unknown = [t for t in requested if t not in known]
    if unknown:
        raise ValueError(
            f"unknown tool(s) {unknown!r} — must be one of {sorted(known)}"
        )
    requested_set = set(requested)
    for conflict in _TOOL_CONFLICTS:
        clash = conflict & requested_set
        if len(clash) > 1:
            raise ValueError(
                f"tools {sorted(clash)!r} cannot be combined — pick one"
            )
    flags = dict(_DEEP_FLAG_DEFAULTS)
    for name in requested:
        if name in TOOL_FLAGS:
            flags.update(TOOL_FLAGS[name])
    return flags


def _prune_toolsets(agent: Any, requested: list[str]) -> None:
    """Apply per-key prune rules from ``_TOOL_PRUNE`` to the live agent.

    Rules for the same ``toolset_id`` are *unioned* across requested keys
    before pruning — otherwise listing two narrow keys (e.g.
    ``filesystem_read`` and ``execute``) would have each prune wipe the
    other's tools, leaving the toolset empty.
    """
    keeps: dict[str, set[str]] = {}
    for key in requested:
        for toolset_id, keep in _TOOL_PRUNE.get(key, {}).items():
            keeps.setdefault(toolset_id, set()).update(keep)
    for toolset_id, keep in keeps.items():
        for ts in agent.toolsets:
            if getattr(ts, "id", None) == toolset_id:
                for name in list(ts.tools):
                    if name not in keep:
                        del ts.tools[name]


def _resolve_subagents(config: dict) -> list[dict[str, Any]]:
    """Build ``SubAgentConfig`` dicts for each entry in ``config['subagents']``.

    Each entry is the bare name of a sibling agent definition file
    (``<name>.md`` under ``AGENT_DIR``). The referenced agent is built
    via ``build_deep_agent`` and passed as a pre-built ``agent=`` so
    pydantic-deep doesn't re-create it through its default factory.
    """
    names = config.get("subagents", [])
    if not isinstance(names, list):
        raise ValueError(
            f"'subagents' must be a list, got {type(names).__name__}"
        )
    configs: list[dict[str, Any]] = []
    for name in names:
        sub_path = resolve_agent_path(name)
        sub_config, sub_instructions = parse_agent_md(sub_path)
        if "description" not in sub_config:
            raise ValueError(
                f"{sub_path}: subagent frontmatter missing required 'description'"
            )
        configs.append(
            {
                "name": sub_config["name"],
                "description": sub_config["description"],
                "instructions": sub_instructions,
                "agent": build_deep_agent(sub_config, sub_instructions),
            }
        )
    return configs


def build_deep_agent(
    config: dict,
    instructions: str,
    *,
    output_type: Any = None,
    **extra: Any,
) -> Any:
    """Build a deep agent from parsed frontmatter ``config``.

    Wires up name, model, toolset selection (from ``config['tools']``),
    and any subagents declared in ``config['subagents']``. Extra kwargs
    are passed through to ``create_deep_agent`` unchanged.
    """
    from pydantic_deep import create_deep_agent

    instructions = _inject_common_fragments(config, instructions)

    requested = list(config.get("tools", []))
    sub_configs = _resolve_subagents(config)
    if sub_configs and "subagents" not in extra:
        extra["subagents"] = sub_configs

    settings = build_model_settings(config)

    # Tell OpenRouter to only route to providers that support every parameter we
    # send (structured-output schema, tool_config, reasoning, etc.). Without this
    # a provider that silently ignores unknown parameters can receive the request,
    # produce a response that doesn't match the schema, and trigger output-
    # validation retries.
    settings = settings or {}
    existing_extra_body = settings.get("extra_body") or {}
    settings["extra_body"] = {"provider": {"require_parameters": True}, **existing_extra_body}

    # Google AI Studio requires tool_config.include_server_side_tool_invocations=True
    # whenever built-in tools (web search, grounding, thinking on pro models, etc.)
    # are combined with function calling. Set it unconditionally for all Google models
    # since the flag is harmless when no built-in tools are active.
    # See: https://ai.google.dev/api/generate-content#v1beta.ToolConfig
    if config.get("model", "").startswith("google/"):
        settings = settings or {}
        existing_extra_body = settings.get("extra_body") or {}
        settings["extra_body"] = {
            **existing_extra_body,
            "tool_config": {"include_server_side_tool_invocations": True},
        }

    if settings is not None and "model_settings" not in extra:
        extra["model_settings"] = settings

    factory_tools = [
        _import_factory(TOOL_FACTORIES[t]) for t in requested if t in TOOL_FACTORIES
    ]
    if factory_tools:
        extra["tools"] = [*(extra.get("tools") or []), *factory_tools]

    # str_replace beat hashline in production: hashline edits churned on
    # multi-edit responses because each applied edit shifted line numbers
    # and invalidated subsequent (line, hash) pairs (see commit c86189f).
    extra.setdefault("edit_format", "str_replace")

    # Tool implementations sometimes raise on bad model inputs (invalid
    # glob, malformed regex). Without this capability such a single-call
    # failure aborts the whole run.
    extra["capabilities"] = [
        *(extra.get("capabilities") or []),
        EditFileGuardrailAsRetry(),
        WriteFileGuardrailAsRetry(),
        UnknownToolRetry(),
        GlobPatternSanitizer(),
        ToolErrorAsRetry(),
        ModelRequestErrorAsRetry(),
        GrepGuardrailAsRetry(),
        ConsecutiveFailureGuardrail(),
        MicroReadGuardCapability(),
        HistoryCompactorCapability(),
    ]

    agent = create_deep_agent(
        build_model(config),
        name=config["name"],
        instructions=instructions,
        output_type=_wrap_output(output_type),
        **build_deep_agent_kwargs(config),
        **extra,
    )
    _prune_toolsets(agent, requested)
    return agent


def load_agent_from_md(
    path: str | Path,
    *,
    output_type: Any = None,
    tools: list | None = None,
    deps_type: Any = None,
) -> Agent:
    """Parse ``path`` and return a configured pydantic-ai ``Agent``.

    ``output_type``, ``tools``, and ``deps_type`` are passed through —
    they live in code (not YAML) because they reference Python types
    and callables.
    """
    config, instructions = parse_agent_md(path)
    factory_tools = [
        _import_factory(TOOL_FACTORIES[t])
        for t in config.get("tools", [])
        if t in TOOL_FACTORIES
    ]
    kwargs: dict = {
        "system_prompt": instructions,
        "name": config["name"],
        "output_type": _wrap_output(output_type),
        "tools": [*(tools or []), *factory_tools],
    }
    if deps_type is not None:
        kwargs["deps_type"] = deps_type
    settings = build_model_settings(config) or {}
    existing_extra_body = settings.get("extra_body") or {}
    settings["extra_body"] = {"provider": {"require_parameters": True}, **existing_extra_body}
    kwargs["model_settings"] = settings
    kwargs["capabilities"] = [
        EditFileGuardrailAsRetry(),
        WriteFileGuardrailAsRetry(),
        UnknownToolRetry(),
        GlobPatternSanitizer(),
        ToolErrorAsRetry(),
        ModelRequestErrorAsRetry(),
        GrepGuardrailAsRetry(),
        MicroReadGuardCapability(),
        HistoryCompactorCapability(),
    ]
    return Agent(build_model(config), **kwargs)
