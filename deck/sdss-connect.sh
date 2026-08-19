#!/usr/bin/env bash
# Steam Deck side of SDSS: pair with the Steam Machine once, then open the second screen.
#
# Usage:
#   sdss-connect.sh <steam-machine-ip> [--pair <pin>]
#
# Pairing needs the PIN to be accepted on the host. The host-side `sdss` session runs
# Sunshine with `-0`, so answer with:  echo <pin> > "$XDG_RUNTIME_DIR/sdss/session/pin"
set -euo pipefail

MOONLIGHT_ID="com.moonlight_stream.Moonlight"
APP_NAME="Second Screen"
# The Deck's panel; the host's headless output is configured to match.
RESOLUTION="${SDSS_RESOLUTION:-1280x800}"
FPS="${SDSS_FPS:-60}"

host="${1:-}"
if [[ -z "$host" ]]; then
    echo "usage: $(basename "$0") <steam-machine-ip> [--pair <pin>]" >&2
    exit 2
fi
shift

moonlight() { env QT_QPA_PLATFORM=offscreen flatpak run "$MOONLIGHT_ID" "$@"; }

if [[ "${1:-}" == "--pair" ]]; then
    pin="${2:?--pair needs a 4 digit PIN}"
    echo "pairing with $host using PIN $pin"
    # Printed literally rather than as $XDG_RUNTIME_DIR: this is a copy-paste instruction
    # for a *different* machine, where the variable would expand to that shell's value (or
    # to nothing over SSH) instead of the host session's runtime dir.
    echo "on the Steam Machine, run: echo $pin > /run/user/1000/sdss/session/pin"
    moonlight pair "$host" --pin "$pin"
    # Pairing is a complete operation. Falling through would append the leftover
    # `--pair <pin>` to the stream command, and on a first-time pair the app is not listed
    # yet — so a *successful* pair would exit 1 with "host is not paired".
    exit 0
fi

# Captured first rather than piped into `grep -q`: grep exits on the first match, the
# (flatpak-wrapped, slow) producer gets SIGPIPE, and under `pipefail` the status becomes
# 141 — reporting a genuinely paired host as unpaired.
apps="$(moonlight list "$host" 2>/dev/null || true)"
if ! grep -qx "$APP_NAME" <<<"$apps"; then
    echo "host $host is not paired (or the SDSS session is not running)" >&2
    echo "pair first: $(basename "$0") $host --pair 1234" >&2
    exit 1
fi

# Do NOT `exec` here. Flatpak's own sandbox helper reparents the real moonlight
# process (and its bwrap wrapper) to the user's systemd, detached from whatever
# spawned `flatpak run` -- so when Steam's reaper sends SIGTERM to this script's
# PID, killing that PID (or even exec'ing straight into flatpak and killing the
# result) never reaches the actual streaming process. Steam's `steam://closeapp`
# / `steam://stopgame` then reports success while `bwrap`+`moonlight` keep running
# forever, which is exactly the "artifact left behind after a Steam stop" failure
# mode this project treats as a hard bug. Instead: launch in the background, trap
# the signals Steam's reaper actually sends, and on any of them explicitly ask
# Flatpak to kill this specific app instance before this script exits.
# Touch reliability depends primarily on the Steam Input layout for this shortcut:
# add the Always-On command `System -> Touchscreen Native Support`.
# Keep `--no-touchscreen-trackpad` as a Moonlight-side guardrail to avoid trackpad
# emulation paths on clients that default touchscreen to trackpad behavior.
cleanup() {
    # Idempotent: harmless if moonlight already exited on its own.
    flatpak kill "$MOONLIGHT_ID" 2>/dev/null || true
}
trap cleanup EXIT TERM INT HUP

flatpak run "$MOONLIGHT_ID" stream "$host" "$APP_NAME" \
    --resolution "$RESOLUTION" --fps "$FPS" \
    --display-mode fullscreen --no-vsync \
    --no-touchscreen-trackpad "$@" &
child=$!
wait "$child"
