"""Fenstersperre und Thermostat-Zielwerte – reine Logik."""
from custom_components.smart_heating.models import validate_config
from custom_components.smart_heating.targets import resolve_effective, window_state

CFG = validate_config({"rooms": {
    "wz": {"name": "WZ", "thermostats": ["climate.wz1", "climate.wz2"],
           "window_sensors": ["binary_sensor.f1", "binary_sensor.terrasse"]},
    "ez": {"name": "EZ", "thermostats": ["climate.ez", "climate.flur"],
           "window_sensors": ["binary_sensor.terrasse"], "window_open_temp": 6},
    "flur": {"name": "Flur", "thermostats": ["climate.flur"], "window_sensors": ["binary_sensor.tuer"]},
    "bad": {"name": "Bad", "thermostats": ["climate.bad"]},
}})
D = {r: {"desired_temp": t, "source": "schedule"} for r, t in
     (("wz", 21), ("ez", 20), ("flur", 18), ("bad", 23))}
CLOSED = {e: "closed" for e in ("binary_sensor.f1", "binary_sensor.terrasse", "binary_sensor.tuer")}


def test_window_state_mapping():
    assert window_state("on") == "open" and window_state("off") == "closed"
    for raw in ("unavailable", "unknown", None, "foo"):
        assert window_state(raw) == "unknown"


def test_all_closed_shared_thermostat_takes_lowest():
    r = resolve_effective(CFG, D, CLOSED)
    th = r["thermostats"]
    assert th["climate.wz1"]["target"] == th["climate.wz2"]["target"] == 21
    assert th["climate.flur"]["target"] == 18 and th["climate.flur"]["decisive_room"] == "flur"
    assert not any(t["locked"] for t in th.values())


def test_any_window_locks_room_all_must_close():
    r = resolve_effective(CFG, D, {**CLOSED, "binary_sensor.f1": "open", "binary_sensor.terrasse": "open"})
    assert r["rooms"]["wz"]["effective_temp"] == 5 and r["rooms"]["wz"]["desired_temp"] == 21
    r = resolve_effective(CFG, D, {**CLOSED, "binary_sensor.terrasse": "open"})
    assert r["rooms"]["wz"]["locked"]  # ein Fenster noch offen


def test_shared_window_and_shared_thermostat():
    r = resolve_effective(CFG, D, {**CLOSED, "binary_sensor.terrasse": "open"})
    th = r["thermostats"]
    assert th["climate.wz1"]["target"] == 5 and th["climate.ez"]["target"] == 6
    # climate.flur gehört zu EZ (Fenster offen) und Flur (zu) -> Sperre greift
    assert th["climate.flur"]["target"] == 6 and th["climate.flur"]["locked"]
    assert r["rooms"]["flur"]["effective_temp"] == 18  # Flur-Sollwert bleibt getrennt
    assert th["climate.bad"]["target"] == 23


def test_unknown_policy():
    w = {**CLOSED, "binary_sensor.tuer": "unknown"}
    r = resolve_effective(CFG, D, w)
    assert r["rooms"]["flur"]["locked"] and r["rooms"]["flur"]["reason"] == "window_unknown"
    assert r["rooms"]["flur"]["window"] == "unknown"
    cfg = validate_config({**CFG, "settings": {**CFG["settings"], "unknown_window_policy": "closed"}})
    r = resolve_effective(cfg, D, w)
    assert not r["rooms"]["flur"]["locked"] and r["rooms"]["flur"]["window"] == "unknown"
    # fehlender Zustand = unbekannt
    assert resolve_effective(CFG, D, {})["rooms"]["wz"]["locked"]


def test_lock_never_raises_temperature():
    d = {**D, "wz": {"desired_temp": 4.5, "source": "manual"}}
    r = resolve_effective(CFG, d, {**CLOSED, "binary_sensor.f1": "open"})
    assert r["rooms"]["wz"]["effective_temp"] == 4.5
