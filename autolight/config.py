"""Configuration: built-in defaults, optional ~/.config/autolight/config.toml, strict clamping.

Nothing in the file can crash the daemon: unknown keys are ignored, bad values fall back to the
default for that key (and are reported once), numbers are clamped to a sane range.
"""
from __future__ import annotations

import logging
import os
import tomllib
from copy import deepcopy
from typing import Any

LOG = logging.getLogger("autolight")

# lux -> percent of the panel's range; interpolated in log10(1 + lux), so the curve is smooth on
# the scale the eye uses. Measured on this machine: ~15 lux at night, ~590 lux in a lit evening room.
SCREEN_CURVE = [[0, 8], [5, 12], [20, 20], [60, 32], [150, 46], [400, 66], [1000, 88], [3000, 100]]
# keyboard backlight, percent of 255, only while "dark" (below on_below_lux .. above off_above_lux)
KBD_CURVE = [[0, 60], [10, 45], [25, 30], [60, 18]]

# section -> key -> (default, lo, hi)   (bool and list keys are validated separately)
SCHEMA: dict[str, dict[str, tuple]] = {
    "sensor": {
        "name": ("als", None, None),            # iio device `name` to use
        "sample_period": (1.0, 0.2, 2.0),       # seconds between ALS reads (<3: the sensor autosuspends after 3 s)
        "tau_up": (4.0, 0.2, 120.0),            # smoothing when it gets brighter (s)
        "tau_down": (10.0, 0.2, 300.0),         # smoothing when it gets darker (s)
        "median": (3, 1, 9),                    # spike rejection window (samples)
        "max_lux": (20000.0, 100.0, 1e6),       # readings above this are clamped
    },
    "screen": {
        "enabled": (True, None, None),
        "min_percent": (5.0, 1.0, 50.0),        # never darker than this
        "max_percent": (100.0, 20.0, 100.0),
        "ramp_rate": (14.0, 1.0, 300.0),        # % of range per second while following the light
        "band_percent": (7.0, 1.0, 50.0),       # ignore target moves smaller than this (of current)
        "band_min_levels": (2, 1, 20),          # ... but at least this many steps
    },
    "keyboard": {
        "enabled": (True, None, None),
        "on_below_lux": (25.0, 0.0, 5000.0),    # backlight comes on below this ...
        "off_above_lux": (60.0, 1.0, 20000.0),  # ... and goes off above this (hysteresis)
        "ramp_rate": (150.0, 10.0, 1000.0),
    },
    "manual": {
        "resume_after": (900.0, 0.0, 86400.0),  # seconds paused after a manual change; 0 = until toggled
        "bias_step": (5.0, 1.0, 20.0),          # brighter/dimmer nudge, percentage points
        "bias_max": (30.0, 0.0, 50.0),
    },
    "idle": {
        "mode": ("auto", None, None),           # auto | on | off  (auto: defer to Cinnamon's own dimming)
        "dim_after": (60.0, 5.0, 3600.0),       # seconds without input before dimming the panel
        "dim_percent": (25.0, 1.0, 100.0),      # dim to at most this percent
        "kbd_off_after": (30.0, 0.0, 3600.0),   # keyboard backlight off after this idle time; 0 = never
    },
}
CURVES = {("screen", "curve"): SCREEN_CURVE, ("keyboard", "curve"): KBD_CURVE}
ENUMS = {("idle", "mode"): ("auto", "on", "off")}


def defaults() -> dict[str, dict[str, Any]]:
    cfg = {sec: {k: v[0] for k, v in keys.items()} for sec, keys in SCHEMA.items()}
    for (sec, key), curve in CURVES.items():
        cfg[sec][key] = deepcopy(curve)
    return cfg


def _curve(value: Any) -> list[list[float]] | None:
    try:
        pts = [[float(a), float(b)] for a, b in value]
    except (TypeError, ValueError):
        return None
    if len(pts) < 2 or len(pts) > 32:
        return None
    if any(a < 0 or not 0 <= b <= 100 for a, b in pts):
        return None
    if any(pts[i][0] >= pts[i + 1][0] for i in range(len(pts) - 1)):
        return None
    return pts


def validate(raw: dict, warn=LOG.warning) -> dict[str, dict[str, Any]]:
    """Merge a parsed TOML dict over the defaults; return a fully valid config."""
    cfg = defaults()
    if not isinstance(raw, dict):
        return cfg
    for sec, keys in raw.items():
        if sec not in SCHEMA or not isinstance(keys, dict):
            warn("config: ignoring unknown section %r", sec)
            continue
        for key, value in keys.items():
            if key in SCHEMA[sec]:
                default, lo, hi = SCHEMA[sec][key]
                if isinstance(default, bool):
                    if isinstance(value, bool):
                        cfg[sec][key] = value
                        continue
                elif isinstance(default, str):
                    allowed = ENUMS.get((sec, key))
                    if isinstance(value, str) and (allowed is None or value in allowed):
                        cfg[sec][key] = value
                        continue
                elif isinstance(value, (int, float)) and not isinstance(value, bool) and value == value:
                    num = min(max(float(value), lo), hi)
                    cfg[sec][key] = int(num) if isinstance(default, int) else num
                    if num != value:
                        warn("config: %s.%s=%r clamped to %r", sec, key, value, num)
                    continue
            elif (sec, key) in CURVES:
                pts = _curve(value)
                if pts is not None:
                    cfg[sec][key] = pts
                    continue
            else:
                warn("config: ignoring unknown key %s.%s", sec, key)
                continue
            warn("config: bad value for %s.%s, using the default", sec, key)
    k = cfg["keyboard"]
    if k["off_above_lux"] <= k["on_below_lux"]:
        warn("config: keyboard.off_above_lux must exceed on_below_lux; using defaults")
        k["on_below_lux"], k["off_above_lux"] = SCHEMA["keyboard"]["on_below_lux"][0], SCHEMA["keyboard"]["off_above_lux"][0]
    return cfg


def config_path() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "autolight", "config.toml")


def load(path: str | None = None, warn=LOG.warning) -> dict[str, dict[str, Any]]:
    path = path or config_path()
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except FileNotFoundError:
        return defaults()
    except (OSError, ValueError) as e:          # ValueError covers TOMLDecodeError and bad UTF-8
        warn("config: cannot read %s (%s); using defaults", path, e)
        return defaults()
    return validate(raw, warn)
