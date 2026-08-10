"""Host-injected blocks must never be stored as things the user said.

THE DEFECT THIS PINS. The host delivers background-task completions, slash-command envelopes and
re-injected context on the SAME `prompt` field as the user's typing. The producer stamps every record
`role='user', declarant='user'`, so all of it was being asserted as user speech. Measured in
production before the fix: 469 of 848 :TurnEvidence records (55.4%) were host text, 463 of them
containing not one word the user typed.

WHY IT DOES NOT STAY A COSMETIC MISLABEL. ADR 0001 makes `declarant` load-bearing precisely so
downstream consumers need not re-litigate it, and `create_evidence_projection` is fail-closed on
`declarant='user'` -- so a task notification passed the guard and became an `:Episodic` stamped
`source='user'`, in the user's name, costing a full enrichment pass.

WHY STRIPPING MUST PRECEDE TRIAGE. 468 of those 469 qualified on triage reason `number`, matching the
hex digits in their own `task-id` and `tool-use-id`. The machine text was not merely stored, it was
what made the turn look worth storing. Stripping after triage would fix neither half.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_COMMON = (Path(__file__).resolve().parents[1]
           / "scripts" / "hooks" / "menhir_turn_evidence_common.py")


def _load():
    spec = importlib.util.spec_from_file_location("menhir_turn_evidence_common", _COMMON)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def hook():
    return _load()


def _build(hook, prompt: str):
    return hook.build_payload(
        {"prompt": prompt, "session_id": "s1", "cwd": None},
        source_kind="k", source_client="c", hook_version="v", triage_version="t",
        git_fn=lambda *a, **k: None,
    )


# A verbatim record from production -- one of the 463 that carried no user text at all.
REAL_NOTIFICATION = """<task-notification>
<task-id>b4fkyn0h3</task-id>
<tool-use-id>toolu_01GfwDDYzupNFfLTQrHFkkgV</tool-use-id>
<output-file>C:\\Users\\dev\\AppData\\Local\\Temp\\claude\\tasks\\b4fkyn0h3.output</output-file>
<status>completed</status>
<summary>Background command "Wait another window for canary deploy" completed (exit code 0)</summary>
</task-notification>"""


# --------------------------------------------------------- nothing the user said => nothing stored

def test_a_pure_task_notification_is_not_captured(hook):
    """The 463-record case. No user text means no user statement to record."""
    assert _build(hook, REAL_NOTIFICATION) is None


def test_a_truncated_notification_is_not_captured(hook):
    """The 6-record case: the block opened and the prompt ended before it closed. Treating the
    remainder as user text would store machine output under the user's name; treating it as host text
    can at worst drop a turn. Absent evidence is recoverable, false evidence is not.

    The two preconditions are asserted, not assumed. An earlier version of this test cut the text
    before the `(exit code 0)` in the summary, leaving only digits embedded in identifiers
    (`toolu_01G`, `b4fkyn0h3`) that `\\b\\d+\\b` cannot match -- so the turn was being dropped by
    triage and the test passed with the unclosed-block handling deleted entirely."""
    truncated = REAL_NOTIFICATION[:REAL_NOTIFICATION.index("</task-notification>")]
    assert "</task-notification>" not in truncated           # genuinely unclosed
    assert hook.triage_user_prompt(truncated)[0] is True     # and would be captured if not stripped
    assert _build(hook, truncated) is None


def test_the_digits_inside_a_notification_no_longer_qualify_it(hook):
    """Directly pins the mechanism: 468 of 469 got in on reason `number`, from their own hex ids.
    Triage must never see this text."""
    assert hook.triage_user_prompt(REAL_NOTIFICATION)[0] is True   # would have qualified raw...
    assert hook.strip_harness_blocks(REAL_NOTIFICATION) == ""      # ...and never reaches triage


# ------------------------------------------------------------- user text survives, host text does not

def test_user_text_after_a_block_survives_and_is_stored_alone(hook):
    payload = _build(hook, REAL_NOTIFICATION + "\n\nI have 25 postcards")
    assert payload is not None
    assert payload["text"] == "I have 25 postcards"


def test_the_stored_text_contains_no_host_text(hook):
    """The declarant contract in one assertion: what is stamped `declarant='user'` is only what the
    user typed."""
    payload = _build(hook, REAL_NOTIFICATION + "\n\nI have 25 postcards")
    for leaked in ("task-notification", "b4fkyn0h3", "toolu_01", "canary deploy", "exit code"):
        assert leaked not in payload["text"]


def test_the_prompt_hash_covers_what_was_actually_sent(hook):
    """metadata.prompt_hash is the correlation handle for the stored record, so it must be derived
    from the stripped text -- a hash of text the server never receives correlates with nothing."""
    payload = _build(hook, REAL_NOTIFICATION + "\n\nI have 25 postcards")
    assert payload["metadata"]["prompt_hash"] == hook.prompt_hash("I have 25 postcards")


# ---------------------------------------------------------------------------- stripping correctness

def test_every_known_harness_tag_is_stripped(hook):
    """Guards the table itself: a tag listed but not actually matched by the regex would be a silent
    hole, since the only symptom is a record that looks plausible."""
    for tag in hook._HARNESS_TAGS:
        assert hook.strip_harness_blocks(f"<{tag}>machine text</{tag}>") == "", tag


def test_a_block_removed_mid_sentence_does_not_fuse_the_words_around_it(hook):
    out = hook.strip_harness_blocks("I own<system-reminder>x</system-reminder>25 postcards")
    assert out == "I own 25 postcards"


def test_multiple_blocks_are_all_removed(hook):
    out = hook.strip_harness_blocks(
        f"{REAL_NOTIFICATION}\nfirst\n{REAL_NOTIFICATION}\nsecond")
    assert out.split() == ["first", "second"]


def test_a_mismatched_close_tag_cannot_swallow_the_users_words(hook):
    """The close tag is backreferenced to the tag that opened. Without that, this `</command-args>`
    would terminate the `<system-reminder>` and take the user's sentence with it."""
    out = hook.strip_harness_blocks(
        "<system-reminder>machine</command-args> I have 25 postcards</system-reminder> real words")
    assert "I have 25 postcards" not in out  # inside the block, correctly removed
    assert out == "real words"


