from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from deploy.release_notes import (
    MAX_DETAILS_LENGTH,
    ReleaseNoteError,
    SECURITY_SCOPES,
    collect_fragments,
    load_fragment,
    main,
    render_json,
    render_markdown,
)


COMMITS = {
    "menhir": "0" * 40,
    "archolith_oauth": "1" * 40,
    "yawn_deploy": "2" * 40,
    "yawn_vps": "3" * 40,
}


def fragment(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema": 1,
        "id": "example-change",
        "category": "changed",
        "deployment_class": "app-only",
        "summary": "Example change.",
        "details": "The complete change description.",
        "operator_impact": "No operator action is required.",
        "repositories": {"menhir": [COMMITS["menhir"]]},
        "security_scopes": ["runtime-hardening-and-observability"],
        "breaking": False,
    }
    value.update(changes)
    return value


def write_fragment(path: Path, value: dict[str, object]) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def test_collect_and_render_are_deterministic(tmp_path: Path) -> None:
    fragments = tmp_path / "fragments"
    fragments.mkdir()
    write_fragment(
        fragments / "z.json",
        fragment(
            id="z-fix",
            category="fixed",
            repositories={"yawn_vps": [COMMITS["yawn_vps"]]},
            security_scopes=["secret-handling"],
        ),
    )
    write_fragment(
        fragments / "b.json",
        fragment(
            id="b-change",
            repositories={
                "yawn_deploy": [COMMITS["yawn_deploy"]],
                "menhir": [COMMITS["menhir"]],
            },
            security_scopes=[
                "authentication-and-oauth-authority",
                "runtime-hardening-and-observability",
            ],
        ),
    )
    write_fragment(
        fragments / "a.json",
        fragment(
            id="a-change",
            repositories={"archolith_oauth": [COMMITS["archolith_oauth"]]},
        ),
    )

    collected = collect_fragments(fragments)
    assert [item.id for item in collected] == ["a-change", "b-change", "z-fix"]
    assert render_markdown(tuple(reversed(collected))) == render_markdown(collected)
    assert render_json(tuple(reversed(collected))) == render_json(collected)
    rendered = json.loads(render_json(collected))
    assert rendered["release_id"] is None
    assert [item["id"] for item in rendered["fragments"]] == [
        "a-change",
        "b-change",
        "z-fix",
    ]
    assert list(rendered["fragments"][1]["repositories"]) == [
        "menhir",
        "yawn_deploy",
    ]


def test_render_can_bind_release_id(tmp_path: Path) -> None:
    path = tmp_path / "fragment.json"
    write_fragment(path, fragment())
    fragments = (load_fragment(path),)

    rendered_json = json.loads(render_json(fragments, "menhir-prod-0.2.0-11"))

    assert rendered_json["release_id"] == "menhir-prod-0.2.0-11"
    assert render_markdown(fragments, "menhir-prod-0.2.0-11").startswith(
        "# menhir-prod-0.2.0-11\n"
    )


def test_render_rejects_unsafe_release_id(tmp_path: Path) -> None:
    path = tmp_path / "fragment.json"
    write_fragment(path, fragment())

    with pytest.raises(ReleaseNoteError, match="release_id"):
        render_json((load_fragment(path),), "../../release")


@pytest.mark.parametrize(
    "content",
    [
        "{",
        '{"schema": 1, "schema": 1}',
    ],
)
def test_malformed_json_and_duplicate_keys_are_rejected(
    tmp_path: Path, content: str
) -> None:
    path = tmp_path / "bad.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ReleaseNoteError):
        load_fragment(path)


def test_duplicate_fragment_ids_are_rejected(tmp_path: Path) -> None:
    write_fragment(tmp_path / "one.json", fragment())
    write_fragment(tmp_path / "two.json", fragment())
    with pytest.raises(ReleaseNoteError, match="duplicate fragment id"):
        collect_fragments(tmp_path)


