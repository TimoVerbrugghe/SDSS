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
STOPPING=0
cleanup() {
    STOPPING=1
    # Idempotent: harmless if moonlight already exited on its own.
    flatpak kill "$MOONLIGHT_ID" 2>/dev/null || true
}
trap cleanup EXIT TERM INT HUP

# The host tears down and rebuilds its whole Sunshine + compositor session on every game
# launch/exit (verified on hardware, docs/architecture.md) rather than keeping one running
# for as long as SDSS is enabled. Moonlight does not treat that as a dropped connection to
# retry: verified on hardware that it stays running, reports normal-looking CPU usage
# briefly then goes idle, and just keeps showing the last frame from the vanished session
# indefinitely -- it does not reconnect on its own even once the host's next session is up.
# CPU ticks are the signal used to notice this because it needs no protocol-level access to
# Moonlight: a stream that is actually decoding/rendering 60 fps burns non-trivial, continuous
# CPU; one stuck on a stale frame burns close to none. A stall this script itself caused by
# asking Flatpak to stop (STOPPING=1) is not a hang to recover from.
STALL_CHECK_INTERVAL=5
STALL_GRACE_SECONDS=15
MIN_CPU_TICKS_PER_CHECK=2

cpu_ticks_of() {
    awk '{print $14+$15}' "/proc/$1/stat" 2>/dev/null || echo ""
}

run_stream_once() {
    flatpak run "$MOONLIGHT_ID" stream "$host" "$APP_NAME" \
        --resolution "$RESOLUTION" --fps "$FPS" \
        --display-mode fullscreen --no-vsync \
        --no-touchscreen-trackpad "$@" &
    local wrapper_pid=$! moonlight_pid="" stalled_for=0 last_ticks="" ticks
    while kill -0 "$wrapper_pid" 2>/dev/null; do
        sleep "$STALL_CHECK_INTERVAL"
        [ "$STOPPING" = 1 ] && break
        if [ -z "$moonlight_pid" ] || ! kill -0 "$moonlight_pid" 2>/dev/null; then
            # `pgrep` exits 1 when nothing matches yet (moonlight still starting up); under
            # `pipefail` that would otherwise propagate through `| head -1` and trip `-e`.
            moonlight_pid=$(pgrep -x moonlight | head -1) || true
        fi
        [ -z "$moonlight_pid" ] && continue
        ticks=$(cpu_ticks_of "$moonlight_pid")
        [ -z "$ticks" ] && continue
        if [ -n "$last_ticks" ] && [ $((ticks - last_ticks)) -lt "$MIN_CPU_TICKS_PER_CHECK" ]; then
            stalled_for=$((stalled_for + STALL_CHECK_INTERVAL))
        else
            stalled_for=0
        fi
        last_ticks=$ticks
        if [ "$stalled_for" -ge "$STALL_GRACE_SECONDS" ]; then
            echo "second screen stream appears stalled (no host-side session reachable?) — reconnecting" >&2
            flatpak kill "$MOONLIGHT_ID" 2>/dev/null || true
            break
        fi
    done
    wait "$wrapper_pid" 2>/dev/null || true
}

while [ "$STOPPING" = 0 ]; do
    run_stream_once "$@"
done

