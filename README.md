# carlyrics

A car karaoke lyric display for the Raspberry Pi. Your phone plays music over
Bluetooth; the Pi reads the *playback position* from the phone and scrolls
time-synced lyrics on a screen mounted in the car — in time with the song, with
no app to install on the phone and nothing to tap while driving.

It's built for a Chinese-leaning music library (the lyric search leads with
Chinese catalogues) but works with anything that has synced lyrics online.

> ⚠️ Personal hobby project. It talks to several **unofficial** music-lyric
> endpoints that can change or disappear at any time. No affiliation with, or
> endorsement by, Apple, Google, Tencent (QQ Music), Kugou, NetEase, or LRCLIB.

---

## How it works

```
  Phone (Apple Music / YouTube Music / …)
        │  Bluetooth A2DP audio  ──────────────►  car stereo
        │  AVRCP metadata + position
        ▼
  Raspberry Pi (this project)
        │  BlueZ D-Bus: org.bluez.MediaPlayer1   → "what song, where in it?"
        │  lyric cascade (cache → online sources) → time-stamped .lrc
        ▼
  HDMI bar display (cage + pygame kiosk)        → the scrolling lyrics
```

- **Position, not audio.** The Pi acts as a Bluetooth speaker (A2DP sink +
  AVRCP controller). It never needs to decode the audio — it asks BlueZ for the
  current track and playback position and extrapolates a local clock between
  updates. Apple Music broadcasts position ~once a second (tight sync); YouTube
  Music only on play/pause/skip (sync is re-anchored at those events).
- **Lyric cascade.** For each track it tries a local cache first, then a series
  of online lyric providers, stopping at the first that returns *synced* lyrics
  (lines with `[mm:ss.xx]` timestamps). Plain unsynced lyrics are rejected.
- **Latency & lead tuning.** Two offsets shift the lyrics relative to the audio
  so a line is already on screen when you reach it — see `config.json`.

---

## Hardware

- Raspberry Pi (developed on a Pi 4 / Pi OS Bookworm, 64-bit).
- A display — designed for a wide "bar" LCD (e.g. 1920×440), mounted above the
  dash. Works on any HDMI screen; set `flip_180` if it's mounted upside-down.
- A touchscreen is optional but recommended — all on-screen controls
  (settings menu, feedback buttons, brightness) are touch gestures.
- Built-in Bluetooth (or a USB BT dongle).

---

## Repository layout

| File | Purpose |
|------|---------|
| `Lyrics_Display.py` | The app: BlueZ/AVRCP watcher, sync clock, pygame renderer, on-screen settings + Bluetooth pairing menu. |
| `lyric_sources.py` | Multi-source synced-lyric fetcher with on-disk cache, per-song rejections, and the source cascade. |
| `lrclib.py` | LRCLIB client + the `.lrc` parser (`parse_lrc`) and `LyricLine` type. |
| `config.json` | Live, hot-reloaded tuning (offsets, fonts, colours, brightness, …). |
| `bt-agent.service` | systemd unit for the headless Bluetooth pairing agent. |
| `99-carlyric-ignore-avrcp-pointer.rules` | udev rule so the phone's AVRCP device isn't treated as a mouse (stops a stray cursor). |
| `wifi.sh` | One-shot helper to join a new Wi-Fi network via NetworkManager. |
| `carlyric-claude.sudoers` | Optional sudoers snippet allowing password-less service restart. |
| `test_lyrics.py` | Unit tests for the pure logic (lrc parsing, lock policy, line indexing). |
| `Test/` | Scratch/manual test scripts from development (display, touch, AVRCP probes). |
| `cache/` | Confirmed lyrics, one `.lrc` per song (created at runtime). |

---

## Setup on the Pi

### 1. Dependencies

```bash
sudo apt update
sudo apt install -y python3-dbus-next python3-pygame fonts-noto-cjk \
                    cage seatd python3-requests bluez bluez-tools
```

`cage` is a single-app Wayland kiosk compositor; `seatd` gives it a seat without
a full desktop. `fonts-noto-cjk` is required for Chinese glyphs.

### 2. Get the code

```bash
git clone https://github.com/xiabo-lab/lyrics.git ~/carlyrics
```

