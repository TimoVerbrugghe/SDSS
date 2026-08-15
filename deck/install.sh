#!/usr/bin/env bash
# Install the SDSS client bits on a Steam Deck. Touches only $HOME.
set -euo pipefail

BIN_DIR="$HOME/.local/bin"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "installing Moonlight ..."
flatpak install -y --user flathub com.moonlight_stream.Moonlight

mkdir -p "$BIN_DIR"
install -m 0755 "$HERE/sdss-connect.sh" "$BIN_DIR/sdss-connect"
echo "installed $BIN_DIR/sdss-connect"

case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) echo "note: $BIN_DIR is not on PATH" ;;
esac

cat <<EOF

Next:
  1. Start a game on the Steam Machine with second screen mode enabled.
  2. Pair once:   sdss-connect <steam-machine-ip> --pair 1234
  3. Connect:     sdss-connect <steam-machine-ip>

To launch it from Game Mode, add $BIN_DIR/sdss-connect as a non-Steam shortcut.
EOF
