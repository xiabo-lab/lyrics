"""Build the offline pinyin→Hanzi table for the Modify Search IME (Phase 2).

Run this ONCE on a machine with internet (e.g. the Pi) to (re)generate
`pinyin_table.json`, then commit that file so every Pi gets it via OTA — the
runtime IME loads the JSON only, never the network.

    python3 build_pinyin_table.py

Sources (fetched fresh each run):
  - mozillazg/pinyin-data pinyin.txt   → each Hanzi's toneless pinyin readings
  - rime/rime-essay      essay.txt     → char + word frequencies (for ranking)

Output `pinyin_table.json`:
  { "chars": { "<syllable>": "<Hanzi ranked by freq>" },     # e.g. "zhou": "周洲州舟…"
    "words": { "<concatenated pinyin>": ["词", …] } }         # e.g. "zhoujielun": ["周杰伦"]

Word pinyin uses each char's PRIMARY (first-listed) reading — a heuristic that
is right for the common monophonic case and merely imperfect (not fatal) for
polyphonic chars, since the char table below is always the reliable fallback.
"""
import json
import sys
from pathlib import Path

import requests

PINYIN_URL = "https://raw.githubusercontent.com/mozillazg/pinyin-data/master/pinyin.txt"
ESSAY_URL = "https://raw.githubusercontent.com/rime/rime-essay/master/essay.txt"
# rime-essay is Traditional; convert output to Simplified (matches the mainland
# QQ/Kugou/NetEase lyric sources) with OpenCC's Traditional→Simplified char map.
TS_URL = "https://raw.githubusercontent.com/BYVoid/OpenCC/master/data/dictionary/TSCharacters.txt"
OUT = Path(__file__).resolve().parent / "pinyin_table.json"

CHARS_PER_SYLLABLE = 40      # cap candidates per single syllable
WORDS_PER_KEY = 12           # cap word suggestions per pinyin key
MAX_WORDS = 60000            # only the most frequent words become suggestions
WORD_MIN_LEN, WORD_MAX_LEN = 2, 4

# Tone-marked vowels → plain; ü → v (how IMEs accept it, e.g. lü typed "lv").
_TONE = str.maketrans({
    "ā": "a", "á": "a", "ǎ": "a", "à": "a",
    "ē": "e", "é": "e", "ě": "e", "è": "e",
    "ī": "i", "í": "i", "ǐ": "i", "ì": "i",
    "ō": "o", "ó": "o", "ǒ": "o", "ò": "o",
    "ū": "u", "ú": "u", "ǔ": "u", "ù": "u",
    "ǖ": "v", "ǘ": "v", "ǚ": "v", "ǜ": "v", "ü": "v",
})


def _toneless(reading: str) -> str | None:
    """A pinyin reading → a-z syllable key, or None if it has odd characters."""
    s = reading.strip().translate(_TONE)
    return s if s and s.isascii() and s.isalpha() else None


def main() -> None:
    print("fetching pinyin.txt…", flush=True)
    pinyin_txt = requests.get(PINYIN_URL, timeout=60).text
    print("fetching essay.txt…", flush=True)
    essay_txt = requests.get(ESSAY_URL, timeout=60).text
    print("fetching TSCharacters.txt…", flush=True)
    ts_txt = requests.get(TS_URL, timeout=60).text

    # Traditional char → Simplified (first listed variant). Chars absent from the
    # map (already simplified / script-neutral) pass through unchanged.
    t2s: dict[str, str] = {}
    for line in ts_txt.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0] and parts[1].strip():
            t2s[parts[0]] = parts[1].split()[0]

    def to_simp(text: str) -> str:
        return "".join(t2s.get(ch, ch) for ch in text)

    # char → [toneless readings] (primary first)
    readings: dict[str, list[str]] = {}
    for line in pinyin_txt.splitlines():
        if not line.startswith("U+") or ":" not in line:
            continue
        code, rest = line.split(":", 1)
        try:
            ch = chr(int(code[2:].strip(), 16))
        except ValueError:
            continue
        syls = []
        for r in rest.split("#", 1)[0].split(","):
            key = _toneless(r)
            if key and key not in syls:
                syls.append(key)
        if syls:
            readings[ch] = syls

    # word/char → frequency
    freq: dict[str, int] = {}
    for line in essay_txt.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            try:
                freq[parts[0]] = int(parts[1])
            except ValueError:
                pass

    # Char table: syllable → chars ranked by frequency.
    by_syl: dict[str, list[str]] = {}
    for ch, syls in readings.items():
        for s in syls:
            by_syl.setdefault(s, []).append(ch)
    chars = {}
    for s, chs in by_syl.items():
        chs.sort(key=lambda c: freq.get(c, 0), reverse=True)
        seen: list[str] = []                       # Simplified, deduped, in rank order
        for c in chs:
            sc = to_simp(c)
            if sc not in seen:
                seen.append(sc)
                if len(seen) >= CHARS_PER_SYLLABLE:
                    break
        chars[s] = "".join(seen)

    # Word table: concatenated primary-reading pinyin → frequent words.
    ranked_words = sorted(
        (w for w in freq if WORD_MIN_LEN <= len(w) <= WORD_MAX_LEN
         and all(c in readings for c in w)),
        key=lambda w: freq[w], reverse=True)[:MAX_WORDS]
    words: dict[str, list[str]] = {}
    for w in ranked_words:
        key = "".join(readings[c][0] for c in w)   # primary reading of each char
        sw = to_simp(w)                            # emit Simplified
        lst = words.setdefault(key, [])
        if sw not in lst and len(lst) < WORDS_PER_KEY:
            lst.append(sw)

    OUT.write_text(json.dumps({"chars": chars, "words": words},
                              ensure_ascii=False, separators=(",", ":")),
                   encoding="utf-8")
    print(f"wrote {OUT.name}: {len(chars)} syllables, {len(words)} word keys, "
          f"{OUT.stat().st_size // 1024} KB", flush=True)


if __name__ == "__main__":
    sys.exit(main())
