"""Actuator command validation (actuator_gateway.validate)."""
import pytest

import actuator_gateway as ag


@pytest.mark.parametrize("actuator, state, expected", [
    ("sprinkler", "on", "on"), ("sprinkler", "off", "off"),
    ("damper", "1", "1.0"), ("damper", 0.5, "0.5"), ("damper", "0.0", "0.0"),
])
def test_valid_commands(actuator, state, expected):
    assert ag.validate(actuator, state) == expected


@pytest.mark.parametrize("actuator, state", [
    ("sprinkler", "ON"), ("sprinkler", 1), ("sprinkler", None),
    ("damper", "open"), ("damper", 1.5), ("damper", -0.1), ("damper", "nan"), ("damper", True),
    ("valve", "on"),
])
def test_invalid_commands(actuator, state):
    with pytest.raises(ValueError):
        ag.validate(actuator, state)


def test_equipment_ids():
    class Z:
        level, name = "level0", "A109"
    eq = ag.equipment(Z)
    assert eq["id"] == "fire_suppression-level0-A109"
    assert [a["id"] for a in eq["actuators"]] == ["level0-A109-sprinkler", "level0-A109-damper"]
