"""Build the fixture repository E2E-3 and E2E-6 index.

E2E-3 requires a repo containing "imports, callers, tests, an endpoint/dependency, and a
small Git history", and asserts specific caller/import/test relationships plus a
blast-radius answer. Those assertions need a repo whose true relationships are known
exactly, so it is generated here rather than checked in: a committed fixture drifts as
people tidy it, and then the expected relationships in the lane quietly stop matching.

The generated shape, with the relationships each part exists to make assertable::

    src/shop/config.py        leaf; imported by storage and api
    src/shop/storage.py       imports config;       called by service
    src/shop/service.py       imports storage;      called by api      <- blast radius hub
    src/shop/api.py           imports service, config; defines the endpoint
    tests/test_service.py     imports service       <- affected test for service.py
    tests/test_storage.py     imports storage       <- affected test for storage.py
    docs/overview.md          prose, for Beacon grounding in E2E-6

So editing ``storage.py`` must reach ``service.py`` and ``api.py`` transitively, and its
affected tests must include ``test_storage.py`` and (transitively) ``test_service.py``.
``config.py`` is imported by two modules to catch a blast radius that collapses a
diamond to one path.

``UNINDEXED_PATH`` is never written to the repo at all. E2E-3 asks an unknown path for
its blast radius and requires a coverage caveat rather than a false-safe empty answer --
the "nothing depends on this, go ahead" reply that is indistinguishable from a correct
one until it deletes something.
"""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path

__all__ = ["FixtureRepo", "UNINDEXED_PATH", "build_fixture_repo"]

#: A path the fixture deliberately does NOT contain. Asking about it must produce a
#: coverage caveat, never a confident empty result.
UNINDEXED_PATH = "src/shop/never_indexed.py"


_FILES: dict[str, str] = {
    "pyproject.toml": """\
[project]
name = "shop"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = ["fastapi>=0.110"]

[tool.setuptools.packages.find]
where = ["src"]
""",
    "README.md": """\
# shop

A tiny order service used as an end-to-end fixture. It exposes one HTTP endpoint,
`GET /orders/{order_id}`, backed by an in-memory store.

Run the tests with `pytest`.
""",
    "docs/overview.md": """\
# Overview

The shop service answers order lookups. Requests enter through `api.py`, which delegates
to `service.py` for business rules, which reads through `storage.py`. Configuration for
every layer comes from `config.py`.

## Guardrails

- Order identifiers are opaque strings and must never be parsed for meaning.
- The storage layer is synchronous on purpose; do not introduce a background writer.
""",
    "src/shop/__init__.py": '"""The shop package."""\n',
    "src/shop/config.py": '''\
"""Configuration shared by every layer."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    """Runtime configuration for the shop service."""

    max_orders: int = 100
    currency: str = "USD"


def load_config() -> Config:
    """Return the default configuration."""

    return Config()
''',
    "src/shop/storage.py": '''\
"""In-memory order storage."""

from __future__ import annotations

from shop.config import Config, load_config


class OrderStore:
    """Holds orders for the lifetime of the process."""

    def __init__(self, config: Config | None = None) -> None:
        self.config = config or load_config()
        self._orders: dict[str, dict[str, object]] = {}

    def put(self, order_id: str, payload: dict[str, object]) -> None:
        if len(self._orders) >= self.config.max_orders:
            raise ValueError("order store is full")
        self._orders[order_id] = payload

    def get(self, order_id: str) -> dict[str, object] | None:
        return self._orders.get(order_id)
''',
    "src/shop/service.py": '''\
"""Order business rules."""

from __future__ import annotations

from shop.storage import OrderStore


class OrderService:
    """Applies business rules on top of :class:`OrderStore`."""

    def __init__(self, store: OrderStore | None = None) -> None:
        self.store = store or OrderStore()

    def fetch_order(self, order_id: str) -> dict[str, object]:
        """Return an order, or raise ``KeyError`` when it is unknown."""

        order = self.store.get(order_id)
        if order is None:
            raise KeyError(order_id)
        return order

    def place_order(self, order_id: str, total: float) -> dict[str, object]:
        payload = {"id": order_id, "total": total, "currency": self.store.config.currency}
        self.store.put(order_id, payload)
        return payload
''',
    "src/shop/api.py": '''\
"""HTTP surface for the shop service."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException

from shop.config import load_config
from shop.service import OrderService

app = FastAPI(title="shop")
_service = OrderService()


@app.get("/orders/{order_id}")
def read_order(order_id: str) -> dict[str, object]:
    """Look up one order by its identifier."""

    try:
        return _service.fetch_order(order_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="order not found")


@app.get("/config")
def read_config() -> dict[str, object]:
    config = load_config()
    return {"max_orders": config.max_orders, "currency": config.currency}
''',
    "tests/test_storage.py": '''\
"""Tests for the storage layer."""

from shop.storage import OrderStore


def test_put_and_get_roundtrip():
    store = OrderStore()
    store.put("abc", {"id": "abc"})
    assert store.get("abc") == {"id": "abc"}


def test_get_unknown_returns_none():
    assert OrderStore().get("missing") is None
''',
    "tests/test_service.py": '''\
"""Tests for the service layer."""

import pytest

from shop.service import OrderService


def test_place_then_fetch():
    service = OrderService()
    service.place_order("abc", 10.0)
    assert service.fetch_order("abc")["total"] == 10.0


def test_fetch_unknown_raises():
    with pytest.raises(KeyError):
        OrderService().fetch_order("nope")
''',
}


