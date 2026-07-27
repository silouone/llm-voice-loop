"""Per-repo TTS voice resolution for higgs_tts.py.

Config lives at ~/.claude/tts-voices.json:
    {"default": "belinda", "repos": {"/abs/repo/root": "<voice>"}}

A <voice> value is a local voice name (voices/<name>.wav + .txt transcript),
an absolute path to such a .wav, or a Higgs preset name (e.g. 'belinda').

Resolution order: HIGGS_VOICE env (preset) > HIGGS_VOICE_REF env (clip) >
longest repo-root prefix match in config > config default > local clips.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

CONFIG_FILE = Path.home() / ".claude" / "tts-voices.json"
VOICES_DIR = Path(__file__).resolve().parent / "voices"
DEFAULT_PRESET = "belinda"


def load_config() -> dict:
    try:
        with open(CONFIG_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def configured_voice(cwd: str) -> str | None:
    """Voice for *cwd* from config: longest repo-root prefix match, else default."""
    config = load_config()
    repos = config.get("repos") or {}
    try:
        cwd_path = str(Path(cwd).resolve()) if cwd else ""
    except OSError:
        cwd_path = ""
    best_root, best_voice = "", None
    for root, voice in repos.items():
        root = root.rstrip("/")
        if (cwd_path == root or cwd_path.startswith(root + "/")) and len(root) > len(best_root):
            best_root, best_voice = root, voice
    return best_voice if best_voice else config.get("default")


def resolve_voice(cwd: str | None = None) -> dict:
    """Resolve the active voice for *cwd* (defaults to the process cwd).

    Returns {"name", "preset", "ref_audio", "ref_text"}; exactly one of
    preset / ref_audio is set, or all are None when nothing usable exists.
    """
    preset = os.getenv("HIGGS_VOICE")
    if preset:
        return {"name": preset, "preset": preset, "ref_audio": None, "ref_text": None}

    candidates = []
    env_ref = os.getenv("HIGGS_VOICE_REF")
    if env_ref:
        candidates.append(Path(env_ref))

    voice = configured_voice(cwd or os.getcwd())
    if voice and not env_ref:
        voice_path = Path(voice)
        if voice_path.is_absolute() and voice_path.suffix == ".wav":
            candidates.append(voice_path)
        elif (VOICES_DIR / f"{voice}.wav").is_file():
            candidates.append(VOICES_DIR / f"{voice}.wav")
        else:
            # not a known clip: treat as a Higgs preset name
            return {"name": voice, "preset": voice, "ref_audio": None, "ref_text": None}

    for ref_audio in candidates:
        ref_text_file = ref_audio.with_suffix(".txt")
        if ref_audio.is_file() and ref_text_file.is_file():
            return {
                "name": ref_audio.stem,
                "preset": None,
                "ref_audio": ref_audio,
                "ref_text": ref_text_file.read_text().strip(),
            }

    # nothing configured, nothing on disk: fall back to a preset that always works
    return {"name": DEFAULT_PRESET, "preset": DEFAULT_PRESET,
            "ref_audio": None, "ref_text": None}


def display_name(name: str | None) -> str:
    """Short badge label (handy if you surface the active voice in a status line)."""
    return name if name else "?"
