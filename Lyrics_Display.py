"""Lyrics_Display: real lyric scroller driven by AVRCP.

Replaces scroller_test.py's fake time.monotonic() clock with the iPhone's
actual playback position via BlueZ MediaPlayer1.

Deploy:
    scp Lyrics_Display.py fuwenxu@carlyric.local:~/carlyrics/

Run (from a VS Code terminal on the Pi, NOT plain SSH — needs a graphical
seat via cage):
    sudo XDG_RUNTIME_DIR=/tmp cage -s -- \
        env PATH=$PATH PYTHONPATH=/home/fuwenxu/carlyrics \
        python3 /home/fuwenxu/carlyrics/Lyrics_Display.py

Prereqs on the Pi (already done in earlier phases):
    sudo apt install -y python3-dbus-next python3-pygame fonts-noto-cjk \
                        cage seatd python3-requests
    # iPhone paired + trusted (see session log 2026-06-03).

On-screen pairing (Settings → Bluetooth → Pair New Phone) needs a headless
pairing agent so BlueZ can accept a new phone without a keyboard/terminal:
    sudo apt install -y bluez-tools
    sudo cp bt-agent.service /etc/systemd/system/
    sudo systemctl enable --now bt-agent
The app only flips the adapter Discoverable/Pairable while the Pair screen is
open; bt-agent (NoInputNoOutput) auto-accepts the Just-Works pairing, then the
app trusts the device and connects its AVRCP profile.
"""
import asyncio
import io
import json
import os
import re
import shutil
import socket
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path

import pygame
import requests
from dbus_next import BusType, Variant
from dbus_next.aio import MessageBus

from lrclib import LyricLine, parse_lrc, shift_lrc_timestamps
from lyric_sources import (
    delete_from_cache,
    fetch_synced_lyrics_any as fetch_synced_lyrics,
    save_to_cache,
    search_candidates,
)

# Silence pygame ALSA warnings — we don't need audio out.
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

# ---- BlueZ ----------------------------------------------------------------
BLUEZ = "org.bluez"
MP_IFACE = "org.bluez.MediaPlayer1"
DEVICE_IFACE = "org.bluez.Device1"
ADAPTER_IFACE = "org.bluez.Adapter1"
PROPS_IFACE = "org.freedesktop.DBus.Properties"
OM_IFACE = "org.freedesktop.DBus.ObjectManager"

# Shown on the Software Version screen (Settings → Software Version). Bump on
# release so the car display can be matched to a known build at a glance.
APP_VERSION = "1.6.0"

# ---- Firmware update (Settings → Software Version → Update Firmware) --------
# "Update Firmware" downloads the latest code straight from GitHub so a user
# can update without a computer/SSH. We pull the branch tarball (no git needed
# on the Pi) and overwrite only the files listed below — config.json, cache/
# and rejections.json are deliberately NOT touched, so the user's tuning and
# confirmed lyrics survive the update. Forks: point UPDATE_URL at your own repo.
INSTALL_DIR = Path(__file__).resolve().parent
UPDATE_URL = "https://github.com/xiabo-lab/lyrics/archive/refs/heads/main.tar.gz"
UPDATE_FILES = (
    "Lyrics_Display.py", "lyric_sources.py", "lrclib.py", "test_lyrics.py",
    "bt-agent.service", "99-carlyric-ignore-avrcp-pointer.rules",
    "wifi.sh", "carlyric-claude.sudoers", "README.md", "LICENSE", ".gitignore",
)
UPDATE_SERVICE = "carlyric.service"   # restarted to load the new code
# AVRCP "A/V Remote Control" profile. We connect THIS explicitly (not a
# plain Device1.Connect()) because a bare Connect() on a dual-mode iPhone
# often brings up only Bluetooth LE — which exposes no MediaPlayer1, so the
# app never sees a track. Connecting the AVRCP profile forces the classic
# BR/EDR control channel that BlueZ surfaces as MediaPlayer1.
AVRCP_UUID = "0000110e-0000-1000-8000-00805f9b34fb"

# ---- Display --------------------------------------------------------------
BG = (0, 0, 0)
# Driving legibility: a strong size + brightness gap between the "now" line
# and its neighbours lets your eye lock onto the current line at a glance.
# Per-line colours. These are DEFAULTS; the live values are set from the
# named-colour palette (SETTING_COLORS) via current_color / top_color /
# bottom_color in config.json, editable in the on-screen settings panel
# (long-press 10s). CURRENT = the now line, PREV = the TOP context line,
# NEXT = the BOTTOM context line.
CURRENT = (255, 220, 80)     # now line    — Yellow
PREV = (235, 235, 235)       # top line    — White
NEXT = (235, 235, 235)       # bottom line — White
PROGRESS = (255, 200, 70)    # progress-bar fill
PROGRESS_TRACK = (55, 55, 55)  # progress-bar background
ROTATION_DEG = 0   # 0 for test monitor; 90 once the bar LCD is mounted.
FLIP_180 = True    # monitor mounted upside-down: turn the whole frame 180°.
FONT_PATH = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
FONT_CURRENT = 56   # current/now line (and intro line)
FONT_TOP = 34       # top context line (previous lyric)
FONT_BOTTOM = 34    # bottom context line (next lyric)
FONT_SYNC = 26      # the transient "sync ±x.xx s" nudge toast (smaller still)
LINE_GAP_PAD = 70   # extra px between stacked lines, on top of font half-heights
TARGET_FPS = 30
INTRO_DOTS_MAX = 3   # pre-song countdown: max dots shown (1 dot = 1 second)
CURRENT_BOLD = True       # render the current line bold for glance legibility
TOP_BOLD = False          # render the top (previous) context line bold
BOTTOM_BOLD = False       # render the bottom (next) context line bold
SHOW_PREV_LINE = True     # False = less-cluttered current+next-only view
PROGRESS_BAR = True       # thin bar under the current line tracking progress
DIM_ENABLED = True        # auto-dim the whole display at night
DAY_START_HOUR = 7        # local hour daytime (full) brightness begins
NIGHT_START_HOUR = 19     # local hour night (dimmed) brightness begins
NIGHT_BRIGHTNESS = 0.55   # 0..1 multiplier applied to all colours at night
MAX_LINE_WIDTH_FRAC = 0.9  # shrink any line wider than this frac of the screen
AUTOCONNECT = True   # on boot/disconnect, have the Pi reconnect to the paired phone

# Bluetooth A2DP buffers ~200–500ms of audio, so iPhone's AVRCP Position
# is "ahead" of what you actually hear from the car stereo. Subtract this
# from our extrapolated clock so lyric lines hit at the right moment.
# Tune by ear: too low = lyrics late; too high = lyrics early.
LATENCY_OFFSET_MS = 300

# Show each lyric line this far AHEAD of the audio, so the line you're
# about to sing is already on screen when the music reaches it. Positive =
# lyrics lead the music. Requested: 0.5 + 0.2 + 0.3 + 0.5 = 1.5s early.
LEAD_OFFSET_MS = 1500

# Two-finger horizontal swipe = a live, CURRENT-SONG-ONLY sync nudge, for the
# rare YT Music master whose timing doesn't match the LRC. Swipe left→right to
# DELAY lyrics, right→left to ADVANCE them, SWIPE_STEP_MS per swipe. The nudge
# lives only on the playing song (State.song_offset_ms) and is wiped on the
# next track change — most songs are perfect and must not inherit it.
SWIPE_STEP_MS = 1000
# Each finger must travel at least this fraction of the screen width for a
# two-finger drag to count as a swipe (filters out taps and tiny jitter).
SWIPE_MIN_FRAC = 0.15

# Screen brightness: a software dimmer multiplied on top of the night auto-dim.
# DOUBLE-TAP the screen toggles a slider on the right edge; while it's shown, a
# one-finger swipe up brightens and swipe down darkens. Held in memory only
# (resets to full on restart). BRIGHTNESS_MIN keeps the panel readable enough to
# find the slider and turn it back up.
BRIGHTNESS_MIN = 0.15
BRIGHTNESS_GAIN = 1.2        # vertical-swipe sensitivity (≈ fraction per screen height)
DOUBLE_TAP_S = 0.4           # max gap between two taps to count as a double-tap
TAP_MAX_MOVE_FRAC = 0.06     # a "tap" stays within this frac of screen width…
TAP_MAX_DUR_S = 0.4          # …and lifts within this long (else it's a swipe/hold)

# Road-safety notice shown full-screen for this many seconds at every startup,
# before lyrics begin. This is a glance-only display: the driver's attention
# belongs on the road, and any real interaction (menus, pairing, picking lyrics)
# should happen parked or on a home desktop. Set to 0 to skip the notice.
SAFETY_NOTICE_S = 6.0

# ---- On-screen settings (long-press 10s) ----------------------------------
# Hold one finger still on the screen for this long to open the settings
# panel: a font-size slider + a 6-colour palette for each of the three lines
# (current / top / bottom). Changes preview live and are written to
# config.json on "Done", so they persist and ride the same hot-reload path as
# a hand edit.
LONGPRESS_OPEN_S = 10.0
SETTINGS_FONT_MIN = 20    # smallest font the size slider can reach (left end)
SETTINGS_FONT_MAX = 160   # largest font the size slider can reach (right end)
# Selectable colour palette, in slider/swatch order. Stored in config.json as
# the lowercased name; rendered from the RGB here.
SETTING_COLORS = [
    ("Yellow", (255, 220, 80)),
    ("Green",  (80, 220, 120)),
    ("White",  (235, 235, 235)),
    ("Red",    (255, 90, 90)),
    ("Blue",   (110, 180, 255)),
    ("Purple", (200, 130, 255)),
]
COLOR_BY_NAME = {name.lower(): rgb for name, rgb in SETTING_COLORS}


def color_name_of(rgb) -> str:
    """Palette name for an RGB (exact match expected; falls back to the first
    entry so a swatch highlight never goes blank)."""
    for name, c in SETTING_COLORS:
        if tuple(c) == tuple(rgb):
            return name.lower()
    return SETTING_COLORS[0][0].lower()


# ---- Live config (config.json) --------------------------------------------
# The values above are DEFAULTS. If a config.json sits next to this script,
# the keys it lists override them — and the render loop reloads the file
# whenever it changes, so you can tune offset / fonts / dots live WITHOUT a
# restart. A missing key, a wrong type, or a broken file silently falls back
# to the default, so a bad edit can never blank the screen.
CONFIG_PATH = Path(__file__).resolve().parent / "config.json"
_CONFIG_DEFAULTS = {
    "latency_offset_ms": LATENCY_OFFSET_MS,
    "lead_offset_ms": LEAD_OFFSET_MS,
    "font_current": FONT_CURRENT,
    "font_top": FONT_TOP,
    "font_bottom": FONT_BOTTOM,
    "font_sync": FONT_SYNC,
    "current_color": color_name_of(CURRENT),
    "top_color": color_name_of(PREV),
    "bottom_color": color_name_of(NEXT),
    "line_gap_pad": LINE_GAP_PAD,
    "intro_dots_max": INTRO_DOTS_MAX,
    "target_fps": TARGET_FPS,
    "current_bold": CURRENT_BOLD,
    "top_bold": TOP_BOLD,
    "bottom_bold": BOTTOM_BOLD,
    "show_prev_line": SHOW_PREV_LINE,
    "progress_bar": PROGRESS_BAR,
    "dim_enabled": DIM_ENABLED,
    "day_start_hour": DAY_START_HOUR,
    "night_start_hour": NIGHT_START_HOUR,
    "night_brightness": NIGHT_BRIGHTNESS,
    "max_line_width_frac": MAX_LINE_WIDTH_FRAC,
    "autoconnect": AUTOCONNECT,
    "flip_180": FLIP_180,
}


def load_config() -> dict:
    """config.json merged over the defaults. Missing/broken file → defaults.

    Each value is validated against its default's type (bool/float/int) and
    clamped to a sane range, so a typo can't crash the display.
    """
    cfg = dict(_CONFIG_DEFAULTS)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            user = json.load(f)
    except FileNotFoundError:
        return cfg
    except (OSError, json.JSONDecodeError) as e:
        print(f"[config] {CONFIG_PATH.name} unreadable ({e}); using defaults")
        return cfg
    for key, val in user.items():
        if key not in cfg:
            print(f"[config] ignoring unknown key: {key}")
            continue
        default = cfg[key]
        if isinstance(default, bool):
            if isinstance(val, bool):
                cfg[key] = val
            else:
                print(f"[config] ignoring non-bool {key}={val!r}")
        elif isinstance(default, str):
            if isinstance(val, str) and val.lower() in COLOR_BY_NAME:
                cfg[key] = val.lower()
            else:
                print(f"[config] ignoring invalid colour {key}={val!r}")
        elif isinstance(default, float):
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                print(f"[config] ignoring non-numeric {key}={val!r}")
            else:
                cfg[key] = float(val)
        else:  # int
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                print(f"[config] ignoring non-numeric {key}={val!r}")
            else:
                cfg[key] = int(val)
    cfg["font_current"] = max(8, cfg["font_current"])
    cfg["font_top"] = max(8, cfg["font_top"])
    cfg["font_bottom"] = max(8, cfg["font_bottom"])
    cfg["font_sync"] = max(8, cfg["font_sync"])
    cfg["line_gap_pad"] = min(300, max(0, cfg["line_gap_pad"]))
    cfg["target_fps"] = min(120, max(1, cfg["target_fps"]))
    cfg["intro_dots_max"] = min(20, max(0, cfg["intro_dots_max"]))
    cfg["day_start_hour"] = min(23, max(0, cfg["day_start_hour"]))
    cfg["night_start_hour"] = min(23, max(0, cfg["night_start_hour"]))
    cfg["night_brightness"] = min(1.0, max(0.05, cfg["night_brightness"]))
    cfg["max_line_width_frac"] = min(1.0, max(0.3, cfg["max_line_width_frac"]))
    return cfg


