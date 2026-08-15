#!/usr/bin/env bash
# Steam Deck side of SDSS: pair with the Steam Machine once, then open the second screen.
#
# Usage:
#   sdss-connect.sh <steam-machine-ip> [--pair <pin>]
#
# Pairing needs the PIN to be accepted on the host. The host-side `sdss` session runs
# Sunshine with `-0`, so answer with:  echo <pin> > "$XDG_RUNTIME_DIR/sdss/pin"
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

moonlight() { flatpak run "$MOONLIGHT_ID" "$@"; }

if [[ "${1:-}" == "--pair" ]]; then
    pin="${2:?--pair needs a 4 digit PIN}"
    echo "pairing with $host using PIN $pin"
    echo "on the Steam Machine, run: echo $pin > \$XDG_RUNTIME_DIR/sdss/pin"
    QT_QPA_PLATFORM=offscreen moonlight pair "$host" --pin "$pin"
    exit $?
fi

if ! QT_QPA_PLATFORM=offscreen moonlight list "$host" 2>/dev/null | grep -qx "$APP_NAME"; then
    echo "host $host is not paired (or the SDSS session is not running)" >&2
    echo "pair first: $(basename "$0") $host --pair 1234" >&2
    exit 1
fi

exec moonlight stream "$host" "$APP_NAME" \
    --resolution "$RESOLUTION" --fps "$FPS" \
    --display-mode fullscreen --no-vsync "$@"
