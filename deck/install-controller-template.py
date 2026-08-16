#!/usr/bin/env python3
"""Install the SDSS Steam Input template on a Steam Deck.

Steam Input can swallow touchscreen events before Moonlight sees them. The fix is the
always-on `Touchscreen Native Support` command, which is the binding
`controller_action ts_n` in a controller template.

Rather than shipping a full template (which would go stale with every Steam controller
schema bump), this derives one from Steam's own `Gamepad with Joystick Trackpad`
template that is already installed on the device, and adds the single binding.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import vdf  # noqa: E402
from atomic import write_atomically  # noqa: E402

TEMPLATE_NAME = "sdss_second_screen.vdf"
BASE_TEMPLATE = "controller_neptune_gamepad_joystick.vdf"
TITLE = "SDSS - Second Screen"
DESCRIPTION = (
    "Gamepad with joystick trackpad plus always-on Touchscreen Native Support, "
    "so the Deck touchscreen reaches the streamed second screen 1:1."
)
NATIVE_TOUCH_BINDING = "controller_action ts_n, , "


def _switch_group(root: vdf.Pairs) -> vdf.Pairs:
    """The 'switches' group holds device-wide inputs, which is where always-on lives."""
    for key, value in root:
        if key == "group" and isinstance(value, list) and vdf.get(value, "mode") == "switches":
            return value
    raise SystemExit("base template has no 'switches' group; Steam layout changed")


def build(base_text: str) -> str:
    root = vdf.loads(base_text)
    mappings = vdf.get(root, "controller_mappings")
    if not isinstance(mappings, list):
        raise SystemExit("base template is not a controller_mappings document")

    vdf.set_value(mappings, "title", TITLE)
    vdf.set_value(mappings, "description", DESCRIPTION)
    vdf.set_value(mappings, "export_type", "template")
    vdf.set_value(mappings, "url", f"template://{TEMPLATE_NAME}")
    vdf.set_value(
        mappings,
        "localization",
        [("english", [("title", TITLE), ("description", DESCRIPTION)])],
    )

    group = _switch_group(mappings)
    inputs = vdf.get(group, "inputs")
    if not isinstance(inputs, list):
        inputs = []
        vdf.set_value(group, "inputs", inputs)
    inputs[:] = [(key, value) for key, value in inputs if key != "always_on_action"]
    inputs.append(
        (
            "always_on_action",
            [
                (
                    "activators",
                    [("Full_Press", [("bindings", [("binding", NATIVE_TOUCH_BINDING)])])],
                ),
                ("disabled_activators", []),
            ],
        )
    )
    return vdf.dumps(root) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--steam-root", default=str(Path.home() / ".steam/steam"))
    parser.add_argument("--print", action="store_true", help="write to stdout instead")
    args = parser.parse_args(argv)

    templates = Path(args.steam_root) / "controller_base/templates"
    base = templates / BASE_TEMPLATE
    if not base.is_file():
        print(f"base template not found: {base}", file=sys.stderr)
        print("start Steam once so it unpacks its controller templates.", file=sys.stderr)
        return 1

    text = build(base.read_text(encoding="utf-8"))
    if args.print:
        sys.stdout.write(text)
        return 0

    destination = templates / TEMPLATE_NAME
    write_atomically(destination, text, 0o644)
    print(f"installed {destination}")
    print(
        "\nApply it in Game Mode:\n"
        "  Second Screen -> Controller Settings -> current layout\n"
        f"  -> Templates -> {TITLE}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
