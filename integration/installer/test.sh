#!/usr/bin/env bash
set -euo pipefail

APPIMAGE="${1:?usage: test.sh /artifacts/SDSS-x86_64.AppImage}"
ARTIFACTS="${SDSS_TEST_ARTIFACTS:-/artifacts}"
ROOT="/work"
RUNTIME="/run/user/1000"
HOME="/home/deck"
SWAY_ENV="/tmp/sdss-installer-test.env"
export APPIMAGE_EXTRACT_AND_RUN=1
export HOME XDG_RUNTIME_DIR="$RUNTIME" SDSS_TEST_ENV="$SWAY_ENV"
export XDG_DATA_HOME="$HOME/.local/share"
export XDG_CONFIG_HOME="$HOME/.config"
export XDG_STATE_HOME="$HOME/.local/state"

mkdir -p "$ARTIFACTS" "$XDG_DATA_HOME" "$XDG_CONFIG_HOME" "$XDG_STATE_HOME"
rm -f "$SWAY_ENV"

WLR_BACKENDS=headless WLR_HEADLESS_OUTPUTS=2 sway -c /etc/sdss-installer-test-sway.conf \
    >"$ARTIFACTS/sway.log" 2>&1 &
SWAY_PID=$!
APP_PID=""

cleanup() {
    [[ -n "$APP_PID" ]] && kill "$APP_PID" 2>/dev/null || true
    kill "$SWAY_PID" 2>/dev/null || true
}
trap cleanup EXIT

for _ in $(seq 1 100); do
    [[ -s "$SWAY_ENV" ]] && break
    sleep 0.1
done
[[ -s "$SWAY_ENV" ]] || { echo "sway never reported its sockets" >&2; exit 1; }
WAYLAND_DISPLAY="$(sed -n 's/^WAYLAND_DISPLAY=//p' "$SWAY_ENV")"
SWAYSOCK="$(sed -n 's/^SWAYSOCK=//p' "$SWAY_ENV")"
export WAYLAND_DISPLAY SWAYSOCK

for _ in $(seq 1 100); do
    swaymsg -t get_outputs >/dev/null 2>&1 && break
    sleep 0.1
done
outputs="$(swaymsg -t get_outputs)"
python3 -c '
import json
import sys

names = {output["name"] for output in json.load(sys.stdin)}
assert {"HEADLESS-1", "HEADLESS-2"} <= names, names
' <<<"$outputs"

"$APPIMAGE" --version
"$APPIMAGE" --status >"$ARTIFACTS/status.json"
python3 -c 'import json, sys; json.load(open(sys.argv[1]))' "$ARTIFACTS/status.json"
"$APPIMAGE" --self-test

"$APPIMAGE" --stage-only >"$ARTIFACTS/app.log" 2>&1 &
APP_PID=$!

window_json="$ARTIFACTS/window.json"
for _ in $(seq 1 100); do
    swaymsg -t get_tree >"$window_json"
    if python3 - "$window_json" <<'PY'
import json
import sys

def walk(node):
    yield node
    for child in node.get("nodes", []) + node.get("floating_nodes", []):
        yield from walk(child)

for node in walk(json.load(open(sys.argv[1]))):
    if node.get("name") == "SDSS — Steam Deck Second Screen":
        rect = node["rect"]
        assert rect["width"] == 980 and rect["height"] == 720, rect
        print(f'{rect["x"]} {rect["y"]}')
        break
else:
    raise SystemExit(1)
PY
    then
        break
    fi
    sleep 0.1
done
geometry="$(python3 - "$window_json" <<'PY'
import json
import sys

def walk(node):
    yield node
    for child in node.get("nodes", []) + node.get("floating_nodes", []):
        yield from walk(child)

for node in walk(json.load(open(sys.argv[1]))):
    if node.get("name") == "SDSS — Steam Deck Second Screen":
        rect = node["rect"]
        print(f'{rect["x"]} {rect["y"]}')
        break
else:
    raise SystemExit("SDSS window did not appear")
PY
)"
grim -o HEADLESS-1 "$ARTIFACTS/installer-before.png"
read -r window_x window_y <<<"$geometry"
/opt/sdss-installer-test/drive.py "$((window_x + 490))" "$((window_y + 200))"

marker="$XDG_DATA_HOME/sdss/release/.sdss-release.json"
for _ in $(seq 1 100); do
    [[ -f "$marker" ]] && break
    sleep 0.1
done
[[ -f "$marker" ]]
test -f "$XDG_DATA_HOME/applications/sdss.desktop"
test -f "$XDG_DATA_HOME/sdss/release/host/src/sdss/cli.py"
python3 -c 'import json, sys; json.load(open(sys.argv[1]))' "$marker"
grim -o HEADLESS-1 "$ARTIFACTS/installer-after.png"

cp -a "$ROOT" /tmp/update
printf 'container-update\n' >/tmp/update/VERSION
"$ROOT/install.sh" --source /tmp/update --role steam-machine --stage-only
[[ "$(<"$XDG_DATA_HOME/sdss/release/VERSION")" == "container-update" ]]

cp -a "$ROOT" /tmp/failing-update
printf 'failed-update\n' >/tmp/failing-update/VERSION
mkdir /tmp/fail-mv
cat >/tmp/fail-mv/mv <<'EOF'
#!/usr/bin/env bash
if [[ "$1" == "$XDG_DATA_HOME"/sdss/.release.* && "$2" == "$XDG_DATA_HOME/sdss/release" ]]; then
    exit 1
fi
exec /usr/bin/mv "$@"
EOF
chmod +x /tmp/fail-mv/mv
if PATH="/tmp/fail-mv:$PATH" "$ROOT/install.sh" --source /tmp/failing-update --role steam-machine --stage-only; then
    echo "the simulated swap failure unexpectedly succeeded" >&2
    exit 1
fi
[[ "$(<"$XDG_DATA_HOME/sdss/release/VERSION")" == "container-update" ]]

mkdir /tmp/dmi /tmp/noninteractive
cat >/tmp/dmi/cat <<'EOF'
#!/usr/bin/env bash
if [[ "$1" == /sys/devices/virtual/dmi/id/product_name ]]; then
    printf 'Fremont\n'
else
    exec /usr/bin/cat "$@"
fi
EOF
chmod +x /tmp/dmi/cat
printf '' | PATH="/tmp/dmi:$PATH" "$ROOT/install.sh" --stage-only >"$ARTIFACTS/noninteractive.log"

PYTHONPATH="$ROOT" python3 - <<'PY'
from pathlib import Path
from app.core import selfinstall

assert not selfinstall.fuse_available()
assert selfinstall.exec_line(Path("/tmp/SDSS.AppImage"), fuse=False).startswith(
    "env APPIMAGE_EXTRACT_AND_RUN=1 "
)
PY

"$XDG_DATA_HOME/sdss/release/packaging/uninstall.sh" --yes
test ! -e "$XDG_DATA_HOME/sdss"
test ! -e "$XDG_DATA_HOME/applications/sdss.desktop"
