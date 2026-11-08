"""v1.4: eigene Meldungstexte (Varianten pro Raum)."""
import copy
import random
from datetime import timedelta
from unittest.mock import MagicMock

import pytest
import voluptuous as vol

from custom_components.smart_heating.models import validate_config
from custom_components.smart_heating.notify import TYPE_CLOSED, TYPE_OPEN, WindowNotifier
from tests.test_stages_coupling import _start, _system_cfg


def _room(**kw):
    return validate_config({"rooms": {"r": {"name": "R", **kw}}})["rooms"]["r"]


# --- Validierung -----------------------------------------------------------------

def test_texts_default_and_normalized():
    r = _room()
    assert r["notify_texts"] == [] and r["notify_close_texts"] == []
    r = _room(notify_texts=["  Hallo  ", "", "   ", "Zwei"], notify_close_texts=[" Zu "])
    assert r["notify_texts"] == ["Hallo", "Zwei"] and r["notify_close_texts"] == ["Zu"]


@pytest.mark.parametrize("texts", [
    ["x"] * 21,
    ["x" * 301],
    "kein Array",
    [5],
])
def test_invalid_texts(texts):
    with pytest.raises(vol.Invalid):
        _room(notify_texts=texts)
    with pytest.raises(vol.Invalid):
        _room(notify_close_texts=texts)


def test_texts_limits_accepted():
    assert len(_room(notify_texts=["x" * 300] * 20)["notify_texts"]) == 20


# --- Auswahl & Platzhalter (Einheit) -------------------------------------------------

def _notifier(texts, close=()):
    cfg = validate_config({"rooms": {"r": {"name": "Bad", "notify_texts": list(texts),
                                           "notify_close_texts": list(close)}}})
    hass = MagicMock()
    hass.states.get.return_value = None
    return WindowNotifier(hass, lambda *_: None), cfg


ROOM_STATE = {"sensors": {"binary_sensor.bad_f": "open"}, "locked": True, "effective_temp": 5.0,
              "desired_temp": 21.5}


def _message(n, cfg, kind=TYPE_OPEN, seconds=600, **kw):
    n.hass.bus.async_fire.reset_mock()
    n._alert(cfg, "r", ROOM_STATE, kind, seconds, notify=False, **kw)
    return n.hass.bus.async_fire.call_args.args[1]["message"]


def test_placeholders_replaced():
    n, cfg = _notifier(["{raum}|{fenster}|{minuten}|{stufe}|{temperatur}|{solltemperatur}"])
    assert _message(n, cfg, stage=2, stages=3) == "Bad|binary_sensor.bad_f|10|2|5,0 °C|21,5 °C"


@pytest.mark.parametrize("text", ["Achtung {unbekannt} offen", "{", "} und {", "{0} {} {raum", "{{raum}}"])
def test_stray_braces_do_not_crash(text):
    n, cfg = _notifier([text])
    msg = _message(n, cfg, stage=1, stages=1)
    assert msg == text.replace("{raum}", "Bad")


def test_action_note_appended_to_open_text_only():
    n, cfg = _notifier(["Zu!"], close=["Wieder zu"])
    msg = _message(n, cfg, stage=1, stages=1, note=" Rollo wird geschlossen.")
    assert msg == "Zu! Rollo wird geschlossen."
    assert _message(n, cfg, TYPE_CLOSED, note=" egal") == "Wieder zu"


def test_default_text_when_no_texts():
    n, cfg = _notifier([])
    msg = _message(n, cfg, stage=1, stages=1)
    assert msg.startswith("🪟 Bad: binary_sensor.bad_f seit 10 Minuten offen.")
    assert _message(n, cfg, TYPE_CLOSED).startswith("✅ Bad: Fenster nach 10 Minuten wieder geschlossen.")


def test_never_same_text_twice_in_a_row(monkeypatch):
    texts = ["A", "B", "C"]
    n, cfg = _notifier(texts, close=["X", "Y"])
    random.seed(1)
    seq = [_message(n, cfg, stage=1, stages=1) for _ in range(30)]
    assert set(seq) == set(texts)
    assert all(a != b for a, b in zip(seq, seq[1:], strict=False))
    closed = [_message(n, cfg, TYPE_CLOSED) for _ in range(10)]
    assert all(a != b for a, b in zip(closed, closed[1:], strict=False))
    # auch bei ungünstigem Zufall: der zuletzt benutzte Index wird nie angeboten
    monkeypatch.setattr(random, "choice", lambda seq_: seq_[0])
    n2, cfg2 = _notifier(texts)
    out = [_message(n2, cfg2, stage=1, stages=1) for _ in range(6)]
    assert all(a != b for a, b in zip(out, out[1:], strict=False))


def test_single_text_repeats():
    n, cfg = _notifier(["Nur einer"])
    assert [_message(n, cfg, stage=1, stages=1) for _ in range(3)] == ["Nur einer"] * 3


# --- Ende-zu-Ende -----------------------------------------------------------------

async def test_end_to_end_custom_texts(hass, hass_storage, freezer):
    data = _system_cfg()
    data["rooms"]["bad"]["notify_stages"] = [{"after": 300}, {"after": 600}, {"after": 900}]
    data["rooms"]["bad"]["notify_texts"] = ["Bad: {fenster} seit {minuten} Min. offen (Stufe {stufe})!",
                                            "Mach das Fenster zu in {raum}, {minuten} Minuten!"]
    data["rooms"]["bad"]["notify_close_texts"] = ["{raum} wieder dicht, jetzt {temperatur}."]
    rig, svc = await _start(hass, hass_storage, freezer, copy.deepcopy(data))
    await rig.window("binary_sensor.bad_f", "on")
    for _ in range(3):
        await rig.tick(timedelta(minutes=5))
    await rig.window("binary_sensor.bad_f", "off")
    texts = [m.data["message"] for m in svc["msgs"]]
    assert len(texts) == 4
    open_texts = texts[:3]
    assert all(t.startswith(("Bad: ", "Mach das Fenster zu in Bad")) for t in open_texts)
    assert all(a != b for a, b in zip(open_texts, open_texts[1:], strict=False))
    assert "5 Min" in open_texts[0] or "5 Minuten" in open_texts[0]
    assert texts[3] == "Bad wieder dicht, jetzt 20,0 °C."
    assert [e.data["message"] for e in svc["events"]] == texts
    assert svc["events"][0].data["type"] == "open_too_long" and svc["events"][3].data["type"] == "closed"
