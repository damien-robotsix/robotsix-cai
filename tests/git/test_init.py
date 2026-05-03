"""Tests for :mod:`cai.git` package exports."""

from cai.git import __all__


def test_fetch_in_all():
    """fetch must be listed in ``__all__``."""
    assert "fetch" in __all__


def test_fetch_is_importable():
    """Importing fetch from cai.git must succeed."""
    import importlib

    mod = importlib.import_module("cai.git")
    assert hasattr(mod, "fetch"), "fetch should be a re-export"


def test_all_matches_module_contents():
    """Every name in __all__ should be accessible via getattr on the module."""
    import importlib

    mod = importlib.import_module("cai.git")
    for name in __all__:
        assert hasattr(mod, name), (
            f"{name} listed in __all__ but not found on cai.git"
        )
