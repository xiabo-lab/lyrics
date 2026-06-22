"""Unit tests for the pure logic in carlyrics.

Run on the Pi (no network or display needed):
    cd ~/carlyrics && python3 -m unittest -v test_lyrics
"""
import unittest

from lrclib import LyricLine, parse_lrc
from Lyrics_Display import decide_lock, find_current_index


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


if __name__ == "__main__":
    unittest.main()
