"""Tests for multi-step issue helpers in cai.py."""
import sys
import os
import unittest

# Ensure the repo root is on the import path.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cai_lib import _parse_decomposition


class TestParseDecomposition(unittest.TestCase):
    """Tests for _parse_decomposition."""

    def test_well_formed_two_steps(self):
        text = (
            "Some preamble text.\n\n"
            "## Multi-Step Decomposition\n\n"
            "### Step 1: Add schema migration\n"
            "### Problem\n"
            "Need to add a new column.\n\n"
            "### Plan\n"
            "1. Create migration file\n"
            "2. Run migrate\n\n"
            "### Step 2: Update API endpoints\n"
            "### Problem\n"
            "API needs to expose the new field.\n\n"
            "### Plan\n"
            "1. Add field to serializer\n"
        )
        steps = _parse_decomposition(text)
        self.assertEqual(len(steps), 2)
        self.assertEqual(steps[0]["step"], 1)
        self.assertEqual(steps[0]["title"], "Add schema migration")
        self.assertIn("new column", steps[0]["body"])
        self.assertEqual(steps[1]["step"], 2)
        self.assertEqual(steps[1]["title"], "Update API endpoints")
        self.assertIn("serializer", steps[1]["body"])

    def test_three_steps(self):
        text = (
            "## Multi-Step Decomposition\n\n"
            "### Step 1: First\n"
            "Body one.\n\n"
            "### Step 2: Second\n"
            "Body two.\n\n"
            "### Step 3: Third\n"
            "Body three.\n"
        )
        steps = _parse_decomposition(text)
        self.assertEqual(len(steps), 3)
        self.assertEqual(steps[0]["title"], "First")
        self.assertEqual(steps[1]["title"], "Second")
        self.assertEqual(steps[2]["title"], "Third")

    def test_no_marker_returns_empty(self):
        text = "## Refined Issue\n\nSome content here."
        steps = _parse_decomposition(text)
        self.assertEqual(steps, [])

    def test_empty_string_returns_empty(self):
        steps = _parse_decomposition("")
        self.assertEqual(steps, [])

    def test_single_step_returns_one(self):
        """A single step is parsed (caller decides minimum threshold)."""
        text = (
            "## Multi-Step Decomposition\n\n"
            "### Step 1: Only step\n"
            "Body of the only step.\n"
        )
        steps = _parse_decomposition(text)
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["step"], 1)
        self.assertEqual(steps[0]["title"], "Only step")

    def test_steps_sorted_by_number(self):
        """Steps should be sorted even if out of order in input."""
        text = (
            "## Multi-Step Decomposition\n\n"
            "### Step 3: Third\n"
            "Body three.\n\n"
            "### Step 1: First\n"
            "Body one.\n\n"
            "### Step 2: Second\n"
            "Body two.\n"
        )
        steps = _parse_decomposition(text)
        self.assertEqual(len(steps), 3)
        self.assertEqual([s["step"] for s in steps], [1, 2, 3])

    def test_step_body_preserves_multiline(self):
        text = (
            "## Multi-Step Decomposition\n\n"
            "### Step 1: Complex step\n"
            "### Problem\n"
            "Line one.\n"
            "Line two.\n\n"
            "### Plan\n"
            "1. Do A\n"
            "2. Do B\n\n"
            "### Verification\n"
            "Run tests.\n"
        )
        steps = _parse_decomposition(text)
        self.assertEqual(len(steps), 1)
        self.assertIn("Line one.", steps[0]["body"])
        self.assertIn("Line two.", steps[0]["body"])
        self.assertIn("Do A", steps[0]["body"])
        self.assertIn("Run tests.", steps[0]["body"])

    def test_title_on_same_line_as_step_header(self):
        """Title is extracted from text after '### Step N: '."""
        text = (
            "## Multi-Step Decomposition\n\n"
            "### Step 1: Inline title\n"
            "Body text here.\n\n"
            "### Step 2: Another title\n"
            "Body two.\n"
        )
        steps = _parse_decomposition(text)
        self.assertEqual(len(steps), 2)
        self.assertEqual(steps[0]["title"], "Inline title")
        self.assertEqual(steps[1]["title"], "Another title")


from unittest.mock import patch, MagicMock
from cai_lib.actions.refine import _issue_depth, _create_sub_issues, handle_refine


