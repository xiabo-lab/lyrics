"""Multi-source synced-lyrics fetcher with local cache.

Lookup: local cache → a single CONCURRENT sweep of QQ Music (QQ音乐), Kugou
(酷狗音乐), NetEase Cloud Music (网易云音乐) and LRCLIB. Every candidate every
source returns is scored against the requested artist + title and the highest
scorer wins — a source's first search hit is often the wrong version of a song,
so we compare them all rather than trusting whoever answers first. Returns raw
LRC text (parseable by lrclib.parse_lrc) or None.

The same sweep populates the RED-button picker grid (see fetch_best_lyrics),
so opening the picker after an automatic lookup costs no network at all.

Cache location: ./cache/ next to this file. Hits are silent network-free
returns; misses fall through to the online sweep and the result is
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
import difflib
import hashlib
import html
import json
import re
import unicodedata
import zlib
from pathlib import Path

import requests

from lrclib import search_synced_lyrics as _search_lrclib

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


# --- QQ Music (QQ音乐) ------------------------------------------------------
# Search via the unified musicu.fcg service. The older client_search_cp endpoint
# started returning HTTP 500 (empty body) for EVERY query around 2026-07, which
# silently dropped QQ from all results; musicu.fcg is the current desktop-client
# search API and returns songmids the lyric endpoint below still resolves.
QQ_SEARCH = "https://u.y.qq.com/cgi-bin/musicu.fcg"
QQ_LYRIC = "https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg"
QQ_HEADERS = {
    "Referer": "https://y.qq.com/",
    "User-Agent": "Mozilla/5.0 (X11; Linux aarch64) carlyric/1.0",
}


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


# Kugou KRC (word-by-word) decryption + conversion to enhanced LRC.
# KRC blobs are base64 → a 4-byte "krc1" magic → the rest XOR'd with this 16-byte
# key (cycled) → zlib-deflated text. The text is line-level `[start,dur]` tags
# followed by per-word `<offset,dur,0>text` (offset relative to the line start).
_KRC_KEY = bytes([0x40, 0x47, 0x61, 0x77, 0x5e, 0x32, 0x74, 0x47,
                  0x51, 0x36, 0x31, 0x2d, 0xce, 0xd2, 0x6e, 0x69])
_KRC_LINE_RE = re.compile(r"^\[(\d+),(\d+)\](.*)$")
_KRC_WORD_RE = re.compile(r"<(\d+),(\d+),\d+>([^<]*)")


def _lrc_ts(ms: int, open_ch: str, close_ch: str) -> str:
    """Format `ms` as an LRC tag `[mm:ss.cc]` / word tag `<mm:ss.cc>`."""
    return (f"{open_ch}{ms // 60_000:02d}:{(ms % 60_000) // 1000:02d}"
            f".{(ms % 1000) // 10:02d}{close_ch}")


def _krc_decrypt(b64: str) -> str | None:
    """Decrypt a base64 KRC blob to its plain KRC text, or None on any failure."""
    try:
        data = base64.b64decode(b64)
    except (ValueError, TypeError):
        return None
    if data[:4] != b"krc1":
        return None
    body = bytes(b ^ _KRC_KEY[i % 16] for i, b in enumerate(data[4:]))
    try:
        return zlib.decompress(body).decode("utf-8", "replace")
    except (zlib.error, ValueError):
        return None


def _krc_to_enhanced_lrc(krc: str) -> str | None:
    """Convert decrypted KRC text to enhanced LRC: `[mm:ss.cc]<mm:ss.cc>word…`
    with ABSOLUTE per-word timestamps (line start + word offset). Metadata lines
    like [ti:...]/[language:...] are skipped. Returns None if no timed lines."""
    out: list[str] = []
    for raw in krc.splitlines():
        m = _KRC_LINE_RE.match(raw)
        if not m:
            continue                       # [ti:]/[ar:]/[language:] etc.
        line_start = int(m.group(1))
        words = _KRC_WORD_RE.findall(m.group(3))
        if not words:
            continue
        parts = [_lrc_ts(line_start, "[", "]")]
        for off, _dur, text in words:
            parts.append(_lrc_ts(line_start + int(off), "<", ">") + text)
        out.append("".join(parts))
    return "\n".join(out) if out else None


def _kugou_lyric(song_hash: str, timeout: float = 10) -> str | None:
    """Resolve a lyric candidate for a Kugou hash and download it, preferring the
    word-by-word KRC (converted to enhanced LRC) and falling back to plain LRC.
    Returns None if there's no candidate or nothing synced."""
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

    def _download(fmt: str) -> str:
        resp = requests.get(
            KUGOU_LYRIC_DOWNLOAD,
            params={"ver": 1, "client": "pc", "id": cid, "accesskey": accesskey,
                    "fmt": fmt, "charset": "utf8"},
            headers=KUGOU_HEADERS, timeout=timeout)
        resp.raise_for_status()
        return resp.json().get("content") or ""

    # Step 2a: try KRC (per-word). A song without KRC returns an empty/garbage
    # blob that decrypt/convert rejects, so we fall through to plain LRC.
    try:
        krc = _krc_decrypt(_download("krc"))
        enhanced = _krc_to_enhanced_lrc(krc) if krc else None
        if enhanced:
            return enhanced
    except requests.RequestException:
        pass  # fall back to LRC below

    # Step 2b: plain LRC fallback.
    content = _download("lrc")
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


