"""
Phase 2: fetch synced lyrics from LRCLIB.

LRCLIB is a free, community-maintained synced-lyrics API (no API key).
Docs: https://lrclib.net/docs

We use the /api/get endpoint with track name + artist. Response includes
syncedLyrics — a string in .lrc format (timestamped lines).
"""
import re

import requests
from dataclasses import dataclass, field

LRCLIB_BASE = "https://lrclib.net/api"

# Line timestamp [mm:ss.xx] and inline word timestamp <mm:ss.xx> (enhanced LRC /
# "A2" extension). Fraction may be 2 or 3 digits; ":" is tolerated as the
# separator too. Both are compiled once and shared by the parser + shifter.
_LINE_TS = re.compile(r"\[(\d+):(\d+)(?:[.:](\d+))?\]")
_WORD_TS = re.compile(r"<(\d+):(\d+)(?:[.:](\d+))?>")


def _ts_to_ms(mm: str, ss: str, frac: str | None) -> int:
    ms = int(mm) * 60_000 + int(ss) * 1000
    if frac:
        ms += int(frac.ljust(3, "0")[:3])   # 2- or 3-digit fraction → ms
    return ms


@dataclass
class Word:
    """One karaoke unit within a line (a word, or a single CJK character).
    `time_ms` is its START, absolute from track start; the end is inferred at
    render time from the next word / next line."""
    text: str
    time_ms: int


@dataclass
class LyricLine:
    time_ms: int   # milliseconds from track start
    text: str
    words: list = field(default_factory=list)   # [] → line-level only (no per-word timing)

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


def _parse_words(body: str) -> list[Word]:
    """Extract per-word timing from a line body carrying <mm:ss.xx> tags (the
    part after the leading [mm:ss.xx]). Each tag times the text that follows it,
    up to the next tag. Returns [] for a plain line with no word tags."""
    matches = list(_WORD_TS.finditer(body))
    if not matches:
        return []
    words: list[Word] = []
    for i, m in enumerate(matches):
        start = _ts_to_ms(*m.groups())
        seg_end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        text = body[m.end():seg_end]
        if text != "":
            words.append(Word(text, start))
    return words


def parse_lrc(lrc: str) -> list[LyricLine]:
    """Parse .lrc text into a sorted list of LyricLine(time_ms, text, words).

    Handles plain LRC ([mm:ss.xx]text) AND enhanced LRC with inline word tags
    ([mm:ss.xx]<mm:ss.xx>word<mm:ss.xx>word …). For plain lines `words` is [].
    Some lines have multiple line timestamps; some have none (metadata like
    [ar:...]). Word timestamps are absolute, so they attach only when the line
    has a single [mm:ss.xx] tag (a repeated-timestamp line can't reuse them)."""
    lines: list[LyricLine] = []
    for raw in lrc.splitlines():
        line_tags = list(_LINE_TS.finditer(raw))
        if not line_tags:
            continue  # metadata or blank
        body = _LINE_TS.sub("", raw)                 # keeps <..> word tags
        words = _parse_words(body) if len(line_tags) == 1 else []
        if words:
            # Keep `text` and the word concatenation IDENTICAL: the karaoke fill
            # measures word widths against the rendered `text`, so any drift
            # (e.g. a trailing-space word that strip() would drop) misplaces the
            # sung/unsung edge. Trim outer whitespace off the edge words instead.
            words[0].text = words[0].text.lstrip()
            words[-1].text = words[-1].text.rstrip()
            words = [w for w in words if w.text]
            text = "".join(w.text for w in words)
        else:
            text = _WORD_TS.sub("", body).strip()
        for m in line_tags:
            lines.append(LyricLine(_ts_to_ms(*m.groups()), text, words))
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
    Enhanced-LRC word tags <mm:ss.xx> are shifted too, so a baked nudge keeps the
    per-word karaoke timing aligned.
    """
    if not delta_ms:
        return lrc

    def _shift(open_ch: str, close_ch: str):
        def _sub(m) -> str:
            t = max(0, _ts_to_ms(*m.groups()) + delta_ms)
            return (f"{open_ch}{t // 60_000:02d}:{(t % 60_000) // 1000:02d}"
                    f".{(t % 1000) // 10:02d}{close_ch}")
        return _sub

    lrc = _LINE_TS.sub(_shift("[", "]"), lrc)
    lrc = _WORD_TS.sub(_shift("<", ">"), lrc)
    return lrc


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
