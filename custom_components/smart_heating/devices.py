"""Geräte-Hilfen: Batteriezustand und Plausibilität von Fensterkontakten.

Batterie: Für jeden Raum werden die Geräte der eingetragenen Thermostate und
Fensterkontakte geprüft, zusätzlich alle Geräte mit Batterie im selben HA-Bereich
(z. B. die Heizkörperthermostate einer Homematic-IP-Heizgruppe, die selbst keine
Batterie hat). Niedrig = binary_sensor (device_class battery) "on" oder
Batterie-Prozentwert unter LOW_PERCENT.
"""
from __future__ import annotations

from typing import Any

from homeassistant.const import STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

LOW_PERCENT = 20

# device_class-Werte, die sicher kein Fenster/keine Tür sind (z. B. Batterie-Warnung)
NOT_A_WINDOW = frozenset({
    "battery", "battery_charging", "carbon_monoxide", "cold", "connectivity", "gas", "heat",
    "light", "moisture", "motion", "moving", "occupancy", "plug", "power", "presence",
    "problem", "running", "safety", "smoke", "sound", "tamper", "update", "vibration",
})


def device_class(hass: HomeAssistant, entity_id: str) -> str | None:
    state = hass.states.get(entity_id)
    if state is not None and state.attributes.get("device_class"):
        return state.attributes["device_class"]
    entry = er.async_get(hass).async_get(entity_id)
    if entry is not None:
        return entry.device_class or entry.original_device_class
    return None


def wrong_window_sensors(hass: HomeAssistant, entity_ids: list[str]) -> list[str]:
    """binary_sensor-Entitäten, die laut device_class kein Fenster-/Türkontakt sind."""
    return [
        e for e in entity_ids
        if e.startswith("binary_sensor.") and device_class(hass, e) in NOT_A_WINDOW
    ]


def _battery_entities(hass: HomeAssistant, device_id: str) -> list[str]:
    reg = er.async_get(hass)
    out = []
    for entry in er.async_entries_for_device(reg, device_id):
        if entry.domain in ("sensor", "binary_sensor") and \
                (entry.device_class or entry.original_device_class) == "battery":
            out.append(entry.entity_id)
    return out


def _battery_state(hass: HomeAssistant, entity_id: str) -> tuple[bool, Any] | None:
    st = hass.states.get(entity_id)
    if st is None or st.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return None
    if entity_id.startswith("binary_sensor."):
        return st.state == STATE_ON, "niedrig" if st.state == STATE_ON else "ok"
    try:
        level = float(st.state)
    except ValueError:
        return None
    return level < LOW_PERCENT, round(level)


def _attr_battery(hass: HomeAssistant, entity_id: str) -> tuple[bool, Any] | None:
    """Batterieinfo aus den Attributen der Entität selbst (low_battery / battery_level ...)."""
    st = hass.states.get(entity_id)
    if st is None:
        return None
    attrs = st.attributes
    for key in ("low_battery", "battery_low"):
        if isinstance(attrs.get(key), bool):
            return attrs[key], "niedrig" if attrs[key] else "ok"
    for key in ("battery_level", "battery"):
        val = attrs.get(key)
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            return val < LOW_PERCENT, round(val)
    return None


def _friendly(hass: HomeAssistant, entity_id: str) -> str:
    st = hass.states.get(entity_id)
    return st.attributes.get("friendly_name") or entity_id if st else entity_id


def battery_report(hass: HomeAssistant, cfg: dict) -> list[dict[str, Any]]:
    """Batteriezustand aller relevanten Geräte, je Eintrag mit Räumen.

    Quellen: manuell zugeordnete Entitäten (source=manual), Geräte der Thermostate/
    Fensterkontakte bzw. im selben Bereich (source=device) und Batterie-Attribute der
    Thermostate/Fensterkontakte selbst (source=attribute).
    """
    ent_reg, dev_reg = er.async_get(hass), dr.async_get(hass)
    devices: dict[str, set[str]] = {}  # device_id -> room_ids
    manual: dict[str, set[str]] = {}   # entity_id -> room_ids
    attr: dict[str, set[str]] = {}     # entity_id -> room_ids
    for rid, room in cfg["rooms"].items():
        areas: set[str] = set()
        for eid in room.get("battery_entities", []):
            manual.setdefault(eid, set()).add(rid)
        for eid in [*room["thermostats"], *room["window_sensors"]]:
            attr.setdefault(eid, set()).add(rid)
            entry = ent_reg.async_get(eid)
            if entry is None or entry.device_id is None:
                continue
            devices.setdefault(entry.device_id, set()).add(rid)
            device = dev_reg.async_get(entry.device_id)
            area = entry.area_id or (device.area_id if device else None)
            if area:
                areas.add(area)
        for area in areas:
            for device in dr.async_entries_for_area(dev_reg, area):
                if _battery_entities(hass, device.id):
                    devices.setdefault(device.id, set()).add(rid)
    report: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(key: str, eid: str, name: str, rooms: set[str], state, source: str) -> None:
        if key in seen or state is None:
            return
        seen.add(key)
        low, level = state
        report.append({
            "entity_id": eid, "device": name, "rooms": sorted(rooms),
            "level": level, "low": low, "source": source,
        })

    for eid, rooms in manual.items():
        entry = ent_reg.async_get(eid)
        device = dev_reg.async_get(entry.device_id) if entry and entry.device_id else None
        name = (device.name_by_user or device.name) if device else _friendly(hass, eid)
        add(eid, eid, name or eid, rooms, _battery_state(hass, eid), "manual")
    for device_id, rooms in devices.items():
        device = dev_reg.async_get(device_id)
        for eid in _battery_entities(hass, device_id):
            name = (device.name_by_user or device.name) if device else eid
            add(eid, eid, name or eid, rooms, _battery_state(hass, eid), "device")
    for eid, rooms in attr.items():
        add(f"{eid}#attr", eid, _friendly(hass, eid), rooms, _attr_battery(hass, eid), "attribute")
    report.sort(key=lambda b: (not b["low"], b["device"] or ""))
    return report
