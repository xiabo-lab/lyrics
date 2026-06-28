# carlyrics

A car karaoke lyric display for the Raspberry Pi. Your phone plays music over
Bluetooth; the Pi reads the playback position and scrolls time-synced lyrics on a
screen mounted in the car — no app on the phone, nothing to tap while driving.

## Setup guides

Pick the guide for your board:

- **Raspberry Pi Zero 2W** (the tested platform) → **[README_Pi_Zero_2W.md](README_Pi_Zero_2W.md)**
- **Raspberry Pi 5** → **[README_Pi5.md](README_Pi5.md)**

> ⚠️ Use a Raspberry Pi OS **Lite** (console-only, 64-bit) image. A "with desktop"
> image runs its own Wayland compositor (labwc) that holds the screen and fights
> `cage`, so the lyrics never appear.
