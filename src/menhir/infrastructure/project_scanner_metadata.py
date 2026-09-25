"""Project description and dependency parsing for the project scanner.

Moved verbatim from ``project_scanner.py``; the facade module re-exports every name here.
"""

from __future__ import annotations

import re
from pathlib import Path

from menhir.infrastructure.text_io import read_text_utf8

# ---------------------------------------------------------------------------
# Description reading
# ---------------------------------------------------------------------------

def _read_project_description(root: Path) -> str:
    # Try .agent/README.md first
    agent_readme = root / ".agent" / "README.md"
    if agent_readme.is_file():
        try:
            text = read_text_utf8(agent_readme)[:1000]
            # Extract first meaningful paragraph
            return _first_paragraph(text)
        except OSError:
            pass

    # Fall back to CLAUDE.md
    claude_md = root / "CLAUDE.md"
    if claude_md.is_file():
        try:
            text = read_text_utf8(claude_md)[:1000]
            return _first_paragraph(text)
        except OSError:
            pass

    return ""


def _first_paragraph(text: str) -> str:
    lines: list[str] = []
    in_content = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            if in_content:
                break
            continue
        if stripped.startswith("#"):
            if in_content:
                break
            continue
        in_content = True
        lines.append(stripped)
    result = " ".join(lines)
    return result[:500] if len(result) > 500 else result


# ---------------------------------------------------------------------------
# Dependency parsing
# ---------------------------------------------------------------------------

def _parse_dependencies(root: Path, stack: str) -> list[str]:
    if stack == "python":
        return _parse_python_deps(root)
    if stack in ("typescript", "javascript"):
        return _parse_node_deps(root)
    if stack in ("java", "kotlin"):
        return _parse_gradle_deps(root)
    return []


def _parse_python_deps(root: Path) -> list[str]:
    # Try pyproject.toml first
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        try:
            text = read_text_utf8(pyproject)
            deps: list[str] = []

            # Strategy 1: PEP 621 inline list — dependencies = ["pkg>=1.0", ...]
            # Collect everything between `dependencies = [` and the closing `]`
            in_inline_deps = False
            for line in text.splitlines():
                stripped = line.strip()
                if re.match(r"^dependencies\s*=\s*\[", stripped):
                    in_inline_deps = True
                    # May have items on the same line
                    for m in re.finditer(r'"([a-zA-Z0-9_][a-zA-Z0-9_.+-]*)', stripped):
                        deps.append(re.split(r"[>=<!\[;,\s]", m.group(1))[0])
                    if "]" in stripped.split("[", 1)[-1]:
                        in_inline_deps = False
                    continue
                if in_inline_deps:
                    if "]" in stripped:
                        # Last line of the list — parse items before ]
                        for m in re.finditer(r'"([a-zA-Z0-9_][a-zA-Z0-9_.+-]*)', stripped):
                            deps.append(re.split(r"[>=<!\[;,\s]", m.group(1))[0])
                        in_inline_deps = False
                        continue
                    m = re.match(r'^"([a-zA-Z0-9_][a-zA-Z0-9_.+-]*)', stripped)
                    if m:
                        deps.append(re.split(r"[>=<!\[;,\s]", m.group(1))[0])

            # Strategy 2: section-based — [project.dependencies] or [dependency-groups]
            if not deps:
                in_section = False
                for line in text.splitlines():
                    if re.match(r"^\[.*dependencies.*\]", line, re.IGNORECASE):
                        in_section = True
                        continue
                    if in_section and line.startswith("["):
                        break
                    if in_section:
                        m = re.match(r'^"?([a-zA-Z0-9_-]+)', line.strip())
                        if m:
                            deps.append(m.group(1))

            if deps:
                return deps
        except OSError:
            pass

    # Fall back to requirements.txt
    reqs = root / "requirements.txt"
    if reqs.is_file():
        try:
            deps = []
            for line in read_text_utf8(reqs).splitlines():
                line = line.strip()
                if not line or line.startswith("#") or line.startswith("-"):
                    continue
                m = re.match(r"^([a-zA-Z0-9_-]+)", line)
                if m:
                    deps.append(m.group(1))
            return deps
        except OSError:
            pass
    return []


def _parse_node_deps(root: Path) -> list[str]:
    pkg = root / "package.json"
    if not pkg.is_file():
        return []
    try:
        import json
        data = json.loads(read_text_utf8(pkg))
        deps: list[str] = []
        for key in ("dependencies", "devDependencies"):
            section = data.get(key, {})
            if isinstance(section, dict):
                deps.extend(section.keys())
        return deps
    except (OSError, json.JSONDecodeError):
        return []


def _parse_gradle_deps(root: Path) -> list[str]:
    for name in ("build.gradle", "build.gradle.kts"):
        gf = root / name
        if not gf.is_file():
            continue
        try:
            text = read_text_utf8(gf)
            deps: list[str] = []
            for m in re.finditer(r"""(?:implementation|api|compileOnly|runtimeOnly)\s*[\('"]\s*([^'")\s]+)""", text):
                deps.append(m.group(1).split(":")[0] if ":" in m.group(1) else m.group(1))
            return deps
        except OSError:
            pass
    return []
