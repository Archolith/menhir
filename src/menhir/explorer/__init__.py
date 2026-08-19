"""Developer explorer for inspecting the memory graph."""

from .app import create_app

# `app` is deliberately NOT re-exported. Importing it here built a FastAPI instance on every
# import of this package, and it also rebound the package attribute `app` from the SUBMODULE to
# that instance -- so `menhir.explorer.app` resolved to a FastAPI object in attribute context
# while sys.modules["menhir.explorer.app"] stayed the module. Tests patch through sys.modules,
# so the two disagreed about what the same dotted name meant.
__all__ = ["create_app"]