def apply_config() -> None:
    """Push config.json into the live tunable globals."""
    global LATENCY_OFFSET_MS, LEAD_OFFSET_MS, FONT_CURRENT, FONT_TOP, FONT_BOTTOM
    global FONT_SYNC, CURRENT, PREV, NEXT
    global LINE_GAP_PAD, INTRO_DOTS_MAX, TARGET_FPS, CURRENT_BOLD, SHOW_PREV_LINE
    global TOP_BOLD, BOTTOM_BOLD
    global PROGRESS_BAR, DIM_ENABLED, DAY_START_HOUR, NIGHT_START_HOUR
    global NIGHT_BRIGHTNESS, MAX_LINE_WIDTH_FRAC, AUTOCONNECT, FLIP_180
    cfg = load_config()
    LATENCY_OFFSET_MS = cfg["latency_offset_ms"]
    LEAD_OFFSET_MS = cfg["lead_offset_ms"]
    FONT_CURRENT = cfg["font_current"]
    FONT_TOP = cfg["font_top"]
    FONT_BOTTOM = cfg["font_bottom"]
    FONT_SYNC = cfg["font_sync"]
    CURRENT = COLOR_BY_NAME[cfg["current_color"]]
    PREV = COLOR_BY_NAME[cfg["top_color"]]
    NEXT = COLOR_BY_NAME[cfg["bottom_color"]]
    LINE_GAP_PAD = cfg["line_gap_pad"]
    INTRO_DOTS_MAX = cfg["intro_dots_max"]
    TARGET_FPS = cfg["target_fps"]
    CURRENT_BOLD = cfg["current_bold"]
    TOP_BOLD = cfg["top_bold"]
    BOTTOM_BOLD = cfg["bottom_bold"]
    SHOW_PREV_LINE = cfg["show_prev_line"]
    PROGRESS_BAR = cfg["progress_bar"]
    DIM_ENABLED = cfg["dim_enabled"]
    DAY_START_HOUR = cfg["day_start_hour"]
    NIGHT_START_HOUR = cfg["night_start_hour"]
    NIGHT_BRIGHTNESS = cfg["night_brightness"]
    MAX_LINE_WIDTH_FRAC = cfg["max_line_width_frac"]
    AUTOCONNECT = cfg["autoconnect"]
    FLIP_180 = cfg["flip_180"]


def write_config_values(updates: dict) -> None:
    """Merge `updates` into config.json and write it back atomically.

    Used by the on-screen settings panel's "Done" to persist edits. Reads the
    existing file so hand-set keys (offset, fps, …) are preserved, updates only
    the keys we changed, and renames a temp file over the original so a crash
    mid-write can never leave a truncated config that blanks the display."""
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except (OSError, json.JSONDecodeError):
        data = {}
    data.update(updates)
    tmp = CONFIG_PATH.with_name(CONFIG_PATH.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, CONFIG_PATH)


def _unwrap(v):
    """Recursively pull plain Python values out of dbus-next Variants."""
    if isinstance(v, Variant):
        return _unwrap(v.value)
    if isinstance(v, dict):
        return {k: _unwrap(val) for k, val in v.items()}
    if isinstance(v, list):
        return [_unwrap(item) for item in v]
    return v


class State:
    """Shared between AVRCP listener and render loop.

    Single asyncio event loop → no lock needed, just don't await between
    a read and a related write.
    """

    def __init__(self):
        self.title: str = ""
        self.artist: str = ""
        self.status: str = "stopped"        # "playing" | "paused" | "stopped"
        self.position_ms: int = 0
        self.position_at_mono: float = time.monotonic()
        # Don't render lyrics until iPhone confirms an actual Position for
        # the current track — otherwise lyrics tick from 0 while audio is
        # already mid-song, causing visible "catch-up" jumps.
        self.position_known: bool = False
        self.lines: list[LyricLine] = []
        self.lyrics_status: str = "(waiting for music)"
        # Touch-feedback state. When lyrics come fresh off a network source we
        # show GREEN/RED edge buttons and wait for the user to confirm before
        # caching. lrc_raw is kept so GREEN can save the exact text; source is
        # the name RED records as wrong. awaiting_feedback gates the buttons.
        self.lrc_raw: str = ""
        self.lyrics_source: str | None = None
        self.awaiting_feedback: bool = False
        # Live, current-song-only sync nudge from two-finger swipes. Reset to
        # 0 on every track change (see _handle) so a fix for one odd master
        # never leaks onto the next (normal) song.
        self.song_offset_ms: int = 0

    def now_ms(self) -> int:
        """Extrapolate the current playback position, minus A2DP latency.

        AVRCP Position updates aren't perfectly continuous, so between
        updates we keep ticking locally as long as status == "playing".
        """
        if self.status == "playing":
            extrap = self.position_ms + int(
                (time.monotonic() - self.position_at_mono) * 1000
            )
        else:
            extrap = self.position_ms
        return max(0, extrap - LATENCY_OFFSET_MS + LEAD_OFFSET_MS
                   + self.song_offset_ms)

    def set_position(self, ms: int) -> None:
        self.position_ms = ms
        self.position_at_mono = time.monotonic()
        self.position_known = True


# Friendly names for the on-screen "Searching …" status, keyed by the source
# names lyric_sources reports. Unknown names fall back to themselves.
SOURCE_LABELS = {"QQ": "QQ Music", "Kugou": "Kugou", "NetEase": "NetEase",
                 "LRCLIB": "LRCLIB"}


async def fetch_lyrics_for(state: State, title: str, artist: str) -> None:
    """requests is blocking → run in a thread so dbus + render keep flowing."""
    print(f"[lyrics] fetching: {artist} — {title}")
    loop = asyncio.get_running_loop()

    def on_source(name: str) -> None:
        # Called from the fetch worker thread as the cascade tries each source.
        # Hop back to the event loop to update the display text, honouring
        # State's single-thread rule (no cross-thread attribute writes).
        label = SOURCE_LABELS.get(name, name)
        loop.call_soon_threadsafe(
            setattr, state, "lyrics_status", f"♪ Searching {label}…")

    try:
        lrc, source = await asyncio.to_thread(
            fetch_synced_lyrics, title, artist, on_source)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        print(f"[lyrics] error: {e}")
        state.lines = []
        state.lyrics_status = "(network error)"
        state.awaiting_feedback = False
        return

    if not lrc:
        state.lines = []
        state.lyrics_status = "♪ Lyrics not found"
        state.lrc_raw = ""
        state.lyrics_source = None
        state.awaiting_feedback = False
        print("[lyrics] none found")
        return

    state.lines = parse_lrc(lrc)
    state.lrc_raw = lrc
    state.lyrics_source = source
    state.lyrics_status = ""
    # Buttons show ONLY for fresh network lyrics awaiting a verdict. A cache
    # hit was already confirmed with GREEN on an earlier play, so it shows no
    # buttons.
    state.awaiting_feedback = source != "cache"
    print(f"[lyrics] {len(state.lines)} lines loaded (source={source})")


class AvrcpWatcher:
    # Poll Position every this-many seconds. We're NOT polling to force a
    # refresh — BlueZ returns whatever the iPhone last broadcast (cached).
    # We poll so that whenever the iPhone DOES broadcast (on play/pause/
    # skip, or — for cooperative apps like Apple Music — continuously
    # during playback and on scrub), we catch the new value within
    # POLL_INTERVAL_S even if the PropertiesChanged signal got missed.
    POLL_INTERVAL_S = 0.5
    # Only print a [seek] line in logs when the delta exceeds this; smaller
    # adjustments still snap silently to avoid log spam during the
    # once-per-second Apple Music broadcasts.
    SEEK_LOG_THRESHOLD_MS = 500
    # Until we've "locked" onto a new track, only trust a position that
    # looks like a genuine fresh start (within this many ms of the top of
    # the song). On a YouTube Music skip, the first Position BlueZ reports
    # is often the STALE tail of the previous track — latching that is what
    # makes a new song start out of sync. A real new-track start sits near
    # 0, so we wait for that before anchoring + rendering.
    FRESH_START_MAX_MS = 5000
    # ...but never wait forever. If no fresh-start value shows up within
    # this window (e.g. resuming mid-track, or a player that just never
    # re-broadcasts), anchor to whatever we have so lyrics don't hang.
    STABILIZE_TIMEOUT_S = 5.0
    # How often to (re)attempt connecting any paired-but-disconnected phone.
    AUTOCONNECT_INTERVAL_S = 15.0

    def __init__(self, bus: MessageBus, state: State):
        self.bus = bus
        self.state = state
        self.player_path: str | None = None
        self.props_iface = None      # cached on _attach for fast polling
        self.last_sig: tuple = (None, None)
        self.fetch_task: asyncio.Task | None = None
        self.poll_task: asyncio.Task | None = None
        self.autoconnect_task: asyncio.Task | None = None
        self._om = None              # ObjectManager, cached for auto-connect
        # Last raw Position value we read from BlueZ. We only snap when
        # this changes between polls — otherwise BlueZ is returning a
        # stale cache (iPhone hasn't broadcast since) and snapping would
        # pull us BACKWARD in time, which is worse than doing nothing.
        self._last_polled_ms: int | None = None
        # monotonic() at the last track change — gates the fresh-start
        # stabilize timeout (see _try_lock).
        self._track_changed_at: float | None = None

    async def start(self) -> None:
        intro = await self.bus.introspect(BLUEZ, "/")
        root = self.bus.get_proxy_object(BLUEZ, "/", intro)
        om = root.get_interface(OM_IFACE)
        self._om = om

        objects = await om.call_get_managed_objects()
        for path, ifaces in objects.items():
            if MP_IFACE in ifaces:
                await self._attach(path, ifaces[MP_IFACE])

        om.on_interfaces_added(self._on_added)
        om.on_interfaces_removed(self._on_removed)
        self.poll_task = asyncio.create_task(self._position_poller())
        self.autoconnect_task = asyncio.create_task(self._auto_connect_loop())
        print("[avrcp] watching for MediaPlayer1…")

    async def _auto_connect_loop(self) -> None:
        """Bring up AVRCP to the iPhone on our own, so the user never has to
        open iPhone Bluetooth settings.

        iOS won't reliably reach out to a freshly-booted accessory, so WE
        initiate. We act only while no AVRCP player is attached
        (self.player_path is None) and we keep retrying every sweep, so this
        also recovers the link if it drops mid-drive. We connect the AVRCP
        profile specifically (see AVRCP_UUID) rather than a plain Connect(),
        which on a dual-mode iPhone can bring up only BLE and leave us with
        no MediaPlayer1. Errors are expected and harmless (phone locked/idle
        → "profile-unavailable", out of range, already connecting) — we just
        try again on the next sweep, succeeding as soon as the phone is awake.
        """
        await asyncio.sleep(3)   # let BlueZ + adapter settle after boot
        while True:
            if AUTOCONNECT and self._om is not None and self.player_path is None:
                try:
                    objects = await self._om.call_get_managed_objects()
                except Exception:
                    objects = {}
                paired = []
                any_connected = False
                for path, ifaces in objects.items():
                    dev = ifaces.get(DEVICE_IFACE)
                    if not dev:
                        continue
                    props = _unwrap(dev)
                    if not props.get("Paired"):
                        continue
                    if props.get("Connected"):
                        any_connected = True
                    paired.append((path, props))
                # We only get here while player_path is None — no AVRCP source
                # is live. Pick who to reach out to:
                #   - Nothing connected → try any paired phone to bring one up.
                #   - A phone IS connected but no MediaPlayer1 has surfaced →
                #     force AVRCP up on THAT phone. iOS on a cold boot sometimes
                #     brings up A2DP audio without ever opening the AVCTP control
                #     channel, so the link shows "connected" on the phone while
                #     the Pi hangs at "waiting for music" forever (until a power
                #     cycle). connect_profile(AVRCP_UUID) forces the control
                #     channel and MediaPlayer1 appears. Only target already-
                #     connected phones here so we don't drag a SECOND source in
                #     beside the live one — still one source at a time.
                if any_connected:
                    targets = [(p, pr) for (p, pr) in paired if pr.get("Connected")]
                else:
                    targets = paired
                for path, props in targets:
                    name = props.get("Alias") or props.get("Name") or path
                    try:
                        dintro = await self.bus.introspect(BLUEZ, path)
                        dobj = self.bus.get_proxy_object(BLUEZ, path, dintro)
                        print(f"[autoconnect] linking AVRCP to {name}…")
                        await dobj.get_interface(
                            DEVICE_IFACE).call_connect_profile(AVRCP_UUID)
                        print(f"[autoconnect] AVRCP linked: {name}")
                        break   # one source is enough
                    except Exception as e:
                        print(f"[autoconnect] {name}: {e}")
            await asyncio.sleep(self.AUTOCONNECT_INTERVAL_S)

    async def _position_poller(self) -> None:
        """Background loop: catch every iPhone-broadcast Position update.

        Policy:
        - If BlueZ's polled Position equals what we read last tick, the
          iPhone hasn't broadcast since — do nothing, trust extrapolation.
        - If it changed, the iPhone broadcast a fresh value. Always snap
          to it (re-anchor our clock to truth, killing accumulated drift).

        Net effect:
        - Apple Music broadcasts ~every 1s → we re-anchor every second,
          zero drift, scrubs caught within 0.5s.
        - YouTube Music broadcasts only on play/pause/skip → we re-anchor
          at those events; between them we rely on extrapolation. Mid-song
          scrubs are NOT detectable (iOS YT Music doesn't broadcast them).
        """
        while True:
            await asyncio.sleep(self.POLL_INTERVAL_S)
            if not self.props_iface or self.state.status != "playing":
                continue
            try:
                pos_var = await self.props_iface.call_get(MP_IFACE, "Position")
            except Exception:
                # Player may have just gone away mid-call; loop will idle.
                continue
            try:
                real_ms = int(pos_var.value)
            except AttributeError:
                real_ms = int(pos_var)

            # Not locked yet (song just started): only anchor once we see a
            # position that looks like a genuine fresh start, so we don't
            # latch the stale previous-track value YT Music leaves behind on
            # a skip. _try_lock handles the freshness test + timeout.
            if not self.state.position_known:
                self._try_lock(real_ms)
                continue

            # Locked. Only snap when the value actually CHANGES — an
            # unchanged read means BlueZ is serving a stale cache (iPhone
            # hasn't broadcast since), and re-anchoring to it every poll
            # would pin our clock and stop extrapolation.
            if self._last_polled_ms is None:
                self._last_polled_ms = real_ms
                continue
            if real_ms == self._last_polled_ms:
                continue
            self._last_polled_ms = real_ms

            # iPhone broadcast fresh value → trust it.
            expected = self.state.position_ms + int(
                (time.monotonic() - self.state.position_at_mono) * 1000
            )
            delta = real_ms - expected
            self.state.set_position(real_ms)
            if abs(delta) > self.SEEK_LOG_THRESHOLD_MS:
                print(f"[seek]     snap: expected {expected}ms, real {real_ms}ms (Δ {delta:+d}ms)")

    def _try_lock(self, real_ms: int) -> None:
        """Anchor onto a new track, but only to a trustworthy position.

        Called while position_known is False (right after a track change).
        YouTube Music doesn't stream position continuously, and the first
        value BlueZ reports after a skip is often the STALE tail of the
        previous track. Anchoring to that is exactly what makes a new song
        start out of sync (and why a manual pause/play — which forces YT
        Music to re-broadcast the true position — fixes it).

        Policy: trust a value only if it's near the top of the track (a
        real fresh start) OR the stabilize window has elapsed (fallback so
        lyrics never hang, e.g. when resuming mid-track).
        """
        self._last_polled_ms = real_ms
        elapsed = (
            None if self._track_changed_at is None
            else time.monotonic() - self._track_changed_at
        )
        should, why = decide_lock(
            real_ms, elapsed, self.FRESH_START_MAX_MS, self.STABILIZE_TIMEOUT_S
        )
        if should:
            self.state.set_position(real_ms)
            print(f"[sync]     locked at {real_ms}ms ({why})")

    def start_fetch(self, title: str, artist: str) -> None:
        """Cancel any in-flight fetch and start a new one. Used both on a
        track change and when the RED button asks to re-search the next
        source — routing both through here means a track change always
        supersedes a pending re-search instead of racing it onto the new song."""
        if self.fetch_task and not self.fetch_task.done():
            self.fetch_task.cancel()
        self.fetch_task = asyncio.create_task(
            fetch_lyrics_for(self.state, title, artist)
        )

    def _on_added(self, path: str, ifaces: dict):
        if MP_IFACE in ifaces:
            asyncio.create_task(self._attach(path, ifaces[MP_IFACE]))

    def _on_removed(self, path: str, ifaces: list):
        if MP_IFACE in ifaces and path == self.player_path:
            print(f"[avrcp] player gone: {path}")
            self.player_path = None
            self.props_iface = None
            self.state.status = "stopped"
            self.last_sig = (None, None)

    async def _attach(self, path: str, initial_props: dict) -> None:
        print(f"[avrcp] player appeared: {path}")
        self.player_path = path
        intro = await self.bus.introspect(BLUEZ, path)
        obj = self.bus.get_proxy_object(BLUEZ, path, intro)
        props = obj.get_interface(PROPS_IFACE)
        # Cache for the poller to read Position without re-introspecting.
        self.props_iface = props

        # This phone is now the live source — drop any other phone still
        # connected so the old one doesn't keep its Bluetooth link. The player
        # path is "<device>/playerN", so its parent is the Device1 to keep.
        await disconnect_other_devices(self.bus, self._om, path.rsplit("/", 1)[0])

        # Start the stabilize clock now, so that if we connect mid-song
        # (no track-change event to trigger it), _try_lock's timeout
        # fallback still fires and lyrics don't hang waiting for a
        # fresh-start value that will never come.
        if self._track_changed_at is None:
            self._track_changed_at = time.monotonic()

        self._handle(_unwrap(initial_props))

        def on_changed(iface: str, changed: dict, invalidated: list):
            if iface != MP_IFACE:
                return
            self._handle(_unwrap(changed))

        props.on_properties_changed(on_changed)

    def _handle(self, changed: dict) -> None:
        if "Status" in changed:
            self.state.status = changed["Status"]
            print(f"[status]   {self.state.status}")
        # IMPORTANT: process Track BEFORE Position. The iPhone often sends
        # both in the same PropertiesChanged batch on a track change. If we
        # consumed Position first, the Track block below would immediately
        # wipe that fresh anchor back to 0 (causing lyrics to start at line
        # 0 and "catch up"). Handling Track first means a same-batch
        # Position correctly anchors the NEW track.
        track_changed = False
        if "Track" in changed:
            track = changed["Track"] or {}
            title = (track.get("Title") or "").strip()
            artist = (track.get("Artist") or "").strip()
            sig = (title, artist)
            if sig != self.last_sig and title and artist:
                self.last_sig = sig
                self.state.title = title
                self.state.artist = artist
                self.state.lines = []
                self.state.lyrics_status = "(fetching…)"
                self.state.awaiting_feedback = False
                self.state.lyrics_source = None
                self.state.lrc_raw = ""
                # Mark position as unknown — block lyric rendering until
                # iPhone sends a real Position for this track. Prevents
                # the "lyrics start at 0 then catch up" sync glitch on
                # mid-song skips.
                self.state.position_known = False
                self.state.position_ms = 0
                self.state.position_at_mono = time.monotonic()
                self.state.song_offset_ms = 0   # new song starts un-nudged
                self._last_polled_ms = None
                self._track_changed_at = time.monotonic()
                track_changed = True
                self.start_fetch(title, artist)
                print(f"[track]    {artist} — {title}")
            elif not (title and artist):
                # iPhone clears the track on stop — wipe display lyrics AND the
                # track meta so the idle clock screen shows (the render loop
                # falls back to the clock only when state.title is empty).
                self.state.lines = []
                self.state.title = ""
                self.state.artist = ""
                self.last_sig = sig
                self.state.lyrics_status = "(waiting for music)"
                self.state.awaiting_feedback = False
        if "Position" in changed:
            pos = int(changed["Position"])
            if not self.state.position_known:
                # Haven't locked onto the current track yet. Route every
                # candidate (same-batch on a skip, or YT Music's real
                # new-track broadcast that arrives a beat later) through the
                # same freshness gate, so a stale value can't anchor us.
                self._try_lock(pos)
            else:
                # Locked: a PropertiesChanged Position is always a real
                # iPhone broadcast (never a stale poll cache) → re-anchor.
                self.state.set_position(pos)
                self._last_polled_ms = pos