# --- Multi-result search (auto best-match + RED-button picker) -------------
# One sweep serves both jobs: every source's candidates are scored against the
# requested (title, artist) to pick what we display, and the same results back
# the picker grid so RED needs no second network round-trip. Balanced 2 per
# source across the 4 sources (sum = 8, fills the 8 result cells; the 9th grid
# cell is Modify Search).
CANDIDATE_CAPS = (("QQ", 1), ("Kugou", 2), ("NetEase", 2), ("LRCLIB", 2))
# The picker shows at most this many results (8 cells; cell 9 is the button).
GRID_MAX = 8
# Picker grid: drop any candidate whose title+artist similarity to the query is
# below this. Search engines (esp. QQ) happily return a same-named but totally
# different song — e.g. the Japanese "ドラえもんのうた" for a Mandarin "多啦A夢 / 陳慧琳"
# — and those cluttered the picker. Loose enough to keep legitimate
# same-artist/alternate-title matches; the auto-pick is scored separately and is
# always pinned into the grid regardless of this filter.
GRID_RELEVANCE_MIN = 0.3
# Query each source a little deeper than its 2-cap so that when a source
# returns fewer than 2 (or none), we can BACKFILL the empty cells from the
# sources that have extras and still fill up to 8. 4 lets two working sources
# cover all 8 on their own.
CANDIDATE_QUERY_LIMIT = 4
# Per-source overrides of CANDIDATE_QUERY_LIMIT. QQ's search relevance is poor
# (it over-returns unrelated same-name songs), so take ONLY its single top hit
# rather than deep-querying it — keeps QQ noise out and never backfills from QQ.
CANDIDATE_QUERY_LIMITS = {"QQ": 1}


# --- Match scoring ----------------------------------------------------------
# Search engines happily return "《歌名》(Live)" or a cover by another singer as
# result #1, so we can't trust position. Every candidate is scored on how well
# its own reported title + artist match what the phone asked for, and the best
# score wins across ALL sources.

# Everything that shouldn't affect a match: spacing, case, width, and the
# punctuation the four catalogues sprinkle differently around the same song.
_PUNCT_RE = re.compile(
    r"[\s\-_·・,，.。!！?？'’‘\"“”:：;；/\\|~〜*&+()（）\[\]【】「」『』<>《》]+")
# Candidate artist fields arrive as "周杰伦/费玉清", "A & B", "X feat. Y"…
_ARTIST_SPLIT_RE = re.compile(r"[/,&;、，]|\bfeat\.?\b|\bft\.?\b|\bwith\b",
                              re.IGNORECASE)
# A real synced lyric has many [mm:ss] lines; some sources hand back a stub
# holding only credits ("[00:00.00]作词：…"). Score those below any real match.
_TIMETAG_RE = re.compile(r"\[\d+:\d+")
MIN_SYNCED_LINES = 5
STUB_PENALTY = 0.35
# Title carries more signal than artist: AVRCP artist strings are often the
# album artist, a group name, or blank, while the title is nearly always right.
TITLE_WEIGHT = 0.65
ARTIST_WEIGHT = 0.35
# Pure tie-break, never enough to overturn a real difference in similarity.
# Mirrors the old cascade order: the library is mostly Chinese, so on an equal
# score prefer the Chinese catalogues over LRCLIB's crowd-sourced entries.
SOURCE_BIAS = {"QQ": 0.003, "Kugou": 0.002, "NetEase": 0.001, "LRCLIB": 0.0}


