"""Tests for publish module."""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

# Ensure the repo root is on the import path so `import cai_lib` works
# regardless of how the test runner is invoked.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cai_lib import publish as publish_mod  # noqa: E402
from cai_lib.dup_check import DupCheckVerdict  # noqa: E402
from cai_lib.publish import (  # noqa: E402
    CHECK_WORKFLOWS_LABELS,
    Finding,
    LABELS_TO_DELETE,
    _finding_body_for_dupcheck,
)


class TestCheckWorkflowsLabels(unittest.TestCase):

    def test_check_workflows_raised_not_in_labels(self):
        """check-workflows:raised must NOT appear in CHECK_WORKFLOWS_LABELS (retired label)."""
        label_names = [name for name, _, _ in CHECK_WORKFLOWS_LABELS]
        self.assertNotIn("check-workflows:raised", label_names)

    def test_check_workflows_in_labels(self):
        """check-workflows source tag must remain in CHECK_WORKFLOWS_LABELS."""
        label_names = [name for name, _, _ in CHECK_WORKFLOWS_LABELS]
        self.assertIn("check-workflows", label_names)

    def test_auto_improve_raised_in_check_workflows_labels(self):
        """auto-improve:raised must be in CHECK_WORKFLOWS_LABELS so new findings enter the FSM."""
        label_names = [name for name, _, _ in CHECK_WORKFLOWS_LABELS]
        self.assertIn("auto-improve:raised", label_names)

    def test_check_workflows_raised_in_labels_to_delete(self):
        """check-workflows:raised must be in LABELS_TO_DELETE so it gets cleaned up on publish runs."""
        self.assertIn("check-workflows:raised", LABELS_TO_DELETE)


class TestUpdateCheckKindCodePrelabel(unittest.TestCase):
    """cai-update-check findings always require source-file edits,
    so kind:code is pre-applied at create_issue time and guaranteed
    to exist via UPDATE_CHECK_LABELS. Prevents the #980 divert
    class where an update-check finding got mis-classified as
    kind:maintenance and routed to cai-maintain (issue #991).
    """

    def test_kind_code_in_update_check_labels(self):
        from cai_lib.publish import UPDATE_CHECK_LABELS
        names = [name for name, _, _ in UPDATE_CHECK_LABELS]
        self.assertIn("kind:code", names)

    def test_create_issue_passes_kind_code_for_update_check(self):
        from cai_lib import publish as pub
        captured = {}

        class FakeResult:
            returncode = 0

        def fake_run(argv, check=False, capture_output=False):
            captured["argv"] = list(argv)
            return FakeResult()

        f = pub.Finding(
            title="Bump CLAUDE_CODE_VERSION to 2.1.114",
            category="version_update",
            key="update-check-2.1.114",
            confidence="high",
            evidence="Release notes mention a relevant fix.",
            remediation="Edit Dockerfile line 12.",
        )
        with mock.patch.object(pub.subprocess, "run", side_effect=fake_run):
            rc = pub.create_issue(f, namespace="update-check")
        self.assertEqual(rc, 0)
        # Extract the --label argument value from the captured argv.
        argv = captured["argv"]
        idx = argv.index("--label")
        label_arg = argv[idx + 1]
        labels = label_arg.split(",")
        self.assertIn("kind:code", labels)
        self.assertIn("auto-improve", labels)
        self.assertIn("auto-improve:raised", labels)

    def test_create_issue_does_not_pass_kind_code_for_other_namespaces(self):
        """Only update-check should pre-apply kind:code; other namespaces
        still rely on cai-triage's haiku classifier to set kind."""
        from cai_lib import publish as pub
        captured = {}

        class FakeResult:
            returncode = 0

        def fake_run(argv, check=False, capture_output=False):
            captured["argv"] = list(argv)
            return FakeResult()

        f = pub.Finding(
            title="Some finding",
            category="reliability",
            key="analyzer-1",
            confidence="high",
            evidence="ev",
            remediation="rm",
        )
        with mock.patch.object(pub.subprocess, "run", side_effect=fake_run):
            rc = pub.create_issue(f, namespace="auto-improve")
        self.assertEqual(rc, 0)
        argv = captured["argv"]
        idx = argv.index("--label")
        label_arg = argv[idx + 1]
        labels = label_arg.split(",")
        self.assertNotIn("kind:code", labels)