#: Relationships the lane asserts. Kept beside the content so a fixture edit that breaks
#: an expectation is visible in the same diff as the expectation.
EXPECTED_IMPORTS: dict[str, set[str]] = {
    "src/shop/storage.py": {"src/shop/config.py"},
    "src/shop/service.py": {"src/shop/storage.py"},
    "src/shop/api.py": {"src/shop/service.py", "src/shop/config.py"},
    "tests/test_storage.py": {"src/shop/storage.py"},
    "tests/test_service.py": {"src/shop/service.py"},
}

#: Editing the key must be understood to reach every value (transitively).
EXPECTED_BLAST_RADIUS: dict[str, set[str]] = {
    "src/shop/storage.py": {"src/shop/service.py", "src/shop/api.py"},
    "src/shop/config.py": {"src/shop/storage.py", "src/shop/service.py", "src/shop/api.py"},
}

EXPECTED_AFFECTED_TESTS: dict[str, set[str]] = {
    "src/shop/storage.py": {"tests/test_storage.py", "tests/test_service.py"},
    "src/shop/service.py": {"tests/test_service.py"},
}


@dataclass(frozen=True)
class FixtureRepo:
    """A built fixture repository and the identity evidence for the run manifest."""

    path: Path
    head_commit: str
    content_digest: str

    def as_evidence(self) -> dict[str, str]:
        return {
            "fixture_path": str(self.path),
            "fixture_head": self.head_commit,
            "fixture_content_sha256": self.content_digest,
        }


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    ).stdout.strip()


def build_fixture_repo(destination: Path) -> FixtureRepo:
    """Create the fixture repo with a small, deterministic Git history.

    Three commits, not one, because E2E-3 requires "a small Git history" and structure
    queries that key on commits need more than an initial import to be meaningful.
    Identity and dates are pinned so the content digest is stable across runs -- E2E-6
    asserts a controlled diff on regeneration, which is untestable if the fixture itself
    changes between runs.
    """

    if destination.exists():
        import shutil

        shutil.rmtree(destination)
    destination.mkdir(parents=True)

    _git(destination, "init", "-q", "-b", "main")
    _git(destination, "config", "user.email", "e2e@menhir.invalid")
    _git(destination, "config", "user.name", "Menhir E2E")
    _git(destination, "config", "commit.gpgsign", "false")

    def write(relative: str) -> None:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_FILES[relative], encoding="utf-8")

    stages = [
        (["pyproject.toml", "README.md", "src/shop/__init__.py", "src/shop/config.py"],
         "feat: project skeleton and configuration"),
        (["src/shop/storage.py", "src/shop/service.py", "tests/test_storage.py", "tests/test_service.py"],
         "feat: storage and service layers with tests"),
        (["src/shop/api.py", "docs/overview.md"],
         "feat: HTTP endpoint and overview documentation"),
    ]
    env_dates = {"GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00"}
    for index, (paths, message) in enumerate(stages):
        for relative in paths:
            write(relative)
            _git(destination, "add", "--", relative)
        subprocess.run(
            ["git", "commit", "-q", "-m", message],
            cwd=str(destination),
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
            env={**_inherited_git_env(), **env_dates,
                 "GIT_AUTHOR_DATE": f"2026-01-0{index + 1}T00:00:00+00:00",
                 "GIT_COMMITTER_DATE": f"2026-01-0{index + 1}T00:00:00+00:00"},
        )

    digest = hashlib.sha256()
    for relative in sorted(_FILES):
        digest.update(relative.encode("utf-8"))
        digest.update(_FILES[relative].encode("utf-8"))

    return FixtureRepo(
        path=destination,
        head_commit=_git(destination, "rev-parse", "HEAD"),
        content_digest=digest.hexdigest(),
    )


def _inherited_git_env() -> dict[str, str]:
    import os

    keep = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE", "LANG")
    return {k: os.environ[k] for k in keep if k in os.environ}
