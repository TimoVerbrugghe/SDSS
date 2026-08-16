#!/usr/bin/env bash
# Install SDSS on either endpoint. This script deliberately only supports SteamOS.
set -euo pipefail

ROLE=""
HOST=""
STAGE_ONLY=0
SOURCE=""
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_ROOT="${XDG_DATA_HOME:-$HOME/.local/share}/sdss/release"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/sdss"
# Set by install_release() and read by cleanup_on_exit(), which is registered with
# `trap ... EXIT` and therefore runs after install_release() has already returned — its
# `local` variables would be out of scope by then, so these must be script-scoped.
staging=""
previous=""
previous_dir=""

usage() {
    cat <<'EOF'
Usage: install.sh [--role steam-machine|steam-deck] [--host ADDRESS]
                  [--source DIR] [--stage-only]

Without --role, the previously installed role is reused, otherwise the endpoint is
detected from the device and confirmed, or chosen interactively. --host supplies the
Steam Machine address when installing on a Steam Deck. --stage-only copies the release
and desktop launcher without installing anything.

--source DIR updates the installed release from a newer checkout. This is what the
desktop launcher cannot do on its own: it runs the *installed* copy, so without --source
there is no newer code to copy and only the endpoint setup is re-run.
EOF
}

while (($#)); do
    case "$1" in
        --role) ROLE="${2:?--role needs a value}"; shift 2 ;;
        --host) HOST="${2:?--host needs a value}"; shift 2 ;;
        --source) SOURCE="${2:?--source needs a value}"; shift 2 ;;
        --stage-only) STAGE_ONLY=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
done

if [[ -n "$SOURCE" ]]; then
    # The tree named here is copied wholesale into $INSTALL_ROOT and parts of it are later
    # run under sudo (packaging/install-host.sh, packaging/install-udev-rule.sh). An
    # `install.sh` alone is a weak signal — almost any project has one — so require the
    # markers that make this specifically an SDSS checkout before trusting it that far.
    for marker in install.sh host/src/sdss/cli.py packaging/install-host.sh; do
        [[ -f "$SOURCE/$marker" && ! -L "$SOURCE/$marker" ]] || {
            echo "--source does not look like an SDSS checkout: missing $marker" >&2
            exit 2
        }
    done
    [[ -x "$SOURCE/install.sh" ]] || {
        echo "--source contains install.sh but it is not executable" >&2
        exit 2
    }
    HERE="$(cd "$SOURCE" && pwd)"
fi

detect_role() {
    # Steam Deck DMI product names: Jupiter (LCD), Galileo (OLED).
    # The Steam Machine reports Fremont. Anything else falls back to asking.
    local product
    product="$(cat /sys/devices/virtual/dmi/id/product_name 2>/dev/null || true)"
    case "$product" in
        Jupiter|Galileo) echo "steam-deck" ;;
        Fremont) echo "steam-machine" ;;
        *) echo "" ;;
    esac
}

choose_role() {
    if [[ -n "$ROLE" ]]; then
        return
    fi
    # A re-run (which is what the desktop launcher does) should not re-interrogate the
    # user about something already decided; that is the whole point of installed-role.
    if [[ -r "$CONFIG_DIR/installed-role" ]]; then
        local recorded
        recorded="$(<"$CONFIG_DIR/installed-role")"
        recorded="${recorded//[$'\t\r\n ']/}"
        case "$recorded" in
            steam-machine|steam-deck)
                ROLE="$recorded"
                echo "using the previously installed role: $ROLE"
                return
                ;;
        esac
    fi
    local detected
    detected="$(detect_role)"
    if [[ -n "$detected" ]]; then
        local label="Steam Machine (host)"
        [[ "$detected" == "steam-deck" ]] && label="Steam Deck (client)"
        # EOF (non-interactive stdin) must fall through to the menu, not abort silently.
        read -r -p "Detected $label. Install that? [Y/n] " answer || answer=""
        if [[ -z "$answer" || "$answer" =~ ^[Yy] ]]; then
            ROLE="$detected"
            return
        fi
    fi
    echo "Install SDSS on:"
    select choice in "Steam Machine (host)" "Steam Deck (client)"; do
        case "$REPLY" in
            1) ROLE="steam-machine"; return ;;
            2) ROLE="steam-deck"; return ;;
            *) echo "Choose 1 or 2." >&2 ;;
        esac
    done
}

require_steamos() {
    if ! grep -qi 'steam' /etc/os-release 2>/dev/null; then
        echo "SDSS installer supports SteamOS only." >&2
        exit 1
    fi
}