class TestIssueDepth(unittest.TestCase):
    def test_no_depth_label_returns_zero(self):
        issue = {"labels": [{"name": "auto-improve"}, {"name": "auto-improve:raised"}]}
        self.assertEqual(_issue_depth(issue), 0)

    def test_depth_label_returns_n(self):
        issue = {"labels": [{"name": "auto-improve"}, {"name": "depth:1"}]}
        self.assertEqual(_issue_depth(issue), 1)

    def test_depth_two(self):
        issue = {"labels": [{"name": "depth:2"}, {"name": "auto-improve:raised"}]}
        self.assertEqual(_issue_depth(issue), 2)

    def test_empty_labels(self):
        issue = {"labels": []}
        self.assertEqual(_issue_depth(issue), 0)

    def test_no_labels_key(self):
        issue = {}
        self.assertEqual(_issue_depth(issue), 0)

    def test_malformed_depth_label_ignored(self):
        issue = {"labels": [{"name": "depth:abc"}]}
        self.assertEqual(_issue_depth(issue), 0)


class TestCreateSubIssuesDepth(unittest.TestCase):
    @patch("cai_lib.actions.refine.link_sub_issue")
    @patch("cai_lib.actions.refine.create_issue")
    @patch("cai_lib.actions.refine._find_sub_issue", return_value=None)
    def test_depth_label_applied(self, mock_find, mock_create, mock_link):
        mock_create.return_value = {"number": 42, "id": 999, "html_url": "http://x"}
        steps = [{"step": 1, "title": "T", "body": "B"}]
        _create_sub_issues(steps, 10, "Parent", depth=1)
        labels = mock_create.call_args[0][2]
        self.assertIn("depth:1", labels)


class TestSplitDepthGate(unittest.TestCase):
    """At max depth, cai-split's user_message instructs the agent not to
    emit a decomposition block. Decomposition responsibility moved from
    cai-refine to cai-split in the refine-split-architecture change.
    """

    @patch("cai_lib.actions.split.log_run")
    @patch("cai_lib.actions.split._run_claude_p")
    @patch("cai_lib.actions.split.fire_trigger")
    @patch("cai_lib.actions.split._build_issue_block", return_value="issue text")
    def test_max_depth_injects_no_decompose(
        self, mock_build, mock_transition, mock_claude, mock_log_run,
    ):
        mock_claude.return_value = MagicMock(
            returncode=0,
            stdout="## Split Verdict\n\nVERDICT: ATOMIC\n\nConfidence: HIGH\n",
            stderr="",
        )
        with patch("cai_lib.actions.split.MAX_DECOMPOSITION_DEPTH", 2):
            from cai_lib.actions.split import handle_split
            issue = {
                "number": 5, "title": "Test",
                "labels": [{"name": "depth:2"}, {"name": "auto-improve:refined"}],
                "body": "test body",
            }
            handle_split(issue)
        call_kwargs = mock_claude.call_args
        input_msg = call_kwargs.kwargs.get("input") or call_kwargs[1].get("input", "")
        self.assertIn("Do NOT", input_msg)
        self.assertIn("Multi-Step Decomposition", input_msg)
        # Regression guard: handle_split's success path always ends in a
        # log_run call, so the mock must see at least one invocation.
        mock_log_run.assert_called()
        for call in mock_log_run.call_args_list:
            args, kwargs = call
            self.assertEqual(args[0], "split")
            self.assertEqual(kwargs.get("issue"), 5)


from cai_lib.actions.refine import (
    _CLONE_PREFIX_RE,
    _DOT_SLASH_RE,
    _PATH_RE,
    _detect_guardrail_contradictions,
    _extract_files_to_change,
    _extract_paths,
    _extract_scope_guardrails_paths,
)


