#!/usr/bin/env python3
"""Add the SDSS second screen as a non-Steam shortcut on a Steam Deck.

Steam stores non-Steam games in a binary VDF (`shortcuts.vdf`) and rewrites it when it
exits, so Steam must not be running while this edits the file — `steam_is_running()`
enforces that rather than trusting the user to remember.

Because this rewrites the *whole* file, every entry it did not create still has to survive
byte-identically. String values are therefore carried as `str` decoded with
`surrogateescape`, which round-trips arbitrary non-UTF-8 bytes (older Steam versions and
manually-added shortcuts routinely contain latin-1 names); a plain `errors="replace"`
silently rewrote those names as `EF BF BD` and broke the user's other launchers.
"""

from __future__ import annotations

import argparse
import shutil
import struct
import sys
import time
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vdf
from atomic import write_atomically

MAP, STRING, INT32 = 0x00, 0x01, 0x02
END = 0x08

APP_NAME = "Second Screen"
TAG = "SDSS"
# Backups kept per user config directory; older ones are pruned so they do not pile up in
# a directory Steam Cloud syncs.
KEEP_BACKUPS = 3


class ShortcutsError(Exception):
    """A shortcuts.vdf we refuse to touch, reported without a traceback."""


def _decode(raw: bytes) -> str:
    return raw.decode("utf-8", "surrogateescape")


def _encode(text: str) -> bytes:
    return text.encode("utf-8", "surrogateescape")


def _read_cstring(data: bytes, offset: int) -> tuple[str, int]:
    end = data.find(b"\x00", offset)
    if end < 0:
        raise ShortcutsError("unterminated string")
    return _decode(data[offset:end]), end + 1


def _byte_at(data: bytes, offset: int) -> int:
    if offset >= len(data):
        raise ShortcutsError("file ends mid-entry")
    return data[offset]


def parse(data: bytes) -> list[dict]:
    """Return the shortcut entries as plain dicts; unknown field types abort the parse."""
    if not data:
        return []
    offset = 0
    kind = _byte_at(data, offset)
    if kind != MAP:
        raise ShortcutsError("not a binary VDF map")
    _, offset = _read_cstring(data, offset + 1)

    entries: list[dict] = []
    # `_byte_at` rather than `offset < len(data)`: a file that simply runs out of bytes is
    # truncated, not finished. Ending the loop on EOF would silently read a file cut short
    # after the header as "no shortcuts", and the caller would then write that back —
    # deleting every shortcut the user had.
    while _byte_at(data, offset) != END:
        if _byte_at(data, offset) != MAP:
            raise ShortcutsError(f"unexpected node type {data[offset]:#x}")
        _, offset = _read_cstring(data, offset + 1)
        entry: dict = {}
        while _byte_at(data, offset) != END:
            kind = _byte_at(data, offset)
            key, offset = _read_cstring(data, offset + 1)
            if kind == STRING:
                value, offset = _read_cstring(data, offset)
            elif kind == INT32:
                if offset + 4 > len(data):
                    raise ShortcutsError("file ends mid-integer")
                value = struct.unpack("<i", data[offset : offset + 4])[0]
                offset += 4
            elif kind == MAP:
                nested: dict = {}
                while _byte_at(data, offset) != END:
                    sub_kind = _byte_at(data, offset)
                    sub_key, offset = _read_cstring(data, offset + 1)
                    if sub_kind != STRING:
                        raise ShortcutsError("unexpected nested type")
                    nested[sub_key], offset = _read_cstring(data, offset)
                offset += 1
                value = nested
            else:
                raise ShortcutsError(f"unexpected field type {kind:#x} for {key!r}")
            entry[key] = value
        offset += 1
        entries.append(entry)
    # Both terminators must be present: one closing the "shortcuts" map, one closing the
    # root. Without this a file truncated inside the trailing bytes parses clean.
    if _byte_at(data, offset + 1) != END:
        raise ShortcutsError("file ends mid-entry")
    return entries


def serialize(entries: list[dict]) -> bytes:
    out = bytearray([MAP])
    out += b"shortcuts\x00"
    for index, entry in enumerate(entries):
        out += bytes([MAP]) + str(index).encode() + b"\x00"
        for key, value in entry.items():
            if isinstance(value, dict):
                out += bytes([MAP]) + _encode(key) + b"\x00"
                for sub_key, sub_value in value.items():
                    out += bytes([STRING]) + _encode(sub_key) + b"\x00"
                    out += _encode(sub_value) + b"\x00"
                out += bytes([END])
            elif isinstance(value, bool):
                # Checked before int: bool is an int subclass, so this branch must win.
                out += bytes([INT32]) + _encode(key) + b"\x00" + struct.pack("<i", int(value))
            elif isinstance(value, int):
                out += bytes([INT32]) + _encode(key) + b"\x00" + struct.pack("<i", value)
            elif isinstance(value, str):
                out += bytes([STRING]) + _encode(key) + b"\x00" + _encode(value) + b"\x00"
            else:
                raise ShortcutsError(
                    f"cannot serialize {type(value).__name__} for key {key!r}"
                )
        out += bytes([END])
    out += bytes([END, END])
    return bytes(out)