async def disconnect_other_devices(bus, om, keep_path: str) -> None:
    """Drop every currently-connected phone EXCEPT keep_path.

    Enforces a single active source: when one phone becomes the live AVRCP
    player (or is freshly paired), any other phone still holding a Bluetooth
    link is disconnected so it stops competing. keep_path is the Device1 object
    path to leave alone. Errors are logged, not raised — a stale path or an
    already-gone device must not break the caller."""
    if om is None:
        return
    try:
        objects = await om.call_get_managed_objects()
    except Exception:
        return
    for path, ifaces in objects.items():
        if path == keep_path:
            continue
        dev = ifaces.get(DEVICE_IFACE)
        if not dev:
            continue
        props = _unwrap(dev)
        if not props.get("Connected"):
            continue
        name = props.get("Alias") or props.get("Name") or path
        try:
            intro = await bus.introspect(BLUEZ, path)
            obj = bus.get_proxy_object(BLUEZ, path, intro)
            await obj.get_interface(DEVICE_IFACE).call_disconnect()
            print(f"[bt] disconnected other phone: {name}")
        except Exception as e:
            print(f"[bt] disconnect {name}: {e}")


class BluetoothAdmin:
    """Pairing/forget helper for the on-screen Bluetooth menu (Settings →
    Bluetooth). Shares AvrcpWatcher's system bus.

    Adding a NEW phone (e.g. swapping to one running Apple Music / YouTube
    Music) only needs the adapter to go Discoverable+Pairable for a moment;
    the actual Just-Works pairing is accepted by an external NoInputNoOutput
    agent (bt-agent — see the module docstring), so no PIN/keyboard is needed
    on the Pi. Once a new device pairs we mark it Trusted and connect its AVRCP
    profile so it behaves like the original iPhone (the autoconnect loop then
    keeps it linked).

    All display fields (adapter_alias, paired, pairing, screen_status) are read
    by the render loop on the same event-loop thread, so no lock is needed.
    """
    PAIR_POLL_S = 2.0   # how often we re-scan for the freshly-paired phone

    def __init__(self, bus: MessageBus):
        self.bus = bus
        self._om = None
        self.adapter_path: str | None = None
        self.adapter_props = None       # Properties iface on the adapter
        self.adapter_alias: str = ""    # the name phones see in their BT list
        # (path, display name, connected?) for each paired device.
        self.paired: list[tuple[str, str, bool]] = []
        self.pairing: bool = False
        self.screen_status: str = ""
        self._pair_task: asyncio.Task | None = None
        self._baseline: set[str] = set()  # paired paths when pairing began

    async def start(self) -> None:
        intro = await self.bus.introspect(BLUEZ, "/")
        root = self.bus.get_proxy_object(BLUEZ, "/", intro)
        self._om = root.get_interface(OM_IFACE)
        await self._find_adapter()

    async def _find_adapter(self) -> None:
        if self._om is None:
            return
        objects = await self._om.call_get_managed_objects()
        for path, ifaces in objects.items():
            if ADAPTER_IFACE in ifaces:
                self.adapter_path = path
                intro = await self.bus.introspect(BLUEZ, path)
                obj = self.bus.get_proxy_object(BLUEZ, path, intro)
                self.adapter_props = obj.get_interface(PROPS_IFACE)
                self.adapter_alias = _unwrap(
                    ifaces[ADAPTER_IFACE]).get("Alias", "")
                break

    async def _adapter_set(self, prop: str, value: Variant) -> None:
        if self.adapter_props is not None:
            await self.adapter_props.call_set(ADAPTER_IFACE, prop, value)

    async def refresh_paired(self) -> None:
        """Re-read the paired-device list into self.paired for the screen."""
        if self._om is None:
            return
        try:
            objects = await self._om.call_get_managed_objects()
        except Exception as e:
            print(f"[bt] list error: {e}")
            return
        out = []
        for path, ifaces in objects.items():
            dev = ifaces.get(DEVICE_IFACE)
            if not dev:
                continue
            p = _unwrap(dev)
            if not p.get("Paired"):
                continue
            name = p.get("Alias") or p.get("Name") or path.rsplit("/", 1)[-1]
            out.append((path, name, bool(p.get("Connected"))))
        out.sort(key=lambda t: t[1].lower())
        self.paired = out

    async def open_screen(self) -> None:
        """Called when the Bluetooth menu opens: refresh the device list (and
        adapter name) so the screen reflects reality."""
        if self.adapter_path is None:
            await self._find_adapter()
        await self.refresh_paired()
        if not self.pairing:
            self.screen_status = ""

    def _paired_paths(self) -> set[str]:
        return {p for p, _n, _c in self.paired}

    async def start_pairing(self) -> None:
        """Make the Pi discoverable+pairable and watch for the new phone."""
        if self.adapter_props is None:
            self.screen_status = "No Bluetooth adapter found"
            return
        await self.refresh_paired()
        self._baseline = self._paired_paths()
        try:
            await self._adapter_set("Pairable", Variant("b", True))
            # 0 = stay discoverable until we turn it back off (we do that on
            # success / Cancel / leaving the screen).
            await self._adapter_set("DiscoverableTimeout", Variant("u", 0))
            await self._adapter_set("Discoverable", Variant("b", True))
        except Exception as e:
            print(f"[bt] could not enter pairing mode: {e}")
            self.screen_status = "Could not enter pairing mode"
            return
        self.pairing = True
        name = self.adapter_alias or "this device"
        self.screen_status = f"On your phone, open Bluetooth and tap “{name}”"
        print("[bt] pairing mode ON (discoverable)")
        if self._pair_task is None or self._pair_task.done():
            self._pair_task = asyncio.create_task(self._await_new_device())

    async def stop_pairing(self) -> None:
        """Leave pairing mode: cancel the watcher and hide the adapter again."""
        self.pairing = False
        if self._pair_task and not self._pair_task.done():
            self._pair_task.cancel()
        try:
            await self._adapter_set("Discoverable", Variant("b", False))
        except Exception:
            pass
        print("[bt] pairing mode OFF")

    async def _await_new_device(self) -> None:
        """Poll until a paired device appears that wasn't there when we
        started, then adopt it (trust + connect AVRCP)."""
        try:
            while self.pairing:
                await asyncio.sleep(self.PAIR_POLL_S)
                await self.refresh_paired()
                new = self._paired_paths() - self._baseline
                if new:
                    await self._adopt(sorted(new)[0])
                    return
        except asyncio.CancelledError:
            raise

    async def _adopt(self, path: str) -> None:
        name = next((n for p, n, _c in self.paired if p == path), path)
        print(f"[bt] new phone paired: {name}")
        try:
            intro = await self.bus.introspect(BLUEZ, path)
            obj = self.bus.get_proxy_object(BLUEZ, path, intro)
            props = obj.get_interface(PROPS_IFACE)
            await props.call_set(DEVICE_IFACE, "Trusted", Variant("b", True))
            try:
                await obj.get_interface(
                    DEVICE_IFACE).call_connect_profile(AVRCP_UUID)
            except Exception as e:
                # Phone may not expose AVRCP until it starts playing — the
                # autoconnect loop will keep retrying, so this isn't fatal.
                print(f"[bt] AVRCP connect (will retry): {e}")
        except Exception as e:
            print(f"[bt] adopt error: {e}")
        # Single-phone policy: this device is now THE phone, so unpair every
        # other one. RemoveDevice also disconnects it, so the old phone drops
        # its Bluetooth link instead of lingering connected.
        await self._forget_others(path)
        self.screen_status = f"Paired ✓  {name}"
        await self.stop_pairing()
        await self.refresh_paired()

    async def _remove_device(self, path: str) -> None:
        """Low-level Adapter1.RemoveDevice (unpair + disconnect)."""
        if not self.adapter_path:
            return
        intro = await self.bus.introspect(BLUEZ, self.adapter_path)
        obj = self.bus.get_proxy_object(BLUEZ, self.adapter_path, intro)
        await obj.get_interface(ADAPTER_IFACE).call_remove_device(path)

    async def _forget_others(self, keep_path: str) -> None:
        """Unpair every paired device except keep_path — enforces the
        one-phone-at-a-time rule when a new phone is adopted."""
        await self.refresh_paired()
        for path, name, _conn in list(self.paired):
            if path == keep_path:
                continue
            try:
                await self._remove_device(path)
                print(f"[bt] removed previous phone (one-phone mode): {name}")
            except Exception as e:
                print(f"[bt] remove {name}: {e}")

    async def forget(self, path: str) -> None:
        """Remove (unpair) a device via Adapter1.RemoveDevice."""
        name = next((n for p, n, _c in self.paired if p == path), path)
        try:
            await self._remove_device(path)
            print(f"[bt] forgot {name}")
        except Exception as e:
            print(f"[bt] forget error: {e}")
        await self.refresh_paired()