def _norm(s: str) -> str:
    """Casefold + strip punctuation/spacing so "Qi Li Xiang" and "七里香(Live)"
    compare on their substance. NFKC folds full-width CJK punctuation first."""
    return _PUNCT_RE.sub("", unicodedata.normalize("NFKC", s or "").lower())


def _sim(a: str, b: str) -> float:
    """0..1 similarity of two already-normalized strings. Containment scores
    high because catalogues pad titles with suffixes ("七里香" vs "七里香live")."""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    if a in b or b in a:
        # Scale by how much of the longer string the shorter one covers, so a
        # 2-char title inside a 20-char one doesn't read as a near-perfect hit.
        ratio = max(ratio, 0.6 + 0.35 * (min(len(a), len(b)) / max(len(a), len(b))))
    return ratio


def _artist_sim(cand_artist: str, want_artist: str) -> float:
    """Best similarity over every pairing of the two multi-artist fields.
    A blank request scores neutral — we can't judge, so don't punish."""
    want_parts = [_norm(p) for p in _ARTIST_SPLIT_RE.split(want_artist or "")]
    cand_parts = [_norm(p) for p in _ARTIST_SPLIT_RE.split(cand_artist or "")]
    want_parts = [p for p in want_parts if p]
    cand_parts = [p for p in cand_parts if p]
    if not want_parts:
        return 0.5
    if not cand_parts:
        return 0.0
    # Compare the whole fields too: "A/B" vs "A/B" is an exact match that
    # per-part pairing alone would score no better than "A" vs "A/B".
    best = _sim(_norm(cand_artist), _norm(want_artist))
    for c in cand_parts:
        for wnt in want_parts:
            best = max(best, _sim(c, wnt))
    return best


def score_candidate(cand: dict, track: str, artist: str) -> float:
    """How well `cand` matches the requested (track, artist), roughly 0..1.

    Weighted title + artist similarity, minus a penalty for lyrics too short to
    be a real synced transcript, plus a hair of source bias to break ties
    deterministically."""
    score = (TITLE_WEIGHT * _sim(_norm(cand.get("title", "")), _norm(track))
             + ARTIST_WEIGHT * _artist_sim(cand.get("artist", ""), artist))
    if len(_TIMETAG_RE.findall(cand.get("lrc", ""))) < MIN_SYNCED_LINES:
        score -= STUB_PENALTY
    return score + SOURCE_BIAS.get(cand.get("source", ""), 0.0)


def _relevance(cand: dict, track: str, artist: str) -> float:
    """Title+artist similarity only (no stub penalty / source bias) — used to
    keep obviously-unrelated search hits out of the picker grid, so a short but
    correct lyric isn't judged by its length here."""
    return (TITLE_WEIGHT * _sim(_norm(cand.get("title", "")), _norm(track))
            + ARTIST_WEIGHT * _artist_sim(cand.get("artist", ""), artist))


def best_candidate(cands: list[dict], track: str, artist: str) -> dict | None:
    """The highest-scoring candidate, or None for an empty list."""
    if not cands:
        return None
    scored = [(score_candidate(c, track, artist), c) for c in cands]
    # max() keeps the first of equal scores, so a within-source tie resolves to
    # that source's own ranking (its result #1 beats its #2).
    best_score, best = max(scored, key=lambda sc: sc[0])
    for score, c in scored:
        print(f"[match] {score:.3f} {'*' if c is best else ' '} "
              f"{c['source']}: {c['artist']} — {c['title']}")
    print(f"[match] best = {best['source']} ({best_score:.3f})")
    return best


# QQ/Kugou/NetEase all work the same way: one search call returns N song IDs,
# then each ID needs its own request to pull the LRC. Those per-ID downloads are
# independent, so run them together — serially they'd make the whole sweep take
# ~(1 + limit) round-trips per source instead of ~2, and the song is already
# playing while we search. Same number of requests either way, just overlapped.
_LYRIC_WORKERS = 4


