"""Unit tests for the pure logic in carlyrics.

Run on the Pi (no network or display needed):
    cd ~/carlyrics && python3 -m unittest -v test_lyrics
"""
import unittest

from lrclib import LyricLine, parse_lrc
from lyric_sources import GRID_MAX, best_candidate, build_grid, score_candidate
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

    def test_two_per_source_in_source_order(self):
        by_name = self._by_name({"QQ": 4, "Kugou": 4, "NetEase": 4, "LRCLIB": 4})
        grid = build_grid(by_name)
        self.assertEqual(len(grid), GRID_MAX)
        self.assertEqual([c["source"] for c in grid],
                         ["QQ", "QQ", "Kugou", "Kugou",
                          "NetEase", "NetEase", "LRCLIB", "LRCLIB"])

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


if __name__ == "__main__":
    unittest.main()
