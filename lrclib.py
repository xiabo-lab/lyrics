"""
Phase 2: fetch synced lyrics from LRCLIB.

LRCLIB is a free, community-maintained synced-lyrics API (no API key).
Docs: https://lrclib.net/docs

We use the /api/get endpoint with track name + artist. Response includes
syncedLyrics — a string in .lrc format (timestamped lines).
"""
import requests
from dataclasses import dataclass

LRCLIB_BASE = "https://lrclib.net/api"

@dataclass
class LyricLine:
    time_ms: int   # milliseconds from track start
    text: str

def fetch_synced_lyrics(track: str, artist: str, album: str = "", duration: int = 0) -> str | None:
    """Return raw .lrc text, or None if no synced lyrics found."""
    params = {"track_name": track, "artist_name": artist}
    if album:
        params["album_name"] = album
    if duration:
        params["duration"] = duration

    r = requests.get(f"{LRCLIB_BASE}/get", params=params, timeout=10)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    data = r.json()
    return data.get("syncedLyrics")

def search_synced_lyrics(track: str, artist: str, limit: int = 2,
                         timeout: float = 10) -> list[tuple[str, str, str]]:
    """Return up to `limit` (trackName, artistName, lrc) tuples that have synced
    lyrics, via LRCLIB's /search endpoint (which can return several matches for
    one query). Used to populate the multi-source candidate picker. Both the
    track name and artist are sent so the matches stay relevant."""
    params = {"track_name": track, "artist_name": artist}
    r = requests.get(f"{LRCLIB_BASE}/search", params=params, timeout=timeout)
    if r.status_code == 404:
        return []
    r.raise_for_status()
    out: list[tuple[str, str, str]] = []
    for item in r.json() or []:
        lrc = item.get("syncedLyrics")
        if not lrc or "[" not in lrc:   # plain/unsynced → useless to us
            continue
        out.append((item.get("trackName") or track,
                    item.get("artistName") or artist, lrc))
        if len(out) >= limit:
            break
    return out


def parse_lrc(lrc: str) -> list[LyricLine]:
    """Parse .lrc text into a sorted list of (time_ms, text) lines.

    .lrc format: [mm:ss.xx]lyric text
    Some lines have multiple timestamps; some have none (metadata like [ar:...]).
    """
    import re
    pattern = re.compile(r"\[(\d+):(\d+)(?:\.(\d+))?\]")
    lines: list[LyricLine] = []
    for raw in lrc.splitlines():
        timestamps = list(pattern.finditer(raw))
        if not timestamps:
            continue  # metadata or blank
        text = pattern.sub("", raw).strip()
        for m in timestamps:
            mm, ss, frac = m.groups()
            t_ms = int(mm) * 60_000 + int(ss) * 1000
            if frac:
                # frac can be 2 or 3 digits — normalize to ms
                t_ms += int(frac.ljust(3, "0")[:3])
            lines.append(LyricLine(t_ms, text))
    lines.sort(key=lambda l: l.time_ms)
    return lines

def shift_lrc_timestamps(lrc: str, delta_ms: int) -> str:
    """Return `lrc` with every [mm:ss.xx] timestamp shifted by `delta_ms`
    (clamped at 0), re-emitted as [mm:ss.cc] centiseconds.

    Used to *bake* a confirmed per-song sync nudge into the cached file so it
    survives restarts: the live nudge (State.song_offset_ms) is added to the
    playback clock, so saving timestamps shifted by -song_offset_ms reproduces
    the same on-screen timing with no nudge on the next play.

    Only real time tags (digits:digits) are touched; metadata like [ar:...],
    [ti:...] or [offset:...] don't match the pattern and pass through unchanged.
    """
    if not delta_ms:
        return lrc
    import re
    ts = re.compile(r"\[(\d+):(\d+)(?:\.(\d+))?\]")

    def _shift(m) -> str:
        mm, ss, frac = m.groups()
        t = int(mm) * 60_000 + int(ss) * 1000
        if frac:
            t += int(frac.ljust(3, "0")[:3])
        t = max(0, t + delta_ms)
        return f"[{t // 60_000:02d}:{(t % 60_000) // 1000:02d}.{(t % 1000) // 10:02d}]"

    return ts.sub(_shift, lrc)


if __name__ == "__main__":
    # Test with a song that's reliably in LRCLIB.
    track = "Bohemian Rhapsody"
    artist = "Queen"
    print(f"Fetching {track!r} by {artist!r}...")
    lrc = fetch_synced_lyrics(track, artist)
    if not lrc:
        print("No synced lyrics found.")
    else:
        lines = parse_lrc(lrc)
        print(f"Got {len(lines)} timestamped lines. First 10:")
        for line in lines[:10]:
            m, s = divmod(line.time_ms // 1000, 60)
            ms = line.time_ms % 1000
            print(f"  [{m:02d}:{s:02d}.{ms:03d}] {line.text}")
