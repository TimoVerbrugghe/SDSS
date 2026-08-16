#!/usr/bin/env bash
# Build the SDSS AppImage: the installer and management app in one file.
#
#   packaging/appimage/build.sh [output.AppImage]
#
# Deliberately never run on the target. SteamOS has a read-only rootfs and no build
# tooling, so the AppImage is produced on a normal Linux machine (or CI) and carries its
# own Python and Qt — it must depend on nothing outside the SteamOS rootfs.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
OUT="${1:-$ROOT/dist/SDSS-x86_64.AppImage}"
WORK="${SDSS_APPIMAGE_WORK:-$ROOT/dist/appimage-build}"
APPDIR="$WORK/SDSS.AppDir"

# Pinned rather than "latest" so a rebuild of an old tag produces the same thing. Both are
# overridable, which is how a build behind a proxy or an air-gapped rebuild is done.
PYTHON_VERSION="${SDSS_PYTHON_VERSION:-3.11.11}"
PYTHON_RELEASE="${SDSS_PYTHON_RELEASE:-20250115}"
PYTHON_URL="${SDSS_PYTHON_URL:-https://github.com/astral-sh/python-build-standalone/releases/download/$PYTHON_RELEASE/cpython-$PYTHON_VERSION+$PYTHON_RELEASE-x86_64-unknown-linux-gnu-install_only.tar.gz}"
PYSIDE_VERSION="${SDSS_PYSIDE_VERSION:-6.8.1.1}"
APPIMAGETOOL_URL="${SDSS_APPIMAGETOOL_URL:-https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage}"

log() { printf '==> %s\n' "$*"; }

need() {
    command -v "$1" >/dev/null || { echo "$1 is required to build the AppImage" >&2; exit 1; }
}
need curl
need tar

fetch() {
    local url="$1" dest="$2"
    if [[ -f "$dest" ]]; then
        log "using cached $(basename "$dest")"
        return 0
    fi
    log "downloading $url"
    curl --fail --location --silent --show-error --output "$dest.part" "$url"
    mv "$dest.part" "$dest"
}

verify_sha256() {
    # The published digest, when there is one. Captured into a variable first rather than
    # piped into `sha256sum -c`: under `pipefail` a checker that exits on the first line
    # SIGPIPEs the producer and the whole script fails with 141 instead of a real result.
    local file="$1" expected="$2" actual
    actual="$(sha256sum "$file")"
    actual="${actual%% *}"
    if [[ "$actual" != "$expected" ]]; then
        echo "checksum mismatch for $file" >&2
        echo "  expected $expected" >&2
        echo "  actual   $actual" >&2
        exit 1
    fi
    log "verified $(basename "$file")"
}

mkdir -p "$WORK" "$(dirname "$OUT")"
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin" "$APPDIR/usr/share/sdss" "$APPDIR/usr/share/icons/hicolor/scalable/apps"

# --- Python runtime ---------------------------------------------------------
python_tarball="$WORK/python.tar.gz"
fetch "$PYTHON_URL" "$python_tarball"
if [[ -z "${SDSS_PYTHON_URL:-}" ]]; then
    # Only the pinned upstream URL is known to publish a digest next to the asset; a
    # custom URL is the builder's own problem.
    checksum_file="$WORK/python.tar.gz.sha256"
    if curl --fail --location --silent --show-error --output "$checksum_file" "$PYTHON_URL.sha256"; then
        expected="$(tr -d '[:space:]' < "$checksum_file")"
        verify_sha256 "$python_tarball" "$expected"
    else
        echo "warning: no published checksum for the Python runtime" >&2
    fi
fi

log "unpacking the Python runtime"
rm -rf "$WORK/python"
mkdir -p "$WORK/python"
tar -xzf "$python_tarball" -C "$WORK/python" --strip-components=1
cp -a "$WORK/python/." "$APPDIR/usr/"
PYTHON="$APPDIR/usr/bin/python3"
[[ -x "$PYTHON" ]] || { echo "the unpacked runtime has no usr/bin/python3" >&2; exit 1; }

