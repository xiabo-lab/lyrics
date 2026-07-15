# carlyrics

A car karaoke lyric display for the Raspberry Pi. Your phone plays music over
Bluetooth; the Pi reads the playback position and scrolls time-synced lyrics on a
screen mounted in the car — no app on the phone, nothing to tap while driving.

The current line **fills with colour in time with the song**, word-by-word where
the source provides it (Kugou KRC / QQ QRC), so it reads like real karaoke. See
**Word-level karaoke** in the [Pi Zero 2W guide](README_Pi_Zero_2W.md).

## Setup guides

Pick the guide for your board:

- **Raspberry Pi Zero 2W** (the tested platform) → **[README_Pi_Zero_2W.md](README_Pi_Zero_2W.md)**
- **Raspberry Pi 5 — full "with desktop" OS** (tested on Pi 5 8GB) → **[README_Pi5.md](README_Pi5.md)**
- **Raspberry Pi 5 — Lite OS** (tested on Pi 5 1GB, fewer steps) → **[README_Pi5_Lite.md](README_Pi5_Lite.md)**

> ⚠️ `cage` must be the only thing driving the screen. The **Lite** (console-only,
> 64-bit) image gives that out of the box; on a "with desktop" image you switch
> the Pi to boot to console (the Pi 5 full-OS guide covers this). Either way, if a
> desktop compositor (labwc) keeps running, the lyrics never appear.
