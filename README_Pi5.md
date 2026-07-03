# carlyrics on Raspberry Pi 5

Step-by-step setup for running the car lyric display on a **Raspberry Pi 5
(8GB)** with a fresh microSD card. The application code is identical to the Pi
Zero 2W build — **no source changes are required.** The only differences are the
OS image you flash and a few physical details (HDMI port, power, cooling).

These instructions assume the Linux user `fuwenxu` and the install path
`~/carlyrics`, matching the rest of the project. If you use a different
username, see *Changing the username* in [README_Pi_Zero_2W.md](README_Pi_Zero_2W.md) and adjust
the paths below.

> ### If your username isn't `fuwenxu`
>
> **The Python code needs no edits** — it resolves `config.json`, `cache/`, and
> `rejections.json` relative to the script itself
> (`Path(__file__).resolve().parent`), so it runs from any path. The `fuwenxu`
> strings inside the `.py` files are comments/example commands only.
>
> Only deployment details change. Substituting `pi5` as an example:
>
> | Where | Change | Required? |
> |-------|--------|-----------|
> | systemd unit `ExecStart` (Part E) | `/home/pi5/carlyrics/Lyrics_Display.py` | **Yes** — wrong path = service won't start |
> | `carlyric-claude.sudoers` (Part D-3) | leading `fuwenxu` → `pi5` | Only if you use password-less restarts |
> | clone path / `ssh` & `scp` host below | use your username + home dir | Yes (it's just where you log in / clone) |
> | `wifi.sh` hint, `.py` docstrings | cosmetic | No |
>
> Fix the repo files in one pass (run from the repo root on the Pi, after Part C):
>
> ```bash
> grep -rl fuwenxu . | xargs sed -i 's/fuwenxu/pi5/g'
> ```
>
> The systemd unit lives in `/etc/systemd/system/` (outside the repo), so set its
> path by hand in Part E.

---

## Hardware notes (Pi 5 vs. Pi Zero 2W)

| Item | What to do on the Pi 5 |
|------|------------------------|
| **HDMI** | Plug the bar display into **HDMI0** — the micro-HDMI port **nearest the USB-C power** connector. Needs a micro-HDMI cable/adapter. |
| **Power** | Use a **5V / 5A (25W) USB-C** supply. A car charger that ran the Zero will likely under-power the Pi 5 and cause low-voltage throttling — size the car power accordingly. |
| **Cooling** | Fit the **official active cooler or a case with a fan**. The Pi 5 runs hot and will throttle in a warm car under sustained load. |
| **Audio** | No 3.5mm jack on the Pi 5 — irrelevant here. The Pi is a Bluetooth A2DP *sink* feeding the car stereo and never decodes audio (`SDL_AUDIODRIVER=dummy`). No change needed. |
| **Bluetooth** | Onboard BT works the same as the Zero; the pairing / `bt-agent` / AVRCP flow is unchanged. |
| **RTC (optional)** | The Pi 5 has a real-time-clock header. A coin cell keeps the clock correct before Wi-Fi/NTP — nice for the idle-clock display. |

---

## Part A — Flash the OS

**OS: Raspberry Pi OS (64-bit) — use the _Lite_ image (no desktop).** 64-bit is
required on the Pi 5 (it uses the `vc4-kms-v3d` graphics stack that `cage`
needs).

> ⚠️ **Do not use the "with desktop" image.** Its desktop (the `labwc` Wayland
> compositor, auto-started by `lightdm`) grabs the HDMI output and holds DRM
> master, so `cage` can never own the screen — you get an endless
> `Swapchain for output 'HDMI-A-1' failed test` and a black display. `cage` is
> our compositor; it must be the *only* one. If you already flashed the desktop
> image, switch the Pi to boot to console instead of reflashing — see
> *Pi 5 display gotchas* below.

1. Install **Raspberry Pi Imager** (<https://www.raspberrypi.com/software/>) on
   your computer and insert the microSD card.
2. In Imager:
   - **Device:** Raspberry Pi 5
   - **OS:** Raspberry Pi OS (64-bit)
   - **Storage:** your microSD card
3. Click **Next → Edit Settings** (the OS customization dialog) and set:
   - **Hostname:** `carlyric` (so `carlyric.local` resolves on the network)
   - **Username:** `fuwenxu` + a password (keep `fuwenxu` to match the
     project's paths)
   - **Wi-Fi:** your SSID, password, and country
   - **Services tab:** enable **SSH** (password authentication)
4. **Write** the card. When finished, insert it into the Pi 5.
5. Connect the display to **HDMI0**, apply the **5A USB-C** power, and boot.
6. After ~1 minute, SSH in from your computer:

   ```bash
   ssh fuwenxu@carlyric.local
   ```

   If `.local` doesn't resolve, use the Pi's IP from your router instead.

---

## Part B — Install dependencies

```bash
sudo apt update && sudo apt full-upgrade -y

sudo apt install -y python3-dbus-next python3-pygame fonts-noto-cjk \
                    cage seatd python3-requests bluez bluez-tools git
```

`cage` is the single-app Wayland kiosk compositor; `seatd` gives it a seat
without a full desktop; `fonts-noto-cjk` provides Chinese glyphs.

Enable `seatd` so it starts at boot (on the desktop image the login manager
pulled it in for you; on Lite/console you must enable it yourself, or `cage`
fails with a `libseat`/seat error):

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

Paste exactly (paths already set for user `fuwenxu`):

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
> console owns the HDMI output (DRM master). A plain background service can't
> take it, so `cage` starts but can never present — you'd see
> `Could not make device fd drm master: Device or resource busy` and endless
> `Swapchain … failed test`. `Conflicts=getty@tty1.service` + `TTYPath=/dev/tty1`
> + `StandardInput=tty` + `PAMName=login` make `cage` the **active VT session on
> `tty1`**, so the kernel hands it DRM master cleanly. Don't add any `WLR_*` env
> vars — they're not needed and were a red herring on this hardware.

> **`Restart=on-failure` + the `Esc` key.** With a keyboard attached, `Esc`
> exits the kiosk cleanly; `Restart=on-failure` then leaves it stopped (a real
> crash still auto-recovers). Manage the Pi over SSH from there — VT switching
> (`Ctrl+Alt+F2`) is blocked under cage on this board. **Do not add
> `OnSuccess=getty@tty1.service`** to get a login prompt after `Esc`: it also
> fires during every `systemctl restart`, which collides with the `tty1`
> hand-off and **breaks "Update Firmware"** and manual restarts.

Save (`Ctrl+O`, `Enter`, `Ctrl+X`), then enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now carlyric.service
journalctl -u carlyric.service -f      # watch it start
```

A healthy start logs `[config] …` and `[display] <W> x <H>`, and the lyrics
screen appears on the HDMI display. Press `Ctrl+C` to stop watching the log (the
service keeps running).

---

## Part F — Pair your phone & verify

1. On the Pi screen, **long-press 10 s** → **Settings → Bluetooth → Pair New
   Phone**.
2. On your phone, pair with **`carlyric`** just like a Bluetooth speaker.
3. Play a song — lyrics should scroll in sync. Tune offsets from the on-screen
   menu or `config.json` (see [README_Pi_Zero_2W.md](README_Pi_Zero_2W.md) for every key).

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

## Pi 5 display gotchas (only if the screen is black or wrong)

Almost every "black screen / `Swapchain … failed test`" problem on the Pi 5 comes
down to **something other than `cage` owning the HDMI output**. `cage` must be the
only thing driving the display. In order of how often they bite:

### 1. The desktop is stealing the screen (most common)

If you flashed the **"with desktop"** image, `labwc` (the desktop's Wayland
compositor, started by `lightdm`) holds DRM master and fights `cage`. Symptom:
nonstop `Swapchain for output 'HDMI-A-1' failed test`, black screen.

Check and fix — boot to console instead of the desktop:

```bash
systemctl get-default                 # if 'graphical.target', the desktop is on
sudo systemctl set-default multi-user.target
sudo systemctl enable --now seatd     # desktop used to start this for you
sudo reboot
```

To confirm nothing else holds the GPU:

```bash
sudo fuser -v /dev/dri/card*          # should list only cage (+seatd), no labwc/Xorg
```

### 2. The console holds DRM master at boot

Even with no desktop, the kernel framebuffer console owns `tty1` at boot, so a
plain service can't get the display: `Could not make device fd drm master:
Device or resource busy`. The **Part E unit fixes this** by binding `cage` to
`tty1` (`Conflicts=getty@tty1.service`, `TTYPath`, `StandardInput=tty`,
`PAMName=login`). If you wrote the unit without those lines, the manual run works
but boot doesn't — add them.

### 3. pygame / SDL picks the wrong video backend

The app opens its window with `pygame.display.set_mode((0,0), FULLSCREEN)` and
logs the result: `[display] <W> x <H> (SDL video driver: …)`.

- **Correct:** `[display] 1920 x 440 (SDL video driver: x11)` — SDL runs through
  cage's Xwayland. **Leave SDL on its default; do _not_ set
  `SDL_VIDEODRIVER=wayland`.** On this trixie/SDL 2.32 build the native Wayland
  driver either reports *"The video driver did not add any displays"* or hangs
  forever inside `set_mode()` (black screen, only a cursor).
- **`[display] 1 x 1`** — you're hitting gotcha #1 (a second compositor is up),
  which corrupts Xwayland's screen size. Fix the desktop conflict, not SDL.

### Harmless log lines (ignore these)

A healthy boot still prints these — they are **not** the problem:

- `[EGL] … eglQueryDeviceStringEXT … EGL_BAD_PARAMETER` — a query V3D doesn't support.
- `xkbcomp … Unsupported maximum keycode 708, clipping` — keymap warning.
- `xwayland/xwm.c … Failed to get window property` / `xcb error … ConfigureWindow` — cosmetic Xwayland startup noise.

### Things that do *not* help (don't waste time)

- **`WLR_DRM_NO_ATOMIC` / `WLR_DRM_NO_MODIFIERS` / `WLR_NO_HARDWARE_CURSORS`** —
  these only *silence* the swapchain error; the output still never reaches the
  app. The real cause is always "who owns the display," above.
- **`WLR_RENDERER=vulkan`** — fails with `Could not match drm and vulkan device`
  (the Pi splits display=vc4 and render=v3d into separate DRM devices).
- **`WLR_RENDERER=pixman` / `SDL_VIDEODRIVER=kmsdrm`** — both fail to start here.
- **Forcing an HDMI mode in `cmdline.txt`** (e.g. `video=HDMI-A-1:1920x1080@60`)
  — the bar panel's native mode is **1920×440**; forcing a mode it can't show
  gives a black screen. Let KMS pick the EDID mode; remove any `video=…` you added.

---

## (Optional) Carry over tuning from the Pi Zero

To bring your existing tuning and confirmed lyrics instead of starting blank,
run these from your computer with the Zero powered on:

```bash
# pull from the Zero
scp    fuwenxu@<zero-ip>:~/carlyrics/config.json     ./
scp -r fuwenxu@<zero-ip>:~/carlyrics/cache           ./
scp    fuwenxu@<zero-ip>:~/carlyrics/rejections.json ./   # if it exists

# push to the Pi 5
scp    ./config.json     fuwenxu@carlyric.local:~/carlyrics/
scp -r ./cache           fuwenxu@carlyric.local:~/carlyrics/
scp    ./rejections.json fuwenxu@carlyric.local:~/carlyrics/
```

Then restart the service on the Pi 5:

```bash
sudo systemctl restart carlyric.service
```

(`config.json` hot-reloads within ~1 s, but a restart cleanly picks up the
copied cache.)

> **Two Pis on one network:** if both the Zero and the Pi 5 are powered at once
> they'll both want the hostname `carlyric`. mDNS will disambiguate (e.g.
> `carlyric-2.local`), but it's cleaner to keep only one powered during setup.

---

## Updating later

Same as the main build — from the screen: long-press → **Software Version →
Update Firmware**, or manually:

```bash
cd ~/carlyrics && git pull && sudo systemctl restart carlyric.service
```

Your `config.json`, `cache/`, and `rejections.json` are preserved across
updates.
