#!/usr/bin/env bash
# Build the SDSS compositor image on the Steam Machine. Nothing is written outside $HOME.
set -euo pipefail

IMAGE="${SDSS_COMPOSITOR_IMAGE:-localhost/sdss-compositor:latest}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v podman >/dev/null; then
    echo "podman is required but not installed" >&2
    exit 1
fi

echo "building ${IMAGE} ..."
podman build --tag "${IMAGE}" --file "${HERE}/Containerfile" "${HERE}"
podman run --rm --entrypoint /usr/bin/sway "${IMAGE}" --version
echo "done: ${IMAGE}"
