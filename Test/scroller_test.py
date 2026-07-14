"""
Phase 3: scroll fetched lyrics on screen, driven by a fake clock.

Layout (rotated 90° for bar LCD; centered for monitor testing):
  - previous line in dim grey, smaller
  - current line in bright yellow, large
  - next line in dim grey, smaller

Press Ctrl+C in the terminal to quit (or any key in the window).
"""
import os
import time
import pygame

# Silence ALSA noise — we don't need sound.
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

from lrclib import fetch_synced_lyrics, parse_lrc

TRACK = "Bohemian Rhapsody"
ARTIST = "Queen"

# Colors
BG = (0, 0, 0)
CURRENT = (255, 220, 80)   # warm yellow
CONTEXT = (110, 110, 110)  # dim grey

# Rotate for the bar LCD. Set to 0 if you want to read it directly on a normal monitor.
ROTATION_DEG = 0

def find_current_index(lines, t_ms: int) -> int:
    """Return the index of the line whose timestamp <= t_ms, or -1 if before first."""
    idx = -1
    for i, line in enumerate(lines):
        if line.time_ms <= t_ms:
            idx = i
        else:
            break
    return idx

def render_centered(screen, font, text, color, center_xy, rotate_deg):
    """Render text, optionally rotate, blit centered at center_xy."""
    surf = font.render(text, True, color)
    if rotate_deg:
        surf = pygame.transform.rotate(surf, rotate_deg)
    rect = surf.get_rect(center=center_xy)
    screen.blit(surf, rect)

def main():
    print(f"Fetching {TRACK!r} by {ARTIST!r}...")
    lrc = fetch_synced_lyrics(TRACK, ARTIST)
    if not lrc:
        print("No synced lyrics — aborting.")
        return
    lines = parse_lrc(lrc)
    print(f"Loaded {len(lines)} lines. Starting playback clock...")

    pygame.init()
    pygame.mouse.set_visible(False)
    screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
    w, h = screen.get_size()
    print(f"Display is {w} x {h}")

    font_current = pygame.font.SysFont("DejaVuSans", 40, bold=True)
    font_context = pygame.font.SysFont("DejaVuSans", 24)

    clock = pygame.time.Clock()
    start = time.monotonic()

    # When rotated 90°, the previous/next lines stack on the X axis (since rotation
    # swaps the visual axes). Pick offsets so the 3 lines space out nicely.
    if ROTATION_DEG in (90, 270):
        prev_pos = (w // 2 - 200, h // 2)
        curr_pos = (w // 2,       h // 2)
        next_pos = (w // 2 + 200, h // 2)
    else:
        prev_pos = (w // 2, h // 2 - 100)
        curr_pos = (w // 2, h // 2)
        next_pos = (w // 2, h // 2 + 100)

    try:
        while True:
            for event in pygame.event.get():
                if event.type in (pygame.QUIT, pygame.KEYDOWN):
                    raise KeyboardInterrupt

            elapsed_ms = int((time.monotonic() - start) * 1000)
            idx = find_current_index(lines, elapsed_ms)

            screen.fill(BG)

            if idx < 0:
                render_centered(screen, font_context, "(intro)", CONTEXT,
                                curr_pos, ROTATION_DEG)
            else:
                if idx - 1 >= 0:
                    render_centered(screen, font_context, lines[idx - 1].text,
                                    CONTEXT, prev_pos, ROTATION_DEG)
                render_centered(screen, font_current, lines[idx].text,
                                CURRENT, curr_pos, ROTATION_DEG)
                if idx + 1 < len(lines):
                    render_centered(screen, font_context, lines[idx + 1].text,
                                    CONTEXT, next_pos, ROTATION_DEG)

            pygame.display.flip()
            clock.tick(30)
    except KeyboardInterrupt:
        pass
    finally:
        pygame.quit()

if __name__ == "__main__":
    main()
