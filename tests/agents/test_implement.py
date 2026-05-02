import pytest
from cai.agents.loader import parse_agent_md, resolve_agent_path


def test_implement_agent_config():
    implement_file = resolve_agent_path("implement")
    assert implement_file.exists(), "implement.md must exist in AGENT_DIR"
    config, instructions = parse_agent_md(implement_file)

    # Assert basics
    assert config["name"] == "implement"
    assert config["model"] == "deepseek/deepseek-v4-pro"

    # Assert expected tools
    tools = config.get("tools", [])
    assert "filesystem" in tools
    assert "web_search" in tools
    assert "web_fetch" in tools
    assert "spike_run" in tools

    # Assert instructions
    assert "web_search" in instructions
    assert "web_fetch" in instructions
    assert "API documentation" in instructions

    # Assert specific rules
    assert (
        r"Do not run repository-wide global searches (like \`grep\` or \`glob\`)"
    ) in instructions
    assert "post-refactor to verify changes" in instructions
    assert "Targeted verification via `spike_run`" in instructions

    # Assert re-read-after-edit guidance is present (issue #1525)
    assert (
        "re-read it before constructing `old_string`"
        in instructions
    ), "Prompt must warn that reference files diverge from disk after edits"
    assert (
        "construct `old_string` from the fresh read, not from memory or the initial snapshot"
        in instructions
    ), "Prompt must instruct agent to build old_string from a fresh re-read"
    assert (
        "re-read the file before each new batch"
        in instructions
    ), "Prompt must instruct re-reading between multi-response edit batches"

    # Assert spike_run verification guidance (issue #1639)
    assert (
        "For code verification (import checks, syntax validation, targeted tests), use `spike_run`"
        in instructions
    ), "Warning block must direct agent to spike_run for code verification"

    # Assert anti-hallucination blockquote restored (issue #1639)
    assert (
        "`run` tool" in instructions
    ), "Anti-hallucination blockquote must list `run` among forbidden tools"
    assert (
        "You cannot run commands, tests, or scripts. Only the tools listed above are available to you."
        in instructions
    ), "Anti-hallucination blockquote must include the restored 'cannot run commands' phrasing"

    # Assert original BAD→GOOD anti-pattern pair is present and contiguous
    # before the spike_run-specific pair (issue #1639)
    assert (
        "`execute('git log')` or `bash('ls')`"
        in instructions
    ), "Anti-pattern examples must include the original BAD example for execute/bash"
    assert (
        "use `read_file`, `grep`, `glob`, or `ls` to discover what changed"
        in instructions
    ), "Anti-pattern examples must include the original GOOD example using read-only tools"
    assert (
        "re-reading a file to verify an edit"
        in instructions
    ), "Anti-pattern example must warn against re-reading for verification"
    assert (
        "use `spike_run` to verify edits"
        in instructions
    ), "Must include a GOOD example using spike_run for verification"

    # Assert Verification with `spike_run` subsection (issue #1639)
    assert (
        "Verification with `spike_run`"
        in instructions
    ), "Must include a Verification with spike_run subsection"
    assert (
        "import sys; sys.path.insert(0, '../repo'); import mymodule"
        in instructions
    ), "Must show import verification pattern using spike_run"
    assert (
        "import py_compile; py_compile.compile"
        in instructions
    ), "Must show syntax validation pattern using spike_run"
    assert (
        "pytest"
        in instructions
    ), "Must show targeted test pattern using spike_run"
    assert (
        "Keep scripts short"
        in instructions
    ), "Must instruct agent to keep spike_run scripts short"
    assert (
        "Prefer one `spike_run` verification over a `read_file` + LLM reasoning cycle"
        in instructions
    ), "Must encourage spike_run over read+LLM verification cycle"

    # Assert softened global-search guideline (issue #1639)
    assert (
        "Assume your targeted edits worked"
        in instructions
    ), "Guideline must tell agent to assume targeted edits worked"

    # Assert trust-successful-edits guidance (issue #1602)
    assert (
        "Trust successful edits"
        in instructions
    ), "Prompt must include 'Trust successful edits' heading"
    assert (
        "do not need to re-read the file to verify the edit succeeded"
        in instructions
    ), "Prompt must tell agent not to re-read just to confirm a successful edit"
    assert (
        "reuse your previous `read_file` output"
        in instructions
    ), "Prompt must mention reusing previous read_file output when HistoryCompactor fires"


def test_implement_prompt_includes_avoid_rereading_guidance():
    """Verify implement.md contains check-conversation-history guidance."""
    implement_file = resolve_agent_path("implement")
    _, instructions = parse_agent_md(implement_file)

    assert "**Check conversation history before re-reading:**" in instructions, (
        "implement.md must contain 'Check conversation history before re-reading' guidance"
    )
    assert (
        "before calling `read_file`, check whether you've already read that file"
        in instructions
    ), "implement.md must instruct agent to check conversation history before read_file"
    assert "the full content is still in your conversation history" in instructions
    assert "Only re-read when the file may have been modified by a prior edit" in instructions
