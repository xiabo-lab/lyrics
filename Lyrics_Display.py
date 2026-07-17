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
    fetch_best_lyrics,
    get_alias,
    save_to_cache,
    search_candidates,
    set_alias,
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
APP_VERSION = "1.9.5"

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
    "qqcrypto.py",   # QQ QRC (word-by-word) buggy-DES decryptor
    "bt-agent.service", "99-carlyric-ignore-avrcp-pointer.rules",
    "wifi.sh", "carlyric-claude.sudoers", "README.md", "LICENSE", ".gitignore",
    # Pinyin IME data table + its generator (Modify Search → 中 mode).
    "pinyin_table.json", "build_pinyin_table.py",
    # Assets: idle-clock fonts + picker source-badge icons. Kept top-level (no
    # subdir) so the OTA apply loop copies them even on older installs.
    "Aldrich-Regular.ttc", "advanced_led_board-7.ttc",
    "qq music icon.jpg", "kugou icon.jpg", "netease icon.png", "lrclib icon.png",
    # Stock backdrops for Settings → Background Picture. Subdir entries are fine
    # (the apply loop mkdirs parents). A user's own pictures dropped into image/
    # are left alone — OTA only overwrites these names.
    "image/Morning.png", "image/Afternoon.png", "image/Dark.png",
)
UPDATE_SERVICE = "carlyric.service"   # restarted to load the new code
# AVRCP "A/V Remote Control" profile. We connect THIS explicitly (not a
# plain Device1.Connect()) because a bare Connect() on a dual-mode iPhone
# often brings up only Bluetooth LE — which exposes no MediaPlayer1, so the
# app never sees a track. Connecting the AVRCP profile forces the classic
# BR/EDR control channel that BlueZ surfaces as MediaPlayer1.
AVRCP_UUID = "0000110e-0000-1000-8000-00805f9b34fb"

# ---- Display --------------------------------------------------------------
BG = (0, 0, 0)   # menus/picker/panels always paint on this, so controls read
# Backdrop of the LYRIC screen (and the idle clock) only — never the menus, so
# a photo can't make the settings controls unreadable. Either a flat colour or
# a picture from image/. Live values come from background_* in config.json,
# editable on-screen via Settings → Background Picture.
BACKGROUND_MODE = "solid"          # "solid" | "picture"
BACKGROUND_COLOR = (0, 0, 0)       # one of BG_SOLID_COLORS (solid mode)
BACKGROUND_IMAGE = ""              # filename inside image/; "" = first found
BACKGROUND_SLIDESHOW = False       # picture mode: cycle through image/
BACKGROUND_SLIDESHOW_S = 60        # seconds per picture when the slideshow runs
BACKGROUND_SLIDESHOW_MIN_S = 5
BACKGROUND_SLIDESHOW_MAX_S = 600
# The three flat backdrops. Deliberately NOT SETTING_COLORS: those are text
# colours (all bright, to read against black), which is the opposite of what a
# backdrop needs.
BG_SOLID_COLORS = (
    ("Black", (0, 0, 0)),
    ("White", (235, 235, 235)),
    ("Grey", (110, 110, 110)),
)
BG_COLOR_BY_NAME = {name.lower(): rgb for name, rgb in BG_SOLID_COLORS}
# Pictures live here, so a user can drop their own in beside the shipped three.
IMAGE_DIR = INSTALL_DIR / "image"
BG_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".bmp")
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
# Square geometric face (Google "Aldrich") used ONLY for the idle clock's big
# time readout. Bundled at the repo root (from the xiabo-lab/pi_dashboard repo)
# — kept top-level so OTA can ship it. get_font falls back to FONT_PATH if it's
# missing. Alt LED face `advanced_led_board-7.ttc` also ships alongside.
CLOCK_FONT_PATH = str(INSTALL_DIR / "Aldrich-Regular.ttc")
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
# Karaoke fill: colour the current line left→right in time with the line, so the
# text "fills" as it's sung (approximated from the line's own duration — plain
# LRC has no per-word times). When on, it REPLACES the progress bar.
KARAOKE_SYNC = True
KARAOKE_COLOR = (235, 235, 235)   # sung-portion colour (White); unsung = CURRENT
DIM_ENABLED = True        # auto-dim the whole display at night
DAY_START_HOUR = 7        # local hour daytime (full) brightness begins
NIGHT_START_HOUR = 19     # local hour night (dimmed) brightness begins
NIGHT_BRIGHTNESS = 0.55   # 0..1 multiplier applied to all colours at night
MAX_LINE_WIDTH_FRAC = 0.9  # shrink any line wider than this frac of the screen
AUTOCONNECT = True   # on boot/disconnect, have the Pi reconnect to the paired phone

# When two songs IN A ROW get no lyrics AND the public internet is actually
# unreachable, connectivity is genuinely down — auto-recover it: power-cycle
# the USB 5G modem (IK511) if that's how we're online, or reboot the Pi if
# we're on Wi-Fi. See ConnectivityRecovery. Set False to disable entirely.
AUTO_RECOVER = True

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
    "auto_recover": AUTO_RECOVER,
    "karaoke_sync": KARAOKE_SYNC,
    "karaoke_color": color_name_of(KARAOKE_COLOR),
    "background_mode": BACKGROUND_MODE,
    "background_color": "black",
    "background_image": BACKGROUND_IMAGE,
    "background_slideshow": BACKGROUND_SLIDESHOW,
    "background_slideshow_s": BACKGROUND_SLIDESHOW_S,
}

# String config keys that are NOT colours. Without these, load_config would
# validate every string against the colour palette and throw them out.
_ENUM_KEYS = {
    "background_mode": {"solid", "picture"},
    "background_color": set(BG_COLOR_BY_NAME),
}
# Free-form strings (a filename we can't validate here — the picture may be
# added to image/ later, and a missing one falls back to the solid colour).
_FREE_STRING_KEYS = {"background_image"}


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
            # String keys are NOT all colours: background_mode/background_color
            # are small enums and background_image is a free-form filename, so
            # validate by key rather than assuming the colour palette.
            if not isinstance(val, str):
                print(f"[config] ignoring non-string {key}={val!r}")
            elif key in _ENUM_KEYS:
                if val.lower() in _ENUM_KEYS[key]:
                    cfg[key] = val.lower()
                else:
                    print(f"[config] ignoring invalid {key}={val!r} "
                          f"(expected one of {sorted(_ENUM_KEYS[key])})")
            elif key in _FREE_STRING_KEYS:
                cfg[key] = val
            elif val.lower() in COLOR_BY_NAME:
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
    global AUTO_RECOVER, KARAOKE_SYNC, KARAOKE_COLOR
    global BACKGROUND_MODE, BACKGROUND_COLOR, BACKGROUND_IMAGE
    global BACKGROUND_SLIDESHOW, BACKGROUND_SLIDESHOW_S
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
    AUTO_RECOVER = cfg["auto_recover"]
    KARAOKE_SYNC = cfg["karaoke_sync"]
    KARAOKE_COLOR = COLOR_BY_NAME[cfg["karaoke_color"]]
    BACKGROUND_MODE = cfg["background_mode"]
    BACKGROUND_COLOR = BG_COLOR_BY_NAME[cfg["background_color"]]
    BACKGROUND_IMAGE = cfg["background_image"]
    BACKGROUND_SLIDESHOW = cfg["background_slideshow"]
    BACKGROUND_SLIDESHOW_S = max(BACKGROUND_SLIDESHOW_MIN_S,
                                 min(BACKGROUND_SLIDESHOW_MAX_S,
                                     cfg["background_slideshow_s"]))


def bg_color_name_of(rgb) -> str:
    """Backdrop rgb → its palette name; falls back to the first (Black)."""
    for name, c in BG_SOLID_COLORS:
        if tuple(c) == tuple(rgb):
            return name.lower()
    return BG_SOLID_COLORS[0][0].lower()


def _active_background_image() -> str:
    """The picture to show: the configured one, else the first in image/.

    Falling back to the first means picking "Picture" mode does something
    sensible before a file has ever been chosen, and that a configured picture
    which has since been deleted doesn't leave a blank screen."""
    images = list_background_images()
    if BACKGROUND_IMAGE in images:
        return BACKGROUND_IMAGE
    return images[0] if images else ""


def list_background_images() -> list[str]:
    """Filenames of the pictures in image/, case-insensitively sorted.

    Read fresh (not cached) so dropping a picture into the folder shows up in
    the picker without a restart. A missing/unreadable folder is not an error —
    the background just falls back to a solid colour."""
    try:
        return sorted((p.name for p in IMAGE_DIR.iterdir()
                       if p.is_file() and p.suffix.lower() in BG_IMAGE_EXTS),
                      key=str.lower)
    except OSError:
        return []


# Scaled backdrops, keyed by (filename, w, h). Scaling a ~2MB PNG takes long
# enough to drop frames, and the result is identical every frame, so do it once.
# Also holds the small picker thumbnails (same call, tile-sized).
_bg_surface_cache: dict = {}


