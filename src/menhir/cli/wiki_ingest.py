"""`menhir ingest-wiki`: bulk-ingest compiled sage-wiki articles into the memory graph.

Extracted verbatim from `menhir/cli/__init__.py`; that package re-exports
`ingest_wiki` so existing `from menhir.cli import ...` sites keep working.
"""

from __future__ import annotations

import typer

from menhir.infrastructure.logging_config import configure_logging
from menhir.infrastructure.text_io import read_text_utf8


def ingest_wiki(
    wiki_dir: str = typer.Argument(..., help="Path to sage-wiki wiki/ directory"),
    project: str | None = typer.Option(
        None, help="Project label (default: parent dir name)"
    ),
    identity_action: str | None = typer.Option(
        None,
        "--identity-action",
        help="Resolve a project identity decision with 'adopt' or 'new'.",
    ),
    adopt_project_id: str | None = typer.Option(
        None,
        "--adopt-project-id",
        help="Existing project ID to use with --identity-action adopt.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="List files without ingesting"
    ),
) -> None:
    """Ingest compiled sage-wiki articles into the memory graph."""
    from pathlib import Path

    configure_logging()

    wiki_path = Path(wiki_dir)
    if not wiki_path.exists():
        raise typer.BadParameter(f"Directory not found: {wiki_dir}")

    project_name = project or wiki_path.parent.name

    # Walk wiki/concepts/ and wiki/summaries/
    concepts_dir = wiki_path / "concepts"
    summaries_dir = wiki_path / "summaries"

    files_to_ingest: list[tuple[str, str]] = []  # (path, doc_type)

    for source_dir in [concepts_dir, summaries_dir]:
        if not source_dir.exists():
            continue
        for md_file in source_dir.glob("**/*.md"):
            # Read frontmatter to detect document_shape
            try:
                content = read_text_utf8(md_file)
                frontmatter = {}
                if content.startswith("---"):
                    end = content.find("---", 3)
                    if end > 0:
                        for line in content[3:end].strip().split("\n"):
                            if ":" in line:
                                key, val = line.split(":", 1)
                                frontmatter[key.strip()] = val.strip()
                doc_shape = frontmatter.get("document_shape", "wiki")
                doc_type = (
                    "reference_article" if doc_shape == "reference" else "wiki_article"
                )
                files_to_ingest.append((str(md_file.resolve()), doc_type))
            except Exception:
                pass

    if dry_run:
        typer.echo(
            f"[DRY RUN] Would ingest {len(files_to_ingest)} files as {project_name}:"
        )
        for path, dtype in files_to_ingest[:10]:
            typer.echo(f"  [{dtype}] {path}")
        return

    # Import and ingest
    import asyncio

    from menhir.config import MemorySettings
    from menhir.core.backend_impl import BackendClient

    settings = MemorySettings.from_env()

    async def run_ingest():
        base_url = f"http://{settings.api_host}:{settings.api_port}"
        backend = BackendClient(base_url, settings=settings)
        ingested = 0
        unresolved: list[tuple[str, dict[str, object]]] = []
        errors = 0
        try:
            for file_path, doc_type in files_to_ingest:
                try:
                    result = await backend.ingest_document(
                        file_path,
                        project=project_name,
                        session_id="cli",
                        user_id="cli",
                        document_type=doc_type,
                        identity_action=identity_action,
                        adopt_project_id=adopt_project_id,
                    )
                    if result.get("entity_written") is True:
                        ingested += 1
                    elif result.get("status") == "needs_decision":
                        unresolved.append((file_path, result))
                    else:
                        errors += 1
                except Exception:
                    errors += 1
        finally:
            await backend.aclose()
        return ingested, unresolved, errors

    ingested, unresolved, errors = asyncio.run(run_ingest())
    if unresolved:
        import json

        typer.echo(f"Identity decision required for {len(unresolved)} document(s):")
        for file_path, decision in unresolved:
            typer.echo(f"  {file_path}")
            typer.echo(json.dumps(decision, indent=2, sort_keys=True))
    typer.echo(
        f"Ingested {ingested} documents ({len(unresolved)} unresolved, {errors} errors) "
        f"for project '{project_name}'."
    )
    if unresolved or errors:
        raise typer.Exit(1)
