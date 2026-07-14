"""
Phase 1: prove the HDMI bar LCD works.
Draws "Hello, Car!" fullscreen, rotated 90° for the bar display.
Press Ctrl+C in the terminal to quit.
"""
import os
import time
import pygame

# Use the Linux framebuffer directly — no X/Wayland needed on Pi OS Lite.
pygame.init()
pygame.mouse.set_visible(False)

# Fullscreen at native resolution. The Waveshare 11.9" reports 320x1480.
screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
width, height = screen.get_size()
print(f"Display is {width} x {height}")

# A big font that should be readable from the driver's seat.
font = pygame.font.SysFont("DejaVuSans", 90, bold=True)

bg = (0, 0, 0)            # black background
fg = (255, 220, 80)       # warm yellow (easy on the eyes at night)

clock = pygame.time.Clock()
start = time.time()

try:
    while True:
        # Quit on any key, in case you hook up a keyboard.
        for event in pygame.event.get():
            if event.type == pygame.QUIT or event.type == pygame.KEYDOWN:
                raise KeyboardInterrupt

        screen.fill(bg)

        # Render text, then rotate 90° so it reads correctly on a landscape-mounted
        # bar LCD whose native orientation is portrait.
        text = font.render("Hello, Car!", True, fg)
        text = pygame.transform.rotate(text, 90)

        # Center it on the screen.
        rect = text.get_rect(center=(width // 2, height // 2))
        screen.blit(text, rect)

        pygame.display.flip()
        clock.tick(30)
except KeyboardInterrupt:
    pygame.quit()
