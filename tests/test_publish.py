"""Tests for publish.parse_findings."""
import sys
import os
import unittest

# Ensure the repo root is on the import path so `import publish` works
# regardless of how the test runner is invoked.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from publish import parse_findings, Finding, VALID_CATEGORIES  # noqa: F401


def _finding_block(title, category, key, confidence, evidence, remediation):
    """Build a well-formed ### Finding: markdown block."""
    return (
        f"### Finding: {title}\n\n"
        f"- **Category:** {category}\n"
        f"- **Key:** {key}\n"
        f"- **Confidence:** {confidence}\n"
        f"- **Evidence:**\n{evidence}\n"
        f"- **Remediation:** {remediation}\n"
    )


class TestParseFindings(unittest.TestCase):

    def test_well_formed_finding(self):
        text = _finding_block(
            "Token waste in analyze loop",
            "reliability",
            "analyze-token-waste",
            "high",
            "  - Session X used 50k tokens\n  - Session Y used 48k tokens",
            "Reduce context window in analyze prompt",
        )
        findings = parse_findings(text)
        self.assertEqual(len(findings), 1)
        f = findings[0]
        self.assertIsInstance(f, Finding)
        self.assertEqual(f.title, "Token waste in analyze loop")
        self.assertEqual(f.category, "reliability")
        self.assertEqual(f.key, "analyze-token-waste")
        self.assertEqual(f.confidence, "high")
        self.assertIn("50k tokens", f.evidence)
        self.assertIn("Reduce context window", f.remediation)

    def test_invalid_category_skipped(self):
        text = _finding_block(
            "Some finding",
            "bogus",
            "some-key",
            "medium",
            "  - evidence line",
            "Fix it",
        )
        findings = parse_findings(text)
        self.assertEqual(findings, [])

    def test_backtick_stripping(self):
        text = _finding_block(
            "Backtick finding",
            "`reliability`",
            "`some-key`",
            "`high`",
            "  - evidence",
            "Remediate",
        )
        findings = parse_findings(text)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].category, "reliability")
        self.assertEqual(findings[0].key, "some-key")
        self.assertEqual(findings[0].confidence, "high")

    def test_multi_finding(self):
        text = (
            _finding_block(
                "First finding",
                "reliability",
                "key-one",
                "high",
                "  - evidence 1",
                "Fix first",
            )
            + "\n"
            + _finding_block(
                "Second finding",
                "cost_reduction",
                "key-two",
                "medium",
                "  - evidence 2",
                "Fix second",
            )
        )
        findings = parse_findings(text)
        self.assertEqual(len(findings), 2)
        self.assertEqual(findings[0].title, "First finding")
        self.assertEqual(findings[1].title, "Second finding")

    def test_multiline_evidence_remediation(self):
        text = (
            "### Finding: Multi-line test\n\n"
            "- **Category:** prompt_quality\n"
            "- **Key:** multi-line-key\n"
            "- **Confidence:** low\n"
            "- **Evidence:**\n"
            "  - Line one of evidence\n"
            "  - Line two of evidence\n"
            "  - Line three of evidence\n"
            "- **Remediation:**\n"
            "  - Step one\n"
            "  - Step two\n"
            "  - Step three\n"
        )
        findings = parse_findings(text)
        self.assertEqual(len(findings), 1)
        self.assertIn("Line one of evidence", findings[0].evidence)
        self.assertIn("Line three of evidence", findings[0].evidence)
        self.assertIn("Step one", findings[0].remediation)
        self.assertIn("Step three", findings[0].remediation)

    def test_empty_input(self):
        findings = parse_findings("")
        self.assertEqual(findings, [])

    def test_custom_valid_categories(self):
        text = _finding_block(
            "Custom category finding",
            "custom_cat",
            "custom-key",
            "medium",
            "  - custom evidence",
            "Custom fix",
        )
        findings = parse_findings(text, valid_categories={"custom_cat"})
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].category, "custom_cat")


if __name__ == "__main__":
    unittest.main()
