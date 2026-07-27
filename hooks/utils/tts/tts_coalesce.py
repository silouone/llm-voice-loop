"""Global coalescing debounce for TTS playback (newest-wins).

A single user turn can re-fire the Stop hook many times (background agents
reporting, cross-session coincidence). Each fire spawns a detached `higgs_tts`
child; without coordination they all generate + play audio and overlap. This
trailing-debounce collapses a burst to exactly one spoken recap — the newest —
globally across all sessions.

Mechanism: each fire writes a unique claim token to one shared file, waits
`window` seconds, then plays only if it is still the latest token. Later fires
write later, so the last physical writer owns the file; every earlier fire wakes
to a newer token and drops *before* generating audio (no wasted Higgs call).

Only the token's uniqueness matters, not its numeric value: the decision is pure
last-writer-wins equality, so cross-process monotonic clocks never need to be
comparable. `now_ns + pid` is unique across distinct live processes.

The decision is factored into `coalesce_claim` with injected clock / file I/O /
sleep / play so it is unit-testable without audio, network, or real time.
"""

import os
import tempfile
import time
from pathlib import Path

RUNTIME_DIR = Path.home() / ".claude" / ".tts-runtime"
CLAIM_FILE = RUNTIME_DIR / "latest"
DEFAULT_WINDOW = 5.0  # seconds; above the observed 3-4s Stop-hook re-fire gap


def coalesce_claim(play, *, window, now_ns, pid, read_latest, write_latest, sleep):
    """Trailing-debounce, newest-wins. Returns True if play() ran, else False.

    window <= 0 disables coalescing: play immediately (today's behavior).
    """
    if window <= 0:
        play()
        return True
    token = f"{now_ns()}:{pid}"
    write_latest(token)          # publish my claim
    sleep(window)                # let any later fires in the burst overwrite it
    if read_latest() != token:   # a later fire claimed the window → superseded
        return False             # drop: never generate, never play
    play()                       # I am the newest in a quiet window
    return True


def _write_latest(token, claim_file=CLAIM_FILE):
    """Atomically publish `token` as the latest claim (unique temp + os.replace).

    Unique temp name per writer so concurrent fires from parallel sessions never
    clobber each other's temp before the replace; os.replace is atomic so the
    final file is never torn.
    """
    claim_file = Path(claim_file)
    claim_file.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(claim_file.parent), prefix=".latest.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(token)
        os.replace(tmp, claim_file)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_latest(claim_file=CLAIM_FILE):
    """Current claim token, or None if the file is missing/unreadable.

    A missing file yields None (never raises): a stale/dead token can only be
    overwritten by the next fire, never permanently silence recaps.
    """
    try:
        return Path(claim_file).read_text().strip()
    except OSError:
        return None


def _window_seconds():
    """HIGGS_COALESCE_WINDOW seconds (default 5; 0 disables). Env is a trust
    boundary — a malformed value falls back to the default rather than raising
    in the detached child and silently dropping the recap."""
    try:
        return float(os.getenv("HIGGS_COALESCE_WINDOW", str(DEFAULT_WINDOW)))
    except ValueError:
        return DEFAULT_WINDOW


def coalesce_and_play(play):
    """Wire the real clock / file / sleep and run the coalescer."""
    return coalesce_claim(
        play,
        window=_window_seconds(),
        now_ns=time.monotonic_ns,
        pid=os.getpid(),
        read_latest=_read_latest,
        write_latest=_write_latest,
        sleep=time.sleep,
    )
