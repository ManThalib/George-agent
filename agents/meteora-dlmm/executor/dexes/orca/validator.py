"""Orca Whirlpool signal validator.

Common rules (required fields, tick containment, range width, remove bps,
sizing, score floor) live in common.signal_validator.
"""

from typing import Any, Dict

from common.signal_validator import validate_core


def validate(signal: Dict[str, Any], cfg: Dict[str, Any], rails: Dict[str, Any]) -> Dict[str, Any]:
    return validate_core(signal, cfg, rails)
