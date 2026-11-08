"""v1.5: Empfänger (Einstellungen) und Auswahl pro Raum, Meldungen mit eigenen Textvarianten, Testversand."""
import copy
from datetime import timedelta

import pytest
import voluptuous as vol
from homeassistant.core import ServiceCall
from pytest_homeassistant_custom_component.common import async_capture_events

from custom_components.smart_heating.const import DOMAIN
from custom_components.smart_heating.models import validate_config
from tests.test_controller import CFG, Rig, _prepare

ICH = {"id": "ich", "name": "Ich", "service": "notify.telegram_daniel", "target": None}
FRAU = {"id": "frau", "name": "Meine Frau", "service": "telegram_bot.send_message", "target": "-12345"}


# --- Validierung ---------------------------------------------------------------

def _settings(recipients):
    return validate_config({"settings": {"recipients": recipients}})["settings"]["recipients"]


def test_recipient_defaults_and_generated_id():
    rec = _settings([{"name": " Ich ", "service": "notify.telegram_daniel"}])[0]
    assert rec["name"] == "Ich" and rec["target"] is None and len(rec["id"]) == 12
    kept = _settings([{"id": "x", "name": "A", "service": "notify.a", "target": " 42 "}])[0]
    assert kept["id"] == "x" and kept["target"] == "42"


@pytest.mark.parametrize("recipients", [
    [{"name": "Ich", "service": "notify.a"}, {"name": "ich", "service": "notify.b"}],       # Name doppelt
    [{"id": "a", "name": "A", "service": "notify.a"}, {"id": "a", "name": "B", "service": "notify.b"}],
    [{"name": "A", "service": "telegram"}],                                                  # Format
    [{"name": "A", "service": "notify.Telegram"}],
    [{"name": "", "service": "notify.a"}],
    [{"name": "x" * 41, "service": "notify.a"}],
    [{"name": "A", "service": "notify.a", "target": "x" * 65}],
    [{"name": f"E{i}", "service": "notify.a"} for i in range(11)],                          # > 10
])
def test_invalid_recipients(recipients):
    with pytest.raises(vol.Invalid):
        _settings(recipients)


def test_orphan_room_recipients_dropped():
    cfg = validate_config({
        "settings": {"recipients": [ICH]},
        "rooms": {"r": {"name": "R", "notify_recipients": ["ich", "weg", "ich"]}},
    })
    assert cfg["rooms"]["r"]["notify_recipients"] == ["ich"]


def test_stage_texts_validated_and_close_default_off():
    room = validate_config({"rooms": {"r": {"name": "R", "notify_stages": [
        {"after": 300, "texts": ["  Hallo  ", "", "Zwei"]}]}}})["rooms"]["r"]
    assert room["notify_stages"][0]["texts"] == ["Hallo", "Zwei"]
    assert room["notify_on_close"] is False
    with pytest.raises(vol.Invalid):
        validate_config({"rooms": {"r": {"name": "R", "notify_stages": [
            {"after": 300, "texts": ["x" * 301]}]}}})


# --- Versand -------------------------------------------------------------------

def _data(room_recipients=("ich", "frau"), texts=("Variante A in {raum}", "Variante B in {raum}")):
    data = copy.deepcopy(CFG)
    data["settings"]["recipients"] = [copy.deepcopy(ICH), copy.deepcopy(FRAU)]
    data["settings"]["notify_service"] = "notify.legacy"
    wz = data["rooms"]["wz"]
    wz["notify_recipients"] = list(room_recipients)
    wz["notify_stages"] = [{"after": 600, "texts": list(texts)}]
    return data


