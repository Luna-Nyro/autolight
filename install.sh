#!/bin/sh
# autolight installer. Runs as your user: no sudo, no root service, no kernel changes.
#   ./install.sh               install and start
#   ./install.sh uninstall     stop and remove everything it installed (keeps your config and settings)
#   ./install.sh uninstall --purge   ... and delete ~/.config/autolight and the saved state too
#   ./install.sh status
# Touch Bar scenes are merged into barkeep's scenes.json only when barkeep is present (optional).
set -eu
here=$(cd "$(dirname "$0")" && pwd)
lib="$HOME/.local/lib/autolight"
bin="$HOME/.local/bin"
unit="$HOME/.config/systemd/user/autolight.service"
cfg="$HOME/.config/autolight"
state="${XDG_STATE_HOME:-$HOME/.local/state}/autolight"

scenes() {   # best effort, never fails the install
	if [ -d "$HOME/.config/barkeep" ] && command -v barkeep >/dev/null 2>&1; then
		python3 "$here/tools/install-bar-scenes.py" "$@" || echo "note: Touch Bar scenes not changed"
	else
		echo "note: barkeep not found, skipping the optional Touch Bar scenes"
	fi
}

case "${1:-install}" in
uninstall)
	systemctl --user disable --now autolight.service 2>/dev/null || true
	rm -f "$unit" "$bin/autolightd" "$bin/autolightctl"
	rm -rf "$lib"
	systemctl --user daemon-reload 2>/dev/null || true
	scenes --remove
	if [ "${2:-}" = "--purge" ]; then
		rm -rf "$cfg" "$state"
		echo "autolight removed, config and state deleted"
	else
		echo "autolight removed; $cfg and $state kept (use --purge to delete them)"
	fi
	exit 0 ;;
status)
	systemctl --user --no-pager --lines=8 status autolight.service || true
	"$bin/autolightctl" status || true
	exit 0 ;;
install) ;;
*)
	echo "usage: $0 [install|uninstall [--purge]|status]" >&2
	exit 2 ;;
esac

python3 -c 'import gi, tomllib; gi.require_version("Gio", "2.0")' 2>/dev/null || {
	echo "need python3 (>= 3.11) and python3-gi:  sudo apt install python3-gi" >&2
	exit 1
}
[ -d /sys/class/backlight ] && [ -n "$(ls /sys/class/backlight 2>/dev/null)" ] || echo "warning: no /sys/class/backlight device found"

install -d "$lib" "$bin" "$HOME/.config/systemd/user" "$cfg"
rm -rf "$lib/autolight" "$lib/bin"
cp -r "$here/autolight" "$lib/autolight"
find "$lib/autolight" -name __pycache__ -prune -exec rm -rf {} +
install -d "$lib/bin"
install -m 755 "$here/bin/autolightd" "$here/bin/autolightctl" "$lib/bin/"
ln -sf "$lib/bin/autolightd" "$bin/autolightd"
ln -sf "$lib/bin/autolightctl" "$bin/autolightctl"
install -m 644 "$here/systemd/autolight.service" "$unit"
[ -e "$cfg/config.toml" ] || install -m 644 "$here/dist/config.example.toml" "$cfg/config.toml"

systemctl --user daemon-reload
systemctl --user enable autolight.service
systemctl --user restart autolight.service
scenes
sleep 1
systemctl --user --no-pager --lines=6 status autolight.service || true
echo
"$bin/autolightctl" status || true
echo "config: $cfg/config.toml   control: autolightctl {status|toggle|resume|brighter|dimmer|probe}"
