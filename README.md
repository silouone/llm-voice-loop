# 🎙️ LLM Voice Loop

**Talk to your coding agent. Your coding agent talks back.**

Every time **Claude Code** or **Codex** finishes a turn, a natural, expressive voice tells you what just happened — while you're across the room, in another window, or deep in another task:

> *"Silou, here on backend-api, the flaky login test is fixed and the suite is green again."*

No model calls, no extra tokens: the recap is **computed deterministically** from the agent's final message by a hook, spoken by [Higgs Audio v3](https://github.com/boson-ai/higgs-audio) (with an offline macOS `say` fallback), and paired with [SuperWhisper](https://superwhisper.com) for the voice-input half. The result is a full hands-free loop: **speak your prompt → the agent works → the agent speaks the result.** Give each agent — and each repo — its own voice, and you know who's talking about what before the first word lands.

Built and battle-tested as part of my daily agentic harness — the model is the engine; this is part of the harness around it.

---

## How it works

```
you speak ──► SuperWhisper (local STT) ──► Claude Code / Codex prompt
                                               │
                                          the agent works
                                               │
                              turn ends ───────┤
                                               │
        Claude Code: Stop hook ── stop.py      │      Codex: notify ── notify_tts.py
                       └───────────────┬───────┴──────────────┘
                                       │
                     computes a ≤220-char spoken recap
                     (headline-first extraction, strips markdown,
                      URLs, UUIDs, hex ids — no LLM call)
                                       │
                     higgs_tts.py — ONE shared engine, detaches
                     instantly (~0.1s), never blocks the turn
                                       │
              ┌────────────────────────┼──────────────────────┐
              │                        │                      │
    tts_coalesce.py             Higgs Audio v3 API      macOS `say`
    burst of hook fires         (Boson hosted, or       (offline
    → ONE spoken recap,          your local server)      fallback)
      the newest                       │
                                  ffplay playback
                                  (volume boost, 1.15x speed,
                                   45s runaway kill-switch)
```

Details that took real-world iteration to get right:

- **Never blocks the agent** — the TTS script re-spawns itself into a detached session and returns in ~0.1s; generation and playback happen while you read the on-screen response.
- **Newest-wins coalescing** — parallel sessions and hook re-fires would stack overlapping audio; a global trailing-debounce collapses any burst into exactly one spoken recap (unit-tested, no audio/network needed: `test_tts_coalesce.py`).
- **Speakable text only** — URLs, UUIDs, commit hashes, and digit soup are scrubbed before speech; small human numbers ("5 files", "24h") survive.
- **Never stops mid-thought** — the budget is soft-target/hard-ceiling: past the 220-char target the sentence in progress may finish (up to 400), a budget break never ends on a half-bullet fragment, and word-boundary cutting is the last resort for a terminator-less monster sentence. One shared recap builder (`recap.py`) serves both agents, so behavior never drifts.
- **Subagents stay silent** — background agents fire the same hooks; only the user-facing session speaks.
- **Graceful degradation** — no API key, no billing, or API down? You still hear the recap via macOS `say`.

## Repo layout

| Path | What it is |
|---|---|
| [`claude-code/hooks/`](claude-code/hooks) | Stop hook (`stop.py`) + the **shared TTS engine** (`utils/tts/`) — installs into `~/.claude/` |
| [`codex/hooks/`](codex/hooks) | Codex `notify` hook (`notify_tts.py`) — installs into `~/.codex/`, reuses the shared engine |
| [`claude-code/examples/`](claude-code/examples), [`codex/examples/`](codex/examples) | settings.json / config.toml wiring, per-repo voice map, `.env` template |

## Quickstart — Claude Code