async def _start(hass, hass_storage, freezer, data=None, failing=()):
    calls, entry = await _prepare(hass, hass_storage, freezer, data or _data())
    sent: dict[str, list] = {}

    def mock(domain, name):
        full = f"{domain}.{name}"
        sent[full] = []

        async def handler(call: ServiceCall) -> None:
            if full in failing:
                raise RuntimeError("kaputt")
            sent[full].append(call)

        hass.services.async_register(domain, name, handler)

    for domain, name in (("notify", "telegram_daniel"), ("telegram_bot", "send_message"),
                         ("notify", "legacy"), ("notify", "send_message")):
        mock(domain, name)
    events = async_capture_events(hass, "smart_heating_window_alert")
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    rig = Rig(hass, freezer, calls, entry)
    rig.new_calls()
    await rig.ack()
    return rig, sent, events


async def _open_wz(rig):
    await rig.window("binary_sensor.wz_f1", "on")
    await rig.tick(timedelta(minutes=10))


async def test_send_to_two_recipients_with_stage_text(hass, hass_storage, freezer):
    rig, sent, events = await _start(hass, hass_storage, freezer)
    await _open_wz(rig)
    (a,) = sent["notify.telegram_daniel"]
    (b,) = sent["telegram_bot.send_message"]
    assert a.data["message"] == b.data["message"] and a.data["message"].startswith("Variante ")
    assert "Wohnzimmer" in a.data["message"] and a.data["title"] == "Heizung"
    assert "target" not in a.data and "entity_id" not in a.data
    assert b.data["target"] == -12345 and isinstance(b.data["target"], int)
    assert sent["notify.legacy"] == []                       # Empfänger ersetzen den alten Dienst
    assert events[0].data["recipients"] == ["Ich", "Meine Frau"]


async def test_stage_texts_never_twice_in_a_row(hass, hass_storage, freezer):
    data = _data()
    data["rooms"]["wz"]["notify_stages"] = [{"after": 60 * (i + 1), "texts": ["A", "B"]} for i in range(1)]
    rig, sent, _ = await _start(hass, hass_storage, freezer, data)
    notifier = rig.hass.config_entries.async_loaded_entries(DOMAIN)[0].runtime_data.controller.notifier
    cfg = rig.hass.config_entries.async_loaded_entries(DOMAIN)[0].runtime_data.store.data
    out = [notifier._custom_text(cfg, "wz", "open_too_long", {}, 0) for _ in range(12)]
    assert all(a != b for a, b in zip(out, out[1:], strict=False))


async def test_room_texts_used_when_stage_has_none(hass, hass_storage, freezer):
    data = _data(texts=())
    data["rooms"]["wz"]["notify_texts"] = ["Raumtext {minuten}"]
    rig, sent, _ = await _start(hass, hass_storage, freezer, data)
    await _open_wz(rig)
    assert sent["notify.telegram_daniel"][0].data["message"] == "Raumtext 10"


async def test_notify_entity_target_for_send_message(hass, hass_storage, freezer):
    data = _data()
    data["settings"]["recipients"] = [
        {"id": "ich", "name": "Ich", "service": "notify.send_message", "target": "notify.daniel"}]
    data["rooms"]["wz"]["notify_recipients"] = ["ich"]
    rig, sent, _ = await _start(hass, hass_storage, freezer, data)
    await _open_wz(rig)
    assert sent["notify.send_message"][0].data["entity_id"] == "notify.daniel"


async def test_one_failing_recipient_does_not_block_other(hass, hass_storage, freezer):
    rig, sent, _ = await _start(hass, hass_storage, freezer, failing=("notify.telegram_daniel",))
    await _open_wz(rig)
    assert sent["notify.telegram_daniel"] == [] and len(sent["telegram_bot.send_message"]) == 1


async def test_legacy_fallback_without_room_recipients(hass, hass_storage, freezer):
    rig, sent, events = await _start(hass, hass_storage, freezer, _data(room_recipients=()))
    await _open_wz(rig)
    assert len(sent["notify.legacy"]) == 1
    assert sent["notify.telegram_daniel"] == [] and sent["telegram_bot.send_message"] == []
    assert events[0].data["recipients"] == []


