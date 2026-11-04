"""Öffnungsverzögerung pro Raum und Benachrichtigung bei zu lange offenem Fenster."""
import copy
from datetime import UTC, datetime, timedelta

import pytest
import voluptuous as vol
from pytest_homeassistant_custom_component.common import async_capture_events, async_mock_service

from custom_components.smart_heating.const import DOMAIN
from custom_components.smart_heating.models import validate_config
from custom_components.smart_heating.targets import resolve_effective
from tests.test_controller import CFG, Rig, _prepare

T0 = datetime(2026, 11, 2, 6, 15, tzinfo=UTC)


# --- reine Logik ---------------------------------------------------------------

def _cfg(delay_wz=300, delay_ez=0):
    c = copy.deepcopy(CFG)
    c["rooms"]["wz"]["window_open_delay"] = delay_wz
    c["rooms"]["ez"]["window_open_delay"] = delay_ez
    return validate_config(c)


D = {r: {"desired_temp": 20, "source": "schedule"} for r in ("wz", "ez", "flur")}
CLOSED = {"binary_sensor.wz_f1": "closed", "binary_sensor.terrasse": "closed", "binary_sensor.tuer": "closed"}


def test_delay_pending_then_locked():
    w = {**CLOSED, "binary_sensor.wz_f1": "open"}
    since = {"binary_sensor.wz_f1": T0}
    r = resolve_effective(_cfg(), D, w, None, since, T0 + timedelta(seconds=299))["rooms"]["wz"]
    assert not r["locked"] and r["effective_temp"] == 20 and r["window"] == "open"
    assert r["lock_pending_until"] == (T0 + timedelta(seconds=300)).isoformat()
    r = resolve_effective(_cfg(), D, w, None, since, T0 + timedelta(seconds=300))["rooms"]["wz"]
    assert r["locked"] and r["effective_temp"] == 5 and r["lock_pending_until"] is None


def test_shared_window_rooms_with_different_delays():
    w = {**CLOSED, "binary_sensor.terrasse": "open"}
    res = resolve_effective(_cfg(), D, w, None, {"binary_sensor.terrasse": T0}, T0 + timedelta(seconds=10))
    assert res["rooms"]["ez"]["locked"] and not res["rooms"]["wz"]["locked"]
    # climate.flur hängt an EZ (sofort) -> gesperrt; climate.wz1 wartet noch
    assert res["thermostats"]["climate.flur"]["target"] == 6
    assert res["thermostats"]["climate.wz1"]["target"] == 20


def test_unknown_contact_ignores_delay():
    w = {**CLOSED, "binary_sensor.wz_f1": "unknown"}
    r = resolve_effective(_cfg(), D, w, None, {}, T0)["rooms"]["wz"]
    assert r["locked"] and r["reason"] == "window_unknown"


def test_second_window_already_due_locks():
    w = {**CLOSED, "binary_sensor.wz_f1": "open", "binary_sensor.terrasse": "open"}
    since = {"binary_sensor.wz_f1": T0 + timedelta(seconds=290), "binary_sensor.terrasse": T0}
    r = resolve_effective(_cfg(), D, w, None, since, T0 + timedelta(seconds=300))["rooms"]["wz"]
    assert r["locked"] and r["open_since"] == T0.isoformat()


def test_without_timing_info_no_delay():
    r = resolve_effective(_cfg(), D, {**CLOSED, "binary_sensor.wz_f1": "open"})["rooms"]["wz"]
    assert r["locked"]


@pytest.mark.parametrize("field, value", [("window_open_delay", -1), ("window_open_delay", 3601),
                                          ("notify_after", -5), ("notify_on_close", "ja")])
def test_room_field_validation(field, value):
    with pytest.raises(vol.Invalid):
        validate_config({"rooms": {"r": {"name": "R", field: value}}})


@pytest.mark.parametrize("value", ["telegram", "notify telegram", "notify.Telegram", 5])
def test_notify_service_validation(value):
    with pytest.raises(vol.Invalid):
        validate_config({"settings": {"notify_service": value}})


# --- im laufenden System -------------------------------------------------------

def _data(**rooms_extra):
    data = copy.deepcopy(CFG)
    data["rooms"]["wz"].update({"window_open_delay": 300, "notify_after": 600})
    data["settings"]["notify_service"] = "notify.telegram"
    for rid, extra in rooms_extra.items():
        data["rooms"][rid].update(extra)
    return data


