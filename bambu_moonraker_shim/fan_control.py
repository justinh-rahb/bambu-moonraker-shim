from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional


class FanTarget(str, Enum):
    PART = "part"
    AUX = "aux"
    CHAMBER = "chamber"


FAN_ALIASES: Dict[str, FanTarget] = {
    "part": FanTarget.PART,
    "part_cooling": FanTarget.PART,
    "toolhead": FanTarget.PART,
    "aux": FanTarget.AUX,
    "auxiliary": FanTarget.AUX,
    "chamber": FanTarget.CHAMBER,
    "rear": FanTarget.CHAMBER,
    "case": FanTarget.CHAMBER,
    "exhaust": FanTarget.CHAMBER,
}

FAN_CHANNELS: Dict[FanTarget, int] = {
    FanTarget.PART: 1,
    FanTarget.AUX: 2,
    FanTarget.CHAMBER: 3,
}


@dataclass(frozen=True)
class FanCommand:
    target: FanTarget
    speed: int
    gcode: str


def normalize_fan_target(name: Optional[str]) -> FanTarget:
    if not name:
        return FanTarget.PART

    key = name.strip().lower()
    if key in FAN_ALIASES:
        return FAN_ALIASES[key]

    supported = ", ".join(target.value for target in FanTarget)
    raise ValueError(f"Unknown fan target '{name}'. Supported targets: {supported}.")


def _parse_numeric_speed(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("Fan speed must be a number or percent value.")

    if isinstance(value, (int, float)):
        return float(value)

    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            raise ValueError("Fan speed must be a number or percent value.")
        if stripped.endswith("%"):
            percent = float(stripped.rstrip("%").strip())
            return percent / 100.0
        return float(stripped)

    raise ValueError("Fan speed must be a number or percent value.")


def normalize_fan_speed(value: Any) -> int:
    numeric = _parse_numeric_speed(value)

    if 0.0 <= numeric <= 1.0:
        scaled = round(numeric * 255)
    else:
        scaled = round(numeric)

    return max(0, min(255, int(scaled)))


def build_fan_command(fan: Optional[str], speed: Any) -> FanCommand:
    target = normalize_fan_target(fan)
    speed_value = normalize_fan_speed(speed)
    channel = FAN_CHANNELS[target]
    gcode = f"M106 P{channel} S{speed_value}\n"
    return FanCommand(target=target, speed=speed_value, gcode=gcode)
