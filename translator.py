#!/usr/bin/env python3
"""Compatibility entry point: Gemini engine.

Kept so `python translator.py` keeps working. Everything now lives in
`subtrans.py` — use it for options, checks and the full CLI.
"""

import sys

from subtrans import main

if __name__ == "__main__":
    sys.exit(main(["run", "--engine", "gemini", *sys.argv[1:]]))
