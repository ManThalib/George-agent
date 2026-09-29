"""Orca Whirlpool signal validator."""

from typing import Any, Dict

from common.signal_validator import SignalValidationError, validate_core


def validate(signal: Dict[str, Any], cfg: Dict[str, Any], rails: Dict[str, Any]) -> Dict[str, Any]:
    signal = validate_core(signal, cfg, rails)

    if signal.get("action") != "open":
        return signal

    bin_range = signal.get("bin_range") or {}
    lower = int(bin_range.get("lower", 0))
    upper = int(bin_range.get("upper", 0))
    width = upper - lower + 1
    max_width = int(rails.get("max_orca_range_width", rails.get("max_bin_range_width", 2000)))
    if width > max_width:
        raise SignalValidationError(f"orca range width {width} (inclusive) exceeds rail {max_width}")

    return signal
