"""Datenmodell/Validierung – ohne laufendes HA."""
import pytest
import voluptuous as vol

from custom_components.smart_heating.models import (
    default_config, validate_config, validate_day,
)


def test_defaults():
    cfg = default_config()
    assert cfg["rooms"] == {} and cfg["profiles"] == {}
    s = cfg["settings"]
    assert s["unknown_window_policy"] == "open"  # sicherer Standard
    assert s["outdoor_enabled"] is False
    assert s["outdoor_curve"] == {"start_at": 12.0, "full_at": 20.0, "max_reduction": 3.0}


def test_room_defaults_and_multi_devices():
    cfg = validate_config({"rooms": {"r1": {
        "name": " Wohnzimmer ",
        "thermostats": ["climate.a", "climate.b", "climate.a"],
        "window_sensors": ["binary_sensor.f1", "binary_sensor.f2"],
    }}})
    r = cfg["rooms"]["r1"]
    assert r["name"] == "Wohnzimmer"
    assert r["thermostats"] == ["climate.a", "climate.b"]  # Duplikate entfernt
    assert r["window_open_temp"] == 5.0
    assert r["outdoor_mode"] == "global"


def test_same_device_in_two_rooms_allowed():
    cfg = validate_config({"rooms": {
        "a": {"name": "A", "thermostats": ["climate.x"], "window_sensors": ["binary_sensor.f"]},
        "b": {"name": "B", "thermostats": ["climate.y"], "window_sensors": ["binary_sensor.f"]},
    }})
    assert len(cfg["rooms"]) == 2


@pytest.mark.parametrize("bad", ["sensor.x", "climate", "Climate.X", "climate.a b"])
def test_invalid_thermostat_ids(bad):
    with pytest.raises(vol.Invalid):
        validate_config({"rooms": {"r": {"name": "R", "thermostats": [bad]}}})


def test_temperature_limits():
    with pytest.raises(vol.Invalid):
        validate_config({"rooms": {"r": {"name": "R", "window_open_temp": 50}}})
    with pytest.raises(vol.Invalid):
        validate_config({"rooms": {"r": {"name": ""}}})


def test_outdoor_curve_must_be_ascending():
    with pytest.raises(vol.Invalid):
        validate_config({"settings": {"outdoor_curve": {"start_at": 20, "full_at": 12}}})


def test_day_sorted_and_24h():
    day = validate_day([
        {"start": "06:00", "end": "08:30", "temp": 21},
        {"start": "00:00", "end": "06:00", "temp": 17},
        {"start": "22:00", "end": "24:00", "temp": 17},
    ])
    assert [s["start"] for s in day] == ["00:00", "06:00", "22:00"]


@pytest.mark.parametrize("slots", [
    [{"start": "06:00", "end": "09:00", "temp": 21}, {"start": "08:00", "end": "10:00", "temp": 18}],
    [{"start": "10:00", "end": "09:00", "temp": 21}],
    [{"start": "24:00", "end": "24:00", "temp": 21}],
    [{"start": "6:00", "end": "09:00", "temp": 21}],
])
def test_day_rejects_overlap_and_bad_times(slots):
    with pytest.raises(vol.Invalid):
        validate_day(slots)


def test_orphan_references_cleaned():
    cfg = validate_config({
        "settings": {"active_profile_id": "gone"},
        "rooms": {"r": {"name": "R", "profile_override_id": "gone"}},
        "profiles": {"p": {"name": "Winter", "schedules": {
            "r": {"mon": [{"start": "00:00", "end": "24:00", "temp": 20}]},
            "deleted_room": {},
        }}},
    })
    assert cfg["settings"]["active_profile_id"] is None
    assert cfg["rooms"]["r"]["profile_override_id"] is None
    assert list(cfg["profiles"]["p"]["schedules"]) == ["r"]
    assert cfg["profiles"]["p"]["schedules"]["r"]["tue"] == []


def test_validate_does_not_mutate_input():
    raw = {"rooms": {"r": {"name": " X "}}}
    validate_config(raw)
    assert raw == {"rooms": {"r": {"name": " X "}}}
