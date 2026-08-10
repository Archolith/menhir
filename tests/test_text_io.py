"""Tests for infrastructure.text_io — the single decode contract for file reads."""

from __future__ import annotations

import json

import pytest

from menhir.infrastructure.text_io import READ_ENCODING, read_text_utf8


@pytest.mark.unit
class TestReadTextUtf8:
    def test_decodes_utf8_regardless_of_platform_locale(self, tmp_path):
        """The original defect: no encoding= meant cp1252 on Windows."""
        p = tmp_path / "doc.md"
        p.write_bytes("Route by task — do not preload.".encode("utf-8"))

        assert read_text_utf8(p) == "Route by task — do not preload."

    def test_strips_leading_bom(self, tmp_path):
        p = tmp_path / "doc.md"
        p.write_bytes(b"\xef\xbb\xbf" + "# Title".encode("utf-8"))

        out = read_text_utf8(p)

        assert out == "# Title"
        assert not out.startswith("﻿")

    def test_bom_does_not_break_json_parsing(self, tmp_path):
        """A BOM-prefixed string raises in json.loads; utf-8-sig is what prevents it."""
        p = tmp_path / "package.json"
        p.write_bytes(b"\xef\xbb\xbf" + b'{"name": "demo"}')

        assert json.loads(read_text_utf8(p)) == {"name": "demo"}

    def test_bom_does_not_break_frontmatter_detection(self, tmp_path):
        """`content.startswith("---")` silently fails when a BOM leads the string."""
        p = tmp_path / "doc.md"
        p.write_bytes(b"\xef\xbb\xbf" + b"---\ntitle: x\n---\nbody\n")

        assert read_text_utf8(p).startswith("---")

    def test_undecodable_bytes_do_not_raise(self, tmp_path):
        """One bad byte must not fail a whole project scan."""
        p = tmp_path / "weird.txt"
        p.write_bytes(b"before \xff\xfe after")

        out = read_text_utf8(p)

        assert "before" in out and "after" in out

    def test_accepts_str_path(self, tmp_path):
        p = tmp_path / "doc.md"
        p.write_bytes(b"hello")

        assert read_text_utf8(str(p)) == "hello"

    def test_missing_file_still_raises_oserror(self, tmp_path):
        """Callers keep their own try/except OSError guards, so this must not be swallowed."""
        with pytest.raises(OSError):
            read_text_utf8(tmp_path / "nope.md")

    def test_contract_is_bom_tolerant_utf8(self):
        """Pin the constant: plain 'utf-8' would silently reintroduce the BOM leak."""
        assert READ_ENCODING == "utf-8-sig"
