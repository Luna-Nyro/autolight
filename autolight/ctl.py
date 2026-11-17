"""autolightctl: talk to the running daemon, and measure the sensor.

    autolightctl status            one-line summary (reads the daemon's state file)
    autolightctl toggle|on|off|resume|brighter|dimmer|bias-reset|reload
    autolightctl probe [seconds]   read the ALS directly and report whether it looks sane (no daemon needed)
"""
from __future__ import annotations

import errno
import json
import math
import os
import statistics
import sys
import time

from .daemon import ACTIONS, runtime_dir
from .system import Als, Paths, SensorError

HELP = __doc__


def state() -> dict | None:
    try:
        with open(os.path.join(runtime_dir(), "barkeep-autolight")) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def describe(d: dict) -> str:
    def chan(name, c):
        if not isinstance(c, dict):
            return f"{name}: n/a"
        extra = f" (resumes in {c['resume_in']}s)" if c.get("resume_in") else ""
        return f"{name}: {c.get('state')} {c.get('level')}/{c.get('max')}{extra}"
    lux = d.get("lux")
    return (f"auto {'on' if d.get('enabled') else 'off'}, {'%.0f lux' % lux if isinstance(lux, (int, float)) else 'no light reading'}, "
            f"bias {d.get('bias', 0):+g}, {chan('screen', d.get('screen'))}, {chan('keyboard', d.get('kbd'))}, "
            f"sensor {d.get('sensor')}, lid {d.get('lid')}")


def send(action: str) -> int:
    path = os.path.join(runtime_dir(), "barkeep-autolight.ctl")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
    except OSError as e:
        if e.errno in (errno.ENOENT, errno.ENXIO):
            print("autolight is not running (systemctl --user start autolight)", file=sys.stderr)
        else:
            print(f"cannot reach autolight: {e}", file=sys.stderr)
        return 1
    try:
        os.write(fd, (action + "\n").encode())
    except OSError as e:
        print(f"cannot reach autolight: {e}", file=sys.stderr)
        return 1
    finally:
        os.close(fd)
    return 0


def probe(seconds: float, root: str = "/") -> int:
    """Sample the sensor at 5 Hz and say whether the data looks like a working light sensor."""
    als = Als(Paths(root).iio, "als")
    vals: list[float] = []
    errors = 0
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            vals.append(als.read())
        except SensorError as e:
            errors += 1
            if not vals and errors == 1:
                print(f"sensor: {e}")
        time.sleep(0.2)
    if not vals:
        print("FAIL: no readings")
        return 1
    lo, hi, med = min(vals), max(vals), statistics.median(vals)
    jitter = (statistics.pstdev(vals) / med) if med > 0 else 0.0
    print(f"{len(vals)} readings in {seconds:g}s ({errors} errors): min {lo:g}  median {med:g}  max {hi:g} lux  (relative jitter {jitter:.1%})")
    ok = errors == 0 and all(math.isfinite(v) and 0 <= v <= 100000 for v in vals)
    print("OK: readings look sane" if ok else "WARN: errors or out-of-range readings")
    return 0 if ok else 1


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help", "help"):
        print(HELP)
        return 0
    cmd = args[0]
    if cmd == "status":
        d = state()
        if d is None:
            print("autolight is not running")
            return 1
        print(json.dumps(d, indent=2) if "--json" in args else describe(d))
        return 0
    if cmd == "probe":
        try:
            secs = float(args[1]) if len(args) > 1 else 5.0
        except ValueError:
            print("probe: seconds must be a number", file=sys.stderr)
            return 2
        return probe(max(0.5, min(secs, 300.0)))
    if cmd in ACTIONS:
        return send(cmd)
    print(f"unknown command {cmd!r}\n{HELP}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
