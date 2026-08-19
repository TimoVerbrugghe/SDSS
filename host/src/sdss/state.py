"""Persistent toggle state, written by the Decky plugin and read by the launch wrapper."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field

from . import paths


@dataclass
class State:
    enabled: bool = False
    profiles: dict[str, bool] = field(default_factory=dict)

    def enabled_for(self, profile_id: str) -> bool:
        return self.enabled and self.profiles.get(profile_id, True)


def load() -> State:
    path = paths.state_file()
    if not path.is_file():
        return State()
    try:
        raw = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return State()
    return State(
        enabled=bool(raw.get("enabled", False)),
        profiles={str(k): bool(v) for k, v in (raw.get("profiles") or {}).items()},
    )


def save(state: State) -> None:
    path = paths.state_file()
    paths.ensure(path.parent)
    tmp = path.with_name(f".{path.name}.sdss-tmp")
    try:
        tmp.write_text(json.dumps(asdict(state), indent=2) + "\n")
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