install_release() {
    # Re-running the installed copy (the .desktop launcher does exactly that) must not
    # delete the tree it is executing from. Nothing to copy, but the caller still wants
    # the endpoint setup below to run, so this is a success, not a skip.
    if [[ "$HERE" -ef "$INSTALL_ROOT" ]]; then
        echo "already running from $INSTALL_ROOT; keeping it in place"
        return
    fi

    mkdir -p "$(dirname "$INSTALL_ROOT")"
    staging="$(mktemp -d "$(dirname "$INSTALL_ROOT")/.release.XXXXXX")"
    # A container made by mktemp rather than a $$-derived name: PIDs are reused, so a
    # stale .previous.<pid> from an interrupted earlier run could collide and be treated
    # as this run's rollback copy. The release is moved *inside* the container so the
    # `mv` target does not already exist.
    previous_dir="$(mktemp -d "$(dirname "$INSTALL_ROOT")/.previous.XXXXXX")"
    previous="$previous_dir/release"
    # Any failure below (including a signal mid-swap) must not leave a half-copied tree
    # lying around, and must never delete the only remaining copy of the release: if
    # INSTALL_ROOT is missing when we exit, "previous" (if present) is the last good
    # release and must be restored, not removed, before cleanup.
    cleanup_on_exit() {
        if [[ ! -e "$INSTALL_ROOT" && -e "$previous" ]]; then
            mv "$previous" "$INSTALL_ROOT"
            echo "install interrupted; restored the previous release" >&2
        fi
        rm -rf "$previous_dir"
        rm -rf "$staging"
    }
    trap cleanup_on_exit EXIT

    cp -R "$HERE"/. "$staging"
    rm -rf "$staging/.git" "$staging/plugin/node_modules"
    # Nested __pycache__ directories are stale bytecode for a different interpreter path;
    # the old `rm -rf "$staging/__pycache__"` only ever removed the top-level one.
    find "$staging" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
    find "$staging" -name '*.pyc' -delete 2>/dev/null || true

    local script
    for script in install.sh deck/install.sh deck/sdss-connect.sh runtime/build.sh \
        packaging/install-host.sh packaging/install-udev-rule.sh plugin/install.sh \
        deck/add-steam-shortcut.py deck/install-controller-template.py; do
        [[ -e "$staging/$script" ]] && chmod +x "$staging/$script"
        true  # a missing optional script must not trip `set -e` on the last iteration
    done

    # Swap the new tree in without ever having no installed tree: move the old one aside
    # first and put it back if the swap fails, so a crash here cannot brick the install.
    # The EXIT trap covers the same "put previous back" case if we're killed by a signal
    # between the two `mv`s below, since $INSTALL_ROOT would be missing at that point.
    if [[ -e "$INSTALL_ROOT" ]]; then
        mv "$INSTALL_ROOT" "$previous"
    fi
    if ! mv "$staging" "$INSTALL_ROOT"; then
        if [[ -e "$previous" ]]; then
            mv "$previous" "$INSTALL_ROOT"
            echo "install failed; restored the previous release" >&2
        fi
        exit 1
    fi
    staging=""

    # The swap succeeded: disarm the trap and remove $previous_dir right here rather than
    # leaving it to `cleanup_on_exit` on function return. Traps registered with `trap ...
    # EXIT` are global, not scoped to this function, so it would otherwise still be armed
    # for the rest of the script -- including the final `exec` into the per-role setup
    # script below, and `exec` replaces the process image without running EXIT traps.
    # Relying on the trap there silently leaked a full copy of the previous release on
    # every successful install/update.
    trap - EXIT
    rm -rf "$previous_dir"
    previous_dir=""
    previous=""
    echo "installed $INSTALL_ROOT"
}

install_desktop_launcher() {
    local applications="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
    mkdir -p "$applications"
    install -m 0644 "$INSTALL_ROOT/packaging/sdss-installer.desktop" \
        "$applications/sdss-installer.desktop"
}

choose_role
case "$ROLE" in
    steam-machine|steam-deck) ;;
    *) echo "--role must be steam-machine or steam-deck" >&2; exit 2 ;;
esac

require_steamos
install_release
install_desktop_launcher
mkdir -p "$CONFIG_DIR"
printf '%s\n' "$ROLE" > "$CONFIG_DIR/installed-role"

if ((STAGE_ONLY)); then
    echo "staged only; skipping $ROLE setup"
    exit 0
fi

case "$ROLE" in
    steam-machine)
        exec "$INSTALL_ROOT/packaging/install-host.sh" "$INSTALL_ROOT"
        ;;
    steam-deck)
        if [[ -z "$HOST" ]]; then
            # EOF leaves HOST empty so the check below prints the real error.
            read -r -p "Steam Machine IP address or hostname: " HOST || HOST=""
        fi
        [[ -n "$HOST" ]] || { echo "a Steam Machine address is required" >&2; exit 2; }
        exec "$INSTALL_ROOT/deck/install.sh" --host "$HOST"
        ;;
esac