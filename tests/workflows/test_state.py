from __future__ import annotations

from cai.workflows.state import DocsOutput, ImplementOutput, ThreadReply


# ---------------------------------------------------------------------------
# ImplementOutput — files_changed field
# ---------------------------------------------------------------------------


def test_implement_output_files_changed_defaults_to_empty_list():
    """files_changed defaults to an empty list when not provided."""
    output = ImplementOutput(
        summary="Implemented feature X.",
        commit_message="feat: implement feature X",
    )
    assert output.files_changed == []


def test_implement_output_files_changed_accepts_list_of_strings():
    """files_changed accepts and stores a list of repo-relative paths."""
    files = ["src/a.py", "src/b.py", "tests/test_a.py"]
    output = ImplementOutput(
        summary="Implemented feature X.",
        commit_message="feat: implement feature X",
        files_changed=files,
    )
    assert output.files_changed == files


def test_implement_output_files_changed_preserves_order():
    """files_changed preserves the order of paths as provided."""
    files = ["z.py", "a.py", "m.py"]
    output = ImplementOutput(
        summary="Implemented feature X.",
        commit_message="feat: implement feature X",
        files_changed=files,
    )
    assert output.files_changed == ["z.py", "a.py", "m.py"]


def test_implement_output_json_schema_includes_files_changed():
    """The JSON schema for ImplementOutput includes the files_changed field."""
    schema = ImplementOutput.model_json_schema()
    props = schema.get("properties", {})
    assert "files_changed" in props, (
        "files_changed must appear in the JSON schema properties"
    )
    assert props["files_changed"].get("title") == "Files Changed"
    assert props["files_changed"].get("type") == "array"


def test_implement_output_existing_fields_still_work():
    """Adding files_changed does not break existing fields like summary,
    commit_message, required_checks, or replies."""
    replies = [
        ThreadReply(
            thread_id="thread_1",
            action="fix",
            reply="Fixed the import issue.",
        ),
    ]
    output = ImplementOutput(
        summary="Fixed import issue.",
        commit_message="fix: resolve circular import",
        required_checks=["python"],
        replies=replies,
        files_changed=["src/module.py"],
    )
    assert output.summary == "Fixed import issue."
    assert output.commit_message == "fix: resolve circular import"
    assert output.required_checks == ["python"]
    assert len(output.replies) == 1
    assert output.replies[0].thread_id == "thread_1"
    assert output.files_changed == ["src/module.py"]


# ---------------------------------------------------------------------------
# DocsOutput — files_changed field
# ---------------------------------------------------------------------------


def test_docs_output_files_changed_defaults_to_empty_list():
    """files_changed defaults to an empty list when not provided."""
    output = DocsOutput(
        summary="Updated docs/cli.md to cover the new flag.",
        commit_message="docs: document --timeout flag in cli.md",
    )
    assert output.files_changed == []


def test_docs_output_files_changed_accepts_list_of_strings():
    """files_changed accepts and stores a list of repo-relative paths."""
    files = ["docs/cli.md", "docs/index.md"]
    output = DocsOutput(
        summary="Updated CLI and index docs.",
        commit_message="docs: update cli and index pages",
        files_changed=files,
    )
    assert output.files_changed == files


def test_docs_output_files_changed_preserves_order():
    """files_changed preserves the order of paths as provided."""
    files = ["docs/z.md", "docs/a.md", "docs/m.md"]
    output = DocsOutput(
        summary="Updated multiple docs pages.",
        commit_message="docs: update multiple pages",
        files_changed=files,
    )
    assert output.files_changed == ["docs/z.md", "docs/a.md", "docs/m.md"]


def test_docs_output_json_schema_includes_files_changed():
    """The JSON schema for DocsOutput includes the files_changed field."""
    schema = DocsOutput.model_json_schema()
    props = schema.get("properties", {})
    assert "files_changed" in props, (
        "files_changed must appear in the JSON schema properties"
    )
    assert props["files_changed"].get("title") == "Files Changed"
    assert props["files_changed"].get("type") == "array"


def test_docs_output_existing_fields_still_work():
    """Adding files_changed does not break existing fields like summary or
    commit_message."""
    output = DocsOutput(
        summary="Updated docs/cli.md to cover the new --timeout flag.",
        commit_message="docs: document --timeout flag in cli.md",
        files_changed=["docs/cli.md"],
    )
    assert output.summary == (
        "Updated docs/cli.md to cover the new --timeout flag."
    )
    assert output.commit_message == "docs: document --timeout flag in cli.md"
    assert output.files_changed == ["docs/cli.md"]
