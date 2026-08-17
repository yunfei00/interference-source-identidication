"""Deprecated compatibility entry point for the DELAY measurement tester."""

from __future__ import annotations

try:
    from tools.delay_measurement_tester import main
except ImportError:  # Direct execution puts the tools directory on sys.path.
    from delay_measurement_tester import main


if __name__ == "__main__":
    raise SystemExit(main())
