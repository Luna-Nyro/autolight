"""Control logic. Pure: no I/O and no clock of its own, so the whole thing can be simulated in tests.

Pipeline for every ALS sample:

    raw lux -> median(3) spike filter -> asymmetric EMA in log domain (quick to brighten, slow to dim)
            -> curve (piecewise linear in log10(1+lux)) + user bias -> target level
            -> hysteresis band (ignore small moves) -> rate-limited ramp, one step per tick

Each output (panel, keyboard) is a `Chan`. A Chan notices when someone else changed its level
(manual change) and then stays paused for `resume_after` seconds (or until `resume`/`toggle`).
"""
from __future__ import annotations

import logging
import math
from collections import deque
from dataclasses import dataclass
from typing import Optional

LOG = logging.getLogger("autolight")

ACTIVE_S = 3.0        # input within this long = the user is at the machine (decides "manual" vs "automatic")
EXT_GRACE = 2.0       # an automatic outside change is forgotten this long after the user returns
FOREVER = float("inf")
TICK_RAMP = 0.1       # seconds between steps while ramping
TICK_AWAY = 0.3       # poll rate while dimmed/dark, so the first keypress is answered quickly

DIM_RATE = 25.0       # % of range per second when fading down for idle
UNDIM_RATE = 200.0    # ... and when coming back (feels instant, still not a jump)
KBD_ON_RATE = 300.0


