"""
Oil price backfill — thin shim, superseded by commodity_backfill.

This module remains so any existing references (docs, muscle memory)
keep working. The implementation now lives in commodity_backfill.py,
which handles WTI and GOLD uniformly. Prefer calling that directly:

    python -m collectors.commodity_backfill --symbol WTI

This shim just backfills WTI via the general collector.
"""

from __future__ import annotations

import sys

from collectors.commodity_backfill import backfill, main as _general_main


def main() -> int:
    # Delegate to the general collector, restricted to WTI, preserving
    # the original CLI surface (--interval / --days are parsed there).
    sys.argv = [arg if i != 0 else "commodity_backfill"
                for i, arg in enumerate(sys.argv)]
    if "--symbol" not in sys.argv:
        sys.argv += ["--symbol", "WTI"]
    return _general_main()


if __name__ == "__main__":
    sys.exit(main())
