"""Tests for build_model_settings — parsing and validation of model frontmatter knobs.

The implementation changed explore.md to deepseek-v4-flash (was v4-pro) and
added ``max_tokens: 16000`` to refine.md and ``max_tokens: 32000`` to
implement.md.  The ``build_model_settings`` function in loader.py is
responsible for translating ``max_tokens`` (and other optional keys) from
the parsed YAML frontmatter into a ``model_settings`` dict consumed by
``build_deep_agent_kwargs`` / ``build_deep_agent``.
"""

from __future__ import annotations

from typing import Any

import pytest

from cai.agents.loader import build_model_settings


# ---------------------------------------------------------------------------
# max_tokens — the key being introduced in this change
# ---------------------------------------------------------------------------


class TestMaxTokens:
    """build_model_settings parses and validates ``max_tokens`` from config."""

    def test_positive_int(self):
        """A positive int is accepted and returned in settings."""
        result = build_model_settings({"max_tokens": 16000})
        assert result is not None
        assert result["max_tokens"] == 16000

    def test_large_positive_int(self):
        """Large token budgets (e.g. 32000) are accepted."""
        result = build_model_settings({"max_tokens": 32000})
        assert result is not None
        assert result["max_tokens"] == 32000

    def test_small_positive_int(self):
        """Small positive token budgets (e.g. 1) are accepted."""
        result = build_model_settings({"max_tokens": 1})
        assert result is not None
        assert result["max_tokens"] == 1

    def test_missing_key(self):
        """When max_tokens is absent, settings dict is returned but without the key."""
        result = build_model_settings({})
        assert result is None or "max_tokens" not in result

    def test_none_value(self):
        """When max_tokens is None, it is treated as absent."""
        result = build_model_settings({"max_tokens": None})
        assert result is None or "max_tokens" not in result

    # ---- validation failures ------------------------------------------------

    def test_zero_raises(self):
        """max_tokens=0 must raise ValueError (not positive)."""
        with pytest.raises(ValueError, match="max_tokens.*positive int"):
            build_model_settings({"max_tokens": 0})

    def test_negative_raises(self):
        """Negative max_tokens must raise ValueError."""
        with pytest.raises(ValueError, match="max_tokens.*positive int"):
            build_model_settings({"max_tokens": -100})

    def test_float_raises(self):
        """A float value (even if convertible to int) must raise TypeError/ValueError."""
        with pytest.raises((ValueError, TypeError)):
            build_model_settings({"max_tokens": 16000.0})

    def test_string_raises(self):
        """A string value must raise ValueError."""
        with pytest.raises((ValueError, TypeError)):
            build_model_settings({"max_tokens": "16000"})

    def test_list_raises(self):
        """A list value must raise ValueError/TypeError."""
        with pytest.raises((ValueError, TypeError)):
            build_model_settings({"max_tokens": [16000]})


# ---------------------------------------------------------------------------
# No settings at all
# ---------------------------------------------------------------------------


class TestNoSettings:
    """When no model-knob keys are present, build_model_settings returns None."""

    def test_empty_config(self):
        """An empty config returns None."""
        assert build_model_settings({}) is None

    def test_irrelevant_keys_only(self):
        """Config with only non-model keys (e.g. name, tools) returns None."""
        config: dict[str, Any] = {
            "name": "explore",
            "tools": ["filesystem_read", "grep"],
        }
        assert build_model_settings(config) is None

    def test_common_key_only(self):
        """Config with only common fragments returns None."""
        config: dict[str, Any] = {
            "common": ["anti_hallucination_guard"],
        }
        assert build_model_settings(config) is None


# ---------------------------------------------------------------------------
# Other supported knob keys (regression coverage)
# ---------------------------------------------------------------------------


