# VinylGuard

Automatic end-of-side alert for manual turntables.

Run the script, drop the needle — it identifies the track, calculates the time left on the side, and warns you with beeps before the end.

---

## Requirements

- [uv](https://docs.astral.sh/uv/getting-started/installation/) installed
- A microphone connected to the PC (picks up sound from the turntable)
- Internet connection (Shazam + MusicBrainz)
- **macOS:** microphone permission granted to your terminal app (the script will prompt on first run)

Python 3.12 and all dependencies (`sounddevice`, `numpy`, `shazamio`, `requests`, `simple-localize`, and `pyobjc-framework-AVFoundation` on macOS) are managed automatically by `uv`.

---

## Run

```
uv run vinyl_guard.py
```

On the first run, `uv` downloads Python 3.12 and installs dependencies (~30s). Subsequent runs are instant.

---

## Usage

### Main menu

```
[1] Start    [2] Add album to catalog    [Q] Quit
```

### Normal flow

1. Launch the script → `Waiting for music...`
2. Drop the needle on the record
3. The mic detects music automatically
4. Shazam identifies the track and its position
5. MusicBrainz calculates the remaining time on the side
6. Countdown starts

```
Identified : Pink Floyd — Money  (The Dark Side of the Moon)
Position   : 0:04 into track
Searching tracklist (MusicBrainz)...
Side remaining: 22:47
▶  Pink Floyd — Money — 22:31 left
```

The track title updates in real time as the side plays.

### Alerts

| Time left | Signal |
|---|---|
| 1 minute | 2 beeps + message |
| 30 seconds | 3 beeps + message |
| 10 seconds | 5 rapid beeps + `LIFT THE NEEDLE` |
| 0 | long beep — side done |

### If Shazam fails to identify (obscure vinyl, no connection)

The script retries up to 3 times with a fresh recording, then prompts for the remaining duration in `MM:SS`.

### Manual start

Press `Enter` to start the countdown without waiting for audio detection.

### Stop the countdown

`Ctrl-C` — returns to the menu for the next side.

---

## Local catalog

VinylGuard builds a personal catalog in the `catalog/` folder. On first play of an album, it fetches the tracklist from MusicBrainz and asks you to assign tracks to sides interactively. Subsequent plays use the catalog directly (no network needed).

You can also pre-populate the catalog from the menu with **[2] Add album to catalog**.

See `catalog/README.txt` for the JSON format.

---

## Edge cases

**Script started mid-track**  
No problem — Shazam returns the exact position within the track. The countdown is adjusted accordingly.

**Script started on the last track of a side**  
Works fine: Shazam identifies the track and its position, MusicBrainz gives its duration, alerts fire correctly.

**Vinyl not on MusicBrainz**  
The script asks for the duration manually and still runs the countdown.

---

## Configuration

At the top of `vinyl_guard.py`:

```python
WARN_TIMES   = [60, 30, 10]   # seconds before end (adjustable)
AUDIO_THRESH = 0.02            # mic sensitivity (increase if false starts)
SHAZAM_SECS  = 15             # listening duration for identification
```

**Language:** Auto-detected from your system locale. Translations live in `translations.json`.

---

## Compatibility

Works on Windows, macOS, and Linux.
