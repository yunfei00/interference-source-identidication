from __future__ import annotations

import math


def format_time_value(seconds: float) -> str:
    """Format an SI-second value for concise human-readable display."""
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return "N/A"
    if not math.isfinite(value):
        return "N/A"

    magnitude = abs(value)
    if magnitude == 0 or magnitude >= 1.0:
        return f"{value:.5g} s"
    if magnitude >= 1e-3:
        return f"{value * 1e3:.5g} ms"
    if magnitude >= 1e-6:
        return f"{value * 1e6:.5g} μs"
    if magnitude >= 1e-9:
        return f"{value * 1e9:.5g} ns"
    return f"{value * 1e12:.5g} ps"
