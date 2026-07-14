"""Low-level touchscreen reader — reads raw evdev events straight from the
kernel input device, bypassing cage/XWayland/SDL entirely. Confirms the
panel + driver actually deliver touches. Runs over plain SSH (no GUI), and
alongside the live service (evdev broadcasts to all readers).

Usage:
    python3 touch_evtest.py [/dev/input/eventN] [seconds]
Defaults to /dev/input/event4 (the ILITEK touch panel) for 30s.
Needs read access to the device — fuwenxu is in the 'input' group, so no sudo.
"""
import select
import struct
import sys
import time

DEV = sys.argv[1] if len(sys.argv) > 1 else "/dev/input/event4"
DURATION = float(sys.argv[2]) if len(sys.argv) > 2 else 30.0

EV_SYN, EV_KEY, EV_ABS = 0x00, 0x01, 0x03
BTN_TOUCH = 0x14a
ABS_X, ABS_Y = 0x00, 0x01
ABS_MT_X, ABS_MT_Y = 0x35, 0x36

# Native sizes match the kernel's struct input_event on this platform
# (24 bytes on 64-bit, 16 on 32-bit) — timeval(2 longs) + u16 + u16 + s32.
FMT = "@llHHi"
SZ = struct.calcsize(FMT)

print(f"[evtest] reading {DEV} for {DURATION:.0f}s (event size {SZ}). TAP NOW.")
try:
    f = open(DEV, "rb", buffering=0)
except PermissionError:
    print("[evtest] permission denied — need 'input' group or sudo")
    sys.exit(1)
except FileNotFoundError:
    print(f"[evtest] {DEV} not found — replug may have changed the eventN")
    sys.exit(1)

end = time.monotonic() + DURATION
downs = syns = 0
last = {}
abs_seen, key_seen = set(), set()
while time.monotonic() < end:
    r, _, _ = select.select([f], [], [], 0.5)
    if not r:
        continue
    data = f.read(SZ)
    if not data or len(data) < SZ:
        continue
    _sec, _usec, etype, code, value = struct.unpack(FMT, data)
    if etype == EV_KEY and code == BTN_TOUCH:
        key_seen.add("BTN_TOUCH")
        if value == 1:
            downs += 1
            print(f"[evtest] TOUCH DOWN  #{downs}  xy={last}")
        else:
            print("[evtest] touch up")
    elif etype == EV_ABS:
        if code in (ABS_X, ABS_MT_X):
            last["x"] = value
            abs_seen.add("X")
        elif code in (ABS_Y, ABS_MT_Y):
            last["y"] = value
            abs_seen.add("Y")
    elif etype == EV_SYN:
        syns += 1

print(f"[evtest] DONE. touch-downs={downs} syn-reports={syns} "
      f"abs_axes={sorted(abs_seen)} keys={sorted(key_seen)}")
if downs == 0 and syns == 0:
    print("[evtest] NO events — taps are NOT reaching the kernel device.")
else:
    print("[evtest] HARDWARE OK — touch events are being delivered.")
