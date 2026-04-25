"""cai — containerized Claude Code launcher with a GitHub App identity."""
from .github.bot import CaiBot

__version__ = "0.1.0"
__all__ = ["CaiBot", "__version__"]
