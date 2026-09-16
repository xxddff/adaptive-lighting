import copy
import json
from pathlib import Path

import pytest
from homeassistant.components.adaptive_lighting.apple_curve import AppleCurve

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def apple_snapshot():
    return json.loads((FIXTURES / "apple-snapshot.json").read_text())


@pytest.mark.parametrize(
    "case",
    json.loads((FIXTURES / "apple-hap-2.2.3-golden.json").read_text())["cases"],
)
def test_hap_nodejs_golden_values(apple_snapshot, case):
    curve = AppleCurve.from_snapshot(apple_snapshot)
    assert curve.mired(case["offset"], case["brightness"]) == case["mired"]
    assert curve.kelvin(case["offset"], case["brightness"]) == round(
        1_000_000 / case["mired"],
    )


def test_holds_final_node_without_repeating_day(apple_snapshot):
    curve = AppleCurve.from_snapshot(apple_snapshot)
    assert curve.mired(curve.end_offset + 500_000, 50) == curve.mired(
        curve.end_offset,
        50,
    )
    assert curve.mired(curve.end_offset + 500_000, 10) != curve.mired(
        curve.end_offset,
        100,
    )


def test_first_offset_and_last_duration(apple_snapshot):
    nodes = apple_snapshot["schedule"]["transitionCurve"]
    nodes[0]["transitionTime"] = 10_000
    nodes[-1]["duration"] = 99_000_000
    curve = AppleCurve.from_snapshot(apple_snapshot)
    assert curve.first_offset == 10_000
    assert curve.end_offset == 7_810_000
    assert curve.mired(9_999, 50) is None
    assert curve.kelvin(9_999, 50) is None
    assert curve.mired(10_000, 50) == 375


def test_js_round_and_post_adjustment_clamp(apple_snapshot):
    nodes = apple_snapshot["schedule"]["transitionCurve"]
    nodes[0].update(temperature=500.5, brightnessAdjustmentFactor=-1)
    curve = AppleCurve.from_snapshot(apple_snapshot)
    # Clamping 500.5 before adjustment would incorrectly produce 404.
    assert curve.mired(0, 50) == 451
    assert curve.mired(0, 10) == 454
    nodes[0].update(temperature=100.5, brightnessAdjustmentFactor=1)
    curve = AppleCurve.from_snapshot(apple_snapshot)
    assert curve.mired(0, 100) == 201
    assert curve.mired(0, 10) == 153


def test_zero_length_transition_and_nonfinite_input(apple_snapshot):
    nodes = apple_snapshot["schedule"]["transitionCurve"]
    nodes[0]["duration"] = 0
    nodes[1]["transitionTime"] = 0
    curve = AppleCurve.from_snapshot(apple_snapshot)
    assert curve.mired(0, 50) == 348
    with pytest.raises(ValueError, match="brightness"):
        curve.mired(0, float("nan"))


@pytest.mark.parametrize("bad", [None, {}, {"schedule": {"transitionCurve": []}}])
def test_rejects_malformed_curve(bad):
    with pytest.raises(ValueError, match=r"(Malformed|requires)"):
        AppleCurve.from_snapshot(bad)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), True, "300", -1])
def test_rejects_nonfinite_or_invalid_temperature(apple_snapshot, value):
    apple_snapshot["schedule"]["transitionCurve"][0]["temperature"] = value
    with pytest.raises(ValueError, match="temperature"):
        AppleCurve.from_snapshot(apple_snapshot)


def test_invalid_capability_and_brightness_ranges(apple_snapshot):
    bad = copy.deepcopy(apple_snapshot)
    bad["source"]["advertisedColorTemperature"]["maxMired"] = 1
    with pytest.raises(ValueError, match="maxMired"):
        AppleCurve.from_snapshot(bad)
    apple_snapshot["schedule"]["brightnessAdjustmentRange"]["maxBrightnessValue"] = 101
    with pytest.raises(ValueError, match="100 percent"):
        AppleCurve.from_snapshot(apple_snapshot)


def test_final_zero_length_transition_selects_final_node(apple_snapshot):
    apple_snapshot["schedule"]["transitionCurve"] = [
        {
            "temperature": 400,
            "brightnessAdjustmentFactor": 0,
            "transitionTime": 0,
            "duration": 600_000,
        },
        {"temperature": 300, "brightnessAdjustmentFactor": 0, "transitionTime": 0},
    ]
    curve = AppleCurve.from_snapshot(apple_snapshot)
    assert curve.mired(599_999, 100) == 400
    assert curve.mired(600_000, 100) == 300
    assert curve.mired(660_000, 100) == 300


def test_finite_fields_cannot_overflow_during_brightness_adjustment(apple_snapshot):
    apple_snapshot["schedule"]["transitionCurve"][0][
        "brightnessAdjustmentFactor"
    ] = 1e308
    with pytest.raises(ValueError, match="adjusted temperature"):
        AppleCurve.from_snapshot(apple_snapshot)
