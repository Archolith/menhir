"""Shared APIRouter for the menhir REST surface.

The route handlers live in the sibling ``routes_*.py`` modules beside ``routes.py``; each
decorates this one router so the application mounts a single API. ``menhir.api.routes``
re-exports ``router`` for every existing import site.
"""

from fastapi import APIRouter

router = APIRouter(prefix="/api")
