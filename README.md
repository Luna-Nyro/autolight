# autolight

Ambient-light brightness and keyboard backlight for the 2017 MacBookPro14,2 (T1) on Linux Mint / Cinnamon.

The T1 iBridge exposes an ambient light sensor (`iio:device0`, name `als`) that nothing used. autolight is a small
user daemon (Python 3.11+, stdlib plus `python3-gi`; no root, no kernel changes) that reads it and

* sets the **panel brightness** (`/sys/class/backlight/acpi_video0`, 0-90) through a smoothed lux curve with hysteresis,
* sets the **keyboard backlight** (`/sys/class/leds/spi::kbd_backlight`, 0-255): on in the dark, off in the light,
* **pauses itself when you change brightness yourself** and resumes after a timeout or on request,
* **dims on idle** (panel fades down, keyboard light goes off) and comes back the moment you touch something.

Writes go through logind (`org.freedesktop.login1.Session.SetBrightness`), so a normal user service can do it.

```sh
./install.sh              # install + start (no sudo)
autolightctl status       # one-line state
./install.sh uninstall    # remove it all (add --purge to delete config and saved state too)
```

## Why

On this hardware under Linux the light sensor works (it shows up as an IIO device), but no desktop component uses
it: the screen stays at whatever level you last set, and the keyboard backlight never turns itself on or off.
autolight fills that gap without root, kernel patches or a system service.

## Requirements

