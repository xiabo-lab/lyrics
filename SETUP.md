# carlyrics — First-Time Setup Guide

Welcome! This guide walks you through setting up your **carlyrics** car lyric
display for the first time, one screen at a time. Everything is done on the
touchscreen — you never need a keyboard, mouse, or computer.

---

## Before you start

- **It turns on by itself.** When the Pi gets power, the app launches
  automatically. For the first few seconds you'll see a short **road-safety
  notice** — please only operate the settings while parked.
- **When no music is playing**, the screen shows a large **clock**.
- **When music plays** on your phone (works best with **YouTube Music**), the
  lyrics appear automatically and scroll in time with the song.

### Opening the Settings menu

> **Press and hold one finger still on the screen for about 5 seconds.**

The **Settings** menu appears with these buttons, top to bottom:

1. Font Settings
2. Background Picture
3. Bluetooth
4. Other Settings
5. Network
6. Software Version
7. **Close** (green — returns you to the lyrics/clock)

Tap any item to open it. Each screen has a **Back** button to return to this
menu.

### 🚀 Quick start — do these first

If you only do two things, do these:

1. **Bluetooth** → pair your phone (Step 3) — so the display can see your music.
2. **Network** → join your Wi‑Fi (Step 5) — so it can look up lyrics online.

Everything else below is about making it look and feel the way you like.

---

## Step 1 — Font Settings

This is where you set the **size, color, and boldness** of the on-screen text.
There are four rows, top to bottom:

| Row | What it controls |
|-----|------------------|
| **Karaoke fill (sung words)** | The color that "fills in" each word as it's sung. Color only — it uses the Current line's size/boldness. |
| **Top line** | The *previous* lyric line (context above the current one). |
| **Current line** | The line being sung right now — the biggest, most important line. |
| **Bottom line** | The *next* lyric line (context below). |

For each of the three "line" rows you can set:

- **Size** — drag the slider left/right (roughly 20–160 px). The label shows the
  live pixel size, e.g. *"Current line — 56px"*.
- **Color** — tap one of the 8 swatches: **Yellow, Green, White, Red, Blue,
  Purple, Black, Brown**. The selected one gets a white outline. (Black and
  Brown are there for light backgrounds.)
- **Bold / Normal** — the big button on the right toggles the weight.

**Tips**
- Changes **preview live** as you make them.
- Tap **Done** (green) to save. Settings are remembered after a restart.
- Suggested starting point: make the **Current line** large and bold, the Top
  and Bottom lines smaller.

---

## Step 2 — Background Picture

Choose what's shown *behind* the lyrics and clock. (Menus always stay on a plain
dark background so the controls remain readable.)

At the top, pick a **mode**:

### Solid Colour
Tap a color: **Black, White, Grey, Light Brown, Sky Blue.** Simple and always
readable — a good default.

### Picture
Show a photo/wallpaper behind the text. The display can switch pictures
automatically by time of day:

- **Day** row — shown from **6:00 AM to 5:59 PM**
- **Night** row — shown from **6:00 PM to 5:59 AM**

Each row shows a strip of thumbnails (use the **‹ ›** arrows to page through
more). **Tap a thumbnail in the Day row** to set your daytime picture, and **tap
one in the Night row** for nighttime. The row that's active right now is tagged
**"(now)"**, and your chosen picture in each row gets a white box around it.

> The display switches between your Day and Night pictures on its own at 6 AM and
> 6 PM — you don't have to do anything.

**Dim picture** — a **±** stepper (0–60%) that darkens the picture *only* (not the
text), so lyrics stay easy to read over a bright or busy photo.

**Adding your own pictures:** drop image files into the **`Wallpaper/`** folder on
the Pi. They look best at the screen's own resolution; other sizes are scaled to
fill and center-cropped. Your images are never overwritten by a firmware update.

Changes here **save immediately** — just tap **Back** when done.

---

## Step 3 — Bluetooth (connect your phone)

This is how the display learns which phone to read the music from.

1. Tap **Pair New Phone**. The button turns amber and the Pi becomes
   *discoverable* for a short while.
2. On **your phone**, open **Settings → Bluetooth** and tap the display's name
   in the device list. (Its name is shown on the **Software Version** screen, next
   to *"Bluetooth name"*.)
