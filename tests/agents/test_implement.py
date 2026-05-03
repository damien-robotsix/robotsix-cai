from cai.agents.loader import parse_agent_md, resolve_agent_path, build_deep_agent


def test_implement_agent_config(monkeypatch):
    implement_file = resolve_agent_path("implement")
    assert implement_file.exists(), "implement.md must exist in AGENT_DIR"
    config, instructions = parse_agent_md(implement_file)

    # Assert basics
    assert config["name"] == "implement"
    assert config["model"] == "deepseek/deepseek-v4-pro"
    # Intentionally no max_tokens: pydantic_ai sends `max_completion_tokens`
    # on the wire, which OpenRouter rejects with 404 under
    # provider.require_parameters=True (no deepseek-v4-pro provider declares
    # support for that field). See revert in this commit.
    assert "max_tokens" not in config

    # Assert expected tools
    tools = config.get("tools", [])
    assert "filesystem" in tools
    assert "web_search" in tools
    assert "web_fetch" in tools
    assert "spike_run" in tools
    assert "block_edit" in tools

    # Build merged output for common-fragment checks.
    captured_instructions = []

    def fake_create(model, *, name, instructions, **kwargs):
        captured_instructions.append(instructions)
        return object()

    monkeypatch.setattr("pydantic_deep.create_deep_agent", fake_create)
    monkeypatch.setattr("cai.agents.loader._resolve_subagents", lambda c: [])
    monkeypatch.setattr("cai.agents.loader.build_model", lambda c: None)
    monkeypatch.setattr("cai.agents.loader.build_deep_agent_kwargs", lambda c: {})
    monkeypatch.setattr("cai.agents.loader._prune_toolsets", lambda a, r: None)
    build_deep_agent(config, instructions)
    assert captured_instructions, "build_deep_agent did not call create_deep_agent"
    merged = captured_instructions[0]

    # Assert anti-hallucination blockquote via common: injection (issue #1639)
    assert (
        "`run` tool" in merged
    ), "Anti-hallucination blockquote must list `run` among forbidden tools"
    assert (
        "You cannot run commands, tests, or scripts. Only the tools listed above are available to you."
        in merged
    ), "Anti-hallucination blockquote must include the restored 'cannot run commands' phrasing"

    # Assert original BAD→GOOD anti-pattern pair via common: injection (issue #1639)
    assert (
        "`execute('git log')` or `bash('ls')`"
        in merged
    ), "Anti-pattern examples must include the original BAD example for execute/bash"
    assert (
        "use `read_file`, `grep`, `glob`, or `ls` to discover what changed"
        in merged
    ), "Anti-pattern examples must include the original GOOD example using read-only tools"

    # Assert instructions (raw body)
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

    # Assert agent-specific anti-pattern examples (spike_run-specific pair) still in body
    assert (
        "re-reading a file to verify an edit"
        in instructions
    ), "Anti-pattern example must warn against re-reading for verification"
    assert (
        "use `spike_run` to verify edits"
        in instructions
    ), "Must include a GOOD example using spike_run for verification"

    # Assert import-name verification anti-pattern pair (issue #1703)
    assert (
        "importing a class or function using the name from the plan text without verifying it exists"
        in instructions
    ), "Must include BAD example warning against importing unverified names from plan"
    assert (
        "before writing an import, verify the exact identifier by reading the module source"
        in instructions
    ), "Must include GOOD example directing agent to verify identifiers against source"
    assert (
        "trust the source, not the plan"
        in instructions
    ), "Must instruct agent to trust source over plan when they disagree"

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

    # Assert trust-successful-edits guidance (issue #1602)
    assert (
        "reuse your previous `read_file` output"
        in instructions
    ), "Prompt must mention reusing previous read_file output when HistoryCompactor fires"

    # Assert explicit edit_file preference and write_file threshold (issue #1700)
    assert (
        "Prefer `edit_file` for targeted changes"
        in instructions
    ), "Prompt must include explicit edit_file preference"
    assert (
        "write_file` only when creating new files or rewriting more than 50%"
        in instructions
    ), "Prompt must include concrete write_file threshold (>50% of lines)"
    assert (
        "2-line fix in a 270-line file should use `edit_file`"
        in instructions
    ), "Prompt must include concrete anti-example for write_file on small fixes"
    assert (
        "bloats conversation context"
        in instructions
    ), "Prompt must explain the context-bloat cost of write_file"
    assert (
        "When in doubt, choose `edit_file`"
        in instructions
    ), "Prompt must close with a decisive edit_file default"


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
