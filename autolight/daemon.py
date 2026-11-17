"""The autolight user daemon: sample the ALS, run the engine, write brightness, publish state.

`Daemon.tick(now)` does one full pass and returns how long to sleep, so tests can drive it with a
fake clock and fake hardware. `main()` wires up the real thing.

Bar channel (optional, best effort, never required): state JSON in $XDG_RUNTIME_DIR/barkeep-autolight,
control actions (names only, from a fixed list) read from the FIFO $XDG_RUNTIME_DIR/barkeep-autolight.ctl.
`autolightctl` uses the same FIFO, so every Touch Bar key is also a command-line action.
"""
from __future__ import annotations

import argparse
import errno
import json
import logging
import math
import os
import select
import signal
import stat
import sys
import time
from typing import Optional

from . import __version__, config as cfgmod
from .engine import Engine, Inputs
from .system import Hardware, Logind, Paths, SensorError, SessionBus, SysfsWriter, WriteError

LOG = logging.getLogger("autolight")

ACTIONS = ("toggle", "on", "off", "resume", "brighter", "dimmer", "bias-reset", "reload")
SENSOR_LOST_AFTER = 10.0
PUBLISH_EVERY = 30.0


def runtime_dir() -> str:
    return os.environ.get("XDG_RUNTIME_DIR") or "/tmp"


def state_dir() -> str:
    return os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "autolight")


