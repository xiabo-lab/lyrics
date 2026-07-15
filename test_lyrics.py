"""Unit tests for the pure logic in carlyrics.

Run on the Pi (no network or display needed):
    cd ~/carlyrics && python3 -m unittest -v test_lyrics
"""
import unittest

from lrclib import LyricLine, Word, parse_lrc, shift_lrc_timestamps
from lyric_sources import (GRID_MAX, best_candidate, build_grid,
                           score_candidate, _krc_to_enhanced_lrc,
                           _qrc_to_enhanced_lrc)
from Lyrics_Display import decide_lock, find_current_index


def _cand(source, title, artist, lines=20):
    """A candidate dict with a plausibly-long synced LRC (no stub penalty)."""
    lrc = "".join(f"[00:{i:02d}.00]line {i}\n" for i in range(lines))
    return {"source": source, "title": title, "artist": artist, "lrc": lrc}


class ParseLrcTests(unittest.TestCase):
    def test_basic_and_sorting(self):
        lines = parse_lrc("[00:10.00]b\n[00:05.00]a\n")
        self.assertEqual([l.text for l in lines], ["a", "b"])
        self.assertEqual([l.time_ms for l in lines], [5000, 10000])

    def test_two_vs_three_digit_fractions(self):
        lines = parse_lrc("[00:01.5]x\n[00:02.250]y\n")
        self.assertEqual(lines[0].time_ms, 1500)   # .5  -> 500ms
        self.assertEqual(lines[1].time_ms, 2250)   # .250 -> 250ms

    def test_minutes(self):
        self.assertEqual(parse_lrc("[01:02.00]z")[0].time_ms, 62000)

    def test_repeated_timestamps_on_one_line(self):
        lines = parse_lrc("[00:01.00][00:03.00]chorus")
        self.assertEqual([l.time_ms for l in lines], [1000, 3000])
        self.assertTrue(all(l.text == "chorus" for l in lines))

    def test_metadata_and_blank_lines_ignored(self):
        lines = parse_lrc("[ar:Artist]\n[al:Album]\n\n[00:00.00]start")
        self.assertEqual([l.text for l in lines], ["start"])


class FindCurrentIndexTests(unittest.TestCase):
    def setUp(self):
        self.lines = [LyricLine(1000, "a"), LyricLine(2000, "b"),
                      LyricLine(3000, "c")]

    def test_before_first_is_minus_one(self):
        self.assertEqual(find_current_index(self.lines, 500), -1)

    def test_exact_boundary_is_inclusive(self):
        self.assertEqual(find_current_index(self.lines, 2000), 1)

    def test_between_lines(self):
        self.assertEqual(find_current_index(self.lines, 2500), 1)

    def test_after_last(self):
        self.assertEqual(find_current_index(self.lines, 99999), 2)

    def test_empty(self):
        self.assertEqual(find_current_index([], 1000), -1)


class DecideLockTests(unittest.TestCase):
    FRESH = 5000
    TIMEOUT = 5.0

    def test_zero_or_negative_never_locks(self):
        self.assertFalse(decide_lock(0, 1.0, self.FRESH, self.TIMEOUT)[0])
        self.assertFalse(decide_lock(-5, 9.0, self.FRESH, self.TIMEOUT)[0])

    def test_fresh_start_locks_immediately(self):
        should, why = decide_lock(1200, 0.1, self.FRESH, self.TIMEOUT)
        self.assertTrue(should)
        self.assertEqual(why, "fresh start")

    def test_stale_value_waits_before_timeout(self):
        should, why = decide_lock(120000, 2.0, self.FRESH, self.TIMEOUT)
        self.assertFalse(should)
        self.assertEqual(why, "waiting")

    def test_stale_value_locks_after_timeout(self):
        should, why = decide_lock(120000, 6.0, self.FRESH, self.TIMEOUT)
        self.assertTrue(should)
        self.assertEqual(why, "stabilize timeout")

    def test_no_track_change_timestamp_keeps_waiting_on_stale(self):
        # elapsed_s is None until a track change / attach is recorded.
        self.assertFalse(decide_lock(120000, None, self.FRESH, self.TIMEOUT)[0])


