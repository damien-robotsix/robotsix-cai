"""Build pydantic-ai Agents from Claude-Code-style ``.md`` definition files.

Each file starts with a YAML frontmatter block delimited by ``---`` lines,
followed by the markdown that becomes the agent's system prompt::

    ---
    name: cai-refine
    model: claude-opus-4-7
    ---

    # Refinement Agent
    ...
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from claude_code_model import ClaudeCodeModel
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.settings import ModelSettings

DEFAULT_MODEL = "sonnet"
VALID_MODELS = {"sonnet", "opus", "haiku"}
DEFAULT_TIMEOUT = 300  # claude-code-model defaults to 30s — too tight for deep-agent runs.


class _NamedClaudeCodeModel(ClaudeCodeModel):
    """ClaudeCodeModel that stamps ``model_name`` on every response.

    Upstream returns ``ModelResponse`` with ``model_name=None``, which makes
    pydantic-ai's instrumentation log a warning per call ("Model name is
    required to calculate price") and leaves Langfuse traces without a
    model identifier. We fill it in from ``self.model_name``.
    """

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        response = await super().request(messages, model_settings, model_request_parameters)
        response.model_name = self.model_name
        return response

AGENT_DIR = Path(__file__).resolve().parent


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


def build_model(config: dict) -> ClaudeCodeModel:
    """Build a pydantic-ai ``Model`` that routes through the local Claude Code CLI.

    Model selection comes from the ``model`` frontmatter key — one of
    ``sonnet``, ``opus``, ``haiku``. Routing through the CLI uses the
    user's logged-in subscription quota; no ``ANTHROPIC_API_KEY`` is
    required.
    """
    name = config.get("model", DEFAULT_MODEL)
    if name not in VALID_MODELS:
        raise ValueError(
            f"invalid model {name!r} — must be one of {sorted(VALID_MODELS)}"
        )
    return _NamedClaudeCodeModel(model=name, timeout=int(config.get("timeout", DEFAULT_TIMEOUT)))


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
