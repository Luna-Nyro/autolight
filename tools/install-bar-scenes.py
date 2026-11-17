#!/usr/bin/env python3
"""Merge autolight's optional Touch Bar scene (dist/barkeep-scenes.json) into barkeep's scenes.json.
Idempotent: a scene of the same name is replaced, everything else is left alone, and the first run
keeps a backup. Run as your user:  python3 tools/install-bar-scenes.py [--remove]
Does nothing (and says so) when barkeep's config directory does not exist."""
import json, os, shutil, sys, tempfile

here = os.path.dirname(os.path.abspath(__file__))
src = os.path.join(here, "..", "dist", "barkeep-scenes.json")
dst = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "barkeep", "scenes.json")
remove = "--remove" in sys.argv
if not os.path.isdir(os.path.dirname(dst)):
    print("barkeep is not set up (no %s); skipping the Touch Bar scene" % os.path.dirname(dst))
    sys.exit(0)

bindir = os.path.join(os.path.expanduser("~"), ".local", "bin")
text = open(src).read().replace("{BIN}", bindir)
mine = json.loads(text)["scenes"]
names = {s["name"] for s in mine}
try:
    doc = json.load(open(dst))
except FileNotFoundError:
    doc = {"scenes": []}
except ValueError as e:
    sys.exit("not touching %s: it is not valid JSON (%s)" % (dst, e))
if os.path.exists(dst) and not os.path.exists(dst + ".bak-before-autolight"):
    shutil.copy2(dst, dst + ".bak-before-autolight")
rest = [s for s in doc.get("scenes", []) if s.get("name") not in names]
doc["scenes"] = rest if remove else rest + mine
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dst))
with os.fdopen(fd, "w") as f:
    json.dump(doc, f, indent=2, ensure_ascii=False)
os.replace(tmp, dst)          # barkeep reloads on change; a key-only scene is invisible until you open it
print(("removed" if remove else "installed"), len(mine), "autolight scene in", dst)
