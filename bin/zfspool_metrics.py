#!/usr/bin/env python3
"""Emit flat numeric metrics (one JSON event per cycle) for analytics."""
import sys

from zfspool_common import discover, emit, metrics_payload


def main():
    emit(metrics_payload(discover()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
