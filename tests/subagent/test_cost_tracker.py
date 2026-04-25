"""Tests for CostRow.from_result_message() in cai_lib/subagent/cost_tracker.py."""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tests._helpers import _mk_result
from cai_lib.subagent.cost_tracker import CostRow


class TestCostRowNoneCostHandling(unittest.TestCase):
    """CostRow.from_result_message handles total_cost_usd=None gracefully."""

    def test_none_total_cost_usd_coerced_to_zero(self):
        """total_cost_usd=None must not raise ValidationError; coerced to 0.0.

        The SDK's ResultMessage.total_cost_usd is typed float | None.
        CostRow.cost_usd is a required float field — passing None straight
        through crashes Pydantic validation.  The coercion
        ``result.total_cost_usd or 0.0`` in from_result_message must
        convert None to 0.0.
        """
        result = _mk_result(total_cost_usd=None)

        with patch("cai_lib.subagent.cost_tracker.socket.gethostname", return_value="test-host"):
            row = CostRow.from_result_message(
                category="test",
                agent="cai-test",
                result=result,
            )

        self.assertEqual(row.cost_usd, 0.0)

    def test_normal_cost_usd_passes_through(self):
        """A normal float total_cost_usd value is preserved unchanged."""
        result = _mk_result(total_cost_usd=0.5678)

        with patch("cai_lib.subagent.cost_tracker.socket.gethostname", return_value="test-host"):
            row = CostRow.from_result_message(
                category="test",
                agent="cai-test",
                result=result,
            )

        self.assertAlmostEqual(row.cost_usd, 0.5678)


if __name__ == "__main__":
    unittest.main()
