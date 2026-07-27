"""Shared spoken-recap builder for the voice loop.

ONE implementation for every agent: Claude Code's Stop hook (stop.py) and
Codex's notify hook (notify_tts.py) both import this module, so recap behavior
never drifts between agents. Deterministic — no model call, no extra tokens.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

RECAP_CHAR_CAP = 220  # soft target: ~2 short sentences ≈ 15s of audio at 1.15x


def repo_name(cwd: str) -> str:
    """Basename of the git repo root at *cwd*, else the cwd basename."""
    try:
        result = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return Path(result.stdout.strip()).name.lstrip(".") or "this repo"
    except (OSError, subprocess.SubprocessError):
        pass
    return Path(cwd).name.lstrip(".") or "this repo"


def _clean_line(raw: str) -> str:
    """Strip list/header markers, inline markdown, and unspeakable tokens
    (URLs, UUIDs, hex ids, long digit runs) from a line for speech."""
    line = raw.strip().lstrip("#>-*+• \t")
    # Unwrap markdown BEFORE stripping symbols, else the symbol strip eats the
    # []`* delimiters first and these subs never fire (URL would leak to speech).
    line = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", line)      # [t](u) -> t
    # Bare URLs must die WHOLE, before the symbol strip mangles them into
    # speakable garbage ("https:developers.cloudflare.com…").
    line = re.sub(r"(?:https?://|www\.)[^\s)\]>\"']+", " ", line)  # stop before closing delims
    line = re.sub(r"\s+\b(?:see|at|via)\s*[.:;]?\s*$", "", line, flags=re.IGNORECASE)
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
    line = re.sub(r"\s+([.,;:!?])", r"\1", line)              # tidy space-before-punctuation
    line = re.sub(r"[:;]\s*\.", ".", line)
    return re.sub(r"\s+", " ", line).strip()


def _sentences(line: str) -> list[str]:
    """Split a cleaned line into sentences, keeping filenames intact.

    A terminator only ends a sentence when followed by whitespace or end-of-
    line, so `pre_tool_use.py` / `settings.json` never split mid-token.
    """
    return [p.strip() for p in re.findall(r".+?[.!?](?:\s|$)|.+$", line) if p.strip()]


def recap_body(message: str) -> str:
    """Speakable multi-sentence recap of the final assistant message.

    Deterministic, no model call. Drops fenced code blocks, then prefers a
    markdown headline (the first `#`/`##` line) as the opening — it states the
    turn's result and lets us skip any commit/meta preamble above it — falling
    back to the first substantive line when there's no headline. Accumulates
    following prose sentences up to RECAP_CHAR_CAP (never cutting
    mid-sentence). Trailing git-diff / summary blocks sit at the bottom, so
    top-down extraction never reaches them.
    """
    if not message:
        return ""
    text = re.sub(r"```.*?```", " ", message, flags=re.DOTALL)  # drop code fences
    lines = text.splitlines()

    # Start at the first markdown headline if there is one; otherwise the top.
    start = next((i for i, raw in enumerate(lines) if raw.lstrip().startswith("#")), 0)

    candidates = []  # (sentence, had_a_real_terminator)
    for raw in lines[start:]:
        line = _clean_line(raw)
        if len(re.sub(r"[^A-Za-z]", "", line)) < 12:
            continue  # skip blank / decorative / too-short lines
        for s in _sentences(line):
            # Number-dense fragments (enumerations, spec/ID soup) are
            # unlistenable — speak prose only.
            if len(re.findall(r"\d", s)) * 2 > len(re.findall(r"[A-Za-z]", s)):
                continue
            real = s[-1] in ".!?"
            candidates.append((s if real else s + ".", real))  # terminator = a spoken pause

    out, total, cut_early = [], 0, False
    for s, real in candidates:
        if out and total + len(s) + 1 > RECAP_CHAR_CAP:
            cut_early = True
            break  # always keep the opening, then stop at the cap
        out.append((s, real))
        total += len(s) + 1

    # A budget break right after terminator-less fragments ("half a bullet")
    # sounds like the voice lost its train of thought — better to end one real
    # sentence early than to speak half a thought. The opening always stays.
    while cut_early and len(out) > 1 and not out[-1][1]:
        out.pop()

    joined = " ".join(s for s, _ in out)
    return re.sub(r"\s+", " ", joined).strip()  # collapse gaps left by stripped symbols


def last_message_from_transcript(transcript_path: str | None) -> str:
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


def build_recap(payload: dict[str, Any]) -> str:
    """'<Name>, here on <repo>, <lead sentence>' — computed, never model-crafted.

    Accepts both payload dialects: Claude Code's Stop hook sends
    `last_assistant_message` (with a transcript fallback); Codex's notify
    sends `last-assistant-message`. Set HIGGS_RECAP_NAME to be addressed
    personally.
    """
    cwd = payload.get("cwd")
    cwd = cwd if isinstance(cwd, str) and cwd else os.getcwd()
    message = payload.get("last_assistant_message")
    if not isinstance(message, str) or not message:
        message = payload.get("last-assistant-message")
    if not isinstance(message, str) or not message:
        message = last_message_from_transcript(payload.get("transcript_path"))
    frag = recap_body(message or "")
    name = os.getenv("HIGGS_RECAP_NAME", "").strip()
    prefix = (f"{name}, here on {repo_name(cwd)}, " if name
              else f"Here on {repo_name(cwd)}, ")
    if not frag:
        return prefix + "turn complete."
    if re.match(r"^\w+, here on ", frag, flags=re.IGNORECASE):
        return frag  # already location-prefixed (legacy output-style message)
    # Lowercase the opening word to flow after the prefix — unless it looks
    # like an acronym ("TTS cap fixed" must not become "tTS cap fixed").
    opening = frag if len(frag) > 1 and frag[:2].isupper() else frag[0].lower() + frag[1:]
    return prefix + opening