class TestOtherSettings:
    """build_model_settings also handles temperature, penalties, and reasoning."""

    @pytest.mark.parametrize("temp", [0.0, 0.2, 1.0, 2.0])
    def test_temperature(self, temp: float):
        """Temperature values are passed through."""
        result = build_model_settings({"temperature": temp})
        assert result is not None
        assert result["temperature"] == temp

    @pytest.mark.parametrize("penalty", [0.0, 0.5, 1.0, 2.0])
    def test_frequency_penalty(self, penalty: float):
        """Frequency penalty values are passed through."""
        result = build_model_settings({"frequency_penalty": penalty})
        assert result is not None
        assert result["frequency_penalty"] == penalty

    @pytest.mark.parametrize("penalty", [0.0, 0.5, 1.0, 2.0])
    def test_presence_penalty(self, penalty: float):
        """Presence penalty values are passed through."""
        result = build_model_settings({"presence_penalty": penalty})
        assert result is not None
        assert result["presence_penalty"] == penalty

    def test_reasoning_true(self):
        """reasoning=True wraps as extra_body.reasoning.enabled."""
        result = build_model_settings({"reasoning": True})
        assert result is not None
        assert result["extra_body"] == {"reasoning": {"enabled": True}}

    def test_reasoning_false(self):
        """reasoning=False wraps as extra_body.reasoning.enabled."""
        result = build_model_settings({"reasoning": False})
        assert result is not None
        assert result["extra_body"] == {"reasoning": {"enabled": False}}

    def test_reasoning_dict_effort(self):
        """reasoning as a dict (e.g. {"effort": "high"}) is stored directly."""
        result = build_model_settings({"reasoning": {"effort": "high"}})
        assert result is not None
        assert result["reasoning"] == {"effort": "high"}

    def test_reasoning_dict_with_enabled(self):
        """reasoning as a dict with multiple keys is stored directly."""
        result = build_model_settings({"reasoning": {"enabled": True, "effort": "high"}})
        assert result is not None
        assert result["reasoning"] == {"enabled": True, "effort": "high"}

    def test_reasoning_empty_dict(self):
        """An empty dict reasoning value is stored directly under the reasoning key."""
        result = build_model_settings({"reasoning": {}})
        assert result is not None
        assert result["reasoning"] == {}

    def test_reasoning_dict_and_max_tokens(self):
        """reasoning as a dict works alongside other settings like max_tokens."""
        config: dict[str, Any] = {
            "max_tokens": 16000,
            "reasoning": {"effort": "low"},
        }
        result = build_model_settings(config)
        assert result is not None
        assert result["max_tokens"] == 16000
        assert result["reasoning"] == {"effort": "low"}

    def test_reasoning_bool_and_max_tokens(self):
        """reasoning as a bool works alongside other settings like max_tokens."""
        config: dict[str, Any] = {
            "max_tokens": 32000,
            "reasoning": True,
        }
        result = build_model_settings(config)
        assert result is not None
        assert result["max_tokens"] == 32000
        assert result["extra_body"] == {"reasoning": {"enabled": True}}

    def test_reasoning_invalid_string_raises(self):
        """A string reasoning value must raise ValueError."""
        with pytest.raises(ValueError, match="'reasoning'.*must be a bool or dict"):
            build_model_settings({"reasoning": "high"})

    def test_reasoning_invalid_int_raises(self):
        """An int reasoning value must raise ValueError."""
        with pytest.raises(ValueError, match="'reasoning'.*must be a bool or dict"):
            build_model_settings({"reasoning": 1})

    def test_reasoning_invalid_list_raises(self):
        """A list reasoning value must raise ValueError."""
        with pytest.raises(ValueError, match="'reasoning'.*must be a bool or dict"):
            build_model_settings({"reasoning": ["high"]})

    def test_all_knobs_together(self):
        """Multiple knobs in the same config are all reflected in the output."""
        config: dict[str, Any] = {
            "max_tokens": 32000,
            "temperature": 0.7,
            "frequency_penalty": 0.1,
            "presence_penalty": 0.0,
            "reasoning": {"effort": "medium"},
        }
        result = build_model_settings(config)
        assert result is not None
        assert result["max_tokens"] == 32000
        assert result["temperature"] == 0.7
        assert result["frequency_penalty"] == 0.1
        assert result["presence_penalty"] == 0.0
        assert result["reasoning"] == {"effort": "medium"}


# ---------------------------------------------------------------------------
# Unknown / unhandled keys are silently ignored
# ---------------------------------------------------------------------------


class TestIgnoredKeys:
    """Keys that are not recognised model knobs are ignored."""

    def test_unknown_top_level_key(self):
        """An unexpected key is silently dropped from the settings output."""
        result = build_model_settings({"unknown_key": 42})
        assert result is None or "unknown_key" not in result
