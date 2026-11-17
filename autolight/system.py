"""Hardware and desktop access: the ALS, the panel and keyboard LED, lid/AC state, idle time.

Everything that touches the machine lives here so that the daemon and the engine can be driven by a
fake in tests. Paths are relative to a root (default "/") so tests can point at a fake sysfs tree.
Gio/GLib (python3-gi) is only imported by the real D-Bus helpers, never at module import.
"""
from __future__ import annotations

import glob
import logging
import math
import os
import time
from typing import Optional

LOG = logging.getLogger("autolight")

LOGIND = "org.freedesktop.login1"


class SensorError(Exception):
    """The ambient light sensor is missing or returned nonsense."""


class WriteError(Exception):
    """A brightness write was refused."""


def _read(path: str) -> str:
    with open(path, "r") as f:
        return f.read().strip()


class Paths:
    def __init__(self, root: str = "/", backlight: str = "acpi_video0", led: str = "spi::kbd_backlight"):
        j = lambda p: os.path.join(root, p.lstrip("/"))
        self.iio = j("/sys/bus/iio/devices")
        self.backlight_name, self.led_name = backlight, led
        self.backlight = j(f"/sys/class/backlight/{backlight}")
        self.led = j(f"/sys/class/leds/{led}")
        self.power_supply = j("/sys/class/power_supply")
        self.lid_glob = j("/proc/acpi/button/lid/*/state")


# The iBridge sensor hub powers the ALS down 3 s after the last read (runtime autosuspend), and the
# first readings after wake-up are far too low for about a second (measured: 38, 57, 127, 155, 161 ...).
WARM_GAP = 2.5


class Als:
    """IIO ambient light sensor found by its `name` file (the iio:deviceN number changes whenever the
    T1 iBridge re-enumerates, e.g. after suspend)."""

    def __init__(self, iio_dir: str, name: str = "als", sleep=time.sleep, clock=time.monotonic):
        self.dir, self.name = iio_dir, name
        self.sleep, self.clock = sleep, clock
        self.dev: Optional[str] = None
        self.next_scan = 0.0
        self.last_ok: Optional[float] = None

    def _find(self) -> Optional[str]:
        for d in sorted(glob.glob(os.path.join(self.dir, "iio:device*"))):
            try:
                if _read(os.path.join(d, "name")) == self.name:
                    return d
            except OSError:
                continue
        return None

    def read(self) -> float:
        """Lux. Raises SensorError (rescans at most every 5 s while the device is missing)."""
        now = self.clock()
        if self.dev is None:
            if now < self.next_scan:
                raise SensorError("no %r sensor" % self.name)
            self.next_scan = now + 5.0
            self.dev = self._find()
            if self.dev is None:
                raise SensorError("no iio device named %r" % self.name)
            LOG.info("light sensor: %s", self.dev)
        try:
            v = self._lux(self.dev)
            if self.last_ok is None or now - self.last_ok > WARM_GAP:
                v = self._settle(v)
        except (OSError, ValueError) as e:
            self.dev = None
            self.last_ok = None
            raise SensorError(str(e)) from e
        self.last_ok = self.clock()
        return v

    def _settle(self, first: float) -> float:
        """The sensor was asleep: wait (<= ~1.6 s, once) until two reads in a row agree."""
        prev = first
        for _ in range(8):
            self.sleep(0.2)
            v = self._lux(self.dev)
            if abs(v - prev) <= max(2.0, 0.06 * max(v, prev)):
                return v
            prev = v
        return prev

    @staticmethod
    def _lux(d: str) -> float:
        # Never read *_hysteresis_relative: on this hub it logs "No report with id 0xffffffff".
        if os.path.exists(os.path.join(d, "in_illuminance_input")):
            v = float(_read(os.path.join(d, "in_illuminance_input")))
        else:
            raw = float(_read(os.path.join(d, "in_illuminance_raw")))
            try:
                scale = float(_read(os.path.join(d, "in_illuminance_scale")))
            except OSError:
                scale = 1.0
            try:
                offset = float(_read(os.path.join(d, "in_illuminance_offset")))
            except OSError:
                offset = 0.0
            v = (raw + offset) * scale
        if not math.isfinite(v) or v < 0:
            raise ValueError("bad illuminance %r" % v)
        return v


class Level:
    """A sysfs brightness device (backlight or LED)."""

    def __init__(self, path: str):
        self.path = path

    def exists(self) -> bool:
        return os.path.isfile(os.path.join(self.path, "max_brightness"))

    def max(self) -> int:
        try:
            return int(_read(os.path.join(self.path, "max_brightness")))
        except (OSError, ValueError):
            return 0

    def read(self) -> Optional[int]:
        for name in ("actual_brightness", "brightness"):
            try:
                return int(_read(os.path.join(self.path, name)))
            except (OSError, ValueError):
                continue
        return None


class SysfsWriter:
    """Direct writes: only for tests and for runs as root. The service uses logind instead."""

    def __init__(self, paths: Paths):
        self.paths = paths

    def set(self, subsystem: str, name: str, value: int) -> None:
        base = self.paths.backlight if subsystem == "backlight" else self.paths.led
        try:
            with open(os.path.join(base, "brightness"), "w") as f:
                f.write(str(int(value)))
        except OSError as e:
            raise WriteError(str(e)) from e


