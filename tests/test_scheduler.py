"""Zeitsteuerung, Handbetrieb, Neustart, Sommer-/Winterzeit und Profil-API – gegen HA-Core."""
import asyncio
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.smart_heating.const import DOMAIN, STORAGE_KEY
from custom_components.smart_heating.profiles import set_day, set_manual

TZ = ZoneInfo("Europe/Berlin")
WORKDAY = [
    {"start": "00:00", "end": "06:00", "temp": 17}, {"start": "06:00", "end": "08:30", "temp": 21},
    {"start": "08:30", "end": "16:00", "temp": 18}, {"start": "16:00", "end": "22:00", "temp": 21},
    {"start": "22:00", "end": "24:00", "temp": 17},
]
SUMMER = [{"start": "00:00", "end": "24:00", "temp": 16}]
BASE = {
    "settings": {"active_profile_id": "winter", "default_temp": 19},
    "rooms": {"wz": {"name": "Wohnzimmer"}, "bad": {"name": "Bad"}},
    "profiles": {
        "winter": {"name": "Winter", "schedules": {
            "wz": {d: WORKDAY for d in ("mon", "tue", "wed", "thu", "fri")},
            "bad": {"mon": [{"start": "05:00", "end": "09:00", "temp": 23}]}}},
        "sommer": {"name": "Sommer", "schedules": {
            "wz": {d: SUMMER for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}}},
    },
}


def loc(*a):
    return datetime(*a, tzinfo=TZ)


async def _start(hass, hass_storage, freezer, when, data=BASE):
    await hass.config.async_set_time_zone("Europe/Berlin")
    freezer.move_to(when)
    hass_storage[STORAGE_KEY] = {"version": 1, "minor_version": 1, "key": STORAGE_KEY, "data": data}
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _tick(hass, freezer, when):
    freezer.move_to(when)
    async_fire_time_changed(hass, when)
    await hass.async_block_till_done()


def T(entry, rid="wz"):
    return entry.runtime_data.scheduler.targets[rid]


async def test_restart_inside_slot_and_single_timer(hass, hass_storage, freezer):
    entry = await _start(hass, hass_storage, freezer, loc(2026, 11, 2, 7, 15))  # Mo 07:15
    assert T(entry)["desired_temp"] == 21 and T(entry)["source"] == "schedule"
    assert T(entry, "bad")["desired_temp"] == 23
    # nächster Weckzeitpunkt = frühester Wechsel aller Räume (wz 08:30, bad 09:00)
    assert entry.runtime_data.scheduler.next_wakeup == loc(2026, 11, 2, 8, 30)


async def test_slot_and_day_changes_via_timer(hass, hass_storage, freezer):
    entry = await _start(hass, hass_storage, freezer, loc(2026, 11, 2, 7, 15))
    await _tick(hass, freezer, loc(2026, 11, 2, 8, 30))
    assert T(entry)["desired_temp"] == 18
    await _tick(hass, freezer, loc(2026, 11, 2, 9, 0))
    assert T(entry, "bad")["source"] == "default" and T(entry, "bad")["desired_temp"] == 19
    for h, temp in ((16, 21), (22, 17)):
        await _tick(hass, freezer, loc(2026, 11, 2, h, 0))
        assert T(entry)["desired_temp"] == temp
    await _tick(hass, freezer, loc(2026, 11, 3, 0, 0))  # Tageswechsel
    assert T(entry)["desired_temp"] == 17 and T(entry)["slot"]["start"] == "00:00"
    await _tick(hass, freezer, loc(2026, 11, 3, 6, 0))
    assert T(entry)["desired_temp"] == 21


async def test_no_timer_without_schedules(hass, hass_storage, freezer):
    entry = await _start(hass, hass_storage, freezer, loc(2026, 11, 2, 7, 0),
                         {"rooms": {"x": {"name": "X"}}})
    assert entry.runtime_data.scheduler.next_wakeup is None
    assert T(entry, "x")["source"] == "default"


@pytest.mark.freeze_time("2026-11-02T06:15:00+00:00")
async def test_global_switch_and_room_override(hass, hass_storage, freezer, hass_ws_client):
    entry = await _start(hass, hass_storage, freezer, loc(2026, 11, 2, 7, 15))
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": f"{DOMAIN}/profiles/set_active", "profile_id": "sommer"})
    assert (await ws.receive_json())["success"]
    assert T(entry)["desired_temp"] == 16 and T(entry)["profile_id"] == "sommer"
    assert T(entry, "bad")["source"] == "default"  # Sommer hat keinen Plan für Bad
    await ws.send_json({"id": 2, "type": f"{DOMAIN}/rooms/update", "room_id": "bad",
                        "profile_override_id": "winter"})
    assert (await ws.receive_json())["success"]
    assert T(entry, "bad")["desired_temp"] == 23 and T(entry, "bad")["profile_source"] == "room"
    assert T(entry)["profile_id"] == "sommer"  # andere Räume weiter global