* **Hardware:** a MacBookPro14,2 (2017, T1 chip) is the only machine it has been tested on. The device names are
  fixed in the code (`Paths` in `autolight/system.py`): panel `acpi_video0`, keyboard LED `spi::kbd_backlight`,
  light sensor found by IIO name (`sensor.name`, default `als`). Other machines will need a code change (see
  [Status and limitations](#status-and-limitations)).
* **Kernel drivers** that expose the iBridge light sensor as an IIO device and the keyboard backlight as an LED,
  i.e. the out-of-tree `apple-ib-drv` / `apple-ibridge` DKMS driver for T1 Macs (not part of this repo).
* **Linux with systemd and logind** (brightness is written through `org.freedesktop.login1.Session.SetBrightness`).
* **Python 3.11 or newer** (uses `tomllib`) and **`python3-gi`** (`sudo apt install python3-gi`).
* **Cinnamon** for idle detection and idle-dim coexistence (Muffin's `IdleMonitor`, `org.gnome.SessionManager`
  inhibitors and the `org.cinnamon.settings-daemon.plugins.power` settings). Without them the daemon still follows
  the light, but idle dimming is off.
* Optional: the barkeep Touch Bar program (a separate project) for the Touch Bar scene described below.

## How it works

```
ALS (1 Hz) -> median(3) spike filter -> log-domain EMA (4 s brighter / 10 s darker)
           -> curve: lux -> % (linear in log10(1+lux)) + bias
           -> hysteresis band (ignore moves < 7 % of the current level, at least 2 steps)
           -> ramp, one step per 100 ms (~14 % of the range per second)
```

* **Smooth, no flicker.** Brightening is followed faster than darkening, so a passing shadow does not dim the
  screen. Small target moves never start a ramp; a ramp ends within one step of the target. Steady light means
  zero writes (verified live: no activity for minutes).
* **Manual override.** Each output watches its own level. If it differs from what autolight last wrote (brightness
  keys, the Cinnamon slider, the Touch Bar, a script) that output pauses for `manual.resume_after` seconds
  (default 900; every further manual change restarts the timer; `0` = until toggled). Screen and keyboard pause
  independently. `autolightctl resume` or `toggle` ends a pause at once.
* **Idle.** After `idle.dim_after` s (60) without input the panel fades to at most `dim_percent` (25 %); it never
  brightens a screen that is already darker, and restores within a fraction of a second on the first input. Video
  players and presentations that inhibit idle are respected. The keyboard light goes off after `kbd_off_after` s (30).
* **Cinnamon's own idle dim.** `csd-power` already dims on battery (`idle-dim-battery`, 90 s, 30 %). With
  `idle.mode = "auto"` (default) autolight leaves dimming to Cinnamon whenever Cinnamon's dim is enabled for the
  current power source (so on AC, where it is off, autolight dims), and while Cinnamon dims it neither fights it nor
  mistakes it for a manual change. `mode = "on"` always dims here (then turn Cinnamon's off:
  `gsettings set org.cinnamon.settings-daemon.plugins.power idle-dim-battery false`), `"off"` never.
* **Keyboard.** Schmitt trigger: on below `keyboard.on_below_lux` (25), off above `off_above_lux` (60), and in
  between it keeps its state. While on, the level follows a curve (darker room, brighter backlight).
* **Lid and sleep.** With the lid closed the sensor sees darkness, so nothing is written. After a lid cycle or
  resume from sleep the filter restarts and whatever the firmware did to the levels is adopted, not treated as a
  manual change. The sensor is found by name, so a re-enumerated iBridge (`iio:device0` becoming another number) is
  picked up again within 5 s.
* **Never stuck.** A device that ignores writes is backed off after 5 attempts; a failed logind call retries after
  5 s; a bug inside one pass is logged and the loop carries on. If the sensor is lost the brightness is held.

Default screen curve (`lux -> %`): 0 → 8, 5 → 12, 20 → 20, 60 → 32, 150 → 46, 400 → 66, 1000 → 88, 3000 → 100.
This machine reads about 15 lux on a dark night and about 600 lux in a lit evening room. The curve is a
starting point: nudge the whole thing with `autolightctl brighter|dimmer` (±5 points per press, persisted, at most
±30) or edit `curve` in the config.

## Commands

```
autolightctl status             auto on, 165 lux, bias +0, screen: auto 41/90, keyboard: auto 0/255, sensor ok, lid open
autolightctl toggle             paused -> resume; on -> off; off -> on  (on/off persists across restarts)
autolightctl on | off | resume | brighter | dimmer | bias-reset | reload
autolightctl status --json      the full state
autolightctl probe [seconds]    read the sensor directly and say whether the data is sane (no daemon needed)
autolightd --dry-run -v         run in the foreground and log what it would write, touching nothing
journalctl --user -u autolight -f
```

## Configuration

`~/.config/autolight/config.toml` (installed fully commented out; see `dist/config.example.toml` for every key).
Unknown keys are ignored, bad values fall back to the default, numbers are clamped; a garbage file cannot stop the
daemon. Apply with `autolightctl reload` (or `systemctl --user restart autolight`).

## The sensor, validated (and the `hid_field_extract` warning)

Checked on this machine (kernel 7.0.0-42 and -43):

* Readings are plain integer lux, `in_illuminance_raw` = `in_intensity_both_raw`, scale 1, HID range 0-5000:
  2-3 lux in a dark night room, 150-210 lux lit, ~590 lux earlier in the evening. At 5 Hz the noise is a few lux.
* **The panel does not feed back into the sensor**: with the panel at 15, 45 and 85 the readings were identical
  (about 158 lux), so no compensation is needed.
* **Warm-up quirk.** The sensor hub suspends the ALS 3 s after the last read (`autosuspend_delay_ms=3000`) and the
  first reads after waking are far too low for about a second (38, 57, 127, 155, 161 ... then 170). autolight polls
  at 1 s so the sensor stays awake, and after any longer gap (start-up, resume) waits for two agreeing reads before
  trusting a value. That is why `sensor.sample_period` is capped at 2 s.
* Do not read `*_hysteresis_relative`: the hub answers with `No report with id 0xffffffff found` in the kernel log.
* **`hid_field_extract() called with n (64) > 32!`** (once per boot, in interrupt context, 40-60 minutes after boot)
  does not touch the light data, so **no report-descriptor fixup in the `apple-ib-drv` DKMS is needed** and nothing
  was rebuilt or changed. Why: the kernel warns once per HID device and then clamps that one field to its low 32 bits.
  The iBridge has two HID interfaces whose descriptors contain a 64-bit *vendor-defined* field: the touchpad
  interface (`hid-generic`, usage `0xff110001`; the warning came from there on the earlier boots) and the sensor hub
  (`hid-sensor-hub`, vendor collection `0xff12`, report ids 3 and 6, usage `0xff120051`; the warning came from there
  on a later boot). The ambient-light collection (`0x200041`, report id 1) has no field wider than 32 bits
  (illuminance is 32-bit, range 0-5000), and `hid_sensor_als` never reads the vendor collection. Readings taken
  after the warning were sane and continuous. Reproduce with `tools/hiddump.py` (device numbers change every boot):
  `ls /sys/bus/hid/devices | grep 05AC:8600`, then
  `python3 -I tools/hiddump.py /sys/bus/hid/devices/0003:05AC:8600.<n>/report_descriptor | grep 'size>32'`.

## Touch Bar (optional)

autolight never needs the Touch Bar or barkeep: install, run and tests work with both absent (the scene step prints a
note and is skipped when `~/.config/barkeep` is missing). With barkeep present, `install.sh` merges one **key-only
scene** into `~/.config/barkeep/scenes.json` (backup `scenes.json.bak-before-autolight` on the first run):

**Auto-brightness** (no window to match, so open it with a `{"scene": "Auto-brightness"}` key, e.g. added in the Scenes
editor to a screen you like): `esc` · auto on/off (`toggle`) · resume · dimmer · brighter · reset bias · `bri-`/`bri+`.
Every key runs `autolightctl` (or a plain brightness key combo), so each one also exists in a terminal. The scene is
invisible until opened; preview it without touching the live bar:

```sh
barkeep preview /tmp/autolight.png --preview-scene "Auto-brightness"
```

Remove just the scene: `python3 tools/install-bar-scenes.py --remove` (uninstall does this too).

The daemon also publishes `$XDG_RUNTIME_DIR/barkeep-autolight` (JSON, atomic, only on change plus a 30 s tick) and
reads action names from the FIFO `$XDG_RUNTIME_DIR/barkeep-autolight.ctl` (fixed list: `toggle on off resume brighter
dimmer bias-reset reload`; anything else is ignored). `autolightctl` uses that same FIFO. barkeep does not read this
state yet, so the toggle key cannot light up; that would be a change on the barkeep side, not in this repo.
State fields: `v, ver, ts, enabled, bias, lux, sensor (ok|lost), lid (open|closed), screen/kbd: {state
(auto|paused|idle|held|off), level, max, pct, target, resume_in}`.

## Tests

```sh
python3 tests/test_autolight.py     # 66 tests, ~8 s, no hardware, no barkeep, no D-Bus
```

They run the real daemon pass against a fake sysfs/IIO/proc tree on a simulated clock: curve and filter maths,
smoothness (monotonic ramps, small steps), no flicker under ±4 % noise, hysteresis, floor/ceiling, manual override
and timeout/toggle, idle dim and undim, Cinnamon-dim coexistence, keyboard hysteresis and idle, lid, resume from
sleep, sensor unplugged and re-enumerated, sensor warm-up, garbage in sysfs/config/FIFO, and the state file.

## Install

```sh
git clone https://github.com/Luna-Nyro/autolight.git
cd autolight
python3 tests/test_autolight.py   # optional: runs without hardware
python3 bin/autolightctl probe 10  # optional: reads the sensor for 10 s and says whether it looks sane
./install.sh
```

`./install.sh` copies the package to `~/.local/lib/autolight`, links `autolightd`/`autolightctl` into `~/.local/bin`,
installs `~/.config/systemd/user/autolight.service` (enabled, `WantedBy=default.target`, `NoNewPrivileges`,
`MemoryMax=96M`), creates `~/.config/autolight/config.toml` if missing, and merges the optional Touch Bar scene.
It needs `python3-gi` (`sudo apt install python3-gi` if missing) and uses no sudo. Re-running `./install.sh`
updates the installed copy and restarts the service; an existing config is never overwritten. `./install.sh status`
shows the service and the daemon state.

## Uninstall and rollback

`./install.sh uninstall` stops and removes all of that, including the scene; your config and saved state
(`~/.local/state/autolight/state.json`: on/off and bias) are kept unless you add `--purge`. Brightness stays where it
was left.

If it misbehaves, in increasing order: `autolightctl off` (stop controlling, keep running),
`systemctl --user stop autolight` (until next login), `systemctl --user disable --now autolight`, or uninstall.
Nothing outside your home directory is changed, so there is nothing system-wide to roll back. If barkeep's
`scenes.json` was modified, the original is in `~/.config/barkeep/scenes.json.bak-before-autolight`.

## Troubleshooting

* **`autolightctl status` says "autolight is not running".** Check `systemctl --user status autolight` and
  `journalctl --user -u autolight -b`. Exit status 78 means a configuration problem the unit will not retry:
  `python3-gi` is missing, or neither `/sys/class/backlight/acpi_video0` nor `/sys/class/leds/spi::kbd_backlight`
  exists.
* **"light sensor unavailable" in the log, or `sensor lost` in the status.** Run `autolightctl probe 10`. If it finds
  nothing, check `cat /sys/bus/iio/devices/*/name` for a device called `als` (or set `sensor.name` to the name your
  sensor uses). The brightness is held while the sensor is missing.
* **"cannot set screen brightness: no login session to set brightness through".** logind could not find a graphical
  session for your user; the service has to run inside a normal desktop login.
* **The screen is consistently too dark or too bright.** `autolightctl brighter` / `dimmer` shifts the whole curve
  and is remembered; for a different shape edit `screen.curve` in the config and `autolightctl reload`.
* **Brightness keys "stop working" for a while.** That is the manual override doing its job: your level is kept for
  `manual.resume_after` seconds. `autolightctl resume` hands control back immediately.
* **Two programs dim the screen on idle.** Set `idle.mode` (see [How it works](#how-it-works)).
* **Trying settings without touching the screen.** Stop the service first (`systemctl --user stop autolight`, both
  instances use the same control FIFO and state file), then run `autolightd --dry-run -v` in a terminal.

## Status and limitations

Version 0.1.0. A personal tool, developed and tested on a single MacBookPro14,2 running Linux Mint (Cinnamon); it is
shared as-is.

* Hardware-specific: the backlight (`acpi_video0`) and keyboard LED (`spi::kbd_backlight`) names are hard-coded and
  not in the config file. Other laptops with an IIO light sensor may work after changing the defaults in
  `autolight/system.py`, but this is untested.
* Idle detection and idle-dim coexistence use Cinnamon-specific D-Bus interfaces; on other desktops idle dimming is
  inactive.
* Screen and keyboard curves were tuned for one machine's sensor and one person's taste.
* The Touch Bar status (toggle key lighting up) is not implemented on the barkeep side.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## Credits

Written by Luna-Nyro. Relies on the Linux IIO `hid-sensor-als` driver, the community T1 iBridge driver
(`apple-ib-drv`) that exposes the sensor and keyboard backlight, systemd-logind's `SetBrightness`, and PyGObject.
The Touch Bar scene refers to [Material Design Icons](https://pictogrammers.com/library/mdi/) by name (`mdi:*`);
no icon files are included here.

## License

Released under the MIT License. See [LICENSE](LICENSE).
