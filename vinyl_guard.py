# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = ["sounddevice", "numpy", "shazamio", "requests", "simple-localize", "pyobjc-framework-AVFoundation; sys_platform == 'darwin'"]
# ///
"""VinylGuard — automatic end-of-side alert for manual turntables.

Run with: uv run vinyl_guard.py
"""

import asyncio
import contextlib
import json
import os
import re
import sys
import tempfile
import time
import wave
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np
import requests
import simple_localize
import sounddevice as sd

# ── Cross-platform keyboard & beep ────────────────────────────────────────────

_IS_WIN = sys.platform == "win32"


def _beep(freq, ms):
    n    = int(SAMPLE_RATE * ms / 1000)
    t    = np.linspace(0, ms / 1000, n, endpoint=False)
    tone = (np.sin(2 * np.pi * freq * t) * 0.4 * 32767).astype(np.int16)
    sd.play(tone, SAMPLE_RATE)
    sd.wait()


if _IS_WIN:
    import msvcrt as _msvcrt

    def _kbhit():
        return _msvcrt.kbhit()

    def _getch():
        k = _msvcrt.getch()
        if k in (b"\x00", b"\xe0"):
            k2 = _msvcrt.getch()
            return {b"H": "UP", b"P": "DOWN"}.get(k2)
        return k

    @contextlib.contextmanager
    def _raw_mode():
        yield

else:
    import select as _select
    import termios as _termios
    import tty as _tty

    def _kbhit():
        return bool(_select.select([sys.stdin], [], [], 0)[0])

    def _getch():
        k = sys.stdin.buffer.read(1)
        if k == b"\x1b" and _select.select([sys.stdin], [], [], 0.05)[0]:
            k2 = sys.stdin.buffer.read(1)
            if k2 == b"[" and _select.select([sys.stdin], [], [], 0.05)[0]:
                k3 = sys.stdin.buffer.read(1)
                return {"A": "UP", "B": "DOWN"}.get(k3.decode("latin1"), k)
        return k

    @contextlib.contextmanager
    def _raw_mode():
        fd  = sys.stdin.fileno()
        old = _termios.tcgetattr(fd)
        try:
            _tty.setraw(fd)
            yield
        finally:
            _termios.tcsetattr(fd, _termios.TCSADRAIN, old)

# ── Constants ─────────────────────────────────────────────────────────────────
WARN_TIMES   = [60, 30, 10]
AUDIO_THRESH = 0.02
SAMPLE_RATE  = 44100
SHAZAM_SECS  = 15
MB_AGENT     = "VinylGuard/1.0 (ganon0044@gmail.com)"
CATALOG_DIR  = Path(__file__).parent / "catalog"

# ── Localisation ──────────────────────────────────────────────────────────────

simple_localize.init_localizer(str(Path(__file__).parent / "translations.json"))


def _t(key, **kw):
    return simple_localize.get_text(key, **kw)


# ── Audio detection ───────────────────────────────────────────────────────────

def wait_for_music():
    print(_t("wait_msg"))
    print(_t("wait_hint"), flush=True)
    consecutive = 0
    with _raw_mode():
        while True:
            if _kbhit():
                key = _getch()
                if key in (b"\r", b"\n"):
                    print()
                    return time.time()
                if key in (b"q", b"Q", b"\x03"):
                    raise SystemExit(0)
            try:
                chunk = sd.rec(int(0.2 * SAMPLE_RATE), samplerate=SAMPLE_RATE, channels=1, dtype="int16")
                sd.wait()
            except Exception as e:
                print(_t("mic_err", e=e))
                raise SystemExit(1)
            rms = float(np.sqrt(np.mean(chunk.astype(np.float32) ** 2))) / 32768
            if rms > AUDIO_THRESH:
                consecutive += 1
                if consecutive >= 3:
                    t = time.time()
                    print(_t("detected"))
                    return t
            else:
                consecutive = 0


def record_audio(seconds):
    audio = sd.rec(int(seconds * SAMPLE_RATE), samplerate=SAMPLE_RATE, channels=1, dtype="int16")
    sd.wait()
    return audio


# ── Shazam identification ─────────────────────────────────────────────────────