@pytest.mark.freeze_time("2026-11-02T06:15:00+00:00")
async def test_manual_until_next_expires_and_is_removed(hass, hass_storage, freezer, hass_ws_client):
    entry = await _start(hass, hass_storage, freezer, loc(2026, 11, 2, 7, 15))
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": f"{DOMAIN}/manual/set", "room_id": "wz", "temperature": 23.5})
    r = await ws.receive_json()
    assert r["success"] and r["result"]["source"] == "manual" and r["result"]["desired_temp"] == 23.5
    assert datetime.fromisoformat(r["result"]["manual"]["until"]) == loc(2026, 11, 2, 8, 30)
    assert r["result"]["scheduled_temp"] == 21  # Plan bleibt getrennt sichtbar
    await entry.runtime_data.store.async_flush()
    assert hass_storage[STORAGE_KEY]["data"]["overrides"]["wz"]["temp"] == 23.5  # persistent
    await _tick(hass, freezer, loc(2026, 11, 2, 8, 30))
    assert T(entry)["source"] == "schedule" and T(entry)["desired_temp"] == 18
    assert entry.runtime_data.store.data["overrides"] == {}


async def test_manual_permanent_survives_slots(hass, hass_storage, freezer):
    entry = await _start(hass, hass_storage, freezer, loc(2026, 11, 2, 7, 15))
    sch = entry.runtime_data.scheduler
    await sch.async_set_manual("wz", 22, "permanent")
    for h in (8, 16, 22):
        await _tick(hass, freezer, loc(2026, 11, 2, h, 30))
        assert T(entry)["desired_temp"] == 22 and T(entry)["source"] == "manual"
    await sch.async_set_manual("wz", 20, "permanent")  # nächste manuelle Änderung ersetzt
    assert T(entry)["desired_temp"] == 20
    await sch.async_clear_manual("wz")
    assert T(entry)["source"] == "schedule" and T(entry)["desired_temp"] == 17


async def test_manual_until_next_without_schedule(hass, hass_storage, freezer):
    entry = await _start(hass, hass_storage, freezer, loc(2026, 11, 2, 7, 0),
                         {"rooms": {"x": {"name": "X"}}})
    await entry.runtime_data.scheduler.async_set_manual("x", 21, "until_next")
    assert T(entry, "x")["manual"]["until"] is None  # kein Wechsel -> bis zur nächsten Änderung
    assert T(entry, "x")["desired_temp"] == 21


@pytest.mark.parametrize("until, expect_source", [
    (datetime(2026, 11, 2, 7, 30, tzinfo=UTC), "manual"),    # noch gültig nach Neustart
    (datetime(2026, 11, 2, 5, 0, tzinfo=UTC), "schedule"),   # während Ausfall abgelaufen
])
async def test_restart_with_stored_manual(hass, hass_storage, freezer, until, expect_source):
    now = loc(2026, 11, 2, 7, 15)
    data = set_manual({**BASE, "overrides": {}}, "wz", 24, "until_next", now.astimezone(UTC), until)
    entry = await _start(hass, hass_storage, freezer, now, data)
    assert T(entry)["source"] == expect_source
    await hass.async_block_till_done()
    assert ("wz" in entry.runtime_data.store.data["overrides"]) == (expect_source == "manual")


DST = {"sun": [{"start": "00:00", "end": "02:30", "temp": 17},
               {"start": "02:30", "end": "24:00", "temp": 21}]}


async def test_spring_forward_in_ha(hass, hass_storage, freezer):
    data = {"settings": {"active_profile_id": "p"}, "rooms": {"r": {"name": "R"}},
            "profiles": {"p": {"name": "P", "schedules": {"r": DST}}}}
    entry = await _start(hass, hass_storage, freezer, datetime(2026, 3, 29, 0, 30, tzinfo=UTC), data)
    assert T(entry, "r")["desired_temp"] == 17
    assert entry.runtime_data.scheduler.next_wakeup == datetime(2026, 3, 29, 1, 0, tzinfo=UTC)
    await _tick(hass, freezer, datetime(2026, 3, 29, 1, 0, tzinfo=UTC))  # 03:00 CEST
    assert T(entry, "r")["desired_temp"] == 21


async def test_fall_back_in_ha(hass, hass_storage, freezer):
    data = {"settings": {"active_profile_id": "p"}, "rooms": {"r": {"name": "R"}},
            "profiles": {"p": {"name": "P", "schedules": {"r": DST}}}}
    entry = await _start(hass, hass_storage, freezer, datetime(2026, 10, 25, 0, 0, tzinfo=UTC), data)
    await _tick(hass, freezer, datetime(2026, 10, 25, 0, 30, tzinfo=UTC))   # 02:30 CEST
    assert T(entry, "r")["desired_temp"] == 21
    await _tick(hass, freezer, datetime(2026, 10, 25, 1, 0, tzinfo=UTC))    # 02:00 CET (Rückstellung)
    assert T(entry, "r")["desired_temp"] == 17
    await _tick(hass, freezer, datetime(2026, 10, 25, 1, 30, tzinfo=UTC))   # 02:30 CET
    assert T(entry, "r")["desired_temp"] == 21


