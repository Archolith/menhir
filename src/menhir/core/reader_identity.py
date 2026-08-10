"""Transport-neutral reader identity normalization."""


def normalize_reader_id(reader_id: str | None) -> str:
    """Return a stable non-empty reader identifier."""

    normalized = (reader_id or "").strip()
    return normalized or "default"


__all__ = ["normalize_reader_id"]