class ScoreCandidateTests(unittest.TestCase):
    def test_exact_match_beats_partial(self):
        exact = score_candidate(_cand("QQ", "七里香", "周杰伦"), "七里香", "周杰伦")
        live = score_candidate(_cand("QQ", "七里香 (Live)", "周杰伦"), "七里香", "周杰伦")
        self.assertGreater(exact, live)

    def test_right_song_wrong_artist_loses(self):
        right = score_candidate(_cand("QQ", "七里香", "周杰伦"), "七里香", "周杰伦")
        cover = score_candidate(_cand("QQ", "七里香", "某某某"), "七里香", "周杰伦")
        self.assertGreater(right, cover)

    def test_wrong_song_right_artist_loses(self):
        right = score_candidate(_cand("QQ", "七里香", "周杰伦"), "七里香", "周杰伦")
        other = score_candidate(_cand("QQ", "稻香", "周杰伦"), "七里香", "周杰伦")
        self.assertGreater(right, other)

    def test_punctuation_case_and_width_ignored(self):
        a = score_candidate(_cand("QQ", "Hello, World!", "Foo"), "hello world", "foo")
        self.assertAlmostEqual(a, 1.0 + 0.003, places=6)

    def test_multi_artist_field_matches_one_name(self):
        c = _cand("Kugou", "千里之外", "周杰伦/费玉清")
        self.assertGreater(score_candidate(c, "千里之外", "周杰伦"), 0.95)

    def test_stub_lyric_penalized_below_real_match(self):
        stub = _cand("QQ", "七里香", "周杰伦", lines=2)     # credits-only blob
        real = _cand("LRCLIB", "七里香", "周杰伦")
        self.assertGreater(score_candidate(real, "七里香", "周杰伦"),
                           score_candidate(stub, "七里香", "周杰伦"))

    def test_blank_requested_artist_is_neutral_not_zero(self):
        # AVRCP sometimes gives no artist; the title alone must still decide.
        good = score_candidate(_cand("QQ", "七里香", "周杰伦"), "七里香", "")
        bad = score_candidate(_cand("QQ", "稻香", "周杰伦"), "七里香", "")
        self.assertGreater(good, bad)
        self.assertGreater(good, 0.5)


class BestCandidateTests(unittest.TestCase):
    def test_none_on_empty(self):
        self.assertIsNone(best_candidate([], "t", "a"))

    def test_picks_best_across_sources_not_first(self):
        cands = [_cand("QQ", "七里香 (Live版)", "群星"),      # QQ's #1, wrong
                 _cand("Kugou", "七里香", "周杰伦")]          # the real one
        self.assertIs(best_candidate(cands, "七里香", "周杰伦"), cands[1])

    def test_source_bias_only_breaks_exact_ties(self):
        cands = [_cand("LRCLIB", "七里香", "周杰伦"),
                 _cand("QQ", "七里香", "周杰伦")]
        self.assertIs(best_candidate(cands, "七里香", "周杰伦"), cands[1])

    def test_equal_within_source_keeps_source_ranking(self):
        cands = [_cand("QQ", "七里香", "周杰伦"), _cand("QQ", "七里香", "周杰伦")]
        self.assertIs(best_candidate(cands, "七里香", "周杰伦"), cands[0])


class BuildGridTests(unittest.TestCase):
    def _by_name(self, counts):
        return {name: [_cand(name, f"{name}{i}", "a") for i in range(n)]
                for name, n in counts.items()}

    def test_core_respects_caps_in_source_order(self):
        # Core row takes each source's cap in source order (QQ is capped at 1 —
        # its search over-returns unrelated songs — so caps sum to 1+2+2+2 = 7);
        # the 8th cell is then backfilled round-robin, starting from QQ's extras.
        by_name = self._by_name({"QQ": 4, "Kugou": 4, "NetEase": 4, "LRCLIB": 4})
        grid = build_grid(by_name)
        self.assertEqual(len(grid), GRID_MAX)
        self.assertEqual([c["source"] for c in grid],
                         ["QQ", "Kugou", "Kugou", "NetEase", "NetEase",
                          "LRCLIB", "LRCLIB", "QQ"])

    def test_backfills_from_sources_with_extras(self):
        # Only QQ and LRCLIB answered — they must cover all 8 cells.
        by_name = self._by_name({"QQ": 4, "Kugou": 0, "NetEase": 0, "LRCLIB": 4})
        grid = build_grid(by_name)
        self.assertEqual(len(grid), GRID_MAX)
        self.assertEqual(sum(c["source"] == "QQ" for c in grid), 4)

    def test_never_exceeds_grid_max(self):
        by_name = self._by_name({"QQ": 9, "Kugou": 9, "NetEase": 9, "LRCLIB": 9})
        self.assertEqual(len(build_grid(by_name)), GRID_MAX)

    def test_short_when_nothing_found(self):
        self.assertEqual(build_grid({}), [])

    def test_pin_kept_when_already_in_grid(self):
        by_name = self._by_name({"QQ": 4, "Kugou": 4, "NetEase": 4, "LRCLIB": 4})
        pin = by_name["QQ"][0]
        grid = build_grid(by_name, pin=pin)
        self.assertEqual(len(grid), GRID_MAX)
        self.assertEqual(sum(c is pin for c in grid), 1)

    def test_pin_below_cap_is_forced_into_grid(self):
        # QQ's 4th result won on score but ranks past the 2-per-source cap.
        by_name = self._by_name({"QQ": 4, "Kugou": 4, "NetEase": 4, "LRCLIB": 4})
        pin = by_name["QQ"][3]
        grid = build_grid(by_name, pin=pin)
        self.assertEqual(len(grid), GRID_MAX)
        self.assertIs(grid[0], pin)


