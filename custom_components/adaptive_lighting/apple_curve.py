"""Pure evaluation of the HAP-NodeJS adaptive lighting transition format."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


def finite_number(value: Any, label: str, minimum: float = -math.inf) -> float:
    """Validate an external number without accepting booleans or NaN."""
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < minimum
    ):
        msg = f"Invalid Apple curve field: {label}"
        raise ValueError(msg)
    return float(value)


@dataclass(frozen=True)
class CurveNode:
    """A node, with its incoming transition and subsequent hold in milliseconds."""

    temperature: float
    adjustment: float
    transition: float
    duration: float


@dataclass(frozen=True)
class AppleCurve:
    """Validated curve independent of Home Assistant and transport clocks."""

    nodes: tuple[CurveNode, ...]
    min_brightness: float
    max_brightness: float
    min_mired: float
    max_mired: float

    @classmethod
    def from_snapshot(cls, snapshot: dict[str, Any]) -> AppleCurve:
        """Read only the curve and source capability from a snapshot."""
        try:
            schedule = snapshot["schedule"]
            entries = schedule["transitionCurve"]
            if not isinstance(entries, list) or len(entries) < 2:
                msg = "An Apple curve requires at least two nodes"
                raise ValueError(msg)
            nodes = tuple(
                CurveNode(
                    finite_number(node["temperature"], "temperature", 0),
                    finite_number(node["brightnessAdjustmentFactor"], "adjustment"),
                    finite_number(node["transitionTime"], "transitionTime", 0),
                    finite_number(node.get("duration", 0), "duration", 0),
                )
                for node in entries
            )
            brightness = schedule["brightnessAdjustmentRange"]
            low = finite_number(brightness["minBrightnessValue"], "minBrightness", 0)
            high = finite_number(brightness["maxBrightnessValue"], "maxBrightness", low)
            limits = snapshot["source"]["advertisedColorTemperature"]
            min_mired = finite_number(limits["minMired"], "minMired", 1)
            max_mired = finite_number(limits["maxMired"], "maxMired", min_mired)
            if high > 100:
                msg = "Apple brightness range exceeds 100 percent"
                raise ValueError(msg)
            for node in nodes:
                finite_number(
                    node.temperature + node.adjustment * low,
                    "adjusted temperature",
                )
                finite_number(
                    node.temperature + node.adjustment * high,
                    "adjusted temperature",
                )
            return cls(nodes, low, high, min_mired, max_mired)
        except (KeyError, TypeError, AttributeError) as err:
            msg = "Malformed Apple transition curve"
            raise ValueError(msg) from err

    @property
    def first_offset(self) -> float:
        """Return the first node's offset from the execution start."""
        return self.nodes[0].transition

    @property
    def end_offset(self) -> float:
        """Return the final node's offset; its trailing duration is ignored by HAP."""
        return (
            sum(node.transition + node.duration for node in self.nodes[:-1])
            + self.nodes[-1].transition
        )

    def mired(self, offset_millis: float, brightness_pct: float) -> int | None:
        """Evaluate a point, holding the final node when called beyond the end.

        This follows HAP-NodeJS 2.2.3's lower-node hold and upper-node transition
        semantics. The caller owns expiry and the optional grace period.
        """
        finite_number(offset_millis, "offset")
        brightness_pct = finite_number(brightness_pct, "brightness")
        if offset_millis < self.first_offset:
            return None
        temperature = self.nodes[-1].temperature
        factor = self.nodes[-1].adjustment
        if offset_millis < self.end_offset:
            lower_offset = 0.0
            for lower, upper in zip(self.nodes, self.nodes[1:], strict=False):
                lower_offset += lower.transition
                elapsed = offset_millis - lower_offset
                if elapsed <= lower.duration + upper.transition:
                    # Zero-length transitions select the upper node immediately once
                    # a hold ends. Avoid the upstream zero/zero NaN edge case.
                    if lower.duration and elapsed <= lower.duration:
                        ratio = 0.0
                    elif upper.transition:
                        ratio = (elapsed - lower.duration) / upper.transition
                    else:
                        ratio = 1.0
                    temperature = (
                        lower.temperature
                        + (upper.temperature - lower.temperature) * ratio
                    )
                    factor = (
                        lower.adjustment + (upper.adjustment - lower.adjustment) * ratio
                    )
                    break
                lower_offset += lower.duration
        brightness = min(self.max_brightness, max(self.min_brightness, brightness_pct))
        # Python round uses ties-to-even; JavaScript Math.round chooses +infinity.
        adjusted = math.floor(temperature + factor * brightness + 0.5)
        return int(min(self.max_mired, max(self.min_mired, adjusted)))

    def kelvin(self, offset_millis: float, brightness_pct: float) -> int | None:
        """Return the evaluated color temperature as whole Kelvin."""
        mired = self.mired(offset_millis, brightness_pct)
        return None if mired is None else round(1_000_000 / mired)