async def _start(hass, hass_storage, freezer, data=None, service=True):
    calls, entry = await _prepare(hass, hass_storage, freezer, data or _data())
    msgs = async_mock_service(hass, "notify", "telegram") if service else []
    events = async_capture_events(hass, "smart_heating_window_alert")
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    rig = Rig(hass, freezer, calls, entry)
    rig.new_calls()
    await rig.ack()
    return rig, msgs, events


async def test_short_opening_no_regulation(hass, hass_storage, freezer):
    rig, msgs, events = await _start(hass, hass_storage, freezer)
    await rig.window("binary_sensor.wz_f1", "on")
    assert rig.new_calls() == {} and rig.room("wz")["lock_pending_until"]
    await rig.tick(timedelta(minutes=3))
    await rig.window("binary_sensor.wz_f1", "off")
    await rig.tick(timedelta(minutes=10))
    assert rig.new_calls() == {} and msgs == [] and events == []
    assert rig.entry.runtime_data.controller.next_wakeup is None  # kein Timer mehr nötig


async def test_long_opening_locks_after_delay_and_restores(hass, hass_storage, freezer):
    rig, _msgs, _ = await _start(hass, hass_storage, freezer)
    await rig.window("binary_sensor.wz_f1", "on")
    await rig.tick(timedelta(seconds=299))
    assert rig.new_calls() == {}
    await rig.tick(timedelta(seconds=1))
    assert rig.new_calls() == {"climate.wz1": 5, "climate.wz2": 5}
    await rig.ack()
    await rig.window("binary_sensor.wz_f1", "off")
    assert rig.new_calls() == {"climate.wz1": 21, "climate.wz2": 21}


async def test_shared_terrace_door_delay_per_room(hass, hass_storage, freezer):
    rig, _, _ = await _start(hass, hass_storage, freezer)
    await rig.window("binary_sensor.terrasse", "on")
    assert rig.new_calls() == {"climate.ez": 6, "climate.flur": 6}  # EZ ohne Verzögerung
    await rig.ack()
    await rig.tick(timedelta(minutes=5))
    assert rig.new_calls() == {"climate.wz1": 5, "climate.wz2": 5}


async def test_manual_during_pending_delay_is_normal_manual(hass, hass_storage, freezer):
    rig, _, _ = await _start(hass, hass_storage, freezer)
    await rig.window("binary_sensor.wz_f1", "on")
    await rig.device("climate.wz1", 23)
    assert rig.new_calls() == {"climate.wz2": 23}
    await rig.ack()
    await rig.tick(timedelta(minutes=5))
    assert rig.new_calls() == {"climate.wz1": 5, "climate.wz2": 5}  # Sperre greift trotzdem
    await rig.ack()
    await rig.window("binary_sensor.wz_f1", "off")
    assert rig.new_calls() == {"climate.wz1": 23, "climate.wz2": 23}


async def test_notification_once_then_close(hass, hass_storage, freezer):
    rig, msgs, events = await _start(hass, hass_storage, freezer)
    await rig.window("binary_sensor.wz_f1", "on")
    await rig.tick(timedelta(minutes=9, seconds=59))
    assert msgs == []
    await rig.tick(timedelta(seconds=1))
    assert len(msgs) == 1 and len(events) == 1
    assert msgs[0].data["title"] == "Heizung"
    assert "Wohnzimmer" in msgs[0].data["message"] and "10 Minuten" in msgs[0].data["message"]
    assert "abgesenkt" in msgs[0].data["message"]
    ev = events[0].data
    assert ev["type"] == "open_too_long" and ev["room_id"] == "wz"
    assert ev["open_sensors"] == ["binary_sensor.wz_f1"]
    await rig.window("binary_sensor.terrasse", "on")     # weiteres Fenster -> keine zweite Meldung
    await rig.window("binary_sensor.wz_f1", "off")
    await rig.tick(timedelta(minutes=30))
    assert len(msgs) == 1
    await rig.window("binary_sensor.terrasse", "off")
    assert len(msgs) == 2 and events[-1].data["type"] == "closed"
    assert "wieder geschlossen" in msgs[1].data["message"]
    await rig.window("binary_sensor.wz_f1", "on")        # neuer Öffnungsvorgang -> neue Meldung
    await rig.tick(timedelta(minutes=10))
    assert len(msgs) == 3