async def test_concurrent_schedule_and_manual_changes(hass, hass_storage, freezer):
    entry = await _start(hass, hass_storage, freezer, loc(2026, 11, 2, 7, 15))
    store, sch = entry.runtime_data.store, entry.runtime_data.scheduler
    days = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
    await asyncio.gather(
        *[store.async_update(lambda c, d=d, i=i: set_day(
            c, "sommer", "bad", d, [{"start": "00:00", "end": "24:00", "temp": 15 + i}]))
          for i, d in enumerate(days)],
        sch.async_set_manual("wz", 25, "permanent"),
    )
    week = store.data["profiles"]["sommer"]["schedules"]["bad"]
    assert [week[d][0]["temp"] for d in days] == [15, 16, 17, 18, 19, 20, 21]
    assert T(entry)["desired_temp"] == 25


@pytest.mark.freeze_time("2026-11-02T06:15:00+00:00")
async def test_profile_and_schedule_api(hass, hass_storage, freezer, hass_ws_client):
    await _start(hass, hass_storage, freezer, loc(2026, 11, 2, 7, 15))
    ws = await hass_ws_client(hass)
    n = 0

    async def call(t, **kw):
        nonlocal n
        n += 1
        await ws.send_json({"id": n, "type": f"{DOMAIN}/{t}", **kw})
        return await ws.receive_json()

    r = await call("profiles/create_presets")
    assert sorted(p["name"] for p in r["result"]["profiles"]) == ["Frühling", "Herbst", "Sommer", "Winter"]
    r = await call("profiles/create", name="Urlaub", copy_from="winter")
    urlaub = next(p for p in r["result"]["profiles"] if p["name"] == "Urlaub")
    assert urlaub["rooms"] == ["bad", "wz"]
    assert (await call("profiles/rename", profile_id=urlaub["profile_id"], name="Abwesend"))["success"]
    assert (await call("profiles/copy", profile_id="sommer", name="Sommer 2"))["success"]
    r = await call("profiles/delete", profile_id="winter")
    assert r["error"]["code"] == "profile_in_use"
    r = await call("profiles/delete", profile_id=urlaub["profile_id"])
    assert r["success"] and "Abwesend" not in [p["name"] for p in r["result"]["profiles"]]

    r = await call("schedule/set_day", profile_id="winter", room_id="bad", day="sat",
                   slots=[{"start": "07:00", "end": "10:00", "temp": 22},
                          {"start": "09:00", "end": "11:00", "temp": 20}])
    assert r["error"]["code"] == "invalid_format"  # Überschneidung
    r = await call("schedule/set_day", profile_id="winter", room_id="bad", day="sat",
                   slots=[{"start": "07:00", "end": "10:00", "temp": 22}])
    assert r["success"] and r["result"]["week"]["sat"][0]["temp"] == 22
    r = await call("schedule/copy_day", profile_id="winter", room_id="bad", source_day="sat",
                   target_days=["sun"])
    assert r["result"]["week"]["sun"] == r["result"]["week"]["sat"]
    r = await call("schedule/copy_room", profile_id="winter", source_room_id="wz", target_room_ids=["bad"])
    assert r["success"]
    r = await call("schedule/get", profile_id="winter", room_id="bad")
    assert r["result"]["week"]["mon"][1]["temp"] == 21
    r = await call("schedule/set_week", profile_id="winter", room_id="bad", week={"funday": []})
    assert r["error"]["code"] == "invalid_day"
    r = await call("schedule/get", profile_id="nix", room_id="bad")
    assert r["error"]["code"] == "profile_not_found"
    r = await call("targets")
    assert r["result"]["targets"]["bad"]["desired_temp"] == 21  # Mo 07:15 nach Kopie
    r = await call("manual/set", room_id="nix", temperature=20)
    assert r["error"]["code"] == "room_not_found"
    r = await call("manual/set", room_id="wz", temperature=60)
    assert r["error"]["code"] == "invalid_format"


@pytest.mark.freeze_time("2026-11-02T06:15:00+00:00")
async def test_non_admin_read_only(hass, hass_storage, freezer, hass_ws_client, hass_read_only_access_token):
    await _start(hass, hass_storage, freezer, loc(2026, 11, 2, 7, 15))
    ws = await hass_ws_client(hass, hass_read_only_access_token)
    for i, (t, kw) in enumerate([("manual/set", {"room_id": "wz", "temperature": 20}),
                                 ("profiles/set_active", {"profile_id": None}),
                                 ("schedule/set_day", {"profile_id": "winter", "room_id": "wz",
                                                       "day": "mon", "slots": []})], 1):
        await ws.send_json({"id": i, "type": f"{DOMAIN}/{t}", **kw})
        assert (await ws.receive_json())["error"]["code"] == "unauthorized"
    for i, t in enumerate(("profiles/list", "targets"), 10):
        await ws.send_json({"id": i, "type": f"{DOMAIN}/{t}"})
        assert (await ws.receive_json())["success"]
