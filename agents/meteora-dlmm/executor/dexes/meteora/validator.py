"""Meteora DLMM signal validator."""

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
    max_width = int(rails.get("max_meteora_range_width", 70))
    if width > max_width:
        raise SignalValidationError(f"meteora range width {width} (inclusive) exceeds rail {max_width}")

    return signal