async def test_stage_without_notify_flag_sends_nothing(hass, hass_storage, freezer):
    data = _data()
    data["rooms"]["wz"]["notify_stages"] = [{"after": 600, "notify": False, "texts": ["X"]}]
    rig, sent, events = await _start(hass, hass_storage, freezer, data)
    await _open_wz(rig)
    assert len(events) == 1 and not any(sent.values())


# --- WebSocket -------------------------------------------------------------------

@pytest.mark.freeze_time("2026-11-02T06:15:00+00:00")
async def test_ws_recipients_and_tests(hass, hass_storage, freezer, hass_ws_client):
    data = _data()
    data["rooms"]["wz"]["notify_stages"] = [{"after": 300, "texts": ["Test in {raum}: {minuten} Min"]}]
    rig, sent, _ = await _start(hass, hass_storage, freezer, data)
    ws = await hass_ws_client(hass)
    n = 0

    async def call(t, **kw):
        nonlocal n
        n += 1
        await ws.send_json({"id": n, "type": f"{DOMAIN}/{t}", **kw})
        r = await ws.receive_json()
        await hass.async_block_till_done()
        return r

    # Empfänger speichern: unbekannter Dienst -> Fehler, bekannter -> IDs bleiben, neue werden erzeugt
    r = await call("settings/update", recipients=[{"name": "X", "service": "notify.gibtsnicht"}])
    assert r["error"]["code"] == "service_not_found" and "notify.gibtsnicht" in r["error"]["message"]
    r = await call("settings/update", recipients=[ICH, FRAU, {"name": "Neu", "service": "notify.legacy"}])
    assert r["success"]
    recs = r["result"]["recipients"]
    assert [x["id"] for x in recs[:2]] == ["ich", "frau"] and len(recs[2]["id"]) == 12

    # Test an einen Empfänger
    r = await call("notify/test", recipient_id="frau")
    assert r["success"] and r["result"] == {"sent": 1}
    last = sent["telegram_bot.send_message"][-1]
    assert last.data["message"] == "🔔 Testnachricht von Smart Heating Control an Meine Frau"
    assert last.data["target"] == -12345
    assert (await call("notify/test", recipient_id="nix"))["error"]["code"] == "recipient_not_found"

    # Test einer Raum-Meldung
    sent["notify.telegram_daniel"].clear()
    r = await call("notify/test", room_id="wz", stage=0)
    assert r["success"] and r["result"] == {"sent": 2}
    msg = sent["notify.telegram_daniel"][0].data["message"]
    assert msg.startswith("🔔 Test: ") and "Wohnzimmer" in msg and "5 Min" in msg
    assert (await call("notify/test", room_id="gibtsnicht"))["error"]["code"] == "room_not_found"

    # Standardtext, wenn die Meldung keine Texte hat
    r = await call("rooms/update", room_id="wz", notify_stages=[{"after": 900}])
    assert r["success"]
    assert (await call("notify/test", room_id="wz", stage=0))["success"]
    assert "15 Minuten" in sent["notify.telegram_daniel"][-1].data["message"]

    # Ohne Empfänger -> alter Dienst; ohne beides -> Fehler
    assert (await call("rooms/update", room_id="wz", notify_recipients=[]))["success"]
    assert (await call("notify/test", room_id="wz", stage=0))["result"] == {"sent": 1}
    assert sent["notify.legacy"][-1].data["message"].startswith("🔔 Test: ")
    assert (await call("settings/update", notify_service=None))["success"]
    assert (await call("notify/test", room_id="wz"))["error"]["code"] == "no_recipient"

    # Löschen eines Empfängers entfernt ihn aus den Räumen
    assert (await call("rooms/update", room_id="wz", notify_recipients=["ich", "frau"]))["success"]
    r = await call("settings/update", recipients=[ICH])
    assert r["success"]
    assert rig.hass.config_entries.async_loaded_entries(DOMAIN)[0].runtime_data.store.data[
        "rooms"]["wz"]["notify_recipients"] == ["ich"]