def test_the_slash_command_envelope_is_stripped_but_the_request_is_kept(hook):
    """The real shape of a `/compact` turn: host preamble, then what the user actually asked for."""
    prompt = ("<local-command-caveat>Caveat: generated while running local commands."
              "</local-command-caveat>\n<command-name>/compact</command-name>\n"
              "<local-command-stdout>Compacted 42 messages</local-command-stdout>\n"
              "I have 25 postcards")
    payload = _build(hook, prompt)
    assert payload["text"] == "I have 25 postcards"


# ------------------------------------------------------------------------------------- no regression

def test_an_ordinary_user_prompt_is_untouched(hook):
    payload = _build(hook, "I have 25 postcards")
    assert payload["text"] == "I have 25 postcards"
    assert "number" in payload["triage_reason"]


def test_angle_brackets_that_are_not_harness_tags_survive(hook):
    """Stripping keys on a fixed tag table, not on '<' -- the user writes code."""
    assert hook.strip_harness_blocks("use <div> for 25 items") == "use <div> for 25 items"


def test_a_boring_prompt_is_still_dropped_by_triage(hook):
    assert _build(hook, "continue") is None


@pytest.mark.parametrize("value", ["", None])
def test_empty_input_is_handled(hook, value):
    assert hook.strip_harness_blocks(value) == ""


# ----------------------------------------------------------------------------------------- dry run

def test_dry_run_reports_the_stripped_verdict(hook):
    """A dry run that triaged the raw prompt would promise captures that will not happen."""
    out = hook.format_dry_run({"prompt": REAL_NOTIFICATION}, source_client="c", triage_version="t")
    assert "would_capture: false" in out
    assert f"harness_stripped_chars: {len(REAL_NOTIFICATION)}" in out