> **Pick your username.** The helper files and the example service unit assume
> the Linux user `fuwenxu` and the path `/home/fuwenxu/carlyrics`. If your Pi
> user is different, change it — see
> [Changing the username](#changing-the-username) below.

### 3. Pair your phone (first time)

Bring up Bluetooth on the Pi, then pair from the phone exactly as you'd pair a
Bluetooth speaker. After that, the app auto-connects on boot and you can pair
*replacement* phones from the on-screen menu (see below). The Pi keeps only one
phone paired at a time.

For on-screen pairing to work, install the headless pairing agent — the Pi has
no keyboard to confirm a pairing, so `bt-agent` (NoInputNoOutput) auto-accepts
the "Just Works" pairing modern phones use:

```bash
sudo cp ~/carlyrics/bt-agent.service /etc/systemd/system/
sudo systemctl enable --now bt-agent
```

### 4. Stop the stray cursor

The phone's AVRCP control channel looks like a mouse to the compositor, which
pops a cursor on screen. Install the udev rule to ignore it:

```bash
sudo cp ~/carlyrics/99-carlyric-ignore-avrcp-pointer.rules /etc/udev/rules.d/
sudo udevadm control --reload && sudo udevadm trigger
```

### 5. Run it as a service

The GUI needs a graphical seat (cage), so it can't be launched over a plain SSH
session — run it from systemd. Create `/etc/systemd/system/carlyric.service`
(adjust the user and path to match your install):

```ini
[Unit]
Description=Car Lyrics Display (cage + pygame scroller)
After=bluetooth.service seatd.service
Wants=bluetooth.service

[Service]
User=root
Environment=XDG_RUNTIME_DIR=/tmp
ExecStart=/usr/bin/cage -s -- /usr/bin/python3 /home/<pi-user>/carlyrics/Lyrics_Display.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now carlyric.service
journalctl -u carlyric.service -f      # watch it start
```

A healthy start logs `[config] …`, `[display] <W> x <H>`, and on a track change
`[track] <artist> — <title>` followed by a lyric source hit.

### 6. (Optional) password-less restarts

If you redeploy often, the sudoers snippet lets a non-root user restart the
service without a password prompt (edit the username first):

```bash
sudo cp ~/carlyrics/carlyric-claude.sudoers /etc/sudoers.d/carlyric
sudo chmod 440 /etc/sudoers.d/carlyric
```

---

## Changing the username

The repo was developed with the Linux user **`fuwenxu`**, which appears as a
hard-coded example in a few places. To use your own Pi user, replace `fuwenxu`
with your username (e.g. the Raspberry Pi OS default `pi`) in these files:

| File | What to change |
|------|----------------|
| `carlyric-claude.sudoers` | the leading `fuwenxu` (the user granted password-less restart). |
| `wifi.sh` | the `ssh fuwenxu@…` hint printed on success. |
| The systemd unit (Step 5 above) | `User=` and the `/home/fuwenxu/carlyrics` path in `ExecStart`. |
| `Lyrics_Display.py` (top docstring) | the example `scp`/run commands — comments only, cosmetic. |
| `Test/*.py` (docstrings) | example paths/commands — comments only, cosmetic. |

A quick way to do them all at once (run from the repo root on the Pi, swapping
`YOURNAME`):

```bash
grep -rl fuwenxu . | xargs sed -i 's/fuwenxu/YOURNAME/g'
```

The home-directory path must also match wherever you cloned the repo
(`/home/YOURNAME/carlyrics`). Nothing here depends on the literal name
`fuwenxu` — it's purely the account the files were written for.

---

## Configuration (`config.json`)

`config.json` lives next to the script and **hot-reloads within ~1 second** —
edit it while the app runs and the change takes effect with no restart. A
missing or malformed key silently falls back to a safe default, so a bad edit
can never blank the screen. Code changes still need a service restart.

| Key | Meaning |
|-----|---------|
| `lead_offset_ms` | How far *ahead* of the audio to show each line (positive = lyrics lead). |
| `latency_offset_ms` | Compensation for Bluetooth A2DP audio buffering. |
| `font_current` / `font_top` / `font_bottom` | Font px for the now line / previous line / next line. |
| `current_color` / `top_color` / `bottom_color` | Named palette colour per line (yellow, green, white, red, blue, purple). |
| `current_bold` / `top_bold` / `bottom_bold` | Render each line (now / previous / next) bold. |
| `show_prev_line` | Show the previous lyric above the current one. |
| `progress_bar` | Thin progress bar under the current line. |
| `intro_dots_max` | Pre-song countdown dots (1 dot ≈ 1 second). |
| `line_gap_pad` | Extra spacing between stacked lines. |
| `target_fps` | Render frame rate. |
| `dim_enabled` / `day_start_hour` / `night_start_hour` / `night_brightness` | Automatic night dimming. |
| `max_line_width_frac` | Shrink-to-fit any line wider than this fraction of the screen. |
| `flip_180` | Rotate the whole frame 180° for an upside-down mount. |
| `autoconnect` | Auto-reconnect AVRCP to the paired phone on boot / after a drop. |

Most font/colour values are also editable live from the on-screen menu (below),
which writes them back to `config.json`.

---

## On-screen controls (touch)

All gestures are tuned to be usable at a glance while driving.

- **Long-press (10 s, one finger)** → opens the **Settings menu**:
  - **Font Settings** — a size slider, a 6-colour palette, and a **Bold/Normal**
    toggle for each of the current / top / bottom lines; *Done* saves to
    `config.json`.
  - **Bluetooth** — **Pair New Phone** (puts the Pi in pairing mode so a new
    phone can connect — the old phone is then dropped), plus a list of paired
    phones each with a **Forget** button.
  - **Software Version** — build info and the Pi's Bluetooth name, plus
    **Update Firmware**: pulls the latest code from GitHub and restarts, so you
    can update with no computer or SSH (tap once to arm, again to confirm). It
    only applies when GitHub is a **newer** version — otherwise it reports
    "Already up to date" and leaves the running build alone, so it can never
    downgrade. Your `config.json`, cached lyrics, and rejections are preserved.
- **Double-tap** → toggles a brightness slider; while it's shown, a one-finger
  vertical swipe brightens/darkens.
- **Two-finger horizontal swipe** → nudges the sync for the *current song only*
  (for the rare track whose master timing doesn't match the lyrics). Reset on
  the next track.
- **Green ✓ / Red ✗ edge buttons** → appear when fresh (uncached) lyrics load.
  **Green** confirms the match and caches it; **Red** marks the source wrong,
  remembers that, and re-searches the next source.

---

## Lyric sources & caching

For each track, `lyric_sources.fetch_synced_lyrics_any()` tries, in order:

1. **Local cache** (`cache/`) — confirmed-correct lyrics from a previous play.
2. **QQ Music**, **Kugou**, **NetEase** — Chinese catalogues first, since they
   match a Chinese library far more reliably.
3. **LRCLIB** — crowd-sourced / Western-leaning fallback.

The first source returning *timestamped* lyrics wins. Results are **not** cached
automatically — a lyric only sticks once you confirm it with the green button,
so a wrong match never gets remembered. Marking a result wrong (red button)
records the offending source in `rejections.json` and skips it for that song on
future searches.

> The QQ/Kugou/NetEase endpoints are public but unofficial and undocumented;
> they may break without notice. The relevant URLs/constants are grouped at the
> top of each source block in `lyric_sources.py` if they need fixing.

---

## Updating

**From the screen (no computer needed):** long-press → **Software Version** →
**Update Firmware**. It downloads the latest code from this repo
(`xiabo-lab/lyrics`, `main` branch) as a tarball, and **only if GitHub's
`APP_VERSION` is newer** than the running build, overwrites the program files
and restarts (otherwise it just says "Already up to date" — it never
downgrades). Tap once to arm, again to confirm. Your `config.json`, cached
lyrics (`cache/`), and `rejections.json` are **not** touched, so your tuning
survives. So bump `APP_VERSION` in `Lyrics_Display.py` whenever you publish a
change you want devices to pull.

> **Forks:** the update always pulls from the repo in `UPDATE_URL` near the top
> of `Lyrics_Display.py`. Point it at your own fork to ship updates to your own
> devices.

**Manually (if you cloned with git):**

```bash
cd ~/carlyrics && git pull && sudo systemctl restart carlyric.service
```

---

## Wi-Fi

To move the Pi to a new network (e.g. your phone's hotspot in the car), edit the
SSID/password at the top of `wifi.sh` and run it once:

```bash
nano ~/carlyrics/wifi.sh    # set SSID and PASSWORD
~/carlyrics/wifi.sh         # asks for sudo password
```

NetworkManager saves every network and auto-picks whichever known one is in
range, so this is a one-time step per network.

---

## Tests

Pure-logic unit tests (no network or display needed):

```bash
cd ~/carlyrics
python3 -m unittest -v test_lyrics
```

---

## Acknowledgements

Synced lyrics via [LRCLIB](https://lrclib.net) and the public QQ Music, Kugou,
and NetEase Cloud Music lyric endpoints. Built on [BlueZ](http://www.bluez.org/),
[cage](https://github.com/cage-kiosk/cage), and [pygame](https://www.pygame.org/).

---

## License

Released under the [MIT License](LICENSE).
