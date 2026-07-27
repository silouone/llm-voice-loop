#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "python-dotenv",
# ]
# ///

import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from voice_config import resolve_voice
from tts_coalesce import coalesce_and_play

# Try to load dotenv if available, but make it optional.
# Load ~/.claude/.env EXPLICITLY: the Stop hook and this script run with the
# active repo as cwd, so a bare load_dotenv() (which reads ./.env) would miss
# the key in every repo but ~/.claude. The global env is the single home.
try:
    from dotenv import load_dotenv
    load_dotenv(Path.home() / ".claude" / ".env")
except ImportError:
    # dotenv not available, use environment variables directly
    pass

# Stderr output to avoid interfering with Claude Code output
SILENT_MODE = os.getenv('HIGGS_SILENT_MODE', 'false').lower() == 'true'

BOSON_URL = "https://api.boson.ai/v1/audio/speech"
BOSON_MODEL = "higgs-audio-v3-tts"
# Default model when HIGGS_API_URL points at a local mlx-audio server
MLX_MODEL = "mlx-community/higgs-audio-v2-3B-mlx-q8"
VOICES_DIR = Path(__file__).resolve().parent / "voices"


def eprint(*args, **kwargs):
    """Print to stderr to avoid interfering with terminal output"""
    if not SILENT_MODE:
        print(*args, **kwargs, file=sys.stderr)


def detach_and_exit():
    """Re-spawn this script in a new session so the caller returns immediately.

    Audio generation AND playback then happen in the background while the user
    reads the on-screen response — instead of blocking on the full notification
    before the recap renders. Skipped when HIGGS_NO_DETACH=true (foreground
    manual testing) or once already re-spawned (_HIGGS_DETACHED guard).
    """
    devnull = os.open(os.devnull, os.O_RDWR)
    try:
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]],
            stdin=devnull,
            stdout=devnull,
            stderr=devnull,
            start_new_session=True,
            env={**os.environ, "_HIGGS_DETACHED": "1", "HIGGS_SILENT_MODE": "true"},
            close_fds=True,
        )
    finally:
        os.close(devnull)


def get_api_key():
    """BOSON_API_KEY env var, falling back to an optional BOSON_KEY_FILE."""
    key = os.getenv('BOSON_API_KEY')
    if key:
        return key.strip()
    key_file = os.getenv('BOSON_KEY_FILE')
    if key_file and Path(key_file).is_file():
        return Path(key_file).read_text().strip()
    return None


def get_text():
    """Text to speak: joined argv, or a default line."""
    if len(sys.argv) > 1:
        return " ".join(sys.argv[1:])
    return "The first move is what sets everything in motion."


MAX_CHARS_DEFAULT = 300  # last-resort cap for ANY caller; stop.py's recap is tighter


