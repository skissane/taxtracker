#!/usr/bin/env python
"""Command-line entry point for lifetracker utility scripts."""

import sys
from pathlib import Path


def main():
    sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
    from lifetracker.cli import cli  # noqa: PLC0415

    cli()


if __name__ == "__main__":
    main()
