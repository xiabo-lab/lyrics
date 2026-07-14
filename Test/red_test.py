"""Red screen test — proves pygame can draw under cage."""
import os
import time
import pygame


pygame.init()
pygame.mouse.set_visible(False)

screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
w, h = screen.get_size()
print(f"Display is {w} x {h}", flush=True)

# Fill red, flip, hold 10 seconds, exit.
screen.fill((255, 0, 0))
pygame.display.flip()
print("Flipped to red — holding 10s", flush=True)
time.sleep(10)

pygame.quit()
