# Codex voice

Give Codex its own voice, distinct from Claude Code's, so you know which agent
is talking before the first word lands:

- **Preset:** `export CODEX_VOICE=en_woman` (any Higgs preset name), or
- **Clone:** drop a `<name>.wav` + `<name>.txt` transcript pair in this folder —
  the first pair found is used as the zero-shot clone reference.

To make an expressive clip with the self-clone trick, see
[`claude-code/hooks/utils/tts/voices/README.md`](../../../claude-code/hooks/utils/tts/voices/README.md).
With neither preset nor clip, the shared engine's own resolution
(`~/.claude/tts-voices.json`) applies.
