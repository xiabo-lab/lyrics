"""Standalone touchscreen tester for carlyrics (Pi Zero 2 W + cage).

WHY: confirm the touchscreen delivers events through SDL/pygame the SAME way
Lyrics_Display.py expects them — i.e. as FINGERDOWN and/or touch-emulated
MOUSEBUTTONDOWN. It draws a marker wherever you touch and prints every input
event to the console, so you can verify the whole chain: panel → kernel HID →
libinput → cage (Wayland) → XWayland → SDL → pygame.

PREREQ: a touch device must actually exist. Check first (no GUI needed):
    lsusb                          # the USB touch controller should be listed
    cat /proc/bus/input/devices    # look for a touchscreen with ABS_MT_* axes
    sudo apt install -y evtest && sudo evtest   # tap → events stream = HW OK
If those show nothing, the panel's USB touch lead isn't connected to the Pi's
USB DATA port — fix that before this test can do anything.

RUN ON THE PI (needs a graphical seat via cage; plain SSH has none):
    sudo systemctl stop carlyric.service
    sudo XDG_RUNTIME_DIR=/tmp cage -s -- \
        env PATH=$PATH python3 /home/fuwenxu/carlyrics/touch_test.py
    # tap around the screen; watch the markers AND the console lines.
    # press ESC or Ctrl+C in the terminal to quit, then:
    sudo systemctl start carlyric.service

READING IT:
- A green ✚ + "FINGERDOWN" line  → native touch events (best case).
- A blue  ✚ + "MOUSEBUTTONDOWN, touch=True" → touch arrives as emulated mouse.
  Either one means the real app's tap handler will fire.
- Marker NOT under your finger → touch/display orientation mismatch (the real
  app inverts coords when flip_180 is on; this raw test does NOT, so a 180°
  offset here is expected if the panel is mounted upside-down).
- NOTHING in the console on tap → SDL isn't getting touch; it's a driver/seat
  issue, not our code. Re-check evtest.
"""
import os
import time

import pygame

# We don't need audio; silence the ALSA probe like the main app does.
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")


def main() -> None:
    pygame.init()
    screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
    w, h = screen.get_size()
    pygame.mouse.set_visible(True)
    font = pygame.font.Font(None, 56)
    small = pygame.font.Font(None, 30)
    print(f"[touchtest] display {w} x {h}")
    print("[touchtest] tap the screen — events print here. ESC / Ctrl+C to quit.")

    # The same edge zones the real app uses (half-width buttons), so you can
    # check the actual button areas are reachable.
    bw = max(55, int(w * 0.035))

    markers: list[tuple[float, float, tuple, float]] = []  # x, y, color, expire
    taps = 0
    finger_events = 0
    mouse_events = 0
    last = "waiting for first touch…"

    clock = pygame.time.Clock()
    running = True
    while running:
        for e in pygame.event.get():
            if e.type == pygame.QUIT:
                running = False
            elif e.type == pygame.KEYDOWN and e.key == pygame.K_ESCAPE:
                running = False
            elif e.type == pygame.FINGERDOWN:
                x, y = e.x * w, e.y * h
                taps += 1
                finger_events += 1
                last = (f"FINGERDOWN  norm=({e.x:.3f},{e.y:.3f})  "
                        f"px=({x:.0f},{y:.0f})  finger={e.finger_id}")
                print(f"[touchtest] {last}")
                markers.append((x, y, (0, 255, 0), time.monotonic() + 3))
            elif e.type == pygame.FINGERMOTION:
                markers.append((e.x * w, e.y * h, (0, 110, 0),
                                time.monotonic() + 0.4))
            elif e.type == pygame.MOUSEBUTTONDOWN:
                taps += 1
                mouse_events += 1
                is_touch = getattr(e, "touch", False)
                last = (f"MOUSEBUTTONDOWN  px={e.pos}  button={e.button}  "
                        f"touch={is_touch}")
                print(f"[touchtest] {last}")
                markers.append((e.pos[0], e.pos[1], (0, 180, 255),
                                time.monotonic() + 3))

        now = time.monotonic()
        markers = [m for m in markers if m[3] > now]

        screen.fill((0, 0, 0))
        # Edge button zones (green left, red right) for reach-testing.
        for x0, col in ((0, (0, 160, 0, 90)), (w - bw, (200, 0, 0, 90))):
            band = pygame.Surface((bw, h), pygame.SRCALPHA)
            band.fill(col)
            screen.blit(band, (x0, 0))

        for x, y, color, _ in markers:
            pygame.draw.circle(screen, color, (int(x), int(y)), 30, 4)
            pygame.draw.line(screen, color, (x - 36, y), (x + 36, y), 2)
            pygame.draw.line(screen, color, (x, y - 36), (x, y + 36), 2)

        screen.blit(font.render(f"taps: {taps}", True, (255, 255, 0)),
                    (w // 2 - 90, 16))
        hud = f"FINGER:{finger_events}  MOUSE:{mouse_events}"
        screen.blit(small.render(hud, True, (160, 160, 160)), (16, 16))
        screen.blit(small.render(last, True, (210, 210, 210)), (16, h - 36))
        pygame.display.flip()
        clock.tick(60)

    pygame.quit()
    print(f"[touchtest] done. taps={taps} finger={finger_events} "
          f"mouse={mouse_events}")


if __name__ == "__main__":
    main()