**Prereqs:** macOS, [uv](https://docs.astral.sh/uv/), ffmpeg (`brew install ffmpeg`).

```bash
# 1. Drop the hooks (and the shared TTS engine) into your Claude config
git clone https://github.com/silouone/llm-voice-loop.git
cp -r llm-voice-loop/claude-code/hooks ~/.claude/

# 2. Wire the Stop hook (merge into ~/.claude/settings.json)
#    see claude-code/examples/settings.json
#    "Stop": [{"hooks": [{"type": "command",
#        "command": "uv run ~/.claude/hooks/stop.py --notify"}]}]

# 3. Add your Boson API key (skip this to use the free offline `say` voice)
echo 'BOSON_API_KEY=your-key' >> ~/.claude/.env
echo 'HIGGS_RECAP_NAME=YourFirstName' >> ~/.claude/.env

# 4. Test it in the foreground
cd ~/.claude/hooks/utils/tts
HIGGS_NO_DETACH=true uv run higgs_tts.py "Voice loop is alive."
```

Next turn Claude Code finishes, you'll hear it.

## Quickstart — Codex

Codex has a native `notify` mechanism — no plugin needed. The hook reuses the shared engine installed above (Codex-only user? Install `claude-code/hooks/utils/tts/` to `~/.claude/hooks/utils/tts/` anyway, or point `CODEX_TTS_ENGINE` anywhere you like).

```bash
# 1. Drop the notify hook into your Codex config
cp -r llm-voice-loop/codex/hooks ~/.codex/

# 2. Wire it (merge into ~/.codex/config.toml — absolute path, ~ not expanded)
#    notify = ["python3", "/Users/you/.codex/hooks/notify_tts.py"]

# 3. Give Codex its own voice so you can tell the agents apart
#    (Codex runs the hook with a bare environment, so config goes in ~/.codex/.env)
echo 'CODEX_VOICE=en_woman' >> ~/.codex/.env   # or drop a clone clip in ~/.codex/hooks/voices/
echo 'HIGGS_RECAP_NAME=YourFirstName' >> ~/.codex/.env

# 4. Test with a fake turn payload
python3 ~/.codex/hooks/notify_tts.py --dry-run \
  '{"type":"agent-turn-complete","last-assistant-message":"Refactor done, tests green."}'
```

`CODEX_TTS_ENABLED=0` silences Codex only; `HIGGS_TTS_ENABLED=0` silences everything.

## The Higgs Audio API (real pricing, July 2026)

The [open-weight Higgs Audio v2 model](https://github.com/boson-ai/higgs-audio) is Apache-2.0. The hosted `higgs-audio-v3-tts` API at `api.boson.ai` was a free public preview, **but that ended — it is now a paid API** (some docs pages still say free preview; they're stale):

| | |
|---|---|
| Price | **$0.015 / 1K input characters** (spaces & punctuation count; the generated audio itself is free) |
| Free trial | **$10 credit** for new accounts ≈ ~3,000 recaps |
| A recap | capped at 220 chars → **~$0.003 per spoken recap** |
| Heavy daily use | ~100 recaps/day ≈ **$0.33/day** |

Keys without billing get HTTP 429 `insufficient_quota` — the hook then silently falls back to `say`, so if you suddenly hear the robot voice, check your credits at [boson.ai](https://boson.ai).

**Want $0/month?** Self-host the Apache-2.0 v2 weights (e.g. an [mlx-audio](https://github.com/Blaizzy/mlx-audio) server on a spare Mac) and point `HIGGS_API_URL` at it — the hosted API stays as automatic fallback.

## Voices

Three ways to pick a voice, from zero-effort to fully custom:

**1. Named presets** (no setup): `belinda` (default — warm, clear), `en_woman`, `mabel`, `vex` (male-ish), and more.

```bash
echo 'HIGGS_VOICE=en_woman' >> ~/.claude/.env
```

**2. Delivery instructions** — the API honors natural-language direction, and it genuinely changes pitch, timbre, and energy:

```bash
echo 'HIGGS_INSTRUCTIONS=speak with bright, jovial energy' >> ~/.claude/.env
```

**3. Zero-shot voice cloning** — drop a 10–20s `.wav` + its `.txt` transcript in `~/.claude/hooks/utils/tts/voices/` and reference it in your config. See [`voices/README.md`](claude-code/hooks/utils/tts/voices/README.md) for the **self-clone trick**: have a preset speak an expressive passage, save it, and use that recording as the reference — the character carries into the clone.

**Per-repo voices** — each project can have its own voice, so you know *which* repo is talking before you hear the first word (`~/.claude/tts-voices.json`, see [`examples/tts-voices.json`](claude-code/examples/tts-voices.json)):

```json
{
  "default": "belinda",
  "repos": {
    "/Users/you/work/backend-api": "en_woman",
    "/Users/you/side-projects/game": "vex"
  }
}
```

**Per-agent voices** — Codex speaks with its own voice on top of all this: `CODEX_VOICE` preset or a clone clip in `codex/hooks/voices/` (see [its README](codex/hooks/voices/README.md)).

## The input half: SuperWhisper

The loop closes with [SuperWhisper](https://superwhisper.com) — hold a key, talk, and your words land in the agent's prompt as text. What matters:

- **The free version is enough.** Pick a *small* local Whisper model — for spoken prompts to a coding agent it's accurate enough, fast, and fully offline.
- Bind it to a comfortable push-to-talk key; it types into any focused field, including the Claude Code and Codex terminals.
- Dictating prompts is dramatically faster than typing them — and combined with the spoken recap you can run whole iterations without touching the keyboard or looking at the screen.

## Configuration reference

All optional, all via env (`~/.claude/.env` is loaded explicitly by the engine — see [`env.example`](claude-code/examples/env.example)):

| Variable | Default | What it does |
|---|---|---|
| `BOSON_API_KEY` | — | Higgs API key (unset → offline `say` fallback) |
| `HIGGS_RECAP_NAME` | — | Your name in the recap: *"Silou, here on …"* |
| `HIGGS_VOICE` | — | Force a preset voice everywhere |
| `HIGGS_VOICE_REF` | — | Path to a reference `.wav` (sibling `.txt` transcript) |
| `HIGGS_INSTRUCTIONS` | — | Delivery direction ("speak softly and warmly") |
| `HIGGS_SPEED` | `1.15` | Playback speed, pitch preserved (0.5–2.0) |
| `HIGGS_VOLUME_BOOST` | `10` | Volume boost in dB |
| `HIGGS_MAX_CHARS` | `300` | Soft cap on spoken text length |
| `HIGGS_MAX_CHARS_HARD` | `400` | Ceiling the in-progress sentence may finish up to |
| `HIGGS_MAX_SECONDS` | `45` | Playback kill-switch for runaway generations |
| `HIGGS_COALESCE_WINDOW` | `5` | Debounce window in s (0 disables) |
| `HIGGS_TTS_ENABLED` | on | Set `0`/`false`/`off` to silence ALL recaps |
| `HIGGS_API_URL` | — | Local/custom `/v1/audio/speech` endpoint (self-host) |
| `HIGGS_SILENT_MODE` | `false` | Suppress stderr logging |
| `HIGGS_NO_DETACH` | `false` | Run in foreground (manual testing) |
| `CODEX_VOICE` | — | Codex-only preset voice |
| `CODEX_TTS_ENABLED` | on | Silence Codex recaps only |
| `CODEX_TTS_ENGINE` | `~/.claude/hooks/utils/tts/higgs_tts.py` | Where the shared engine lives |
| `CODEX_NOTIFY_FORWARD` | — | Chain the raw payload to another notifier executable |

## Troubleshooting

- **Hearing the robotic `say` voice instead of Higgs** → no key, no billing (429 `insufficient_quota`), or the API is down. Run with `HIGGS_NO_DETACH=true` to see the actual error.
- **`ffplay not found`** → `brew install ffmpeg`.
- **Codex is silent** → check the `notify` path in `~/.codex/config.toml` is absolute, and test with `--dry-run` (prints the recap instead of speaking).
- **Two voices overlapping** → shouldn't happen (coalescing); if you disabled it with `HIGGS_COALESCE_WINDOW=0`, re-enable it.
- **Nothing plays at all** → check `HIGGS_TTS_ENABLED` isn't set to a falsey value, and test the engine directly in the foreground.

## Tests

```bash
cd claude-code/hooks/utils/tts && uv run test_tts_coalesce.py && uv run test_recap.py
```

Deterministic, no audio, no network — covers the newest-wins coalescing contract, stale-token recovery, the off-switch gate, the sentence-aware speech budget, and the recap builder.

## License & credits

- This code: [MIT](LICENSE).
- [Higgs Audio](https://github.com/boson-ai/higgs-audio) by Boson AI — open-weight v2 model under Apache-2.0; the hosted v3 API is their commercial (paid) service. Not affiliated.
- [SuperWhisper](https://superwhisper.com) — not affiliated.

---

*Part of a larger production agentic harness — hooks, verify gates, observability.*