def quoted(path: str) -> str:
    """Steam stores Exe/StartDir wrapped in literal double quotes."""
    return f'"{path}"'


def shortcut_appid(exe_field: str, name: str) -> int:
    """Steam's own id derivation: crc32 over the Exe field *as stored* plus AppName.

    The Exe field keeps its literal quotes, so the same quoted string must be hashed —
    hashing the bare path yields an id Steam will never look up.
    """
    return zlib.crc32(_encode(f"{exe_field}{name}")) | 0x80000000


def steam_is_running() -> bool:
    """True when a Steam client process is live.

    Steam holds shortcuts.vdf in memory and rewrites it on exit, so editing the file
    underneath it silently loses the shortcut. Stdlib-only /proc scan; on a system without
    /proc (development machines) this reports False and the caller proceeds.
    """
    proc = Path("/proc")
    if not proc.is_dir():
        return False
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            name = (entry / "comm").read_text().strip()
        except OSError:
            continue
        if name in ("steam", "steamwebhelper"):
            return True
    return False


def prune_backups(config: Path, keep: int = KEEP_BACKUPS) -> None:
    """Keep only the newest `keep` backups — this directory is synced by Steam Cloud."""
    backups = sorted(
        config.glob("shortcuts.vdf.sdss-*.bak"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for stale in backups[keep:]:
        stale.unlink(missing_ok=True)


def owned_by_sdss(entry: dict, quoted_exe: str) -> bool:
    """Is this entry one we previously wrote, and therefore safe to replace?

    Deduping on AppName alone would delete an unrelated user shortcut that happens to be
    called "Second Screen" — a name a user could plausibly pick for their own Moonlight
    launcher. Ownership is proven by our tag, or by the entry pointing at the exact
    executable we are about to install (which is how an entry written before the tag
    existed, or with `--name`, is still recognised).
    """
    tags = entry.get("tags")
    if isinstance(tags, dict) and TAG in tags.values():
        return True
    return entry.get("Exe") == quoted_exe


def rungameid(appid: int) -> int:
    return (appid << 32) | 0x02000000


def build_entry(exe: str, name: str, launch_options: str, start_dir: str) -> dict:
    appid = shortcut_appid(quoted(exe), name)
    return {
        "appid": struct.unpack("<i", struct.pack("<I", appid))[0],
        "AppName": name,
        "Exe": quoted(exe),
        "StartDir": quoted(start_dir),
        "icon": "",
        "ShortcutPath": "",
        "LaunchOptions": launch_options,
        "IsHidden": 0,
        "AllowDesktopConfig": 1,
        "AllowOverlay": 1,
        "OpenVR": 0,
        "Devkit": 0,
        "DevkitGameID": "",
        "DevkitOverrideAppID": 0,
        "LastPlayTime": 0,
        "FlatpakID": "",
        "tags": {"0": "SDSS"},
    }


_STEAM_ACCOUNT_ID_BASE = 76561197960265728  # SteamID64 of account-id 0


def _account_id(steam_id64: str) -> str:
    """Convert a loginusers.vdf SteamID64 key to the 32-bit account id used as the
    `userdata/<N>` folder name."""
    try:
        return str(int(steam_id64) - _STEAM_ACCOUNT_ID_BASE)
    except ValueError:
        return steam_id64


def user_config_dirs(steam_root: Path) -> list[Path]:
    userdata = steam_root / "userdata"
    if not userdata.is_dir():
        return []
    configs = {
        d.name: d / "config"
        for d in userdata.iterdir()
        if d.is_dir() and d.name.isdigit() and (d / "config").is_dir()
    }
    if not configs:
        return []

    most_recent: set[str] = set()
    loginusers = steam_root / "config" / "loginusers.vdf"
    if loginusers.is_file():
        try:
            root = vdf.loads(loginusers.read_text(encoding="utf-8"))
            users = vdf.get(root, "users")
            if isinstance(users, list):
                most_recent = set()
                for steam_id, value in users:
                    if not isinstance(value, list):
                        continue
                    if vdf.get(value, "MostRecent") not in ("1", "true"):
                        continue
                    # `steam_id` is the 64-bit SteamID (loginusers.vdf's top-level
                    # keys), but `configs`/`userdata/<N>` folder names are the 32-bit
                    # account ID (SteamID64 minus the account-number base). Comparing
                    # the two directly never matches, silently disabling this sort key.
                    account_id = _account_id(steam_id)
                    if account_id in configs:
                        most_recent.add(account_id)
        except Exception:
            # Best-effort user detection, not required for correctness: any parse
            # failure (malformed VDF, unexpected shape from a future Steam version,
            # etc.) should degrade to the mtime-sort fallback below, not crash.
            pass

    return sorted(
        configs.values(),
        key=lambda path: (
            path.parent.name not in most_recent,
            -path.stat().st_mtime,
        ),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "host",
        nargs="?",
        help="Steam Machine address the Deck should connect to (not used with --remove)",
    )
    parser.add_argument("--name", default=APP_NAME)
    parser.add_argument(
        "--exe",
        default=str(Path.home() / ".local/bin/sdss-connect"),
        help="launcher the shortcut runs",
    )
    parser.add_argument("--steam-root", default=str(Path.home() / ".steam/steam"))
    parser.add_argument("--all-users", action="store_true", help="add for every Steam user")
    parser.add_argument(
        "--remove",
        action="store_true",
        help="delete the SDSS shortcut instead of adding it",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="edit shortcuts.vdf even while Steam is running (it will be overwritten)",
    )
    args = parser.parse_args(argv)

    if args.remove:
        if args.host is not None:
            parser.error("--remove does not take a host")
    else:
        if args.host is None:
            parser.error("a host is required unless --remove is given")
        # Only meaningful when installing: on removal the launcher is expected to be gone
        # already, and refusing then would strand the shortcut permanently.
        if not Path(args.exe).exists():
            print(
                f"launcher not found: {args.exe} (run deck/install.sh first)",
                file=sys.stderr,
            )
            return 1

    if steam_is_running() and not args.force:
        print(
            "Steam is running; it rewrites shortcuts.vdf on exit and would discard this "
            "shortcut.\nClose Steam and re-run, or pass --force to edit anyway.",
            file=sys.stderr,
        )
        return 1

    configs = user_config_dirs(Path(args.steam_root))
    if not configs:
        print("no Steam user config directories found", file=sys.stderr)
        return 1
    if not args.all_users:
        # Removal sweeps every user: the shortcut may have been added under a different
        # account than the one that happens to sort first today.
        if not args.remove:
            configs = configs[:1]

    entry = (
        None
        if args.remove
        else build_entry(args.exe, args.name, args.host, str(Path(args.exe).parent))
    )
    for config in configs:
        path = config / "shortcuts.vdf"
        data = path.read_bytes() if path.is_file() else b""
        try:
            entries = parse(data) if data else []
        except ShortcutsError as error:
            print(f"{path}: {error}", file=sys.stderr)
            return 1
        if data and serialize(entries) != data:
            # A mismatch means round-tripping would rewrite bytes we do not understand, so
            # every other shortcut in the file is at risk. Refuse rather than corrupt it.
            print(
                f"{path}: cannot round-trip this file byte-for-byte; refusing to rewrite "
                "it. Please report this with a copy of the file.",
                file=sys.stderr,
            )
            return 1

        quoted_exe = quoted(args.exe)
        remaining = [e for e in entries if not owned_by_sdss(e, quoted_exe)]
        if args.remove and len(remaining) == len(entries):
            continue  # nothing of ours here; leave the file (and its mtime) untouched

        if path.is_file():
            backup = path.with_suffix(f".vdf.sdss-{int(time.time())}.bak")
            shutil.copyfile(path, backup)
            prune_backups(config)

        if entry is not None:
            remaining.append(entry)
        write_atomically(path, serialize(remaining))
        verb = "removed from" if args.remove else "wrote"
        print(f"{verb} {path} ({len(remaining)} shortcut(s))")

    if args.remove:
        return 0

    appid = shortcut_appid(quoted(args.exe), args.name)
    print(f"appid: {appid}")
    print(f"launch with: steam steam://rungameid/{rungameid(appid)}")
    print(
        "\nSteam Input template for touch:\n"
        "  1) Controller Settings -> Edit Layout\n"
        "  2) Action Sets (bottom)\n"
        "  3) Cog next to Default -> Add Always-On Command\n"
        "  4) Add Command -> System -> Touchscreen Native Support\n"
        "  5) Back out to save"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
