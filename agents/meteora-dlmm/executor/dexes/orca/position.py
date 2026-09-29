"""Orca Whirlpool position state helpers."""

from typing import Any, Dict


class OrcaPositionState:
    def __init__(self, data: Dict[str, Any]):
        self.data = data

    @property
    def tick_lower(self) -> int:
        return self.data.get("tickLower", 0)

    @property
    def tick_upper(self) -> int:
        return self.data.get("tickUpper", 0)

    @property
    def liquidity(self) -> str:
        return str(self.data.get("liquidity", 0))

    def in_range(self, tick_current: int) -> bool:
        return self.tick_lower <= tick_current < self.tick_upper


def tick_to_price(tick: int) -> float:
    return pow(1.0001, tick)
