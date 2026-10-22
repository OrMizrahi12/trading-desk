"""Makes the trading lab's modules importable (`live`, `indicators`, `strategies`, ...). Read-only use.

The lab is a folder of plain modules with no build system, so it cannot be installed as a package; its folder is
put on `sys.path` instead. Set TRADING_LAB to use another checkout. Importing `live.config` makes the lab itself
refuse any non-paper port or non-local host, so a live account cannot be reached through it.
"""

import os
import sys
from pathlib import Path

LAB = Path(os.environ.get("TRADING_LAB", "~/trading-lab")).expanduser()
if not (LAB / "live").is_dir():
    raise ImportError(f"trading lab not found at {LAB} (set TRADING_LAB)")
if str(LAB) not in sys.path:
    sys.path.insert(0, str(LAB))
