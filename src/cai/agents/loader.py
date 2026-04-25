"""Build pydantic-ai Agents from Claude-Code-style ``.md`` definition files.

Each file starts with a YAML frontmatter block delimited by ``---`` lines,
followed by the markdown that becomes the agent's system prompt::

    ---
    name: cai-refine
    model: opus
    ---

    # Refinement Agent
    ...
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from anthropic import AsyncAnthropic
from pydantic_ai import Agent
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.providers.anthropic import AnthropicProvider

# Map the short names used in agent frontmatter to concrete Anthropic model IDs.
_MODEL_IDS: dict[str, str] = {
    "opus": "claude-opus-4-7",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5-20251001",
}

DEFAULT_MODEL = "sonnet"
VALID_MODELS = set(_MODEL_IDS)

AGENT_DIR = Path(__file__).resolve().parent


@lru_cache(maxsize=None)
def _provider() -> AnthropicProvider:
    """Build an AnthropicProvider, preferring a Claude Code OAuth token.

    Set ``CLAUDE_CODE_OAUTH_TOKEN`` (mint via ``claude setup-token``) to
    bill against your Max plan. Otherwise the SDK falls back to
    ``ANTHROPIC_API_KEY`` env for paid API metering. Cached so all
    agents in the process share one HTTP client.
    """
    oauth = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if oauth:
        return AnthropicProvider(anthropic_client=AsyncAnthropic(auth_token=oauth))
    return AnthropicProvider()


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


def build_model(config: dict) -> AnthropicModel:
    """Build a pydantic-ai ``AnthropicModel`` from frontmatter ``config``.

    Model selection comes from the ``model`` frontmatter key — one of
    ``sonnet``, ``opus``, ``haiku``, mapped to concrete Anthropic model
    IDs. Auth is handled by ``_provider()`` (OAuth token or API key).
    """
    name = config.get("model", DEFAULT_MODEL)
    if name not in _MODEL_IDS:
        raise ValueError(
            f"invalid model {name!r} — must be one of {sorted(_MODEL_IDS)}"
        )
    return AnthropicModel(_MODEL_IDS[name], provider=_provider())


# Maps a ``tools:`` frontmatter entry to the ``create_deep_agent`` flags it enables.
# Anything not listed in an agent's ``tools:`` is forced off — the full surface is
# explicit, so adding a new toolset to pydantic-deep won't silently leak in.
TOOL_FLAGS: dict[str, dict[str, Any]] = {
    "filesystem": {"include_filesystem": True},
    "filesystem_read": {"include_filesystem": True},
    "todo": {"include_todo": True},
    "subagents": {
        "include_subagents": True,
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
    frozenset({"filesystem", "filesystem_read"}),
]

# Pruning rules applied after ``create_deep_agent`` for tool keys that share
# a backing toolset with another, broader key. Each entry is keyed on the
# tool name in ``tools:`` and maps a toolset id to the set of tool names to
# keep on that toolset.
_TOOL_PRUNE: dict[str, dict[str, frozenset[str]]] = {
    "filesystem_read": {
        "deep-console": frozenset({"read_file", "ls", "glob", "grep"}),
    },
}

_DEEP_FLAG_DEFAULTS: dict[str, bool] = {
    flag: False for kwargs in TOOL_FLAGS.values() for flag in kwargs
}


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
    unknown = [t for t in requested if t not in TOOL_FLAGS]
    if unknown:
        raise ValueError(
            f"unknown tool(s) {unknown!r} — must be one of {sorted(TOOL_FLAGS)}"
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
        flags.update(TOOL_FLAGS[name])
    return flags


def _prune_toolsets(agent: Any, requested: list[str]) -> None:
    """Apply per-key prune rules from ``_TOOL_PRUNE`` to the live agent."""
    for key in requested:
        for toolset_id, keep in _TOOL_PRUNE.get(key, {}).items():
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
    }
    if deps_type is not None:
        kwargs["deps_type"] = deps_type
    return Agent(build_model(config), **kwargs)
