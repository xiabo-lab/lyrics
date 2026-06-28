# carlyrics on Raspberry Pi 5

Step-by-step setup for running the car lyric display on a **Raspberry Pi 5
(8GB)** with a fresh microSD card. The application code is identical to the Pi
Zero 2W build — **no source changes are required.** The only differences are the
OS image you flash and a few physical details (HDMI port, power, cooling).

These instructions assume the Linux user `fuwenxu` and the install path
`~/carlyrics`, matching the rest of the project. If you use a different
username, see *Changing the username* in the main [README](README.md) and adjust
the paths below.

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

**OS: Raspberry Pi OS (64-bit), Bookworm.** The "with desktop" image is fine.
64-bit Bookworm is required on the Pi 5 (it uses the Wayland / `vc4-kms-v3d`
graphics stack that `cage` needs).

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

---

## Part C — Get the code

```bash
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
   menu or `config.json` (see the main README for every key).

---

## Part G — (Optional) Carry over tuning from the Pi Zero

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