3. Accept the pairing request if your phone asks.
4. Once paired, the phone appears in the list on this screen. A
   **"• connected"** tag shows when it's actively linked.

**Notes**
- After the first pairing, the display **reconnects to your phone automatically**
  whenever it's nearby and unlocked — you normally never touch this screen again.
- If your phone is locked, iOS may refuse the connection; it links up once you
  unlock/use the phone.
- To remove a phone, tap its **Forget** button.

---

## Step 4 — Other Settings

Four adjustments for how the display behaves:

| Setting | What it does |
|---------|--------------|
| **Rotate Screen 180°** (Yes/No) | Flip everything upside-down — use this if the screen is mounted the other way up. |
| **Bluetooth A2DP Offset** (0.0s–3.0s, steps of 0.1s) | Compensates for audio delay over Bluetooth. If lyrics run **ahead** of the sound, increase this. |
| **Lyrics Timing Offset** (−3.0s to +3.0s, steps of 0.5s) | Fine nudge for lyric timing. **Negative** = show lyrics earlier; **positive** = later. |
| **Auto Dim** (Yes/No) | Automatically dims the whole display at night so it isn't glaring while driving. |

Use the **±** buttons for the sliders and the **Yes/No** button for the toggles.
Everything saves immediately; tap **Back** when done.

> **Tip:** If lyrics feel slightly out of sync, first try **Lyrics Timing
> Offset** in small steps. Use **Bluetooth A2DP Offset** only if there's a
> consistent gap between what you *hear* and what you *see*.

---

## Step 5 — Network (join Wi-Fi)

The display needs the internet to look up lyrics. This screen shows:

- **Wi-Fi:** the network you're connected to (or *"Not connected"*)
- **IP address**
- **Internet:** **Online** (green) / **Offline** (red) / **Checking…** (amber)

To connect:

1. Tap **Wi-Fi Setup**.
2. Pick your Wi-Fi network from the list.
3. Enter the password using the **on-screen keyboard**.
4. Confirm — the display joins the network and remembers it for next time.

Tap **Check now** any time to re-test whether it's online. Tap **Back** when done.

> If you see *"Online"* in green, you're all set — lyrics will be fetched
> automatically.

---

## Step 6 — Software Version (clock format & updates)

This screen shows the app **version** and the display's **Bluetooth name**, and
has two buttons:

### Clock format
Tap the **Clock** button to switch the idle-screen clock between:

- **24-hour** — `13:05:07` (`HH:MM:SS`)
- **12-hour** — `1:05 PM` (`HH:MM AM/PM`)

It toggles each time you tap and is remembered after a restart.

### Update Firmware
Pulls the latest version of the app from the internet and restarts.

1. Tap **Update Firmware** — it arms and asks you to confirm.
2. Tap **again** to confirm. The display downloads the update and restarts on its
   own.

> Make sure you're **Online** (Step 5) before updating.

---

## Day-to-day use

- **Play music on your phone** (YouTube Music recommended) — lyrics appear and
  follow the song automatically. No music → the big **clock** is shown.
- **If the lyrics for a song look wrong or are missing**, on-screen feedback
  buttons let you fix it:
  - **Green** — confirm the current lyrics are correct (saves them for next time).
  - **Red** — open a picker to choose a different match or **search manually**
    (with an on-screen keyboard, including pinyin input for Chinese titles).
  - **Triple-tap** — remove the saved lyric for the current song so it's looked
    up fresh.
- **To change any setting later**, just hold a finger on the screen for ~5
  seconds to reopen the menu.

---

## Troubleshooting quick reference

| Problem | Try this |
|---------|----------|
| No lyrics, only the clock | Make sure music is **playing** and the phone is **connected** (Step 3). |
| "Offline" / no lyrics found | Rejoin **Wi-Fi** (Step 5), then tap **Check now**. |
| Lyrics slightly out of sync | Adjust **Lyrics Timing Offset** (Step 4) in small steps. |
| Screen is upside-down | Turn on **Rotate Screen 180°** (Step 4). |
| Text hard to read on a photo | Increase **Dim picture** (Step 2) or pick a **Solid Colour** background. |
| Phone won't reconnect | Unlock the phone; if needed, **Forget** and **Pair** again (Step 3). |

---

*Enjoy your carlyrics display — and please only adjust settings while parked.*
