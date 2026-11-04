"""Profile, Wochenpläne, Handbetrieb – reine Logik."""
from datetime import UTC, datetime, timedelta

import pytest
import voluptuous as vol

from custom_components.smart_heating.models import default_config, validate_config
from custom_components.smart_heating.profiles import (
    clear_manual, copy_day, copy_profile, copy_room_schedule, create_profile, create_season_presets,
    delete_profile, drop_expired_manual, effective_profile_id, get_week, rename_profile,
    set_active_profile, set_day, set_manual,
)
from custom_components.smart_heating.rooms import RoomError, create_room, update_room

DAY = [{"start": "06:00", "end": "22:00", "temp": 21}]
NOW = datetime(2026, 11, 3, 12, tzinfo=UTC)


@pytest.fixture
def cfg():
    c = default_config()
    c, _ = create_room(c, {"name": "Wohnzimmer"}, "wz")
    c, _ = create_room(c, {"name": "Bad"}, "bad")
    c, _ = create_profile(c, "Winter", "winter")
    c, _ = create_profile(c, "Sommer", "sommer")
    return c


def test_presets_idempotent(cfg):
    cfg, created = create_season_presets(cfg)
    assert len(created) == 2  # Winter/Sommer existieren schon
    names = sorted(p["name"] for p in cfg["profiles"].values())
    assert names == ["Frühling", "Herbst", "Sommer", "Winter"]
    assert create_season_presets(cfg)[1] == []


def test_duplicate_and_rename(cfg):
    with pytest.raises(RoomError):
        create_profile(cfg, "winter")
    cfg = rename_profile(cfg, "winter", "Winter Komfort")
    assert cfg["profiles"]["winter"]["name"] == "Winter Komfort"
    with pytest.raises(RoomError):
        rename_profile(cfg, "winter", "Sommer")


def test_copy_is_independent(cfg):
    cfg = set_day(cfg, "winter", "wz", "mon", DAY)
    cfg, pid = copy_profile(cfg, "winter", "Winter Kopie")
    cfg = set_day(cfg, "winter", "wz", "mon", [])
    assert get_week(cfg, pid, "wz")["mon"][0]["temp"] == 21
    assert get_week(cfg, "winter", "wz")["mon"] == []


def test_delete_in_use_blocked(cfg):
    cfg = set_active_profile(cfg, "winter")
    with pytest.raises(RoomError) as e:
        delete_profile(cfg, "winter")
    assert e.value.code == "profile_in_use"
    cfg = update_room(cfg, "bad", {"profile_override_id": "sommer"})
    with pytest.raises(RoomError):
        delete_profile(cfg, "sommer")
    cfg = update_room(cfg, "bad", {"profile_override_id": None})
    cfg = delete_profile(cfg, "sommer")
    assert "sommer" not in cfg["profiles"]


def test_global_and_room_profile(cfg):
    cfg = set_active_profile(cfg, "winter")
    assert effective_profile_id(cfg, "wz") == "winter"
    cfg = update_room(cfg, "bad", {"profile_override_id": "sommer"})
    assert effective_profile_id(cfg, "bad") == "sommer"
    cfg = set_active_profile(cfg, "sommer")
    assert effective_profile_id(cfg, "wz") == "sommer"
    with pytest.raises(RoomError):
        set_active_profile(cfg, "gibtsnicht")
    assert effective_profile_id(set_active_profile(cfg, None), "wz") is None


def test_set_day_validation(cfg):
    with pytest.raises(vol.Invalid):
        set_day(cfg, "winter", "wz", "mon", [{"start": "06:00", "end": "09:00", "temp": 21},
                                              {"start": "08:00", "end": "10:00", "temp": 19}])
    with pytest.raises(vol.Invalid):
        set_day(cfg, "winter", "wz", "mon", [{"start": "09:00", "end": "08:00", "temp": 21}])
    with pytest.raises(vol.Invalid):
        set_day(cfg, "winter", "wz", "mon", [{"start": "06:00", "end": "08:00", "temp": 99}])
    with pytest.raises(RoomError) as e:
        set_day(cfg, "winter", "wz", "montag", DAY)
    assert e.value.code == "invalid_day"
    with pytest.raises(RoomError):
        set_day(cfg, "winter", "keinraum", "mon", DAY)


def test_copy_day_and_room(cfg):
    cfg = set_day(cfg, "winter", "wz", "mon", DAY)
    cfg = copy_day(cfg, "winter", "wz", "mon", ["tue", "wed", "thu", "fri"])
    w = get_week(cfg, "winter", "wz")
    assert all(w[d] == w["mon"] for d in ("tue", "wed", "thu", "fri")) and w["sat"] == []
    cfg = copy_room_schedule(cfg, "winter", "wz", ["bad"])
    assert get_week(cfg, "winter", "bad") == w
    assert get_week(cfg, "sommer", "bad")["mon"] == []  # anderes Profil unberührt
    assert validate_config(cfg) == cfg


def test_manual_modes_and_expiry(cfg):
    until = NOW + timedelta(hours=2)
    cfg = set_manual(cfg, "wz", 23, "until_next", NOW, until)
    cfg = set_manual(cfg, "bad", 24, "permanent", NOW, until)
    assert cfg["overrides"]["bad"]["until"] is None
    assert drop_expired_manual(cfg, NOW + timedelta(hours=1))["overrides"].keys() == {"wz", "bad"}
    left = drop_expired_manual(cfg, until)
    assert set(left["overrides"]) == {"bad"}
    assert clear_manual(left, "bad")["overrides"] == {}
    with pytest.raises(vol.Invalid):
        set_manual(cfg, "wz", 80, "permanent", NOW, None)


def test_manual_removed_with_room(cfg):
    cfg = set_manual(cfg, "wz", 23, "permanent", NOW, None)
    del cfg["rooms"]["wz"]
    assert validate_config(cfg)["overrides"] == {}