def test_duplicate_commits_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "duplicate.json"
    write_fragment(
        path,
        fragment(
            repositories={
                "menhir": [COMMITS["menhir"]],
                "yawn_deploy": [COMMITS["menhir"]],
            }
        ),
    )
    with pytest.raises(ReleaseNoteError, match="duplicate commit"):
        load_fragment(path)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"extra": True}, "unknown keys"),
        ({"category": "removed"}, "category"),
        ({"deployment_class": "direct"}, "deployment_class"),
        ({"id": "../escape"}, "safe lowercase slug"),
        ({"repositories": {"other": [COMMITS["menhir"]]}}, "unknown names"),
        ({"security_scopes": ["unknown-scope"]}, "unknown security scope"),
        ({"summary": None}, "summary"),
        ({"details": "x" * (MAX_DETAILS_LENGTH + 1)}, "details exceeds"),
        (
            {
                "repositories": {
                    "menhir": [f"{index:040x}" for index in range(101)]
                }
            },
            "exceeds 100 commits",
        ),
        ({"security_scopes": [*SECURITY_SCOPES, SECURITY_SCOPES[0]]}, "exceeds 8"),
        ({"security_scopes": []}, "nonempty"),
        (
            {
                "security_scopes": [
                    "runtime-hardening-and-observability",
                    "authentication-and-oauth-authority",
                ]
            },
            "must be sorted",
        ),
        (
            {
                "security_scopes": [
                    "runtime-hardening-and-observability",
                    "runtime-hardening-and-observability",
                ]
            },
            "duplicates",
        ),
    ],
)
def test_unknown_null_empty_oversized_and_unsorted_values_are_rejected(
    tmp_path: Path, changes: dict[str, object], message: str
) -> None:
    path = tmp_path / "bad.json"
    write_fragment(path, fragment(**changes))
    with pytest.raises(ReleaseNoteError, match=message):
        load_fragment(path)


def test_missing_key_is_rejected(tmp_path: Path) -> None:
    value = fragment()
    del value["details"]
    path = tmp_path / "bad.json"
    write_fragment(path, value)
    with pytest.raises(ReleaseNoteError, match="missing keys"):
        load_fragment(path)


def test_non_json_extra_file_is_rejected(tmp_path: Path) -> None:
    write_fragment(tmp_path / "valid.json", fragment())
    (tmp_path / "README.md").write_text("unexpected", encoding="utf-8")
    with pytest.raises(ReleaseNoteError, match="unknown entry"):
        collect_fragments(tmp_path)


def test_symlink_input_is_rejected_when_supported(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    link = tmp_path / "link.json"
    write_fragment(target, fragment())
    try:
        os.symlink(target, link)
    except (NotImplementedError, OSError):
        pytest.skip("symlinks are not available")
    with pytest.raises(ReleaseNoteError, match="symlink"):
        load_fragment(link)


def test_cli_refuses_overwrite_without_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fragments = tmp_path / "fragments"
    fragments.mkdir()
    write_fragment(fragments / "valid.json", fragment())
    output = tmp_path / "notes.json"
    output.write_text("keep me", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert main(["render", "fragments", "notes.json", "--format", "json"]) == 2
    assert output.read_text(encoding="utf-8") == "keep me"
    assert main(
        ["render", "fragments", "notes.json", "--format", "json", "--overwrite"]
    ) == 0
    assert json.loads(output.read_text(encoding="utf-8"))["schema"] == 1


@pytest.mark.parametrize("output", ["../notes.json", "/tmp/notes.json", "C:/notes.json"])
def test_cli_rejects_traversal_and_absolute_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    output: str,
) -> None:
    fragments = tmp_path / "fragments"
    fragments.mkdir()
    write_fragment(fragments / "valid.json", fragment())
    monkeypatch.chdir(tmp_path)
    assert main(["render", "fragments", output, "--format", "json"]) == 2
