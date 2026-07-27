#!/usr/bin/env python3
"""Codex notify hook: speak a concise outcome recap of the finished turn.

Codex calls this via the native `notify` setting in ~/.codex/config.toml:

    notify = ["python3", "/Users/you/.codex/hooks/notify_tts.py"]

The recap comes from the SAME shared builder as the Claude Code Stop hook
(recap.py, beside the TTS engine under ~/.claude/hooks/utils/tts/ or wherever
CODEX_TTS_ENGINE points) — deterministic, no model call. Falls back to macOS
`say` when the engine is missing. Codex runs this with a bare environment, so
config lives in ~/.codex/.env (simple KEY=VALUE lines, loaded at start).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


def load_env_file(path: Path) -> None:
    """Minimal .env loader (no deps): KEY=VALUE lines, '#' comments; existing
    environment variables win."""
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


load_env_file(Path.home() / ".codex" / ".env")

ENGINE = Path(
    os.environ.get("CODEX_TTS_ENGINE")
    or Path.home() / ".claude" / "hooks" / "utils" / "tts" / "higgs_tts.py"
).expanduser()
VOICES_DIR = Path(__file__).resolve().parent / "voices"

sys.path.insert(0, str(ENGINE.parent))
try:
    from recap import build_recap  # the shared, agent-agnostic recap builder
except ImportError:
    build_recap = None  # engine not installed — degrade to a crude recap


def fallback_recap(payload: dict[str, Any]) -> str:
    """Crude recap for when the shared builder is missing (engine not
    installed): word-capped raw message, still location/name-prefixed so the
    degraded path sounds like the real one."""
    message = payload.get("last-assistant-message") or payload.get(
        "last_assistant_message"
    )
    message = re.sub(r"\s+", " ", str(message or "")).strip()
    if len(message) > 220:  # mirrors recap.RECAP_CHAR_CAP, unimportable here
        message = message[:220].rsplit(" ", 1)[0] + "."
    cwd = payload.get("cwd")
    where = Path(cwd).name if isinstance(cwd, str) and cwd else "this repo"
    name = os.environ.get("HIGGS_RECAP_NAME", "").strip()
    prefix = f"{name}, here on {where}, " if name else f"Here on {where}, "
    if not message:
        return prefix + "turn complete."
    return prefix + message[0].lower() + message[1:]


def make_recap(payload: dict[str, Any]) -> str:
    if build_recap is not None:
        return build_recap(payload)
    return fallback_recap(payload)


def enabled(name: str, default: bool = True) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in {"", "0", "false", "no", "off"}


def detached(
    command: list[str],
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> None:
    try:
        subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except OSError:
        pass


def forward_original(raw_payload: str) -> None:
    """Optionally chain to another notifier (e.g. the desktop notification app
    this hook replaced). Point CODEX_NOTIFY_FORWARD at its executable."""
    target = os.environ.get("CODEX_NOTIFY_FORWARD")
    if target and Path(target).is_file():
        detached([target, "turn-ended", raw_payload])


def codex_voice() -> tuple[str | None, str | None]:
    """Codex's own voice, so you can tell which agent is talking.

    CODEX_VOICE names a Higgs preset; otherwise the first .wav + .txt pair in
    voices/ beside this script is used as a clone reference. With neither, the
    engine's own resolution (~/.claude/tts-voices.json) applies.
    """
    preset = os.environ.get("CODEX_VOICE")
    if preset:
        return preset, None
    if VOICES_DIR.is_dir():
        for wav in sorted(VOICES_DIR.glob("*.wav")):
            if wav.with_suffix(".txt").is_file():
                return None, str(wav)
    return None, None


def speak(recap: str, cwd: str | None = None) -> None:
    if not enabled("CODEX_TTS_ENABLED") or not enabled("HIGGS_TTS_ENABLED"):
        return

    if cwd and not Path(cwd).is_dir():
        cwd = None

    uv = (
        os.environ.get("CODEX_TTS_UV")
        or shutil.which("uv")
        or str(Path.home() / ".local" / "bin" / "uv")
    )

    if ENGINE.is_file() and Path(uv).is_file():
        tts_env = os.environ.copy()
        preset, ref = codex_voice()
        if preset:
            tts_env["HIGGS_VOICE"] = preset
            tts_env.pop("HIGGS_VOICE_REF", None)
        elif ref:
            tts_env.pop("HIGGS_VOICE", None)
            tts_env["HIGGS_VOICE_REF"] = ref
        detached(
            [uv, "run", "--script", str(ENGINE), recap],
            cwd=cwd,
            env=tts_env,
        )
        return

    say = shutil.which("say")
    if say:
        detached([say, "-r", "200", recap], cwd=cwd)


def parse_payload(raw_payload: str) -> dict[str, Any]:
    try:
        value = json.loads(raw_payload)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    dry_run = bool(args and args[0] == "--dry-run")
    if dry_run:
        args.pop(0)
    if not args:
        return 0

    raw_payload = args[-1]
    payload = parse_payload(raw_payload)
    if dry_run:
        print(make_recap(payload))
        return 0

    forward_original(raw_payload)
    if payload.get("type") == "agent-turn-complete":
        cwd = payload.get("cwd")
        speak(make_recap(payload), cwd if isinstance(cwd, str) else None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
