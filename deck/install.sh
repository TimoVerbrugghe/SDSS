#!/usr/bin/env bash
# Install the SDSS client bits on a Steam Deck. Touches only $HOME.
set -euo pipefail

BIN_DIR="$HOME/.local/bin"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOST=""

usage() {
  echo "usage: $(basename "$0") --host <steam-machine-ip-or-name>" >&2
}

while (($#)); do
  case "$1" in
    --host) HOST="${2:?--host needs a value}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) usage; exit 2 ;;
  esac
done

[[ -n "$HOST" ]] || { usage; exit 2; }

# Steam rewrites shortcuts.vdf from memory when it exits, silently discarding edits
# made while it runs. add-steam-shortcut.py refuses in that case, so ask here and pass
# the override through rather than letting the user hit the same wall twice.
SHORTCUT_ARGS=()
# `pgrep -x steam` misses the case that actually matters: in Game Mode the process is
# steamwebhelper / a differently-named wrapper, so an exact match on "steam" reports
# "not running" and the shortcut is silently discarded when Steam next exits.
if pgrep -x steam >/dev/null 2>&1 || pgrep -x steamwebhelper >/dev/null 2>&1; then
  echo "Steam is running. Close Steam (or return to Game Mode later) before adding" >&2
  echo "the shortcut, otherwise Steam will overwrite it on exit." >&2
  # `read` returns 1 at EOF; under `set -e` that would abort with no message.
  read -r -p "Continue anyway? [y/N] " answer || answer=""
  [[ "$answer" =~ ^[Yy] ]] || exit 1
  SHORTCUT_ARGS=(--force)
fi

echo "installing Moonlight ..."
# shellcheck source=../packaging/common.sh
source "$HERE/../packaging/common.sh"
# A failed remote-add or install must stop here: continuing would add a Steam shortcut
# for a launcher that can never work, which looks like SDSS itself is broken.
install_flatpak com.moonlight_stream.Moonlight "Moonlight" || exit 1

mkdir -p "$BIN_DIR"
install -m 0755 "$HERE/sdss-connect.sh" "$BIN_DIR/sdss-connect"
echo "installed $BIN_DIR/sdss-connect"
# Under `set -e` a refusal here (Steam running, unparseable shortcuts.vdf) would abort the
# script before the guidance below is printed, leaving the user with a half-installed deck
# and no hint about what to do. The launcher itself is already in place and usable.
if ! python3 "$HERE/add-steam-shortcut.py" "${SHORTCUT_ARGS[@]+"${SHORTCUT_ARGS[@]}"}" "$HOST"; then
  echo "could not add the Steam shortcut; sdss-connect is installed and can be run" >&2
  echo "from the terminal, or re-run this script once Steam is closed." >&2
fi
python3 "$HERE/install-controller-template.py" || echo "controller template skipped" >&2

# Recorded here too, not only by the top-level install.sh: this script is a supported
# direct entry point, and uninstall reads the role to know what to clean up.
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/sdss"
mkdir -p "$CONFIG_DIR"
printf '%s\n' "steam-deck" > "$CONFIG_DIR/installed-role"
# The address is otherwise only recoverable by parsing it back out of shortcuts.vdf, which
# Steam rewrites; the app shows it and reuses it when re-running this installer.
printf '%s\n' "$HOST" > "$CONFIG_DIR/host"

note_path "$BIN_DIR"

cat <<EOF

Next:
  1. Start a game on the Steam Machine with second screen mode enabled.
  2. Pair once:   sdss-connect $HOST --pair 1234
  3. Connect:     sdss-connect $HOST

Important (touch):
  In Steam Controller Settings for the "Second Screen" shortcut, pick the
  installed template: Templates -> "SDSS - Second Screen".
  It is the stock "Gamepad with Joystick Trackpad" layout plus an always-on
  System -> Touchscreen Native Support command, which touch needs.
EOF
