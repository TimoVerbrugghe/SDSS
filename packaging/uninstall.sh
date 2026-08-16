#!/usr/bin/env bash
# Remove SDSS from this SteamOS device.
#
#   packaging/uninstall.sh [--keep-configs] [--yes]
#
# Emulator configuration is restored first and separately: the patch journal and the
# config backups live under $XDG_STATE_HOME/sdss, which this script deletes, so removing
# state before restoring would strand every emulator on an SDSS config with no way back.
set -euo pipefail

KEEP_CONFIGS=0
ASSUME_YES=0

usage() {
    cat <<'EOF'
Usage: packaging/uninstall.sh [--keep-configs] [--yes]

Restores every emulator config SDSS patched, then removes the installed release, the
`sdss` shim, the desktop launcher, the Decky plugin and the container image. The SDSS
AppImage is left in place; delete ~/Applications/SDSS.AppImage to remove it too.

  --keep-configs  leave emulator configs patched (they will point at a missing SDSS)
  --yes           do not ask for confirmation

The udev rule under /etc is left in place; it is inert without SDSS. Remove it with:
    sudo rm -f /etc/udev/rules.d/60-sdss-input.rules \
               /etc/atomic-update.conf.d/sdss-atomic-update.conf
EOF
}

while (($#)); do
    case "$1" in
        --keep-configs) KEEP_CONFIGS=1; shift ;;
        --yes|-y) ASSUME_YES=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
done

INSTALL_ROOT="${XDG_DATA_HOME:-$HOME/.local/share}/sdss/release"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/sdss"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/sdss"
BIN_DIR="$HOME/.local/bin"
APPLICATIONS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
PLUGIN_DIR="$HOME/homebrew/plugins/SDSS"
IMAGE="${SDSS_COMPOSITOR_IMAGE:-localhost/sdss-compositor:latest}"
STEAM_ROOT="$HOME/.steam/steam"
# Read before the config directory is deleted below.
ROLE=""
[[ -r "$CONFIG_DIR/installed-role" ]] && ROLE="$(<"$CONFIG_DIR/installed-role")"

if ((!ASSUME_YES)); then
    # `read` returns 1 at EOF; under `set -e` that would abort with no message.
    read -r -p "Remove SDSS from this device? [y/N] " answer || answer=""
    [[ "$answer" =~ ^[Yy] ]] || { echo "aborted"; exit 1; }
fi

if ((!KEEP_CONFIGS)); then
    # Best effort: a half-installed tree should still let the rest of the uninstall run.
    if [[ -x "$BIN_DIR/sdss" ]]; then
        echo "restoring emulator configs ..."
        "$BIN_DIR/sdss" restore || echo "restore failed; configs left patched" >&2
    elif [[ -d "$INSTALL_ROOT/host/src" ]]; then
        echo "restoring emulator configs ..."
        PYTHONPATH="$INSTALL_ROOT/host/src" python3 -m sdss.cli restore \
            || echo "restore failed; configs left patched" >&2
    else
        echo "no sdss command found; skipping config restore" >&2
    fi
fi

if [[ -d "$PLUGIN_DIR" ]]; then
    # Decky owns ~/homebrew as root, so removing the plugin needs the same elevation the
    # installer used to write it.
    SUDO=()
    [[ -w "$(dirname "$PLUGIN_DIR")" ]] || SUDO=(sudo)
    "${SUDO[@]}" rm -rf "$PLUGIN_DIR"
    echo "removed $PLUGIN_DIR"
fi

# Keyed off the artifacts themselves, not the recorded role: `deck/install.sh --host ...`
# is a supported direct entry point that predates (and can bypass) installed-role, and
# uninstall must still clean up after it.
DECK_TEMPLATE="$STEAM_ROOT/controller_base/templates/sdss_second_screen.vdf"
if [[ "$ROLE" == "steam-deck" || -e "$BIN_DIR/sdss-connect" || -e "$DECK_TEMPLATE" ]]; then
    if [[ -f "$INSTALL_ROOT/deck/add-steam-shortcut.py" ]]; then
        # --force: in Game Mode Steam is always running, and refusing here would mean the
        # shortcut can never be removed. Steam may rewrite the file on exit, which is why
        # the message below tells the user what to check.
        python3 "$INSTALL_ROOT/deck/add-steam-shortcut.py" --remove --force \
            || echo "could not remove the Steam shortcut; delete it from your library" >&2
    fi
    rm -f "$BIN_DIR/sdss-connect"
    rm -f "$DECK_TEMPLATE"
    echo "removed the deck launcher, Steam shortcut and controller template"
fi

rm -f "$BIN_DIR/sdss" "$APPLICATIONS/sdss.desktop"
# The pre-app entry name. Removed unconditionally so an upgrade-then-uninstall does not
# strand a launcher pointing at a release that no longer exists.
rm -f "$APPLICATIONS/sdss-installer.desktop"
rm -rf "$INSTALL_ROOT" "$STATE_DIR" "$CONFIG_DIR"
# Installers before the mktemp-container fix could leave a full copy of the release behind
# on every successful update. They are invisible (dot-prefixed) and can be gigabytes.
find "$(dirname "$INSTALL_ROOT")" -maxdepth 1 \
    \( -name '.previous.*' -o -name '.release.*' \) -exec rm -rf {} + 2>/dev/null || true
rmdir "$(dirname "$INSTALL_ROOT")" 2>/dev/null || true
echo "removed the installed release, shim and launcher"

if command -v podman >/dev/null && podman image exists "$IMAGE" 2>/dev/null; then
    podman rmi -f "$IMAGE" >/dev/null && echo "removed the $IMAGE image"
fi

cat <<'EOF'

SDSS is uninstalled.

Sunshine and Moonlight were installed as flatpaks and are left alone; remove them with
`flatpak uninstall --user dev.lizardbyte.app.Sunshine` if you no longer want them.

The SDSS app itself (~/Applications/SDSS.AppImage) is left in place: it is a single file
that can reinstall SDSS at any time. Delete it if you no longer want it.
EOF
