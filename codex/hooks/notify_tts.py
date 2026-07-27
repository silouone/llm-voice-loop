#!/usr/bin/env python3
"""Codex notify hook: speak a concise outcome recap of the finished turn.

Codex calls this via the native `notify` setting in ~/.codex/config.toml:

    notify = ["python3", "/Users/you/.codex/hooks/notify_tts.py"]

The recap is COMPUTED from the last assistant message — no model call — and
spoken through the shared Higgs Audio TTS engine (installed with the Claude
Code half of this repo under ~/.claude/hooks/utils/tts/, or anywhere via
CODEX_TTS_ENGINE). Falls back to macOS `say` when the engine is missing.
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


DEFAULT_TTS_ENGINE = (
    Path.home() / ".claude" / "hooks" / "utils" / "tts" / "higgs_tts.py"
)
VOICES_DIR = Path(__file__).resolve().parent / "voices"
RECAP_CHAR_CAP = 220


def enabled(name: str, default: bool = True) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in {"", "0", "false", "no", "off"}


def repo_name(cwd: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return Path(result.stdout.strip()).name.lstrip(".") or "this repo"
    except (OSError, subprocess.SubprocessError):
        pass
    return Path(cwd).name.lstrip(".") or "this repo"


def clean_line(raw: str) -> str:
    line = raw.strip().lstrip("#>-*+• \t")
    line = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", line)
    line = re.sub(r"(?:https?://|www\.)[^\s)\]>\"']+", " ", line)
    line = re.sub(
        r"\s+\b(?:see|at|via)\s*[.:;]?\s*$", "", line, flags=re.IGNORECASE
    )
    line = re.sub(r"\*\*(.+?)\*\*", r"\1", line)
    line = re.sub(r"`([^`]+)`", r"\1", line)
    line = re.sub(
        r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
        r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b",
        " ",
        line,
    )
    line = re.sub(r"\b(?=[0-9a-fA-F]*\d)[0-9a-fA-F]{7,}\b", " ", line)
    line = re.sub(r"\b\d(?:[.,]?\d){4,}\w*\b", " ", line)
    line = re.sub(r"[^\w\s.,;:!?'\"()—-]", "", line)
    line = re.sub(r"\s+([.,;:!?])", r"\1", line)
    line = re.sub(r"[:;]\s*\.", ".", line)
    return re.sub(r"\s+", " ", line).strip()


def recap_body(message: str) -> str:
    if not message:
        return "turn complete."

    text = re.sub(r"```.*?```", " ", message, flags=re.DOTALL)
    lines = text.splitlines()
    start = next(
        (index for index, raw in enumerate(lines) if raw.lstrip().startswith("#")),
        0,
    )

    sentences: list[str] = []
    for raw in lines[start:]:
        line = clean_line(raw)
        if len(re.sub(r"[^A-Za-z]", "", line)) < 12:
            continue
        parts = re.findall(r".+?[.!?](?:\s|$)|.+$", line)
        for part in parts:
            sentence = part.strip()
            if not sentence:
                continue
            if len(re.findall(r"\d", sentence)) * 2 > len(
                re.findall(r"[A-Za-z]", sentence)
            ):
                continue
            sentences.append(
                sentence if sentence[-1] in ".!?" else sentence + "."
            )

    chosen: list[str] = []
    total = 0
    for sentence in sentences:
        if chosen and total + len(sentence) + 1 > RECAP_CHAR_CAP:
            break
        chosen.append(sentence)
        total += len(sentence) + 1
    return " ".join(chosen) or "turn complete."


def build_recap(payload: dict[str, Any]) -> str:
    cwd = payload.get("cwd")
    cwd = cwd if isinstance(cwd, str) and cwd else os.getcwd()
    message = payload.get("last-assistant-message")
    if not isinstance(message, str):
        message = payload.get("last_assistant_message")
    body = recap_body(message if isinstance(message, str) else "")
    name = os.environ.get("HIGGS_RECAP_NAME", "").strip()
    prefix = (
        f"{name}, here on {repo_name(cwd)}, "
        if name
        else f"Here on {repo_name(cwd)}, "
    )
    if re.match(r"^\w+, here on ", body, flags=re.IGNORECASE):
        return body
    opening = (
        body
        if len(body) > 1 and body[:2].isupper()
        else body[0].lower() + body[1:]
    )
    return prefix + opening


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

    configured = os.environ.get("CODEX_TTS_ENGINE")
    engine = Path(configured).expanduser() if configured else DEFAULT_TTS_ENGINE
    uv = (
        os.environ.get("CODEX_TTS_UV")
        or shutil.which("uv")
        or str(Path.home() / ".local" / "bin" / "uv")
    )

    if engine.is_file() and Path(uv).is_file():
        tts_env = os.environ.copy()
        preset, ref = codex_voice()
        if preset:
            tts_env["HIGGS_VOICE"] = preset
            tts_env.pop("HIGGS_VOICE_REF", None)
        elif ref:
            tts_env.pop("HIGGS_VOICE", None)
            tts_env["HIGGS_VOICE_REF"] = ref
        detached(
            [uv, "run", "--script", str(engine), recap],
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
        print(build_recap(payload))
        return 0

    forward_original(raw_payload)
    if payload.get("type") == "agent-turn-complete":
        cwd = payload.get("cwd")
        speak(build_recap(payload), cwd if isinstance(cwd, str) else None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
