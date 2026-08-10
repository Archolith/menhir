"""Single source of truth for decoding text files read off disk.

Every path that reads a user's file into the graph goes through here, because the
encoding contract is exactly the kind of invariant that rots when it is restated at
each call site.

It rotted once already. Thirteen reads in `project_scanner.py` called
`Path.read_text(errors="replace")` with no `encoding=`, so Python fell back to
`locale.getpreferredencoding()` -- cp1252 on Windows. UTF-8 files decoded through the
wrong codec, and the mojibake was persisted onto Neo4j Entity nodes, where it stayed:
`query_structure("projects")` rendered em-dashes in project descriptions as a
three-character cp1252 sequence. Nobody wrote the wrong encoding on purpose; thirteen
call sites each independently forgot to write the right one.

So: state the contract once, and make the correct read the shortest one to type.

- `utf-8-sig` decodes plain UTF-8 identically to `utf-8`, and additionally strips a
  leading BOM. Reading is always at least as correct with it, never less, so there is
  no call site that wants bare `utf-8`. A leaked BOM is not cosmetic -- U+FEFF at the
  head of a document becomes the first character of an ingested episode body, and
  `json.loads` raises on a BOM-prefixed string outright.
- `errors="replace"` keeps a single undecodable byte from failing an entire project
  scan. We are indexing other people's repositories, not validating them.
"""

from __future__ import annotations

from pathlib import Path

# Decode, never encode. Writing a BOM is a separate decision and not this module's job.
READ_ENCODING = "utf-8-sig"
READ_ERRORS = "replace"


def read_text_utf8(path: Path | str) -> str:
    """Read a file as UTF-8, tolerating a BOM and undecodable bytes.

    Raises `OSError` exactly as `Path.read_text` does, so callers keep their own
    missing-file and size-guard handling.
    """
    return Path(path).read_text(encoding=READ_ENCODING, errors=READ_ERRORS)
