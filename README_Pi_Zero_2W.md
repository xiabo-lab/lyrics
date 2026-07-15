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

## ⚠️ Road safety

**This is a glance-only display. Your attention belongs on the road, not the
screen.** Treat it like a passenger singing along — never stare at it, and never
operate its touch controls (menus, Bluetooth pairing, the lyric picker) while
the car is moving. Do all setup and tuning **parked, or on a home desktop.** The
app shows a brief safety reminder at every startup for this reason.

Driving safely is your responsibility. The author accepts no liability for use
of this hobby project; use it only in a way that is legal where you drive and
that keeps your eyes on the road.

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

> **Tested platform.** This project is tested and run **only on a Raspberry Pi
> Zero 2W with Raspberry Pi OS _Lite_ (64-bit, no desktop).** Other Pi models and
> images may work but are not validated. Use a **Lite / console-only** image: a
> "with desktop" image runs its own Wayland compositor (labwc, via the display
> manager) that holds the screen and fights `cage` for it, so the lyrics never
> appear. If you must use a desktop image, switch the Pi to boot to console
> (`sudo systemctl set-default multi-user.target`). For the Raspberry Pi 5,
> follow the separate [README_Pi5.md](README_Pi5.md), which covers its extra
> boot/seat quirks.

- Raspberry Pi (developed on a Pi 4 / Pi OS Bookworm, 64-bit; **tested in-car on
  a Pi Zero 2W with Pi OS Lite**).
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
| `lyric_sources.py` | Multi-source synced-lyric fetcher (QQ / Kugou / NetEase / LRCLIB): one concurrent sweep, best-match scored, with on-disk cache and the RED-button picker grid. Prefers word-by-word lyrics (Kugou KRC / QQ QRC) where available. |
| `lrclib.py` | LRCLIB client + the `.lrc` parser (`parse_lrc`, handles enhanced/word-level LRC) and `LyricLine`/`Word` types. |
| `qqcrypto.py` | Pure-Python decryptor for QQ's word-by-word QRC lyrics (QQ's "buggy DES"). |
| `config.json` | Live, hot-reloaded tuning (offsets, fonts, colours, brightness, …). |
| `fnt/` | Extra fonts for the idle clock (`Aldrich-Regular.ttc`, `advanced_led_board-7.ttc`). Missing → the clock falls back to Noto. |
| `*icon.png` / `*icon.jpg` | Per-source badges (QQ / Kugou / NetEase / LRCLIB) shown on picker results. |
| `pinyin_table.json` | Offline pinyin→Hanzi table for the Modify Search IME. |
| `bt-agent.service` | systemd unit for the headless Bluetooth pairing agent. |
| `99-carlyric-ignore-avrcp-pointer.rules` | udev rule so the phone's AVRCP device isn't treated as a mouse (stops a stray cursor). |
| `wifi.sh` | One-shot helper to join a new Wi-Fi network via NetworkManager. |
| `carlyric-claude.sudoers` | Optional sudoers snippet allowing password-less service restart. |
| `test_lyrics.py` | Unit tests for the pure logic (lrc parsing, lock policy, line indexing). |
| `Test/` | Scratch/manual test scripts from development (display, touch, AVRCP probes). |
| `cache/` | Confirmed lyrics, one `.lrc` per song (created at runtime). |

---

## Part A — Flash the OS

**OS: Raspberry Pi OS (64-bit) — use the _Lite_ image (no desktop).** This is the
tested platform: **Pi Zero 2W + Pi OS Lite**. A "with desktop" image runs its own
Wayland compositor (`labwc`, via the display manager) that holds the screen and
fights `cage`, so the lyrics never appear. If you must use a desktop image, switch
the Pi to boot to console (`sudo systemctl set-default multi-user.target`).

1. Install **Raspberry Pi Imager** (<https://www.raspberrypi.com/software/>) and
   insert the microSD card.
2. In Imager:
   - **Device:** Raspberry Pi Zero 2W
   - **OS:** Raspberry Pi OS (other) → **Raspberry Pi OS Lite (64-bit)**
   - **Storage:** your microSD card
3. Click **Next → Edit Settings** (the OS customization dialog) and set:
   - **Hostname:** `carlyric` (so `carlyric.local` resolves on the network)
   - **Username:** `fuwenxu` + a password (keep `fuwenxu` to match the project's
     paths, or see *Changing the username* below)
   - **Wi-Fi:** your SSID, password, and country
   - **Services tab:** enable **SSH** (password authentication)
4. **Write** the card, insert it into the Pi, connect the HDMI display, and power
   on.
5. After ~1 minute, SSH in from your computer:

   ```bash
   ssh fuwenxu@carlyric.local
   ```

   If `.local` doesn't resolve, use the Pi's IP from your router instead.

---

## Part B — Install dependencies

```bash
sudo apt update
sudo apt install -y python3-dbus-next python3-pygame fonts-noto-cjk \
                    cage seatd python3-requests bluez bluez-tools uhubctl
```

`cage` is a single-app Wayland kiosk compositor; `seatd` gives it a seat without
a full desktop. `fonts-noto-cjk` is required for Chinese glyphs. `uhubctl` lets
the app power-cycle the USB 5G modem's port on a connectivity drop (see
**Auto-recovery** below) — optional, but recommended for in-car use.

