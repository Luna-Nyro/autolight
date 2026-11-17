#!/usr/bin/env python3
"""Unit tests: no hardware, no barkeep, no D-Bus. Everything runs against a fake sysfs/IIO tree and a
simulated clock.   python3 tests/test_autolight.py"""
import io
import json
import logging
import os
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from autolight import config as cfgmod, ctl
from autolight.daemon import ACTIONS, Daemon
from autolight.engine import Engine, Inputs, LuxFilter, curve_pct
from autolight.system import Als, Hardware, Level, Paths, SensorError, SysfsWriter

logging.getLogger("autolight").setLevel(logging.CRITICAL)


# ------------------------------------------------------------------------------------ helpers
def slurp(path):
    with open(path) as f:
        return f.read()


def jload(path):
    return json.loads(slurp(path))


def cfg(**over):
    c = cfgmod.defaults()
    for sec, vals in over.items():
        c[sec].update(vals)
    return c


class EngSim:
    """Engine + simulated devices, one ALS sample per second like the daemon."""

    def __init__(self, c=None, screen=50, kbd=0, smax=90, kmax=255):
        self.c = c or cfg()
        self.e = Engine(self.c, smax, kmax)
        self.t, self.lux, self.screen, self.kbd = 0.0, 100.0, screen, kbd
        self.idle, self.csd, self.lid, self.inhibit, self.resumed = 0.0, False, False, False, False
        self.sensor_ok, self.last_sample = True, -1e9
        self.accrue = False              # True: the user walks away and idle time grows
        self.writes = []                 # (t, name, level)

    def tick(self):
        lux = None
        if self.t - self.last_sample >= self.c["sensor"]["sample_period"] - 1e-9:
            self.last_sample, lux = self.t, self.lux
        inp = Inputs(now=self.t, lux=lux, sensor_ok=self.sensor_ok, screen=self.screen, kbd=self.kbd, idle_s=self.idle,
                     inhibited=self.inhibit, lid_closed=self.lid, csd_dims=self.csd, resumed=self.resumed)
        self.resumed = False
        for name, level in self.e.update(inp):
            setattr(self, name if name == "kbd" else "screen", level)
            self.e.wrote(name, level, self.t)
            self.writes.append((self.t, name, level))
        return self.e.next_delay()

    def run(self, secs):
        end = self.t + secs
        while self.t < end - 1e-9:
            d = self.tick()
            self.t += d
            if self.accrue and self.idle is not None:
                self.idle += d

    def input(self):
        self.idle = 0.0

    def user_sets(self, name, level):
        """The user presses a brightness key: someone else writes the device, with input activity."""
        self.input()
        setattr(self, name, level)

    def of(self, name):
        return [w for w in self.writes if w[1] == name]


