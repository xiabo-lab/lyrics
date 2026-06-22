"""Multi-source synced-lyrics fetcher with local cache.

Cascade: local cache → QQ Music (QQ音乐) → Kugou (酷狗音乐) → NetEase Cloud
Music (网易云音乐) → LRCLIB. Chinese-catalog sources lead because the library
is mostly Chinese; LRCLIB is the Western-leaning fallback. Returns raw LRC
text (parseable by lrclib.parse_lrc) or None.

Cache location: ./cache/ next to this file. Hits are silent network-free
returns; misses fall through to the online cascade and the result is
written to disk for next time.

Negative results (all sources missed) are intentionally NOT cached, so
if a source adds lyrics later we'll pick them up on the next play.

NetEase, QQ and Kugou endpoints are public/unofficial; no API key required.
They may change without notice — the _NETEASE_* / _QQ_* / KUGOU_* constants
are the knobs if they break.
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
from pathlib import Path

import requests

from lrclib import fetch_synced_lyrics as _fetch_lrclib

# --- Local cache -----------------------------------------------------------
CACHE_DIR = Path(__file__).resolve().parent / "cache"


def _cache_path(track: str, artist: str) -> Path:
    """Deterministic cache filename: SHA-1 of normalized (artist|title)."""
    key = f"{artist.strip().lower()}|{track.strip().lower()}".encode("utf-8")
    h = hashlib.sha1(key).hexdigest()[:16]
    return CACHE_DIR / f"{h}.lrc"


def _cache_load(track: str, artist: str) -> str | None:
    p = _cache_path(track, artist)
    if not p.exists():
        return None
    try:
        return p.read_text(encoding="utf-8")
    except OSError as e:
        print(f"[cache] read error for {p.name}: {e}")
        return None


def _cache_save(track: str, artist: str, lrc: str) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        p = _cache_path(track, artist)
        # Prepend a comment line with the original metadata for debuggability
        # — the LRC parser ignores anything before the first [mm:ss.xxx] tag.
        header = f"# cached: {artist} — {track}\n"
        p.write_text(header + lrc, encoding="utf-8")
        print(f"[cache] saved → {p.name}")
    except OSError as e:
        print(f"[cache] write error: {e}")


def save_to_cache(track: str, artist: str, lrc: str) -> None:
    """Public hook: persist a confirmed-correct LRC. Called when the user
    taps the GREEN button — we no longer auto-cache fetch results, so a
    lyric only sticks once a human says it matched the song."""
    _cache_save(track, artist, lrc)


def delete_from_cache(track: str, artist: str) -> None:
    """Drop a song's cached LRC. Called when the user taps RED on a cache
    hit — the stored lyric was wrong, so evict it and let the next fetch
    re-search. Safe if the entry is already gone."""
    p = _cache_path(track, artist)
    try:
        p.unlink()
        print(f"[cache] deleted → {p.name}")
    except FileNotFoundError:
        pass
    except OSError as e:
        print(f"[cache] delete error: {e}")


# --- Rejections (user said "wrong") ----------------------------------------
# When the user taps RED, the source that produced those lyrics is recorded
# here per-song. The cascade then skips it on every future fetch (even after
# a cache wipe), so we move on to the next source instead of re-serving the
# wrong match. Stored as { "artist|title": ["QQ", ...] } next to this script.
REJECT_PATH = Path(__file__).resolve().parent / "rejections.json"


def _reject_key(track: str, artist: str) -> str:
    return f"{artist.strip().lower()}|{track.strip().lower()}"


def _rejections_load() -> dict:
    try:
        with open(REJECT_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as e:
        print(f"[reject] {REJECT_PATH.name} unreadable ({e}); ignoring")
        return {}


def get_rejections(track: str, artist: str) -> list:
    """Sources the user has marked wrong for this song."""
    val = _rejections_load().get(_reject_key(track, artist), [])
    return val if isinstance(val, list) else []


def add_rejection(track: str, artist: str, source: str) -> None:
    """Record that `source` gave wrong lyrics for this song. 'cache' is never
    recorded (it has no upstream source to skip)."""
    if not source or source == "cache":
        return
    data = _rejections_load()
    key = _reject_key(track, artist)
    lst = data.get(key) if isinstance(data.get(key), list) else []
    if source not in lst:
        lst.append(source)
    data[key] = lst
    try:
        with open(REJECT_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"[reject] {source} marked wrong → {artist} — {track}")
    except OSError as e:
        print(f"[reject] write error: {e}")


# --- NetEase (网易云音乐) ----------------------------------------------------
NETEASE_SEARCH = "https://music.163.com/api/search/get/"
NETEASE_LYRIC = "https://music.163.com/api/song/lyric"
NETEASE_HEADERS = {
    "Referer": "https://music.163.com/",
    "User-Agent": "Mozilla/5.0 (X11; Linux aarch64) carlyric/1.0",
}


def _netease_search_id(track: str, artist: str, timeout: float = 10) -> int | None:
    """Find the best-matching NetEase song ID for a (track, artist)."""
    query = f"{track} {artist}".strip()
    if not query:
        return None
    r = requests.get(
        NETEASE_SEARCH,
        params={"s": query, "type": 1, "limit": 5},
        headers=NETEASE_HEADERS,
        timeout=timeout,
    )
    r.raise_for_status()
    data = r.json()
    songs = (data.get("result") or {}).get("songs") or []
    if not songs:
        return None
    # Prefer a song whose artist name overlaps with what AVRCP gave us.
    artist_lc = artist.lower()
    for s in songs:
        names = [a.get("name", "").lower() for a in (s.get("artists") or [])]
        if any(artist_lc in n or n in artist_lc for n in names if n):
            return s["id"]
    return songs[0]["id"]


def _netease_lyric(song_id: int, timeout: float = 10) -> str | None:
    """Pull the LRC blob for a NetEase song ID. Returns None if unsynced."""
    r = requests.get(
        NETEASE_LYRIC,
        params={"id": song_id, "lv": -1, "kv": -1, "tv": -1},
        headers=NETEASE_HEADERS,
        timeout=timeout,
    )
    r.raise_for_status()
    data = r.json()
    lrc = (data.get("lrc") or {}).get("lyric") or ""
    # NetEase returns plain text "暂无歌词" (no lyrics) sometimes — reject it.
    if not lrc.strip():
        return None
    if "[" not in lrc:  # no timestamps → useless to us
        return None
    return lrc


def fetch_netease(track: str, artist: str) -> str | None:
    try:
        sid = _netease_search_id(track, artist)
        if sid is None:
            return None
        return _netease_lyric(sid)
    except requests.RequestException as e:
        print(f"[netease] error: {e}")
        return None


# --- QQ Music (QQ音乐) ------------------------------------------------------
QQ_SEARCH = "https://c.y.qq.com/soso/fcgi-bin/client_search_cp"
QQ_LYRIC = "https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg"
QQ_HEADERS = {
    "Referer": "https://y.qq.com/",
    "User-Agent": "Mozilla/5.0 (X11; Linux aarch64) carlyric/1.0",
}


def _qq_search_mid(track: str, artist: str, timeout: float = 10) -> str | None:
    """Find the best-matching QQ Music songmid for a (track, artist)."""
    query = f"{track} {artist}".strip()
    if not query:
        return None
    r = requests.get(
        QQ_SEARCH,
        params={"w": query, "format": "json", "n": 5, "p": 1},
        headers=QQ_HEADERS,
        timeout=timeout,
    )
    r.raise_for_status()
    data = r.json()
    songs = (((data.get("data") or {}).get("song")) or {}).get("list") or []
    if not songs:
        return None
    artist_lc = artist.lower()
    for s in songs:
        singers = s.get("singer") or []
        names = [a.get("name", "").lower() for a in singers]
        if any(artist_lc in n or n in artist_lc for n in names if n):
            return s.get("songmid")
    return songs[0].get("songmid")


def _qq_lyric(songmid: str, timeout: float = 10) -> str | None:
    """Pull the LRC blob for a QQ songmid. Returns None if unsynced."""
    r = requests.get(
        QQ_LYRIC,
        params={"songmid": songmid, "format": "json", "nobase64": 1},
        headers=QQ_HEADERS,
        timeout=timeout,
    )
    r.raise_for_status()
    data = r.json()
    lyric = data.get("lyric") or ""
    if not lyric.strip() or "[" not in lyric:
        return None
    # QQ returns the LRC with HTML entities (&apos;, &quot;, etc.) escaped.
    return html.unescape(lyric)


def fetch_qq(track: str, artist: str) -> str | None:
    try:
        mid = _qq_search_mid(track, artist)
        if not mid:
            return None
        return _qq_lyric(mid)
    except requests.RequestException as e:
        print(f"[qq] error: {e}")
        return None


# --- Kugou (酷狗音乐) -------------------------------------------------------
# Three-step flow: search for the song's hash, ask krcs for a matching lyric
# candidate (id + accesskey), then download that candidate as base64 LRC from
# lyrics.kugou.com. All public/unofficial — no key.
# NOTE: plain HTTP — mobilecdn.kugou.com (a CDN host) serves a TLS cert that
# doesn't match its hostname, so HTTPS fails cert verification. These are
# public lyric endpoints with no auth, so cleartext is fine here.
KUGOU_SEARCH = "http://mobilecdn.kugou.com/api/v3/search/song"
KUGOU_LYRIC_SEARCH = "http://krcs.kugou.com/search"
KUGOU_LYRIC_DOWNLOAD = "http://lyrics.kugou.com/download"
KUGOU_HEADERS = {
    "Referer": "https://www.kugou.com/",
    "User-Agent": "Mozilla/5.0 (X11; Linux aarch64) carlyric/1.0",
}


def _kugou_search_hash(track: str, artist: str, timeout: float = 10) -> str | None:
    """Find the best-matching Kugou song hash for a (track, artist)."""
    query = f"{track} {artist}".strip()
    if not query:
        return None
    r = requests.get(
        KUGOU_SEARCH,
        params={"format": "json", "keyword": query, "page": 1,
                "pagesize": 5, "showtype": 1},
        headers=KUGOU_HEADERS,
        timeout=timeout,
    )
    r.raise_for_status()
    data = r.json()
    songs = (data.get("data") or {}).get("info") or []
    if not songs:
        return None
    artist_lc = artist.lower()
    for s in songs:
        singer = (s.get("singername") or "").lower()
        if singer and (artist_lc in singer or singer in artist_lc):
            return s.get("hash")
    return songs[0].get("hash")


def _kugou_lyric(song_hash: str, timeout: float = 10) -> str | None:
    """Resolve a lyric candidate for a Kugou hash and download it as LRC.
    Returns None if there's no candidate or the result isn't synced."""
    # Step 1: candidate (id + accesskey) for this song hash.
    r = requests.get(
        KUGOU_LYRIC_SEARCH,
        params={"ver": 1, "man": "yes", "client": "mobi", "hash": song_hash},
        headers=KUGOU_HEADERS,
        timeout=timeout,
    )
    r.raise_for_status()
    candidates = r.json().get("candidates") or []
    if not candidates:
        return None
    cid = candidates[0].get("id")
    accesskey = candidates[0].get("accesskey")
    if not cid or not accesskey:
        return None
    # Step 2: download that candidate as base64-encoded LRC.
    r = requests.get(
        KUGOU_LYRIC_DOWNLOAD,
        params={"ver": 1, "client": "pc", "id": cid, "accesskey": accesskey,
                "fmt": "lrc", "charset": "utf8"},
        headers=KUGOU_HEADERS,
        timeout=timeout,
    )
    r.raise_for_status()
    content = r.json().get("content") or ""
    if not content:
        return None
    try:
        # utf-8-sig drops the leading BOM Kugou prepends to its LRC blobs.
        lrc = base64.b64decode(content).decode("utf-8-sig", "replace")
    except (ValueError, UnicodeDecodeError):
        return None
    if not lrc.strip() or "[" not in lrc:  # empty or no timestamps → useless
        return None
    return lrc


def fetch_kugou(track: str, artist: str) -> str | None:
    try:
        song_hash = _kugou_search_hash(track, artist)
        if not song_hash:
            return None
        return _kugou_lyric(song_hash)
    except requests.RequestException as e:
        print(f"[kugou] error: {e}")
        return None


def fetch_lrclib(track: str, artist: str) -> str | None:
    """LRCLIB wrapper that swallows network errors, matching fetch_qq/_netease
    so all sources share one calling convention in the cascade."""
    try:
        return _fetch_lrclib(track, artist)
    except requests.RequestException as e:
        print(f"[lrclib] error: {e}")
        return None


# Library is mostly Chinese, so the Chinese-catalog sources (QQ, Kugou,
# NetEase) go first — they match Chinese tracks far more reliably. LRCLIB is
# the crowd-sourced/Western-leaning fallback, last so its loose matches can't
# win ahead of a correct Chinese result. The names here are what gets stored
# in rejections.json, so keep them stable.
_SOURCES = (("QQ", fetch_qq), ("Kugou", fetch_kugou),
            ("NetEase", fetch_netease), ("LRCLIB", fetch_lrclib))


# --- Combined cascade ------------------------------------------------------
def fetch_synced_lyrics_any(track: str, artist: str,
                            progress=None) -> tuple[str | None, str | None]:
    """Cache, then each online source, until one returns usable synced lyrics.

    Returns (lrc, source) where source is "cache" / "QQ" / "Kugou" /
    "NetEase" / "LRCLIB", or (None, None) if nothing matched. Sources the user
    previously marked wrong for this song are skipped. Network results are NOT
    cached
    here — caching now waits for the user's GREEN confirmation (see
    save_to_cache).

    `progress`, if given, is called with each source's name just before that
    source is queried (e.g. for a live "Searching QQ…" status on the display).
    It must not raise — a failing callback is swallowed so it can't break the
    fetch.
    """
    # 1. Local cache — fastest, no network. A cached entry was already
    #    confirmed by the user, so we trust it.
    cached = _cache_load(track, artist)
    if cached:
        print(f"[cache] hit: {artist} — {track}")
        return cached, "cache"
    print(f"[cache] miss: {artist} — {track}")

    rejected = get_rejections(track, artist)
    if rejected:
        print(f"[lyrics] skipping rejected sources: {rejected}")

    for name, fn in _SOURCES:
        if name in rejected:
            continue
        if progress is not None:
            try:
                progress(name)
            except Exception:
                pass
        print(f"[lyrics] {name}: {artist} — {track}")
        lrc = fn(track, artist)
        if lrc:
            print(f"[lyrics] {name} hit (awaiting confirm — not cached yet)")
            return lrc, name

    # Intentionally NOT caching the negative result — leave the door open
    # to picking up lyrics if a source adds them later.
    print("[lyrics] no source had it")
    return None, None


if __name__ == "__main__":
    # Quick smoke test from the shell.
    import sys
    if len(sys.argv) >= 3:
        t, a = sys.argv[1], sys.argv[2]
    else:
        t, a = "世间美好与你环环相扣", "柏松"
    out, src = fetch_synced_lyrics_any(t, a)
    if out:
        print(f"--- (from {src})")
        print(out[:500])
    else:
        print("nothing found")