class FirmwareUpdater:
    """Self-update from GitHub for the Software Version screen.

    Pulls the repo tarball and overwrites only the code/support files in
    UPDATE_FILES (never config.json / cache/ / rejections.json), then restarts
    the service so the new code loads. All blocking work runs in a worker
    thread; `status` is read by the render loop and `busy`/`armed` gate taps.
    `armed` gives a two-tap confirm so a stray touch can't restart the display
    mid-drive."""

    def __init__(self):
        self.status = ""
        self.busy = False
        self.armed = False        # first tap arms, second tap confirms

    def reset(self) -> None:
        """Clear the confirm/state when (re)entering the Version screen."""
        if not self.busy:
            self.armed = False
            self.status = ""

    @staticmethod
    def _parse_version(s):
        """'1.3.0' → (1, 3, 0) for ordered comparison, or None if unparseable."""
        try:
            return tuple(int(x) for x in s.strip().split("."))
        except (ValueError, AttributeError):
            return None

    async def run(self) -> None:
        """Check GitHub, apply only if it's strictly NEWER, then restart.

        The version guard means the button can't downgrade or pointlessly
        restart: if GitHub is the same or older, we say so and leave the running
        build alone. Guarded against re-entry."""
        if self.busy:
            return
        self.busy = True
        self.armed = False
        try:
            self.status = "Checking GitHub…"
            kind, remote_v, count = await asyncio.to_thread(
                self._download_and_apply)
            if kind == "updated":
                self.status = f"Updated to v{remote_v} ({count} files) — restarting…"
                print(f"[update] applied v{remote_v} ({count} files); restarting")
                await asyncio.sleep(1.5)     # let the message land on screen
                await asyncio.to_thread(self._restart)
                # If the restart takes hold, the process is replaced before here.
                self.status = "Restart requested…"
            elif kind == "uptodate":
                self.status = f"Already up to date (v{remote_v})"
                print(f"[update] already up to date (v{remote_v})")
            else:  # "older" — refuse to downgrade
                self.status = f"GitHub has older v{remote_v}; kept v{APP_VERSION}"
                print(f"[update] refused downgrade: GitHub v{remote_v} "
                      f"< running v{APP_VERSION}")
        except Exception as e:
            print(f"[update] failed: {e}")
            self.status = f"Update failed: {e}"
        finally:
            self.busy = False

    @staticmethod
    def _download_and_apply():
        """Blocking: fetch the tarball, compare versions, and (only if GitHub is
        newer) overwrite UPDATE_FILES in place.

        Returns (kind, remote_version, count) where kind is
        "updated" / "uptodate" / "older". Raises on any failure BEFORE touching
        the install; each file is written atomically (temp + replace) and then
        chown'd back to the repo-directory owner, so a root-run update doesn't
        leave root-owned files that break a later scp/git from a dev box."""
        r = requests.get(UPDATE_URL, timeout=30)
        r.raise_for_status()
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            with tarfile.open(fileobj=io.BytesIO(r.content), mode="r:gz") as tar:
                tar.extractall(tmpdir, filter="data")
            # The GitHub tarball wraps everything in a single "<repo>-<branch>/".
            roots = [p for p in tmpdir.iterdir() if p.is_dir()]
            if not roots:
                raise RuntimeError("empty archive")
            src_root = roots[0]

            # Version guard: read the incoming APP_VERSION and only apply when
            # it's strictly newer than what's running.
            remote_v = None
            main_src = src_root / "Lyrics_Display.py"
            if main_src.exists():
                m = re.search(r'APP_VERSION\s*=\s*"([^"]+)"',
                              main_src.read_text(encoding="utf-8",
                                                 errors="replace"))
                if m:
                    remote_v = m.group(1)
            local_t = FirmwareUpdater._parse_version(APP_VERSION)
            remote_t = FirmwareUpdater._parse_version(remote_v)
            if remote_t is not None and local_t is not None:
                if remote_t == local_t:
                    return ("uptodate", remote_v, 0)
                if remote_t < local_t:
                    return ("older", remote_v, 0)
            # remote is newer (or version couldn't be parsed → trust the
            # explicit user request and apply).

            # Owner to restore on each written file (the dir owner, e.g. the
            # repo user), since the service runs as root.
            try:
                dir_stat = os.stat(INSTALL_DIR)
            except OSError:
                dir_stat = None
            count = 0
            for name in UPDATE_FILES:
                src = src_root / name
                if not src.exists():
                    continue
                dst = INSTALL_DIR / name
                tmp_dst = dst.with_name(dst.name + ".new")
                shutil.copy2(src, tmp_dst)
                os.replace(tmp_dst, dst)
                if dir_stat is not None:
                    try:
                        os.chown(dst, dir_stat.st_uid, dir_stat.st_gid)
                    except (OSError, AttributeError):
                        pass   # not root / not POSIX — leave ownership as-is
                count += 1
            if count == 0:
                raise RuntimeError("no matching files in archive")
            return ("updated", remote_v or "?", count)

    @staticmethod
    def _restart() -> None:
        """Restart the service so the new code loads.

        --no-block: hand the restart job to systemd (PID 1) and return at once,
        rather than waiting — otherwise systemd kills this very process (and the
        waiting systemctl child) during the stop phase. The job still runs to
        completion in PID 1. Absolute path because the cage/root env has a
        minimal PATH. The app runs as root via systemd, so plain systemctl
        works; fall back to sudo -n for a non-root launch."""
        last = None
        for cmd in (["/usr/bin/systemctl", "restart", "--no-block", UPDATE_SERVICE],
                    ["sudo", "-n", "/usr/bin/systemctl", "restart", "--no-block",
                     UPDATE_SERVICE]):
            try:
                subprocess.run(cmd, check=True, timeout=15)
                return
            except Exception as e:
                last = e
        raise RuntimeError(f"restart failed ({last})")


class NetworkStatus:
    """Read-only connectivity info for the Network screen (Settings → Network).

    Answers "is the Pi online?" with three facts: the Wi-Fi SSID it joined, its
    LAN IP, and whether the public internet is actually reachable. The
    reachability probe is the one that matters — being associated to Wi-Fi
    doesn't mean traffic flows — so we open a real TCP connection to GitHub,
    which is exactly what OTA updates and lyric fetching depend on. A green
    "Online" therefore means those features will work, not just "Wi-Fi is up".

    All blocking work (subprocess + socket) runs in a worker thread; the render
    loop reads the plain fields, and `busy` gates the Check-now button. Mirrors
    FirmwareUpdater's threading model."""

    # Probe GitHub specifically: it's what the OTA + several lyric sources hit,
    # so its reachability is the connectivity that actually matters here. A full
    # TCP connect exercises DNS + routing + handshake, unlike a bare ping.
    PROBE_HOST = "github.com"
    PROBE_PORT = 443
    PROBE_TIMEOUT_S = 3.0

    def __init__(self):
        self.ssid = ""                  # "" → not on Wi-Fi (or name unknown)
        self.ip = ""                    # "" → no IP address
        self.online: bool | None = None  # None → not checked yet / checking
        self.busy = False

    async def refresh(self) -> None:
        """(Re)collect SSID/IP and probe the internet. Guarded against re-entry
        so a rapid double-tap on Check-now (or reopening the screen mid-check)
        runs the probe once."""
        if self.busy:
            return
        self.busy = True
        self.online = None              # render shows "Checking…" meanwhile
        try:
            self.ssid, self.ip, self.online = await asyncio.to_thread(
                self._collect)
        except Exception as e:
            print(f"[net] check failed: {e}")
            self.ssid, self.ip, self.online = "", "", False
        finally:
            self.busy = False

    @classmethod
    def _collect(cls):
        """Blocking: gather SSID, IP, and internet reachability."""
        return cls._ssid(), cls._ip(), cls._probe()

    @staticmethod
    def _ssid() -> str:
        """Current Wi-Fi SSID, or "" if not associated. iwgetid is lightest;
        fall back to nmcli (already the tool wifi.sh uses)."""
        for cmd in (["iwgetid", "-r"],
                    ["nmcli", "-t", "-f", "active,ssid", "dev", "wifi"]):
            try:
                out = subprocess.run(cmd, capture_output=True, text=True,
                                     timeout=4).stdout.strip()
            except Exception:
                continue
            if not out:
                continue
            if cmd[0] == "iwgetid":
                return out
            # nmcli: lines like "yes:MyNetwork" / "no:Other" — pick the active.
            for line in out.splitlines():
                if line.startswith("yes:"):
                    return line[4:]
        return ""

    @staticmethod
    def _ip() -> str:
        """First IPv4 from `hostname -I` (space-separated), or "" if none."""
        try:
            out = subprocess.run(["hostname", "-I"], capture_output=True,
                                 text=True, timeout=4).stdout.split()
            if out:
                return out[0]
        except Exception:
            pass
        return ""

    @classmethod
    def _probe(cls) -> bool:
        """True if a TCP connection to the probe host completes in time."""
        try:
            with socket.create_connection(
                    (cls.PROBE_HOST, cls.PROBE_PORT), cls.PROBE_TIMEOUT_S):
                return True
        except OSError:
            return False


def decide_lock(real_ms, elapsed_s, fresh_max_ms, timeout_s):
    """Pure policy: should a just-read Position anchor a freshly-started
    track, and why? Returns (should_lock, reason).

    Kept free of BlueZ/pygame so it's unit-testable. See
    AvrcpWatcher._try_lock for the rationale.
    - <= 0: reset/not-started value, never lock.
    - near the top of the track: a genuine fresh start, lock now.
    - otherwise (a stale previous-track value): wait until the stabilize
      window has elapsed, then lock anyway so lyrics never hang.
    """
    if real_ms <= 0:
        return (False, "not-started")
    if real_ms < fresh_max_ms:
        return (True, "fresh start")
    if elapsed_s is not None and elapsed_s > timeout_s:
        return (True, "stabilize timeout")
    return (False, "waiting")


