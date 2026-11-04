"""Raumverwaltung (reine Logik)."""
import pytest
import voluptuous as vol

from custom_components.smart_heating.models import default_config, validate_config
from custom_components.smart_heating.rooms import (
    RoomError,
    all_entities,
    assign_entity,
    create_room,
    delete_room,
    device_map,
    rooms_for_thermostat,
    thermostats_for_window,
    unassign_entity,
    update_room,
    windows_for_thermostat,
)


def _cfg_two_rooms_shared():
    """Flur + Bad teilen sich climate.flur; binary_sensor.terrasse wirkt auf zwei Räume."""
    cfg = default_config()
    cfg, _ = create_room(cfg, {"name": "Wohnzimmer", "thermostats": ["climate.wz1", "climate.wz2"],
                               "window_sensors": ["binary_sensor.wz_f1", "binary_sensor.terrasse"]}, "wz")
    cfg, _ = create_room(cfg, {"name": "Esszimmer", "thermostats": ["climate.ez", "climate.flur"],
                               "window_sensors": ["binary_sensor.terrasse"]}, "ez")
    cfg, _ = create_room(cfg, {"name": "Flur", "thermostats": ["climate.flur"],
                               "window_sensors": ["binary_sensor.haustuer"]}, "flur")
    return validate_config(cfg)


def test_create_defaults_and_id():
    cfg, rid = create_room(default_config(), {"name": "Bad"})
    assert len(rid) == 12
    room = cfg["rooms"][rid]
    assert room["thermostats"] == [] and room["window_open_temp"] == 5.0


def test_duplicate_name_case_insensitive():
    cfg, _ = create_room(default_config(), {"name": "Bad"})
    with pytest.raises(RoomError) as e:
        create_room(cfg, {"name": " bad "})
    assert e.value.code == "duplicate_name"


def test_unknown_field_rejected():
    with pytest.raises(RoomError) as e:
        create_room(default_config(), {"name": "X", "foo": 1})
    assert e.value.code == "invalid_field"


def test_invalid_entity_domains():
    with pytest.raises(vol.Invalid):
        create_room(default_config(), {"name": "X", "thermostats": ["switch.heizung"]})
    with pytest.raises(vol.Invalid):
        create_room(default_config(), {"name": "X", "window_sensors": ["climate.x"]})


def test_update_partial_keeps_other_fields():
    cfg = _cfg_two_rooms_shared()
    cfg = update_room(cfg, "wz", {"window_open_temp": 7, "outdoor_curve": {"max_reduction": 2}})
    r = cfg["rooms"]["wz"]
    assert r["window_open_temp"] == 7.0
    assert r["thermostats"] == ["climate.wz1", "climate.wz2"]
    assert r["outdoor_curve"] == {"start_at": 12.0, "full_at": 20.0, "max_reduction": 2.0}


def test_update_rename_to_existing_rejected_but_self_ok():
    cfg = _cfg_two_rooms_shared()
    update_room(cfg, "wz", {"name": "wohnzimmer"})  # eigener Name, andere Schreibweise
    with pytest.raises(RoomError):
        update_room(cfg, "wz", {"name": "Flur"})


def test_update_unknown_room_and_profile():
    cfg = _cfg_two_rooms_shared()
    with pytest.raises(RoomError) as e:
        update_room(cfg, "nope", {"name": "X"})
    assert e.value.code == "room_not_found"
    with pytest.raises(RoomError) as e:
        update_room(cfg, "wz", {"profile_override_id": "gibtsnicht"})
    assert e.value.code == "profile_not_found"


def test_delete_removes_schedules():
    cfg = _cfg_two_rooms_shared()
    cfg["profiles"]["p"] = {"name": "Winter", "schedules": {"wz": {}, "ez": {}}}
    cfg = validate_config(delete_room(cfg, "wz"))
    assert "wz" not in cfg["rooms"]
    assert list(cfg["profiles"]["p"]["schedules"]) == ["ez"]
    with pytest.raises(RoomError):
        delete_room(cfg, "wz")


def test_assign_unassign_idempotent():
    cfg = _cfg_two_rooms_shared()
    cfg = assign_entity(cfg, "flur", "thermostat", "climate.flur2")
    cfg = assign_entity(cfg, "flur", "thermostat", "climate.flur2")
    assert cfg["rooms"]["flur"]["thermostats"] == ["climate.flur", "climate.flur2"]
    cfg = unassign_entity(cfg, "flur", "thermostat", "climate.flur2")
    assert cfg["rooms"]["flur"]["thermostats"] == ["climate.flur"]
    with pytest.raises(RoomError) as e:
        unassign_entity(cfg, "flur", "thermostat", "climate.flur2")
    assert e.value.code == "not_assigned"
    with pytest.raises(RoomError) as e:
        assign_entity(cfg, "flur", "heater", "climate.x")
    assert e.value.code == "invalid_kind"


def test_multiple_thermostats_and_windows_per_room():
    cfg = _cfg_two_rooms_shared()
    assert len(cfg["rooms"]["wz"]["thermostats"]) == 2
    assert len(cfg["rooms"]["wz"]["window_sensors"]) == 2


def test_one_window_many_thermostats():
    cfg = _cfg_two_rooms_shared()
    assert thermostats_for_window(cfg, "binary_sensor.terrasse") == [
        "climate.wz1", "climate.wz2", "climate.ez", "climate.flur"]
    assert thermostats_for_window(cfg, "binary_sensor.wz_f1") == ["climate.wz1", "climate.wz2"]


def test_shared_thermostat_considers_windows_of_all_rooms():
    """Entscheidung 3: Fenster in irgendeinem zugeordneten Raum sperrt das Thermostat."""
    cfg = _cfg_two_rooms_shared()
    assert rooms_for_thermostat(cfg, "climate.flur") == ["ez", "flur"]
    assert windows_for_thermostat(cfg, "climate.flur") == [
        "binary_sensor.terrasse", "binary_sensor.haustuer"]
    assert windows_for_thermostat(cfg, "climate.wz1") == [
        "binary_sensor.wz_f1", "binary_sensor.terrasse"]


def test_device_map_and_all_entities():
    cfg = _cfg_two_rooms_shared()
    cfg = update_room(cfg, "wz", {"temperature_sensor": "sensor.wz_temp"})
    dm = device_map(cfg)
    assert dm["thermostats"]["climate.flur"] == ["ez", "flur"]
    assert dm["window_sensors"]["binary_sensor.terrasse"] == ["wz", "ez"]
    assert "sensor.wz_temp" in all_entities(cfg)
    assert "climate.flur" in all_entities(cfg)
