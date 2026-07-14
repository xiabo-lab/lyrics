"""Phase 4: AVRCP listener. Prints Track + Position from the paired iPhone.

Deploy to ~/carlyrics/phase4_avrcp.py on the Pi. Run without sudo (the user
must be in the 'bluetooth' group, which bookworm sets up by default for the
default user; if not: `sudo usermod -aG bluetooth fuwenxu` then re-login).

Prereqs on the Pi:
    sudo apt install -y python3-dbus-next
    # iPhone already paired + trusted via bluetoothctl (see session log).

What it does:
- Watches BlueZ ObjectManager for any org.bluez.MediaPlayer1 interface.
- When one appears (iPhone connects + starts playing), subscribes to its
  PropertiesChanged signal.
- Prints Track dict changes (Title/Artist/Album/Duration) and Position ticks.

This script is read-only — it does NOT route audio. The iPhone's A2DP audio
keeps flowing to the car stereo as before.
"""
import asyncio
from dbus_next.aio import MessageBus
from dbus_next import BusType, Variant

BLUEZ = "org.bluez"
MP_IFACE = "org.bluez.MediaPlayer1"
PROPS_IFACE = "org.freedesktop.DBus.Properties"
OM_IFACE = "org.freedesktop.DBus.ObjectManager"


def _unwrap(v):
    """dbus-next gives Variants; recursively pull the plain Python value out."""
    if isinstance(v, Variant):
        return _unwrap(v.value)
    if isinstance(v, dict):
        return {k: _unwrap(val) for k, val in v.items()}
    if isinstance(v, list):
        return [_unwrap(item) for item in v]
    return v


def _fmt_track(track: dict) -> str:
    if not track:
        return "(no track)"
    artist = track.get("Artist", "?")
    title = track.get("Title", "?")
    album = track.get("Album", "")
    dur_ms = track.get("Duration", 0)
    dur_s = dur_ms // 1000 if isinstance(dur_ms, int) else 0
    return f"{artist} — {title}" + (f" [{album}]" if album else "") + f"  ({dur_s}s)"


class AvrcpWatcher:
    def __init__(self, bus: MessageBus):
        self.bus = bus
        self.player_path: str | None = None
        self.last_track_sig: tuple = (None, None)

    async def start(self):
        # Walk current objects, then subscribe to add/remove for late connects.
        introspection = await self.bus.introspect(BLUEZ, "/")
        root = self.bus.get_proxy_object(BLUEZ, "/", introspection)
        om = root.get_interface(OM_IFACE)

        objects = await om.call_get_managed_objects()
        for path, ifaces in objects.items():
            if MP_IFACE in ifaces:
                await self._attach(path, ifaces[MP_IFACE])

        om.on_interfaces_added(self._on_added)
        om.on_interfaces_removed(self._on_removed)
        print("[avrcp] watching for MediaPlayer1…")

    def _on_added(self, path: str, ifaces: dict):
        if MP_IFACE in ifaces:
            asyncio.create_task(self._attach(path, ifaces[MP_IFACE]))

    def _on_removed(self, path: str, ifaces: list):
        if MP_IFACE in ifaces and path == self.player_path:
            print(f"[avrcp] player gone: {path}")
            self.player_path = None
            self.last_track_sig = (None, None)

    async def _attach(self, path: str, initial_props: dict):
        print(f"[avrcp] player appeared: {path}")
        self.player_path = path

        intro = await self.bus.introspect(BLUEZ, path)
        obj = self.bus.get_proxy_object(BLUEZ, path, intro)
        props = obj.get_interface(PROPS_IFACE)

        # Print current state once.
        self._handle_props(_unwrap(initial_props))

        def on_changed(iface: str, changed: dict, invalidated: list):
            if iface != MP_IFACE:
                return
            self._handle_props(_unwrap(changed))

        props.on_properties_changed(on_changed)

    def _handle_props(self, changed: dict):
        if "Track" in changed:
            track = changed["Track"] or {}
            sig = (track.get("Title"), track.get("Artist"))
            if sig != self.last_track_sig:
                self.last_track_sig = sig
                print(f"[track]    {_fmt_track(track)}")
        if "Status" in changed:
            print(f"[status]   {changed['Status']}")
        if "Position" in changed:
            ms = changed["Position"]
            print(f"[position] {ms // 1000}.{ms % 1000:03d}s")


async def main():
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    watcher = AvrcpWatcher(bus)
    await watcher.start()
    # Park forever; signals fire callbacks.
    await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
