"""
menhir - Graph-based long-term memory system.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    # Single source of truth: the installed package's own metadata (which
    # setuptools populates from pyproject.toml's [project].version). Avoids
    # the drift a second hardcoded literal here would eventually cause (see
    # SSOT-13: server.py and explorer/app.py each hardcoded their own,
    # already-diverged "0.2.0"/"0.1.0" instead of reading this).
    __version__ = version("archolith-menhir")
except PackageNotFoundError:
    # Not installed (e.g. running from a source checkout without `pip install -e .`).
    __version__ = "0.0.0-dev"