def shazam_identify(audio_array):
    rms = float(np.sqrt(np.mean(audio_array.astype(np.float32) ** 2))) / 32768
    if rms < 0.001:
        print(_t("mic_silent"))
        return None

    import warnings
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=SyntaxWarning)
        from shazamio import Shazam

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        tmp = f.name
    try:
        with wave.open(tmp, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(audio_array.tobytes())

        async def _run():
            return await Shazam().recognize(tmp)

        result = asyncio.run(_run())
    except Exception as e:
        print(_t("shazam_err", e=e))
        return None
    finally:
        os.unlink(tmp)

    if not result or "track" not in result:
        return None

    track = result["track"]
    album = ""
    for section in track.get("sections", []):
        for meta in section.get("metadata", []):
            if meta.get("title") == "Album":
                album = meta.get("text", "")
                break
    # offset = position in seconds within the track at sampling time
    offset_secs = float((result.get("matches") or [{}])[0].get("offset", 0.0))
    return (
        track.get("subtitle", ""),  # artist
        track.get("title", ""),     # title
        track.get("isrc", ""),      # ISRC (sometimes absent)
        album,
        offset_secs,
    )


# ── MusicBrainz ───────────────────────────────────────────────────────────────

def _mb_get(path, params=None):
    r = requests.get(
        f"https://musicbrainz.org/ws/2/{path}",
        params={"fmt": "json", **(params or {})},
        headers={"User-Agent": MB_AGENT},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


def _split_point(tracks):
    """Midpoint heuristic: last track index of side A for single-medium releases."""
    total_ms = sum(t.get("length", 0) or 0 for t in tracks)
    half_ms  = total_ms / 2
    a_end    = len(tracks) // 2 - 1
    cumsum   = 0
    for j, t in enumerate(tracks):
        cumsum += t.get("length", 0) or 0
        if cumsum >= half_ms:
            return max(0, j - 1)
    return a_end


def _side_remaining_ms(tracks, idx):
    a_end = _split_point(tracks)
    if idx <= a_end:
        return sum(t.get("length", 0) or 0 for t in tracks[idx:a_end + 1])
    return sum(t.get("length", 0) or 0 for t in tracks[idx:])


def _media_to_flat(media):
    return [
        {"title": t.get("title", ""),
         "duration": (t.get("length") or t.get("recording", {}).get("length") or 0) // 1000}
        for medium in media
        for t in medium.get("tracks", [])
    ]


def checkbox_select(items, prompt):
    """Interactive checkbox UI. Returns sorted list of selected indices."""
    n        = len(items)
    selected = set()
    cursor   = 0
    first    = True

    header = [prompt, _t("cb_hint"), ""]
    H = len(header)

    def _render():
        nonlocal first
        lines = list(header)
        for i, item in enumerate(items):
            mm, ss = divmod(item.get("duration", 0), 60)
            mark  = "x" if i in selected else " "
            arrow = ">" if i == cursor else " "
            title = item["title"][:36]
            lines.append(f"  {arrow} [{mark}] {i + 1:>2}. {title:<36} {mm}:{ss:02d}")
        content = "\r\n".join(lines)
        if not first:
            print(f"\r\033[{H + n}A", end="")
        print(content, end="\r\n", flush=True)
        first = False

    _render()
    with _raw_mode():
        while True:
            key = _getch()
            if key == "UP":
                cursor = (cursor - 1) % n
            elif key == "DOWN":
                cursor = (cursor + 1) % n
            elif key == b"\t":
                break
            elif key == b"\x03":
                raise KeyboardInterrupt
            elif key == b" ":
                selected ^= {cursor}
            elif key == b"\r":
                selected ^= {cursor}
                cursor = min(cursor + 1, n - 1)
            _render()

    print()
    return sorted(selected)


def ask_side_assignment(flat_tracks):
    remaining = list(range(len(flat_tracks)))
    sides: dict = {}

    for label in "ABCDEFGH":
        if not remaining:
            break
        available = [flat_tracks[i] for i in remaining]
        print(_t("face_header", label=label, n=len(available)))
        sel_local = checkbox_select(available, _t("face_prompt", label=label))
        if not sel_local:
            break
        sides[label] = [available[i] for i in sel_local]
        sel_global   = {remaining[i] for i in sel_local}
        remaining    = [i for i in remaining if i not in sel_global]

    return sides


def _scan_media(media, *, by_id=None, by_title=None):
    multi = len(media) >= 2

    def _ms_from(tracks, i):
        if multi:
            return sum(t.get("length", 0) or 0 for t in tracks[i:])
        return _side_remaining_ms(tracks, i)

    def _hit(tracks, i):
        ms       = _ms_from(tracks, i)
        track_ms = tracks[i].get("length", 0) or 0
        return (ms // 1000, track_ms // 1000) if ms > 0 else None

    if by_id:
        for medium in media:
            tracks = medium.get("tracks", [])
            for i, track in enumerate(tracks):
                if track.get("recording", {}).get("id") == by_id:
                    return _hit(tracks, i)

    if by_title:
        t_lower = by_title.lower()
        for medium in media:
            tracks = medium.get("tracks", [])
            for i, track in enumerate(tracks):
                if t_lower in track.get("title", "").lower():
                    return _hit(tracks, i)

    return None


def get_remaining_on_side(artist, title, isrc, album=""):
    album_clean = _clean_album(album)
    result = _get_remaining(artist, title, isrc, album)
    if result is None and album_clean != album:
        result = _get_remaining(artist, title, isrc, album_clean)
    return result


def _get_remaining(artist, title, isrc, album=""):
    try:
        recording_id = None

        if isrc:
            try:
                data = _mb_get(f"isrc/{isrc}", {"inc": "releases"})
                recs = data.get("recordings", [])
                if recs:
                    recording_id = recs[0]["id"]
            except Exception:
                pass

        if album:
            time.sleep(1)
            data = _mb_get("release/", {
                "query": f'release:"{album}" AND artist:"{artist}" AND format:Vinyl',
                "limit": 3,
            })
            for rel in data.get("releases", [])[:2]:
                try:
                    time.sleep(1)
                    rel_data = _mb_get(f"release/{rel['id']}", {"inc": "recordings+media"})
                    media  = rel_data.get("media", [])
                    result = _scan_media(media, by_id=recording_id, by_title=title)
                    if result is not None:
                        return (*result, media)
                except Exception:
                    continue

        if not recording_id:
            time.sleep(1)
            query = f'recording:"{title}" AND artist:"{artist}"'
            if album:
                query += f' AND release:"{album}"'
            data = _mb_get("recording/", {"query": query, "limit": 5})
            recs = data.get("recordings", [])
            if not recs and album:
                time.sleep(1)
                data = _mb_get("recording/", {
                    "query": f'recording:"{title}" AND artist:"{artist}"',
                    "limit": 5,
                })
                recs = data.get("recordings", [])
            if not recs:
                # Fallback for compilations/soundtracks where MB artist differs
                time.sleep(1)
                data = _mb_get("recording/", {"query": f'recording:"{title}"', "limit": 5})
                recs = data.get("recordings", [])
            if not recs:
                return None
            recording_id = recs[0]["id"]

        time.sleep(1)
        data = _mb_get(f"recording/{recording_id}", {"inc": "releases"})
        releases = data.get("releases", [])
        if album:
            filtered = [r for r in releases
                        if album.lower() in r.get("title", "").lower()
                        or r.get("title", "").lower() in album.lower()]
            if filtered:
                releases = filtered
        if not releases:
            return None
        time.sleep(1)
        data  = _mb_get(f"release/{releases[0]['id']}", {"inc": "recordings+media"})
        media = data.get("media", [])
        result = _scan_media(media, by_id=recording_id, by_title=title)
        if result is not None:
            return (*result, media)
        return None

    except Exception as e:
        print(_t("mb_err", e=e))

    return None


# ── Local catalog ─────────────────────────────────────────────────────────────

def _clean_album(s):
    """Strip edition qualifiers that Shazam adds but vinyl releases don't have."""
    return re.sub(
        r'\s*\((?:extended|deluxe|special|limited|bonus|remaster\w*)[^)]*\)',
        '', s, flags=re.IGNORECASE,
    ).strip()


def _slugify(s):
    return re.sub(r"[^\w]+", "_", s.lower()).strip("_")[:60]


def _norm_apostrophes(s):
    return s.replace("’", "'").replace("‘", "'")


def _find_in_sides(sides, title):
    t_low = _norm_apostrophes(title.lower())
    for tracks in sides.values():
        for i, track in enumerate(tracks):
            tl = _norm_apostrophes(track.get("title", "").lower())
            if t_low in tl or tl in t_low:
                remaining = sum(t.get("duration", 0) for t in tracks[i:])
                return (remaining, track.get("duration", 0), tracks[i:])
    return None


def _words(s):
    return set(re.findall(r'\w{3,}', s.lower()))


def catalog_lookup(artist, album, title):
    if not CATALOG_DIR.exists():
        return None
    album    = _clean_album(album)
    a_low    = artist.lower()
    al_low   = album.lower()
    a_words  = _words(artist)
    for f in sorted(CATALOG_DIR.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            da  = data.get("artist", "").lower()
            dab = data.get("album",  "").lower()
            artist_ok = (a_low in da or da in a_low
                         or bool(a_words & _words(da)))
            album_ok  = (al_low in dab or dab in al_low)
            if not artist_ok or not album_ok:
                continue
            result = _find_in_sides(data.get("sides", {}), title)
            if result:
                return result
        except Exception:
            continue
    return None


def catalog_save(artist, album, sides):
    CATALOG_DIR.mkdir(exist_ok=True)
    path = CATALOG_DIR / f"{_slugify(artist)}_{_slugify(album)}.json"
    if path.exists():
        return None
    path.write_text(
        json.dumps({"artist": artist, "album": album, "sides": sides},
                   indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


# ── Countdown ─────────────────────────────────────────────────────────────────

def _beep_warn(warn_at):
    if warn_at == 60:
        print(_t("warn_60"), flush=True)
        _beep(880, 400); time.sleep(0.15); _beep(880, 400)
    elif warn_at == 30:
        print(_t("warn_30"), flush=True)
        for _ in range(3):
            _beep(1100, 300); time.sleep(0.1)
    elif warn_at == 10:
        print(_t("warn_10"), flush=True)
        for _ in range(5):
            _beep(1400, 200); time.sleep(0.1)


def _current_track_label(tracks, elapsed):
    cumul = 0
    for t in tracks:
        cumul += t["duration"]
        if elapsed < cumul:
            return t["title"]
    return tracks[-1]["title"] if tracks else ""


def countdown(total_seconds, label, tracks=None):
    warned = set()
    start  = time.time()
    try:
        while True:
            elapsed       = time.time() - start
            remaining     = total_seconds - elapsed
            remaining_int = int(remaining)
            current_label = _current_track_label(tracks, elapsed) if tracks else label
            if remaining <= 0:
                print(_t("done", label=current_label))
                _beep(440, 1000)
                break
            mins, secs = divmod(remaining_int, 60)
            print(_t("playing", label=current_label, mm=mins, ss=secs), end="", flush=True)
            for w in WARN_TIMES:
                if remaining_int <= w and w not in warned:
                    warned.add(w)
                    print()
                    _beep_warn(w)
                    print()
            time.sleep(0.5)
    except KeyboardInterrupt:
        print(_t("stopped"))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_duration(s):
    s = s.strip()
    if ":" in s:
        mm, ss = s.split(":", 1)
        return int(mm) * 60 + int(ss)
    return int(s)


def _ask_manual_duration():
    while True:
        try:
            return _parse_duration(input(_t("dur_prompt")))
        except ValueError:
            print(_t("dur_hint"))


def _confirm_or_override(remaining):
    print(_t("modify_hint"), flush=True)
    t0      = time.time()
    pressed = None
    with _raw_mode():
        while time.time() - t0 < 5:
            if _kbhit():
                pressed = _getch()
                break
            time.sleep(0.1)
    if isinstance(pressed, bytes) and pressed.lower() == b"m":
        try:
            return _ask_manual_duration()
        except (KeyboardInterrupt, EOFError):
            pass
    return remaining


# ── Main loop ─────────────────────────────────────────────────────────────────

def mb_fetch_album_tracks(artist, album):
    album = _clean_album(album)
    try:
        releases = []
        for query in [
            f'release:"{album}" AND artist:"{artist}" AND format:Vinyl',
            f'release:"{album}" AND artist:"{artist}"',
        ]:
            time.sleep(1)
            data = _mb_get("release/", {"query": query, "limit": 3})
            releases = data.get("releases", [])
            if releases:
                break
        if not releases:
            return None
        rel = releases[0]
        time.sleep(1)
        rel_data = _mb_get(f"release/{rel['id']}", {"inc": "recordings+media"})
        return (rel_data.get("media", []), rel.get("title", album))
    except Exception as e:
        print(_t("mb_err", e=e))
        return None


def add_to_catalog():
    print(_t("add_title"))
    print(_t("catalog_list_title"))
    if CATALOG_DIR.exists():
        for f in sorted(CATALOG_DIR.glob("*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                print(f"  - {data.get('artist', '?')} - {data.get('album', '?')}")
            except Exception:
                continue
    try:
        artist = input(_t("artist_in")).strip()
        album  = input(_t("album_in")).strip()
    except (KeyboardInterrupt, EOFError):
        return
    if not artist or not album:
        return

    print(_t("search_msg"))
    result = mb_fetch_album_tracks(artist, album)
    if result is None:
        print(_t("not_found"))
        return

    media, release_title = result
    flat = _media_to_flat(media)
    print(_t("found", title=release_title, n=len(flat)))
    sides = ask_side_assignment(flat)
    saved = catalog_save(artist, album, sides)
    print(f"\n  {_t('saved', name=saved.name) if saved else _t('already')}")


def _run_detector():
    while True:
        try:
            music_start = wait_for_music()
        except (SystemExit, KeyboardInterrupt):
            return

        MAX_ATTEMPTS = 3
        result = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            print(_t("identifying", secs=SHAZAM_SECS))
            audio  = record_audio(SHAZAM_SECS)
            result = shazam_identify(audio)
            if result:
                break
            if attempt < MAX_ATTEMPTS:
                print(_t("shazam_retry", attempt=attempt, total=MAX_ATTEMPTS))

        remaining_tracks     = None
        tracks_for_countdown = None

        if not result:
            print(_t("shazam_fail"))
            try:
                remaining = _ask_manual_duration()
                label = _t("label_now")
            except KeyboardInterrupt:
                continue
        else:
            artist, title, isrc, album, shazam_offset = result
            print(_t("identified", artist=artist, title=title)
                  + (_t("album_suffix", album=album) if album else ""))
            if shazam_offset > 0:
                mm, ss = divmod(int(shazam_offset), 60)
                print(_t("position", mm=mm, ss=ss))
            print(_t("search_mb"))

            label  = f"{artist} — {title}"
            lookup = catalog_lookup(artist, album, title)
            source = _t("src_catalog")

            if lookup is None:
                mb_full = get_remaining_on_side(artist, title, isrc or None, album)
                if mb_full is not None:
                    remaining_mb, track_secs_mb, media = mb_full
                    flat = _media_to_flat(media)
                    print(_t("new_catalog", artist=artist, album=album))
                    sides = ask_side_assignment(flat)
                    saved = catalog_save(artist, album, sides)
                    if saved:
                        print(_t("saved", name=saved.name))
                    lookup = _find_in_sides(sides, title)
                    source = _t("src_new")

            if lookup is None:
                print(_t("no_tracklist"))
                try:
                    remaining  = _ask_manual_duration()
                    track_secs = 0
                except KeyboardInterrupt:
                    continue
            else:
                remaining, track_secs, remaining_tracks = lookup
                if track_secs > 0:
                    track_left = max(0, track_secs - int(shazam_offset))
                    mm, ss = divmod(track_left, 60)
                    print(_t("track_left", mm=mm, ss=ss, source=source))

        elapsed_since_start = int(time.time() - music_start)
        offset_correction = int(shazam_offset) if result else 0
        remaining = max(10, remaining - offset_correction - elapsed_since_start)

        if result and tracks_for_countdown is None and remaining_tracks:
            first_dur = max(0, track_secs - offset_correction - elapsed_since_start)
            tracks_for_countdown = [
                {"title": f"{artist} — {remaining_tracks[0]['title']}", "duration": first_dur},
                *[{"title": f"{artist} — {t['title']}", "duration": t["duration"]}
                  for t in remaining_tracks[1:]],
            ]

        mins, secs = divmod(remaining, 60)
        print(_t("side_left", mm=mins, ss=secs))
        remaining = _confirm_or_override(remaining)

        try:
            countdown(remaining, label, tracks=tracks_for_countdown)
        except KeyboardInterrupt:
            pass

        print(_t("next_side"))
        try:
            if input("> ").strip().upper() == "Q":
                break
        except KeyboardInterrupt:
            break


def _check_mic():
    if sys.platform == "darwin":
        import threading
        try:
            from AVFoundation import AVCaptureDevice, AVMediaTypeAudio
            from Foundation import NSRunLoop, NSDate
            done = threading.Event()
            def _cb(_):
                done.set()
            AVCaptureDevice.requestAccessForMediaType_completionHandler_(AVMediaTypeAudio, _cb)
            deadline = time.time() + 35
            while not done.is_set() and time.time() < deadline:
                NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.05))
        except Exception:
            pass
    try:
        probe = sd.rec(int(0.5 * SAMPLE_RATE), samplerate=SAMPLE_RATE, channels=1, dtype="int16")
        sd.wait()
    except Exception as e:
        print(_t("mic_err", e=e))
        raise SystemExit(1)
    rms = float(np.sqrt(np.mean(probe.astype(np.float32) ** 2))) / 32768
    if rms < 0.0001:
        print(_t("mic_silent"))
        raise SystemExit(1)


def main():
    _check_mic()
    print("\nVinylGuard")
    print("══════════════════════════════")

    while True:
        print(_t("menu"))
        try:
            choice = input("> ").strip().upper()
        except (KeyboardInterrupt, EOFError):
            return

        if choice in ("1", ""):
            _run_detector()
        elif choice == "2":
            add_to_catalog()
        elif choice == "Q":
            return


if __name__ == "__main__":
    main()