def _line_positions(w: int, h: int):
    """Centers for the prev/current/next lines, spaced from the LIVE font
    sizes so bigger fonts both fill a short bar display and never overlap.
    Recomputed on every config reload so font tweaks reflow the layout."""
    if ROTATION_DEG in (90, 270):
        # Bar LCD orientation — three lines spread horizontally.
        gap = FONT_CURRENT + max(FONT_TOP, FONT_BOTTOM) // 2 + LINE_GAP_PAD
        return ((w // 2 - gap, h // 2),
                (w // 2,       h // 2),
                (w // 2 + gap, h // 2))
    # Normal monitor — three lines stacked vertically. Each gap = half the
    # current font + half that neighbour's own font (so it clears the current
    # line) + LINE_GAP_PAD of breathing room. Top and bottom are spaced
    # independently so they can carry different font sizes.
    top_gap = FONT_CURRENT // 2 + FONT_TOP // 2 + LINE_GAP_PAD
    bot_gap = FONT_CURRENT // 2 + FONT_BOTTOM // 2 + LINE_GAP_PAD
    return ((w // 2, h // 2 - top_gap),
            (w // 2, h // 2),
            (w // 2, h // 2 + bot_gap))


def find_current_index(lines: list[LyricLine], t_ms: int) -> int:
    """Binary-ish scan: last line whose time_ms <= t_ms."""
    idx = -1
    for i, line in enumerate(lines):
        if line.time_ms <= t_ms:
            idx = i
        else:
            break
    return idx


_FONT_CACHE: dict = {}


def get_font(size: int, bold: bool):
    """Memoized font loader. Bold is synthesized via set_bold() so we don't
    depend on a separate bold .ttc being installed."""
    key = (size, bold)
    font = _FONT_CACHE.get(key)
    if font is None:
        font = pygame.font.Font(FONT_PATH, size)
        font.set_bold(bold)
        _FONT_CACHE[key] = font
    return font


def scale_color(color, factor):
    """Multiply an RGB tuple by a 0..1 brightness factor (night dimming)."""
    if factor >= 0.999:
        return color
    return (int(color[0] * factor), int(color[1] * factor),
            int(color[2] * factor))


def brightness_factor() -> float:
    """1.0 in daytime, NIGHT_BRIGHTNESS at night, per the Pi's local clock."""
    if not DIM_ENABLED:
        return 1.0
    hour = time.localtime().tm_hour
    if DAY_START_HOUR <= NIGHT_START_HOUR:
        is_day = DAY_START_HOUR <= hour < NIGHT_START_HOUR
    else:  # day window wraps past midnight
        is_day = hour >= DAY_START_HOUR or hour < NIGHT_START_HOUR
    return 1.0 if is_day else NIGHT_BRIGHTNESS


def draw_line(screen, text, size, bold, color, center_xy, max_len, rotate_deg):
    """Render one centered line, shrinking the font if the text would exceed
    max_len px — so long lines never clip (one quick read per line)."""
    if not text:
        return
    surf = get_font(size, bold).render(text, True, color)
    if max_len and surf.get_width() > max_len:
        shrunk = max(8, int(size * max_len / surf.get_width()))
        if shrunk < size:
            surf = get_font(shrunk, bold).render(text, True, color)
    if rotate_deg:
        surf = pygame.transform.rotate(surf, rotate_deg)
    screen.blit(surf, surf.get_rect(center=center_xy))


def _draw_progress(screen, w, curr_center, lines, idx, t_ms, bf):
    """Thin bar under the current line showing how far through it we are
    (toward when the next line becomes current). No moving words to track."""
    span = lines[idx + 1].time_ms - lines[idx].time_ms
    if span <= 0:
        return
    frac = (t_ms - lines[idx].time_ms) / span
    frac = 0.0 if frac < 0 else (1.0 if frac > 1 else frac)
    bar_w = int(w * 0.40)
    bar_h = 6
    bar_x = w // 2 - bar_w // 2
    bar_y = curr_center[1] + FONT_CURRENT // 2 + 20
    pygame.draw.rect(screen, scale_color(PROGRESS_TRACK, bf),
                     (bar_x, bar_y, bar_w, bar_h), border_radius=3)
    if frac > 0:
        pygame.draw.rect(screen, scale_color(PROGRESS, bf),
                         (bar_x, bar_y, int(bar_w * frac), bar_h),
                         border_radius=3)


def _draw_feedback_buttons(screen, green_rect, red_rect):
    """Translucent edge bars asking 'are these lyrics right?': a green ✓ strip
    on the left, a red ✗ strip on the right. Drawn BEFORE the FLIP_180 flip so
    they ride the same orientation correction as the lyrics (taps are inverted
    to match — see render_loop). Kept to the edges so centered lyrics show
    through on the wide bar display."""
    for rect, fill in ((green_rect, (0, 160, 0, 120)),
                       (red_rect, (200, 0, 0, 120))):
        bar = pygame.Surface((rect.w, rect.h), pygame.SRCALPHA)
        bar.fill(fill)
        screen.blit(bar, rect.topleft)
    # Symbols sized/thickened off the (narrow) bar width, in solid WHITE for
    # contrast — a same-hue tint on a coloured bar was washing them out.
    s = max(14, green_rect.w // 3)
    lw = max(8, green_rect.w // 8)
    white = (255, 255, 255)
    # Green check mark.
    gx, gy = green_rect.center
    pygame.draw.lines(screen, white, False,
                      [(gx - s, gy), (gx - s // 3, gy + s), (gx + s, gy - s)], lw)
    # Red cross.
    rx, ry = red_rect.center
    pygame.draw.line(screen, white, (rx - s, ry - s), (rx + s, ry + s), lw)
    pygame.draw.line(screen, white, (rx - s, ry + s), (rx + s, ry - s), lw)


# ---- Source picker (RED button → 3x3 candidate grid) -----------------------
def _picker_layout(w: int, h: int):
    """A fixed 3x3 grid of candidate cells, row-major, in LOGICAL (pre-FLIP_180)
    pixels. Shared by the draw pass and the touch hit-test so they never drift.
    Always 9 cells; empties are drawn faint when there are fewer candidates."""
    cols = rows = 3
    mx = max(16, int(w * 0.03))
    top = max(12, int(h * 0.03))
    bottom = max(12, int(h * 0.03))
    gx = max(10, int(w * 0.015))
    gy = max(10, int(h * 0.02))
    cell_w = (w - 2 * mx - (cols - 1) * gx) // cols
    cell_h = (h - top - bottom - (rows - 1) * gy) // rows
    cells = []
    for r in range(rows):
        for c in range(cols):
            x = mx + c * (cell_w + gx)
            y = top + r * (cell_h + gy)
            cells.append(pygame.Rect(x, y, cell_w, cell_h))
    return cells


def _fit_text(text: str, font, max_w: int) -> str:
    """Truncate text with a trailing ellipsis so it fits within max_w px."""
    if not text or font.size(text)[0] <= max_w:
        return text
    while text and font.size(text + "…")[0] > max_w:
        text = text[:-1]
    return (text + "…") if text else "…"


def draw_picker(screen, w, h, candidates) -> None:
    """The consolidated multi-source candidate grid (RED button). Each filled
    cell is a tappable lyric option showing the song title (big) and artist
    (smaller), centred so it reads from across the car; empty cells (when fewer
    than 9 matched) are dimmed. The source isn't shown — dropping that chip
    frees the whole cell for larger text (still tracked internally for caching).
    Drawn before the FLIP_180 flip, like the menus (taps inverted to match)."""
    screen.fill(BG)
    cells = _picker_layout(w, h)
    cell_h = cells[0].h if cells else h // 3
    # Fonts scale off the cell height so the title fills the freed space; the
    # min/max clamps keep it sane on very short or very tall panels.
    title_font = get_font(max(26, min(60, int(cell_h * 0.36))), True)
    sub_font = get_font(max(20, min(46, int(cell_h * 0.27))), False)
    gap = max(4, cell_h // 16)
    for i, rect in enumerate(cells):
        if i >= len(candidates):
            pygame.draw.rect(screen, (30, 30, 38), rect, border_radius=12)
            continue
        c = candidates[i]
        pygame.draw.rect(screen, (48, 48, 62), rect, border_radius=12)
        inner = rect.w - 2 * max(10, rect.w // 22)
        ts = title_font.render(_fit_text(c["title"], title_font, inner),
                               True, (245, 245, 245))
        a_s = sub_font.render(_fit_text(c["artist"], sub_font, inner),
                              True, (185, 185, 195))
        # Vertically centre the title+artist block inside the cell.
        block_h = ts.get_height() + gap + a_s.get_height()
        y0 = rect.y + (rect.h - block_h) // 2
        screen.blit(ts, ts.get_rect(midtop=(rect.centerx, y0)))
        screen.blit(a_s, a_s.get_rect(
            midtop=(rect.centerx, y0 + ts.get_height() + gap)))


def draw_safety(screen, w, h, remaining: float) -> None:
    """Full-screen road-safety notice shown for the first SAFETY_NOTICE_S
    seconds after startup. Amber on dark with an attention border and a
    countdown so it's clearly temporary. Drawn before the FLIP_180 flip and via
    draw_line so it rides the same orientation correction as the lyrics."""
    screen.fill(BG)
    amber = (255, 190, 60)
    pygame.draw.rect(screen, amber, screen.get_rect(),
                     width=max(4, h // 80), border_radius=10)
    along = h if ROTATION_DEG in (90, 270) else w
    max_len = int(along * 0.92)
    big = max(28, min(72, h // 6))
    sub = max(18, min(38, h // 11))
    small = max(14, min(26, h // 16))
    cy = h // 2
    draw_line(screen, "KEEP YOUR EYES ON THE ROAD", big, True, amber,
              (w // 2, cy - sub), max_len, ROTATION_DEG)
    draw_line(screen, "Glance only — set up while parked or on a desktop.",
              sub, False, (235, 235, 235),
              (w // 2, cy + big // 2 + 4), max_len, ROTATION_DEG)
    if remaining > 0:
        draw_line(screen, f"Starting in {int(remaining) + 1}s…", small, False,
                  (160, 160, 160), (w // 2, h - small - 8), max_len, ROTATION_DEG)


def _draw_brightness_bar(screen, w, h, level, btn_w):
    """Vertical brightness slider hugging the right edge, positioned just LEFT
    of where the red feedback button sits so the two never overlap. `level`
    (0..1) sets the fill height (from the bottom) and the % label above it.
    Drawn before the FLIP_180 flip, like the feedback buttons."""
    bar_w = max(14, int(w * 0.013))
    bar_h = int(h * 0.6)
    gap = max(24, btn_w // 2)
    bar_x = w - btn_w - gap - bar_w           # left of the red button strip
    bar_y = (h - bar_h) // 2
    radius = bar_w // 2
    # Track.
    pygame.draw.rect(screen, (70, 70, 70), (bar_x, bar_y, bar_w, bar_h),
                     border_radius=radius)
    # Fill from the bottom up.
    fill_h = int(bar_h * level)
    if fill_h > 0:
        pygame.draw.rect(screen, (255, 220, 120),
                         (bar_x, bar_y + bar_h - fill_h, bar_w, fill_h),
                         border_radius=radius)
    # Percentage label just above the bar.
    pct = get_font(max(14, FONT_SYNC), True).render(
        f"{int(round(level * 100))}%", True, (255, 230, 150))
    screen.blit(pct, pct.get_rect(center=(bar_x + bar_w // 2, bar_y - 22)))


# ---- Main settings menu ----------------------------------------------------
# Top level reached by the long-press: pick a sub-screen (or Close to return to
# the lyrics). Each entry is (screen-key, button label) in draw order.
MAIN_MENU_ITEMS = (
    ("font", "Font Settings"),
    ("bluetooth", "Bluetooth"),
    ("other", "Other Settings"),
    ("network", "Network"),
    ("version", "Software Version"),
    ("close", "Close"),
)


def _main_menu_layout(w: int, h: int):
    """Evenly stacked full-width buttons. Returns [(key, Rect), ...] in draw
    order; shared by the draw pass and the touch hit-test."""
    n = len(MAIN_MENU_ITEMS)
    margin_x = max(20, int(w * 0.12))
    top = int(h * 0.09)
    gap = max(12, int(h * 0.03))
    avail = h - top - int(h * 0.05)
    btn_h = min(int(h * 0.17), (avail - (n - 1) * gap) // n)
    rects = []
    for i, (key, _label) in enumerate(MAIN_MENU_ITEMS):
        y = top + i * (btn_h + gap)
        rects.append((key, pygame.Rect(margin_x, y, w - 2 * margin_x, btn_h)))
    return rects


def draw_main_menu(screen, w, h) -> None:
    """The top-level Settings menu. Drawn before the FLIP_180 flip."""
    screen.fill(BG)
    font = get_font(max(22, min(48, h // 15)), True)
    labels = dict(MAIN_MENU_ITEMS)
    for key, rect in _main_menu_layout(w, h):
        bg = (40, 120, 50) if key == "close" else (45, 45, 58)
        pygame.draw.rect(screen, bg, rect, border_radius=14)
        lbl = font.render(labels[key], True, (235, 235, 235))
        screen.blit(lbl, lbl.get_rect(center=rect.center))


# ---- Bluetooth screen ------------------------------------------------------
def _bt_layout(w: int, h: int, n_paired: int):
    """Geometry for the Bluetooth screen. Returns (pair, back, rows, list_top)
    where rows = [(row_rect, forget_rect), ...] for each paired device."""
    margin_x = max(20, int(w * 0.06))
    pair = pygame.Rect(margin_x, int(h * 0.13), w - 2 * margin_x, int(h * 0.15))
    back = pygame.Rect(margin_x, h - int(h * 0.19), w - 2 * margin_x,
                       int(h * 0.15))
    list_top = pair.bottom + int(h * 0.12)
    list_bottom = back.top - int(h * 0.03)
    rows = []
    if n_paired > 0:
        row_h = max(44, min(int(h * 0.11),
                            (list_bottom - list_top) // n_paired))
        fw = max(110, int(w * 0.20))
        for i in range(n_paired):
            y = list_top + i * row_h
            row = pygame.Rect(margin_x, y, w - 2 * margin_x, row_h - 8)
            forget = pygame.Rect(row.right - fw, row.y, fw, row.h)
            rows.append((row, forget))
    return pair, back, rows, list_top


def draw_bluetooth(screen, w, h, bt: "BluetoothAdmin") -> None:
    """Pair-new-phone button + status + the paired-device list with Forget
    buttons, plus Back. Drawn before the FLIP_180 flip."""
    screen.fill(BG)
    title_font = get_font(max(20, min(42, h // 17)), True)
    body_font = get_font(max(16, min(30, h // 26)), False)
    pair, back, rows, list_top = _bt_layout(w, h, len(bt.paired))
    # Pair button — amber while actively in pairing mode.
    pygame.draw.rect(screen, (150, 110, 30) if bt.pairing else (40, 90, 140),
                     pair, border_radius=14)
    pair_lbl = "Cancel Pairing" if bt.pairing else "Pair New Phone"
    t = title_font.render(pair_lbl, True, (255, 255, 255))
    screen.blit(t, t.get_rect(center=pair.center))
    # Status line just under the pair button.
    if bt.screen_status:
        s = body_font.render(bt.screen_status, True, (255, 230, 150))
        screen.blit(s, s.get_rect(midtop=(w // 2, pair.bottom + 12)))
    # Paired devices, each with a Forget button.
    if bt.paired:
        for (row, forget), (_path, name, connected) in zip(rows, bt.paired):
            tag = "  • connected" if connected else ""
            nm = body_font.render(name + tag, True,
                                  (235, 235, 235) if connected
                                  else (185, 185, 185))
            screen.blit(nm, (row.x + 6, row.centery - nm.get_height() // 2))
            pygame.draw.rect(screen, (150, 40, 40), forget, border_radius=10)
            fl = body_font.render("Forget", True, (255, 255, 255))
            screen.blit(fl, fl.get_rect(center=forget.center))
    else:
        none = body_font.render("No phones paired yet", True, (150, 150, 150))
        screen.blit(none, none.get_rect(midtop=(w // 2, list_top)))
    # Back button.
    pygame.draw.rect(screen, (45, 45, 58), back, border_radius=14)
    b = title_font.render("Back", True, (235, 235, 235))
    screen.blit(b, b.get_rect(center=back.center))


# ---- Software version screen -----------------------------------------------
def _version_layout(w: int, h: int):
    """Geometry for the Version screen: (update_btn, back_btn). Both full-width,
    stacked at the bottom so the build info sits above them."""
    margin_x = max(20, int(w * 0.12))
    bw = w - 2 * margin_x
    bh = int(h * 0.15)
    back = pygame.Rect(margin_x, h - int(h * 0.19), bw, bh)
    update = pygame.Rect(margin_x, back.y - bh - max(12, int(h * 0.04)), bw, bh)
    return update, back


def draw_version(screen, w, h, bt: "BluetoothAdmin",
                 updater: "FirmwareUpdater") -> None:
    """Build info + Bluetooth name, plus an Update Firmware button (pulls the
    latest code from GitHub) and Back. Drawn before FLIP_180."""
    screen.fill(BG)
    big = get_font(max(26, min(64, h // 11)), True)
    small = get_font(max(16, min(32, h // 24)), False)
    btn_font = get_font(max(20, min(42, h // 17)), True)
    lines = [
        (big.render("carlyrics", True, (235, 235, 235))),
        (small.render(f"version {APP_VERSION}", True, (185, 185, 185))),
    ]
    if bt.adapter_alias:
        lines.append(small.render(f"Bluetooth name: {bt.adapter_alias}",
                                  True, (150, 150, 150)))
    total = sum(s.get_height() for s in lines) + 12 * (len(lines) - 1)
    y = int(h * 0.10)
    for surf in lines:
        screen.blit(surf, surf.get_rect(midtop=(w // 2, y)))
        y += surf.get_height() + 12
    update, back = _version_layout(w, h)
    # Update button: amber while armed/working, slate otherwise.
    busy_or_armed = updater.busy or updater.armed
    pygame.draw.rect(screen, (150, 110, 30) if busy_or_armed else (40, 90, 140),
                     update, border_radius=14)
    ulbl = "Updating…" if updater.busy else (
        "Tap again to confirm" if updater.armed else "Update Firmware")
    u = btn_font.render(ulbl, True, (255, 255, 255))
    screen.blit(u, u.get_rect(center=update.center))
    # Status line between the two buttons.
    if updater.status:
        s = small.render(updater.status, True, (255, 230, 150))
        screen.blit(s, s.get_rect(center=(w // 2, (update.bottom + back.top) // 2)))
    # Back button.
    pygame.draw.rect(screen, (45, 45, 58), back, border_radius=14)
    bl = btn_font.render("Back", True, (235, 235, 235))
    screen.blit(bl, bl.get_rect(center=back.center))


# ---- Network screen --------------------------------------------------------
def _network_layout(w: int, h: int):
    """Geometry for the Network screen: (check_btn, back_btn). Mirrors the
    Version screen — two stacked full-width buttons with the info above."""
    margin_x = max(20, int(w * 0.12))
    bw = w - 2 * margin_x
    bh = int(h * 0.15)
    back = pygame.Rect(margin_x, h - int(h * 0.19), bw, bh)
    check = pygame.Rect(margin_x, back.y - bh - max(12, int(h * 0.04)), bw, bh)
    return check, back


def draw_network(screen, w, h, net: "NetworkStatus") -> None:
    """Wi-Fi SSID / IP / Internet status, a Check-now button and Back. The
    Internet line is the headline answer to "is the Pi online?" and is colour-
    coded (green online / red offline / amber checking). Drawn before FLIP_180."""
    screen.fill(BG)
    big = get_font(max(26, min(64, h // 11)), True)
    small = get_font(max(16, min(32, h // 24)), False)
    btn_font = get_font(max(20, min(42, h // 17)), True)

    if net.online is None:
        net_txt, net_col = "Checking…", (210, 200, 120)
    elif net.online:
        net_txt, net_col = "Online", (120, 210, 120)
    else:
        net_txt, net_col = "Offline", (225, 110, 110)

    lines = [
        big.render("Network", True, (235, 235, 235)),
        small.render(f"Wi-Fi: {net.ssid or 'Not connected'}",
                     True, (185, 185, 185)),
        small.render(f"IP address: {net.ip or '—'}", True, (150, 150, 150)),
        small.render(f"Internet: {net_txt}", True, net_col),
    ]
    y = int(h * 0.10)
    for surf in lines:
        screen.blit(surf, surf.get_rect(midtop=(w // 2, y)))
        y += surf.get_height() + 12

    check, back = _network_layout(w, h)
    pygame.draw.rect(screen, (150, 110, 30) if net.busy else (40, 90, 140),
                     check, border_radius=14)
    c = btn_font.render("Checking…" if net.busy else "Check now",
                        True, (255, 255, 255))
    screen.blit(c, c.get_rect(center=check.center))
    pygame.draw.rect(screen, (45, 45, 58), back, border_radius=14)
    bl = btn_font.render("Back", True, (235, 235, 235))
    screen.blit(bl, bl.get_rect(center=back.center))


# ---- Other settings screen -------------------------------------------------
# Misc toggles/steppers that don't belong on the font panel. Each row is
# (key, label, kind, *args):
#   ("flip_180", "...", "toggle")                  → live FLIP_180/DIM_ENABLED
#   ("latency_offset_ms", "...", "step", lo, hi, step)  → ± a numeric global
# lo/hi/step are in the global's own units (ms here). Edits preview live and
# are written to config.json immediately, riding the same hot-reload path as a
# hand edit.
OTHER_ROWS = (
    ("flip_180", "Rotate Screen 180°", "toggle"),
    ("latency_offset_ms", "Bluetooth A2DP Offset", "step", 0, 3000, 100),
    ("lead_offset_ms", "Lyrics Timing Offset", "step", -3000, 3000, 500),
    ("dim_enabled", "Auto Dim", "toggle"),
)


def _other_value(key: str):
    """Current live value for an Other-settings key (reads the global)."""
    return {
        "flip_180": FLIP_180,
        "latency_offset_ms": LATENCY_OFFSET_MS,
        "lead_offset_ms": LEAD_OFFSET_MS,
        "dim_enabled": DIM_ENABLED,
    }[key]


def _fmt_offset(ms: int, signed: bool) -> str:
    """ms → a compact seconds label, e.g. '0.3s' or '+1.5s' / '-0.5s'."""
    return f"{ms / 1000.0:+.1f}s" if signed else f"{ms / 1000.0:.1f}s"


def _other_layout(w: int, h: int):
    """Geometry for the Other Settings screen, in LOGICAL (pre-FLIP_180) px.

    Returns (rows, back) where rows = [(key, kind, controls), ...] and controls
    is {"row": Rect, "toggle": Rect} for a toggle, or
    {"row": Rect, "minus": Rect, "plus": Rect, "value": Rect} for a stepper."""
    margin_x = max(20, int(w * 0.06))
    top = int(h * 0.08)
    back_h = int(h * 0.15)
    bottom_pad = int(h * 0.03)
    n = len(OTHER_ROWS)
    gap = max(10, int(h * 0.025))
    avail = h - top - back_h - bottom_pad - int(h * 0.04)
    row_h = max(56, (avail - (n - 1) * gap) // n)
    # Controls live in the right ~46% of each row; the label fills the left.
    ctrl_w = int(w * 0.46)
    ctrl_x = w - margin_x - ctrl_w
    btn = min(row_h - 8, int(ctrl_w * 0.30))
    rows = []
    for i, row in enumerate(OTHER_ROWS):
        key, kind = row[0], row[2]
        y = top + i * (row_h + gap)
        rrect = pygame.Rect(margin_x, y, w - 2 * margin_x, row_h)
        cy = y + (row_h - btn) // 2
        if kind == "toggle":
            tw = int(ctrl_w * 0.55)
            toggle = pygame.Rect(ctrl_x + ctrl_w - tw, cy, tw, btn)
            rows.append((key, kind, {"row": rrect, "toggle": toggle}))
        else:
            minus = pygame.Rect(ctrl_x, cy, btn, btn)
            plus = pygame.Rect(ctrl_x + ctrl_w - btn, cy, btn, btn)
            value = pygame.Rect(minus.right, y, plus.left - minus.right, row_h)
            rows.append((key, kind,
                         {"row": rrect, "minus": minus, "plus": plus,
                          "value": value}))
    margin_b = max(20, int(w * 0.12))
    back = pygame.Rect(margin_b, h - back_h - bottom_pad, w - 2 * margin_b,
                       back_h)
    return rows, back


def draw_other(screen, w, h) -> None:
    """Render the Other Settings screen: Yes/No toggles + ± steppers, plus
    Back. Reads the live globals; drawn before the FLIP_180 flip."""
    screen.fill(BG)
    label_font = get_font(max(18, min(36, h // 20)), True)
    val_font = get_font(max(20, min(40, h // 18)), True)
    btn_font = get_font(max(22, min(46, h // 16)), True)
    rows, back = _other_layout(w, h)
    labels = {r[0]: r[1] for r in OTHER_ROWS}
    for key, kind, ctrl in rows:
        rrect = ctrl["row"]
        lbl = label_font.render(labels[key], True, (235, 235, 235))
        screen.blit(lbl, (rrect.x + 6, rrect.centery - lbl.get_height() // 2))
        if kind == "toggle":
            on = bool(_other_value(key))
            t = ctrl["toggle"]
            pygame.draw.rect(screen, (40, 120, 50) if on else (95, 55, 55),
                             t, border_radius=12)
            ts = btn_font.render("Yes" if on else "No", True, (255, 255, 255))
            screen.blit(ts, ts.get_rect(center=t.center))
        else:
            for bk, sym in (("minus", "−"), ("plus", "+")):
                b = ctrl[bk]
                pygame.draw.rect(screen, (45, 80, 130), b, border_radius=10)
                bs = btn_font.render(sym, True, (255, 255, 255))
                screen.blit(bs, bs.get_rect(center=b.center))
            vtxt = _fmt_offset(_other_value(key), key == "lead_offset_ms")
            vs = val_font.render(vtxt, True, (255, 220, 120))
            screen.blit(vs, vs.get_rect(center=ctrl["value"].center))
    pygame.draw.rect(screen, (45, 45, 58), back, border_radius=14)
    bl = btn_font.render("Back", True, (235, 235, 235))
    screen.blit(bl, bl.get_rect(center=back.center))


# Settings panel rows, in top-to-bottom display order. Each is
# (key, human label). The key indexes into the live font/colour globals.
SETTINGS_ROWS = (
    ("top", "Top line"),
    ("current", "Current line"),
    ("bottom", "Bottom line"),
)


def _settings_layout(w: int, h: int):
    """Geometry for the settings panel, in LOGICAL (pre-FLIP_180) pixels.

    Pure geometry only — no live values — so the draw pass and the touch
    hit-test share one source of truth for where every control sits. Returns
    (sliders, swatches, bolds, done) where:
      sliders  = {key: Rect}                       — the size-slider track
      swatches = {key: [(name, rgb, Rect), ...]}   — six colour squares
      bolds    = {key: Rect}                        — the Bold/Normal toggle
      done     = Rect                              — the Done/save button
    """
    margin_x = max(20, int(w * 0.08))
    content_w = w - 2 * margin_x
    top = int(h * 0.07)
    done_h = max(48, int(h * 0.11))
    bottom_pad = int(h * 0.03)
    avail = h - top - done_h - bottom_pad
    sec_h = max(60, avail // len(SETTINGS_ROWS))

    # Each section is split horizontally: the LEFT 3/5 holds the size slider +
    # colour swatches, the RIGHT 2/5 is a big Bold/Normal button. Keeping them
    # in separate columns means the slider's wide (vertically generous) hit zone
    # can never swallow a tap meant for the Bold button.
    col_gap = max(16, int(content_w * 0.03))
    left_w = int(content_w * 0.60)
    right_x = margin_x + left_w + col_gap
    right_w = content_w - left_w - col_gap
    sw = max(22, min(int(sec_h * 0.34), left_w // 8))
    gap = (left_w - 6 * sw) // 5 if left_w > 6 * sw else 6

    sliders, swatches, bolds = {}, {}, {}
    for i, (key, _label) in enumerate(SETTINGS_ROWS):
        sec_y = top + i * sec_h
        slider_y = sec_y + int(sec_h * 0.34)
        slider_h = max(10, int(sec_h * 0.09))
        sliders[key] = pygame.Rect(margin_x, slider_y, left_w, slider_h)
        sw_y = sec_y + int(sec_h * 0.58)
        rects = []
        for j, (name, rgb) in enumerate(SETTING_COLORS):
            x = margin_x + j * (sw + gap)
            rects.append((name, rgb, pygame.Rect(x, sw_y, sw, sw)))
        swatches[key] = rects
        # Big Bold/Normal button filling the right column.
        bolds[key] = pygame.Rect(right_x, sec_y + int(sec_h * 0.12),
                                 right_w, int(sec_h * 0.68))
    done_w = max(140, int(w * 0.30))
    done = pygame.Rect(w // 2 - done_w // 2, top + len(SETTINGS_ROWS) * sec_h,
                       done_w, done_h)
    return sliders, swatches, bolds, done


def draw_settings(screen, w, h, sizes: dict, names: dict, bolds: dict) -> None:
    """Render the settings panel: a size slider + 6-colour palette + Bold/Normal
    toggle per line, plus a Done button. `sizes` maps key→font px, `names` maps
    key→colour name (lowercased), `bolds` maps key→bool. Drawn before the
    FLIP_180 flip so it rides the same orientation correction as everything
    else."""
    screen.fill(BG)
    sliders, swatches, bold_rects, done = _settings_layout(w, h)
    label_font = get_font(max(20, min(40, h // 22)), True)
    for key, label in SETTINGS_ROWS:
        rect = sliders[key]
        size = sizes[key]
        lbl = label_font.render(f"{label} — {size}px", True, (235, 235, 235))
        screen.blit(lbl, (rect.x, rect.y - lbl.get_height() - 8))
        # Big Bold/Normal toggle on the right: filled blue when bold, dim grey
        # when normal. Font scales with the button so it reads at a glance.
        brect = bold_rects[key]
        is_bold = bolds.get(key, False)
        pygame.draw.rect(screen, (40, 90, 140) if is_bold else (60, 60, 68),
                         brect, border_radius=12)
        bold_font = get_font(max(18, min(48, brect.height // 2)), True)
        bsurf = bold_font.render("Bold" if is_bold else "Normal", True,
                                 (255, 255, 255) if is_bold else (205, 205, 205))
        screen.blit(bsurf, bsurf.get_rect(center=brect.center))
        # Slider track + filled portion + knob.
        radius = max(2, rect.h // 2)
        pygame.draw.rect(screen, (70, 70, 70), rect, border_radius=radius)
        frac = (size - SETTINGS_FONT_MIN) / (SETTINGS_FONT_MAX - SETTINGS_FONT_MIN)
        frac = min(1.0, max(0.0, frac))
        fill_w = int(rect.w * frac)
        if fill_w > 0:
            pygame.draw.rect(screen, (255, 220, 120),
                             (rect.x, rect.y, fill_w, rect.h), border_radius=radius)
        knob_r = max(rect.h, 16)
        pygame.draw.circle(screen, (255, 255, 255),
                           (rect.x + fill_w, rect.centery), knob_r)
        # Colour swatches; the selected one gets a white outline.
        for name, rgb, srect in swatches[key]:
            pygame.draw.rect(screen, rgb, srect, border_radius=8)
            if names.get(key) == name.lower():
                pygame.draw.rect(screen, (255, 255, 255), srect.inflate(10, 10),
                                 3, border_radius=11)
    # Done button.
    pygame.draw.rect(screen, (40, 120, 50), done, border_radius=14)
    dlbl = label_font.render("Done", True, (255, 255, 255))
    screen.blit(dlbl, dlbl.get_rect(center=done.center))


async def render_loop(state: State, watcher: "AvrcpWatcher",
                      bt: "BluetoothAdmin", updater: "FirmwareUpdater") -> None:
    pygame.init()
    screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
    w, h = screen.get_size()
    print(f"[display] {w} x {h} (SDL video driver: {pygame.display.get_driver()})")

    # Hide the pointer (after set_mode so SDL registers it against the window).
    # The real cursor fix lives in a udev rule that stops the iPhone's AVRCP
    # device from being seen as a mouse; this is just standard kiosk hygiene.
    pygame.mouse.set_visible(False)

    prev_pos, curr_pos, next_pos = _line_positions(w, h)

    # Touch-feedback buttons: full-height strips hugging each edge, wide enough
    # to hit at a glance while driving. Defined in logical (pre-flip) coords.
    btn_w = max(55, int(w * 0.035))
    green_rect = pygame.Rect(0, 0, btn_w, h)
    red_rect = pygame.Rect(w - btn_w, 0, btn_w, h)
    last_tap = 0.0   # monotonic timestamp, for debouncing double taps/events

    def handle_tap(px: float, py: float) -> None:
        nonlocal last_tap
        now_t = time.monotonic()
        if now_t - last_tap < 0.4:   # ignore FINGERDOWN+MOUSEBUTTONDOWN doubles
            return
        # Buttons are drawn before the 180° flip, so when FLIP_180 is on the
        # physical touch panel is rotated relative to the upright image —
        # invert the tap to land in the same logical space as the buttons.
        if FLIP_180:
            px, py = w - 1 - px, h - 1 - py
        if not (state.awaiting_feedback and state.lines):
            return
        if green_rect.collidepoint(px, py):
            last_tap = now_t
            try:
                # Bake any live per-song sync nudge into the saved timestamps so
                # it survives restarts (shift by -song_offset_ms — see now_ms).
                # Also rewrite the in-memory lines and zero the nudge so the
                # currently-playing display is unchanged (shifted lines + 0
                # offset == original lines + offset).
                lrc_to_cache = state.lrc_raw
                if state.song_offset_ms:
                    lrc_to_cache = shift_lrc_timestamps(
                        state.lrc_raw, -state.song_offset_ms)
                    state.lrc_raw = lrc_to_cache
                    state.lines = parse_lrc(lrc_to_cache)
                    print(f"[feedback] baked sync {state.song_offset_ms:+d}ms "
                          f"into cached lyrics")
                    state.song_offset_ms = 0
                save_to_cache(state.title, state.artist, lrc_to_cache)
                print(f"[feedback] ✓ correct → cached ({state.lyrics_source})")
            except Exception as e:
                print(f"[feedback] cache save failed: {e}")
            state.awaiting_feedback = False
        elif red_rect.collidepoint(px, py):
            last_tap = now_t
            print("[feedback] ✗ wrong → opening multi-source picker")
            asyncio.create_task(open_picker())

    # ---- Touch gestures -----------------------------------------------------
    # fingers maps SDL finger_id → {sx, sy, x, y, t} in LOGICAL (pre-flip)
    # pixels (+ down-time). Drives three gestures:
    #   • two-finger horizontal swipe → sync nudge (swipe_fired latches one
    #     nudge per gesture until every finger lifts);
    #   • double-tap → toggle the brightness slider (brightness_ui);
    #   • one-finger vertical swipe while the slider is shown → brightness.
    fingers: dict = {}
    swipe_fired = False
    swipe_min_px = max(40, int(w * SWIPE_MIN_FRAC))
    tap_max_move_px = max(20, int(w * TAP_MAX_MOVE_FRAC))
    toast_text = ""
    toast_until = 0.0
    brightness_ui = False        # slider visible?
    user_brightness = 1.0        # 0.15..1.0 software dimmer
    last_tap_time = 0.0          # for double-/triple-tap detection
    tap_count = 0                # consecutive quick taps (2 = brightness, 3 = delete)

    # ---- Source picker (RED button → 3x3 candidate grid) -------------------
    # picker is the candidate list while the grid is shown (None = hidden).
    # While picker_searching we're gathering candidates in a worker thread.
    # picker_cache (+ its sig) lets a re-open of the SAME song skip the network.
    picker = None
    picker_searching = False
    picker_cache: list = []
    picker_cache_sig = None
    last_picker_tap = 0.0

    # ---- Settings menu (long-press 10s) -------------------------------------
    # menu_screen drives a small state machine that replaces lyric rendering:
    #   None         → live lyrics
    #   "main"       → top-level menu (Font / Bluetooth / Software Version)
    #   "font"       → the size-slider + colour-swatch panel
    #   "bluetooth"  → pair a new phone / forget paired phones
    #   "other"      → misc toggles/steppers (flip, A2DP/lyric offset, auto-dim)
    #   "version"    → read-only build info
    #   "network"    → read-only connectivity (SSID / IP / online) + Check now
    menu_screen: str | None = None
    net = NetworkStatus()        # connectivity for the Network screen
    settings_armed = False       # ignore touches until the opening hold lifts
    # A touch fires BOTH a FINGERDOWN and a synthesized MOUSEBUTTONDOWN; without
    # debouncing, every menu tap is handled twice — harmless for sliders/swatches
    # (idempotent) but it double-toggles the Bold buttons (net no-op) and would
    # arm+confirm the firmware update in one tap. Ignore a second tap within this
    # window. Drags (FINGERMOTION) are NOT gated by this.
    last_menu_tap = 0.0
    # Selected colour name per line, for the swatch highlight + save. Refreshed
    # from the live colours each time the panel opens.
    set_color_names = {"current": color_name_of(CURRENT),
                       "top": color_name_of(PREV),
                       "bottom": color_name_of(NEXT)}

    def _logical_x(nx: float) -> float:
        """Normalized touch x (0..1) → logical pixel x, undoing FLIP_180 so a
        physical left→right drag reads as left→right regardless of mounting."""
        px = nx * w
        return (w - 1 - px) if FLIP_180 else px

    def _logical_y(ny: float) -> float:
        """Normalized touch y (0..1) → logical pixel y (FLIP_180-corrected)."""
        py = ny * h
        return (h - 1 - py) if FLIP_180 else py

    def fire_swipe(direction: int) -> None:
        """direction: +1 = left→right (delay lyrics), -1 = right→left (advance)."""
        nonlocal toast_text, toast_until
        if not state.lines:
            return
        state.song_offset_ms += -SWIPE_STEP_MS if direction > 0 else SWIPE_STEP_MS
        toast_text = f"sync {state.song_offset_ms / 1000:+.2f}s"
        toast_until = time.monotonic() + 1.5
        print(f"[nudge] {'delay' if direction > 0 else 'advance'} → "
              f"song_offset={state.song_offset_ms:+d}ms")

    def delete_current_lyrics() -> None:
        """Triple-tap: evict the playing song's cached LRC so the next play
        re-searches it. Same effect as the RED button on a cache hit, but
        reachable any time lyrics are on screen. Safe if nothing was cached
        (delete_from_cache no-ops on a missing file)."""
        nonlocal toast_text, toast_until
        if not state.lines:
            return                # nothing displayed → nothing to delete
        try:
            delete_from_cache(state.title, state.artist)
            print(f"[delete] cache evicted → {state.artist} — {state.title}")
        except Exception as e:
            print(f"[delete] cache evict failed: {e}")
        toast_text = "This lyric has been deleted."
        toast_until = time.monotonic() + 2.5

    async def open_picker() -> None:
        """RED tapped: gather a consolidated multi-source candidate list and
        show the 3x3 grid. Re-opening the same song reuses the cached results
        (no second network sweep). Runs the blocking fetch in a thread."""
        nonlocal picker, picker_searching, picker_cache, picker_cache_sig
        if picker_searching or picker is not None:
            return
        sig = (state.title, state.artist)
        if picker_cache_sig == sig and picker_cache:
            picker = picker_cache
            fingers.clear()      # drop the RED-tap finger so it can't linger
            return
        picker_searching = True
        try:
            cands = await asyncio.to_thread(
                search_candidates, state.title, state.artist)
        except Exception as e:
            print(f"[picker] search error: {e}")
            cands = []
        picker_searching = False
        # A track change during the sweep makes these results stale — drop them.
        if (state.title, state.artist) != sig:
            return
        if cands:
            picker_cache, picker_cache_sig, picker = cands, sig, cands
            fingers.clear()      # drop the RED-tap finger so it can't linger
            print(f"[picker] showing {len(cands)} candidate(s)")
        else:
            state.lyrics_status = "♪ No other versions found"
            print("[picker] no candidates found")

    def select_candidate(i: int) -> None:
        """Tap on a grid cell: show that candidate's lyrics and keep BOTH
        feedback buttons up — GREEN to confirm/cache, RED to reopen the grid —
        until the user commits with GREEN."""
        nonlocal picker, last_tap
        if picker is None or not (0 <= i < len(picker)):
            return
        c = picker[i]
        state.lines = parse_lrc(c["lrc"])
        state.lrc_raw = c["lrc"]
        state.lyrics_source = c["source"]
        state.lyrics_status = ""
        state.awaiting_feedback = True       # both buttons stay until GREEN
        picker = None
        # Swallow the trailing synthesized MOUSEBUTTONDOWN so it can't land on a
        # feedback button right after the pick (handle_tap honours last_tap).
        last_tap = time.monotonic()
        print(f"[picker] selected {c['source']} — {c['title']}")

    def check_swipe() -> None:
        nonlocal swipe_fired
        if swipe_fired or len(fingers) < 2:
            return
        moved = [f for f in fingers.values()
                 if abs(f["x"] - f["sx"]) >= swipe_min_px]
        if len(moved) < 2:
            return
        dirs = {1 if f["x"] - f["sx"] > 0 else -1 for f in moved}
        if len(dirs) == 1:   # both fingers travelled the same way → a swipe
            fire_swipe(dirs.pop())
            swipe_fired = True

    # ---- Settings panel actions --------------------------------------------
    def _settings_sizes() -> dict:
        return {"current": FONT_CURRENT, "top": FONT_TOP, "bottom": FONT_BOTTOM}

    def _apply_settings_size(key: str, size) -> None:
        """Live-preview a font-size slider drag (no file write until Done).
        Snaps to 5px steps so the slider lands on round sizes."""
        nonlocal prev_pos, curr_pos, next_pos
        global FONT_CURRENT, FONT_TOP, FONT_BOTTOM
        size = int(round(size / 5.0)) * 5
        size = int(min(SETTINGS_FONT_MAX, max(SETTINGS_FONT_MIN, size)))
        if key == "current":
            FONT_CURRENT = size
        elif key == "top":
            FONT_TOP = size
        else:
            FONT_BOTTOM = size
        prev_pos, curr_pos, next_pos = _line_positions(w, h)

    def _apply_settings_color(key: str, name: str) -> None:
        """Live-preview a colour swatch tap (no file write until Done)."""
        global CURRENT, PREV, NEXT
        rgb = COLOR_BY_NAME[name]
        if key == "current":
            CURRENT = rgb
        elif key == "top":
            PREV = rgb
        else:
            NEXT = rgb
        set_color_names[key] = name

    def _settings_bolds() -> dict:
        return {"current": CURRENT_BOLD, "top": TOP_BOLD, "bottom": BOTTOM_BOLD}

    def _apply_settings_bold(key: str) -> None:
        """Flip a line's Bold/Normal state (no file write until Done)."""
        global CURRENT_BOLD, TOP_BOLD, BOTTOM_BOLD
        if key == "current":
            CURRENT_BOLD = not CURRENT_BOLD
        elif key == "top":
            TOP_BOLD = not TOP_BOLD
        else:
            BOTTOM_BOLD = not BOTTOM_BOLD

    def open_menu() -> None:
        """Long-press landed: show the top-level menu."""
        nonlocal menu_screen, settings_armed, brightness_ui
        menu_screen = "main"
        settings_armed = False        # consume the opening hold's finger-up
        brightness_ui = False         # don't stack overlays
        print("[menu] opened (long press)")

    def save_settings() -> None:
        """Persist the font panel's edits to config.json and return to the main
        menu. We bump last_cfg_mtime to the just-written value so the hot-reload
        watcher doesn't re-apply (and log) the same change a beat later."""
        nonlocal menu_screen, last_cfg_mtime
        try:
            write_config_values({
                "font_current": FONT_CURRENT,
                "font_top": FONT_TOP,
                "font_bottom": FONT_BOTTOM,
                "current_color": set_color_names["current"],
                "top_color": set_color_names["top"],
                "bottom_color": set_color_names["bottom"],
                "current_bold": CURRENT_BOLD,
                "top_bold": TOP_BOLD,
                "bottom_bold": BOTTOM_BOLD,
            })
            last_cfg_mtime = _cfg_mtime()
            print(f"[settings] saved fonts={FONT_CURRENT}/{FONT_TOP}/{FONT_BOTTOM}"
                  f" colours={set_color_names['current']}/{set_color_names['top']}"
                  f"/{set_color_names['bottom']}"
                  f" bold={TOP_BOLD}/{CURRENT_BOLD}/{BOTTOM_BOLD}")
        except Exception as e:
            print(f"[settings] save failed: {e}")
        menu_screen = "main"

    def other_save() -> None:
        """Persist the Other Settings to config.json and bump last_cfg_mtime so
        the hot-reload watcher doesn't re-apply (and log) our own write."""
        nonlocal last_cfg_mtime
        try:
            write_config_values({
                "flip_180": FLIP_180,
                "latency_offset_ms": LATENCY_OFFSET_MS,
                "lead_offset_ms": LEAD_OFFSET_MS,
                "dim_enabled": DIM_ENABLED,
            })
            last_cfg_mtime = _cfg_mtime()
            print(f"[other] saved flip={FLIP_180} "
                  f"latency={LATENCY_OFFSET_MS}ms lead={LEAD_OFFSET_MS}ms "
                  f"dim={DIM_ENABLED}")
        except Exception as e:
            print(f"[other] save failed: {e}")

    def other_touch(lx: float, ly: float) -> None:
        """Tap handler for the Other Settings screen (tap-only): flip a toggle
        or ± a stepper, persist it, or return to the main menu via Back."""
        nonlocal menu_screen
        global FLIP_180, DIM_ENABLED, LATENCY_OFFSET_MS, LEAD_OFFSET_MS
        rows, back = _other_layout(w, h)
        if back.collidepoint(lx, ly):
            menu_screen = "main"
            return
        specs = {r[0]: r for r in OTHER_ROWS}
        for key, kind, ctrl in rows:
            if kind == "toggle":
                if ctrl["toggle"].collidepoint(lx, ly):
                    if key == "flip_180":
                        FLIP_180 = not FLIP_180
                    else:
                        DIM_ENABLED = not DIM_ENABLED
                    other_save()
                    return
                continue
            lo, hi, step = specs[key][3], specs[key][4], specs[key][5]
            cur = LATENCY_OFFSET_MS if key == "latency_offset_ms" else LEAD_OFFSET_MS
            if ctrl["minus"].collidepoint(lx, ly):
                new = max(lo, cur - step)
            elif ctrl["plus"].collidepoint(lx, ly):
                new = min(hi, cur + step)
            else:
                continue
            if key == "latency_offset_ms":
                LATENCY_OFFSET_MS = new
            else:
                LEAD_OFFSET_MS = new
            other_save()
            return

    def settings_touch(lx: float, ly: float, motion: bool) -> None:
        """Route a logical-coord touch on the font panel to a slider
        (drag-or-tap), a colour swatch (tap), a Bold/Normal toggle (tap), or the
        Done button (tap)."""
        sliders, swatches, bold_rects, done = _settings_layout(w, h)
        # Bold toggles are tap-only and take priority over the slider's wide hit
        # zone (they're in a separate right-hand column, but check first so a tap
        # there can never be stolen by the slider).
        if not motion:
            for key, brect in bold_rects.items():
                if brect.collidepoint(lx, ly):
                    _apply_settings_bold(key)
                    return
        for key, rect in sliders.items():
            # Thin track → generous vertical hit zone so it's easy to grab.
            if rect.inflate(0, max(40, rect.h * 4)).collidepoint(lx, ly):
                frac = (lx - rect.x) / rect.w if rect.w else 0.0
                _apply_settings_size(
                    key,
                    SETTINGS_FONT_MIN + frac
                    * (SETTINGS_FONT_MAX - SETTINGS_FONT_MIN))
                return
        if motion:
            return   # swatches + Done are taps, not drags
        for key, rects in swatches.items():
            for name, _rgb, srect in rects:
                if srect.collidepoint(lx, ly):
                    _apply_settings_color(key, name.lower())
                    return
        if done.collidepoint(lx, ly):
            save_settings()

    def menu_touch(lx: float, ly: float, motion: bool) -> None:
        """Dispatch a logical-coord touch to whichever menu screen is showing.
        Only the font panel cares about motion (slider drags); the others are
        tap-only. Bluetooth actions are async, so they're fired as tasks."""
        nonlocal menu_screen, last_menu_tap
        # Debounce discrete taps so the FINGERDOWN + synthesized MOUSEBUTTONDOWN
        # pair counts once (drags pass through — they must stay responsive).
        if not motion:
            now_t = time.monotonic()
            if now_t - last_menu_tap < 0.35:
                return
            last_menu_tap = now_t
        if menu_screen == "font":
            settings_touch(lx, ly, motion)
            return
        if motion:
            return
        if menu_screen == "main":
            for key, rect in _main_menu_layout(w, h):
                if not rect.collidepoint(lx, ly):
                    continue
                if key == "close":
                    menu_screen = None
                elif key == "font":
                    set_color_names["current"] = color_name_of(CURRENT)
                    set_color_names["top"] = color_name_of(PREV)
                    set_color_names["bottom"] = color_name_of(NEXT)
                    menu_screen = "font"
                elif key == "bluetooth":
                    menu_screen = "bluetooth"
                    asyncio.create_task(bt.open_screen())
                elif key == "other":
                    menu_screen = "other"
                elif key == "network":
                    menu_screen = "network"
                    asyncio.create_task(net.refresh())   # auto-check on open
                elif key == "version":
                    updater.reset()
                    menu_screen = "version"
                return
        elif menu_screen == "bluetooth":
            pair, back, rows, _lt = _bt_layout(w, h, len(bt.paired))
            if pair.collidepoint(lx, ly):
                asyncio.create_task(
                    bt.stop_pairing() if bt.pairing else bt.start_pairing())
                return
            for (_row, forget), (path, _n, _c) in zip(rows, bt.paired):
                if forget.collidepoint(lx, ly):
                    asyncio.create_task(bt.forget(path))
                    return
            if back.collidepoint(lx, ly):
                if bt.pairing:
                    asyncio.create_task(bt.stop_pairing())
                menu_screen = "main"
                return
        elif menu_screen == "version":
            update, back = _version_layout(w, h)
            if update.collidepoint(lx, ly):
                if updater.busy:
                    return
                if updater.armed:
                    asyncio.create_task(updater.run())   # confirmed → go
                else:
                    updater.armed = True                 # first tap arms
                    updater.status = "Pulls latest code from GitHub & restarts"
                return
            if back.collidepoint(lx, ly):
                updater.reset()
                menu_screen = "main"
        elif menu_screen == "network":
            check, back = _network_layout(w, h)
            if check.collidepoint(lx, ly):
                asyncio.create_task(net.refresh())   # re-check on demand
                return
            if back.collidepoint(lx, ly):
                menu_screen = "main"
        elif menu_screen == "other":
            other_touch(lx, ly)

    frame_interval = 1.0 / TARGET_FPS
    start_mono = time.monotonic()   # for the startup road-safety notice

    def _cfg_mtime() -> float:
        try:
            return CONFIG_PATH.stat().st_mtime
        except OSError:
            return 0.0

    last_cfg_mtime = _cfg_mtime()
    last_cfg_check = time.monotonic()

    try:
        while True:
            for event in pygame.event.get():
                # A real QUIT (window closed) or the Esc key exits cleanly. The
                # systemd unit uses Restart=on-failure, so a clean Esc exit STAYS
                # exited (no relaunch loop) — manage the Pi over SSH from there.
                # Every OTHER key is ignored, so a stray keypress on an attached
                # keyboard can't drop the kiosk (which Restart would relaunch,
                # stealing the screen back).
                if event.type == pygame.QUIT or (
                        event.type == pygame.KEYDOWN
                        and event.key == pygame.K_ESCAPE):
                    return
                if menu_screen is not None:
                    # Inside a menu, touches drive its buttons/sliders, not the
                    # lyric gestures. settings_armed stays False until the finger
                    # that opened the menu (the 10s hold) has lifted, so that
                    # hold's own finger-up can't land on a control.
                    if event.type == pygame.FINGERDOWN:
                        lx, ly = _logical_x(event.x), _logical_y(event.y)
                        fingers[event.finger_id] = {
                            "sx": lx, "sy": ly, "x": lx, "y": ly,
                            "t": time.monotonic()}
                        if settings_armed:
                            menu_touch(lx, ly, False)
                    elif event.type == pygame.FINGERMOTION:
                        if settings_armed:
                            menu_touch(_logical_x(event.x),
                                       _logical_y(event.y), True)
                    elif event.type == pygame.FINGERUP:
                        fingers.pop(event.finger_id, None)
                        if not fingers:
                            settings_armed = True
                    elif (event.type == pygame.MOUSEBUTTONDOWN
                          and event.button == 1 and settings_armed):
                        px, py = event.pos
                        if FLIP_180:
                            px, py = w - 1 - px, h - 1 - py
                        menu_touch(px, py, False)
                    continue
                if picker is not None:
                    # The candidate grid owns the screen: a tap on a cell selects
                    # that lyric. Debounced so the FINGERDOWN + synthesized
                    # MOUSEBUTTONDOWN pair counts once. All other events are
                    # swallowed so they can't leak into the lyric gestures.
                    # IMPORTANT: still drain FINGERUP from `fingers`, else a
                    # finger held through the RED tap stays stuck → it would trip
                    # the long-press (opening Settings) and wedge settings_armed.
                    if event.type == pygame.FINGERUP:
                        fingers.pop(event.finger_id, None)
                        continue
                    tap_xy = None
                    if event.type == pygame.FINGERDOWN:
                        tap_xy = (_logical_x(event.x), _logical_y(event.y))
                    elif (event.type == pygame.MOUSEBUTTONDOWN
                          and event.button == 1):
                        px, py = event.pos
                        if FLIP_180:
                            px, py = w - 1 - px, h - 1 - py
                        tap_xy = (px, py)
                    if tap_xy is not None:
                        now_t = time.monotonic()
                        if now_t - last_picker_tap >= 0.35:
                            last_picker_tap = now_t
                            for i, rect in enumerate(_picker_layout(w, h)):
                                if i < len(picker) and rect.collidepoint(*tap_xy):
                                    select_candidate(i)
                                    break
                    continue
                # Touch: SDL delivers FINGERDOWN (normalized 0..1 coords) and,
                # with touch-mouse emulation, a MOUSEBUTTONDOWN (pixel coords).
                # handle_tap debounces so a single tap fires once.
                if event.type == pygame.FINGERDOWN:
                    lx, ly = _logical_x(event.x), _logical_y(event.y)
                    fingers[event.finger_id] = {
                        "sx": lx, "sy": ly, "x": lx, "y": ly,
                        "t": time.monotonic()}
                    handle_tap(event.x * w, event.y * h)
                elif event.type == pygame.FINGERMOTION:
                    f = fingers.get(event.finger_id)
                    if f is not None:
                        f["x"] = _logical_x(event.x)
                        f["y"] = _logical_y(event.y)
                        check_swipe()
                        # One-finger vertical drag = brightness (only while the
                        # slider is open). event.dy is normalized; up = negative.
                        if brightness_ui and len(fingers) == 1:
                            dy = -event.dy if FLIP_180 else event.dy
                            user_brightness = min(1.0, max(
                                BRIGHTNESS_MIN,
                                user_brightness - dy * BRIGHTNESS_GAIN))
                elif event.type == pygame.FINGERUP:
                    f = fingers.pop(event.finger_id, None)
                    # Count consecutive quick, near-stationary taps: the 2nd
                    # toggles the brightness slider, the 3rd deletes the playing
                    # song's cached lyric. A gap longer than DOUBLE_TAP_S (or any
                    # non-tap gesture) restarts the count.
                    if f is not None:
                        dur = time.monotonic() - f["t"]
                        moved = max(abs(f["x"] - f["sx"]), abs(f["y"] - f["sy"]))
                        if dur <= TAP_MAX_DUR_S and moved <= tap_max_move_px:
                            now_t = time.monotonic()
                            if now_t - last_tap_time <= DOUBLE_TAP_S:
                                tap_count += 1
                            else:
                                tap_count = 1
                            last_tap_time = now_t
                            if tap_count == 2:
                                brightness_ui = not brightness_ui
                            elif tap_count >= 3:
                                # The 2nd tap already flipped the slider; undo
                                # that so a triple-tap leaves brightness as it
                                # was and only deletes.
                                brightness_ui = not brightness_ui
                                delete_current_lyrics()
                                tap_count = 0
                                last_tap_time = 0.0   # consume, no 4th-tap toggle
                        else:
                            tap_count = 0
                    if not fingers:        # gesture over → arm the next swipe
                        swipe_fired = False
                elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                    handle_tap(event.pos[0], event.pos[1])

            # Long-press: one finger held near-still for LONGPRESS_OPEN_S opens
            # the settings panel. (Two-finger swipes have their own gesture, so
            # we only arm this on a single stationary finger.) Suppressed while
            # the source picker is up or being gathered, so resting a finger on
            # the grid to choose can't pop the Settings menu.
            if (menu_screen is None and picker is None and not picker_searching
                    and len(fingers) == 1):
                f = next(iter(fingers.values()))
                if (max(abs(f["x"] - f["sx"]), abs(f["y"] - f["sy"]))
                        <= tap_max_move_px
                        and time.monotonic() - f["t"] >= LONGPRESS_OPEN_S):
                    open_menu()

            # A new song arrived while the picker was up → its candidates are
            # stale, so drop the grid and let the new track's lyrics show.
            if picker is not None and (state.title, state.artist) != picker_cache_sig:
                picker = None

            # Hot-reload config.json (≤1s after you save it) so offset / font
            # / dot tweaks take effect without a restart. Skipped while the
            # settings panel is open so its live previews aren't clobbered.
            now = time.monotonic()
            if menu_screen is None and now - last_cfg_check >= 1.0:
                last_cfg_check = now
                m = _cfg_mtime()
                if m != last_cfg_mtime:
                    last_cfg_mtime = m
                    apply_config()
                    frame_interval = 1.0 / TARGET_FPS
                    prev_pos, curr_pos, next_pos = _line_positions(w, h)
                    print(f"[config] reloaded: lead={LEAD_OFFSET_MS}ms "
                          f"latency={LATENCY_OFFSET_MS}ms "
                          f"fonts={FONT_CURRENT}/{FONT_TOP}/{FONT_BOTTOM} "
                          f"bold={TOP_BOLD}/{CURRENT_BOLD}/{BOTTOM_BOLD} "
                          f"prev={SHOW_PREV_LINE} "
                          f"bar={PROGRESS_BAR} dim={DIM_ENABLED} "
                          f"dots={INTRO_DOTS_MAX} fps={TARGET_FPS}")

            screen.fill(BG)

            safety_left = SAFETY_NOTICE_S - (time.monotonic() - start_mono)
            if safety_left > 0:
                # Road-safety notice takes the whole screen for its first few
                # seconds, ahead of menus/picker/lyrics.
                draw_safety(screen, w, h, safety_left)
            elif menu_screen == "main":
                draw_main_menu(screen, w, h)
            elif menu_screen == "font":
                # Font panel replaces the lyric frame entirely.
                draw_settings(screen, w, h, _settings_sizes(), set_color_names,
                              _settings_bolds())
            elif picker is not None:
                # RED-button candidate grid replaces the lyric frame.
                draw_picker(screen, w, h, picker)
            elif menu_screen == "bluetooth":
                draw_bluetooth(screen, w, h, bt)
            elif menu_screen == "other":
                draw_other(screen, w, h)
            elif menu_screen == "version":
                draw_version(screen, w, h, bt, updater)
            elif menu_screen == "network":
                draw_network(screen, w, h, net)
            else:
                # Night auto-dim × manual brightness, then max line width before
                # shrink-to-fit (both honour live config changes).
                bf = brightness_factor() * user_brightness
                c_current = scale_color(CURRENT, bf)
                c_next = scale_color(NEXT, bf)
                c_prev = scale_color(PREV, bf)
                along = h if ROTATION_DEG in (90, 270) else w
                max_len = int(along * MAX_LINE_WIDTH_FRAC)

                # Only scroll lyrics once we know an actual playback position
                # for the current track (avoids the "tick from 0 then catch up"
                # glitch when fetch outlasts a song-skip).
                if state.lines and state.position_known:
                    t_ms = state.now_ms()
                    idx = find_current_index(state.lines, t_ms)
                    if idx < 0:
                        # Intro: show the upcoming first line, with a dot
                        # countdown above (one dot per remaining second) so the
                        # singer knows when to come in.
                        remaining = state.lines[0].time_ms - t_ms
                        dots = max(0, min(INTRO_DOTS_MAX,
                                          (remaining + 999) // 1000))
                        if dots > 0:
                            draw_line(screen, " ".join(["●"] * dots), FONT_TOP,
                                      False, c_current, prev_pos, max_len,
                                      ROTATION_DEG)
                        draw_line(screen, state.lines[0].text, FONT_CURRENT,
                                  CURRENT_BOLD, c_current, curr_pos, max_len,
                                  ROTATION_DEG)
                    else:
                        if SHOW_PREV_LINE and idx - 1 >= 0:
                            draw_line(screen, state.lines[idx - 1].text,
                                      FONT_TOP, TOP_BOLD, c_prev, prev_pos,
                                      max_len, ROTATION_DEG)
                        draw_line(screen, state.lines[idx].text, FONT_CURRENT,
                                  CURRENT_BOLD, c_current, curr_pos, max_len,
                                  ROTATION_DEG)
                        if idx + 1 < len(state.lines):
                            draw_line(screen, state.lines[idx + 1].text,
                                      FONT_BOTTOM, BOTTOM_BOLD, c_next, next_pos,
                                      max_len, ROTATION_DEG)
                            if PROGRESS_BAR and ROTATION_DEG == 0:
                                _draw_progress(screen, w, curr_pos,
                                               state.lines, idx, t_ms, bf)
                else:
                    # No lyrics (yet) — show track meta with status centered.
                    if state.title:
                        draw_line(screen, state.artist or "", FONT_TOP, TOP_BOLD,
                                  c_prev, prev_pos, max_len, ROTATION_DEG)
                        draw_line(screen, state.title, FONT_BOTTOM, BOTTOM_BOLD,
                                  c_next, next_pos, max_len, ROTATION_DEG)
                        if state.lyrics_status:
                            draw_line(screen, state.lyrics_status, FONT_CURRENT,
                                      CURRENT_BOLD, c_current, curr_pos, max_len,
                                      ROTATION_DEG)
                    else:
                        # Idle (no track at all): a clock instead of a bare
                        # "(waiting for music)". Top = date MM/DD/YYYY,
                        # middle = 24h time HH:MM:SS rendered big (90px, the
                        # current-line size) so the clock is the focal point,
                        # bottom = the waiting note. strftime is re-evaluated
                        # every frame so the seconds tick live.
                        now_local = time.localtime()
                        draw_line(screen, time.strftime("%m/%d/%Y", now_local),
                                  FONT_TOP, TOP_BOLD, c_prev, prev_pos,
                                  max_len, ROTATION_DEG)
                        draw_line(screen, time.strftime("%H:%M:%S", now_local),
                                  90, CURRENT_BOLD, c_current,
                                  curr_pos, max_len, ROTATION_DEG)
                        draw_line(screen, "Waiting for Music",
                                  FONT_BOTTOM, BOTTOM_BOLD, c_next, next_pos,
                                  max_len, ROTATION_DEG)

                # Ask for a verdict on fresh (uncached) lyrics. Drawn before the
                # flip so the buttons orient with the lyrics.
                if state.awaiting_feedback and state.lines:
                    _draw_feedback_buttons(screen, green_rect, red_rect)

                # Brightness slider (double-tap to toggle). Drawn at full
                # intensity — NOT scaled by bf — so it stays visible even when
                # the screen is dimmed right down, to find it and turn it back up.
                if brightness_ui:
                    _draw_brightness_bar(screen, w, h, user_brightness, btn_w)

                # Live sync-nudge toast, pinned to the top edge (center). Drawn
                # before the flip so it rides the same orientation correction as
                # the lyrics — y is half the font height so it hugs the top.
                if toast_text and time.monotonic() < toast_until:
                    draw_line(screen, toast_text, FONT_SYNC, True,
                              scale_color((120, 200, 255), bf),
                              (w // 2, FONT_SYNC // 2 + 4), max_len, ROTATION_DEG)

                # Gathering candidates after a RED tap — banner near the bottom
                # so the user knows the grid is on its way.
                if picker_searching:
                    draw_line(screen, "♪ Searching all sources…", FONT_SYNC,
                              True, scale_color((120, 200, 255), bf),
                              (w // 2, h - FONT_SYNC), max_len, ROTATION_DEG)

            if FLIP_180:
                # Monitor is physically mounted upside-down — flip the whole
                # composed frame on both axes (= a 180° rotation) so it reads
                # right-side-up. Done last so the layout, progress bar and
                # shrink-to-fit logic all stay simple and upright.
                screen.blit(pygame.transform.flip(screen, True, True), (0, 0))

            pygame.display.flip()
            await asyncio.sleep(frame_interval)
    finally:
        pygame.quit()


async def main() -> None:
    apply_config()
    print(f"[config] lead={LEAD_OFFSET_MS}ms latency={LATENCY_OFFSET_MS}ms "
          f"fonts={FONT_CURRENT}/{FONT_TOP}/{FONT_BOTTOM} "
          f"bold={TOP_BOLD}/{CURRENT_BOLD}/{BOTTOM_BOLD} "
          f"prev={SHOW_PREV_LINE} bar={PROGRESS_BAR} dim={DIM_ENABLED} "
          f"dots={INTRO_DOTS_MAX} fps={TARGET_FPS}")
    state = State()
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    watcher = AvrcpWatcher(bus, state)
    await watcher.start()
    bt = BluetoothAdmin(bus)
    await bt.start()
    updater = FirmwareUpdater()
    await render_loop(state, watcher, bt, updater)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