class TestGuardrailContradictionLint(unittest.TestCase):
    """Tests for the #919 scope-guardrail contradiction lint."""

    def test_no_contradiction_when_sections_disjoint(self):
        body = (
            "## Refined Issue\n\n"
            "### Files to change\n"
            "- `cai_lib/audit/runner.py`\n"
            "- `cai.py`\n\n"
            "### Scope guardrails\n"
            "- Do not modify `cai_lib/publish.py`\n"
        )
        self.assertEqual(_detect_guardrail_contradictions(body), [])

    def test_direct_contradiction_detected(self):
        body = (
            "## Refined Issue\n\n"
            "### Files to change\n"
            "- `cai_lib/audit/runner.py`\n"
            "- `cai_lib/publish.py`\n\n"
            "### Scope guardrails\n"
            "- Do not modify `cai_lib/publish.py` beyond the "
            "  minimal category-set extension\n"
        )
        self.assertEqual(
            _detect_guardrail_contradictions(body),
            ["cai_lib/publish.py"],
        )

    def test_extract_files_to_change_handles_bold_and_backticks(self):
        body = (
            "### Files to change\n"
            "- **`cai_lib/audit/runner.py`** — new file\n"
            "- `cai.py` — update parser\n"
            "- docs/modules.yaml (new)\n\n"
            "### Scope guardrails\n"
        )
        self.assertEqual(
            _extract_files_to_change(body),
            {"cai_lib/audit/runner.py", "cai.py", "docs/modules.yaml"},
        )

    def test_extract_scope_guardrails_paths_multiline(self):
        body = (
            "### Scope guardrails\n"
            "- Do not modify `cai_lib/publish.py`.\n"
            "- Do not delete `cai_lib/cmd_agents.py`.\n"
            "- Keep the YAML schema flat.\n"
        )
        self.assertEqual(
            _extract_scope_guardrails_paths(body),
            {"cai_lib/publish.py", "cai_lib/cmd_agents.py"},
        )

    def test_docs_path_is_ignored_even_if_in_both(self):
        body = (
            "### Files to change\n"
            "- `docs/modules.yaml`\n"
            "- `cai.py`\n\n"
            "### Scope guardrails\n"
            "- Do not edit `docs/modules.yaml`\n"
        )
        self.assertEqual(_detect_guardrail_contradictions(body), [])

    def test_clone_prefix_stripped_so_paths_match(self):
        body = (
            "### Files to change\n"
            "- `/tmp/cai-plan-902-abcd1234/cai_lib/publish.py`\n\n"
            "### Scope guardrails\n"
            "- Do not modify `cai_lib/publish.py`\n"
        )
        self.assertEqual(
            _detect_guardrail_contradictions(body),
            ["cai_lib/publish.py"],
        )

    def test_empty_body_no_contradictions(self):
        self.assertEqual(_detect_guardrail_contradictions(""), [])

    def test_missing_sections_no_contradictions(self):
        body = "## Refined Issue\n\n### Description\n\nSomething.\n"
        self.assertEqual(_detect_guardrail_contradictions(body), [])


class TestExtractPathsHelper(unittest.TestCase):
    """Tests for the _extract_paths pre-strips (clone-prefix + leading ./).

    These lock in that both _CLONE_PREFIX_RE and _DOT_SLASH_RE are
    load-bearing: without them, _PATH_RE's word-char-anchored lookbehind
    silently drops the following legitimate path references.
    """

    def test_clone_prefix_stripped_so_path_is_extracted(self):
        body = (
            "### Files to change\n"
            "- `/tmp/cai-plan-902-abcd1234/cai_lib/publish.py`\n"
        )
        self.assertEqual(
            _extract_paths(body),
            {"cai_lib/publish.py"},
        )

    def test_clone_prefix_is_load_bearing(self):
        # Without _CLONE_PREFIX_RE, _PATH_RE cannot match the bare path
        # because every candidate start position is preceded by "/" or
        # "-", both of which are in _PATH_RE's excluded-lookbehind class.
        raw = "/tmp/cai-plan-902-abcd1234/cai_lib/publish.py"
        self.assertEqual(_PATH_RE.findall(raw), [])
        stripped = _CLONE_PREFIX_RE.sub("", raw)
        self.assertEqual(stripped, "cai_lib/publish.py")
        self.assertEqual(_PATH_RE.findall(stripped), ["cai_lib/publish.py"])

    def test_clone_prefix_regex_matches_canonical_shape(self):
        for prefix in (
            "/tmp/cai-plan-902-abcd1234/",
            "/tmp/cai-implement-998-94eb0b0b/",
            "/tmp/cai-refine-7-deadbeef/",
        ):
            self.assertIsNotNone(
                _CLONE_PREFIX_RE.fullmatch(prefix),
                f"expected _CLONE_PREFIX_RE to fullmatch {prefix!r}",
            )

    def test_leading_dot_slash_is_normalised(self):
        body = (
            "### Files to change\n"
            "- `./cai_lib/publish.py`\n"
        )
        self.assertEqual(
            _extract_paths(body),
            {"cai_lib/publish.py"},
        )

    def test_dot_slash_is_load_bearing(self):
        # Without _DOT_SLASH_RE, _PATH_RE cannot match "./cai_lib/publish.py"
        # because the "c" of "cai_lib" is preceded by "/", which is in
        # _PATH_RE's excluded-lookbehind class [\w/.-].
        raw = "./cai_lib/publish.py"
        self.assertEqual(_PATH_RE.findall(raw), [])
        stripped = _DOT_SLASH_RE.sub("", raw)
        self.assertEqual(stripped, "cai_lib/publish.py")
        self.assertEqual(_PATH_RE.findall(stripped), ["cai_lib/publish.py"])

    def test_empty_and_none_input(self):
        self.assertEqual(_extract_paths(""), set())
        self.assertEqual(_extract_paths(None), set())


if __name__ == "__main__":
    unittest.main()
