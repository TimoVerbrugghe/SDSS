#!/usr/bin/env python3
"""Drive the test compositor through the same virtual-pointer protocol as inputd."""

import sys
import time

sys.path.insert(0, "/usr/local/lib/sdss")

from sdss_inputd import VirtualPointer


def main() -> int:
    if len(sys.argv) != 3:
        raise SystemExit("usage: drive.py X Y")
    pointer = VirtualPointer("HEADLESS-1")
    try:
        pointer.motion(int(sys.argv[1]), 1280, int(sys.argv[2]), 800)
        pointer.button(True)
        pointer.button(False)
        time.sleep(0.1)
    finally:
        pointer.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
