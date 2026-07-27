#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pytest",
# ]
# ///
"""Tests for the shared recap builder and the sentence-aware speech budget.

Contracts: past the soft cap the current sentence may finish (up to the hard
ceiling); word-boundary cutting is the last resort for a terminator-less
monster; a budget break never leaves a trailing half-thought fragment.
"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import pytest  # noqa: E402

from recap import recap_body, build_recap  # noqa: E402
from higgs_tts import truncate_for_speech  # noqa: E402


# ── truncate_for_speech: soft target, hard ceiling, finish the sentence ──────

def test_under_soft_untouched():
    assert truncate_for_speech("Short and done.", 220, 400) == "Short and done."

def test_sentence_in_progress_finishes_past_soft():
    body = ("alpha " * 40).strip() + " omega."          # terminator at ~246
    text = body + " " + ("tail " * 10).strip()
    out = truncate_for_speech(text, 220, 400)
    assert out == body                                   # finished, tail dropped
    assert len(out) > 220                                # allowed past soft

def test_no_terminator_but_within_hard_returns_whole():
    text = ("beta " * 60).strip()                        # 299 chars, no terminator
    assert truncate_for_speech(text, 220, 400) == text

def test_monster_sentence_word_cuts_at_hard():
    text = ("gamma " * 100).strip()                      # ~599 chars, no terminator
    out = truncate_for_speech(text, 220, 400)
    assert out == text[:400].rsplit(" ", 1)[0].strip()
    assert len(out) <= 400 and out.endswith("gamma")

def test_falls_back_to_sentence_end_in_second_half_of_soft():
    lead = ("aa " * 60).strip() + "."                    # 180 chars, ends a sentence
    text = lead + " " + ("bb " * 200).strip()            # then a terminator-less flood
    assert truncate_for_speech(text, 220, 400) == lead


# ── recap_body: budget break never ends on a pseudo-sentence fragment ────────

S1 = "Fixed the parser bug in the tokenizer module today."

def test_budget_break_drops_trailing_fragment():
    frag = "- " + ("cleanup " * 20).strip()              # bullet with no terminator
    s3 = ("Another long bullet sentence that clearly overflows the two hundred "
          "twenty character budget for spoken recaps.")
    out = recap_body(f"{S1}\n{frag}\n{s3}")
    assert out == S1                                     # fragment dropped, not spoken

def test_fragment_kept_when_nothing_was_cut():
    out = recap_body(f"{S1}\n- final fragment without any period marker here")
    assert out.endswith("final fragment without any period marker here.")

def test_lone_fragment_opening_always_kept():
    text = ("mega " * 60).strip()                        # single huge fragment line
    out = recap_body(text)
    assert out == text + "."                             # opening survives the cap


# ── build_recap: prefix, payload key variants, acronym guard ─────────────────

def test_name_prefix_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HIGGS_RECAP_NAME", "Silou")
    out = build_recap({"cwd": str(tmp_path), "last_assistant_message": "All tests pass."})
    assert out == f"Silou, here on {tmp_path.name}, all tests pass."

def test_anonymous_prefix_without_env(tmp_path, monkeypatch):
    monkeypatch.delenv("HIGGS_RECAP_NAME", raising=False)
    out = build_recap({"cwd": str(tmp_path), "last_assistant_message": "All tests pass."})
    assert out == f"Here on {tmp_path.name}, all tests pass."

def test_codex_hyphenated_payload_key(tmp_path, monkeypatch):
    monkeypatch.delenv("HIGGS_RECAP_NAME", raising=False)
    out = build_recap({"cwd": str(tmp_path), "last-assistant-message": "Refactor done, tests green."})
    assert out.endswith("refactor done, tests green.")

def test_empty_message_says_turn_complete(tmp_path, monkeypatch):
    monkeypatch.delenv("HIGGS_RECAP_NAME", raising=False)
    out = build_recap({"cwd": str(tmp_path)})
    assert out == f"Here on {tmp_path.name}, turn complete."

def test_leading_acronym_not_lowercased(tmp_path, monkeypatch):
    monkeypatch.delenv("HIGGS_RECAP_NAME", raising=False)
    out = build_recap({"cwd": str(tmp_path), "last_assistant_message": "TTS cap fixed for good."})
    assert ", TTS cap fixed for good." in out


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
