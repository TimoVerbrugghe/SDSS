#!/usr/bin/env bash
# Install the SDSS Decky plugin into ~/homebrew/plugins. Nothing outside $HOME is touched.
set -euo pipefail

ROOT="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
SRC="$ROOT/plugin"
PLUGIN_NAME="SDSS"
PLUGINS_DIR="$HOME/homebrew/plugins"
DST="$PLUGINS_DIR/$PLUGIN_NAME"

if [[ ! -f "$SRC/plugin.json" || ! -f "$SRC/main.py" ]]; then
    echo "not an SDSS plugin source directory: $SRC" >&2
    exit 1
fi

if [[ ! -d "$PLUGINS_DIR" ]]; then
    cat >&2 <<'MSG'
Decky Loader is not installed (~/homebrew/plugins is missing).

Install it from https://decky.xyz and re-run:
    ~/.local/share/sdss/release/plugin/install.sh

Second screen mode still works from the terminal with `sdss enable` / `sdss disable`.
MSG
    exit 0
fi

build_frontend() {
    # Decky plugins ship a prebuilt dist/index.js. Building needs node, which SteamOS
    # does not provide, so a prebuilt bundle in the checkout always wins -- unless the
    # caller explicitly asks for a rebuild, which is the only way to pick up local
    # changes to plugin/src on a machine that does have node.
    if [[ -f "$SRC/dist/index.js" && "${SDSS_REBUILD_PLUGIN:-0}" != "1" ]]; then
        return 0
    fi
    if ! command -v npm >/dev/null; then
        if [[ -f "$SRC/dist/index.js" ]]; then
            echo "SDSS_REBUILD_PLUGIN=1 set but npm is unavailable; using the prebuilt bundle" >&2
            return 0
        fi
        frontend_build_status="npm-missing"
        return 1
    fi
    echo "building the plugin frontend ..."
    if ! (cd "$SRC" && npm install --no-audit --no-fund && npm run build); then
        frontend_build_status="build-failed"
        return 1
    fi
    return 0
}

frontend_build_status=""
if ! build_frontend; then
    if [[ "$frontend_build_status" == "npm-missing" ]]; then
        cat >&2 <<'MSG'
No prebuilt plugin/dist/index.js and npm is unavailable, so the Decky UI cannot be
built here. Build it once on any machine with node installed:

    cd plugin && npm install && npm run build

then re-run this installer. Skipping the plugin for now; `sdss enable` and
`sdss disable` remain available from a terminal.
MSG
        exit 0
    fi
    echo "plugin frontend build failed; see npm output above" >&2
    exit 1
fi

# Decky Loader owns ~/homebrew as root, so writing a plugin needs elevation.
SUDO=()
if [[ ! -w "$PLUGINS_DIR" ]]; then
    if ! command -v sudo >/dev/null; then
        echo "cannot write $PLUGINS_DIR and sudo is unavailable" >&2
        exit 1
    fi
    echo "Decky's plugin directory is root-owned; sudo is needed to install there."
    SUDO=(sudo)
fi

"${SUDO[@]}" rm -rf "$DST"
"${SUDO[@]}" mkdir -p "$DST/dist"
"${SUDO[@]}" install -m 0644 "$SRC/plugin.json" "$SRC/package.json" "$SRC/main.py" "$DST/"
"${SUDO[@]}" install -m 0644 "$SRC/dist/index.js" "$DST/dist/index.js"
# Deliberately left root-owned: Decky Loader executes main.py as root, so anything the
# `deck` user can rewrite here is a root escalation. `install -m 0644` above is enough.

echo "installed $DST"
echo "restart Decky Loader (or reboot) to load the SDSS plugin"