async def test_unavailable_contact_no_false_all_clear(hass, hass_storage, freezer):
    rig, msgs, _ = await _start(hass, hass_storage, freezer)
    await rig.window("binary_sensor.wz_f1", "on")
    await rig.tick(timedelta(minutes=10))
    assert len(msgs) == 1
    await rig.window("binary_sensor.wz_f1", "unavailable")
    assert len(msgs) == 1                                 # keine Entwarnung
    await rig.window("binary_sensor.wz_f1", "off")
    assert len(msgs) == 2


async def test_notify_on_close_disabled(hass, hass_storage, freezer):
    rig, msgs, events = await _start(hass, hass_storage, freezer, _data(wz={"notify_on_close": False}))
    await rig.window("binary_sensor.wz_f1", "on")
    await rig.tick(timedelta(minutes=10))
    await rig.window("binary_sensor.wz_f1", "off")
    assert len(msgs) == 1 and len(events) == 1


async def test_event_only_without_service(hass, hass_storage, freezer):
    data = _data()
    data["settings"]["notify_service"] = None
    rig, _, events = await _start(hass, hass_storage, freezer, data, service=False)
    await rig.window("binary_sensor.wz_f1", "on")
    await rig.tick(timedelta(minutes=10))
    assert len(events) == 1 and events[0].data["message"].startswith("🪟 Wohnzimmer")


async def test_notify_entity_and_failing_service(hass, hass_storage, freezer):
    data = _data()
    data["settings"].update(
        {"notify_service": "notify.send_message", "notify_entity_id": "notify.telegram_daniel"})
    calls, entry = await _prepare(hass, hass_storage, freezer, data)
    got = []

    async def broken(call):
        got.append(call.data)
        raise RuntimeError("Telegram nicht erreichbar")

    hass.services.async_register("notify", "send_message", broken)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    rig = Rig(hass, freezer, calls, entry)
    await rig.ack()
    await rig.window("binary_sensor.wz_f1", "on")
    await rig.tick(timedelta(minutes=10))
    assert got[0]["entity_id"] == "notify.telegram_daniel"
    actions = rig.entry.runtime_data.controller.status()["actions"]["wz"]
    assert any("fehlgeschlagen" in a["message"] for a in actions)
    assert rig.thermo("climate.wz1")["locked"] is True       # Regelung läuft weiter


async def test_room_removed_clears_notification_state(hass, hass_storage, freezer):
    rig, msgs, _ = await _start(hass, hass_storage, freezer)
    await rig.window("binary_sensor.wz_f1", "on")
    await rig.tick(timedelta(minutes=10))
    from custom_components.smart_heating.rooms import delete_room
    await rig.entry.runtime_data.store.async_update(lambda c: delete_room(c, "wz"))
    await rig.hass.async_block_till_done()
    assert "wz" not in rig.entry.runtime_data.controller.notifier.sent


@pytest.mark.freeze_time("2026-11-02T06:15:00+00:00")
async def test_api_room_fields_settings_and_test_message(hass, hass_storage, freezer, hass_ws_client):
    rig, msgs, _ = await _start(hass, hass_storage, freezer)
    ws = await hass_ws_client(hass)
    n = 0

    async def call(t, **kw):
        nonlocal n
        n += 1
        await ws.send_json({"id": n, "type": f"{DOMAIN}/{t}", **kw})
        r = await ws.receive_json()
        await hass.async_block_till_done()
        return r

    r = await call("rooms/update", room_id="ez", window_open_delay=600, notify_after=1200,
                   notify_on_close=False)
    assert r["success"] and r["result"]["window_open_delay"] == 600
    # v1.2: einzelne Meldezeit wird als erste Meldestufe gespeichert
    assert r["result"]["notify_stages"][0]["after"] == 1200 and r["result"]["notify_after"] == 0
    r = await call("rooms/update", room_id="ez", window_open_delay=99999)
    assert r["error"]["code"] == "invalid_format"
    r = await call("settings/update", notify_service="notify.gibtsnicht")
    assert r["error"]["code"] == "service_not_found"
    r = await call("settings/update", notify_entity_id="notify.unbekannt")
    assert r["error"]["code"] == "entity_not_found"
    assert (await call("notify/test"))["success"]
    assert msgs[-1].data["message"].startswith("🔔")
    assert (await call("settings/update", notify_service=None))["success"]
    assert (await call("notify/test"))["error"]["code"] == "no_service"
