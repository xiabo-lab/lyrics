# carlyrics

A car karaoke lyric display for the Raspberry Pi. Your phone plays music over
Bluetooth; the Pi reads the playback position and scrolls time-synced lyrics on a
screen mounted in the car — no app on the phone, nothing to tap while driving.

The current line **fills with colour in time with the song**, word-by-word where
the source provides it (Kugou KRC / QQ QRC), so it reads like real karaoke. See
**[Word-level karaoke](../README.md#word-level-karaoke)** in the setup guide.

Everything below is set on the screen itself — long-press the display for 10
seconds to open Settings. Nothing needs a keyboard or a config file.

## Look and feel

- **Font Settings** — size, weight and colour for the top / current / bottom
  lines, plus the karaoke fill colour. Eight colours, including Black and Brown
  for use over a light backdrop.
- **Background Picture** — the lyric screen's backdrop is either a solid colour
  (Black, White, Grey, Light Brown, Sky Blue) or a picture from `Wallpaper/`, with
  an optional slideshow (30 min – 4 h) and a **Dim picture** control for busy
  or bright pictures. Drop your own images into `Wallpaper/` and they appear in the
  picker without a restart — no update overwrites them.
  > Pictures are scaled to **fill** the panel and centre-cropped, so match your
  > panel's aspect ratio (the screen tells you its exact size) or the top and
  > bottom of the image are trimmed.
- Lyrics are drawn with an **outline**, so they stay readable over any picture,
  and the backdrop dims along with the text at night.

## Setup guides

The application code is identical across boards — only the OS-setup steps differ.
Pick the guide for yours:

- **Raspberry Pi 5 — Lite OS** (the primary, supported build; tested on a Pi 5
  1GB) → **[README.md](../README.md)** (the repo's main page)
- **Raspberry Pi 5 — full "with desktop" OS** (tested on a Pi 5 8GB) →
  **[README_Pi5.md](README_Pi5.md)**
- **Raspberry Pi Zero 2W** → **[README_Pi_Zero_2W.md](README_Pi_Zero_2W.md)**

> ⚠️ `cage` must be the only thing driving the screen. The **Lite** (console-only,
> 64-bit) image gives that out of the box; on a "with desktop" image you switch
> the Pi to boot to console (the Pi 5 full-OS guide covers this). Either way, if a
> desktop compositor (labwc) keeps running, the lyrics never appear.
