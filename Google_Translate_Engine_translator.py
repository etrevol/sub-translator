#!/usr/bin/env python3
"""Compatibility entry point: free Google Translate engine.

Kept so `python Google_Translate_Engine_translator.py` keeps working.
Everything now lives in `subtrans.py` — use it for options, checks and the
full CLI.
"""

import sys

from subtrans import main

if __name__ == "__main__":
    sys.exit(main(["run", "--engine", "google", *sys.argv[1:]]))
