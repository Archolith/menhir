"""Entry point for menhir CLI."""

from __future__ import annotations


def main() -> None:
    """Launch the Typer CLI app."""
    from menhir.cli import app

    app()


if __name__ == "__main__":
    main()
