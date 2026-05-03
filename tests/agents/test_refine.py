from cai.agents.loader import parse_agent_md, resolve_agent_path

def test_refine_agent_config():
    refine_file = resolve_agent_path("refine")
    assert refine_file.exists(), "refine.md must exist in AGENT_DIR"
    config, instructions = parse_agent_md(refine_file)
    
    # Assert basics
    assert config["name"] == "refine"
    assert config["model"] == "deepseek/deepseek-v4-pro"
    # Intentionally no max_tokens: pydantic_ai sends `max_completion_tokens`
    # on the wire, which OpenRouter rejects with 404 under
    # provider.require_parameters=True (no deepseek-v4-pro provider declares
    # support for that field). See revert in this commit.
    assert "max_tokens" not in config
    
    # Assert expected tools
    tools = config.get("tools", [])
    assert "filesystem" in tools
    assert "subagents" in tools
    assert "web_search" in tools
    assert "web_fetch" in tools
    assert "traces_list" in tools
    assert "traces_show" in tools
    assert "traces_failures" in tools
    assert "traces_session" in tools
    assert "traces_solve_sessions" in tools
    assert "context_manager" in tools
    assert "history_archive" in tools
    assert "spike_run" in tools
    
    # Assert subagents
    subagents = config.get("subagents", [])
    assert "explore" in subagents
    assert "spike" in subagents
    assert "trace_analyst" in subagents
    
    # Assert instructions reference trace tools and subagent
    assert "web_search" in instructions
    assert "web_fetch" in instructions
    assert "trace_analyst" in instructions
    assert "traces_session" in instructions
    assert "traces_solve_sessions" in instructions
    
    # Assert verification template updates
    assert "grep for Y" not in instructions
    assert "exact subheading names, ordering, and presence" in instructions

    # Assert context management instructions
    assert "## Context management" in instructions
    assert "Write intermediate research findings" in instructions
    assert "context_manager" in instructions
    assert "history_archive" in instructions


def test_refine_prompt_includes_minimize_delegation_section():
    """Verify refine.md contains the '## Minimize delegation' section."""
    refine_file = resolve_agent_path("refine")
    _, instructions = parse_agent_md(refine_file)

    assert "## Minimize delegation" in instructions, (
        "refine.md must contain '## Minimize delegation' section"
    )


def test_refine_prompt_includes_spike_run_as_direct_tool():
    """Verify refine.md describes spike_run as a direct tool (not a subagent)
    in the 'Choosing a subagent' section."""
    refine_file = resolve_agent_path("refine")
    _, instructions = parse_agent_md(refine_file)

    assert "**spike_run** is a **direct tool** (not a subagent)" in instructions, (
        "refine.md must describe spike_run as a direct tool in 'Choosing a subagent'"
    )
    assert "Prefer direct `spike_run` over delegating to the spike subagent" in instructions, (
        "refine.md must instruct preferring direct spike_run over spike subagent"
    )


def test_refine_prompt_includes_use_direct_spike_run_in_minimize_delegation():
    """The Minimize delegation section instructs using direct spike_run
    for simple runtime facts."""
    refine_file = resolve_agent_path("refine")
    _, instructions = parse_agent_md(refine_file)

    assert "**Use direct `spike_run` for simple runtime facts**" in instructions, (
        "refine.md Minimize delegation must recommend direct spike_run for simple facts"
    )
    assert (
        "one-liner import checks, return-type inspections, and exception-class probes"
        in instructions
    ), (
        "refine.md Minimize delegation must list examples of simple spike_run uses"
    )


def test_refine_prompt_includes_read_files_directly_in_minimize_delegation():
    """The Minimize delegation section instructs using read_file/grep/glob/ls
    directly rather than delegating to explore subagent."""
    refine_file = resolve_agent_path("refine")
    _, instructions = parse_agent_md(refine_file)

    assert "**Read files directly**" in instructions, (
        "refine.md Minimize delegation must recommend reading files directly"
    )


def test_refine_prompt_includes_batch_related_questions_in_minimize_delegation():
    """The Minimize delegation section instructs batching related questions
    when delegation is truly needed."""
    refine_file = resolve_agent_path("refine")
    _, instructions = parse_agent_md(refine_file)

    assert "**Batch related questions**" in instructions, (
        "refine.md Minimize delegation must include batch related questions guidance"
    )
    assert "combine related questions into a single sub-agent call" in instructions


def test_refine_prompt_stay_in_your_lane_mentions_spike_run():
    """The 'Stay in your lane' section mentions using spike_run for verification
    rather than editing repo files."""
    refine_file = resolve_agent_path("refine")
    _, instructions = parse_agent_md(refine_file)

    assert "## Stay in your lane" in instructions
    assert "spike_run" in instructions.split("## Stay in your lane")[1].split("## ")[0], (
        "refine.md 'Stay in your lane' section must mention spike_run as the "
        "verification alternative to write_file/edit_file"
    )
    assert (
        "do **not** call `write_file`/`edit_file` on anything under `repo/`"
        in instructions
    ), "refine.md 'Stay in your lane' must forbid write_file/edit_file on repo files"


def test_refine_prompt_relaxed_body_format():
    """Verify the body format section requires only ## Refined Issue,
    a description section, and a plan section — no rigid subheading
    list, exact names left to the model's judgment."""
    refine_file = resolve_agent_path("refine")
    _, instructions = parse_agent_md(refine_file)

    assert "## Body format" in instructions
    assert "`## Refined Issue`" in instructions, (
        "Body must start with ## Refined Issue"
    )
    assert "must contain at minimum a description section and a plan section" in instructions, (
        "Only description and plan are required sections"
    )
    assert "exact subheading names, ordering, and presence" in instructions, (
        "Subheading names should not be rigidly prescribed"
    )
    assert "left to your judgment" in instructions, (
        "Model should have discretion over section headings"
    )


def test_refine_prompt_includes_avoid_rereading_guidance():
    """Verify refine.md contains avoid-re-reading-files guidance."""
    refine_file = resolve_agent_path("refine")
    _, instructions = parse_agent_md(refine_file)

    assert "**Avoid re-reading files you've already read.**" in instructions, (
        "refine.md must contain 'Avoid re-reading files you've already read' guidance"
    )
    assert "Before calling `read_file` yourself" in instructions


def test_refine_prompt_softened_scope_guardrails_guideline():
    """The Files-to-change-vs-Scope-guardrails guideline uses conditional wording."""
    refine_file = resolve_agent_path("refine")
    config, instructions = parse_agent_md(refine_file)

    assert (
        "Files to change vs Scope guardrails are disjoint" in instructions
    ), "The guideline header must still be present"
    assert (
        "If both" in instructions and "are present" in instructions
    ), "The guideline must use conditional ('If both ... are present') wording"
    assert (
        "a path should appear in only one" in instructions
    ), "The softened rule must still say paths should not overlap"