# --------------------------------------------------------------------------------------- config
class ConfigTests(unittest.TestCase):
    def test_defaults_valid_and_idempotent(self):
        c = cfgmod.validate(cfgmod.defaults())
        self.assertEqual(c, cfgmod.defaults())

    def test_clamp_and_bad_values(self):
        seen = []
        c = cfgmod.validate({"screen": {"min_percent": 999, "ramp_rate": "fast", "enabled": "yes"},
                             "idle": {"mode": "sometimes", "dim_after": -5}, "bogus": {"x": 1},
                             "sensor": {"median": 4.7}}, warn=lambda *a: seen.append(a))
        d = cfgmod.defaults()
        self.assertEqual(c["screen"]["min_percent"], 50.0)            # clamped
        self.assertEqual(c["screen"]["ramp_rate"], d["screen"]["ramp_rate"])
        self.assertIs(c["screen"]["enabled"], True)
        self.assertEqual(c["idle"]["mode"], "auto")
        self.assertEqual(c["idle"]["dim_after"], 5.0)
        self.assertIsInstance(c["sensor"]["median"], int)
        self.assertGreaterEqual(len(seen), 4)

    def test_curves_validated(self):
        bad = [[[0, 10]], [[0, 10], [0, 20]], [[5, 10], [1, 20]], [[0, 10], [5, 101]], "x", [[0, 10], [5]]]
        for curve in bad:
            c = cfgmod.validate({"screen": {"curve": curve}}, warn=lambda *a: None)
            self.assertEqual(c["screen"]["curve"], cfgmod.SCREEN_CURVE, curve)
        c = cfgmod.validate({"screen": {"curve": [[0, 20], [100, 60], [1000, 90]]}})
        self.assertEqual(c["screen"]["curve"][1], [100.0, 60.0])

    def test_keyboard_hysteresis_order_enforced(self):
        c = cfgmod.validate({"keyboard": {"on_below_lux": 100, "off_above_lux": 50}}, warn=lambda *a: None)
        self.assertLess(c["keyboard"]["on_below_lux"], c["keyboard"]["off_above_lux"])

    def test_load_toml_and_garbage(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.toml")
            self.assertEqual(cfgmod.load(p), cfgmod.defaults())       # missing file
            open(p, "w").write("[screen]\nmin_percent = 8\ncurve = [[0, 20], [500, 80]]\n")
            c = cfgmod.load(p)
            self.assertEqual(c["screen"]["min_percent"], 8.0)
            self.assertEqual(c["screen"]["curve"], [[0.0, 20.0], [500.0, 80.0]])
            open(p, "wb").write(b"\x00\xff not toml [[[")
            self.assertEqual(cfgmod.load(p, warn=lambda *a: None), cfgmod.defaults())


# ---------------------------------------------------------------------------- curve and filter
class CurveFilterTests(unittest.TestCase):
    def test_curve_monotonic_and_bounded(self):
        pts = cfgmod.SCREEN_CURVE
        prev = -1
        for lux in [0, 1, 2, 5, 10, 30, 100, 300, 1000, 3000, 10000, 1e6]:
            v = curve_pct(pts, lux)
            self.assertGreaterEqual(v, prev)
            self.assertTrue(pts[0][1] <= v <= pts[-1][1])
            prev = v
        self.assertEqual(curve_pct(pts, 0), pts[0][1])
        self.assertEqual(curve_pct(pts, 5), pts[1][1])                 # exact breakpoint
        self.assertAlmostEqual(curve_pct(pts, 1e9), 100)

    def test_filter_seeds_on_first_sample_and_rejects_spike(self):
        f = LuxFilter(4, 10, 3)
        self.assertAlmostEqual(f.update(0, 200), 200)                  # no slow ramp from zero
        for t in range(1, 6):
            f.update(t, 200)
        self.assertLess(f.update(6, 5000), 230)                        # one bright flash is a spike
        self.assertLess(f.update(7, 200), 230)

    def test_filter_fast_up_slow_down(self):
        up, down = LuxFilter(4, 10, 1), LuxFilter(4, 10, 1)
        up.update(0, 100)
        down.update(0, 100)
        a = [up.update(t, 400) for t in range(1, 6)]
        b = [down.update(t, 25) for t in range(1, 6)]
        self.assertGreater(a[-1] - 100, 100 - b[-1])                   # rises further than it falls
        self.assertTrue(all(x < y for x, y in zip(a, a[1:])))
        self.assertTrue(all(x > y for x, y in zip(b, b[1:])))


# ----------------------------------------------------------------------------------- following
class FollowTests(unittest.TestCase):
    def test_brightens_smoothly_to_curve_target(self):
        s = EngSim(screen=20)
        s.lux = 590
        s.run(40)
        want = round(curve_pct(s.c["screen"]["curve"], 590) / 100 * 90)
        self.assertLessEqual(abs(s.screen - want), 1)
        levels = [w[2] for w in s.of("screen")]
        self.assertTrue(all(b >= a for a, b in zip(levels, levels[1:])), "monotonic, no overshoot")
        self.assertTrue(all(b - a <= 2 for a, b in zip([20] + levels, levels)), "small steps, no jumps")
        self.assertGreater(len(levels), 20)

    def test_darkens_slower_than_it_brightens(self):
        up, down = EngSim(screen=40), EngSim(screen=40)
        up.lux, down.lux = 40, 40
        up.run(60), down.run(60)                                        # settle both at the same place
        base = up.screen
        up.lux, down.lux = 400, 4
        up.run(8), down.run(8)
        self.assertGreater(abs(up.screen - base), abs(down.screen - base))

    def test_no_flicker_on_sensor_noise(self):
        s = EngSim(screen=45)
        s.lux = 60
        s.run(60)
        n = len(s.of("screen"))
        import random
        rnd = random.Random(1)
        for _ in range(300):                                            # +-4% jitter for 5 minutes
            s.lux = 60 * (1 + rnd.uniform(-0.04, 0.04))
            s.run(1)
        self.assertLessEqual(len(s.of("screen")) - n, 1)

    def test_hysteresis_band_ignores_small_target_moves(self):
        s = EngSim(screen=50)
        s.lux = 150
        s.run(60)
        before = s.screen
        s.lux = 165                                                     # +10% light: well inside the band
        s.run(30)
        self.assertEqual(s.screen, before)

    def test_floor_and_ceiling(self):
        s = EngSim(screen=50)
        s.lux = 0
        s.run(120)
        self.assertLessEqual(abs(s.screen - round(s.c["screen"]["curve"][0][1] * 0.9)), 1)   # the curve's first point
        for _ in range(6):
            s.e.command(s.t, "dimmer")                                  # bias -30 pushes it into min_percent
        s.run(120)
        self.assertEqual(s.screen, s.e.screen.min)
        s.e.command(s.t, "bias-reset")
        s.lux = 1e6
        s.run(120)
        self.assertEqual(s.screen, 90)

    def test_bias_shifts_curve_and_is_clamped(self):
        s = EngSim(screen=50)
        s.lux = 150
        s.run(60)
        mid = s.screen
        for _ in range(3):
            s.e.command(s.t, "brighter")
        s.run(20)
        self.assertGreater(s.screen, mid)
        for _ in range(20):
            s.e.command(s.t, "dimmer")
        self.assertEqual(s.e.bias, -s.c["manual"]["bias_max"])
        s.e.command(s.t, "bias-reset")
        self.assertEqual(s.e.bias, 0)

    def test_sensor_lost_holds_brightness(self):
        s = EngSim(screen=50)
        s.lux = 150
        s.run(60)
        held, n = s.screen, len(s.writes)
        s.sensor_ok = False
        s.lux = 1.0
        s.run(60)
        self.assertEqual((s.screen, len(s.writes)), (held, n))
        self.assertEqual(s.e.snapshot(s.t)["sensor"], "lost")

    def test_lid_closed_holds_then_adopts(self):
        s = EngSim(screen=50)
        s.lux = 150
        s.run(60)
        n = len(s.writes)
        s.lid = True
        s.lux = 0                                                       # lid shut: sensor sees darkness
        s.run(60)
        self.assertEqual(len(s.writes), n)
        s.lid, s.screen, s.lux = False, 77, 150                         # firmware changed it meanwhile
        s.run(5)
        self.assertIsNone(s.e.screen.paused_until, "a lid cycle is not a manual change")

    def test_resume_from_sleep_adopts_levels(self):
        s = EngSim(screen=50)
        s.lux = 150
        s.run(60)
        s.screen, s.resumed = 30, True
        s.input()
        s.run(5)
        self.assertIsNone(s.e.screen.paused_until)


# ----------------------------------------------------------------------------- manual override
class ManualTests(unittest.TestCase):
    def settled(self, lux=150, **kw):
        s = EngSim(**kw)
        s.lux = lux
        s.run(60)
        s.input()
        return s

    def test_manual_change_pauses_then_resumes_after_timeout(self):
        s = self.settled()
        auto = s.screen
        s.user_sets("screen", 10)
        s.lux = 300                                                     # light changes while paused
        s.run(30)
        self.assertEqual(s.screen, 10, "auto must stay out of the way")
        self.assertEqual(s.e.snapshot(s.t)["screen"]["state"], "paused")
        s.input()
        s.run(s.c["manual"]["resume_after"] + 5)
        self.assertGreater(s.screen, 10, "auto resumes after the timeout")
        self.assertGreaterEqual(s.screen, auto)

    def test_each_manual_change_restarts_the_timeout(self):
        s = self.settled()
        s.user_sets("screen", 10)
        s.run(600)
        s.user_sets("screen", 20)
        s.run(600)                                                      # 1200 s after the first change
        self.assertEqual(s.screen, 20)
        s.input()
        s.run(400)
        self.assertGreater(s.screen, 20)

    def test_toggle_resumes_when_paused_and_flips_otherwise(self):
        s = self.settled()
        s.user_sets("screen", 10)
        s.run(10)
        self.assertFalse(s.e.command(s.t, "toggle"), "resume is not persisted state")
        s.run(20)
        self.assertGreater(s.screen, 10)
        self.assertTrue(s.e.command(s.t, "toggle"))                     # now: switch auto off
        self.assertFalse(s.e.enabled)
        held = s.screen
        s.lux = 2000
        s.run(60)
        self.assertEqual(s.screen, held)
        self.assertEqual(s.e.snapshot(s.t)["screen"]["state"], "off")
        self.assertTrue(s.e.command(s.t, "toggle"))
        s.run(60)
        self.assertNotEqual(s.screen, held)

    def test_resume_after_zero_means_until_toggled(self):
        s = EngSim(c=cfg(manual={"resume_after": 0.0}))
        s.lux = 150
        s.run(60)
        s.user_sets("screen", 10)
        s.run(86400)
        self.assertEqual(s.screen, 10)
        s.e.command(s.t, "resume")
        s.run(60)
        self.assertGreater(s.screen, 10)

    def test_manual_screen_change_does_not_pause_keyboard(self):
        s = EngSim(screen=50, kbd=0)
        s.lux = 3
        s.run(60)
        self.assertGreater(s.kbd, 0)
        s.user_sets("screen", 30)
        s.lux = 3
        s.run(30)
        self.assertIsNone(s.e.kbd.paused_until)
        s.user_sets("kbd", 0)                                           # user turns the backlight off
        s.run(60)
        self.assertEqual(s.kbd, 0)
        self.assertEqual(s.e.snapshot(s.t)["kbd"]["state"], "paused")

    def test_our_own_writes_are_never_manual(self):
        s = EngSim(screen=10)
        s.lux = 1500
        s.run(120)
        self.assertEqual([e for e in s.e.events if "manual" in e], [])

    def test_tolerance_of_one_level(self):
        s = self.settled()
        s.screen += 1                                                   # rounding by the driver
        s.run(5)
        self.assertIsNone(s.e.screen.paused_until)

    def test_device_that_ignores_writes_is_backed_off(self):
        s = EngSim(screen=20)
        s.lux = 590
        s.run(2)
        real = s.e.wrote
        s.e.wrote = lambda name, level, now: None                       # readback never changes
        s.screen = 30
        n = len(s.writes)
        s.run(8)
        self.assertLessEqual(len(s.writes) - n, 8)

    def test_failed_write_backs_off(self):
        s = self.settled(screen=10)
        s.e.failed("screen", s.t)
        n = len(s.of("screen"))
        s.lux = 1500
        s.run(4)
        self.assertEqual(len(s.of("screen")), n)                        # waits 5 s before retrying


# ------------------------------------------------------------------------------------- idle
class IdleTests(unittest.TestCase):
    def bright(self, **over):
        s = EngSim(c=cfg(**over) if over else None, screen=60)
        s.lux = 400
        s.run(60)
        s.input()
        s.accrue = True
        return s

    def test_dims_after_idle_and_restores_on_input(self):
        s = self.bright()
        before = s.screen
        s.run(s.c["idle"]["dim_after"] + 10)
        self.assertEqual(s.e.snapshot(s.t)["screen"]["state"], "idle")
        self.assertLessEqual(s.screen, round(s.c["idle"]["dim_percent"] / 100 * 90) + 1)
        s.input()
        s.run(2)
        self.assertLessEqual(abs(s.screen - before), 2)                 # both ends settle within one step of the target
        self.assertEqual(s.e.snapshot(s.t)["screen"]["state"], "auto")

    def test_dim_is_a_fade_and_undim_is_fast(self):
        s = self.bright()
        t0 = s.t
        s.run(s.c["idle"]["dim_after"] + 10)
        fade = [w for w in s.of("screen") if w[0] >= t0]
        self.assertGreater(len(fade), 10)
        self.assertTrue(all(abs(a[2] - b[2]) <= 4 for a, b in zip(fade, fade[1:])))
        s.input()
        t1 = s.t
        s.run(1.5)
        back = [w for w in s.of("screen") if w[0] >= t1]
        self.assertLess(back[-1][0] - t1, 0.8, "back to full brightness in well under a second")

    def test_never_brightens_a_dark_screen_to_dim_it(self):
        s = EngSim(screen=10)
        s.lux = 5
        s.run(60)
        low = s.screen
        s.accrue = True
        s.run(300)
        self.assertLessEqual(s.screen, low)

    def test_inhibited_idle_does_not_dim(self):
        s = self.bright()
        s.inhibit = True
        before = s.screen
        s.run(200)
        self.assertEqual(s.screen, before)

    def test_idle_mode_off(self):
        s = self.bright(idle={"mode": "off"})
        before = s.screen
        s.run(300)
        self.assertEqual(s.screen, before)

    def test_defers_to_cinnamon_dim_and_does_not_call_it_manual(self):
        s = self.bright()
        s.csd = True
        before = s.screen
        s.run(s.c["idle"]["dim_after"] + 10)
        self.assertEqual(s.screen, before, "we do not dim when Cinnamon does")
        s.screen = 27                                                   # Cinnamon dims (nobody typing)
        s.run(20)
        self.assertEqual(s.screen, 27, "and we do not fight it")
        self.assertIsNone(s.e.screen.paused_until)
        s.input()
        s.screen = before                                               # Cinnamon restores on first input
        s.run(5)
        self.assertIsNone(s.e.screen.paused_until, "the restore is not a manual change")
        self.assertEqual(s.screen, before)

    def test_outside_change_while_idle_is_manual_unless_cinnamon_dims(self):
        s = self.bright()
        s.run(20)                                                       # nobody at the keys; csd False
        s.screen = 33                                                   # e.g. a script or the bar writes the brightness
        s.run(5)
        self.assertIsNotNone(s.e.screen.paused_until)
        self.assertEqual(s.screen, 33)

    def test_manual_change_during_cinnamon_dim_window_still_detected(self):
        s = self.bright()
        s.csd = True
        s.run(100)
        s.screen = 27
        s.run(5)
        s.input()
        s.screen = 80                                                   # user presses brightness-up: not the old level
        s.run(3)
        self.assertIsNotNone(s.e.screen.paused_until)

    def test_unknown_idle_time_never_dims(self):
        s = self.bright()
        s.idle = None
        before = s.screen
        s.run(300)
        self.assertEqual(s.screen, before)


# ------------------------------------------------------------------------------------ keyboard
class KeyboardTests(unittest.TestCase):
    def test_on_in_the_dark_off_in_the_light_with_hysteresis(self):
        s = EngSim(kbd=0)
        s.lux = 3
        s.run(30)
        self.assertGreater(s.kbd, 40)
        s.lux = 40                                                      # between on_below (25) and off_above (60)
        s.run(60)
        self.assertGreater(s.kbd, 0, "stays on inside the hysteresis band")
        s.lux = 200
        s.run(60)
        self.assertEqual(s.kbd, 0)
        s.lux = 40
        s.run(60)
        self.assertEqual(s.kbd, 0, "and stays off inside the band")

    def test_brighter_room_lowers_the_backlight(self):
        a, b = EngSim(), EngSim()
        a.lux, b.lux = 2, 20
        a.run(60), b.run(60)
        self.assertGreater(a.kbd, b.kbd)

    def test_idle_off_and_instant_back(self):
        s = EngSim(kbd=0)
        s.lux = 3
        s.run(30)
        s.input()
        s.accrue = True
        lit = s.kbd
        s.run(s.c["idle"]["kbd_off_after"] + 5)
        self.assertEqual(s.kbd, 0)
        self.assertEqual(s.e.snapshot(s.t)["kbd"]["state"], "idle")
        s.input()
        s.run(1)
        self.assertEqual(s.kbd, lit)
        self.assertIsNone(s.e.kbd.paused_until, "our own idle-off is never a manual change")

    def test_kbd_off_after_zero_never_turns_off(self):
        s = EngSim(c=cfg(idle={"kbd_off_after": 0.0}))
        s.lux = 3
        s.run(30)
        lit = s.kbd
        s.accrue = True
        s.run(600)
        self.assertEqual(s.kbd, lit)

    def test_disabled_sections_are_left_alone(self):
        s = EngSim(c=cfg(keyboard={"enabled": False}), kbd=77)
        s.lux = 3
        s.run(60)
        self.assertEqual(s.kbd, 77)
        self.assertEqual(s.of("kbd"), [])


# ------------------------------------------------------------------------------ fake sysfs
class FakeTree:
    """A fake sysfs/proc tree plus a fake Cinnamon session."""

    def __init__(self, lux=150, screen=50, kbd=0, smax=90, kmax=255, als_dir="iio:device0"):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.paths = Paths(self.root)
        self.als_dir = als_dir
        d = os.path.join(self.paths.iio, als_dir)
        os.makedirs(d)
        self.put(os.path.join(d, "name"), "als")
        self.put(os.path.join(d, "in_illuminance_scale"), "1.000000000")
        self.put(os.path.join(d, "in_illuminance_offset"), "0")
        self.set_lux(lux)
        os.makedirs(self.paths.backlight)
        self.put(os.path.join(self.paths.backlight, "max_brightness"), smax)
        self.put(os.path.join(self.paths.backlight, "brightness"), screen)
        self.put(os.path.join(self.paths.backlight, "actual_brightness"), screen)
        os.makedirs(self.paths.led)
        self.put(os.path.join(self.paths.led, "max_brightness"), kmax)
        self.put(os.path.join(self.paths.led, "brightness"), kbd)
        ac = os.path.join(self.paths.power_supply, "ADP1")
        os.makedirs(ac)
        self.put(os.path.join(ac, "type"), "Mains")
        self.put(os.path.join(ac, "online"), 1)
        os.makedirs(os.path.dirname(self.paths.lid_glob.replace("*", "LID0")))
        self.lid(False)
        self.rt = os.path.join(self.root, "run")
        os.makedirs(self.rt)
        self.session = FakeSession()

    @staticmethod
    def put(path, value):
        with open(path, "w") as f:
            f.write(str(value) + "\n")

    def set_lux(self, v):
        self.put(os.path.join(self.paths.iio, self.als_dir, "in_illuminance_raw"), v)

    def lid(self, closed):
        self.put(self.paths.lid_glob.replace("*", "LID0"), "state:      closed" if closed else "state:      open")

    def screen(self):
        return int(slurp(os.path.join(self.paths.backlight, "brightness")))

    def kbd(self):
        return int(slurp(os.path.join(self.paths.led, "brightness")))

    def user_sets_screen(self, v):                                     # a brightness key: root-side write
        self.put(os.path.join(self.paths.backlight, "brightness"), v)
        self.put(os.path.join(self.paths.backlight, "actual_brightness"), v)
        self.session.idle = 0.0

    def make_daemon(self, c=None):
        hw = Hardware(self.paths, "als", FakeWriter(self.paths), self.session)
        boot = [0.0]
        d = Daemon(hw, c or cfgmod.defaults(), self.rt, os.path.join(self.root, "state", "state.json"),
                   boot_clock=lambda: boot[0] + d.t_offset)
        d.t_offset = 0.0
        d.sim_boot = boot
        return d

    def cleanup(self):
        self.tmp.cleanup()


class FakeWriter(SysfsWriter):
    """Like the kernel: writing `brightness` also moves `actual_brightness`."""

    def set(self, subsystem, name, value):
        super().set(subsystem, name, value)
        if subsystem == "backlight":
            FakeTree.put(os.path.join(self.paths.backlight, "actual_brightness"), int(value))


class FakeSession:
    def __init__(self):
        self.idle, self.inh, self.csd = 0.0, False, False

    def idle_s(self):
        return self.idle

    def inhibited(self):
        return self.inh

    def csd_idle_dim(self, on_ac):
        return self.csd


def drive(tree, d, secs, start=0.0, away=False):
    """Run the real Daemon.tick against the fake tree on a simulated clock. away=True: nobody touches
    the machine, so idle time grows."""
    t = start
    end = start + secs
    while t < end:
        delay = d.tick(t)
        t += delay
        tree.session.idle = tree.session.idle + delay if away else 0.0
        d.t_offset = t - start
    return t


class SysfsTests(unittest.TestCase):
    def setUp(self):
        self.tree = FakeTree()
        self.addCleanup(self.tree.cleanup)

    def test_als_reads_scale_and_offset(self):
        p = os.path.join(self.tree.paths.iio, "iio:device0")
        FakeTree.put(os.path.join(p, "in_illuminance_scale"), "0.5")
        FakeTree.put(os.path.join(p, "in_illuminance_offset"), "10")
        FakeTree.put(os.path.join(p, "in_illuminance_raw"), "590")
        self.assertAlmostEqual(Als(self.tree.paths.iio, sleep=lambda s: None).read(), 300.0)

    def test_als_prefers_input_attribute(self):
        p = os.path.join(self.tree.paths.iio, "iio:device0")
        FakeTree.put(os.path.join(p, "in_illuminance_input"), "123.5")
        self.assertEqual(Als(self.tree.paths.iio, sleep=lambda s: None).read(), 123.5)

    def test_als_found_by_name_not_number_and_rediscovered(self):
        als = Als(self.tree.paths.iio, sleep=lambda s: None)
        als.read()
        old = os.path.join(self.tree.paths.iio, "iio:device0")
        new = os.path.join(self.tree.paths.iio, "iio:device3")
        os.rename(old, new)                                             # iBridge re-enumerated
        with self.assertRaises(SensorError):
            als.read()
        als.next_scan = 0
        self.assertEqual(als.read(), 150.0)
        self.assertTrue(als.dev.endswith("iio:device3"))

    def test_als_other_devices_ignored(self):
        other = os.path.join(self.tree.paths.iio, "iio:device1")
        os.makedirs(other)
        FakeTree.put(os.path.join(other, "name"), "accel_3d")
        FakeTree.put(os.path.join(other, "in_illuminance_raw"), "9999")
        self.assertEqual(Als(self.tree.paths.iio, sleep=lambda s: None).read(), 150.0)

    def test_als_garbage_raises(self):
        p = os.path.join(self.tree.paths.iio, "iio:device0", "in_illuminance_raw")
        for junk in ("", "abc", "-5", "nan", "inf"):
            FakeTree.put(p, junk)
            als = Als(self.tree.paths.iio, sleep=lambda s: None)
            with self.assertRaises(SensorError, msg=junk):
                als.read()

    def test_waits_out_the_sensor_warm_up_after_a_gap(self):
        p = os.path.join(self.tree.paths.iio, "iio:device0", "in_illuminance_raw")
        warm = iter([38, 57, 127, 155, 161, 170, 170, 170])             # measured wake-up sequence
        now = [100.0]

        def fake_sleep(_):
            FakeTree.put(p, next(warm))
            now[0] += 0.2

        FakeTree.put(p, 20)
        als = Als(self.tree.paths.iio, sleep=fake_sleep, clock=lambda: now[0])
        FakeTree.put(p, next(warm))                                     # first read lands on a cold sensor
        v = als.read()
        self.assertGreaterEqual(v, 155, "the cold 38/57/127 readings must not be returned")
        now[0] += 1.0                                                   # steady 1 s polling: no settling, one read
        FakeTree.put(p, 171)
        self.assertEqual(als.read(), 171)
        now[0] += 5.0                                                   # a gap longer than the autosuspend: settle again
        FakeTree.put(p, 40)
        warm = iter([60, 130, 160, 165, 165])
        self.assertGreaterEqual(als.read(), 160)

    def test_missing_sensor_is_an_error_not_a_crash(self):
        with tempfile.TemporaryDirectory() as e:
            als = Als(os.path.join(e, "none"))
            with self.assertRaises(SensorError):
                als.read()

    def test_level_and_lid_and_ac(self):
        hw = Hardware(self.tree.paths, "als", SysfsWriter(self.tree.paths))
        self.assertEqual((hw.screen_max(), hw.read_screen(), hw.kbd_max(), hw.read_kbd()), (90, 50, 255, 0))
        self.assertFalse(hw.lid_closed())
        self.tree.lid(True)
        self.assertTrue(hw.lid_closed())
        self.assertTrue(hw.on_ac())
        FakeTree.put(os.path.join(self.tree.paths.power_supply, "ADP1", "online"), 0)
        self.assertFalse(hw.on_ac())
        hw.set_screen(33)
        hw.set_kbd(100)
        self.assertEqual((self.tree.screen(), self.tree.kbd()), (33, 100))
        self.assertIsNone(hw.idle_s())                                  # no session bus: idle unknown
        self.assertFalse(hw.csd_dims())

    def test_missing_devices_report_zero(self):
        self.assertEqual(Level("/nonexistent").max(), 0)
        self.assertIsNone(Level("/nonexistent").read())


class DaemonTests(unittest.TestCase):
    def setUp(self):
        self.tree = FakeTree(lux=590, screen=20)
        self.addCleanup(self.tree.cleanup)
        self.d = self.tree.make_daemon()

    def test_follows_light_end_to_end(self):
        drive(self.tree, self.d, 60)
        want = round(curve_pct(cfgmod.SCREEN_CURVE, 590) / 100 * 90)
        self.assertLessEqual(abs(self.tree.screen() - want), 1)
        self.assertEqual(self.tree.kbd(), 0)

    def test_dark_room_turns_keyboard_on_and_dims_screen(self):
        self.tree.set_lux(2)
        drive(self.tree, self.d, 60)
        self.assertGreater(self.tree.kbd(), 100)
        self.assertLess(self.tree.screen(), 15)

    def test_manual_change_pauses_and_state_file_says_so(self):
        drive(self.tree, self.d, 60)
        self.tree.user_sets_screen(12)
        t = drive(self.tree, self.d, 30, start=60)
        self.assertEqual(self.tree.screen(), 12)
        st = jload(self.d.state_path)
        self.assertEqual(st["screen"]["state"], "paused")
        self.assertGreater(st["screen"]["resume_in"], 0)
        self.assertEqual(st["v"], 1)

    def test_state_file_published_atomically_and_only_on_change(self):
        t = drive(self.tree, self.d, 60)
        mtime = os.stat(self.d.state_path).st_mtime_ns
        self.assertTrue(stat.S_ISREG(os.stat(self.d.state_path).st_mode))
        self.assertEqual([f for f in os.listdir(self.tree.rt) if f.endswith(".tmp")], [])
        self.d.publish(t, force=True)
        mtime = os.stat(self.d.state_path).st_mtime_ns
        t = drive(self.tree, self.d, 10, start=t)                       # nothing changes -> no rewrite
        self.assertEqual(os.stat(self.d.state_path).st_mtime_ns, mtime)
        drive(self.tree, self.d, 25, start=t)                           # ... but a slow tick keeps it fresh
        self.assertNotEqual(os.stat(self.d.state_path).st_mtime_ns, mtime)

    def test_fifo_actions_allowlist_and_garbage(self):
        self.d.open_fifo()
        self.addCleanup(lambda: os.close(self.d.fifo))
        self.assertTrue(stat.S_ISFIFO(os.stat(self.d.fifo_path).st_mode))
        fd = os.open(self.d.fifo_path, os.O_WRONLY | os.O_NONBLOCK)
        os.write(fd, b"toggle\n$(rm -rf ~)\nBRIGHTER\n\x00\xff\xfe\n" + b"x" * 9000 + b"\nresume\nnope\n")
        os.close(fd)
        got = self.d.read_actions()
        self.assertEqual(got[:2], ["toggle", "brighter"])
        self.assertTrue(set(got) <= set(ACTIONS))

    def test_toggle_via_handle_persists_and_survives_restart(self):
        drive(self.tree, self.d, 10)
        self.d.handle("toggle", 10.0)
        self.assertFalse(self.d.engine.enabled)
        self.d.handle("brighter", 10.0)
        again = self.tree.make_daemon()
        self.assertFalse(again.engine.enabled)
        self.assertEqual(again.engine.bias, 5.0)
        held = self.tree.screen()
        drive(self.tree, again, 30)
        self.assertEqual(self.tree.screen(), held)
        self.assertEqual(jload(again.state_path)["screen"]["state"], "off")

    def test_idle_dim_end_to_end(self):
        drive(self.tree, self.d, 60)
        bright = self.tree.screen()
        t = drive(self.tree, self.d, 90, start=60, away=True)
        self.assertLess(self.tree.screen(), bright - 20)
        self.tree.session.idle = 0.0
        drive(self.tree, self.d, 3, start=t)
        self.assertLessEqual(abs(self.tree.screen() - bright), 1)

    def test_sensor_unplugged_mid_run_and_back(self):
        drive(self.tree, self.d, 60)
        held = self.tree.screen()
        os.rename(os.path.join(self.tree.paths.iio, "iio:device0"), os.path.join(self.tree.paths.iio, "gone"))
        self.tree.set_lux = lambda v: None
        t = drive(self.tree, self.d, 60, start=60)
        self.assertEqual(self.tree.screen(), held)
        self.assertEqual(jload(self.d.state_path)["sensor"], "lost")
        os.rename(os.path.join(self.tree.paths.iio, "gone"), os.path.join(self.tree.paths.iio, "iio:device5"))
        self.d.hw.als.next_scan = 0
        FakeTree.put(os.path.join(self.tree.paths.iio, "iio:device5", "in_illuminance_raw"), 3)
        drive(self.tree, self.d, 90, start=t)
        self.assertLess(self.tree.screen(), held)
        self.assertEqual(jload(self.d.state_path)["sensor"], "ok")

    def test_suspend_resume_is_not_a_manual_change(self):
        drive(self.tree, self.d, 60)
        self.d.sim_boot[0] += 3600                                      # an hour asleep: BOOTTIME runs, MONOTONIC does not
        self.tree.user_sets_screen(30)                                  # firmware restored something else
        drive(self.tree, self.d, 10, start=60)
        self.assertIsNone(self.d.engine.screen.paused_until)

    def test_no_runtime_dir_no_problem(self):
        d = self.tree.make_daemon()
        d.state_path = "/nonexistent/dir/barkeep-autolight"
        d.fifo_path = d.state_path + ".ctl"
        d.open_fifo()
        self.assertIsNone(d.fifo)
        drive(self.tree, d, 20)                                         # must not raise

    def test_reload_keeps_persisted_state(self):
        self.d.handle("off", 0.0)
        self.d.config_loader = lambda: cfg(screen={"min_percent": 20.0})
        self.d.handle("reload", 1.0)
        self.assertFalse(self.d.engine.enabled)
        self.assertEqual(self.d.engine.screen.min, 18)

    def test_no_barkeep_files_needed_or_touched(self):
        for name in os.listdir(self.tree.rt):
            self.assertFalse(name.startswith("barkeep-focus"))
        drive(self.tree, self.d, 20)
        self.assertEqual(sorted(os.listdir(self.tree.rt)), ["barkeep-autolight"])


# ----------------------------------------------------------------------------------------- ctl
class CtlTests(unittest.TestCase):
    def test_status_without_daemon_and_unknown_command(self):
        with tempfile.TemporaryDirectory() as rt:
            old = os.environ.get("XDG_RUNTIME_DIR")
            os.environ["XDG_RUNTIME_DIR"] = rt
            try:
                out, err = io.StringIO(), io.StringIO()
                real = sys.stdout, sys.stderr
                sys.stdout, sys.stderr = out, err
                try:
                    self.assertEqual(ctl.main(["status"]), 1)
                    self.assertEqual(ctl.main(["toggle"]), 1)           # no FIFO: says so, does not hang
                    self.assertEqual(ctl.main(["frobnicate"]), 2)
                finally:
                    sys.stdout, sys.stderr = real
                self.assertIn("not running", out.getvalue() + err.getvalue())
            finally:
                if old is None:
                    del os.environ["XDG_RUNTIME_DIR"]
                else:
                    os.environ["XDG_RUNTIME_DIR"] = old

    def test_describe_tolerates_garbage(self):
        self.assertIn("auto on", ctl.describe({"enabled": True, "lux": 12.3, "screen": {"state": "auto", "level": 5, "max": 90},
                                               "kbd": None, "sensor": "ok", "lid": "open", "bias": 0}))
        ctl.describe({"screen": "x", "kbd": [1], "lux": "y"})

    def test_probe_on_fake_tree(self):
        tree = FakeTree()
        self.addCleanup(tree.cleanup)
        out = io.StringIO()
        real, sys.stdout = sys.stdout, out
        try:
            rc = ctl.probe(0.6, root=tree.root)
        finally:
            sys.stdout = real
        self.assertEqual(rc, 0)
        self.assertIn("OK", out.getvalue())


class ProcessTests(unittest.TestCase):
    """The real autolightd process (run loop, FIFO, signals) against a fake tree, real clock, no session bus."""

    def wait_for(self, fn, secs=8.0):
        import time
        end = time.time() + secs
        while time.time() < end:
            v = fn()
            if v:
                return v
            time.sleep(0.1)
        return None

    def test_daemon_process_end_to_end(self):
        import signal
        import subprocess
        tree = FakeTree(lux=3, screen=40, kbd=0)
        self.addCleanup(tree.cleanup)
        env = dict(os.environ, XDG_RUNTIME_DIR=tree.rt, XDG_STATE_HOME=os.path.join(tree.root, "state"),
                   XDG_CONFIG_HOME=os.path.join(tree.root, "cfg"), DBUS_SESSION_BUS_ADDRESS="unix:path=/nonexistent",
                   PYTHONDONTWRITEBYTECODE="1")
        here = os.path.dirname(os.path.abspath(__file__))
        proc = subprocess.Popen([sys.executable, "-I", os.path.join(here, "..", "bin", "autolightd"), "--root", tree.root],
                                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        state = os.path.join(tree.rt, "barkeep-autolight")
        self.assertTrue(self.wait_for(lambda: os.path.exists(state) and tree.kbd() > 100),
                        "dark room: keyboard light on within a few seconds")
        self.assertLess(tree.screen(), 40, "and the panel follows the dark")
        r = subprocess.run([sys.executable, "-I", os.path.join(here, "..", "bin", "autolightctl"), "off"], env=env)
        self.assertEqual(r.returncode, 0)
        self.assertTrue(self.wait_for(lambda: jload(state)["enabled"] is False))
        out = subprocess.run([sys.executable, "-I", os.path.join(here, "..", "bin", "autolightctl"), "status"],
                             env=env, capture_output=True, text=True).stdout
        self.assertIn("auto off", out)
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(proc.wait(timeout=5), 0)
        self.assertEqual([f for f in os.listdir(tree.rt) if f.startswith("barkeep-autolight")], [], "cleans up after itself")
        self.assertEqual(jload(os.path.join(tree.root, "state", "autolight", "state.json"))["enabled"], False)

    def test_no_hardware_exits_with_config_error(self):
        import subprocess
        with tempfile.TemporaryDirectory() as empty:
            env = dict(os.environ, XDG_RUNTIME_DIR=empty, DBUS_SESSION_BUS_ADDRESS="unix:path=/nonexistent")
            here = os.path.dirname(os.path.abspath(__file__))
            r = subprocess.run([sys.executable, "-I", os.path.join(here, "..", "bin", "autolightd"), "--root", empty],
                               env=env, capture_output=True, timeout=20)
            self.assertEqual(r.returncode, 78)                          # the unit does not restart-loop on this


if __name__ == "__main__":
    unittest.main(verbosity=1)