Enable `seatd` so it starts at boot (on a Lite/console image you must enable it
yourself, or `cage` fails with a `libseat`/seat error):

```bash
sudo systemctl enable --now seatd
```

---

## Part C — Get the code

Install `git` first, then clone:

```bash
sudo apt install git -y
git clone https://github.com/xiabo-lab/lyrics.git ~/carlyrics
```

> **Pick your username.** The helper files and the example service unit assume
> the Linux user `fuwenxu` and the path `/home/fuwenxu/carlyrics`. If your Pi
> user is different, change it — see
> [Changing the username](#changing-the-username) below.

---

## Part D — Install the support pieces

**1. Bluetooth auto-pairing agent** (lets you pair phones from the touchscreen —
the Pi has no keyboard to confirm a pairing, so `bt-agent` (NoInputNoOutput)
auto-accepts the "Just Works" pairing modern phones use):

```bash
sudo cp ~/carlyrics/bt-agent.service /etc/systemd/system/
sudo systemctl enable --now bt-agent
```

**2. Stop the stray cursor** (the phone's AVRCP control channel looks like a
mouse to the compositor, which pops a cursor on screen — this udev rule ignores
it):

```bash
sudo cp ~/carlyrics/99-carlyric-ignore-avrcp-pointer.rules /etc/udev/rules.d/
sudo udevadm control --reload && sudo udevadm trigger
```

**3. (Optional) password-less service restarts** (if you redeploy often, lets a
non-root user restart the service without a password prompt — edit the username
first):

```bash
sudo cp ~/carlyrics/carlyric-claude.sudoers /etc/sudoers.d/carlyric
sudo chmod 440 /etc/sudoers.d/carlyric
```

---

## Part E — Create the main service

The GUI needs a graphical seat (cage), so it can't be launched over a plain SSH
session — run it from systemd. Create the unit:

```bash
sudo nano /etc/systemd/system/carlyric.service
```

Paste this exactly. **The paths below are for user `fuwenxu`. If your username is
different, replace `fuwenxu` in the `ExecStart` line with your real username —
never leave a `<pi-user>` placeholder there, or the service loops with
`can't open file '/home/<pi-user>/carlyrics/Lyrics_Display.py': [Errno 2] No such
file or directory`.**

```ini
[Unit]
Description=Car Lyrics Display (cage + pygame scroller)
After=bluetooth.service seatd.service
Wants=bluetooth.service

[Service]
User=root
Environment=XDG_RUNTIME_DIR=/tmp
ExecStart=/usr/bin/cage -s -- /usr/bin/python3 /home/fuwenxu/carlyrics/Lyrics_Display.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

Sanity-check the path if it's looping:

```bash
grep ExecStart /etc/systemd/system/carlyric.service   # must show YOUR real home dir
```

Then enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now carlyric.service
journalctl -u carlyric.service -f      # watch it start
```

A healthy start logs `[config] …`, `[display] <W> x <H>`, and on a track change
`[track] <artist> — <title>` followed by a lyric source hit.

---

## Part F — Get the Pi online in the car (mobile hotspot)

**Only needed for in-car / outdoor use.** The Pi fetches lyrics for *new* songs
over the internet (songs you've already confirmed are cached and play offline). At
home it's on the Wi-Fi you set in Part A; in the car it needs a mobile internet
source. Set this up **once, parked**, and NetworkManager auto-connects whenever
that network is in range — nothing to do per drive.

- **Using a dedicated mobile hotspot (e.g. the TCL LINKPORT IK511 5G / T-Mobile)?**
  Add its network to the Pi once (below) and you're set — it powers up with the
  car and the Pi rejoins automatically. **Skip this part on future drives.**
- **Using your phone's hotspot?** Add the phone's hotspot the same way; the Pi
  then joins it whenever you turn Personal Hotspot on.

Add the network from your computer over SSH while the Pi is still on your home
Wi-Fi:

```bash
nano ~/carlyrics/wifi.sh    # set SSID and PASSWORD to the hotspot's
~/carlyrics/wifi.sh         # asks for the sudo password; joins and saves it
```

NetworkManager remembers every network and auto-picks whichever known one is in
range, so this is a one-time step per hotspot (this is the same `wifi.sh` covered
under *Wi-Fi* below).

> **The Pi Zero 2W is 2.4 GHz-only.** The hotspot must broadcast a **2.4 GHz**
> band or the Zero won't see it. On iPhone, turn on **Settings → Personal Hotspot
> → Maximize Compatibility** (forces 2.4 GHz); the TCL IK511 and most Android
> hotspots already expose a 2.4 GHz SSID.

---

## Auto-recovery (when the internet drops)

Mobile data in the car is flaky — a 5G hotspot or USB modem can silently stop
passing traffic. The app watches for this: if **two songs in a row** get no
lyrics **and** a live reachability check confirms the internet is actually down,
it recovers automatically:

- **On a USB 5G modem** (e.g. the IK511 plugged in as a USB network adapter):
  it **power-cycles just that modem's USB port** with `uhubctl`, falling back to
  a driver re-enumeration if `uhubctl` isn't installed. Only the modem's own port
  is touched — never the touchscreen or anything else on the bus.
- **On Wi-Fi** (home Wi-Fi or a Wi-Fi hotspot): it **reboots the Pi**, which
  re-kicks NetworkManager and the radio.

Two genuinely-unfindable songs on a *working* connection never trigger it — the
reachability check gates on the internet actually being down. Turn it off with
`"auto_recover": false` in `config.json`.

---

## Part G — Pair your phone & verify

1. On the Pi screen, **long-press 10 s** → **Settings → Bluetooth → Pair New
   Phone**.
2. On your phone, pair with **`carlyric`** just like a Bluetooth speaker. This is
   a one-time step — afterwards the app auto-connects on boot, and you can swap in
   a *replacement* phone from this same menu (the Pi keeps one phone paired at a
   time).
3. Play a song — lyrics should scroll in sync. Tune offsets from the on-screen
   menu or `config.json` (see *Configuration* below for every key).

**✅ That completes the required setup (Parts A–G)** — the lyrics display now
starts on boot and reconnects your phone. Everything below is optional or
reference; skip it unless you need it.

---

# Reference & optional (skip unless you need it)

Everything below is background, tuning reference, and troubleshooting — the
display already works after Parts A–G above. Read a section only when you need it.

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
| `karaoke_sync` | Karaoke fill: the current line's colour sweeps left→right in time (per-word when the source has word timing — see **Word-level karaoke** — else across the whole line). Replaces the progress bar when on. Default `true`. |
| `karaoke_color` | The *sung* colour of the karaoke fill (palette name); the not-yet-sung part uses `current_color`. Default `white`. |
| `progress_bar` | Thin progress bar under the current line. Ignored while `karaoke_sync` is on (the fill already shows progress). |
| `intro_dots_max` | Pre-song countdown dots (1 dot ≈ 1 second). |
| `line_gap_pad` | Extra spacing between stacked lines. |
| `target_fps` | Render frame rate. |
| `dim_enabled` / `day_start_hour` / `night_start_hour` / `night_brightness` | Automatic night dimming. |
| `max_line_width_frac` | Shrink-to-fit any line wider than this fraction of the screen. |
| `flip_180` | Rotate the whole frame 180° for an upside-down mount. |
| `autoconnect` | Auto-reconnect AVRCP to the paired phone on boot / after a drop. |
| `auto_recover` | Auto-recover lost internet after two lyric-less songs (see **Auto-recovery**). Default `true`. |

Most font/colour values are also editable live from the on-screen menu (below),
which writes them back to `config.json`.

---

## Word-level karaoke

The current lyric line **fills with colour left→right in time with the song** —
the already-sung part in `karaoke_color` (default white), the rest in
`current_color`. This replaces the old progress bar (`karaoke_sync`, default on).

How precisely the fill tracks the words depends on what the source provides:

- **Per-word / per-character timing** when the matched lyric comes from **Kugou
  (KRC)** or **QQ Music (QRC)** — the fill edge lands on the exact word (or CJK
  character) being sung. QRC needs `qqcrypto.py` (bundled).
- **Line-level fill** otherwise (NetEase, LRCLIB, or any plain `.lrc`) — the edge
  glides evenly across the line over its duration. Still a smooth karaoke sweep,
  just not word-accurate.

Both are automatic — no configuration beyond the two `karaoke_*` keys. Songs
cached before this feature stay line-level until re-fetched (delete a cached
lyric with a triple-tap, or clear `cache/`, to pull the word-level version).

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
  - **Other Settings** — Yes/No toggles for **Rotate Screen 180°** (`flip_180`)
    and **Auto Dim** (`dim_enabled`), plus ± steppers for **Bluetooth A2DP
    Offset** (`latency_offset_ms`, 0–3 s in 0.1 s steps) and **Lyrics Timing
    Offset** (`lead_offset_ms`, −3…+3 s in 0.5 s steps). Each tap previews live
    and saves to `config.json` immediately.
  - **Network** — a read-only status screen showing the Wi-Fi network name, the
    Pi's IP address, and whether the **internet** is actually reachable
    (Online / Offline), with a **Check now** button. "Online" means a live
    connection to GitHub succeeded — i.e. firmware updates and lyric look-ups
    will work — not merely that Wi-Fi is associated.
  - **Software Version** — build info and the Pi's Bluetooth name, plus
    **Update Firmware**: pulls the latest code from GitHub and restarts, so you
    can update with no computer or SSH (tap once to arm, again to confirm). It
    only applies when GitHub is a **newer** version — otherwise it reports
    "Already up to date" and leaves the running build alone, so it can never
    downgrade. Your `config.json`, cached lyrics, rejections, and saved name
    corrections (`aliases.json`) are preserved.
- **Double-tap** → toggles a brightness slider; while it's shown, a one-finger
  vertical swipe brightens/darkens.
- **Triple-tap** → deletes the current song's **cached** lyrics and shows
  "This lyric has been deleted.", so a wrong cached match is dropped and
  re-searched the next time the song plays.
- **Two-finger horizontal swipe** → nudges the sync for the *current song only*
  (for the rare track whose master timing doesn't match the lyrics). The nudge
  is live by default and resets on the next track — **but you can make it
  permanent** by confirming with the Green button (see below).
- **Green ✓ / Red ✗ edge buttons** → appear when fresh (uncached) lyrics load.
  **Green** confirms the match and caches it. **Red** opens a **picker** — a
  grid of up to **8** candidate versions (2 from each source: QQ, Kugou, NetEase,
  LRCLIB), each cell showing the song title and artist — tap the right one to
  switch to it. After a pick both buttons stay up (Red reopens the grid) until
  you confirm with **Green**.
  - If lyrics **can't be found** at all ("Lyrics not found"), the **Red** bar
    still appears on its own so you can open the picker and hand-search.
  - **Modify Search** (lower-right cell of the grid) → opens an on-screen
    keyboard to correct a garbled/wrong **artist** and **song** name and search
    again. The song name alone is enough (artist optional); `←`/`→` move the
    cursor to fix the middle of a name. A **中 / A** key toggles a built-in
    **pinyin → Chinese** input: type pinyin (e.g. `zhoujielun`) and tap a
    character/word from the candidate strip (`«` `»` page through all of them).
    Correcting a name here is **remembered** — the next time that song plays,
    the display uses your corrected name and its cached lyric automatically.

  **Saving a sync fix so it sticks:** if a song's timing is off, **swipe to fix
  it first (two-finger horizontal swipe), _then_ press Green.** Green bakes
  whatever sync adjustment is active at that moment into the saved lyrics, so
  the song plays in sync automatically on every future play — you never have to
  nudge it again, and the fix survives reboots and firmware updates.

  > ⚠️ **Order matters — swipe first, then Green.** Pressing Green *before*
  > swiping saves the un-adjusted timing, and once you press Green the buttons
  > disappear, so a swipe afterwards won't be saved. You can swipe as many times
  > as you need to dial it in, then press Green once. If you confirmed too early,
  > **triple-tap** to delete the cached lyric, let it re-search, then
  > swipe-then-Green.

When nothing is playing (the phone has stopped, not just paused), the display
falls back to an **idle clock** instead of a blank "waiting" note: the date
(`MM/DD/YYYY`) on top, the current 24-hour time (`HH:MM:SS`) big in the middle
ticking by the second, and "Waiting for Music" at the bottom.

---

## Lyric sources & caching

For each track, `lyric_sources.fetch_synced_lyrics_any()` tries, in order:

1. **Local cache** (`cache/`) — confirmed-correct lyrics from a previous play.
2. **QQ Music**, **Kugou**, **NetEase** — Chinese catalogues first, since they
   match a Chinese library far more reliably.
3. **LRCLIB** — crowd-sourced / Western-leaning fallback.

The first source returning *timestamped* lyrics wins. Results are **not** cached
automatically — a lyric only sticks once you confirm it with the green button,
so a wrong match never gets remembered. If the auto-picked version is wrong, the
**red button** opens a picker of up to **8** candidates (2 from each of QQ,
Kugou, NetEase, LRCLIB — the four sources are queried in parallel so the grid
appears quickly) so you can pick the right one by hand, then confirm with green.
If the song's name is wrong or missing, **Modify Search** in the grid lets you
retype the artist/song — including Chinese via a built-in pinyin keyboard — and
search again; the correction is remembered for future plays. A **triple-tap** on
a playing lyric deletes a bad **cached** entry so it's re-searched next time.

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
lyrics (`cache/`), `rejections.json`, and `aliases.json` (saved name
corrections) are **not** touched, so your tuning survives. So bump `APP_VERSION`
in `Lyrics_Display.py` whenever you publish a change you want devices to pull.

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