def sanitize_for_speech(text):
    """Scrub tokens that are meaningless when spoken, whatever the caller sent:
    URLs, UUIDs, hex ids (must contain a digit), digit runs of 5+ digits.
    Small human numbers ('5 files', '24h', '1,000') stay."""
    text = re.sub(r"(?:https?://|www\.)[^\s)\]>\"']+", " ", text)  # stop before closing delims
    text = re.sub(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
                  r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b", " ", text)
    text = re.sub(r"\b(?=[0-9a-fA-F]*\d)[0-9a-fA-F]{7,}\b", " ", text)
    text = re.sub(r"\b\d(?:[.,]?\d){4,}\w*\b", " ", text)  # \w* eats unit suffixes ("86,400s")
    text = re.sub(r"\(\s*\)", " ", text)                   # parens left empty by the scrubs
    return re.sub(r"\s+", " ", text).strip()


def truncate_for_speech(text, limit):
    """Hard cap the spoken text, cutting at a sentence end when one lands in
    the second half of the budget, else at a word boundary."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    sentence = re.search(r"^.*[.!?]", cut, re.DOTALL)
    if sentence and len(sentence.group(0)) >= limit // 2:
        return sentence.group(0).strip()
    return cut.rsplit(" ", 1)[0].strip()


def say_fallback(text):
    """Offline fallback: speak via macOS `say` when Higgs is unavailable.

    No voice clone here — this is the graceful-degradation path (no key /
    endpoints down) so the recap is still heard.
    """
    try:
        speed = max(0.5, min(2.0, float(os.getenv('HIGGS_SPEED', '1.15'))))
        subprocess.run(["say", "-r", str(int(175 * speed)), text], check=False)
        eprint("🗣️  Spoke via macOS `say` fallback")
    except (FileNotFoundError, OSError, subprocess.SubprocessError) as e:
        eprint(f"⚠️  `say` fallback failed: {e}")


def get_targets():
    """Endpoints to try in order: optional local/custom server first, hosted Boson last.

    Style differences: Boson takes ref_audio as base64 content; an mlx-audio
    server takes ref_audio as a file path readable by the server process.
    """
    targets = []
    custom = os.getenv('HIGGS_API_URL')
    if custom and "api.boson.ai" not in custom:
        targets.append({
            "url": custom,
            "style": "mlx",
            "model": os.getenv('HIGGS_MODEL', MLX_MODEL),
        })
    targets.append({"url": BOSON_URL, "style": "boson", "model": BOSON_MODEL})
    return targets


def generate_audio(api_key, text, target, ref_audio=None, ref_text=None, voice=None, instructions=None):
    """Call one Higgs Audio endpoint; returns WAV bytes."""
    body = {
        "model": target["model"],
        "input": text,
        "response_format": "wav",
    }
    if target["style"] == "mlx":
        # path as seen by the server (override when the server is another machine)
        body["ref_audio"] = os.getenv('HIGGS_VOICE_REF_REMOTE', str(ref_audio))
        body["ref_text"] = ref_text
        body["temperature"] = 0.7
        body["max_tokens"] = 1200
    else:
        body["max_new_tokens"] = 2048
        if voice:
            body["voice"] = voice
        else:
            body["ref_audio"] = base64.b64encode(ref_audio.read_bytes()).decode()
            body["ref_text"] = ref_text
        if instructions:
            body["instructions"] = instructions
    headers = {"Content-Type": "application/json"}
    if target["style"] == "boson":
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(target["url"], data=json.dumps(body).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=300) as response:
        return response.read()


def main():
    """
    Higgs Audio v3 TTS Script (Boson hosted API)

    Higgs Audio served by Boson AI. The voice is either a named preset or a
    zero-shot clone of a local reference clip (a .wav with a sibling .txt
    transcript).

    Usage:
    - ./higgs_tts.py                    # Uses default text
    - ./higgs_tts.py "Your custom text" # Uses provided text

    Environment Variables:
    - BOSON_API_KEY: Boson API key (falls back to BOSON_KEY_FILE)
    - BOSON_KEY_FILE: Path to a file containing the API key
    - HIGGS_API_URL: Optional local/custom /v1/audio/speech endpoint (e.g. an
      mlx-audio server on a spare Mac); hosted Boson remains the fallback
    - HIGGS_MODEL: Model id for the custom endpoint (default: mlx-community/higgs-audio-v2-3B-mlx-q8)
    - HIGGS_VOICE_REF_REMOTE: Reference wav path AS SEEN BY the custom server
    - HIGGS_VOICE: Named preset voice (e.g. 'belinda', 'en_woman', 'mabel',
      'vex') — skips the reference clip
    - HIGGS_VOICE_REF: Path to a reference .wav (transcript expected at same path .txt)
      Without env overrides, the voice comes from ~/.claude/tts-voices.json
      (per-repo map keyed by repo root, with a 'default' for unknown repos)
    - HIGGS_INSTRUCTIONS: Optional delivery instructions (e.g. 'speak softly and warmly')
    - HIGGS_VOLUME_BOOST: Volume boost in dB (default: 10)
    - HIGGS_SPEED: Playback speed multiplier, pitch preserved (default: 1.15, range 0.5-2.0)
    - HIGGS_MAX_CHARS: Hard cap on spoken text length (default: 300, min 40)
    - HIGGS_MAX_SECONDS: Hard cap on playback duration (default: 45, min 5)
    - HIGGS_SILENT_MODE: 'true' to suppress stderr logging
    - HIGGS_SAVE_AUDIO: Optional path to also save the generated audio
    - HIGGS_NO_DETACH: 'true' to run in the foreground (blocks until playback ends)
    - HIGGS_COALESCE_WINDOW: Seconds to debounce overlapping recaps (default 5;
      a burst collapses to one spoken recap — the newest; 0 disables coalescing)
    """
    # Return to the caller instantly: detach generation + playback into a new
    # session so Claude Code's recap text renders before the audio plays.
    if os.getenv("_HIGGS_DETACHED") != "1" and \
            os.getenv("HIGGS_NO_DETACH", "false").lower() != "true":
        detach_and_exit()
        return

    # Sanitize + cap ONCE, up front: every downstream path (Higgs endpoints,
    # `say` fallbacks) speaks the same bounded, ID-free text.
    max_chars = max(40, int(float(os.getenv('HIGGS_MAX_CHARS', str(MAX_CHARS_DEFAULT)))))
    text = truncate_for_speech(sanitize_for_speech(get_text()), max_chars)
    if not text:
        text = "Turn complete."

    # Coalesce across all sessions: a burst of Stop-hook re-fires collapses to a
    # single spoken recap — the newest. Superseded fires drop here, before any
    # Higgs API call or `say` fallback.
    coalesce_and_play(lambda: generate_and_play(text))


def generate_and_play(text):
    """Generate audio for `text` via Higgs (or `say` fallback) and play it."""
    api_key = get_api_key()
    if not api_key:
        eprint("❌ No Boson API key (set BOSON_API_KEY in ~/.claude/.env) — using `say`")
        say_fallback(text)
        return

    instructions = os.getenv('HIGGS_INSTRUCTIONS')
    # Per-repo voice: ~/.claude/tts-voices.json maps repo roots to voices,
    # with env overrides on top.
    resolved = resolve_voice()
    voice = resolved["preset"]
    ref_audio, ref_text = resolved["ref_audio"], resolved["ref_text"]
    if not voice and not ref_audio:
        eprint("❌ Error: reference voice not found")
        eprint(f"Expected a .wav + .txt transcript pair in {VOICES_DIR},")
        eprint("or a voice entry in ~/.claude/tts-voices.json,")
        eprint("or set HIGGS_VOICE_REF to a .wav with a sibling .txt transcript,")
        eprint("or set HIGGS_VOICE to a preset name")
        sys.exit(1)

    eprint("🎙️  Higgs Audio v3 TTS (Boson)")
    eprint("=" * 40)
    eprint(f"🎯 Text: {text}")
    eprint(f"🗣️  Voice: {voice if voice else ref_audio.name}")
    eprint("🔊 Generating and playing...")

    # One retry per endpoint (occasional silent/looping generations happen),
    # then fall back to the next endpoint — local server first, hosted Boson last.
    audio_bytes = b''
    for target in get_targets():
        for attempt in range(2):
            try:
                audio_bytes = generate_audio(api_key, text, target, ref_audio=ref_audio,
                                             ref_text=ref_text, voice=voice,
                                             instructions=instructions)
                if len(audio_bytes) > 1000:
                    break
                eprint(f"⚠️  Suspiciously small response ({len(audio_bytes)} bytes), retrying...")
            except urllib.error.HTTPError as e:
                eprint(f"⚠️  API error ({e.code}) from {target['url']}: "
                       f"{e.read().decode(errors='replace')[:200]}")
            except Exception as e:
                eprint(f"⚠️  Error from {target['url']}: {e}")
        if len(audio_bytes) > 1000:
            break
        eprint(f"↪️  Falling back past {target['url']}")

    if len(audio_bytes) <= 1000:
        eprint("❌ No usable audio from any endpoint — using `say` fallback")
        say_fallback(text)
        return

    volume_boost_db = float(os.getenv('HIGGS_VOLUME_BOOST', '10'))
    # ffmpeg's atempo only accepts 0.5-2.0 per filter instance
    speed = max(0.5, min(2.0, float(os.getenv('HIGGS_SPEED', '1.15'))))

    save_path = os.getenv('HIGGS_SAVE_AUDIO')
    if save_path:
        Path(save_path).write_bytes(audio_bytes)
        eprint(f"💾 Saved audio copy: {save_path}")

    with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as temp_file:
        temp_file.write(audio_bytes)
        temp_path = temp_file.name

    audio_filter = f'volume={volume_boost_db}dB'
    if speed != 1.0:
        audio_filter += f',atempo={speed}'

    # Kill-switch: Higgs occasionally emits looping/runaway generations;
    # -t bounds playback no matter how much audio came back.
    max_seconds = max(5, int(float(os.getenv('HIGGS_MAX_SECONDS', '45'))))

    try:
        eprint(f"🔊 Volume boost: +{volume_boost_db} dB, speed: {speed}x, cap: {max_seconds}s")
        subprocess.run(
            [
                'ffplay',
                '-nodisp',
                '-autoexit',
                '-loglevel', 'quiet',
                '-t', str(max_seconds),
                '-af', audio_filter,
                temp_path,
            ],
            check=True,
        )
        eprint("✅ Playback complete!")
    except FileNotFoundError:
        eprint("❌ Error: ffplay not found (install ffmpeg)")
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        eprint(f"❌ Playback error: {e}")
        sys.exit(1)
    finally:
        os.unlink(temp_path)


if __name__ == "__main__":
    main()