def atomic_write(path: str, text: str, mode: int = 0o644) -> None:
    tmp = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class Daemon:
    def __init__(self, hw, cfg: dict, rt: str, persist: str, boot_clock=None, config_loader=None):
        self.hw, self.cfg = hw, cfg
        self.rt, self.persist_path = rt, persist
        self.boot_clock = boot_clock or (lambda: time.clock_gettime(time.CLOCK_BOOTTIME))
        self.config_loader = config_loader or cfgmod.load
        self.state_path = os.path.join(rt, "barkeep-autolight")
        self.fifo_path = self.state_path + ".ctl"
        self.engine = self._new_engine()
        self.last_sample = -1e9
        self.last_good: Optional[float] = None
        self.sensor_state = "unknown"
        self.last_clock: Optional[tuple[float, float]] = None
        self.inhibit_cache = (-1e9, False)
        self.published_key = None
        self.published_at = -1e9
        self.fifo: Optional[int] = None
        self.write_errors = 0
        self.dry_run = False

    def _new_engine(self) -> Engine:
        e = Engine(self.cfg, self.hw.screen_max(), self.hw.kbd_max())
        try:
            with open(self.persist_path) as f:
                saved = json.load(f)
            if isinstance(saved, dict):
                e.restore(saved)
        except (OSError, ValueError):
            pass
        return e

    # -- one pass -----------------------------------------------------------------------------
    def tick(self, now: float) -> float:
        cfg, eng = self.cfg, self.engine
        boot = self.boot_clock()
        resumed = bool(self.last_clock and (boot - self.last_clock[1]) - (now - self.last_clock[0]) > 3.0)
        self.last_clock = (now, boot)
        if resumed:
            LOG.info("resumed from sleep")
            self.last_sample = -1e9

        lux = None
        if now - self.last_sample >= cfg["sensor"]["sample_period"]:
            self.last_sample = now
            try:
                lux = self.hw.read_lux()
                self.last_good = now
                if self.sensor_state != "ok":
                    LOG.info("light sensor ok (%.0f lux)", lux)
                    self.sensor_state = "ok"
            except SensorError as e:
                if self.sensor_state != "lost" and (self.last_good is None or now - self.last_good >= SENSOR_LOST_AFTER):
                    LOG.warning("light sensor unavailable: %s (holding brightness)", e)
                    self.sensor_state = "lost"
        sensor_ok = self.last_good is not None and now - self.last_good < SENSOR_LOST_AFTER

        idle_s = self.hw.idle_s()
        idl = cfg["idle"]
        inhibited = False
        if idle_s is not None and idl["mode"] != "off" and idle_s >= idl["dim_after"] - 5.0:
            if now - self.inhibit_cache[0] > 2.0:
                self.inhibit_cache = (now, self.hw.inhibited())
            inhibited = self.inhibit_cache[1]

        writes = eng.update(Inputs(
            now=now, lux=lux, sensor_ok=sensor_ok, screen=self.hw.read_screen(), kbd=self.hw.read_kbd(),
            idle_s=idle_s, inhibited=inhibited, lid_closed=self.hw.lid_closed(),
            csd_dims=self.hw.csd_dims(), resumed=resumed))
        for name, level in writes:
            try:
                (self.hw.set_screen if name == "screen" else self.hw.set_kbd)(level)
                eng.wrote(name, level, now)
                self.write_errors = 0
            except WriteError as e:
                eng.failed(name, now)
                self.write_errors += 1
                if self.write_errors in (1, 10, 100):
                    LOG.warning("cannot set %s brightness: %s", name, e)
        self.publish(now)
        return eng.next_delay()

    # -- bar channel / persistence ------------------------------------------------------------
    def save_persisted(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.persist_path), exist_ok=True)
            atomic_write(self.persist_path, json.dumps(self.engine.persisted()), 0o600)
        except OSError as e:
            LOG.debug("cannot save state: %s", e)

    def publish(self, now: float, force: bool = False) -> None:
        snap = self.engine.snapshot(now)
        snap["ver"] = __version__
        s, k = snap["screen"], snap["kbd"]
        key = (snap["enabled"], snap["bias"], snap["sensor"], snap["lid"],
               s and (s["state"], s["pct"] // 5, s["resume_in"] and s["resume_in"] // 30),
               k and (k["state"], k["pct"] // 5),
               None if snap["lux"] is None else round(2 * math.log10(1 + snap["lux"])))
        if not force and key == self.published_key and now - self.published_at < PUBLISH_EVERY:
            return
        self.published_key, self.published_at = key, now
        snap["ts"] = int(time.time())
        try:
            atomic_write(self.state_path, json.dumps(snap, separators=(",", ":")))
        except OSError:
            pass          # the bar is optional: no bar, no runtime dir, no problem

    def open_fifo(self) -> None:
        try:
            try:
                if not stat.S_ISFIFO(os.stat(self.fifo_path).st_mode):
                    os.unlink(self.fifo_path)
            except FileNotFoundError:
                pass
            if not os.path.exists(self.fifo_path):
                os.mkfifo(self.fifo_path, 0o600)
            self.fifo = os.open(self.fifo_path, os.O_RDWR | os.O_NONBLOCK)
        except OSError as e:
            LOG.warning("control channel unavailable (%s); autolightctl will not work", e)
            self.fifo = None

    def read_actions(self) -> list[str]:
        out: list[str] = []
        if self.fifo is None:
            return out
        try:
            data = os.read(self.fifo, 4096)
        except OSError as e:
            if e.errno not in (errno.EAGAIN, errno.EINTR):
                LOG.debug("fifo read: %s", e)
            return out
        for line in data.decode("ascii", "ignore").splitlines():
            line = line.strip().lower()
            if line in ACTIONS:         # names only, never executed or interpolated
                out.append(line)
        return out

    def handle(self, action: str, now: float) -> None:
        if action == "reload":
            self.reload()
            return
        LOG.info("control: %s", action)
        if self.engine.command(now, action):
            self.save_persisted()
        self.publish(now, force=True)

    def reload(self) -> None:
        cfg = self.config_loader()
        persisted = self.engine.persisted()
        self.cfg = cfg
        self.engine = self._new_engine()
        self.engine.restore(persisted)
        LOG.info("configuration reloaded")

    # -- real run loop ------------------------------------------------------------------------
    def run(self) -> int:
        stop = []
        signal.signal(signal.SIGTERM, lambda *a: stop.append(1))
        signal.signal(signal.SIGINT, lambda *a: stop.append(1))
        signal.signal(signal.SIGHUP, lambda *a: self.reload())
        self.open_fifo()
        LOG.info("autolight %s started: screen max %s, keyboard max %s", __version__,
                 self.engine.screen and self.engine.screen.max, self.engine.kbd and self.engine.kbd.max)
        try:
            while not stop:
                now = time.monotonic()
                try:
                    delay = self.tick(now)
                except Exception:        # a bug must not stop the daemon and leave the screen stuck
                    LOG.exception("tick failed")
                    delay = 2.0
                if self.fifo is not None:
                    ready, _, _ = select.select([self.fifo], [], [], delay)
                    if ready:
                        for action in self.read_actions():
                            self.handle(action, time.monotonic())
                        continue
                else:
                    time.sleep(delay)
        finally:
            for p in (self.state_path, self.fifo_path):
                try:
                    os.unlink(p)
                except OSError:
                    pass
        LOG.info("stopped")
        return 0


class DryWriter:
    """--dry-run: log what would be written instead of changing anything. The levels are remembered
    here and read back by Hardware, so the engine sees its own writes without the device moving."""

    def __init__(self, paths: Paths):
        self.paths = paths
        self.vals: dict[str, int] = {}

    def set(self, subsystem: str, name: str, value: int) -> None:
        self.vals[subsystem] = int(value)
        LOG.info("[dry-run] %s/%s -> %d", subsystem, name, value)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="autolightd", description="Ambient-light brightness and keyboard backlight daemon")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--config", help="config file (default ~/.config/autolight/config.toml)")
    ap.add_argument("--root", default="/", help="sysfs root (for testing with a fake tree)")
    ap.add_argument("--sysfs", action="store_true", help="write sysfs directly instead of via logind (needs root)")
    ap.add_argument("--dry-run", action="store_true", help="log the writes instead of doing them")
    ap.add_argument("--version", action="version", version=__version__)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s")

    cfg = cfgmod.load(args.config)
    paths = Paths(args.root)
    if args.dry_run:
        writer = DryWriter(paths)
    elif args.sysfs or args.root != "/":
        writer = SysfsWriter(paths)
    else:
        try:
            writer = Logind()
        except Exception as e:                                   # python3-gi missing
            LOG.error("need python3-gi for logind brightness control: %s", e)
            return 78
    try:
        session = SessionBus()
    except Exception:
        session = None
        LOG.warning("no Gio: idle dimming and manual-change detection by idle time are off")
    hw = Hardware(paths, cfg["sensor"]["name"], writer, session)
    if hw.screen_max() <= 0 and hw.kbd_max() <= 0:
        LOG.error("no backlight (%s) and no keyboard LED (%s): nothing to control", paths.backlight, paths.led)
        return 78
    d = Daemon(hw, cfg, runtime_dir(), os.path.join(state_dir(), "state.json"),
               config_loader=lambda: cfgmod.load(args.config))
    d.dry_run = args.dry_run
    return d.run()


if __name__ == "__main__":
    sys.exit(main())
