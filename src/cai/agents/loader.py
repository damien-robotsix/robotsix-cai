"""Build pydantic-ai Agents from Claude-Code-style ``.md`` definition files.

Each file starts with a YAML frontmatter block delimited by ``---`` lines,
followed by the markdown that becomes the agent's system prompt::

    ---
    name: cai-refine
    model: anthropic/claude-sonnet-4-6
    ---

    # Refinement Agent
    ...
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import httpx
import yaml
from openai import AsyncOpenAI
from pydantic_ai import Agent
from pydantic_ai.capabilities.abstract import AbstractCapability
from pydantic_ai.exceptions import ModelRetry, UnexpectedModelBehavior
from pydantic_ai.models.openai import OpenAIModel
from pydantic_ai.providers.openai import OpenAIProvider

AGENT_DIR = Path(__file__).resolve().parent


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
        self, ctx, *, call, tool_def, args, error
    ):
        if isinstance(error, ModelRetry):
            raise error
        raise ModelRetry(
            f"Tool {call.tool_name!r} raised {type(error).__name__}: {error}. "
            f"Adjust the arguments and try again."
        )


class ModelRequestErrorAsRetry(AbstractCapability):
    """Turn ``UnexpectedModelBehavior`` into ``ModelRetry`` for transient API errors.

    OpenRouter occasionally returns a malformed HTTP 200 (all-None fields in the
    ChatCompletion schema) on preview models, especially on the final structured-
    output call. Without this, one bad response aborts the entire agent run.
    pydantic_ai's built-in request-level retry budget (``request_limit``) still
    caps total retries, so this does not loop indefinitely.
    """

    async def on_model_request_error(self, ctx, *, request_context, error):
        if isinstance(error, UnexpectedModelBehavior):
            raise ModelRetry(
                f"Model returned an unexpected response ({error}). Retrying..."
            )
        raise error


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
def _provider() -> OpenAIProvider:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY is not set")

    client = httpx.AsyncClient(
        timeout=_HTTP_TIMEOUT,
        event_hooks={"response": [_capture_openrouter_cost]},
    )

    return OpenAIProvider(
        openai_client=AsyncOpenAI(
            api_key=key,
            base_url="https://openrouter.ai/api/v1",
            max_retries=_MAX_RETRIES,
            http_client=client,
        )
    )


def parse_agent_md(path: str | Path) -> tuple[dict, str]:
    """Return ``(config_dict, system_prompt_text)`` from a frontmatter ``.md`` file."""
    text = Path(path).read_text()
    if not text.startswith("---"):
        raise ValueError(f"{path}: missing YAML frontmatter")
    parts = text.split("---", 2)
    if len(parts) != 3:
        raise ValueError(f"{path}: malformed frontmatter (expected two '---' delimiters)")
    config = yaml.safe_load(parts[1]) or {}
    if "name" not in config:
        raise ValueError(f"{path}: frontmatter missing required 'name' field")
    return config, parts[2].strip()


def build_model(config: dict) -> OpenAIModel:
    """Build a pydantic-ai model from the ``model`` frontmatter key.

    The value is the full OpenRouter model ID, e.g. ``anthropic/claude-sonnet-4-6``
    or ``google/gemini-flash-1.5``.
    """
    model_id = config.get("model")
    if not model_id:
        raise ValueError("frontmatter missing required 'model' field")
    return OpenAIModel(model_id, provider=_provider())


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
    "web_search": {"web_search": True},
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
    "traces_list": "cai.log.traces:TRACES_LIST_TOOL",
    "traces_show": "cai.log.traces:TRACES_SHOW_TOOL",
    "traces_failures": "cai.log.traces:TRACES_FAILURES_TOOL",
    "traces_session_cost": "cai.log.traces:TRACES_SESSION_COST_TOOL",
    "traces_session": "cai.log.traces:TRACES_SESSION_TOOL",
}

_DEEP_FLAG_DEFAULTS: dict[str, bool] = {
    flag: False for kwargs in TOOL_FLAGS.values() for flag in kwargs
}


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
        sub_path = AGENT_DIR / f"{name}.md"
        if not sub_path.exists():
            raise FileNotFoundError(f"subagent definition not found: {sub_path}")
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

    requested = list(config.get("tools", []))
    sub_configs = _resolve_subagents(config)
    if sub_configs and "subagents" not in extra:
        extra["subagents"] = sub_configs

    settings = build_model_settings(config)
    if settings is not None and "model_settings" not in extra:
        extra["model_settings"] = settings

    factory_tools = [
        _import_factory(TOOL_FACTORIES[t]) for t in requested if t in TOOL_FACTORIES
    ]
    if factory_tools:
        extra["tools"] = [*(extra.get("tools") or []), *factory_tools]

    # 'exhaustive' so a model that emits a side-effect tool call (e.g.
    # write_file) in the same assistant turn as final_result still has the
    # side-effect executed. The pydantic-ai default 'early' silently stubs
    # those calls with "Tool not executed - a final result was already
    # processed", which lost the refined body in cai-solve.
    extra.setdefault("end_strategy", "exhaustive")

    # str_replace beat hashline in production: hashline edits churned on
    # multi-edit responses because each applied edit shifted line numbers
    # and invalidated subsequent (line, hash) pairs (see commit c86189f).
    extra.setdefault("edit_format", "str_replace")

    # Tool implementations sometimes raise on bad model inputs (invalid
    # glob, malformed regex). Without this capability such a single-call
    # failure aborts the whole run.
    extra["capabilities"] = [*(extra.get("capabilities") or []), ToolErrorAsRetry(), ModelRequestErrorAsRetry()]

    agent = create_deep_agent(
        build_model(config),
        name=config["name"],
        instructions=instructions,
        output_type=output_type,
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
    kwargs: dict = {
        "system_prompt": instructions,
        "name": config["name"],
        "output_type": output_type,
        "tools": tools or [],
        # See build_deep_agent: 'exhaustive' so side-effect tool calls
        # bundled in the final-result turn still execute.
        "end_strategy": "exhaustive",
    }
    if deps_type is not None:
        kwargs["deps_type"] = deps_type
    settings = build_model_settings(config)
    if settings is not None:
        kwargs["model_settings"] = settings
    return Agent(build_model(config), **kwargs)