class WordLevelLyricTests(unittest.TestCase):
    def test_enhanced_lrc_words_parsed(self):
        line = parse_lrc("[00:06.59]<00:06.59>Hello <00:09.25>it's <00:09.40>me")[0]
        self.assertEqual(line.time_ms, 6590)
        self.assertEqual(line.text, "Hello it's me")   # word tags stripped
        self.assertEqual([(w.text, w.time_ms) for w in line.words],
                         [("Hello ", 6590), ("it's ", 9250), ("me", 9400)])

    def test_plain_lrc_has_no_words(self):
        self.assertEqual(parse_lrc("[00:01.00]plain line")[0].words, [])

    def test_words_not_attached_to_multi_timestamp_line(self):
        # Absolute word times can't be reused for a repeated timestamp.
        for line in parse_lrc("[00:01.00][00:03.00]<00:01.00>x<00:01.50>y"):
            self.assertEqual(line.words, [])

    def test_shift_moves_word_tags_too(self):
        out = shift_lrc_timestamps("[00:06.59]<00:06.59>Hi <00:09.25>yo", 500)
        self.assertEqual(out, "[00:07.09]<00:07.09>Hi <00:09.75>yo")

    def test_krc_to_enhanced_lrc(self):
        # KRC offsets are ms; enhanced LRC is centiseconds, so times truncate to
        # the nearest 10ms (invisible at 30 fps). 6591→6590, 6591+2660=9251→9250.
        krc = ("[ti:test]\n[6591,3110]<0,2660,0>Hello <2660,150,0>it's\n"
               "[0,2250]<0,160,0>晴<160,160,0>天")
        enhanced = _krc_to_enhanced_lrc(krc)
        lines = parse_lrc(enhanced)
        self.assertEqual(len(lines), 2)
        first = next(l for l in lines if l.time_ms == 6590)
        # absolute word start = line start + KRC offset (centisecond-truncated)
        self.assertEqual([(w.text, w.time_ms) for w in first.words],
                         [("Hello ", 6590), ("it's", 9250)])

    def test_krc_no_timed_lines_returns_none(self):
        self.assertIsNone(_krc_to_enhanced_lrc("[ti:x]\n[ar:y]\nplain"))

    def test_qrc_to_enhanced_lrc(self):
        # QRC word times are ABSOLUTE and the tag follows the word.
        qrc = "[ti:x]\n[6591,3110]Hello (6591,2660)it's (9251,150)me(9401,300)"
        line = parse_lrc(_qrc_to_enhanced_lrc(qrc))[0]
        self.assertEqual(line.text, "Hello it's me")
        self.assertEqual([(w.text, w.time_ms) for w in line.words],
                         [("Hello ", 6590), ("it's ", 9250), ("me", 9400)])

    def test_qrc_literal_paren_in_lyric(self):
        # A literal '(' in the lyric must not be mistaken for a timing tag.
        line = parse_lrc(_qrc_to_enhanced_lrc("[0,1000]((0,160)Jay(160,300)"))[0]
        self.assertEqual([(w.text, w.time_ms) for w in line.words],
                         [("(", 0), ("Jay", 160)])

    def test_word_concat_equals_text(self):
        # The karaoke fill measures word widths against the rendered `text`, so
        # "".join(words) MUST equal text — even when the source has leading or
        # trailing whitespace words (real QRC lines often end with a space).
        for enh in (
            "[00:00.00]<00:00.00> hi <00:00.10>yo ",   # lead + trail
            "[00:00.00]<00:00.00>Re <00:00.25>La ",    # trailing (QRC-style)
            "[00:00.00]<00:00.00> <00:00.10>hi<00:00.20> ",  # pure-space edges
            "[00:00.00]<00:00.00>晴<00:00.16>天",       # normal CJK
        ):
            line = parse_lrc(enh)[0]
            self.assertEqual("".join(w.text for w in line.words), line.text)


class QQCryptoTests(unittest.TestCase):
    def test_qrc_decrypt_known_vector(self):
        # Encrypt a known plaintext with the QRC key (buggy-DES, ENCRYPT), then
        # confirm qrc_decrypt round-trips it — validates the cipher end to end
        # without needing the network.
        import zlib
        from qqcrypto import (QRC_KEY, _triple_setup, _triple_crypt, _ENCRYPT,
                              qrc_decrypt)
        plain = b"[0,500]hi(0,250)yo(250,250)"
        comp = bytearray(zlib.compress(plain))
        comp += bytes((-len(comp)) % 8)                 # pad to 8-byte blocks
        enc_sched = _triple_setup(QRC_KEY, _ENCRYPT)
        blob = bytearray()
        for i in range(0, len(comp), 8):
            blob += _triple_crypt(comp[i:i + 8], enc_sched)
        self.assertEqual(qrc_decrypt(bytes(blob)), plain.decode())

    def test_qrc_decrypt_rejects_garbage(self):
        from qqcrypto import qrc_decrypt
        self.assertIsNone(qrc_decrypt("zznothex"))
        self.assertIsNone(qrc_decrypt(b"\x00" * 8))     # decrypts, not zlib


if __name__ == "__main__":
    unittest.main()
