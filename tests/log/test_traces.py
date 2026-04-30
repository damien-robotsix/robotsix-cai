"""Tests for helper functions in cai.log.traces."""

from __future__ import annotations

from datetime import datetime, timezone

from cai.log.traces import _build_list_kwargs, _format_trace


class TestFormatTrace:
    """Tests for _format_trace(t) — the dict builder that maps a trace object
    to a five-field dict.
    """

    def test_all_fields_present_and_correct(self):
        """Happy-path: all attributes set, all five fields correct."""
        t = _FakeTrace(
            id="tr-001",
            name="cai-solve",
            timestamp=datetime(2025, 1, 15, 10, 30, 0, tzinfo=timezone.utc),
            total_cost=0.042,
            latency=3.7,
        )
        result = _format_trace(t)
        assert result == {
            "id": "tr-001",
            "name": "cai-solve",
            "timestamp": "2025-01-15T10:30:00+00:00",
            "cost": 0.042,
            "latency": 3.7,
        }

    def test_name_falls_back_to_empty_string_when_none(self):
        """``name`` becomes ``""`` when the trace name is None."""
        t = _FakeTrace(id="tr-002", name=None)
        result = _format_trace(t)
        assert result["name"] == ""

    def test_timestamp_none_when_attribute_missing(self):
        """``timestamp`` is None when the object has no timestamp attribute."""
        t = _FakeTrace(id="tr-003", name="wf", _omit={"timestamp"})
        result = _format_trace(t)
        assert result["timestamp"] is None

    def test_timestamp_none_when_explicit_none(self):
        """``timestamp`` is None when timestamp is explicitly None."""
        t = _FakeTrace(id="tr-004", name="wf", timestamp=None)
        result = _format_trace(t)
        assert result["timestamp"] is None

    def test_cost_none_when_attribute_missing(self):
        """``cost`` is None when the object has no total_cost attribute."""
        t = _FakeTrace(id="tr-005", name="wf", _omit={"total_cost"})
        result = _format_trace(t)
        assert result["cost"] is None

    def test_latency_none_when_attribute_missing(self):
        """``latency`` is None when the object has no latency attribute."""
        t = _FakeTrace(id="tr-006", name="wf", _omit={"latency"})
        result = _format_trace(t)
        assert result["latency"] is None

    def test_cost_zero_is_preserved(self):
        """``cost`` is 0 when total_cost is 0 (zero is a valid cost)."""
        t = _FakeTrace(id="tr-007", name="wf", total_cost=0)
        result = _format_trace(t)
        assert result["cost"] == 0

    def test_latency_zero_is_preserved(self):
        """``latency`` is 0 when latency is 0."""
        t = _FakeTrace(id="tr-008", name="wf", latency=0)
        result = _format_trace(t)
        assert result["latency"] == 0

    def test_dict_has_exactly_five_keys(self):
        """The returned dict has exactly the five expected keys."""
        t = _FakeTrace(id="tr-009", name="wf")
        result = _format_trace(t)
        assert set(result.keys()) == {"id", "name", "timestamp", "cost", "latency"}
        assert len(result) == 5


class TestFakeTrace:
    """Tests for _FakeTrace.__init__ — especially _omit AttributeError handling."""

    def test_omit_deletes_attribute_when_present(self):
        """_omit removes an attribute that was set by kwargs."""
        t = _FakeTrace(id="t1", name="wf", timestamp=datetime(2025, 1, 1, tzinfo=timezone.utc),
                       _omit={"timestamp"})
        assert not hasattr(t, "timestamp")
        assert t.id == "t1"
        assert t.name == "wf"

    def test_omit_silently_skips_attribute_never_set(self):
        """_omit does not raise when the attribute was never passed to kwargs."""
        t = _FakeTrace(id="t1", name="wf", _omit={"total_cost", "latency"})
        assert not hasattr(t, "total_cost")
        assert not hasattr(t, "latency")
        assert t.id == "t1"
        assert t.name == "wf"

    def test_omit_mixed_some_present_some_missing(self):
        """Mixed _omit: present attrs are deleted, missing ones are silently skipped."""
        t = _FakeTrace(id="t1", name="wf", timestamp=None, latency=1.5,
                       _omit={"timestamp", "total_cost"})
        # timestamp was passed (as None) → deleted
        assert not hasattr(t, "timestamp")
        # total_cost was never passed → silently skipped (no AttributeError)
        assert not hasattr(t, "total_cost")
        # latency was passed and not in _omit → present
        assert t.latency == 1.5
        # id and name always present
        assert t.id == "t1"
        assert t.name == "wf"

    def test_empty_omit_has_no_effect(self):
        """Empty _omit (default) leaves all kwargs as attributes."""
        t = _FakeTrace(id="t1", name="wf", total_cost=0.0, latency=0.0)
        assert t.id == "t1"
        assert t.name == "wf"
        assert t.total_cost == 0.0
        assert t.latency == 0.0

    def test_no_omit_keyword_uses_default_empty_set(self):
        """When _omit is not passed at all, all kwargs become attributes."""
        t = _FakeTrace(id="t1", name="wf", total_cost=1.23)
        assert hasattr(t, "total_cost")
        assert t.total_cost == 1.23


class TestBuildListKwargs:
    """Tests for _build_list_kwargs(limit, since) — the shared kwargs builder."""

    def test_basic_without_since(self):
        """Returns dict with limit and page=1 when since is None."""
        result = _build_list_kwargs(20)
        assert result == {"limit": 20, "page": 1}

    def test_with_since(self):
        """Sets a UTC-aware from_timestamp when since is an ISO string."""
        result = _build_list_kwargs(50, since="2025-06-01T12:00:00")
        assert result["limit"] == 50
        assert result["page"] == 1
        ts = result["from_timestamp"]
        assert ts == datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)

    def test_since_none_explicit(self):
        """When since is explicitly None no from_timestamp key is added."""
        result = _build_list_kwargs(10, since=None)
        assert "from_timestamp" not in result
        assert result == {"limit": 10, "page": 1}

    def test_returned_dict_is_mutable(self):
        """Callers can safely add extra keys to the returned dict."""
        result = _build_list_kwargs(5, since="2025-01-01")
        result["name"] = "cai-solve"
        assert result["name"] == "cai-solve"
        assert result["limit"] == 5
        assert result["page"] == 1
        assert "from_timestamp" in result


class _FakeTrace:
    """Minimal fake that mimics a Langfuse trace object.

    Any keyword passed as ``_omit`` will be removed from the instance
    *after* ``__init__`` so that ``getattr(t, attr, None)`` sees no
    attribute at all (simulating a trace that simply lacks the field).
    """

    def __init__(self, **kwargs):
        _omit = set(kwargs.pop("_omit", ()))
        for k, v in kwargs.items():
            setattr(self, k, v)
        for k in _omit:
            try:
                delattr(self, k)
            except AttributeError:
                pass