# --- Qt ---------------------------------------------------------------------
# PySide6-Essentials, not the full PySide6: the extras (WebEngine, 3D, Charts) triple the
# size and none of them are imported by app/ui.
log "installing PySide6 $PYSIDE_VERSION"
"$PYTHON" -m pip install --quiet --no-cache-dir --upgrade pip
"$PYTHON" -m pip install --quiet --no-cache-dir "PySide6-Essentials==$PYSIDE_VERSION"

log "pruning unused Qt modules"
site="$("$PYTHON" -c 'import PySide6, os; print(os.path.dirname(PySide6.__file__))')"
for unused in Qt3D QtCharts QtDataVisualization QtQuick3D QtMultimediaWidgets QtPdf \
              QtSensors QtSerialPort QtSpatialAudio QtTextToSpeech QtWebSockets QtNfc; do
    find "$site" -maxdepth 1 -name "*${unused}*" -exec rm -rf {} + 2>/dev/null || true
done
find "$APPDIR/usr" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true

# --- SDSS payload -----------------------------------------------------------
# The whole tree, because installing *is* `install.sh --source <this tree>`: a newer
# AppImage has to be newer code, not just a newer front end.
log "copying the SDSS payload"
payload="$APPDIR/usr/share/sdss"
while IFS= read -r path; do
    mkdir -p "$payload/$(dirname "$path")"
    cp -a "$ROOT/$path" "$payload/$path"
done < <(cd "$ROOT" && git ls-files)
find "$payload" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
chmod +x "$payload/install.sh" "$payload/app/sdss-app"
find "$payload" -name '*.sh' -exec chmod +x {} +
# The payload is `git ls-files`, so anything not committed yet is simply absent — and an
# AppImage missing app/ui builds, packages and passes --version, then fails to open a
# window on the user's machine. Fail here instead.
for required in install.sh app/sdss-app app/cli.py app/core/probe.py app/ui/main.py \
                host/src/sdss/cli.py packaging/uninstall.sh packaging/sdss.desktop VERSION; do
    [[ -e "$payload/$required" ]] || {
        echo "payload is missing $required (untracked? run: git add $required)" >&2
        exit 1
    }
done
[[ -f "$payload/plugin/dist/index.js" ]] || {
    echo "plugin/dist/index.js is missing; SteamOS has no node and needs it prebuilt" >&2
    exit 1
}

# --- AppDir metadata --------------------------------------------------------
install -m 0755 "$HERE/AppRun" "$APPDIR/AppRun"
install -m 0644 "$HERE/sdss.desktop" "$APPDIR/sdss.desktop"
install -m 0644 "$ROOT/assets/logo.svg" "$APPDIR/sdss.svg"
install -m 0644 "$ROOT/assets/logo.svg" \
    "$APPDIR/usr/share/icons/hicolor/scalable/apps/sdss.svg"
cp "$APPDIR/sdss.svg" "$APPDIR/.DirIcon"

# --- Package ----------------------------------------------------------------
tool="$WORK/appimagetool"
fetch "$APPIMAGETOOL_URL" "$tool"
chmod +x "$tool"

version="$(tr -d '[:space:]' < "$ROOT/VERSION")"
log "building $OUT (version $version)"
# Extract-and-run: CI runners and containers routinely have no FUSE, and appimagetool is
# itself an AppImage. This affects only the build machine, not the produced file.
APPIMAGE_EXTRACT_AND_RUN=1 ARCH=x86_64 VERSION="$version" "$tool" "$APPDIR" "$OUT"

sha="$(sha256sum "$OUT")"
printf '%s\n' "$sha" > "$OUT.sha256"
log "built $OUT"
log "sha256 ${sha%% *}"