def _scale_cover(surf, w: int, h: int):
    """Scale `surf` to COVER w×h, centre-cropping the overflow.

    Fills the screen without distorting, at the cost of cropping whatever
    doesn't fit — the usual backdrop behaviour, and why the screen tells users
    to supply images at the panel's own aspect ratio."""
    sw, sh = surf.get_size()
    if sw <= 0 or sh <= 0:
        return None
    factor = max(w / sw, h / sh)
    tw, th = max(1, round(sw * factor)), max(1, round(sh * factor))
    scaled = pygame.transform.smoothscale(surf, (tw, th))
    out = pygame.Surface((w, h))
    out.blit(scaled, ((w - tw) // 2, (h - th) // 2))
    return out


def background_surface(name: str, w: int, h: int):
    """image/<name> scaled to cover w×h, or None if it can't be used.

    Failures (missing file, corrupt image) are cached as None so a broken
    picture costs one log line, not a decode attempt every frame."""
    key = (name, w, h)
    if key in _bg_surface_cache:
        return _bg_surface_cache[key]
    surf = None
    if name:
        try:
            raw = pygame.image.load(str(IMAGE_DIR / name)).convert()
            surf = _scale_cover(raw, w, h)
        except Exception as e:
            print(f"[background] cannot load {name}: {e}")
    _bg_surface_cache[key] = surf
    return surf


def paint_lyric_background(screen, w: int, h: int, name: str) -> None:
    """Paint the lyric screen's backdrop: a picture, else the solid colour.

    Anything that stops the picture working — mode is solid, image/ is empty,
    the file vanished — lands on the solid colour rather than a black void."""
    if BACKGROUND_MODE == "picture":
        surf = background_surface(name, w, h)
        if surf is not None:
            screen.blit(surf, (0, 0))
            return
    screen.fill(BACKGROUND_COLOR)


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
        # The UNMODIFIED title/artist the phone last reported over AVRCP. When a
        # user renames via the picker's Modify Search, title/artist above hold
        # the corrected (display) name, while these keep the phone's original so
        # the alias can be keyed by what the phone will report again next play.
        self.raw_title: str = ""
        self.raw_artist: str = ""
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
        # Every candidate the initial sweep collected, kept so the RED picker
        # opens instantly instead of re-searching all four sources. candidates
        # is the grid list; candidates_sig is the (title, artist) it belongs to
        # — a mismatch (or None, e.g. after a cache hit, where no sweep ran)
        # means RED must go to the network.
        self.candidates: list = []
        self.candidates_sig: tuple[str, str] | None = None
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


# Friendly names for the on-screen "Searching …" status, keyed by the labels
# lyric_sources reports through its progress callback. Unknown names fall back
# to themselves.
SOURCE_LABELS = {"all sources": "lyrics", "QQ": "QQ Music",
                 "Kugou": "Kugou", "NetEase": "NetEase", "LRCLIB": "LRCLIB"}


async def fetch_lyrics_for(state: State, title: str, artist: str) -> None:
    """requests is blocking → run in a thread so dbus + render keep flowing.

    One sweep of all four sources picks the best-scoring match to display AND
    fills state.candidates, so a later RED tap opens the picker with no network
    round-trip at all."""
    print(f"[lyrics] fetching: {artist} — {title}")
    loop = asyncio.get_running_loop()

    def on_source(name: str) -> None:
        # Called from the fetch worker thread when the sweep starts. Hop back to
        # the event loop to update the display text, honouring State's
        # single-thread rule (no cross-thread attribute writes).
        label = SOURCE_LABELS.get(name, name)
        loop.call_soon_threadsafe(
            setattr, state, "lyrics_status", f"♪ Searching {label}…")

    try:
        lrc, source, cands = await asyncio.to_thread(
            fetch_best_lyrics, title, artist, on_source)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        print(f"[lyrics] error: {e}")
        state.lines = []
        state.lyrics_status = "(network error)"
        state.awaiting_feedback = False
        state.candidates, state.candidates_sig = [], None
        RECOVERY.record_result(title, artist, found=False)
        return

    # cands is None on a cache hit — no sweep ran, so drop whatever the previous
    # song left behind rather than let the picker show someone else's results.
    if cands is None:
        state.candidates, state.candidates_sig = [], None
    else:
        state.candidates, state.candidates_sig = cands, (title, artist)
        print(f"[lyrics] {len(cands)} candidate(s) held for the picker")

    if not lrc:
        state.lines = []
        state.lyrics_status = "♪ Lyrics not found"
        state.lrc_raw = ""
        state.lyrics_source = None
        state.awaiting_feedback = False
        print("[lyrics] none found")
        RECOVERY.record_result(title, artist, found=False)
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
    RECOVERY.record_result(title, artist, found=True)


class AvrcpWatcher:
    # Poll Position every this-many seconds. BlueZ does NOT hand back a frozen
    # cache between broadcasts: it extrapolates Position from its own timer, so
    # every read advances (measured on BlueZ 5.82: 13533, 13634, 13735… at 100ms
    # intervals). Polling therefore agrees with our own extrapolation in the
    # steady state; its job is to re-adopt BlueZ's clock whenever the phone
    # broadcasts a fresh value and the PropertiesChanged signal got missed.
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
    # Right after a track change the phone's Position reports queue up behind
    # its track-metadata burst and land ~1s stale, each one staler than the last
    # (measured, iPhone/YouTube Music: 198ms, then 305ms 690ms later — i.e. a
    # clock that walks BACKWARD). Inside this window we refuse to let one of
    # those drag our clock back. See _startup_backstep.
    STARTUP_GUARD_S = 3.0
    # A start-up report has to undercut our clock by more than this to count as
    # stale rather than ordinary jitter.
    STARTUP_BACKSTEP_MS = 250
    # How far a polled Position must depart from BlueZ's own extrapolation
    # before we read it as a fresh phone broadcast rather than BlueZ ticking.
    REBROADCAST_MS = 150
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
        # Last raw Position value we read from BlueZ, and when we read it.
        # Together they reconstruct BlueZ's own extrapolation, so the poller can
        # tell a fresh phone broadcast (value departs from that line) from BlueZ
        # simply ticking its timer.
        self._last_polled_ms: int | None = None
        self._last_poll_at: float | None = None
        # How far our clock sits AHEAD of BlueZ's, in ms. Non-zero only after we
        # reject a stale start-up report: BlueZ re-anchors its timer to every
        # value the phone sends, including the ones we refuse, so from then on
        # its Position reads low by a fixed amount. Cleared on a track change and
        # whenever the phone broadcasts something we do trust.
        self._bluez_skew_ms: int = 0
        # monotonic() at the last track change — gates the fresh-start
        # stabilize timeout (see _try_lock) and the start-up backstep guard.
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

            # Locked. Work out whether BlueZ re-anchored since the last poll
            # (the phone broadcast a fresh Position) or is just ticking its own
            # timer, by comparing against the line BlueZ was already on.
            now = time.monotonic()
            if self._last_polled_ms is not None and self._last_poll_at is not None:
                bluez_tick = self._last_polled_ms + int(
                    (now - self._last_poll_at) * 1000)
                if abs(real_ms - bluez_tick) > self.REBROADCAST_MS:
                    if self._startup_backstep(real_ms, now):
                        self._note_startup_backstep(real_ms, now)
                        self._last_polled_ms, self._last_poll_at = real_ms, now
                        continue
                    # A broadcast we trust (play/pause, seek, or the phone
                    # correcting itself after buffering) — BlueZ is authoritative
                    # again, so stop carrying any skew.
                    self._bluez_skew_ms = 0
            self._last_polled_ms, self._last_poll_at = real_ms, now

            corrected = real_ms + self._bluez_skew_ms
            expected = self.state.position_ms + int(
                (now - self.state.position_at_mono) * 1000
            )
            delta = corrected - expected
            self.state.set_position(corrected)
            if abs(delta) > self.SEEK_LOG_THRESHOLD_MS:
                print(f"[seek]     snap: expected {expected}ms, real {corrected}ms (Δ {delta:+d}ms)")

    def _clock_ms(self, now: float) -> int:
        """Our own extrapolated playback clock, without the display offsets."""
        return self.state.position_ms + int(
            (now - self.state.position_at_mono) * 1000)

    def _startup_backstep(self, real_ms: int, now: float) -> bool:
        """Is real_ms one of the phone's stale track-start reports?

        See decide_startup_backstep for the policy and its rationale.
        """
        elapsed = (
            None if self._track_changed_at is None
            else now - self._track_changed_at
        )
        return decide_startup_backstep(
            real_ms, self._clock_ms(now), elapsed,
            self.STARTUP_GUARD_S, self.STARTUP_BACKSTEP_MS)

    def _note_startup_backstep(self, real_ms: int, now: float) -> None:
        """Keep our clock, and record how far BlueZ just fell behind it.

        BlueZ re-anchors its timer to every value the phone sends — including
        the stale ones we reject — so from here its Position reads low by a
        fixed amount. Holding that skew lets the poller keep re-adopting BlueZ
        without walking us back onto the stale anchor we just refused.
        """
        clock = self._clock_ms(now)
        self._bluez_skew_ms = clock - real_ms
        print(f"[sync]     ignoring stale start-up position {real_ms}ms "
              f"(clock {clock}ms, BlueZ skew {self._bluez_skew_ms:+d}ms)")

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
        self._last_polled_ms, self._last_poll_at = real_ms, time.monotonic()
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
                # Dedup on the phone's RAW report (last_sig above), but display
                # + fetch under any user-set correction so a renamed song stays
                # renamed on every future play and hits its cache.
                self.state.raw_title, self.state.raw_artist = title, artist
                alias = get_alias(title, artist)
                if alias:
                    title, artist = alias["title"], alias["artist"]
                    print(f"[alias] {sig} → {artist} — {title}")
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
                self._last_poll_at = None
                self._bluez_skew_ms = 0
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
                now = time.monotonic()
                # Locked. A PropertiesChanged Position is a real broadcast, but
                # during the track-start burst it is a LATE one — later reports
                # are staler than the first, so taking them makes the anchor
                # worse, not better.
                if self._startup_backstep(pos, now):
                    self._note_startup_backstep(pos, now)
                    self._last_polled_ms, self._last_poll_at = pos, now
                    return
                # Outside that burst the phone is telling the truth (play/pause,
                # seek, a correction after buffering) → take it, drop any skew.
                self._bluez_skew_ms = 0
                self.state.set_position(pos)
                self._last_polled_ms, self._last_poll_at = pos, now


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
                dst.parent.mkdir(parents=True, exist_ok=True)  # support subdir entries
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


class ConnectivityRecovery:
    """Auto-recover the car's internet when it silently dies mid-drive.

    The Pi gets online either over Wi-Fi (home, phone hotspot) or through a TCL
    LINKPORT IK511 5G modem plugged in as a USB network adapter. Outdoors the
    IK511 hangs at random and stops passing traffic; the only reliable fix is to
    power-cycle its USB port. We can't read the modem's state, so we infer an
    outage from a behavioural signal the display already produces: lyrics for a
    *new* song are always fetched over the network, so when two songs IN A ROW
    come up empty, the internet is a prime suspect.

    To avoid acting on a false alarm (two genuinely obscure songs on healthy
    Wi-Fi at home), we confirm with a real reachability probe before doing
    anything. Only if the internet is actually unreachable do we recover:
      - online via the USB modem → power-cycle just that USB port
      - online via Wi-Fi         → reboot the Pi (re-kicks NetworkManager + radio)

    Failures are counted per DISTINCT song, so a RED-button re-search of the
    same track can't inflate the streak, and a cooldown stops us thrashing the
    modem/reboot if the outage persists. Recovery runs in a worker thread so the
    render loop never stalls. All of this is gated by the AUTO_RECOVER config."""

    FAIL_THRESHOLD = 2        # consecutive lyric-less songs before we investigate
    COOLDOWN_S = 90.0         # min seconds between recovery actions (anti-thrash)
    REBIND_SETTLE_S = 2.0     # pause between USB unbind and rebind

    def __init__(self):
        self._fail_streak = 0
        self._last_sig: tuple | None = None   # last DISTINCT song we counted
        self._busy = False                    # a recovery is already scheduled/running
        self._last_action_mono = 0.0          # monotonic time of the last action

    def record_result(self, title: str, artist: str, found: bool) -> None:
        """Call at the end of every lyric fetch. `found` is True when lyrics were
        shown (cache OR network), False on 'not found' / network error. A cache
        hit counts as found — if some songs still resolve, the internet isn't the
        problem, so the streak resets."""
        if not AUTO_RECOVER:
            return
        sig = (title, artist)
        if found:
            self._fail_streak = 0
            self._last_sig = sig
            return
        if sig == self._last_sig:
            return                      # same song re-searched — don't double-count
        self._last_sig = sig
        self._fail_streak += 1
        print(f"[recover] lyric-less song "
              f"{self._fail_streak}/{self.FAIL_THRESHOLD}: {artist} — {title}")
        if self._fail_streak >= self.FAIL_THRESHOLD and not self._busy:
            self._fail_streak = 0       # reset now; next action needs a fresh streak
            self._busy = True
            asyncio.create_task(self._recover())

    async def _recover(self) -> None:
        """Run the blocking recovery off the event loop so rendering keeps up."""
        try:
            await asyncio.to_thread(self._recover_blocking)
        except Exception as e:
            print(f"[recover] error: {e}")
        finally:
            self._busy = False

    def _recover_blocking(self) -> None:
        now = time.monotonic()
        if now - self._last_action_mono < self.COOLDOWN_S:
            print("[recover] within cooldown — skipping")
            return
        # Confirm the outage: two empty songs on WORKING internet just means the
        # lyrics genuinely aren't out there, and no reboot will conjure them.
        if NetworkStatus._probe():
            print("[recover] internet reachable — lyrics simply missing, no action")
            return
        self._last_action_mono = now
        mode = self._decide_mode()
        print(f"[recover] internet unreachable, connected via {mode} — recovering")
        if mode == "wifi":
            self._reboot()
        else:
            self._power_cycle_usb()

    # ---- connection-type detection ----------------------------------------
    @classmethod
    def _decide_mode(cls) -> str:
        """'wifi' or 'usb' — how the Pi is (was) reaching the internet."""
        iface = cls._default_route_iface()
        if iface:
            # wlan0 / wlp* are Wi-Fi; usb0 / eth1 (the IK511) are the USB modem.
            return "wifi" if iface.startswith("wl") else "usb"
        # No default route — typical once the link has dropped. If we're still
        # associated to a Wi-Fi SSID it's a Wi-Fi problem; otherwise the modem.
        return "wifi" if NetworkStatus._ssid() else "usb"

    @staticmethod
    def _default_route_iface() -> str | None:
        """Interface carrying the default route, e.g. 'wlan0' / 'usb0', or None."""
        try:
            out = subprocess.run(["ip", "route", "show", "default"],
                                 capture_output=True, text=True,
                                 timeout=4).stdout
        except Exception:
            return None
        for line in out.splitlines():
            parts = line.split()
            if "dev" in parts:
                return parts[parts.index("dev") + 1]
        return None

    # ---- Wi-Fi recovery ----------------------------------------------------
    @staticmethod
    def _reboot() -> None:
        """Reboot the Pi. Runs as root under systemd, so plain systemctl works;
        fall back to sudo -n, then /sbin/reboot for a non-root/odd launch."""
        last = None
        for cmd in (["/usr/bin/systemctl", "reboot"],
                    ["sudo", "-n", "/usr/bin/systemctl", "reboot"],
                    ["/sbin/reboot"]):
            try:
                subprocess.run(cmd, check=True, timeout=15)
                print("[recover] reboot requested")
                return
            except Exception as e:
                last = e
        print(f"[recover] reboot failed ({last})")

    # ---- USB-modem recovery ------------------------------------------------
    @classmethod
    def _power_cycle_usb(cls) -> None:
        """Power-cycle just the IK511's USB port. Prefers a true VBUS cycle via
        uhubctl (if installed); otherwise forces a driver re-enumeration by
        unbinding/rebinding the device — which clears most modem hangs without
        cutting power. Only ever touches the modem's own port, never the
        touchscreen or anything else."""
        busid = cls._modem_busid()
        if not busid:
            print("[recover] USB modem port not found — rebooting as last resort")
            cls._reboot()
            return
        print(f"[recover] power-cycling USB modem at {busid}")
        # uhubctl ships in /usr/sbin, which a restricted systemd/cage PATH may
        # omit — so fall back to the absolute path rather than miss the real
        # VBUS power cycle and silently drop to rebind.
        uhubctl = shutil.which("uhubctl")
        if not uhubctl and os.path.exists("/usr/sbin/uhubctl"):
            uhubctl = "/usr/sbin/uhubctl"
        if uhubctl and cls._uhubctl_cycle(uhubctl, busid):
            return
        cls._rebind_usb(busid)

    @classmethod
    def _modem_busid(cls) -> str | None:
        """USB bus-id (e.g. '1-1.2') of the modem's port. Prefer the interface
        that holds the default route; fall back to any USB-backed, non-Wi-Fi
        network interface (the modem, when its route has already dropped)."""
        candidates: list[str] = []
        drt = cls._default_route_iface()
        if drt and not drt.startswith("wl"):
            candidates.append(drt)
        try:
            for name in sorted(os.listdir("/sys/class/net")):
                if name == "lo" or name.startswith("wl") or name in candidates:
                    continue
                candidates.append(name)
        except OSError:
            pass
        for iface in candidates:
            busid = cls._usb_busid_for_iface(iface)
            if busid:
                return busid
        return None

    @staticmethod
    def _usb_busid_for_iface(iface: str) -> str | None:
        """Resolve a network interface to the bus-id of its backing USB device,
        or None if it isn't USB-backed."""
        try:
            dev = os.path.realpath(f"/sys/class/net/{iface}/device")
        except OSError:
            return None
        if "/usb" not in dev:
            return None
        # Walk up to the USB *device* directory (the one carrying idVendor);
        # its basename is the bus-id the usb driver / uhubctl expect.
        path = dev
        for _ in range(8):
            if os.path.exists(os.path.join(path, "idVendor")):
                return os.path.basename(path)
            parent = os.path.dirname(path)
            if parent == path:
                break
            path = parent
        return None

    @staticmethod
    def _uhubctl_cycle(uhubctl: str, busid: str) -> bool:
        """True on a successful uhubctl power cycle of the port carrying `busid`.
        The port number is the trailing segment of the bus-id ('1-1.2' → hub
        '1-1', port '2'); a root-hub port like '1-1' → hub '1', port '1'."""
        sep = "." if "." in busid else "-"
        loc, _, port = busid.rpartition(sep)
        if not loc or not port:
            return False
        try:
            subprocess.run(
                [uhubctl, "-l", loc, "-p", port, "-a", "cycle", "-d", "2"],
                check=True, timeout=30, capture_output=True, text=True)
            print(f"[recover] uhubctl cycled hub {loc} port {port}")
            return True
        except Exception as e:
            print(f"[recover] uhubctl failed ({e}) — falling back to rebind")
            return False

    @classmethod
    def _rebind_usb(cls, busid: str) -> None:
        """Force a USB re-enumeration by unbinding then rebinding the device's
        driver. Reloads the modem's CDC/RNDIS driver and usually clears a hung
        IK511 without needing VBUS switching. Reboots as a last resort if the
        sysfs write isn't permitted."""
        base = "/sys/bus/usb/drivers/usb"
        try:
            with open(f"{base}/unbind", "w") as f:
                f.write(busid)
            time.sleep(cls.REBIND_SETTLE_S)
            with open(f"{base}/bind", "w") as f:
                f.write(busid)
            print(f"[recover] re-enumerated USB device {busid}")
        except OSError as e:
            print(f"[recover] USB rebind failed ({e}) — rebooting as last resort")
            cls._reboot()


# Module-wide singleton, updated from fetch_lyrics_for after every fetch.
RECOVERY = ConnectivityRecovery()


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


def decide_startup_backstep(real_ms, clock_ms, elapsed_s, guard_s, tol_ms):
    """Pure policy: is a just-received Position a stale track-start report that
    we should refuse rather than anchor to?

    Right after a track change the phone's Position reports queue up behind its
    track-metadata burst and land ~1s late, each one staler than the last —
    measured on an iPhone running YouTube Music: locked at 198ms, then told
    305ms 690ms later, i.e. a clock walking BACKWARD by ~580ms. Adopting one
    pins the lyrics that far behind the music for the WHOLE song, because
    YouTube Music broadcasts nothing else until the user pauses/plays.

    A song plays forward, so only a seek can legitimately move the clock back,
    and only inside the start-up window do we override that: a seek in the first
    seconds of a track is rare, and self-corrects on the next broadcast.

    Kept free of BlueZ/pygame so it's unit-testable, like decide_lock.
    """
    if elapsed_s is None or elapsed_s > guard_s:
        return False
    return real_ms < clock_ms - tol_ms


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


def get_font(size: int, bold: bool, font_path: str = FONT_PATH):
    """Memoized font loader. Bold is synthesized via set_bold() so we don't
    depend on a separate bold .ttc being installed. font_path selects a
    non-default face (e.g. the LED clock font); a missing or unreadable file
    silently falls back to FONT_PATH so the display never blanks."""
    key = (font_path, size, bold)
    font = _FONT_CACHE.get(key)
    if font is None:
        try:
            font = pygame.font.Font(font_path, size)
        except (OSError, RuntimeError, pygame.error):
            if font_path != FONT_PATH:
                print(f"[font] {font_path} unusable — using default")
            font = pygame.font.Font(FONT_PATH, size)
        font.set_bold(bold)
        _FONT_CACHE[key] = font
    return font


# Per-source badge images (in the repo root) shown in each picker result cell so
# you can tell at a glance which service a candidate came from. Keyed by the
# candidate "source" values lyric_sources emits.
SOURCE_ICON_FILES = {
    "QQ":     "qq music icon.jpg",
    "Kugou":  "kugou icon.jpg",
    "NetEase": "netease icon.png",
    "LRCLIB": "lrclib icon.png",
}
_ICON_CACHE: dict = {}   # (source, size) -> Surface | None


def get_source_icon(source, size: int):
    """Memoized loader for a source badge scaled to size×size px. Returns None
    (drawn as nothing) if the source is unknown or its file is missing/unreadable
    — a missing icon must never break the picker."""
    key = (source, size)
    if key in _ICON_CACHE:
        return _ICON_CACHE[key]
    icon = None
    fname = SOURCE_ICON_FILES.get(source)
    if fname:
        try:
            img = pygame.image.load(str(INSTALL_DIR / fname)).convert_alpha()
            icon = pygame.transform.smoothscale(img, (size, size))
        except (OSError, pygame.error) as e:
            print(f"[icon] {fname} load failed: {e}")
    _ICON_CACHE[key] = icon
    return icon


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


def draw_line(screen, text, size, bold, color, center_xy, max_len, rotate_deg,
              font_path=FONT_PATH):
    """Render one centered line, shrinking the font if the text would exceed
    max_len px — so long lines never clip (one quick read per line). font_path
    picks a non-default face (e.g. the LED clock font)."""
    if not text:
        return
    surf = get_font(size, bold, font_path).render(text, True, color)
    if max_len and surf.get_width() > max_len:
        shrunk = max(8, int(size * max_len / surf.get_width()))
        if shrunk < size:
            surf = get_font(shrunk, bold, font_path).render(text, True, color)
    if rotate_deg:
        surf = pygame.transform.rotate(surf, rotate_deg)
    screen.blit(surf, surf.get_rect(center=center_xy))


def _karaoke_split_px(font, words, t_ms, line_end_ms, total_w):
    """Pixel x at which the sung/unsung boundary sits, from per-word timing.
    Walks the words measuring cumulative text width (accurate for proportional
    fonts), interpolating within the word currently being sung. Before the first
    word → 0; after the last → full width."""
    prefix = ""
    for i, w in enumerate(words):
        w_end = words[i + 1].time_ms if i + 1 < len(words) else line_end_ms
        x0 = font.size(prefix)[0]
        prefix_after = prefix + w.text
        if t_ms < w.time_ms:
            return x0                                  # this word not reached yet
        if w_end is not None and t_ms < w_end:
            x1 = font.size(prefix_after)[0]
            span = w_end - w.time_ms
            wf = (t_ms - w.time_ms) / span if span > 0 else 1.0
            return int(x0 + wf * (x1 - x0))            # mid-word
        prefix = prefix_after                          # word fully sung
    return total_w


def draw_karaoke_line(screen, text, size, bold, base_color, sung_color,
                      words, t_ms, line_start_ms, line_end_ms,
                      center_xy, max_len, rotate_deg, font_path=FONT_PATH):
    """Like draw_line, but with a KARAOKE fill: the sung part of the line is
    drawn in sung_color, the rest in base_color, split at a vertical edge that
    sweeps left→right as the song plays.

    With per-word timing (`words` from enhanced-LRC/KRC/QRC) the edge tracks the
    actual word being sung; with none it interpolates across the whole line from
    line_start_ms→line_end_ms. Same shrink-to-fit + centering as draw_line; the
    composited line is rotated as a whole so the fill stays correct when the
    panel is rotated."""
    if not text:
        return
    font = get_font(size, bold, font_path)
    if max_len and font.size(text)[0] > max_len:
        shrunk = max(8, int(size * max_len / font.size(text)[0]))
        if shrunk < size:
            font = get_font(shrunk, bold, font_path)
    base = font.render(text, True, base_color)
    total_w = base.get_width()
    if words:
        split = _karaoke_split_px(font, words, t_ms, line_end_ms, total_w)
    else:
        span = line_end_ms - line_start_ms
        frac = (t_ms - line_start_ms) / span if span and span > 0 else 1.0
        frac = 0.0 if frac < 0 else (1.0 if frac > 1 else frac)
        split = int(total_w * frac)
    combo = base.copy()
    if split > 0:
        # Paint the sung colour over just the left `split` px — a glyph straddling
        # the edge is filled part-way, which reads as a word being sung.
        sung = font.render(text, True, sung_color)
        combo.blit(sung, (0, 0), area=pygame.Rect(0, 0, split, base.get_height()))
    if rotate_deg:
        combo = pygame.transform.rotate(combo, rotate_deg)
    screen.blit(combo, combo.get_rect(center=center_xy))


def draw_clock(screen, segments, size, bold, center_xy, rotate_deg, font_path):
    """Render a clock as coloured segments — `segments` is a list of (text, rgb),
    e.g. HH red / ':' white / MM yellow / ':' white / SS green — on a MONOSPACED
    grid so the time never drifts as digits change.

    Every digit gets the same cell width (the widest of 0-9) and is centered in
    it, so a proportional face like Aldrich (where '1' is narrower than '8')
    still keeps each digit and colon pinned to a fixed spot — only the glyph
    swaps. Colons keep their own natural cell width.

    Horizontal placement uses the grid's FIXED box (constant width → the clock
    never moves). Vertical placement uses the glyph INK (get_bounding_rect), not
    the font's padded line box, so uneven top/bottom padding still gives equal
    space above and below. (rotate_deg is 0 for the clock's monitor.)"""
    font = get_font(size, bold, font_path)
    digit_w = max(font.size(str(d))[0] for d in range(10))
    chars = [(ch, col) for text, col in segments for ch in text]
    if not chars:
        return

    def cell_w(ch):
        return digit_w if ch.isdigit() else font.size(ch)[0]

    total_w = sum(cell_w(ch) for ch, _ in chars)
    strip = pygame.Surface((total_w, font.get_height()), pygame.SRCALPHA)
    x = 0
    for ch, col in chars:
        cw = cell_w(ch)
        g = font.render(ch, True, col)
        strip.blit(g, (x + (cw - g.get_width()) // 2, 0))  # centre glyph in its cell
        x += cw
    if rotate_deg:
        strip = pygame.transform.rotate(strip, rotate_deg)
    ink = strip.get_bounding_rect()
    cx, cy = center_xy
    screen.blit(strip, (cx - strip.get_width() // 2, cy - ink.centery))


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


# Settled "no lyrics" outcomes where we still offer the RED bar (→ picker →
# Modify Search) so the user can hand-search a song the automatic lookup missed.
# Deliberately excludes the transient "Searching…/(fetching…)" states.
NO_LYRIC_STATUSES = ("♪ Lyrics not found", "(network error)")


def _draw_feedback_buttons(screen, green_rect, red_rect, green=True):
    """Translucent edge bars asking 'are these lyrics right?': a green ✓ strip
    on the left, a red ✗ strip on the right. With green=False only the red ✗
    bar is drawn — used when there are no lyrics to confirm but the user should
    still be able to open the picker / Modify Search. Drawn BEFORE the FLIP_180
    flip so they ride the same orientation correction as the lyrics (taps are
    inverted to match — see render_loop). Kept to the edges so centered lyrics
    show through on the wide bar display."""
    fills = [(red_rect, (200, 0, 0, 120))]
    if green:
        fills.insert(0, (green_rect, (0, 160, 0, 120)))
    for rect, fill in fills:
        bar = pygame.Surface((rect.w, rect.h), pygame.SRCALPHA)
        bar.fill(fill)
        screen.blit(bar, rect.topleft)
    # Symbols sized/thickened off the (narrow) bar width, in solid WHITE for
    # contrast — a same-hue tint on a coloured bar was washing them out.
    s = max(14, red_rect.w // 3)
    lw = max(8, red_rect.w // 8)
    white = (255, 255, 255)
    if green:
        # Green check mark.
        gx, gy = green_rect.center
        pygame.draw.lines(screen, white, False,
                          [(gx - s, gy), (gx - s // 3, gy + s), (gx + s, gy - s)],
                          lw)
    # Red cross.
    rx, ry = red_rect.center
    pygame.draw.line(screen, white, (rx - s, ry - s), (rx + s, ry + s), lw)
    pygame.draw.line(screen, white, (rx - s, ry + s), (rx + s, ry - s), lw)


# ---- Source picker (RED button → 3x3 candidate grid) -----------------------
# 8 result cells (0-7) + the lower-right cell (8) is the "Modify Search" button
# that opens the edit-and-re-search screen.
MODIFY_SEARCH_CELL = 8


def _picker_layout(w: int, h: int):
    """A fixed 3x3 grid of candidate cells, row-major, in LOGICAL (pre-FLIP_180)
    pixels. Shared by the draw pass and the touch hit-test so they never drift.
    Cells 0-7 hold candidates; cell 8 (lower-right) is the Modify Search button.
    Empty result cells are drawn faint when there are fewer than 8 candidates."""
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


_LRC_TS_RE = re.compile(r"\[(\d+):(\d+)(?:[.:]\d+)?\]")


def _lrc_duration_ms(lrc: str) -> int:
    """The lyric's time span = its largest [mm:ss] timestamp, in ms (0 if none).
    Metadata tags like [ti:...]/[offset:0] never match — they aren't mm:ss."""
    best = 0
    for m in _LRC_TS_RE.finditer(lrc or ""):
        ms = int(m.group(1)) * 60000 + int(m.group(2)) * 1000
        if ms > best:
            best = ms
    return best


def _fmt_mmss(ms: int) -> str:
    s = ms // 1000
    return f"{s // 60}:{s % 60:02d}"


def draw_picker(screen, w, h, candidates) -> None:
    """The consolidated multi-source candidate grid (RED button). Each filled
    cell is a tappable lyric option showing the song title (big) and artist
    (smaller) centred so it reads from across the car, with a small source badge
    in the lower-right corner and the lyric's time length in the lower-left;
    empty cells (when fewer than 9 matched) are dimmed. Drawn before the FLIP_180
    flip, like the menus (taps inverted to match)."""
    screen.fill(BG)
    cells = _picker_layout(w, h)
    cell_h = cells[0].h if cells else h // 3
    # Fonts scale off the cell height so the title fills the freed space; the
    # min/max clamps keep it sane on very short or very tall panels.
    title_font = get_font(max(26, min(60, int(cell_h * 0.36))), True)
    sub_font = get_font(max(20, min(46, int(cell_h * 0.27))), False)
    gap = max(4, cell_h // 16)
    for i, rect in enumerate(cells):
        if i == MODIFY_SEARCH_CELL:
            # Lower-right cell: the Modify Search button (always shown).
            pygame.draw.rect(screen, (40, 90, 140), rect, border_radius=12)
            l1 = sub_font.render("Modify", True, (255, 255, 255))
            l2 = sub_font.render("Search", True, (255, 255, 255))
            block_h = l1.get_height() + gap + l2.get_height()
            y0 = rect.y + (rect.h - block_h) // 2
            screen.blit(l1, l1.get_rect(midtop=(rect.centerx, y0)))
            screen.blit(l2, l2.get_rect(
                midtop=(rect.centerx, y0 + l1.get_height() + gap)))
            continue
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
        # Small source badge tucked into the cell's lower-right corner so you can
        # see which service each candidate came from at a glance.
        icon = get_source_icon(c.get("source"), max(18, int(cell_h * 0.24)))
        pad = max(2, rect.w // 80)       # small inset → tucked into the corner
        if icon:
            screen.blit(icon, icon.get_rect(
                bottomright=(rect.right - pad, rect.bottom - pad)))
        # Lyric time length (span of the synced LRC) in the lower-LEFT corner, so
        # you can sanity-check a candidate against how long the song runs.
        # Cached on the candidate dict — the LRC doesn't change while the picker
        # is open, so we parse it once, not every frame.
        dur = c.get("_dur_ms")
        if dur is None:
            dur = c["_dur_ms"] = _lrc_duration_ms(c.get("lrc", ""))
        if dur > 0:
            dur_font = get_font(max(16, int(cell_h * 0.16)), False)
            ds = dur_font.render(_fmt_mmss(dur), True, (150, 155, 165))
            screen.blit(ds, ds.get_rect(
                bottomleft=(rect.left + pad, rect.bottom - pad)))


# ---- Pinyin IME (Modify Search → 中 mode) ----------------------------------
class PinyinIME:
    """Offline pinyin→Hanzi conversion for the Modify Search keyboard.

    Loads `pinyin_table.json` (built by build_pinyin_table.py, shipped via OTA)
    once, lazily. Given a raw pinyin buffer it returns tappable candidates as
    (hanzi, consume_len) — consume_len is how many buffer letters that choice
    eats, so a whole-word pick clears the buffer while a single-char pick eats
    just its leading syllable. Everything is dict lookups + a greedy syllable
    segmentation; no network, no heavy deps."""

    TABLE = INSTALL_DIR / "pinyin_table.json"
    MAX_SYL = 6            # longest pinyin syllable (e.g. "zhuang", "shuang")

    def __init__(self):
        self._loaded = False
        self.chars: dict = {}      # syllable → ranked Hanzi string
        self.words: dict = {}      # concatenated pinyin → [words]
        self._syls: set = set()

    def _ensure(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            data = json.loads(self.TABLE.read_text(encoding="utf-8"))
            self.chars = data.get("chars", {})
            self.words = data.get("words", {})
            self._syls = set(self.chars)
            print(f"[ime] loaded {len(self.chars)} syllables, "
                  f"{len(self.words)} word keys")
        except Exception as e:
            print(f"[ime] table load failed: {e}")

    def available(self) -> bool:
        self._ensure()
        return bool(self.chars)

    def segment(self, comp: str) -> list[str]:
        """Greedy longest-match split of a pinyin buffer into syllables; an
        unrecognised letter becomes its own 1-char segment."""
        self._ensure()
        out: list[str] = []
        i, n = 0, len(comp)
        while i < n:
            hit = None
            for j in range(min(self.MAX_SYL, n - i), 0, -1):
                if comp[i:i + j] in self._syls:
                    hit = comp[i:i + j]
                    break
            if hit:
                out.append(hit)
                i += len(hit)
            else:
                out.append(comp[i])
                i += 1
        return out

    def candidates(self, comp: str, limit: int = 9) -> list[tuple[str, int]]:
        """Ranked (hanzi, consume_len) for a pinyin buffer: whole-buffer words
        first, then leading multi-syllable words, then single chars for the
        first syllable."""
        self._ensure()
        out: list[tuple[str, int]] = []
        seen: set = set()

        def add(h: str, consume: int) -> None:
            if h and (h, consume) not in seen:
                seen.add((h, consume))
                out.append((h, consume))

        for word in self.words.get(comp, []):
            add(word, len(comp))
        segs = self.segment(comp)
        for nseg in range(len(segs), 1, -1):          # longest leading group first
            key = "".join(segs[:nseg])
            if key == comp:
                continue
            for word in self.words.get(key, []):
                add(word, len(key))
        if segs and segs[0] in self._syls:
            for ch in self.chars.get(segs[0], ""):
                add(ch, len(segs[0]))
        return out[:limit]


_PINYIN = PinyinIME()


# ---- Modify Search editor (RED grid → Modify Search) -----------------------
# On-screen QWERTY to correct a garbled/wrong artist/song and re-search. A 中/A
# key toggles a pinyin→Hanzi IME (Phase 2) that surfaces a candidate strip. Left
# column shows the two editable fields + Search + Back; the keyboard fills right.
class SearchEditor:
    """Editing state for the Modify Search screen.

    Holds the working artist/title text, which field is active (tapped), and a
    one-shot Shift. orig_sig is the phone's ORIGINAL (raw) artist/title so a
    Search can persist the correction as an alias keyed to what the phone will
    report next play."""

    def __init__(self, title: str, artist: str, orig_sig: tuple):
        self.title = title
        self.artist = artist
        self.orig_sig = orig_sig     # (raw_title, raw_artist)
        self.active = "artist"       # which field the keyboard types into
        self.shift = False
        self.cursor = len(artist)    # caret index within the active field
        self.pinyin_mode = False     # 中 mode: letters compose Hanzi
        self.comp = ""               # raw pinyin buffer while composing
        self.cand_page = 0           # current candidate page (paged strip)

    def _get(self) -> str:
        return self.title if self.active == "title" else self.artist

    def _set(self, v: str) -> None:
        if self.active == "title":
            self.title = v
        else:
            self.artist = v

    def _set_comp(self, v: str) -> None:
        """Set the pinyin buffer and reset paging (candidates changed)."""
        self.comp = v
        self.cand_page = 0

    def set_active(self, field: str) -> None:
        """Focus a field (from a tap) and drop the caret at its end. Any pending
        pinyin composition is discarded."""
        self.active = field
        self.cursor = len(self._get())
        self._set_comp("")

    def insert_text(self, s: str) -> None:
        """Insert s at the caret and advance the caret past it."""
        cur = self._get()
        i = max(0, min(self.cursor, len(cur)))
        self._set(cur[:i] + s + cur[i:])
        self.cursor = i + len(s)

    def select_candidate(self, hanzi: str, consume: int) -> None:
        """Commit a chosen candidate: insert it and drop the pinyin it used."""
        self.insert_text(hanzi)
        self._set_comp(self.comp[consume:])

    def commit_first(self) -> None:
        """Commit the top candidate (Space in 中 mode); if none, drop the raw
        pinyin in as-is so the buffer never gets stuck."""
        cands = _PINYIN.candidates(self.comp)
        if cands:
            self.select_candidate(*cands[0])
        else:
            self.insert_text(self.comp)
            self._set_comp("")

    def apply_key(self, action: str) -> None:
        """action: a single character to insert at the caret, or 'toggle' /
        'shift' / 'left' / 'right' / 'space' / 'back' / 'clear'. In 中 mode,
        letters build a pinyin buffer and digits 1-9 pick a candidate."""
        if action == "toggle":
            if not self.pinyin_mode and not _PINYIN.available():
                return                       # no table → stay in Latin mode
            self.pinyin_mode = not self.pinyin_mode
            self._set_comp("")
            self.shift = False
            return
        # ---- 中 mode: intercept keys that drive composition ------------------
        if self.pinyin_mode:
            if len(action) == 1 and action.isalpha():
                self._set_comp(self.comp + action.lower())
                return
            if self.comp:
                # Composing: keys act on the buffer, not the field. Candidates
                # are chosen by TAP (« » page through them), so number/arrow
                # keys are swallowed here rather than leaking into the field.
                if action == "space":
                    self.commit_first()
                elif action == "back":
                    self._set_comp(self.comp[:-1])
                elif action == "clear":
                    self._set_comp("")
                return
            # else: buffer empty → fall through to normal field editing
        # ---- Latin field editing --------------------------------------------
        if action == "shift":
            self.shift = not self.shift
            return
        cur = self._get()
        i = max(0, min(self.cursor, len(cur)))   # clamp (field may have changed)
        if action == "left":
            self.cursor = max(0, i - 1)
            return
        if action == "right":
            self.cursor = min(len(cur), i + 1)
            return
        if action == "back":
            if i > 0:
                self._set(cur[:i - 1] + cur[i:])
                self.cursor = i - 1
        elif action == "clear":
            self._set("")
            self.cursor = 0
        elif action == "space":
            self._set(cur[:i] + " " + cur[i:])
            self.cursor = i + 1
        elif len(action) == 1:
            ch = action.upper() if self.shift else action
            self._set(cur[:i] + ch + cur[i:])
            self.cursor = i + 1
            self.shift = False       # Shift is one-shot, like a phone keyboard


# Keyboard rows (lowercase; Shift renders/inserts uppercase). Bottom row is a
# set of (label, action, width-units) specials.
_KB_ROWS = ("1234567890", "qwertyuiop", "asdfghjkl", "zxcvbnm")
# "中/A" label is replaced at draw time by the live mode (中 / A).
_KB_BOTTOM = (("中/A", "toggle", 1.7), ("Shift", "shift", 1.4),
              ("←", "left", 1.0), ("→", "right", 1.0), ("Space", "space", 2.6),
              ("Del", "back", 1.3), ("Clear", "clear", 1.5))


def _editor_keys(box: "pygame.Rect"):
    """Build [(label, rect, action), ...] for the keyboard inside box."""
    keys = []
    n_rows = len(_KB_ROWS) + 1
    gy = max(4, box.h // 40)
    kh = (box.h - (n_rows - 1) * gy) // n_rows
    y = box.y
    for row in _KB_ROWS:
        n = len(row)
        gx = max(4, box.w // 60)
        kw = (box.w - (n - 1) * gx) // n
        row_w = n * kw + (n - 1) * gx
        x = box.x + (box.w - row_w) // 2      # centre the row
        for ch in row:
            keys.append((ch, pygame.Rect(x, y, kw, kh), ch))
            x += kw + gx
        y += kh + gy
    # Bottom row: proportional widths from the width-units.
    gx = max(4, box.w // 60)
    total_u = sum(u for _l, _a, u in _KB_BOTTOM)
    avail = box.w - (len(_KB_BOTTOM) - 1) * gx
    x = box.x
    for label, action, u in _KB_BOTTOM:
        kw = int(avail * (u / total_u))
        keys.append((label, pygame.Rect(x, y, kw, kh), action))
        x += kw + gx
    return keys


def _editor_layout(w: int, h: int):
    """Geometry for the Modify Search screen, LOGICAL (pre-FLIP_180) px.
    Returns {"back","artist","title","search","compbar","keys"}. The compbar is
    a strip above the keyboard that holds the pinyin buffer + candidates in 中
    mode; it's always reserved so the keyboard never jumps when toggling."""
    mx = max(16, int(w * 0.03))
    my = max(12, int(h * 0.05))
    lw = int(w * 0.40)                        # left column width
    back = pygame.Rect(mx, my, int(lw * 0.5), int(h * 0.11))
    field_h = int(h * 0.17)
    gap = max(10, int(h * 0.035))
    artist = pygame.Rect(mx, back.bottom + gap, lw, field_h)
    title = pygame.Rect(mx, artist.bottom + gap, lw, field_h)
    search = pygame.Rect(mx, title.bottom + gap, lw, int(h * 0.15))
    kx = mx + lw + max(16, int(w * 0.03))
    kw = w - kx - mx
    kh_total = h - 2 * my
    comp_h = int(kh_total * 0.13)
    compbar = pygame.Rect(kx, my, kw, comp_h)
    kb = pygame.Rect(kx, my + comp_h + max(6, int(h * 0.012)),
                     kw, kh_total - comp_h - max(6, int(h * 0.012)))
    return {"back": back, "artist": artist, "title": title, "search": search,
            "compbar": compbar, "keys": _editor_keys(kb)}


def _ime_layout(compbar: "pygame.Rect", comp: str, page: int = 0):
    """Shared by draw + hit-test. Given the compbar, the pinyin buffer, and a
    page index, return (font, label, cand_rects, prev_rect, next_rect, n_pages,
    page). label is the raw pinyin (syllables joined by '); cand_rects are the
    tappable candidates for THIS page; prev/next_rect are the « » page buttons
    (drawn only when n_pages > 1). Candidates pack left-to-right; when they'd
    overflow the row a new page begins, so every candidate is reachable."""
    f = get_font(max(16, min(34, int(compbar.h * 0.58))), True)
    pad = max(6, compbar.h // 6)
    gap = max(4, pad // 2)
    y, kh = compbar.y + 3, compbar.h - 6
    segs = _PINYIN.segment(comp) if comp else []
    label = "'".join(segs) if segs else comp
    label_w = (f.size(label)[0] + 2 * pad) if comp else 0
    cands = _PINYIN.candidates(comp, limit=200) if comp else []
    nav_w = f.size("«")[0] + 2 * pad
    left = compbar.x + pad + label_w + pad
    # Always reserve the two nav buttons on the right so the row width — and
    # thus the pagination — is stable whether or not nav ends up shown.
    region_w = max(10, (compbar.right - pad - 2 * (nav_w + gap)) - left)
    # Pack candidates greedily into pages.
    pages, i, n = [], 0, len(cands)
    while i < n:
        x, start = 0, i
        while i < n:
            cw = f.size(cands[i][0])[0] + 2 * pad
            need = cw if i == start else cw + gap
            if x + need > region_w and i > start:
                break
            x += need
            i += 1
        pages.append((start, i))
    n_pages = max(1, len(pages))
    pg = max(0, min(page, n_pages - 1))
    rects = []
    if pages:
        s, e = pages[pg]
        x = left
        for k in range(s, e):
            hanzi, consume = cands[k]
            cw = f.size(hanzi)[0] + 2 * pad
            rects.append((hanzi, consume, pygame.Rect(x, y, cw, kh)))
            x += cw + gap
    prev_rect = pygame.Rect(compbar.right - pad - 2 * nav_w - gap, y, nav_w, kh)
    next_rect = pygame.Rect(compbar.right - pad - nav_w, y, nav_w, kh)
    return f, label, rects, prev_rect, next_rect, n_pages, pg


def draw_search_editor(screen, w, h, ed: "SearchEditor") -> None:
    """Left: Artist/Song fields (tap to focus, active one outlined + cursor),
    Search, Back. Right: QWERTY keyboard. Drawn before the FLIP_180 flip."""
    screen.fill(BG)
    lay = _editor_layout(w, h)
    cap_font = get_font(max(13, min(24, h // 32)), False)
    field_font = get_font(max(20, min(40, h // 16)), True)
    btn_font = get_font(max(16, min(30, h // 22)), True)
    key_font = get_font(max(16, min(34, h // 20)), True)

    # Back
    pygame.draw.rect(screen, (45, 45, 58), lay["back"], border_radius=10)
    bl = btn_font.render("Back", True, (235, 235, 235))
    screen.blit(bl, bl.get_rect(center=lay["back"].center))

    # Fields
    for fld, rect, text, cap in (("artist", lay["artist"], ed.artist, "Artist"),
                                 ("title", lay["title"], ed.title, "Song")):
        active = ed.active == fld
        pygame.draw.rect(screen, (60, 60, 82) if active else (40, 40, 52),
                         rect, border_radius=10)
        pygame.draw.rect(screen, (120, 170, 240) if active else (70, 70, 86),
                         rect, width=max(2, h // 320), border_radius=10)
        cs = cap_font.render(cap, True, (150, 150, 165))
        screen.blit(cs, (rect.x + 12, rect.y + 6))
        # Show a caret at the cursor position (active field only) by inserting a
        # literal '|' there, so ◀/▶ visibly move it within the text.
        if active:
            i = max(0, min(ed.cursor, len(text)))
            text = text[:i] + "|" + text[i:]
        shown = _fit_text(text, field_font, rect.w - 28)
        ts = field_font.render(shown, True, (245, 245, 245))
        screen.blit(ts, (rect.x + 14,
                         rect.centery - ts.get_height() // 2 + cs.get_height() // 2))

    # Search (below the Song field). Song name is required; artist is optional
    # so the user can search by title alone.
    ready = bool(ed.title.strip())
    pygame.draw.rect(screen, (40, 120, 50) if ready else (45, 55, 46),
                     lay["search"], border_radius=12)
    ss = btn_font.render("Search", True,
                         (255, 255, 255) if ready else (150, 160, 150))
    screen.blit(ss, ss.get_rect(center=lay["search"].center))

    # Composition bar (中 mode): raw pinyin on the left, tappable candidates
    # after it. Drawn faint when idle so the reserved space reads as part of the
    # keyboard rather than a gap.
    cb = lay["compbar"]
    if ed.pinyin_mode:
        pygame.draw.rect(screen, (38, 38, 50), cb, border_radius=8)
        f, label, cands, prev_r, next_r, n_pages, pg = _ime_layout(
            cb, ed.comp, ed.cand_page)
        pad = max(6, cb.h // 6)
        if ed.comp:
            ps = f.render(label, True, (255, 220, 120))
            screen.blit(ps, (cb.x + pad, cb.centery - ps.get_height() // 2))
            for hanzi, _consume, rect in cands:
                pygame.draw.rect(screen, (60, 60, 78), rect, border_radius=6)
                hs = f.render(hanzi, True, (245, 245, 245))
                screen.blit(hs, hs.get_rect(center=rect.center))
            if n_pages > 1:                    # « page » nav + "p/N" indicator
                for r, glyph in ((prev_r, "«"), (next_r, "»")):
                    pygame.draw.rect(screen, (70, 70, 90), r, border_radius=6)
                    gs = f.render(glyph, True, (235, 235, 235))
                    screen.blit(gs, gs.get_rect(center=r.center))
                idx = get_font(max(11, min(20, cb.h // 5)), False).render(
                    f"{pg + 1}/{n_pages}", True, (150, 150, 165))
                screen.blit(idx, idx.get_rect(
                    midbottom=(prev_r.left - pad, prev_r.bottom)))

    # Keyboard
    for label, rect, action in lay["keys"]:
        lit = (action == "shift" and ed.shift) or (
            action == "toggle" and ed.pinyin_mode)
        pygame.draw.rect(screen, (95, 95, 60) if lit else (55, 55, 70),
                         rect, border_radius=8)
        if action == "toggle":
            disp = "中" if ed.pinyin_mode else "A"
        elif len(action) == 1 and action.isalpha():
            disp = action.upper() if ed.shift else action
        else:
            disp = label
        kl = key_font.render(disp, True, (235, 235, 235))
        screen.blit(kl, kl.get_rect(center=rect.center))


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
    ("background", "Background Picture"),
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


# ---- Background screen -----------------------------------------------------
# How many picture tiles fit on one page before we paginate. The panel is wide
# but short, so tiles go in a single row.
BG_TILES_PER_PAGE = 5


def _background_layout(w: int, h: int, mode: str, n_images: int, page: int):
    """Geometry for the Background Picture screen, in LOGICAL (pre-flip) px.

    Returns (rows, choices, slide, nav, back):
      rows    = {"mode": Rect, "choice": Rect, "slide": Rect}  — label strips
      choices = [(value, Rect), ...] — colour names (solid) or filenames
      slide   = {"toggle","minus","plus","value"} Rects, or None in solid mode
      nav     = {"prev": Rect, "next": Rect} or None when everything fits
      back    = Rect

    The three row bands are fixed regardless of mode, so switching Solid↔Picture
    doesn't make the controls jump around under the user's finger.
    """
    margin_x = max(20, int(w * 0.05))
    top = int(h * 0.06)
    back_h = max(44, int(h * 0.15))
    bottom_pad = int(h * 0.03)
    notice_h = max(22, int(h * 0.09))
    gap = max(8, int(h * 0.02))
    avail = h - top - back_h - bottom_pad - notice_h - gap
    row_h = max(44, (avail - 2 * gap) // 3)

    label_w = int(w * 0.17)
    ctrl_x = margin_x + label_w
    ctrl_w = w - margin_x - ctrl_x

    rows, y = {}, top
    for name in ("mode", "choice", "slide"):
        rows[name] = pygame.Rect(margin_x, y, w - 2 * margin_x, row_h)
        y += row_h + gap

    def _btn_row(rect, values, count):
        """Evenly split `rect`'s control column into `count` buttons."""
        bgap = max(8, int(ctrl_w * 0.015))
        bw = (ctrl_w - (count - 1) * bgap) // max(1, count)
        bh = min(rect.h - 6, int(rect.h * 0.86))
        by = rect.y + (rect.h - bh) // 2
        return [(v, pygame.Rect(ctrl_x + i * (bw + bgap), by, bw, bh))
                for i, v in enumerate(values)]

    # --- mode row: Solid | Picture
    mode_rects = _btn_row(rows["mode"], ("solid", "picture"), 2)

    # --- choice row: three flat colours, or one page of picture tiles
    nav = None
    if mode == "solid":
        choices = _btn_row(rows["choice"],
                           [n.lower() for n, _rgb in BG_SOLID_COLORS], 3)
    else:
        images = list_background_images()
        n_images = len(images)
        pages = max(1, (n_images + BG_TILES_PER_PAGE - 1) // BG_TILES_PER_PAGE)
        page = max(0, min(page, pages - 1))
        shown = images[page * BG_TILES_PER_PAGE:(page + 1) * BG_TILES_PER_PAGE]
        crect = rows["choice"]
        if pages > 1:
            # Reserve arrows at both ends, tiles share what's left.
            aw = max(36, int(ctrl_w * 0.05))
            nav = {"prev": pygame.Rect(ctrl_x, crect.y, aw, crect.h),
                   "next": pygame.Rect(w - margin_x - aw, crect.y, aw, crect.h)}
            tiles_x = ctrl_x + aw + gap
            tiles_w = (w - margin_x - aw - gap) - tiles_x
        else:
            tiles_x, tiles_w = ctrl_x, ctrl_w
        choices = []
        if shown:
            tgap = max(8, int(tiles_w * 0.015))
            tw = (tiles_w - (len(shown) - 1) * tgap) // len(shown)
            for i, nm in enumerate(shown):
                choices.append((nm, pygame.Rect(tiles_x + i * (tw + tgap),
                                                crect.y, tw, crect.h)))

    # --- slideshow row: Yes/No + interval stepper (pictures only)
    slide = None
    if mode == "picture":
        srect = rows["slide"]
        bh = min(srect.h - 6, int(srect.h * 0.86))
        by = srect.y + (srect.h - bh) // 2
        tw = int(ctrl_w * 0.22)
        toggle = pygame.Rect(ctrl_x, by, tw, bh)
        # Keep −/value/+ grouped next to the toggle instead of spanning the
        # control column: this panel is 1920px wide, so pinning + to the far
        # edge would leave the two halves of one stepper ~1000px apart, reading
        # as unrelated buttons.
        btn = min(bh, max(44, int(ctrl_w * 0.07)))
        val_w = max(90, int(ctrl_w * 0.10))
        minus = pygame.Rect(ctrl_x + tw + gap * 3, by, btn, bh)
        value = pygame.Rect(minus.right + gap, by, val_w, bh)
        plus = pygame.Rect(value.right + gap, by, btn, bh)
        slide = {"toggle": toggle, "minus": minus, "plus": plus, "value": value}

    margin_b = max(20, int(w * 0.12))
    back = pygame.Rect(margin_b, h - back_h - bottom_pad, w - 2 * margin_b,
                       back_h)
    return rows, mode_rects, choices, slide, nav, back


def draw_background(screen, w, h, page: int) -> None:
    """Render the Background Picture screen. Reads the live globals."""
    screen.fill(BG)
    label_font = get_font(max(16, min(32, h // 22)), True)
    btn_font = get_font(max(16, min(34, h // 20)), True)
    note_font = get_font(max(14, min(24, h // 28)), False)
    rows, mode_rects, choices, slide, nav, back = _background_layout(
        w, h, BACKGROUND_MODE, len(list_background_images()), page)

    def _label(rect, text, dim=False):
        s = label_font.render(text, True, (120, 120, 130) if dim
                              else (235, 235, 235))
        screen.blit(s, (rect.x + 4, rect.centery - s.get_height() // 2))

    # Mode row.
    _label(rows["mode"], "Background")
    for value, r in mode_rects:
        on = BACKGROUND_MODE == value
        pygame.draw.rect(screen, (40, 120, 50) if on else (45, 45, 58), r,
                         border_radius=12)
        s = btn_font.render("Solid Colour" if value == "solid" else "Picture",
                            True, (255, 255, 255) if on else (200, 200, 200))
        screen.blit(s, s.get_rect(center=r.center))

    # Choice row.
    if BACKGROUND_MODE == "solid":
        _label(rows["choice"], "Colour")
        for value, r in choices:
            pygame.draw.rect(screen, BG_COLOR_BY_NAME[value], r,
                             border_radius=10)
            # Black on black needs an outline to be visible at all.
            pygame.draw.rect(screen, (90, 90, 100), r, 2, border_radius=10)
            if bg_color_name_of(BACKGROUND_COLOR) == value:
                pygame.draw.rect(screen, (255, 255, 255), r.inflate(8, 8), 3,
                                 border_radius=13)
            txt = value.capitalize()
            s = btn_font.render(txt, True, (0, 0, 0) if value == "white"
                                else (235, 235, 235))
            screen.blit(s, s.get_rect(center=r.center))
    else:
        _label(rows["choice"], "Picture")
        if not choices:
            s = note_font.render(
                f"No pictures in image/ — copy {w} x {h} images there.",
                True, (255, 170, 90))
            screen.blit(s, (rows["choice"].x + int(w * 0.17),
                            rows["choice"].centery - s.get_height() // 2))
        for value, r in choices:
            thumb = background_surface(value, r.w, r.h)
            if thumb is not None:
                screen.blit(thumb, r.topleft)
            else:
                pygame.draw.rect(screen, (60, 60, 70), r, border_radius=10)
            # Caption on a dark strip so it reads over any picture.
            cap = note_font.render(Path(value).stem, True, (245, 245, 245))
            strip = pygame.Rect(r.x, r.bottom - cap.get_height() - 6, r.w,
                                cap.get_height() + 6)
            shade = pygame.Surface((strip.w, strip.h))
            shade.set_alpha(150)
            shade.fill((0, 0, 0))
            screen.blit(shade, strip.topleft)
            screen.blit(cap, cap.get_rect(center=strip.center))
            if value == _active_background_image():
                pygame.draw.rect(screen, (255, 255, 255), r, 4,
                                 border_radius=10)
        if nav:
            for nk, sym in (("prev", "‹"), ("next", "›")):
                pygame.draw.rect(screen, (45, 45, 58), nav[nk],
                                 border_radius=10)
                s = btn_font.render(sym, True, (235, 235, 235))
                screen.blit(s, s.get_rect(center=nav[nk].center))

    # Slideshow row — pictures only; there is nothing to cycle through when the
    # background is one flat colour.
    if slide is None:
        _label(rows["slide"], "Slideshow", dim=True)
        s = note_font.render("(pictures only)", True, (120, 120, 130))
        screen.blit(s, (rows["slide"].x + int(w * 0.17),
                        rows["slide"].centery - s.get_height() // 2))
    else:
        _label(rows["slide"], "Slideshow")
        on = BACKGROUND_SLIDESHOW
        pygame.draw.rect(screen, (40, 120, 50) if on else (95, 55, 55),
                         slide["toggle"], border_radius=12)
        s = btn_font.render("Yes" if on else "No", True, (255, 255, 255))
        screen.blit(s, s.get_rect(center=slide["toggle"].center))
        for bk, sym in (("minus", "−"), ("plus", "+")):
            pygame.draw.rect(screen, (45, 80, 130) if on else (50, 50, 60),
                             slide[bk], border_radius=10)
            s = btn_font.render(sym, True, (255, 255, 255) if on
                                else (120, 120, 130))
            screen.blit(s, s.get_rect(center=slide[bk].center))
        vs = btn_font.render(f"{BACKGROUND_SLIDESHOW_S}s", True,
                             (255, 220, 120) if on else (120, 120, 130))
        screen.blit(vs, vs.get_rect(center=slide["value"].center))

    # Size notice — uses the panel's REAL size, so it tells the truth on any
    # display rather than quoting a number that may not apply.
    note = note_font.render(
        f"Pictures look best at {w} x {h}. Others are scaled to fill and "
        f"centre-cropped.", True, (150, 150, 160))
    screen.blit(note, (max(20, int(w * 0.05)), back.y - note.get_height() - 6))

    pygame.draw.rect(screen, (45, 45, 58), back, border_radius=14)
    bl = btn_font.render("Back", True, (235, 235, 235))
    screen.blit(bl, bl.get_rect(center=back.center))


# Settings panel rows, in top-to-bottom display order. Each is
# (key, label, sized). sized=False means the row is colour-only: no size slider
# and no Bold toggle, because it borrows another row's font. The karaoke fill is
# drawn in the CURRENT line's font/size/bold — only its colour is its own — so
# offering it a size or weight of its own would be a lie.
SETTINGS_ROWS = (
    ("top", "Top line", True),
    ("current", "Current line", True),
    ("bottom", "Bottom line", True),
    ("karaoke", "Karaoke fill (sung words)", False),
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

    Colour-only rows (sized=False) appear in `swatches` but NOT in `sliders` or
    `bolds`, so settings_touch simply never finds a slider/bold to hit for them.
    """
    margin_x = max(20, int(w * 0.08))
    content_w = w - 2 * margin_x
    top = int(h * 0.07)
    done_h = max(48, int(h * 0.11))
    bottom_pad = int(h * 0.03)
    avail = h - top - done_h - bottom_pad
    # A colour-only row carries just a label + swatches, so it needs less height
    # than a row with a slider and a Bold button. Weight the split rather than
    # dividing evenly, so adding it doesn't squeeze the sized rows.
    weights = [1.0 if sized else 0.7 for _k, _l, sized in SETTINGS_ROWS]
    unit = avail / sum(weights)
    row_heights = [max(48, int(unit * wgt)) for wgt in weights]
    if sum(row_heights) > avail:
        # Screen too short for the per-row floor — honouring it would push Done
        # off the bottom edge, so drop the floor and split strictly by weight.
        row_heights = [int(avail * wgt / sum(weights)) for wgt in weights]
    # Swatch size keys off a FULL row, so every row's palette matches.
    sec_h = row_heights[0]

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
    sec_y = top
    for (key, _label, sized), row_h in zip(SETTINGS_ROWS, row_heights):
        if sized:
            slider_y = sec_y + int(row_h * 0.34)
            slider_h = max(10, int(row_h * 0.09))
            sliders[key] = pygame.Rect(margin_x, slider_y, left_w, slider_h)
            sw_y = sec_y + int(row_h * 0.58)
            # Big Bold/Normal button filling the right column.
            bolds[key] = pygame.Rect(right_x, sec_y + int(row_h * 0.12),
                                     right_w, int(row_h * 0.68))
        else:
            # No slider/bold to clear — sit the swatches just under the label.
            sw_y = sec_y + int(row_h * 0.40)
        rects = []
        for j, (name, rgb) in enumerate(SETTING_COLORS):
            x = margin_x + j * (sw + gap)
            rects.append((name, rgb, pygame.Rect(x, sw_y, sw, sw)))
        swatches[key] = rects
        sec_y += row_h
    done_w = max(140, int(w * 0.30))
    done = pygame.Rect(w // 2 - done_w // 2, sec_y, done_w, done_h)
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
    for key, label, sized in SETTINGS_ROWS:
        if sized:
            rect = sliders[key]
            size = sizes[key]
            lbl = label_font.render(f"{label} — {size}px", True, (235, 235, 235))
            screen.blit(lbl, (rect.x, rect.y - lbl.get_height() - 8))
            # Big Bold/Normal toggle on the right: filled blue when bold, dim
            # grey when normal. Font scales with the button so it reads at a
            # glance.
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
            frac = ((size - SETTINGS_FONT_MIN)
                    / (SETTINGS_FONT_MAX - SETTINGS_FONT_MIN))
            frac = min(1.0, max(0.0, frac))
            fill_w = int(rect.w * frac)
            if fill_w > 0:
                pygame.draw.rect(screen, (255, 220, 120),
                                 (rect.x, rect.y, fill_w, rect.h),
                                 border_radius=radius)
            knob_r = max(rect.h, 16)
            pygame.draw.circle(screen, (255, 255, 255),
                               (rect.x + fill_w, rect.centery), knob_r)
        else:
            # Colour-only row: label sits directly above its swatches, since it
            # borrows the Current line's font/size/bold.
            first = swatches[key][0][2]
            lbl = label_font.render(label, True, (235, 235, 235))
            screen.blit(lbl, (first.x, first.y - lbl.get_height() - 8))
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
        # No-lyric case: only the RED bar is up (see draw), so a RED tap opens
        # the picker/Modify Search and nothing else is interactive.
        no_lyric = (state.title and not state.lines
                    and state.lyrics_status in NO_LYRIC_STATUSES)
        if no_lyric:
            if red_rect.collidepoint(px, py):
                last_tap = now_t
                print("[feedback] no lyrics → opening picker for manual search")
                asyncio.create_task(open_picker())
            return
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
    # While picker_searching we're gathering candidates in a worker thread —
    # only ever the case for a Modify Search re-query, since the initial lookup
    # already left every source's results in state.candidates and RED opens the
    # grid straight from those, with zero network work.
    picker = None
    picker_searching = False
    last_picker_tap = 0.0
    # Modify Search editor (opened from the picker's lower-right cell). None =
    # hidden; a SearchEditor while the edit-and-re-search screen is up. The
    # picker list is kept underneath so Back returns to the grid unchanged.
    editor: "SearchEditor | None" = None

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
                       "bottom": color_name_of(NEXT),
                       "karaoke": color_name_of(KARAOKE_COLOR)}
    # Background screen: which tile page is shown, and when the slideshow last
    # advanced. bg_slide_name is the picture actually on screen — held separately
    # from BACKGROUND_IMAGE so a slideshow can move on without overwriting (and
    # persisting) the user's chosen picture.
    bg_page = 0
    bg_slide_at = time.monotonic()
    bg_slide_name = ""
    bg_images: list[str] = []
    bg_images_at = 0.0     # last image/ scan; 0.0 forces one on the first frame

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
        """RED tapped: show the 3x3 candidate grid.

        Instantaneous in the normal case — the initial lookup already swept
        every source and parked its results in state.candidates. The network
        path below only runs when we hold nothing for this name: a Modify
        Search re-query (the user typed a new title/artist). That blocking
        search goes to a thread."""
        nonlocal picker, picker_searching
        if picker_searching or picker is not None:
            return
        sig = (state.title, state.artist)
        if state.candidates_sig == sig:
            picker = state.candidates
            fingers.clear()      # drop the RED-tap finger so it can't linger
            print(f"[picker] showing {len(picker)} cached candidate(s)")
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
        # Show the grid even with ZERO results: the empty cells read faint and
        # the Modify Search button is always present, so the user can still edit
        # the query and re-search a song the automatic lookup missed.
        state.candidates, state.candidates_sig, picker = cands, sig, cands
        fingers.clear()          # drop the RED-tap finger so it can't linger
        print(f"[picker] showing {len(cands)} candidate(s)")

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

    def open_editor() -> None:
        """Modify Search cell tapped: open the editor prefilled with the current
        (possibly already-corrected) display name. orig_sig carries the phone's
        RAW report so a Search can persist the fix as an alias."""
        nonlocal editor
        editor = SearchEditor(
            state.title, state.artist,
            (state.raw_title or state.title, state.raw_artist or state.artist))

    def submit_search() -> None:
        """Search button in the editor: persist any rename as a durable alias,
        adopt the edited name as the live track, and re-run the candidate sweep
        for the new terms (returning to a refreshed grid)."""
        nonlocal editor, picker
        new_title = editor.title.strip()
        new_artist = editor.artist.strip()
        if not new_title:
            return                       # song name is the minimum to search
        orig_title, orig_artist = editor.orig_sig
        # Persist a rename only when both are given (set_alias needs an artist to
        # key + no-ops on empty/unchanged); a title-only search is transient.
        set_alias(orig_title, orig_artist, new_title, new_artist)
        state.title, state.artist = new_title, new_artist
        editor = None
        picker = None
        # New search terms → the held candidates are for the old name. Drop them
        # so open_picker sweeps the network for what the user actually typed.
        state.candidates, state.candidates_sig = [], None
        asyncio.create_task(open_picker())

    def editor_touch(lx: float, ly: float) -> None:
        """Route one logical-coord tap on the Modify Search screen."""
        nonlocal editor
        lay = _editor_layout(w, h)
        # Candidate strip (中 mode) wins the tap while composing.
        if editor.pinyin_mode and editor.comp:
            _f, _label, cands, prev_r, next_r, n_pages, pg = _ime_layout(
                lay["compbar"], editor.comp, editor.cand_page)
            if n_pages > 1 and prev_r.collidepoint(lx, ly):
                editor.cand_page = (pg - 1) % n_pages
                return
            if n_pages > 1 and next_r.collidepoint(lx, ly):
                editor.cand_page = (pg + 1) % n_pages
                return
            for hanzi, consume, rect in cands:
                if rect.collidepoint(lx, ly):
                    editor.select_candidate(hanzi, consume)
                    return
        if lay["back"].collidepoint(lx, ly):
            editor = None                # back to the grid, unchanged
            return
        if lay["artist"].collidepoint(lx, ly):
            editor.set_active("artist")
            return
        if lay["title"].collidepoint(lx, ly):
            editor.set_active("title")
            return
        if lay["search"].collidepoint(lx, ly):
            submit_search()
            return
        for _label, rect, action in lay["keys"]:
            if rect.collidepoint(lx, ly):
                editor.apply_key(action)
                return

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
        global CURRENT, PREV, NEXT, KARAOKE_COLOR
        rgb = COLOR_BY_NAME[name]
        if key == "current":
            CURRENT = rgb
        elif key == "top":
            PREV = rgb
        elif key == "karaoke":
            KARAOKE_COLOR = rgb
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
                "karaoke_color": set_color_names["karaoke"],
                "current_bold": CURRENT_BOLD,
                "top_bold": TOP_BOLD,
                "bottom_bold": BOTTOM_BOLD,
            })
            last_cfg_mtime = _cfg_mtime()
            print(f"[settings] saved fonts={FONT_CURRENT}/{FONT_TOP}/{FONT_BOTTOM}"
                  f" colours={set_color_names['current']}/{set_color_names['top']}"
                  f"/{set_color_names['bottom']}"
                  f" karaoke={set_color_names['karaoke']}"
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

    def background_save() -> None:
        """Persist the background settings and bump last_cfg_mtime so the
        hot-reload watcher doesn't re-apply (and log) our own write."""
        nonlocal last_cfg_mtime
        try:
            write_config_values({
                "background_mode": BACKGROUND_MODE,
                "background_color": bg_color_name_of(BACKGROUND_COLOR),
                "background_image": BACKGROUND_IMAGE,
                "background_slideshow": BACKGROUND_SLIDESHOW,
                "background_slideshow_s": BACKGROUND_SLIDESHOW_S,
            })
            last_cfg_mtime = _cfg_mtime()
            print(f"[background] saved mode={BACKGROUND_MODE} "
                  f"colour={bg_color_name_of(BACKGROUND_COLOR)} "
                  f"image={BACKGROUND_IMAGE or '(first)'} "
                  f"slideshow={BACKGROUND_SLIDESHOW}/{BACKGROUND_SLIDESHOW_S}s")
        except Exception as e:
            print(f"[background] save failed: {e}")

    def background_touch(lx: float, ly: float) -> None:
        """Tap handler for the Background Picture screen (tap-only). Every
        change previews live and persists immediately, like Other Settings."""
        nonlocal menu_screen, bg_page, bg_slide_at, bg_slide_name
        global BACKGROUND_MODE, BACKGROUND_COLOR, BACKGROUND_IMAGE
        global BACKGROUND_SLIDESHOW, BACKGROUND_SLIDESHOW_S
        images = list_background_images()
        rows, mode_rects, choices, slide, nav, back = _background_layout(
            w, h, BACKGROUND_MODE, len(images), bg_page)
        if back.collidepoint(lx, ly):
            menu_screen = "main"
            return
        for value, r in mode_rects:
            if r.collidepoint(lx, ly):
                BACKGROUND_MODE = value
                bg_slide_at = time.monotonic()
                background_save()
                return
        if nav:
            pages = max(1, (len(images) + BG_TILES_PER_PAGE - 1)
                        // BG_TILES_PER_PAGE)
            if nav["prev"].collidepoint(lx, ly):
                bg_page = (bg_page - 1) % pages
                return
            if nav["next"].collidepoint(lx, ly):
                bg_page = (bg_page + 1) % pages
                return
        for value, r in choices:
            if r.collidepoint(lx, ly):
                if BACKGROUND_MODE == "solid":
                    BACKGROUND_COLOR = BG_COLOR_BY_NAME[value]
                else:
                    # Show it at once (the render loop only re-derives this when
                    # the current picture disappears), and restart the timer so
                    # an explicit choice isn't cycled away a moment later.
                    BACKGROUND_IMAGE = value
                    bg_slide_name = value
                    bg_slide_at = time.monotonic()
                background_save()
                return
        if slide:
            if slide["toggle"].collidepoint(lx, ly):
                BACKGROUND_SLIDESHOW = not BACKGROUND_SLIDESHOW
                bg_slide_at = time.monotonic()
                background_save()
                return
            new = BACKGROUND_SLIDESHOW_S
            if slide["minus"].collidepoint(lx, ly):
                new = max(BACKGROUND_SLIDESHOW_MIN_S,
                          BACKGROUND_SLIDESHOW_S - 15)
            elif slide["plus"].collidepoint(lx, ly):
                new = min(BACKGROUND_SLIDESHOW_MAX_S,
                          BACKGROUND_SLIDESHOW_S + 15)
            if new != BACKGROUND_SLIDESHOW_S:
                BACKGROUND_SLIDESHOW_S = new
                background_save()

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
        nonlocal menu_screen, last_menu_tap, bg_page
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
                    set_color_names["karaoke"] = color_name_of(KARAOKE_COLOR)
                    menu_screen = "font"
                elif key == "background":
                    bg_page = 0
                    menu_screen = "background"
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
        elif menu_screen == "background":
            if not motion:
                background_touch(lx, ly)

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
                if editor is not None:
                    # The Modify Search screen owns the screen (drawn over the
                    # grid). Same debounced tap handling as the picker; drain
                    # FINGERUP so a held finger can't trip the long-press.
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
                        if now_t - last_picker_tap >= 0.25:
                            last_picker_tap = now_t
                            editor_touch(*tap_xy)
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
                                if not rect.collidepoint(*tap_xy):
                                    continue
                                if i == MODIFY_SEARCH_CELL:
                                    open_editor()
                                elif i < len(picker):
                                    select_candidate(i)
                                else:
                                    # An empty result cell → dismiss the grid
                                    # back to lyrics. This is the only way out
                                    # when a search returned nothing (no
                                    # candidate to tap), so the user isn't
                                    # trapped on an empty grid.
                                    picker = None
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

            # A new song arrived while the picker/editor was up → its candidates
            # are stale, so drop them and let the new track's lyrics show. (A
            # rename via Search closes both before clearing the sig, so it can't
            # trip this mid-flight.)
            if (picker is not None or editor is not None) and (
                    state.title, state.artist) != state.candidates_sig:
                picker = None
                editor = None

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

            # Which picture the backdrop shows. Settle on the configured one,
            # then let the slideshow walk forward from there on its own timer.
            # bg_slide_name is display-only state — the slideshow never writes
            # BACKGROUND_IMAGE, so the user's pick survives it.
            if BACKGROUND_MODE == "picture":
                if now - bg_images_at >= 2.0:
                    # Re-scan on a slow timer, not every frame — it's a
                    # directory syscall, and it only needs to be fast enough
                    # that a picture dropped into image/ shows up without a
                    # restart.
                    bg_images = list_background_images()
                    bg_images_at = now
                if bg_slide_name not in bg_images:
                    bg_slide_name = _active_background_image()
                    bg_slide_at = now
                if (BACKGROUND_SLIDESHOW and len(bg_images) > 1
                        and now - bg_slide_at >= BACKGROUND_SLIDESHOW_S):
                    nxt = (bg_images.index(bg_slide_name) + 1) % len(bg_images)
                    bg_slide_name = bg_images[nxt]
                    bg_slide_at = now

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
            elif editor is not None:
                # Modify Search screen replaces the grid while editing.
                draw_search_editor(screen, w, h, editor)
            elif picker is not None:
                # RED-button candidate grid replaces the lyric frame.
                draw_picker(screen, w, h, picker)
            elif menu_screen == "bluetooth":
                draw_bluetooth(screen, w, h, bt)
            elif menu_screen == "other":
                draw_other(screen, w, h)
            elif menu_screen == "background":
                draw_background(screen, w, h, bg_page)
            elif menu_screen == "version":
                draw_version(screen, w, h, bt, updater)
            elif menu_screen == "network":
                draw_network(screen, w, h, net)
            else:
                # Backdrop first — lyrics and the idle clock draw on top of it.
                # Menus deliberately never get here: they keep the flat BG fill
                # above, so their controls stay readable.
                paint_lyric_background(screen, w, h, bg_slide_name)
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
                        has_next = idx + 1 < len(state.lines)
                        # Current line: karaoke fill when we know its end (the
                        # next line's timestamp bounds the fill); otherwise a
                        # plain solid line (e.g. the very last line). The fill
                        # tracks per-word timing when the line carries it, else
                        # interpolates across the whole line.
                        if KARAOKE_SYNC and has_next:
                            cur = state.lines[idx]
                            draw_karaoke_line(
                                screen, cur.text, FONT_CURRENT, CURRENT_BOLD,
                                c_current, scale_color(KARAOKE_COLOR, bf),
                                cur.words, t_ms, cur.time_ms,
                                state.lines[idx + 1].time_ms,
                                curr_pos, max_len, ROTATION_DEG)
                        else:
                            draw_line(screen, state.lines[idx].text, FONT_CURRENT,
                                      CURRENT_BOLD, c_current, curr_pos, max_len,
                                      ROTATION_DEG)
                        if has_next:
                            draw_line(screen, state.lines[idx + 1].text,
                                      FONT_BOTTOM, BOTTOM_BOLD, c_next, next_pos,
                                      max_len, ROTATION_DEG)
                            # Progress bar only when karaoke is off — the fill
                            # already shows progress through the line.
                            if (PROGRESS_BAR and not KARAOKE_SYNC
                                    and ROTATION_DEG == 0):
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
                        # Idle (no track at all): just the 24h HH:MM:SS clock,
                        # grown to fill 3/4 of the screen, with the hour RED,
                        # minute YELLOW, second GREEN (colons white). draw_clock
                        # lays the digits on a fixed monospaced grid (so the time
                        # never drifts as digits change) and centers by glyph ink
                        # (equal top/bottom spacing). Size off that same grid width
                        # — 6 max-width digit cells + 2 colon cells — measured at a
                        # reference size, so the widest possible time fills ~3/4.
                        now_local = time.localtime()
                        ref = get_font(200, CURRENT_BOLD, CLOCK_FONT_PATH)
                        grid_w = (max(ref.size(str(d))[0] for d in range(10)) * 6
                                  + ref.size(":")[0] * 2)
                        clock_size = max(8, int(200 * min(w * 0.75 / grid_w,
                                                          h * 0.75 / ref.get_height())))
                        colon = scale_color(COLOR_BY_NAME["white"], bf)
                        segments = [
                            (time.strftime("%H", now_local),
                             scale_color(COLOR_BY_NAME["red"], bf)),
                            (":", colon),
                            (time.strftime("%M", now_local),
                             scale_color(COLOR_BY_NAME["yellow"], bf)),
                            (":", colon),
                            (time.strftime("%S", now_local),
                             scale_color(COLOR_BY_NAME["green"], bf)),
                        ]
                        draw_clock(screen, segments, clock_size, CURRENT_BOLD,
                                   curr_pos, ROTATION_DEG, CLOCK_FONT_PATH)

                # Ask for a verdict on fresh (uncached) lyrics. Drawn before the
                # flip so the buttons orient with the lyrics.
                if state.awaiting_feedback and state.lines:
                    _draw_feedback_buttons(screen, green_rect, red_rect)
                # No lyrics were found for this song → still offer RED alone so
                # the user can open the picker / Modify Search and hand-search.
                elif state.title and state.lyrics_status in NO_LYRIC_STATUSES:
                    _draw_feedback_buttons(screen, green_rect, red_rect,
                                           green=False)

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
                    draw_line(screen, "♪ Searching lyrics…", FONT_SYNC,
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
