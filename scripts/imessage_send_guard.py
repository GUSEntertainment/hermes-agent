#!/usr/bin/env python3
"""Source-checkout launcher for the installed ``hermes-imsg`` guard."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hermes_cli.imessage_send_guard import main


if __name__ == "__main__":
    raise SystemExit(main())
