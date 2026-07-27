#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "python-dotenv",
# ]
# ///
"""Claude Code Stop hook: speak a short recap of the turn via Higgs Audio TTS.

The recap is COMPUTED from the final assistant message — deterministic, no
model call, no extra tokens. Wire it in settings.json:

    "Stop": [{"hooks": [{"type": "command",
        "command": "uv run ~/.claude/hooks/stop.py --notify"}]}]
"""

import argparse
import json
import os
import re
import sys
import subprocess
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv(Path.home() / ".claude" / ".env")
except ImportError:
    pass  # dotenv is optional

HIGGS_TTS = Path(__file__).parent / "utils" / "tts" / "higgs_tts.py"


def repo_name(cwd):
    """Basename of the git repo root at *cwd*, else the cwd basename."""
    try:
        root = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5,
        )
        if root.returncode == 0 and root.stdout.strip():
            return Path(root.stdout.strip()).name.lstrip(".") or "this repo"
    except (subprocess.SubprocessError, OSError):
        pass
    return Path(cwd).name.lstrip(".") or "this repo"


RECAP_CHAR_CAP = 220  # ~2 short sentences ≈ 15s of audio at 1.15x — a notification, not a report


def _clean_line(raw):
    """Strip list/header markers, inline markdown, and unspeakable tokens
    (URLs, UUIDs, hex ids, long digit runs) from a line for speech."""
    line = raw.strip().lstrip("#>-*• \t")
    # Unwrap markdown BEFORE stripping symbols, else the symbol strip eats the
    # []`* delimiters first and these subs never fire (URL would leak to speech).
    line = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", line)      # [t](u) -> t
    # Bare URLs must die WHOLE, before the symbol strip mangles them into
    # speakable garbage ("https:developers.cloudflare.com…").
    line = re.sub(r"(?:https?://|www\.)[^\s)\]>\"']+", " ", line)  # stop before closing delims
    line = re.sub(r"\*\*(.+?)\*\*", r"\1", line)              # **bold** -> bold
    line = re.sub(r"`([^`]+)`", r"\1", line)                  # `code` -> code
    # IDs are noise when spoken: UUIDs, hex ids (must contain a digit, so rare
    # all-letter words like "effaced" survive), digit runs of 5+ digits.
    # Small human numbers ("5 files", "24h", "1,000") stay.
    line = re.sub(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
                  r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b", " ", line)
    line = re.sub(r"\b(?=[0-9a-fA-F]*\d)[0-9a-fA-F]{7,}\b", " ", line)
    line = re.sub(r"\b\d(?:[.,]?\d){4,}\w*\b", " ", line)     # \w* eats unit suffixes ("86,400s")
    line = re.sub(r"[^\w\s.,;:!?'\"()—-]", "", line)          # strip leftover emoji/symbols
    line = re.sub(r"\(\s*\)", " ", line)                      # parens left empty by the scrubs
    return line.strip()


def _sentences(line):
    """Split a cleaned line into sentences, keeping filenames intact.

    A terminator only ends a sentence when followed by whitespace or end-of-
    line, so `pre_tool_use.py` / `settings.json` never split mid-token.
    """
    return [p.strip() for p in re.findall(r".+?[.!?](?:\s|$)|.+$", line) if p.strip()]


def recap_body(message):
    """Speakable multi-sentence recap of the final assistant message.

    Deterministic, no model call. Drops fenced code blocks, then prefers a
    markdown headline (the first `#`/`##` line) as the opening — it states the
    turn's result and lets us skip any commit/meta preamble above it — falling
    back to the first substantive line when there's no headline. Accumulates
    following prose sentences up to RECAP_CHAR_CAP. Trailing git-diff / summary
    blocks sit at the bottom, so top-down extraction never reaches them.
    """
    if not message:
        return ""
    text = re.sub(r"```.*?```", " ", message, flags=re.DOTALL)  # drop code fences
    lines = text.splitlines()

    # Start at the first markdown headline if there is one; otherwise the top.
    start = next((i for i, raw in enumerate(lines) if raw.lstrip().startswith("#")), 0)

    candidates = []
    for raw in lines[start:]:
        line = _clean_line(raw)
        if len(re.sub(r"[^A-Za-z]", "", line)) < 12:
            continue  # skip blank / decorative / too-short lines
        for s in _sentences(line):
            # Number-dense fragments (enumerations, spec/ID soup) are
            # unlistenable — speak prose only.
            if len(re.findall(r"\d", s)) * 2 > len(re.findall(r"[A-Za-z]", s)):
                continue
            candidates.append(s if s[-1] in ".!?" else s + ".")  # terminator = a spoken pause

    out, total = [], 0
    for s in candidates:
        if out and total + len(s) + 1 > RECAP_CHAR_CAP:
            break  # always keep the opening, then stop at the cap
        out.append(s)
        total += len(s) + 1
    return re.sub(r"\s+", " ", " ".join(out)).strip()  # collapse gaps left by stripped symbols


def build_recap(input_data):
    """'<Name>, here on <repo>, <lead sentence>' — computed, never model-crafted.

    Set HIGGS_RECAP_NAME to your first name to be addressed personally.
    """
    cwd = input_data.get("cwd") or os.getcwd()
    repo = repo_name(cwd)
    # `last_assistant_message` IS present in the live Stop payload (verified
    # 2026-07 against real Stop-hook logs) even though it's absent from the
    # documented schema — keep it as the primary source; transcript is fallback.
    message = input_data.get("last_assistant_message") or last_message_from_transcript(
        input_data.get("transcript_path")
    )
    frag = recap_body(message)
    name = os.getenv("HIGGS_RECAP_NAME", "").strip()
    prefix = f"{name}, here on {repo}, " if name else f"Here on {repo}, "
    if not frag:
        return prefix + "turn complete."
    return prefix + frag[0].lower() + frag[1:]


def last_message_from_transcript(transcript_path):
    """Fallback source: last assistant text block from the transcript .jsonl."""
    if not transcript_path or not os.path.exists(transcript_path):
        return ""
    last = ""
    try:
        with open(transcript_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") != "assistant":
                    continue
                msg = obj.get("message", {})
                content = msg.get("content") if isinstance(msg, dict) else None
                if isinstance(content, list):
                    texts = [c.get("text", "") for c in content
                             if isinstance(c, dict) and c.get("type") == "text"]
                    if any(t.strip() for t in texts):
                        last = "\n".join(texts)
    except OSError:
        return ""
    return last


def tts_enabled(env=None):
    """Whether the TTS recap should speak. Default on (unset = speak).

    A real, reversible off-switch the hook honors: HIGGS_TTS_ENABLED set to a
    falsey value silences recaps. (settings.json `voiceEnabled` is Claude Code's
    built-in-voice flag and never reaches this custom path.)
    """
    env = os.environ if env is None else env
    val = env.get("HIGGS_TTS_ENABLED")
    if val is None:
        return True
    return val.strip().lower() not in ("0", "false", "no", "off", "")


def announce_completion(input_data):
    """Speak the location-prefixed recap via the single higgs_tts.py path.

    higgs_tts.py self-detaches in ~0.1s (background generation + playback, with
    its own macOS `say` offline fallback), so this call never blocks the turn.
    """
    if not tts_enabled():
        return
    try:
        recap = build_recap(input_data)
        subprocess.run(
            ["uv", "run", str(HIGGS_TTS), recap],
            capture_output=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, subprocess.SubprocessError, FileNotFoundError):
        pass  # Fail silently if TTS encounters issues
    except Exception:
        pass


def main():
    try:
        parser = argparse.ArgumentParser()
        parser.add_argument(
            "--notify", action="store_true", help="Announce completion via TTS"
        )
        args = parser.parse_args()

        # Read JSON input from stdin
        input_data = json.load(sys.stdin)

        if args.notify:
            # Background agents/teammates run as their own sessions and fire
            # this same Stop hook — their payload carries `agent_type`, a real
            # user session's never does (verified against real hook logs).
            # Only the user-facing session speaks; a parallel fan-out would
            # otherwise stack one audio per agent.
            if not input_data.get("agent_type"):
                announce_completion(input_data)

        sys.exit(0)

    except json.JSONDecodeError:
        sys.exit(0)
    except Exception:
        # A Stop hook must never break the turn
        sys.exit(0)


if __name__ == "__main__":
    main()
