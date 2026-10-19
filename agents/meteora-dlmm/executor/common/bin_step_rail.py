"""Meteora bin_step rail — executor-side predicate.

Mirrors `Sheldon-agent/strategy/bin_step_rail.py`; the two codebases are
deployed separately so they cannot share an import, but they must agree.
The *value* has one owner on each side: `execution_limits.json`
(`range_limits.bin_step_rule`) for George, `sheldon_policy.json`
(`pool_eligibility.bin_step_rule`) for Sheldon.

Owner ruling 2026-10-19: any Meteora bin_step >= 4 is allowed; 1, 2 and 3 are
rejected; missing/0/malformed stay fail-closed; > 400 is protocol-invalid.
"""

from typing import Any, Dict, Optional, Tuple

METEORA_MIN_BIN_STEP = 4
METEORA_MAX_BIN_STEP = 400

DEFAULT_BIN_STEP_RULE: Dict[str, int] = {
    "min": METEORA_MIN_BIN_STEP,
    "max": METEORA_MAX_BIN_STEP,
}


def normalize_rule(raw: Optional[Any]) -> Dict[str, int]:
    """Coerce a configured rail into ``{"min", "max"}``, bounded by protocol."""
    lo, hi = METEORA_MIN_BIN_STEP, METEORA_MAX_BIN_STEP
    if isinstance(raw, dict):
        try:
            lo = int(raw.get("min", lo))
            hi = int(raw.get("max", hi))
        except (TypeError, ValueError):
            return dict(DEFAULT_BIN_STEP_RULE)
    elif isinstance(raw, (list, tuple)) and raw:
        try:
            values = [int(v) for v in raw]
        except (TypeError, ValueError):
            return dict(DEFAULT_BIN_STEP_RULE)
        lo, hi = min(values), max(values)
    else:
        return dict(DEFAULT_BIN_STEP_RULE)
    return {"min": max(lo, METEORA_MIN_BIN_STEP),
            "max": min(hi, METEORA_MAX_BIN_STEP)}


def meteora_bin_step_allowed(value: Any,
                             rule: Optional[Any] = None,
                             ) -> Tuple[bool, str]:
    """Return ``(allowed, reason)`` for one Meteora bin_step value."""
    if value is None or value == "" or value is False:
        return False, "bin_step unknown (fail-closed)"
    if isinstance(value, bool):
        return False, f"bin_step malformed ({value!r})"
    try:
        step = int(value)
    except (TypeError, ValueError):
        return False, f"bin_step malformed ({value!r})"
    if step == 0:
        return False, "bin_step unknown (fail-closed)"
    if step > METEORA_MAX_BIN_STEP:
        return False, (f"bin_step {step} exceeds Meteora protocol maximum "
                       f"{METEORA_MAX_BIN_STEP}")
    resolved = normalize_rule(rule)
    if step < resolved["min"]:
        return False, f"bin_step {step} below owner minimum {resolved['min']}"
    if step > resolved["max"]:
        return False, f"bin_step {step} above configured maximum {resolved['max']}"
    return True, ""
