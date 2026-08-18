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
# Moonlight has no server-driven/native resolution mode (only fixed presets or an
# explicit --resolution), so the client has to be told what size to request. The host
# always sizes HEADLESS-1 to the Deck's own panel resolution (see
# sdss.compositor.DECK_PANEL_RESOLUTION), which never varies, so this is a fixed
# constant rather than something parsed out of the app name.
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
if ! grep -qxF "$APP_NAME" <<<"$apps"; then
    echo "host $host is not paired (or the SDSS session is not running)" >&2
    echo "pair first: $(basename "$0") $host --pair 1234" >&2
    exit 1
fi

# `exec` bypasses shell functions, so the flatpak has to be spelled out here.
# Touch reliability depends primarily on the Steam Input layout for this shortcut:
# add the Always-On command `System -> Touchscreen Native Support`.
# Keep `--no-touchscreen-trackpad` as a Moonlight-side guardrail to avoid trackpad
# emulation paths on clients that default touchscreen to trackpad behavior.
exec flatpak run "$MOONLIGHT_ID" stream "$host" "$APP_NAME" \
    --resolution "$RESOLUTION" --fps "$FPS" \
    --display-mode fullscreen --no-vsync \
    --no-touchscreen-trackpad "$@"
