"""Unit tests for parse_agent_md skills:/commands: -> tools synthesis.

Tests the new split frontmatter format introduced in issue #1804, where
the monolithic ``tools:`` key was replaced by ``skills:`` and ``commands:``
in six agent definition files.  ``parse_agent_md`` synthesises a unified
``tools`` key from ``skills + commands`` when the legacy key is absent.
"""

from pathlib import Path

from cai.agents.loader import parse_agent_md


def _write_md(tmp_path: Path, frontmatter: str, body: str = "Test body.\n") -> Path:
    """Write a temporary .md file with YAML frontmatter and return the path."""
    md = tmp_path / "test_agent.md"
    md.write_text(f"---\n{frontmatter}---\n\n{body}")
    return md


# ---------------------------------------------------------------------------
# skills:/commands: -> tools synthesis
# ---------------------------------------------------------------------------


def test_synthesizes_tools_from_skills_and_commands(tmp_path):
    """When ``tools:`` is absent but ``skills:`` and ``commands:`` are
    present, ``parse_agent_md`` produces a config dict with a unified
    ``tools`` key built from ``skills + commands``."""
    md = _write_md(tmp_path,
        "name: test-agent\n"
        "model: test-model\n"
        "skills:\n"
        "  - filesystem_read\n"
        "  - subagents\n"
        "commands:\n"
        "  - raise_issue\n"
    )
    config, _ = parse_agent_md(md)
    assert "tools" in config, "Expected a synthesized 'tools' key"
    assert config["tools"] == ["filesystem_read", "subagents", "raise_issue"], (
        f"Expected tools=['filesystem_read', 'subagents', 'raise_issue'], "
        f"got {config['tools']!r}"
    )


def test_synthesizes_tools_from_skills_only(tmp_path):
    """When only ``skills:`` is present (no ``commands:``), the
    synthesised ``tools`` equals the skills list."""
    md = _write_md(tmp_path,
        "name: test-agent\n"
        "model: test-model\n"
        "skills:\n"
        "  - web_search\n"
        "  - web_fetch\n"
    )
    config, _ = parse_agent_md(md)
    assert config["tools"] == ["web_search", "web_fetch"]


def test_synthesizes_tools_from_commands_only(tmp_path):
    """When only ``commands:`` is present (no ``skills:``), the
    synthesised ``tools`` equals the commands list."""
    md = _write_md(tmp_path,
        "name: test-agent\n"
        "model: test-model\n"
        "commands:\n"
        "  - spike_run\n"
        "  - raise_issue\n"
    )
    config, _ = parse_agent_md(md)
    assert config["tools"] == ["spike_run", "raise_issue"]


# ---------------------------------------------------------------------------
# Legacy tools: key preservation
# ---------------------------------------------------------------------------


def test_legacy_tools_key_unchanged(tmp_path):
    """When the frontmatter contains only the legacy ``tools:`` key (no
    ``skills:`` or ``commands:``), it is passed through unmodified."""
    md = _write_md(tmp_path,
        "name: test-agent\n"
        "model: test-model\n"
        "tools:\n"
        "  - filesystem_read\n"
        "  - subagents\n"
        "  - raise_issue\n"
    )
    config, _ = parse_agent_md(md)
    assert config["tools"] == ["filesystem_read", "subagents", "raise_issue"]


def test_tools_key_takes_precedence_over_skills(tmp_path):
    """When both ``tools:`` and ``skills:`` are present, the ``tools:``
    value is preserved and no synthesis occurs."""
    md = _write_md(tmp_path,
        "name: test-agent\n"
        "model: test-model\n"
        "tools:\n"
        "  - filesystem_read\n"
        "skills:\n"
        "  - subagents\n"
        "commands:\n"
        "  - raise_issue\n"
    )
    config, _ = parse_agent_md(md)
    # ``tools:`` value is preserved, not overwritten by skills+commands
    assert config["tools"] == ["filesystem_read"], (
        f"Expected ['filesystem_read'] (legacy key), got {config['tools']!r}"
    )


# ---------------------------------------------------------------------------
# Edge cases: missing/empty keys
# ---------------------------------------------------------------------------


def test_no_synthesis_without_skills_or_commands(tmp_path):
    """When neither ``tools:``, ``skills:``, nor ``commands:`` is
    present, no ``tools`` key is added to the returned config dict."""
    md = _write_md(tmp_path,
        "name: test-agent\n"
        "model: test-model\n"
    )
    config, _ = parse_agent_md(md)
    assert "tools" not in config, (
        f"Expected no 'tools' key, got {config.get('tools')!r}"
    )


def test_no_synthesis_when_skills_is_empty(tmp_path):
    """When ``skills:`` and ``commands:`` are both empty lists, no
    ``tools`` key is synthesised (both lists contribute nothing)."""
    md = _write_md(tmp_path,
        "name: test-agent\n"
        "model: test-model\n"
        "skills:\n"
        "commands:\n"
    )
    config, _ = parse_agent_md(md)
    assert "tools" not in config


def test_synthesizes_tools_when_skills_is_none_and_commands_present(tmp_path):
    """When ``skills:`` is null (``None``) but ``commands:`` has items,
    synthesis still happens using the commands list only."""
    md = _write_md(tmp_path,
        "name: test-agent\n"
        "model: test-model\n"
        "skills:\n"
        "commands:\n"
        "  - raise_issue\n"
    )
    config, _ = parse_agent_md(md)
    assert config["tools"] == ["raise_issue"], (
        f"Expected ['raise_issue'] (commands only), got {config['tools']!r}"
    )


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------


def test_synthesized_tools_ordering(tmp_path):
    """The synthesised ``tools`` list preserves the original ordering
    of skills followed by commands."""
    md = _write_md(tmp_path,
        "name: test-agent\n"
        "model: test-model\n"
        "skills:\n"
        "  - aaa\n"
        "  - bbb\n"
        "commands:\n"
        "  - ccc\n"
        "  - ddd\n"
    )
    config, _ = parse_agent_md(md)
    assert config["tools"] == ["aaa", "bbb", "ccc", "ddd"], (
        f"Expected ['aaa', 'bbb', 'ccc', 'ddd'], got {config['tools']!r}"
    )
