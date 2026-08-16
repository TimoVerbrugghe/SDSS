"""Checking for, downloading and applying a newer SDSS AppImage.

Being offline is a normal state here, not an error: the dashboard has to stay usable on a
device that has never had the internet, so every failure in this module is reported as
"could not check" rather than raised at the user as a dialog.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

REPOSITORY = os.environ.get("SDSS_UPDATE_REPO", "TimoVerbrugghe/SDSS")
API_ROOT = "https://api.github.com"
#: Only these hosts are ever fetched from. A release payload is attacker-controlled data
#: as far as this process is concerned, so the URLs inside it are never followed blindly.
ALLOWED_HOSTS = ("api.github.com", "github.com", "objects.githubusercontent.com")
USER_AGENT = "sdss-app"
TIMEOUT = 15
#: Refuse absurd downloads rather than filling the Deck's disk from a bad URL.
MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024

_SHA256_RE = re.compile(r"\b[0-9a-fA-F]{64}\b")
_VERSION_PART = re.compile(r"\d+|[a-zA-Z]+")


class UpdateError(Exception):
    """Anything that stopped an update check or download. Always shown inline."""


@dataclass(frozen=True)
class ReleaseInfo:
    version: str
    notes: str
    url: str
    checksum_url: str | None = None

    @property
    def filename(self) -> str:
        return Path(urlparse(self.url).path).name


def _check_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
        raise UpdateError(f"refusing to fetch {url}")
    return url


def _fetch(url: str, opener: Callable | None = None) -> bytes:
    request = urllib.request.Request(  # noqa: S310 - scheme and host checked above
        _check_url(url), headers={"User-Agent": USER_AGENT, "Accept": "*/*"}
    )
    open_url = opener or urllib.request.urlopen
    try:
        with open_url(request, timeout=TIMEOUT) as response:
            return response.read(MAX_DOWNLOAD_BYTES + 1)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise UpdateError(f"could not reach {urlparse(url).hostname}: {exc}") from exc


def normalise(version: str) -> tuple:
    """A comparable form of a version string, tolerant of a leading `v`."""
    text = version.strip().lstrip("vV")
    return tuple(
        int(part) if part.isdigit() else part.lower() for part in _VERSION_PART.findall(text)
    )


def is_newer(candidate: str, current: str) -> bool:
    """Whether `candidate` should be offered as an update over `current`.

    An unknown current version (no VERSION file, an old install) counts as older, because
    offering an update the user can decline is better than hiding one they need.
    """
    if current in ("", "unknown"):
        return True
    left, right = normalise(candidate), normalise(current)
    try:
        return left > right
    except TypeError:
        # Mixed numeric/alpha parts at the same position are not orderable; treat any
        # difference as newer rather than crashing the dashboard.
        return left != right


def latest_release(repository: str = REPOSITORY, opener: Callable | None = None) -> ReleaseInfo:
    payload = _fetch(f"{API_ROOT}/repos/{repository}/releases/latest", opener)
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise UpdateError("the release feed was not valid JSON") from exc
    if not isinstance(data, dict):
        raise UpdateError("the release feed was not valid JSON")
    tag = str(data.get("tag_name") or "").strip()
    if not tag:
        raise UpdateError("the latest release has no tag")
    assets = data.get("assets")
    assets = assets if isinstance(assets, list) else []
    appimage = None
    checksum = None
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name") or "")
        url = str(asset.get("browser_download_url") or "")
        if not url:
            continue
        if name.endswith(".AppImage") and appimage is None:
            appimage = url
        elif (name.endswith(".sha256") or name == "SHA256SUMS") and checksum is None:
            checksum = url
    if appimage is None:
        raise UpdateError(f"release {tag} has no AppImage asset")
    return ReleaseInfo(
        version=tag,
        notes=str(data.get("body") or ""),
        url=_check_url(appimage),
        checksum_url=_check_url(checksum) if checksum else None,
    )


def parse_checksum(text: str, filename: str) -> str | None:
    """Pull the digest for `filename` out of a `sha256sum`-style file.

    A release may publish one digest per line for several assets, so the line naming this
    file wins; a single-digest file with no filename is accepted as-is.
    """
    fallback = None
    for line in text.splitlines():
        match = _SHA256_RE.search(line)
        if not match:
            continue
        digest = match.group(0).lower()
        if filename and filename in line:
            return digest
        if fallback is None:
            fallback = digest
    return fallback


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(release: ReleaseInfo, destination: Path, opener: Callable | None = None) -> Path:
    """Fetch the AppImage and verify its published checksum before it is ever executed.

    An unverifiable download is refused rather than installed: this file replaces the one
    the user launches, so accepting it on trust would be the worst kind of shortcut.
    """
    if release.checksum_url is None:
        raise UpdateError(
            f"release {release.version} publishes no checksum; refusing to install it"
        )
    expected = parse_checksum(_fetch(release.checksum_url, opener).decode("utf-8", "replace"),
                              release.filename)
    if not expected:
        raise UpdateError(f"release {release.version} has an unreadable checksum file")

    data = _fetch(release.url, opener)
    if len(data) > MAX_DOWNLOAD_BYTES:
        raise UpdateError("the download is implausibly large; refusing it")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)
    actual = sha256(destination)
    if actual != expected:
        destination.unlink(missing_ok=True)
        raise UpdateError("the download does not match its published checksum")
    destination.chmod(destination.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return destination


def apply(new_file: Path, target: Path) -> Path:
    """Replace `target` with `new_file`, atomically and on the same filesystem.

    The target is usually the AppImage this very process is running from. Replacing an
    open executable by rename is safe on Linux — the running process keeps the old inode —
    whereas writing over it in place is not.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    staged = target.with_name(f".{target.name}.new")
    try:
        shutil.copyfile(new_file, staged)
        staged.chmod(staged.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        os.replace(staged, target)
    except OSError as exc:
        staged.unlink(missing_ok=True)
        raise UpdateError(f"could not install the update: {exc}") from exc
    return target


def install_local(path: Path, target: Path) -> Path:
    """Update from a file the user already has, for developers and offline installs."""
    if not path.is_file():
        raise UpdateError(f"{path} is not a file")
    return apply(path, target)


def staging_path() -> Path:
    return Path(tempfile.gettempdir()) / "sdss-update" / "SDSS.AppImage"
