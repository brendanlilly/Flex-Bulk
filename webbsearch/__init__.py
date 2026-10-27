"""Flex Bulk: bulk order matching (the engine is FlexSearch v2.46's, copied)."""
from pathlib import Path

try:
    __version__ = (Path(__file__).resolve().parent.parent / "VERSION").read_text().strip()
except OSError:
    __version__ = "dev"
