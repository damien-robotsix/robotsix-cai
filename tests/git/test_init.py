"""Tests for :mod:`cai.git` package exports."""

from cai.git import __all__


def test_fetch_not_in_all():
    """fetch was removed and must not appear in ``__all__``."""
    assert "fetch" not in __all__


def test_fetch_not_importable():
    """Importing fetch from cai.git must raise ImportError."""
    import importlib

    mod = importlib.import_module("cai.git")
    assert not hasattr(mod, "fetch"), "fetch should not be a re-export"


def test_all_matches_module_contents():
    """Every name in __all__ should be accessible via getattr on the module."""
    import importlib

    mod = importlib.import_module("cai.git")
    for name in __all__:
        assert hasattr(mod, name), (
            f"{name} listed in __all__ but not found on cai.git"
        )
