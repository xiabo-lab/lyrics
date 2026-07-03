# carlyrics on Raspberry Pi 5 — Raspberry Pi OS **Lite** (64-bit)

Step-by-step setup for the car lyric display on a **Raspberry Pi 5** running the
**64-bit _Lite_ (console-only, no desktop)** image. Tested end-to-end on a **Pi 5
1GB**; any Pi 5 (1/2/4/8 GB) works — the app is light and never decodes audio.
**No source-code changes are required**; the code is identical to the Pi Zero 2W
build.

> ⚠️ **Use the _Lite_ image, not "with desktop."** The desktop image auto-starts
> its own Wayland compositor (`labwc` via `lightdm`), which grabs the HDMI output
> and fights `cage`. You'll get an endless `Swapchain … failed test` and a black
> screen. `cage` must be the **only** compositor. (Already on the desktop image?
> See *Display gotchas → #1* below to switch to console without reflashing.)

These instructions assume the Linux username **`fuwenxu`** and the install path
**`~/carlyrics`**. If your username differs, read *If your username isn't
`fuwenxu`* just below — **the one place it truly matters is the service
`ExecStart` path in Part E.**

> ### If your username isn't `fuwenxu`
>
> **The Python code needs no edits** — it resolves `config.json`, `cache/`, and
> `rejections.json` relative to the script itself, so it runs from any path. Only
> deployment paths change. Substituting `pi5` as an example:
>
> | Where | Change | Required? |
> |-------|--------|-----------|
> | systemd unit `ExecStart` (Part E) | `/home/pi5/carlyrics/Lyrics_Display.py` | **Yes — a wrong/placeholder path is the #1 reason the service fails to start** |
> | `carlyric-claude.sudoers` (Part D-3) | leading `fuwenxu` → `pi5` | Only if you use password-less restarts |
> | clone path, `ssh`/`scp` host | your username + home dir | Yes (it's just where you log in / clone) |
>
> Fix the repo files in one pass (from the repo root, after Part C):
>
> ```bash
> grep -rl fuwenxu . | xargs sed -i 's/fuwenxu/pi5/g'
> ```
>
> The systemd unit lives in `/etc/systemd/system/` (outside the repo), so set its
> path **by hand** in Part E — do not leave any `<pi-user>` placeholder in it.

---

## Hardware notes (Pi 5)

| Item | What to do |
|------|------------|
| **HDMI** | Plug the bar display into **HDMI0** — the micro-HDMI port **nearest the USB-C power** connector. Needs a micro-HDMI cable/adapter. |
| **Power** | Use a **5V / 5A (25W) USB-C** supply. An under-sized car charger causes low-voltage throttling on the Pi 5 — size the car power accordingly. |
| **Cooling** | Fit the **official active cooler** or a fan case. The Pi 5 runs hot and throttles in a warm car under sustained load. |
| **RAM** | 1GB is enough (tested). More RAM gives no benefit for this app. |
| **Audio** | No 3.5mm jack, and irrelevant here — the Pi is a Bluetooth A2DP *sink* and never decodes audio (`SDL_AUDIODRIVER=dummy`). |
| **Bluetooth** | Onboard BT works the same as the Zero; pairing / `bt-agent` / AVRCP flow is unchanged. |
| **RTC (optional)** | The Pi 5 has an RTC header. A coin cell keeps the clock correct before Wi-Fi/NTP — nice for the idle-clock display. |

---

## Part A — Flash the OS

**OS: Raspberry Pi OS (64-bit) — the _Lite_ image.** 64-bit is required on the
Pi 5 (it uses the `vc4-kms-v3d` graphics stack `cage` needs).

1. Install **Raspberry Pi Imager** (<https://www.raspberrypi.com/software/>) and
   insert the microSD card.
2. In Imager:
   - **Device:** Raspberry Pi 5
   - **OS:** Raspberry Pi OS (other) → **Raspberry Pi OS Lite (64-bit)**
   - **Storage:** your microSD card
3. Click **Next → Edit Settings** and set:
   - **Hostname:** `carlyric` (so `carlyric.local` resolves on the network)
   - **Username:** `fuwenxu` + a password (keep `fuwenxu` to match the paths)
   - **Wi-Fi:** your SSID, password, and country
   - **Services tab:** enable **SSH** (password authentication)
4. **Write** the card, then insert it into the Pi 5.
5. Connect the display to **HDMI0**, apply the **5A USB-C** power, and boot.
6. After ~1 minute, SSH in from your computer:

   ```bash
   ssh fuwenxu@carlyric.local
   ```

   If `.local` doesn't resolve, use the Pi's IP from your router.

---

## Part B — Install dependencies

```bash
sudo apt update && sudo apt full-upgrade -y

sudo apt install -y python3-dbus-next python3-pygame fonts-noto-cjk \
                    cage seatd python3-requests bluez bluez-tools git
```

`cage` is the single-app Wayland kiosk compositor; `seatd` gives it a seat
without a full desktop; `fonts-noto-cjk` provides Chinese glyphs.

Enable `seatd` at boot (on Lite you must do this yourself, or `cage` fails with a
`libseat`/seat error):

```bash
sudo systemctl enable --now seatd
```

---

## Part C — Get the code

Install `git` first, then clone:

```bash
sudo apt install git -y
git clone https://github.com/xiabo-lab/lyrics.git ~/carlyrics
cd ~/carlyrics
```

---

## Part D — Install the support pieces

**1. Bluetooth auto-pairing agent** (lets you pair phones from the touchscreen):

```bash
sudo cp ~/carlyrics/bt-agent.service /etc/systemd/system/
sudo systemctl enable --now bt-agent
```

**2. Stop the stray cursor** (the phone's AVRCP channel looks like a mouse):

```bash
sudo cp ~/carlyrics/99-carlyric-ignore-avrcp-pointer.rules /etc/udev/rules.d/
sudo udevadm control --reload && sudo udevadm trigger
```

**3. (Optional) password-less service restarts:**

```bash
sudo cp ~/carlyrics/carlyric-claude.sudoers /etc/sudoers.d/carlyric
sudo chmod 440 /etc/sudoers.d/carlyric
```

---

## Part E — Create the main service

The GUI needs a graphical seat (cage), so it can't run over plain SSH — it runs
from systemd. Create the unit:

```bash
sudo nano /etc/systemd/system/carlyric.service
```

Paste this exactly. **The paths below are for user `fuwenxu`. If your username
is different, replace `fuwenxu` in the `ExecStart` line — never leave a
`<pi-user>` placeholder there, or the service loops with
`can't open file '/home/<pi-user>/carlyrics/Lyrics_Display.py'`.**

```ini
[Unit]
Description=Car Lyrics Display (cage + pygame scroller)
After=systemd-user-sessions.service getty@tty1.service bluetooth.service
Wants=bluetooth.service
Conflicts=getty@tty1.service

[Service]
User=root
PAMName=login
TTYPath=/dev/tty1
StandardInput=tty
StandardOutput=journal
StandardError=journal
Environment=XDG_RUNTIME_DIR=/tmp
ExecStart=/usr/bin/cage -s -- /usr/bin/python3 /home/fuwenxu/carlyrics/Lyrics_Display.py
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
```

> **Why the `tty1` binding matters (Pi 5).** At boot the kernel framebuffer
> console owns the HDMI output (DRM master). A plain background service can't take
> it, so `cage` starts but never presents — you'd see `Could not make device fd
> drm master: Device or resource busy` and endless `Swapchain … failed test`.
> `Conflicts=getty@tty1.service` + `TTYPath=/dev/tty1` + `StandardInput=tty` +
> `PAMName=login` make `cage` the **active VT session on `tty1`**, so the kernel
> hands it DRM master cleanly. Don't add any `WLR_*` env vars — not needed here.

> **`Restart=on-failure` + the `Esc` key.** With a keyboard attached, `Esc` exits
> the kiosk cleanly and it stays stopped (a real crash still auto-recovers).
> Manage the Pi over SSH — VT switching (`Ctrl+Alt+F2`) is blocked under cage on
> this board. **Do not add `OnSuccess=getty@tty1.service`**: it also fires during
> every `systemctl restart`, collides with the `tty1` hand-off, and breaks
> "Update Firmware" / manual restarts.

Save (`Ctrl+O`, `Enter`, `Ctrl+X`), then enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now carlyric.service
journalctl -u carlyric.service -f      # watch it start
```

A healthy start logs `[config] …` and `[display] <W> x <H>`, and the lyrics
appear on the HDMI display. Press `Ctrl+C` to stop watching the log (the service
keeps running).

**Sanity-check the path if it's looping:**

```bash
grep ExecStart /etc/systemd/system/carlyric.service   # must show YOUR real home dir
```

---

## Part F — Pair your phone & verify

1. On the Pi screen, **long-press 10 s** → **Settings → Bluetooth → Pair New
   Phone**.
2. On your phone, pair with **`carlyric`** just like a Bluetooth speaker.
3. Play a song — lyrics should scroll in sync. Tune offsets from the on-screen
   menu or `config.json` (see [README_Pi_Zero_2W.md](README_Pi_Zero_2W.md) for
   every key).

**✅ That completes the required setup (Parts A–F).** If the lyrics scroll in
sync, you're done. **Everything below is optional or for troubleshooting only** —
skip it unless something isn't working or you want to migrate settings.

---

# Optional & troubleshooting (skip unless you need it)

## After you press `Esc` (expected behaviour, not a fault)

> **The local console looks broken — that's expected.** Two things happen once
> `cage` releases the screen:
>
> - **Undervoltage messages** may scroll by, e.g. `hwmon hwmon3: Undervoltage
>   detected!`. These were happening all along — `cage` just hid the console. They
>   mean the Pi 5 isn't getting a full 5V/5A; fix the supply/cable (a 27W USB-C PD
>   adapter + a 5A-rated cable). Check with `vcgencmd get_throttled` (`0x0` = fine,
>   `0x10000` = undervoltage occurred since boot). Don't suppress the warning.
> - **The keyboard does nothing.** The unit binds `tty1`
>   (`Conflicts=getty@tty1.service`), so with `cage` stopped there is no login
>   shell on `tty1`, and VT switching (`Ctrl+Alt+F2`) is blocked — by design you
>   manage this Pi over **SSH**. Recover with `ssh fuwenxu@carlyric.local` then
>   `sudo systemctl restart carlyric.service` (brings the display back), or
>   `sudo systemctl start getty@tty1` for a local login prompt. No other computer?
>   Power-cycle — it boots straight back into the display.

## Display gotchas (Pi 5) — only if the screen is black or wrong

Almost every "black screen / `Swapchain … failed test`" comes down to **something
other than `cage` owning the HDMI output.** In order of how often they bite:

### 1. The desktop is stealing the screen (most common)

If you flashed the **"with desktop"** image, `labwc` (started by `lightdm`) holds
DRM master and fights `cage`. Symptom: nonstop `Swapchain for output 'HDMI-A-1'
failed test`, black screen. Switch to console — no reflash needed:

```bash
systemctl get-default                 # if 'graphical.target', the desktop is on
sudo systemctl set-default multi-user.target
sudo systemctl enable --now seatd     # the desktop used to start this for you
sudo reboot
```

Confirm nothing else holds the GPU:

```bash
sudo fuser -v /dev/dri/card*          # should list only cage (+seatd), no labwc/Xorg
```

### 2. The console holds DRM master at boot

Even with no desktop, the framebuffer console owns `tty1` at boot: `Could not make
device fd drm master: Device or resource busy`. The **Part E unit fixes this** via
the `tty1` binding. If you wrote the unit without those lines, a manual run works
but boot doesn't — add them.

### 3. pygame / SDL picks the wrong video backend

The app logs `[display] <W> x <H> (SDL video driver: …)`.

- **Correct:** `[display] 1920 x 440 (SDL video driver: x11)` — SDL runs through
  cage's Xwayland. **Leave SDL on its default; do _not_ set
  `SDL_VIDEODRIVER=wayland`** (on this trixie/SDL build the native Wayland driver
  reports *"did not add any displays"* or hangs in `set_mode()`).
- **`[display] 1 x 1`** — you're hitting gotcha #1 (a second compositor is up).
  Fix the desktop conflict, not SDL.

### Harmless log lines (ignore these)

A healthy boot still prints these — they are **not** the problem:

- `[EGL] … eglQueryDeviceStringEXT … EGL_BAD_PARAMETER` — a query V3D doesn't
  support. This is the warning you'll see on every boot; safe to ignore.
- `xkbcomp … Unsupported maximum keycode 708, clipping` — keymap warning.
- `xwayland/xwm.c … Failed to get window property` / `xcb error …
  ConfigureWindow` — cosmetic Xwayland startup noise.

### Things that do *not* help (don't waste time)

- **`WLR_DRM_NO_ATOMIC` / `WLR_DRM_NO_MODIFIERS` / `WLR_NO_HARDWARE_CURSORS`** —
  only *silence* the swapchain error; the output still never reaches the app.
- **`WLR_RENDERER=vulkan`** — fails with `Could not match drm and vulkan device`.
- **`WLR_RENDERER=pixman` / `SDL_VIDEODRIVER=kmsdrm`** — both fail to start here.
- **Forcing an HDMI mode in `cmdline.txt`** — the bar panel's native mode is
  **1920×440**; forcing another mode gives a black screen. Let KMS pick the EDID
  mode; remove any `video=…` you added.

---

## (Optional) Carry over tuning from another Pi

To bring existing tuning and confirmed lyrics instead of starting blank, run from
your computer with the other Pi powered on:

```bash
# pull from the old Pi
scp    fuwenxu@<old-ip>:~/carlyrics/config.json     ./
scp -r fuwenxu@<old-ip>:~/carlyrics/cache           ./
scp    fuwenxu@<old-ip>:~/carlyrics/rejections.json ./   # if it exists

# push to the Pi 5
scp    ./config.json     fuwenxu@carlyric.local:~/carlyrics/
scp -r ./cache           fuwenxu@carlyric.local:~/carlyrics/
scp    ./rejections.json fuwenxu@carlyric.local:~/carlyrics/
```

Then restart the service on the Pi 5:

```bash
sudo systemctl restart carlyric.service
```

> **Two Pis on one network:** if both are powered at once they'll both want the
> hostname `carlyric`. mDNS disambiguates (e.g. `carlyric-2.local`), but it's
> cleaner to keep only one powered during setup.

---

## Updating later

From the screen: long-press → **Software Version → Update Firmware**, or manually:

```bash
cd ~/carlyrics && git pull && sudo systemctl restart carlyric.service
```

Your `config.json`, `cache/`, and `rejections.json` are preserved across updates.