class TestFindingBodyForDupCheck(unittest.TestCase):

    def test_body_contains_evidence_and_remediation(self):
        f = Finding(
            title="t", category="reliability", key="k",
            confidence="high",
            evidence="Ev text", remediation="Rm text",
        )
        body = _finding_body_for_dupcheck(f)
        self.assertIn("Ev text", body)
        self.assertIn("Rm text", body)
        self.assertIn("reliability", body)


def _write_findings(findings: list[dict]) -> str:
    fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump({"findings": findings}, fh)
    fh.close()
    return fh.name


class TestSemanticDupCheckIntegration(unittest.TestCase):
    """Exercise ``main()``'s pre-publish dup-check branch end-to-end."""

    def _one_finding(self):
        return [{
            "title": "Flaky watchdog",
            "category": "reliability",
            "key": "fp-1",
            "confidence": "high",
            "evidence": "evidence",
            "remediation": "remediation",
        }]

    def test_semantic_duplicate_skips_create(self):
        path = _write_findings(self._one_finding())
        try:
            dup_verdict = DupCheckVerdict(
                verdict="DUPLICATE", confidence="HIGH",
                target=42, commit_sha=None,
                reasoning="Already covered by #42",
            )
            with mock.patch.object(publish_mod, "issue_exists", return_value=False), \
                 mock.patch.object(publish_mod, "ensure_labels"), \
                 mock.patch.object(publish_mod, "check_finding_duplicate",
                                    return_value=dup_verdict) as m_dup, \
                 mock.patch.object(publish_mod, "create_issue") as m_create, \
                 mock.patch.object(sys, "argv",
                                    ["publish.py", "--findings-file", path]), \
                 mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("CAI_SKIP_DUPCHECK_ON_PUBLISH", None)
                rc = publish_mod.main()
            self.assertEqual(rc, 0)
            m_dup.assert_called_once()
            m_create.assert_not_called()
        finally:
            os.unlink(path)

    def test_non_duplicate_proceeds_to_create(self):
        path = _write_findings(self._one_finding())
        try:
            none_verdict = DupCheckVerdict(
                verdict="NONE", confidence="HIGH",
                target=None, commit_sha=None, reasoning="distinct",
            )
            with mock.patch.object(publish_mod, "issue_exists", return_value=False), \
                 mock.patch.object(publish_mod, "ensure_labels"), \
                 mock.patch.object(publish_mod, "check_finding_duplicate",
                                    return_value=none_verdict), \
                 mock.patch.object(publish_mod, "create_issue",
                                    return_value=0) as m_create, \
                 mock.patch.object(sys, "argv",
                                    ["publish.py", "--findings-file", path]), \
                 mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("CAI_SKIP_DUPCHECK_ON_PUBLISH", None)
                rc = publish_mod.main()
            self.assertEqual(rc, 0)
            m_create.assert_called_once()
        finally:
            os.unlink(path)

    def test_env_var_disables_dupcheck(self):
        path = _write_findings(self._one_finding())
        try:
            with mock.patch.object(publish_mod, "issue_exists", return_value=False), \
                 mock.patch.object(publish_mod, "ensure_labels"), \
                 mock.patch.object(publish_mod, "check_finding_duplicate") as m_dup, \
                 mock.patch.object(publish_mod, "create_issue",
                                    return_value=0) as m_create, \
                 mock.patch.object(sys, "argv",
                                    ["publish.py", "--findings-file", path]), \
                 mock.patch.dict(os.environ,
                                  {"CAI_SKIP_DUPCHECK_ON_PUBLISH": "1"},
                                  clear=False):
                rc = publish_mod.main()
            self.assertEqual(rc, 0)
            m_dup.assert_not_called()
            m_create.assert_called_once()
        finally:
            os.unlink(path)

    def test_agent_failure_proceeds_to_create(self):
        """A ``None`` verdict (agent crash / parse failure) must NOT block publish."""
        path = _write_findings(self._one_finding())
        try:
            with mock.patch.object(publish_mod, "issue_exists", return_value=False), \
                 mock.patch.object(publish_mod, "ensure_labels"), \
                 mock.patch.object(publish_mod, "check_finding_duplicate",
                                    return_value=None), \
                 mock.patch.object(publish_mod, "create_issue",
                                    return_value=0) as m_create, \
                 mock.patch.object(sys, "argv",
                                    ["publish.py", "--findings-file", path]), \
                 mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("CAI_SKIP_DUPCHECK_ON_PUBLISH", None)
                rc = publish_mod.main()
            self.assertEqual(rc, 0)
            m_create.assert_called_once()
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
