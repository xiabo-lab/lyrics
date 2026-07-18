#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""On-screen Wi-Fi setup for the carlyrics touchscreen (Settings → Network → Wi-Fi Setup).

Ported from the Pi_dashboard project: the nmcli logic is shared verbatim, the UI
is reimplemented in pygame to match the lyric display's Settings menus. Connects
via nmcli so the change persists in NetworkManager and reconnects on boot; the
service runs as root, so nmcli needs no sudo.

The UI is a small state machine:
    SCANNING -> LIST -> PASSWORD -> CONNECTING -> RESULT
It is drawn in LOGICAL (pre-FLIP_180) space and hit-tested in the same space,
exactly like draw_network / the other menus, so taps line up after the frame is
flipped. All blocking nmcli work runs in daemon threads; the render loop just
reads the fields (guarded by a lock) and redraws every frame.
"""
import math
import subprocess
import threading
import time

import pygame

# UI states
SCANNING, LIST, PASSWORD, CONNECTING, RESULT = "scan", "list", "pw", "connecting", "result"

WIFI_DEVICE = "wlan0"

# Palette — matches the other Settings screens (draw_network / draw_main_menu).
_BG = (0, 0, 0)
_FG = (235, 235, 235)
_MUTED = (150, 150, 150)
_SLATE = (45, 45, 58)
_BLUE = (40, 90, 140)
_GREEN = (40, 120, 50)
_RED = (150, 40, 40)
_AMBER = (150, 110, 30)
_FIELD = (48, 48, 62)
_OK = (120, 210, 120)
_ALERT = (225, 110, 110)


# --- nmcli helpers (shared with Pi_dashboard) --------------------------------

def _split_terse(line):
    """Split an `nmcli -t` line on unescaped ':' (nmcli escapes ':' and '\\')."""
    out, cur, i = [], [], 0
    while i < len(line):
        c = line[i]
        if c == "\\" and i + 1 < len(line):
            cur.append(line[i + 1]); i += 2; continue
        if c == ":":
            out.append("".join(cur)); cur = []; i += 1; continue
        cur.append(c); i += 1
    out.append("".join(cur))
    return out


def scan_networks(rescan=False):
    """Return [{'ssid','signal','secure'}], strongest first, deduped."""
    if rescan:
        subprocess.run(["nmcli", "device", "wifi", "rescan"],
                       capture_output=True, timeout=20)
        time.sleep(2)
    try:
        out = subprocess.run(
            ["nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY", "dev", "wifi", "list"],
            capture_output=True, text=True, timeout=15).stdout
    except Exception as e:
        print(f"[wifi] scan failed: {e}")
        return []

    best = {}
    for line in out.splitlines():
        if not line:
            continue
        parts = _split_terse(line)
        ssid = parts[0].strip()
        if not ssid:  # hidden network, no usable name
            continue
        try:
            signal = int(parts[1]) if len(parts) > 1 and parts[1] else 0
        except ValueError:
            signal = 0
        secure = bool(parts[2].strip()) if len(parts) > 2 else True
        if ssid not in best or signal > best[ssid]["signal"]:
            best[ssid] = {"ssid": ssid, "signal": signal, "secure": secure}
    return sorted(best.values(), key=lambda n: n["signal"], reverse=True)


def current_ssid():
    try:
        out = subprocess.run(["nmcli", "-t", "-f", "active,ssid", "dev", "wifi"],
                             capture_output=True, text=True, timeout=8).stdout
        for line in out.splitlines():
            parts = _split_terse(line)
            if len(parts) >= 2 and parts[0] == "yes":
                return parts[1]
    except Exception:
        pass
    return None


def _delete_profiles_for_ssid(ssid):
    """Delete every saved connection bound to this SSID, whatever its name.

    A stale/incomplete profile for the target network makes `nmcli dev wifi
    connect` reuse it and fail with "802-11-wireless-security.key-mgmt: property
    is missing". These profiles are often not named after the SSID (e.g.
    netplan-created `netplan-wlan0-<ssid>`), so we match on the actual SSID
    field, not the connection name.
    """
    try:
        out = subprocess.run(["nmcli", "-t", "-f", "NAME,TYPE", "con", "show"],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return
    for line in out.splitlines():
        parts = _split_terse(line)
        if len(parts) < 2 or parts[1] != "802-11-wireless":
            continue
        name = parts[0]
        try:
            info = subprocess.run(
                ["nmcli", "-t", "-f", "802-11-wireless.ssid", "con", "show", name],
                capture_output=True, text=True, timeout=10).stdout.strip()
        except Exception:
            continue
        info_parts = _split_terse(info)  # ['802-11-wireless.ssid', '<ssid>']
        prof_ssid = info_parts[1] if len(info_parts) >= 2 else ""
        if prof_ssid == ssid or name == ssid:
            subprocess.run(["nmcli", "con", "delete", name],
                           capture_output=True, timeout=10)


def connect(ssid, password, secure):
    """Blocking nmcli connect. Returns (ok, message)."""
    # Clear any stale profile for this SSID first so we always build a fresh,
    # complete one. (The other saved networks remain, so this can't strand the
    # Pi offline if the new password is wrong.)
    _delete_profiles_for_ssid(ssid)
    cmd = ["nmcli", "dev", "wifi", "connect", ssid]
    if secure and password:
        cmd += ["password", password]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
    except subprocess.TimeoutExpired:
        return False, "Timed out"
    if r.returncode == 0:
        return True, "Connected"
    err = (r.stderr or r.stdout or "Failed").strip().splitlines()
    msg = err[-1] if err else "Failed"
    return False, msg.replace("Error: ", "")[:60]


# --- keyboard layout ---------------------------------------------------------

_ROWS_ABC = ("1234567890", "qwertyuiop", "asdfghjkl", "zxcvbnm")
_ROWS_SYM = ("1234567890", "!@#$%^&*()", "-_=+[]{};:", "'\",.?/\\|~")


class WifiSetup:
    """Full-screen Wi-Fi setup overlay, drawn onto the pygame screen surface.

    Pass the display's get_font(size, bold) so the module needs no import back
    into Lyrics_Display. handle_tap() takes LOGICAL (pre-flip) coordinates and
    returns 'back' to leave the overlay, else None."""

    def __init__(self, w, h, get_font):
        self.w, self.h = w, h
        self._get_font = get_font
        self.state = SCANNING
        self.networks = []
        self.selected = None               # chosen network dict
        self.password = ""
        self.show_pw = False
        self.shift = False
        self.symbols = False
        self.result_ok = False
        self.result_msg = ""
        self.current = None
        self._hits = []                    # [(x0, y0, x1, y1, action)]
        self._lock = threading.Lock()
        self._spin = 0
        # Fresh rescan on entry so the list is complete, not just NM's cache.
        threading.Thread(target=self._scan_thread, args=(True,), daemon=True).start()

    def _font(self, px, bold=False):
        return self._get_font(max(12, int(px)), bold)

    # --- background work ---
    def _scan_thread(self, rescan):
        nets = scan_networks(rescan)
        cur = current_ssid()
        with self._lock:
            self.networks = nets
            self.current = cur
            if self.state == SCANNING:
                self.state = LIST

    def _connect_thread(self):
        ok, msg = connect(self.selected["ssid"], self.password,
                          self.selected["secure"])
        cur = current_ssid()
        with self._lock:
            self.result_ok, self.result_msg = ok, msg
            self.current = cur
            self.state = RESULT

    def _begin_connect(self):
        self.state = CONNECTING
        threading.Thread(target=self._connect_thread, daemon=True).start()

    # --- input ---
    def handle_tap(self, x, y):
        """Process a tap. Returns 'back' to leave Wi-Fi setup, else None."""
        action = None
        for x0, y0, x1, y1, act in self._hits:
            if x0 <= x <= x1 and y0 <= y <= y1:
                action = act
                break
        if action is None:
            return None

        if action == "cancel":
            return "back"
        if action == "rescan":
            self.state = SCANNING
            threading.Thread(target=self._scan_thread, args=(True,),
                             daemon=True).start()
            return None
        if action.startswith("ssid:"):
            idx = int(action[5:])
            if 0 <= idx < len(self.networks):
                self.selected = self.networks[idx]
                if self.selected["secure"]:
                    self.password, self.shift, self.symbols = "", False, False
                    self.state = PASSWORD
                else:
                    self._begin_connect()
            return None
        if self.state == PASSWORD:
            return self._password_key(action)
        if self.state == RESULT:
            if action in ("ok", "done"):
                return "back" if self.result_ok else None
            if action == "retry":
                self.state = PASSWORD
        return None

    def _password_key(self, action):
        if action == "back":
            self.state = LIST
        elif action == "connect":
            if self.password:
                self._begin_connect()
        elif action == "shift":
            self.shift = not self.shift
        elif action == "sym":
            self.symbols = not self.symbols
        elif action == "space":
            self.password += " "
        elif action == "del":
            self.password = self.password[:-1]
        elif action == "show":
            self.show_pw = not self.show_pw
        elif len(action) == 1:
            ch = action.upper() if (self.shift and not self.symbols) else action
            self.password += ch
            self.shift = False   # one-shot shift, like a phone keyboard
        return None

    # --- drawing helpers ---
    def _text(self, screen, s, px, color, center=None, midtop=None,
              topleft=None, bold=False):
        surf = self._font(px, bold).render(s, True, color)
        if center is not None:
            screen.blit(surf, surf.get_rect(center=center))
        elif midtop is not None:
            screen.blit(surf, surf.get_rect(midtop=midtop))
        else:
            screen.blit(surf, topleft)
        return surf

    def _key(self, screen, rect, label, action, fill=_SLATE, fg=_FG):
        pygame.draw.rect(screen, fill, rect, border_radius=8)
        self._text(screen, label, int(rect.h * 0.5), fg, center=rect.center)
        self._hits.append((rect.x, rect.y, rect.right, rect.bottom, action))

    def _button(self, screen, rect, label, action, kind="normal"):
        fill = {"ok": _GREEN, "alert": _RED, "accent": _BLUE}.get(kind, _SLATE)
        px = min(int(rect.h * 0.42), max(20, self.h // 17))
        pygame.draw.rect(screen, fill, rect, border_radius=10)
        self._text(screen, label, px, _FG, center=rect.center)
        self._hits.append((rect.x, rect.y, rect.right, rect.bottom, action))

    def _draw_lock(self, screen, cx, cy, color):
        """Small padlock drawn with primitives (the font lacks a lock glyph)."""
        bw, bh = 16, 13
        pygame.draw.rect(screen, color, pygame.Rect(cx - bw // 2, cy - 1, bw, bh),
                         border_radius=2)
        sr = 5
        pygame.draw.arc(screen, color,
                        pygame.Rect(cx - sr, cy - 1 - sr * 2, sr * 2, sr * 2 + 2),
                        0, math.pi, 2)

    # --- render (call every frame) ---
    def draw(self, screen):
        with self._lock:
            state = self.state
        self._hits = []
        w, h = self.w, self.h
        screen.fill(_BG)
        pad = max(20, int(w * 0.012))
        self._text(screen, "Wi-Fi Setup", int(h * 0.09), _FG,
                   topleft=(pad, int(h * 0.03)), bold=True)
        cur = f"Connected: {self.current}" if self.current else "Not connected"
        self._text(screen, cur, int(h * 0.05), _OK if self.current else _MUTED,
                   midtop=(w - pad - 120, int(h * 0.05)))

        if state == SCANNING:
            self._center(screen, "Scanning for networks" + "." * (self._spin // 8 % 4))
        elif state == LIST:
            self._render_list(screen, pad)
        elif state == PASSWORD:
            self._render_password(screen, pad)
        elif state == CONNECTING:
            self._center(screen, f"Connecting to {self.selected['ssid']}"
                                 + "." * (self._spin // 8 % 4))
        elif state == RESULT:
            self._render_result(screen)
        self._spin += 1

    def _center(self, screen, text):
        self._center_at(screen, text, self.h // 2, int(self.h * 0.09), _FG)

    def _center_at(self, screen, text, cy, px, color, bold=False):
        self._text(screen, text, px, color, center=(self.w // 2, cy), bold=bold)

    def _render_list(self, screen, pad):
        w, h = self.w, self.h
        top = int(h * 0.16)
        rowh, gap = int(h * 0.11), max(4, int(h * 0.015))
        # Footer buttons (fixed position).
        bh = int(h * 0.14)
        by = h - bh - int(h * 0.02)
        bw = max(160, int(w * 0.14))
        self._button(screen, pygame.Rect(pad, by, bw, bh), "Rescan", "rescan", "accent")
        self._button(screen, pygame.Rect(w - pad - bw, by, bw, bh), "Back", "cancel")
        # Network rows.
        avail = by - gap - top
        maxrows = max(1, avail // (rowh + gap))
        small = int(rowh * 0.5)
        for i, net in enumerate(self.networks[:maxrows]):
            y0 = top + i * (rowh + gap)
            row = pygame.Rect(pad, y0, w - 2 * pad, rowh)
            pygame.draw.rect(screen, _SLATE, row, border_radius=8)
            mark = "  • current" if net["ssid"] == self.current else ""
            self._text(screen, net["ssid"] + mark, small,
                       _OK if net["ssid"] == self.current else _FG,
                       topleft=(row.x + 16, row.centery - small // 2))
            sig = f"{net['signal']}%"
            sig_surf = self._font(int(rowh * 0.42)).render(sig, True, _MUTED)
            sx = row.right - 20 - sig_surf.get_width()
            screen.blit(sig_surf, (sx, row.centery - sig_surf.get_height() // 2))
            if net["secure"]:
                self._draw_lock(screen, sx - 22, row.centery, _MUTED)
            self._hits.append((row.x, row.y, row.right, row.bottom, f"ssid:{i}"))
        if not self.networks:
            self._center(screen, "No networks found — tap Rescan")

    def _render_password(self, screen, pad):
        w, h = self.w, self.h
        self._text(screen, f"Password for  {self.selected['ssid']}",
                   int(h * 0.06), _FG, topleft=(pad, int(h * 0.13)))
        # Password field + Show/Back on its row.
        fy = int(h * 0.22)
        fh = int(h * 0.11)
        bw = max(120, int(w * 0.09))
        field = pygame.Rect(pad, fy, w - 2 * pad - 2 * bw - 24, fh)
        pygame.draw.rect(screen, _FIELD, field, border_radius=8)
        pygame.draw.rect(screen, _BLUE, field, width=2, border_radius=8)
        shown = self.password if self.show_pw else "•" * len(self.password)
        self._text(screen, shown or " ", int(fh * 0.5), _FG,
                   topleft=(field.x + 12, field.centery - int(fh * 0.28)))
        self._button(screen, pygame.Rect(field.right + 12, fy, bw, fh),
                     "Hide" if self.show_pw else "Show", "show")
        self._button(screen, pygame.Rect(field.right + 12 + bw + 12, fy, bw, fh),
                     "Back", "back")
        # Keyboard.
        self._render_keyboard(screen, pad, top=int(h * 0.37), bottom=h - int(h * 0.02))

    def _render_keyboard(self, screen, pad, top, bottom):
        w = self.w
        rows = _ROWS_SYM if self.symbols else _ROWS_ABC
        n_rows = len(rows) + 1
        gy = max(4, int((bottom - top) * 0.03))
        kh = (bottom - top - (n_rows - 1) * gy) // n_rows
        y = top
        for row in rows:
            n = len(row)
            gx = 8
            kw = (w - 2 * pad - (n - 1) * gx) // n
            row_w = n * kw + (n - 1) * gx
            x = (w - row_w) // 2
            for ch in row:
                label = ch.upper() if (self.shift and not self.symbols and ch.isalpha()) else ch
                self._key(screen, pygame.Rect(x, y, kw, kh), label, ch)
                x += kw + gx
            y += kh + gy
        # Bottom function row: proportional widths.
        specs = [("ABC" if self.symbols else "?123", "sym", 1.4),
                 ("Shift", "shift", 1.4),
                 ("Space", "space", 3.0),
                 ("Del", "del", 1.4),
                 ("Connect", "connect", 2.2)]
        gx = 8
        total_u = sum(u for _l, _a, u in specs)
        avail = w - 2 * pad - (len(specs) - 1) * gx
        x = pad
        for label, action, u in specs:
            kw = int(avail * (u / total_u))
            kind = "ok" if action == "connect" else \
                   "accent" if (action == "shift" and self.shift) else "normal"
            self._button(screen, pygame.Rect(x, y, kw, kh), label, action, kind)
            x += kw + gx

    def _render_result(self, screen):
        w, h = self.w, self.h
        ok = self.result_ok
        self._center_at(screen, "Connected!" if ok else "Could not connect",
                        int(h * 0.28), int(h * 0.10),
                        _OK if ok else _ALERT, bold=True)
        detail = (f"{self.selected['ssid']}  —  {self.current}" if ok
                  else self.result_msg)
        self._center_at(screen, detail, int(h * 0.46), int(h * 0.055), _MUTED)
        bh = int(h * 0.14)
        by = h - bh - int(h * 0.03)
        if ok:
            self._button(screen, pygame.Rect(w // 2 - 130, by, 260, bh),
                         "Done", "done", "ok")
        else:
            self._button(screen, pygame.Rect(w // 2 - 270, by, 250, bh),
                         "Retry", "retry", "accent")
            self._button(screen, pygame.Rect(w // 2 + 20, by, 250, bh),
                         "Back", "cancel")