def _candidates(source: str, search_fn, lyric_fn,
                track: str, artist: str, limit: int) -> list[dict]:
    """Search `source`, download every hit's LRC concurrently, and return the
    ones that have synced lyrics as {"source","title","artist","lrc"} — in the
    source's own ranking order, which best_candidate() uses to break ties.

    A failed search yields []; a single failed/unsynced LRC just drops that one
    candidate."""
    try:
        entries = search_fn(track, artist, limit)
    except requests.RequestException as e:
        print(f"[{source.lower()}] candidates error: {e}")
        return []
    if not entries:
        return []

    def _lrc(key):
        try:
            return lyric_fn(key)
        except requests.RequestException:
            return None

    with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(len(entries), _LYRIC_WORKERS)) as ex:
        # map() preserves input order, so the source's ranking survives.
        lrcs = list(ex.map(_lrc, [key for key, _t, _a in entries]))
    return [{"source": source, "title": title, "artist": a, "lrc": lrc}
            for (_key, title, a), lrc in zip(entries, lrcs) if lrc][:limit]


def _qq_search_list(track: str, artist: str, limit: int,
                    timeout: float = 10) -> list[tuple[str, str, str]]:
    """Up to `limit` (songmid, title, artist) QQ matches for (track, artist).

    Uses the musicu.fcg SearchCgiService: the request is a JSON `data` blob and
    the matches come back at req_1.data.body.song.list, each carrying `mid`
    (the songmid the lyric endpoint needs), `name`/`title`, and `singer`."""
    query = f"{track} {artist}".strip()
    if not query:
        return []
    body = {
        "req_1": {
            "method": "DoSearchForQQMusicDesktop",
            "module": "music.search.SearchCgiService",
            "param": {"num_per_page": max(limit, 1), "page_num": 1,
                      "query": query, "search_type": 0},
        }
    }
    r = requests.get(
        QQ_SEARCH,
        params={"format": "json", "data": json.dumps(body)},
        headers=QQ_HEADERS, timeout=timeout)
    r.raise_for_status()
    songs = ((((r.json().get("req_1") or {}).get("data") or {})
              .get("body") or {}).get("song") or {}).get("list") or []
    out = []
    for s in songs[:limit]:
        mid = s.get("mid")
        if not mid:
            continue
        names = "/".join(x.get("name", "") for x in (s.get("singer") or [])
                         if x.get("name"))
        out.append((mid, s.get("name") or s.get("title") or track,
                    names or artist))
    return out


def qq_candidates(track: str, artist: str, limit: int) -> list[dict]:
    return _candidates("QQ", _qq_search_list, _qq_lyric, track, artist, limit)


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
    return _candidates("Kugou", _kugou_search_list, _kugou_lyric,
                       track, artist, limit)


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
    return _candidates("NetEase", _netease_search_list, _netease_lyric,
                       track, artist, limit)


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


def gather_candidates(track: str, artist: str, progress=None) -> dict[str, list[dict]]:
    """Query every source and return {source name: [candidate, ...]}, each item
    {"source", "title", "artist", "lrc"} in that source's own ranking order.

    The four sources are queried CONCURRENTLY (one thread each) so the total
    wait is ~the slowest single source, not the sum. A source that errors or
    exceeds the timeout simply contributes an empty list. `progress(name)`
    (optional) is called once before the sweep, for a live status line; it must
    not raise."""
    if progress is not None:
        try:
            progress("all sources")
        except Exception:
            pass
    by_name: dict[str, list[dict]] = {}
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=len(CANDIDATE_CAPS))
    try:
        futs = {name: ex.submit(
                    _CANDIDATE_FNS[name], track, artist,
                    CANDIDATE_QUERY_LIMITS.get(name, CANDIDATE_QUERY_LIMIT))
                for name, _cap in CANDIDATE_CAPS}
        for name, fut in futs.items():
            try:
                by_name[name] = fut.result(timeout=25)
            except Exception as e:
                print(f"[search] {name} failed: {e}")
                by_name[name] = []
            print(f"[search] {name}: {len(by_name[name])} candidate(s)")
    finally:
        # Don't block on a straggler source: let any still-running query finish
        # in the background (its request has its own 10s timeout) and be
        # discarded. The futures already collected are unaffected.
        ex.shutdown(wait=False)
    return by_name


