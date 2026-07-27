# Custom voices

Drop a `<name>.wav` + `<name>.txt` (its exact transcript) pair in this folder,
then reference `<name>` in `~/.claude/tts-voices.json`. Higgs Audio zero-shot
clones the voice from the clip on every request.

No clips ship with this repo — voice clips are personal (and cloning a voice
you don't have rights to is on you). Two clean ways to make your own:

## 1. Record yourself

10–20 seconds of clean speech, then write the exact transcript to the sibling
`.txt`. Done.

## 2. The self-clone trick (expressive presets)

Have a *preset* speak an expressive passage, save the audio, and use that
recording as the reference clip — the delivery and character carry into the
clone, and you can steer it with `HIGGS_INSTRUCTIONS` at generation time:

```bash
cd hooks/utils/tts
HIGGS_NO_DETACH=true \
HIGGS_VOICE=belinda \
HIGGS_INSTRUCTIONS="speak with bright, jovial energy" \
HIGGS_SAVE_AUDIO=voices/my-energetic-voice.wav \
uv run higgs_tts.py "Oh, I love how this is coming together! Honestly, today feels amazing. We are getting so much done, and I can't wait to show you what's next."
```

Then save the same text as the transcript:

```bash
echo "Oh, I love how this is coming together! Honestly, today feels amazing. We are getting so much done, and I can't wait to show you what's next." > voices/my-energetic-voice.txt
```

And point your config at it:

```json
{ "default": "my-energetic-voice" }
```
