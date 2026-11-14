#!/usr/bin/env python3
"""Emit one JSON event per cycle with storage-pool state (md / ZFS / btrfs)."""
import sys

from zfspool_common import discover, emit


def main():
    emit(discover())
    return 0


if __name__ == "__main__":
    sys.exit(main())
