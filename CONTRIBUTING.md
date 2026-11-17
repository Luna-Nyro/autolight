# Contributing

Thanks for your interest. autolight is a small personal project, so please keep changes focused.

## Ground rules

* **No root, no kernel changes.** The daemon runs as a systemd *user* service and writes brightness only through
  logind. Changes that need sudo, a system service or a kernel module are out of scope.
* **Standard library plus `python3-gi` only.** Please do not add third-party Python dependencies.
* **The Touch Bar stays optional.** Nothing may require barkeep or a Touch Bar to install, run or test.
* **Never crash, never get stuck.** Bad config, odd sysfs contents or a missing sensor must be logged and survived;
  see the existing handling in `autolight/config.py` and `autolight/daemon.py`.

## Layout

| Path | What |
|------|------|
| `autolight/engine.py` | Control logic. Pure: no I/O, no clock of its own. |
| `autolight/system.py` | Everything that touches hardware or D-Bus (sysfs, IIO, logind, Cinnamon). |
| `autolight/daemon.py` | Run loop, state file, control FIFO, `autolightd` entry point. |
| `autolight/config.py` | Defaults, schema, validation and clamping. |
| `autolight/ctl.py` | `autolightctl`. |
| `tests/test_autolight.py` | Unit and process tests against a fake sysfs tree and a simulated clock. |
| `tools/` | Touch Bar scene installer and a HID report-descriptor dumper used for diagnosis. |
| `dist/` | Example config and the optional Touch Bar scene (source files, not build output). |

## Tests

```sh
python3 tests/test_autolight.py
```

They need no hardware, no D-Bus and no barkeep. Please add a test for any behaviour change; new logic should go in
`engine.py` (testable with `EngSim`) or behind the fake tree (`FakeTree`) rather than calling real devices.

To try a change on real hardware without writing anything, stop the service and run
`python3 bin/autolightd --dry-run -v`.

## Supporting other hardware

The backlight and keyboard LED names are currently hard-coded in `Paths` (`autolight/system.py`). Making them
configurable (with the current values as defaults) is a welcome change, as are reports of which other machines work.

## Pull requests

* Describe what you changed and how you tested it (tests, and on which hardware if you tried it live).
* Keep the README accurate: if you change a default, a command or a config key, update it too.
