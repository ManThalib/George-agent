"""Meteora DLMM position state helpers."""

from typing import Any, Dict, Optional


class MeteoraPositionState:
    """In-memory cache of a Meteora position read from the chain."""

    def __init__(self, data: Dict[str, Any]):
        self.data = data

    @property
    def bins(self) -> list:
        return self.data.get("positionData", {}).get("positionBinData", [])

    @property
    def total_liquidity(self) -> int:
        return sum(int(b.get("positionLiquidity", 0)) for b in self.bins)

    @property
    def fee_x(self) -> str:
        return str(self.data.get("positionData", {}).get("feeX", 0))

    @property
    def fee_y(self) -> str:
        return str(self.data.get("positionData", {}).get("feeY", 0))

    def is_empty(self) -> bool:
        return self.total_liquidity == 0


def empty_position_state() -> Optional[MeteoraPositionState]:
    return None
