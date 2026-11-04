"""Außentemperaturregelung – reine Logik."""
import copy

import pytest
import voluptuous as vol

from custom_components.smart_heating.models import validate_config
from custom_components.smart_heating.targets import outdoor_reduction, resolve_effective, room_reduction

LINEAR = {"start_at": 12.0, "full_at": 20.0, "max_reduction": 3.0}
# Tabelle aus der Spezifikation als Stützpunkte
SPEC_POINTS = {**LINEAR, "points": [[12, 0], [14, 0.5], [16, 1.0], [18, 2.0], [20, 3.0]]}


@pytest.mark.parametrize("t, red", [(8, 0), (12, 0), (14, 0.75), (16, 1.5), (18, 2.25), (20, 3), (25, 3)])
def test_linear_interpolation(t, red):
    assert outdoor_reduction(LINEAR, t) == pytest.approx(red, abs=0.051)


@pytest.mark.parametrize("t, red", [(8, 0), (12, 0), (14, 0.5), (16, 1.0), (18, 2.0), (20, 3.0),
                                    (17, 1.5), (13, 0.2), (30, 3.0)])
def test_spec_table_with_points(t, red):
    curve = validate_config({"settings": {"outdoor_curve": SPEC_POINTS}})["settings"]["outdoor_curve"]
    assert outdoor_reduction(curve, t) == pytest.approx(red, abs=0.051)


@pytest.mark.parametrize("points", [
    [[14, 0.5], [12, 0]],             # nicht steigend
    [[12, 1.0], [14, 0.5]],           # Absenkung fällt
    [[12, 0]],                        # nur ein Punkt
    [[12, 0, 1], [14, 1]],            # falsches Format
    [[12, 0], [14, 20]],              # Absenkung zu groß
])
def test_invalid_points(points):
    with pytest.raises(vol.Invalid):
        validate_config({"settings": {"outdoor_curve": {**LINEAR, "points": points}}})


def _cfg(**settings):
    return validate_config({
        "settings": {"outdoor_enabled": True, "outdoor_sensor": "sensor.aussen", **settings},
        "rooms": {
            "wz": {"name": "WZ", "thermostats": ["climate.wz"], "window_sensors": ["binary_sensor.f"]},
            "bad": {"name": "Bad", "thermostats": ["climate.bad"], "outdoor_mode": "off"},
            "sz": {"name": "SZ", "thermostats": ["climate.sz"], "outdoor_mode": "custom",
                   "outdoor_curve": {"start_at": 0, "full_at": 10, "max_reduction": 2}},
        },
    })


def test_room_modes_and_global_switch():
    cfg = _cfg()
    assert room_reduction(cfg, "wz", 16) == 1.5
    assert room_reduction(cfg, "bad", 16) == 0
    assert room_reduction(cfg, "sz", 5) == 1.0 and room_reduction(cfg, "sz", 16) == 2.0
    assert room_reduction(cfg, "wz", None) == 0  # Sensor ungültig
    assert room_reduction(_cfg(outdoor_enabled=False), "wz", 16) == 0
    off = copy.deepcopy(cfg)
    off["settings"]["outdoor_sensor"] = None
    assert room_reduction(off, "wz", 16) == 0


CLOSED = {"binary_sensor.f": "closed"}


def D(t, source="schedule"):
    return {r: {"desired_temp": t, "source": source} for r in ("wz", "bad", "sz")}


def test_reduction_applied_and_desired_kept():
    r = resolve_effective(_cfg(), D(21), CLOSED, 16)["rooms"]["wz"]
    assert r["desired_temp"] == 21 and r["outdoor_reduction"] == 1.5
    assert r["adjusted_temp"] == r["effective_temp"] == 19.5


def test_manual_setpoint_not_reduced():
    r = resolve_effective(_cfg(), D(22, "manual"), CLOSED, 18)["rooms"]["wz"]
    assert r["effective_temp"] == 22 and r["outdoor_reduction"] == 0


def test_window_lock_has_priority():
    r = resolve_effective(_cfg(), D(21), {"binary_sensor.f": "open"}, 20)["rooms"]["wz"]
    assert r["locked"] and r["effective_temp"] == 5 and r["adjusted_temp"] == 18


def test_floor_and_no_sensor_value():
    assert resolve_effective(_cfg(), D(6), CLOSED, 25)["rooms"]["wz"]["effective_temp"] == 5
    assert resolve_effective(_cfg(), D(4.5), CLOSED, 25)["rooms"]["wz"]["effective_temp"] == 4.5
    assert resolve_effective(_cfg(), D(21), CLOSED, None)["rooms"]["wz"]["effective_temp"] == 21
