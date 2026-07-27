#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "python-dotenv",
# ]
# ///
"""Claude Code Stop hook: speak a short recap of the turn via Higgs Audio TTS.

The recap is COMPUTED from the final assistant message — deterministic, no
model call, no extra tokens — by the shared recap module (utils/tts/recap.py,
also used by the Codex notify hook). Wire it in settings.json:

    "Stop": [{"hooks": [{"type": "command",
        "command": "uv run ~/.claude/hooks/stop.py --notify"}]}]
"""

import argparse
import json
import os
import sys
import subprocess
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv(Path.home() / ".claude" / ".env")
except ImportError:
    pass  # dotenv is optional

TTS_DIR = Path(__file__).resolve().parent / "utils" / "tts"
HIGGS_TTS = TTS_DIR / "higgs_tts.py"
sys.path.insert(0, str(TTS_DIR))

from recap import build_recap  # noqa: E402


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
