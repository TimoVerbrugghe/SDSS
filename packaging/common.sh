#!/usr/bin/env bash
# Helpers shared by the endpoint installers. Sourced, never executed.

FLATHUB_URL="https://dl.flathub.org/repo/flathub.flatpakrepo"

# install_flatpak <app-id> <human name>
# Both endpoints need a Flathub app before anything else works, and a failure has to stop
# the install: continuing would leave a launcher (or Steam shortcut) that can never run.
install_flatpak() {
    local app_id="$1" label="$2"
    if ! command -v flatpak >/dev/null 2>&1; then
        echo "flatpak is unavailable; install $label ($app_id) manually and re-run" >&2
        return 1
    fi
    if ! flatpak remote-add --if-not-exists --user flathub "$FLATHUB_URL"; then
        echo "could not add the Flathub remote; install $label manually and re-run" >&2
        return 1
    fi
    if ! flatpak install --user -y flathub "$app_id"; then
        echo "could not install $label; install $app_id manually and re-run" >&2
        return 1
    fi
}

# note_path <dir> — remind the user when a directory we installed into is off PATH.
note_path() {
    case ":$PATH:" in
        *":$1:"*) ;;
        *) echo "Note: add $1 to PATH for terminal use." ;;
    esac
}