def build_grid(by_name: dict[str, list[dict]], pin: dict | None = None,
               track: str = "", artist: str = "") -> list[dict]:
    """Flatten a gather_candidates() result into the ≤ GRID_MAX (8) list the
    picker renders.

    Selection is two-pass so the grid feels balanced but never wastes cells:
      1. take up to each source's cap (2) in source order — the "one row each"
         core;
      2. if that leaves fewer than 8, backfill round-robin from each source's
         leftovers (results beyond its cap) until 8 are filled or nothing's
         left. So a thin/empty source is covered by whichever sources have
         extras, instead of leaving a hole.

    `pin` (the auto-selected best match) is guaranteed a cell even when its
    source ranked it below the cap — the user must be able to see, and pick
    around, whatever we chose to display."""
    per_source = [by_name.get(name) or [] for name, _cap in CANDIDATE_CAPS]
    # Drop obviously-unrelated hits so the picker only offers plausible matches
    # (preserving each source's own order). Skip when there's nothing to compare
    # against (a blank query).
    if track or artist:
        per_source = [[c for c in items
                       if _relevance(c, track, artist) >= GRID_RELEVANCE_MIN]
                      for items in per_source]
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
    out = out[:GRID_MAX]
    if pin is not None and not any(c is pin for c in out):
        out = [pin] + out[:GRID_MAX - 1]
    return out


def search_candidates(track: str, artist: str, progress=None) -> list[dict]:
    """One-shot sweep → picker grid, for a manual (Modify Search) query where
    there's no automatic selection to make."""
    return build_grid(gather_candidates(track, artist, progress),
                      track=track, artist=artist)


# --- Combined lookup -------------------------------------------------------
def fetch_best_lyrics(track: str, artist: str, progress=None
                      ) -> tuple[str | None, str | None, list[dict] | None]:
    """Cache, else sweep every source and return the best-scoring match.

    Returns (lrc, source, grid):
      • cache hit  → (lrc, "cache", None) — no sweep ran, so there's nothing to
        hand the picker (a cached lyric the user already confirmed shows no
        feedback buttons, so the picker isn't reachable for it anyway).
      • sweep      → (best lrc, its source, candidates for the picker) with grid
        possibly [] when no source had the song. lrc/source are None then.

    Unlike the old first-hit-wins cascade, every candidate from every source is
    scored against (track, artist) and the highest wins — search engines rank by
    popularity, not by whether it's the song the phone is actually playing.
    Sources the user previously marked wrong for this song can't win the
    automatic pick, but they still appear in the picker so a manual override
    stays possible. Network results are NOT cached here — caching waits for the
    user's GREEN confirmation (see save_to_cache).

    `progress`, if given, is called with a status label before the sweep. It
    must not raise — a failing callback is swallowed so it can't break the
    fetch.
    """
    # 1. Local cache — fastest, no network. A cached entry was already
    #    confirmed by the user, so we trust it.
    cached = _cache_load(track, artist)
    if cached:
        print(f"[cache] hit: {artist} — {track}")
        return cached, "cache", None
    print(f"[cache] miss: {artist} — {track}")

    # 2. One concurrent sweep of every source. Its results serve BOTH the
    #    automatic pick below and the RED picker (no second search).
    by_name = gather_candidates(track, artist, progress)

    rejected = get_rejections(track, artist)
    if rejected:
        print(f"[lyrics] not auto-selecting rejected sources: {rejected}")
    pool = [c for name, items in by_name.items() if name not in rejected
            for c in items]

    best = best_candidate(pool, track, artist)
    grid = build_grid(by_name, pin=best, track=track, artist=artist)
    if best is None:
        # Intentionally NOT caching the negative result — leave the door open
        # to picking up lyrics if a source adds them later.
        print("[lyrics] no source had it")
        return None, None, grid
    print(f"[lyrics] {best['source']} wins (awaiting confirm — not cached yet)")
    return best["lrc"], best["source"], grid


if __name__ == "__main__":
    # Quick smoke test from the shell.
    import sys
    if len(sys.argv) >= 3:
        t, a = sys.argv[1], sys.argv[2]
    else:
        t, a = "世间美好与你环环相扣", "柏松"
    out, src, cands = fetch_best_lyrics(t, a)
    print(f"--- {len(cands or [])} candidate(s) cached for the picker")
    if out:
        print(f"--- (from {src})")
        print(out[:500])
    else:
        print("nothing found")
