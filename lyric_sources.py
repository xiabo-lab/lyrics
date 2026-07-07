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
import concurrent.futures
import hashlib
import html
import json
from pathlib import Path

import requests

from lrclib import (
    fetch_synced_lyrics as _fetch_lrclib,
    search_synced_lyrics as _search_lrclib,
)

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


# --- Aliases (user-corrected track identity) -------------------------------
# The RED picker's "Modify Search" lets the user fix a wrong/garbled title or
# artist. To make that fix DURABLE, we key the correction by the song the phone
# actually reports over AVRCP, so on a future play of the same song we resolve
# to the corrected name (and its cache entry) instead of re-searching a bad
# name. Stored as { "<orig artist>|<orig title>": {"artist","title"} } next to
# this script. Runtime data → gitignored, not shipped/overwritten by OTA.
ALIAS_PATH = Path(__file__).resolve().parent / "aliases.json"


def _aliases_load() -> dict:
    try:
        with open(ALIAS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as e:
        print(f"[alias] {ALIAS_PATH.name} unreadable ({e}); ignoring")
        return {}


def get_alias(track: str, artist: str) -> dict | None:
    """The corrected {"artist","title"} the user set for this AVRCP-reported
    song, or None. Keyed by the ORIGINAL (phone-reported) artist|title."""
    val = _aliases_load().get(_reject_key(track, artist))
    if isinstance(val, dict) and val.get("title") and val.get("artist"):
        return {"artist": val["artist"], "title": val["title"]}
    return None


def set_alias(orig_track: str, orig_artist: str,
              new_track: str, new_artist: str) -> None:
    """Remember that the phone-reported (orig_artist, orig_track) should be
    treated as (new_artist, new_track). A no-op if the name is unchanged."""
    new_track, new_artist = new_track.strip(), new_artist.strip()
    if not (new_track and new_artist):
        return
    if (new_track.lower() == orig_track.strip().lower()
            and new_artist.lower() == orig_artist.strip().lower()):
        return                                  # nothing corrected
    data = _aliases_load()
    data[_reject_key(orig_track, orig_artist)] = {
        "artist": new_artist, "title": new_track}
    try:
        with open(ALIAS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"[alias] {orig_artist} — {orig_track} → {new_artist} — {new_track}")
    except OSError as e:
        print(f"[alias] write error: {e}")


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


# --- Multi-result search (RED-button picker) -------------------------------
# When the user taps RED we no longer just reject + advance one source; we
# gather a CONSOLIDATED list of candidate lyrics from every source so they can
# pick the right version by hand. Balanced 2 per source across the 4 sources
# (sum = 8, fills the 8 result cells; the 9th grid cell is Modify Search).
CANDIDATE_CAPS = (("QQ", 2), ("Kugou", 2), ("NetEase", 2), ("LRCLIB", 2))
# The picker shows at most this many results (8 cells; cell 9 is the button).
GRID_MAX = 8
# Query each source a little deeper than its 2-cap so that when a source
# returns fewer than 2 (or none), we can BACKFILL the empty cells from the
# sources that have extras and still fill up to 8. 4 lets two working sources
# cover all 8 on their own.
CANDIDATE_QUERY_LIMIT = 4


def _qq_search_list(track: str, artist: str, limit: int,
                    timeout: float = 10) -> list[tuple[str, str, str]]:
    """Up to `limit` (songmid, title, artist) QQ matches for (track, artist)."""
    query = f"{track} {artist}".strip()
    if not query:
        return []
    r = requests.get(
        QQ_SEARCH,
        params={"w": query, "format": "json", "n": max(limit, 1), "p": 1},
        headers=QQ_HEADERS, timeout=timeout)
    r.raise_for_status()
    songs = (((r.json().get("data") or {}).get("song")) or {}).get("list") or []
    out = []
    for s in songs[:limit]:
        mid = s.get("songmid")
        if not mid:
            continue
        names = "/".join(x.get("name", "") for x in (s.get("singer") or [])
                         if x.get("name"))
        out.append((mid, s.get("songname") or track, names or artist))
    return out


def qq_candidates(track: str, artist: str, limit: int) -> list[dict]:
    out: list[dict] = []
    try:
        for mid, title, a in _qq_search_list(track, artist, limit):
            try:
                lrc = _qq_lyric(mid)
            except requests.RequestException:
                lrc = None
            if lrc:
                out.append({"source": "QQ", "title": title,
                            "artist": a, "lrc": lrc})
            if len(out) >= limit:
                break
    except requests.RequestException as e:
        print(f"[qq] candidates error: {e}")
    return out


def _kugou_search_list(track: str, artist: str, limit: int,
                       timeout: float = 10) -> list[tuple[str, str, str]]:
    """Up to `limit` (hash, title, artist) Kugou matches for (track, artist)."""
    query = f"{track} {artist}".strip()
    if not query:
        return []
    r = requests.get(
        KUGOU_SEARCH,
        params={"format": "json", "keyword": query, "page": 1,
                "pagesize": max(limit, 1), "showtype": 1},
        headers=KUGOU_HEADERS, timeout=timeout)
    r.raise_for_status()
    songs = (r.json().get("data") or {}).get("info") or []
    out = []
    for s in songs[:limit]:
        h = s.get("hash")
        if not h:
            continue
        out.append((h, s.get("songname") or track, s.get("singername") or artist))
    return out


def kugou_candidates(track: str, artist: str, limit: int) -> list[dict]:
    out: list[dict] = []
    try:
        for h, title, a in _kugou_search_list(track, artist, limit):
            try:
                lrc = _kugou_lyric(h)
            except requests.RequestException:
                lrc = None
            if lrc:
                out.append({"source": "Kugou", "title": title,
                            "artist": a, "lrc": lrc})
            if len(out) >= limit:
                break
    except requests.RequestException as e:
        print(f"[kugou] candidates error: {e}")
    return out


def _netease_search_list(track: str, artist: str, limit: int,
                         timeout: float = 10) -> list[tuple[int, str, str]]:
    """Up to `limit` (song_id, title, artist) NetEase matches."""
    query = f"{track} {artist}".strip()
    if not query:
        return []
    r = requests.get(
        NETEASE_SEARCH,
        params={"s": query, "type": 1, "limit": max(limit, 1)},
        headers=NETEASE_HEADERS, timeout=timeout)
    r.raise_for_status()
    songs = ((r.json().get("result") or {}).get("songs")) or []
    out = []
    for s in songs[:limit]:
        sid = s.get("id")
        if sid is None:
            continue
        names = "/".join(x.get("name", "") for x in (s.get("artists") or [])
                         if x.get("name"))
        out.append((sid, s.get("name") or track, names or artist))
    return out


def netease_candidates(track: str, artist: str, limit: int) -> list[dict]:
    out: list[dict] = []
    try:
        for sid, title, a in _netease_search_list(track, artist, limit):
            try:
                lrc = _netease_lyric(sid)
            except requests.RequestException:
                lrc = None
            if lrc:
                out.append({"source": "NetEase", "title": title,
                            "artist": a, "lrc": lrc})
            if len(out) >= limit:
                break
    except requests.RequestException as e:
        print(f"[netease] candidates error: {e}")
    return out


def lrclib_candidates(track: str, artist: str, limit: int) -> list[dict]:
    out: list[dict] = []
    try:
        for title, a, lrc in _search_lrclib(track, artist, limit):
            out.append({"source": "LRCLIB", "title": title,
                        "artist": a, "lrc": lrc})
    except requests.RequestException as e:
        print(f"[lrclib] candidates error: {e}")
    return out


_CANDIDATE_FNS = {"QQ": qq_candidates, "Kugou": kugou_candidates,
                  "NetEase": netease_candidates, "LRCLIB": lrclib_candidates}


def search_candidates(track: str, artist: str, progress=None) -> list[dict]:
    """Consolidated candidate list across every source for the RED-button
    picker: up to 2 per source, backfilled to GRID_MAX (8) total. Each item is
    {"source", "title", "artist", "lrc"}. Sources are queried independently and
    a failing one simply contributes nothing. `progress(name)` (optional) is
    called before each source is queried, for a live status line; it must not
    raise.

    Selection is two-pass so the grid feels balanced but never wastes cells:
      1. take up to each source's cap (2) in source order — the "one row each"
         core;
      2. if that leaves fewer than 8, backfill round-robin from each source's
         leftovers (results beyond its cap) until 8 are filled or nothing's
         left. So a thin/empty source is covered by whichever sources have
         extras, instead of leaving a hole.

    The four sources are queried CONCURRENTLY (one thread each) so the total
    wait is ~the slowest single source, not the sum — otherwise the deeper
    per-source query (needed for backfill) makes the grid take too long to
    appear. A source that errors or exceeds the timeout simply contributes
    nothing."""
    if progress is not None:
        try:
            progress("all sources")
        except Exception:
            pass
    by_name: dict[str, list[dict]] = {}
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=len(CANDIDATE_CAPS))
    try:
        futs = {name: ex.submit(_CANDIDATE_FNS[name], track, artist,
                                CANDIDATE_QUERY_LIMIT)
                for name, _cap in CANDIDATE_CAPS}
        for name, fut in futs.items():
            try:
                by_name[name] = fut.result(timeout=25)
            except Exception as e:
                print(f"[picker] {name} failed: {e}")
                by_name[name] = []
            print(f"[picker] {name}: {len(by_name[name])} candidate(s)")
    finally:
        # Don't block the grid on a straggler source: let any still-running
        # query finish in the background (its request has its own 10s timeout)
        # and be discarded. The futures already collected are unaffected.
        ex.shutdown(wait=False)
    per_source = [by_name[name] for name, _cap in CANDIDATE_CAPS]
    caps = [cap for _name, cap in CANDIDATE_CAPS]
    # Pass 1: the per-source core (≤ cap each, in source order).
    out: list[dict] = []
    for items, cap in zip(per_source, caps):
        out.extend(items[:cap])
    # Pass 2: backfill to GRID_MAX from leftovers, round-robin across sources.
    nxt = list(caps)                       # next unused index per source
    made_progress = True
    while len(out) < GRID_MAX and made_progress:
        made_progress = False
        for si, items in enumerate(per_source):
            if len(out) >= GRID_MAX:
                break
            if nxt[si] < len(items):
                out.append(items[nxt[si]])
                nxt[si] += 1
                made_progress = True
    return out[:GRID_MAX]


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
