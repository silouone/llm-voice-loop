#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pytest",
# ]
# ///
"""Deterministic tests for the TTS coalescing debounce (no audio, no network).

Contract under test: given N claims within the window → exactly one play, and it
is the newest claim's; claims > window apart → each plays; a lone claim plays; a
stale/dead token never blocks a later claim. Also the stop.py off-switch gate.
"""

import sys
from pathlib import Path

# tts_coalesce lives beside this file; stop.py lives two dirs up (hooks/).
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent.parent))

import pytest  # noqa: E402

import tts_coalesce  # noqa: E402
from tts_coalesce import coalesce_claim, _read_latest, _write_latest  # noqa: E402
import stop  # noqa: E402


class World:
    """A shared claim file modeled as one mutable holder."""

    def __init__(self, latest=None):
        self.latest = latest

    def write(self, tok):
        self.latest = tok

    def read(self):
        return self.latest


def run_claim(world, token_val, *, window=5, later_writes=()):
    """Run one claim against `world`. During its window, `later_writes` tokens
    fire (simulating overlapping later claims in the burst). Returns True if it
    played."""
    played = []
    coalesce_claim(
        play=lambda: played.append(token_val),
        window=window,
        now_ns=lambda: token_val,
        pid="p",
        read_latest=world.read,
        write_latest=world.write,
        sleep=lambda w: [world.write(f"{t}:p") for t in later_writes],
    )
    return bool(played)


# ── Story 1/2: a burst coalesces to exactly one play — the newest ────────────

def test_burst_of_six_only_newest_plays():
    world = World()
    tokens = list(range(6))
    results = {}
    for i in tokens:
        # earlier fires overlap every later fire; the newest (5) overlaps none
        results[i] = run_claim(world, i, later_writes=tokens[i + 1:])
    assert results == {0: False, 1: False, 2: False, 3: False, 4: False, 5: True}


# ── Story 4: claims > window apart each play ─────────────────────────────────

def test_far_apart_claims_both_play():
    world = World()
    first = run_claim(world, 10)            # its window closes before the next fires
    second = run_claim(world, 20)
    assert first and second


# ── Story 10: a lone claim in a quiet window always plays ────────────────────

def test_lone_claim_plays():
    assert run_claim(World(), 42) is True


# ── Superseded: a later fire during my window drops me ───────────────────────

def test_newer_token_during_window_drops_older():
    world = World()
    assert run_claim(world, 10, later_writes=[99]) is False
    assert world.read() == "99:p"


# ── Story 11: a stale/dead token never blocks a later claim ──────────────────

def test_stale_token_is_overwritten_not_suppressing():
    world = World(latest="1:dead")          # crashed fire left a stale token
    assert run_claim(world, 10) is True     # my write overwrites it; I read my own


# ── Window <= 0 disables coalescing: play now, never sleep ───────────────────

def test_window_zero_plays_immediately_without_sleeping():
    played, slept = [], []
    coalesce_claim(
        play=lambda: played.append(1),
        window=0,
        now_ns=lambda: 1,
        pid="p",
        read_latest=lambda: None,
        write_latest=lambda t: None,
        sleep=lambda w: slept.append(w),
    )
    assert played and not slept


# ── Real file I/O: round-trip + missing-file sentinel ────────────────────────

def test_write_then_read_roundtrip(tmp_path):
    claim = tmp_path / "sub" / "latest"     # parent auto-created
    _write_latest("123:456", claim_file=claim)
    assert _read_latest(claim_file=claim) == "123:456"


def test_read_missing_file_returns_none(tmp_path):
    assert _read_latest(claim_file=tmp_path / "does-not-exist") is None


def test_concurrent_writers_leave_no_temp_files(tmp_path):
    claim = tmp_path / "latest"
    for i in range(20):
        _write_latest(f"{i}:p", claim_file=claim)
    assert _read_latest(claim_file=claim) == "19:p"
    leftover = [p for p in tmp_path.iterdir() if p.name != "latest"]
    assert leftover == []                   # every temp got replaced/cleaned


# ── Story 12: the off-switch gate ────────────────────────────────────────────

@pytest.mark.parametrize("val,expected", [
    (None, True),        # unset → default on (today's behavior)
    ("1", True),
    ("true", True),
    ("on", True),
    ("0", False),
    ("false", False),
    ("no", False),
    ("off", False),
    ("", False),
    ("  OFF  ", False),  # trimmed + case-insensitive
])
def test_tts_enabled_predicate(val, expected):
    env = {} if val is None else {"HIGGS_TTS_ENABLED": val}
    assert stop.tts_enabled(env) is expected


def test_announce_completion_gate_silences_when_off(monkeypatch):
    """The off-switch must stop announce_completion before it spawns higgs_tts."""
    spawned = []
    monkeypatch.setattr(stop.subprocess, "run", lambda *a, **k: spawned.append(a))
    monkeypatch.setattr(stop, "build_recap", lambda d: "recap")

    monkeypatch.setenv("HIGGS_TTS_ENABLED", "0")
    stop.announce_completion({})
    assert spawned == []                    # off → no subprocess spawned

    monkeypatch.delenv("HIGGS_TTS_ENABLED")
    stop.announce_completion({})
    assert len(spawned) == 1                # default on → spawns higgs_tts


# ── Malformed window env is a trust boundary: fall back, don't raise ─────────

@pytest.mark.parametrize("val,expected", [
    (None, 5.0), ("5", 5.0), ("0", 0.0), ("2.5", 2.5),
    ("garbage", 5.0), ("", 5.0),
])
def test_window_seconds_parse(monkeypatch, val, expected):
    if val is None:
        monkeypatch.delenv("HIGGS_COALESCE_WINDOW", raising=False)
    else:
        monkeypatch.setenv("HIGGS_COALESCE_WINDOW", val)
    assert tts_coalesce._window_seconds() == expected


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