class Logind:
    """org.freedesktop.login1.Session.SetBrightness: lets the logged-in user set backlight and LED
    levels without root (logind does the privileged sysfs write)."""

    def __init__(self):
        from gi.repository import Gio, GLib
        self.Gio, self.GLib = Gio, GLib
        self.bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        self.session: Optional[str] = None

    def _call(self, path: str, iface: str, method: str, params):
        return self.bus.call_sync(LOGIND, path, iface, method, params, None, self.Gio.DBusCallFlags.NONE, 2000, None)

    def _find_session(self) -> str:
        """`session/auto` works from a login session; a systemd user service has none, so fall back
        to the user's primary ("display") session."""
        auto = "/org/freedesktop/login1/session/auto"
        try:
            self._call(auto, "org.freedesktop.DBus.Properties", "Get",
                       self.GLib.Variant("(ss)", (LOGIND + ".Session", "Id")))
            return auto
        except self.GLib.Error:
            pass
        user =self._call("/org/freedesktop/login1", LOGIND + ".Manager", "GetUser", self.GLib.Variant("(u)", (os.getuid(),)))
        upath = user.unpack()[0]
        disp = self._call(upath, "org.freedesktop.DBus.Properties", "Get",
                          self.GLib.Variant("(ss)", (LOGIND + ".User", "Display"))).unpack()[0]
        if not disp or not disp[1] or disp[1] == "/":
            raise WriteError("no login session to set brightness through")
        return disp[1]

    def set(self, subsystem: str, name: str, value: int) -> None:
        try:
            if self.session is None:
                self.session = self._find_session()
            self._call(self.session, LOGIND + ".Session", "SetBrightness",
                       self.GLib.Variant("(ssu)", (subsystem, name, int(value))))
        except self.GLib.Error as e:
            self.session = None
            raise WriteError(e.message) from e


class SessionBus:
    """Cinnamon bits: idle time (Muffin), idle inhibitors (session manager), Cinnamon's idle-dim settings."""

    def __init__(self):
        from gi.repository import Gio, GLib
        self.Gio, self.GLib = Gio, GLib
        self.bus = None
        self.next_try = 0.0
        self.settings = None
        self.csd_cache = (0.0, None, False)   # (when, on_ac, value)

    def _bus(self):
        if self.bus is None and time.monotonic() >= self.next_try:
            try:
                self.bus = self.Gio.bus_get_sync(self.Gio.BusType.SESSION, None)
            except self.GLib.Error:
                self.next_try = time.monotonic() + 30.0
        return self.bus

    def _call(self, dest: str, path: str, iface: str, method: str, params=None):
        bus = self._bus()
        if bus is None:
            raise OSError("no session bus")
        try:
            return bus.call_sync(dest, path, iface, method, params, None, self.Gio.DBusCallFlags.NONE, 1000, None).unpack()
        except self.GLib.Error as e:
            raise OSError(e.message) from e

    def idle_s(self) -> Optional[float]:
        try:
            return self._call("org.cinnamon.Muffin.IdleMonitor", "/org/cinnamon/Muffin/IdleMonitor/Core",
                              "org.cinnamon.Muffin.IdleMonitor", "GetIdletime")[0] / 1000.0
        except OSError:
            return None

    def inhibited(self) -> bool:
        try:
            return bool(self._call("org.gnome.SessionManager", "/org/gnome/SessionManager",
                                   "org.gnome.SessionManager", "IsInhibited", self.GLib.Variant("(u)", (8,)))[0])
        except OSError:
            return False

    def csd_idle_dim(self, on_ac: bool) -> bool:
        """Does Cinnamon's own idle dimming apply right now (depends on AC vs battery)?"""
        now = time.monotonic()
        if now - self.csd_cache[0] < 10.0 and self.csd_cache[1] == on_ac:
            return self.csd_cache[2]
        val = False
        schema = "org.cinnamon.settings-daemon.plugins.power"
        src = self.Gio.SettingsSchemaSource.get_default()
        if src is not None and src.lookup(schema, True) is not None:
            if self.settings is None:
                self.settings = self.Gio.Settings.new(schema)
            val = self.settings.get_boolean("idle-dim-ac" if on_ac else "idle-dim-battery")
        self.csd_cache = (now, on_ac, val)
        return val


class Hardware:
    """The real thing, as the daemon sees it."""

    def __init__(self, paths: Paths, als_name: str, writer, session=None):
        self.paths = paths
        self.als = Als(paths.iio, als_name)
        self.panel = Level(paths.backlight)
        self.leds = Level(paths.led)
        self.writer = writer
        self.session = session

    def read_lux(self) -> float:
        return self.als.read()

    def screen_max(self) -> int:
        return self.panel.max()

    def kbd_max(self) -> int:
        return self.leds.max()

    def read_screen(self) -> Optional[int]:
        return getattr(self.writer, "vals", {}).get("backlight", self.panel.read())

    def read_kbd(self) -> Optional[int]:
        return getattr(self.writer, "vals", {}).get("leds", self.leds.read())

    def set_screen(self, level: int) -> None:
        self.writer.set("backlight", self.paths.backlight_name, level)

    def set_kbd(self, level: int) -> None:
        self.writer.set("leds", self.paths.led_name, level)

    def idle_s(self) -> Optional[float]:
        return self.session.idle_s() if self.session else None

    def inhibited(self) -> bool:
        return self.session.inhibited() if self.session else False

    def lid_closed(self) -> bool:
        for p in glob.glob(self.paths.lid_glob):
            try:
                return "closed" in _read(p)
            except OSError:
                pass
        return False

    def on_ac(self) -> bool:
        for p in glob.glob(os.path.join(self.paths.power_supply, "*")):
            try:
                if _read(os.path.join(p, "type")) == "Mains" and _read(os.path.join(p, "online")) == "1":
                    return True
            except OSError:
                continue
        return False

    def csd_dims(self) -> bool:
        return bool(self.session and self.session.csd_idle_dim(self.on_ac()))