def curve_pct(points: list[list[float]], lux: float) -> float:
    """Piecewise-linear interpolation in log10(1+lux); flat outside the first/last point."""
    x = math.log10(1.0 + max(0.0, lux))
    xs = [(math.log10(1.0 + a), b) for a, b in points]
    if x <= xs[0][0]:
        return xs[0][1]
    for (x0, y0), (x1, y1) in zip(xs, xs[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return xs[-1][1]


class LuxFilter:
    """Median spike filter, then an exponential average in log domain with different time
    constants for rising and falling light (a passing shadow must not dim the screen)."""

    def __init__(self, tau_up: float, tau_down: float, window: int):
        self.tau_up, self.tau_down = tau_up, tau_down
        self.buf: deque[float] = deque(maxlen=max(1, window))
        self.y: Optional[float] = None
        self.t = 0.0

    def reset(self) -> None:
        self.buf.clear()
        self.y = None

    def update(self, now: float, lux: float) -> float:
        self.buf.append(lux)
        x = math.log1p(sorted(self.buf)[len(self.buf) // 2])
        if self.y is None:
            self.y = x
        else:
            tau = self.tau_up if x > self.y else self.tau_down
            self.y += (x - self.y) * (1.0 - math.exp(-max(0.0, now - self.t) / tau))
        self.t = now
        return math.expm1(self.y)


class Chan:
    """One output device (panel or keyboard LED): last-known level, pause state, ramp state."""

    def __init__(self, name: str, max_level: int, min_level: int, tol: int):
        self.name, self.max, self.min, self.tol = name, max_level, min_level, tol
        self.cur: Optional[int] = None        # level read back from the device
        self.written: Optional[int] = None    # level we last set, or last adopted from someone else
        self.paused_until: Optional[float] = None
        self.ext_pre: Optional[int] = None    # level before an automatic outside change (Cinnamon idle dim)
        self.ext_since: Optional[float] = None
        self.ramping = False
        self.last_step = 0.0
        self.retry_at = 0.0
        self.target: Optional[int] = None
        self.state = "auto"                   # auto | paused | idle | held | off
        self.away = False                     # we dimmed it for idle
        self.fade: Optional[float] = None     # ramp rate of an idle fade in progress (% of range per second)
        self.last_emit: Optional[tuple[int, int]] = None   # (from, to) of the last step we asked for
        self.stall = 0

    def paused(self, now: float) -> bool:
        return self.paused_until is not None and now < self.paused_until

    def resume_in(self, now: float) -> Optional[float]:
        if self.paused_until is None or self.paused_until == FOREVER:
            return None
        return max(0.0, self.paused_until - now)

    def observe(self, now: float, actual: Optional[int], idle_s: Optional[float], resume_after: float,
                other_dimmer: bool = False) -> Optional[str]:
        """Compare the device with what we last wrote. Returns 'manual', 'external', 'restore' or None.

        Any outside change is a manual one, except while Cinnamon's own idle dimming is on
        (`other_dimmer`): then a change that happens while nobody is typing is that dimmer, not the user."""
        if actual is None:
            return None
        self.cur = actual
        if self.written is None:
            self.written = actual
            return None
        active = idle_s is None or idle_s < ACTIVE_S or not other_dimmer
        if abs(actual - self.written) <= self.tol:
            # forget an automatic outside change once the user has been back for a moment
            if self.ext_pre is not None and active:
                self.ext_since = now if self.ext_since is None else self.ext_since
                if now - self.ext_since > EXT_GRACE:
                    self.ext_pre = self.ext_since = None
            return None
        before, self.written = self.written, actual
        self.ramping = False
        if not active:
            # nobody is typing, so this was automatic (e.g. Cinnamon's idle dim): follow, don't fight
            if self.ext_pre is None:
                self.ext_pre = before
            self.ext_since = None
            return "external"
        if self.ext_pre is not None and abs(actual - self.ext_pre) <= max(self.tol, 2):
            self.ext_pre = self.ext_since = None
            return "restore"
        self.ext_pre = self.ext_since = None
        self.paused_until = FOREVER if resume_after <= 0 else now + resume_after
        return "manual"

    def step(self, now: float, target: int, rate_pct: float, band_pct: float, band_min: int, forced: bool) -> Optional[int]:
        """Next level to write on the way to `target`, or None (inside the band, finished, or too soon)."""
        cur = self.cur
        err = target - cur
        edge = target <= self.min or target >= self.max
        if err == 0 or (not edge and abs(err) <= self.tol and self.ramping):
            self.ramping = False
            self.fade = None
            return None
        if not self.ramping:
            band = max(band_min, round(band_pct / 100.0 * cur))
            if not forced and abs(err) < band:
                return None
            self.ramping = True
            self.last_step = now - TICK_RAMP
        dt = now - self.last_step
        if dt < TICK_RAMP * 0.5:
            return None
        n = max(1, round(rate_pct / 100.0 * self.max * dt))
        self.last_step = now
        new = cur + max(-n, min(n, err))
        if self.last_emit == (cur, new):          # the same step again: the device is not following our writes
            self.stall += 1
            if self.stall >= 5:
                LOG.warning("%s: wrote %d five times but it still reads %d; backing off", self.name, new, cur)
                self.stall, self.ramping, self.retry_at = 0, False, now + 10.0
                return None
        else:
            self.stall = 0
        self.last_emit = (cur, new)
        return new


@dataclass
class Inputs:
    now: float
    lux: Optional[float] = None          # a fresh ALS sample this tick, if any
    sensor_ok: bool = True
    screen: Optional[int] = None         # current panel level read back
    kbd: Optional[int] = None
    idle_s: Optional[float] = None       # seconds since last input; None = unknown
    inhibited: bool = False              # a video/presentation inhibits idle
    lid_closed: bool = False
    csd_dims: bool = False               # Cinnamon's own idle dim is active for the current power source
    resumed: bool = False                # the machine slept since the last tick


class Engine:
    def __init__(self, cfg: dict, max_screen: int, max_kbd: int):
        self.cfg = cfg
        s = cfg["screen"]
        self.filter = LuxFilter(cfg["sensor"]["tau_up"], cfg["sensor"]["tau_down"], cfg["sensor"]["median"])
        self.lux: Optional[float] = None
        self.raw: Optional[float] = None
        self.sensor_ok = False
        self.enabled = True
        self.bias = 0.0
        self.dark = False
        self.lid_closed = False
        self.screen = Chan("screen", max_screen, max(1, round(s["min_percent"] / 100 * max_screen)),
                           1) if max_screen > 0 else None
        self.kbd = Chan("kbd", max_kbd, 0, max(1, round(max_kbd / 100))) if max_kbd > 0 else None
        self.events: list[str] = []

    # -- persistence / commands ---------------------------------------------------------------
    def persisted(self) -> dict:
        return {"enabled": self.enabled, "bias": self.bias}

    def restore(self, saved: dict) -> None:
        if isinstance(saved.get("enabled"), bool):
            self.enabled = saved["enabled"]
        b = saved.get("bias")
        if isinstance(b, (int, float)) and not isinstance(b, bool) and b == b:
            self.bias = self._clamp_bias(float(b))

    def _clamp_bias(self, b: float) -> float:
        m = self.cfg["manual"]["bias_max"]
        return max(-m, min(m, b))

    def _chans(self):
        return [c for c in (self.screen, self.kbd) if c is not None]

    def command(self, now: float, action: str) -> bool:
        """Apply a control action; True if persisted state changed. Unknown actions are ignored."""
        before = (self.enabled, self.bias)
        step = self.cfg["manual"]["bias_step"]
        paused = [c for c in self._chans() if c.paused_until is not None]
        if action == "toggle":
            if paused and self.enabled:
                action = "resume"
            else:
                action = "off" if self.enabled else "on"
        if action == "on":
            self.enabled = True
            for c in paused:
                c.paused_until = None
        elif action == "off":
            self.enabled = False
        elif action == "resume":
            self.enabled = True
            for c in paused:
                c.paused_until = None
        elif action == "brighter":
            self.bias = self._clamp_bias(self.bias + step)
        elif action == "dimmer":
            self.bias = self._clamp_bias(self.bias - step)
        elif action == "bias-reset":
            self.bias = 0.0
        else:
            return False
        for c in self._chans():
            c.ramping = False
        self.events.append("command " + action)
        return (self.enabled, self.bias) != before

    # -- main step ----------------------------------------------------------------------------
    def update(self, inp: Inputs) -> list[tuple[str, int]]:
        """Feed one tick of inputs; returns the writes to perform as [("screen"|"kbd", level)]."""
        now = inp.now
        cfg = self.cfg
        if inp.resumed or (self.lid_closed and not inp.lid_closed):
            # after sleep or a lid cycle the firmware may have changed levels: adopt, don't call it manual
            self.filter.reset()
            self.lux = None
            for ch in self._chans():
                ch.written = ch.ext_pre = None
                ch.ramping = False
        self.lid_closed = inp.lid_closed
        self.sensor_ok = inp.sensor_ok
        if inp.lux is not None:
            self.raw = min(inp.lux, cfg["sensor"]["max_lux"])
            self.lux = self.filter.update(now, self.raw)
        resume_after = cfg["manual"]["resume_after"]
        for ch, actual in ((self.screen, inp.screen), (self.kbd, inp.kbd)):
            if ch is None:
                continue
            ev = ch.observe(now, actual, inp.idle_s, resume_after, inp.csd_dims)
            if ev:
                self.events.append(f"{ch.name} {ev}")
                LOG.info("%s: %s change (now %s)%s", ch.name, ev, ch.written,
                         f", auto paused for {resume_after:.0f}s" if ev == "manual" and resume_after > 0
                         else ", auto paused until toggled" if ev == "manual" else "")
            if ch.paused_until is not None and now >= ch.paused_until:
                ch.paused_until = None
                ch.ramping = False
                self.events.append(f"{ch.name} resume")
                LOG.info("%s: auto resumed", ch.name)

        writes: list[tuple[str, int]] = []
        for ch, fn, enabled in ((self.screen, self._want_screen, cfg["screen"]["enabled"]),
                                (self.kbd, self._want_kbd, cfg["keyboard"]["enabled"])):
            if ch is None or ch.cur is None:
                continue
            ch.target = None
            if not enabled or not self.enabled:
                ch.state, ch.ramping = "off", False
                continue
            if ch.paused(now):
                ch.state, ch.ramping = "paused", False
                continue
            if inp.lid_closed or self.lux is None or not inp.sensor_ok or ch.ext_pre is not None:
                ch.state, ch.ramping = "held", False
                continue
            want = fn(ch, inp)
            if want is None:
                continue
            target, rate, band, forced = want
            ch.target = target
            if now < ch.retry_at:
                continue
            new = ch.step(now, target, rate, band, cfg["screen"]["band_min_levels"] if ch is self.screen else 1, forced)
            if new is not None:
                writes.append((ch.name, new))
        return writes

    def _want_screen(self, ch: Chan, inp: Inputs):
        s, idl = self.cfg["screen"], self.cfg["idle"]
        pct = curve_pct(s["curve"], self.lux) + self.bias
        pct = max(s["min_percent"], min(s["max_percent"], pct))
        own = idl["mode"] == "on" or (idl["mode"] == "auto" and not inp.csd_dims)
        dim = bool(own and inp.idle_s is not None and not inp.inhibited
                   and (inp.idle_s >= idl["dim_after"] or (ch.away and inp.idle_s >= 1.0)))
        forced = dim != ch.away
        if forced:
            ch.fade = DIM_RATE if dim else UNDIM_RATE
            LOG.info("screen: %s", "dimming, idle" if dim else "back from idle")
        rate = ch.fade or s["ramp_rate"]
        ch.away = dim
        if dim:
            pct = max(s["min_percent"], min(pct, idl["dim_percent"]))
        ch.state = "idle" if dim else "auto"
        level = max(ch.min, min(round(s["max_percent"] / 100 * ch.max), round(pct / 100 * ch.max)))
        if dim:
            level = min(level, ch.cur)          # dimming never brightens (rounding, or an already dark screen)
        return level, rate, s["band_percent"], forced

    def _want_kbd(self, ch: Chan, inp: Inputs):
        k, idl = self.cfg["keyboard"], self.cfg["idle"]
        lux = self.lux
        if self.dark and lux > k["off_above_lux"]:
            self.dark = False
        elif not self.dark and lux < k["on_below_lux"]:
            self.dark = True
        pct = curve_pct(self.cfg["keyboard"]["curve"], lux) if self.dark else 0.0
        own = idl["mode"] == "on" or (idl["mode"] == "auto" and not inp.csd_dims)
        away = bool(own and idl["kbd_off_after"] > 0 and inp.idle_s is not None
                    and (inp.idle_s >= idl["kbd_off_after"] or (ch.away and inp.idle_s >= 1.0)))
        forced = away != ch.away
        if forced:
            ch.fade = KBD_ON_RATE
            LOG.info("keyboard: backlight %s", "off, idle" if away else "back on")
        ch.away = away
        ch.state = "idle" if away else "auto"
        if away:
            pct = 0.0
        level = max(0, min(ch.max, round(pct / 100 * ch.max)))
        return level, ch.fade or k["ramp_rate"], 8.0, forced

    # -- scheduling / reporting ---------------------------------------------------------------
    def next_delay(self) -> float:
        base = self.cfg["sensor"]["sample_period"]
        if any(c.ramping for c in self._chans()):
            return TICK_RAMP
        if any(c.away or c.ext_pre is not None for c in self._chans()):
            return min(TICK_AWAY, base)
        return base

    def wrote(self, name: str, level: int, now: float) -> None:
        ch = self.screen if name == "screen" else self.kbd
        ch.written = ch.cur = level
        ch.last_step = now

    def failed(self, name: str, now: float) -> None:
        ch = self.screen if name == "screen" else self.kbd
        ch.retry_at = now + 5.0
        ch.ramping = False

    def snapshot(self, now: float) -> dict:
        def chan(c: Optional[Chan]):
            if c is None or c.cur is None:
                return None
            rin = c.resume_in(now)
            return {"state": c.state if c.state != "auto" or c.target is not None else "held",
                    "level": c.cur, "max": c.max, "pct": round(100 * c.cur / c.max),
                    "target": c.target, "resume_in": None if rin is None else round(rin)}
        return {"v": 1, "enabled": self.enabled, "bias": round(self.bias, 1),
                "lux": None if self.lux is None else round(self.lux, 1),
                "sensor": "ok" if self.sensor_ok else "lost",
                "lid": "closed" if self.lid_closed else "open",
                "screen": chan(self.screen), "kbd": chan(self.kbd)}
