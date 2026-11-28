"""Watch a GUI progress session without owning or cancelling its render work."""

from pathlib import Path
import sys
from time import sleep, time


def main():
    heartbeat, result = map(Path, sys.argv[1:])
    while not result.is_file():
        if not heartbeat.is_file() or time() - heartbeat.stat().st_mtime > 15:
            return 1
        sleep(0.3)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
