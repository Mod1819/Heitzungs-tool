"""Raumverwaltung und Gerätezuordnung.

Reine Funktionen auf dem Konfigurations-dict (ohne HA-Abhängigkeit).
Jede Operation erhält eine Kopie und gibt die geänderte Konfiguration zurück;
gespeichert wird ausschließlich über ConfigStore.async_update.
"""
from __future__ import annotations

from typing import Any

from .models import ROOM_SCHEMA, new_id

KIND_FIELDS = {"thermostat": "thermostats", "window_sensor": "window_sensors"}
ROOM_FIELDS = (
    "name", "thermostats", "window_sensors", "temperature_sensor", "window_open_temp",
    "profile_override_id", "outdoor_mode", "outdoor_curve",
    "window_open_delay", "notify_after", "notify_on_close", "notify_stages", "coupled_sensors",
    "battery_entities",
)


class RoomError(Exception):
    """Fachlicher Fehler mit maschinenlesbarem Code (für WebSocket-Antworten)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _get(cfg: dict, room_id: str) -> dict:
    try:
        return cfg["rooms"][room_id]
    except KeyError:
        raise RoomError("room_not_found", f"Raum {room_id!r} existiert nicht") from None


def _check_name_unique(cfg: dict, name: str, exclude: str | None = None) -> None:
    key = name.strip().casefold()
    for rid, room in cfg["rooms"].items():
        if rid != exclude and room["name"].casefold() == key:
            raise RoomError("duplicate_name", f"Ein Raum mit dem Namen {name.strip()!r} existiert bereits")


def _check_profile(cfg: dict, room: dict) -> None:
    pid = room.get("profile_override_id")
    if pid is not None and pid not in cfg["profiles"]:
        raise RoomError("profile_not_found", f"Profil {pid!r} existiert nicht")


def create_room(cfg: dict, data: dict[str, Any], room_id: str | None = None) -> tuple[dict, str]:
    unknown = set(data) - set(ROOM_FIELDS)
    if unknown:
        raise RoomError("invalid_field", f"Unbekannte Felder: {', '.join(sorted(unknown))}")
    room = ROOM_SCHEMA(dict(data))
    _check_name_unique(cfg, room["name"])
    _check_profile(cfg, room)
    room_id = room_id or new_id()
    if room_id in cfg["rooms"]:
        raise RoomError("duplicate_id", f"Raum-ID {room_id!r} existiert bereits")
    cfg["rooms"][room_id] = room
    return cfg, room_id


def update_room(cfg: dict, room_id: str, changes: dict[str, Any]) -> dict:
    """Teiländerung; nicht übergebene Felder bleiben erhalten."""
    unknown = set(changes) - set(ROOM_FIELDS)
    if unknown:
        raise RoomError("invalid_field", f"Unbekannte Felder: {', '.join(sorted(unknown))}")
    current = _get(cfg, room_id)
    merged = {**current, **changes}
    if "outdoor_curve" in changes:
        merged["outdoor_curve"] = {**current["outdoor_curve"], **changes["outdoor_curve"]}
    room = ROOM_SCHEMA(merged)
    _check_name_unique(cfg, room["name"], exclude=room_id)
    _check_profile(cfg, room)
    cfg["rooms"][room_id] = room
    return cfg


def delete_room(cfg: dict, room_id: str) -> dict:
    _get(cfg, room_id)
    del cfg["rooms"][room_id]
    # Zeitpläne dieses Raums in allen Profilen entfernen
    for profile in cfg["profiles"].values():
        profile["schedules"].pop(room_id, None)
    return cfg


def _field(kind: str) -> str:
    try:
        return KIND_FIELDS[kind]
    except KeyError:
        raise RoomError("invalid_kind", f"Unbekannter Gerätetyp {kind!r}") from None


def assign_entity(cfg: dict, room_id: str, kind: str, entity_id: str) -> dict:
    field = _field(kind)
    room = _get(cfg, room_id)
    if entity_id in room[field]:
        return cfg  # idempotent
    return update_room(cfg, room_id, {field: [*room[field], entity_id]})


def unassign_entity(cfg: dict, room_id: str, kind: str, entity_id: str) -> dict:
    field = _field(kind)
    room = _get(cfg, room_id)
    if entity_id not in room[field]:
        raise RoomError("not_assigned", f"{entity_id} ist {room['name']!r} nicht zugeordnet")
    room[field] = [e for e in room[field] if e != entity_id]
    return cfg


# --- Abfragen für die spätere Regelung (Modul 4) und die Oberfläche -----------

def device_map(cfg: dict) -> dict[str, dict[str, list[str]]]:
    """Entity -> Liste der Räume, getrennt nach Gerätetyp."""
    out: dict[str, dict[str, list[str]]] = {"thermostats": {}, "window_sensors": {}}
    for rid, room in cfg["rooms"].items():
        for field in out:
            for eid in room[field]:
                out[field].setdefault(eid, []).append(rid)
    return out


def rooms_for_thermostat(cfg: dict, entity_id: str) -> list[str]:
    return [rid for rid, r in cfg["rooms"].items() if entity_id in r["thermostats"]]


def windows_for_thermostat(cfg: dict, entity_id: str) -> list[str]:
    """Alle Fensterkontakte aller Räume, denen das Thermostat zugeordnet ist.

    Entscheidung: Ein offenes Fenster in irgendeinem dieser Räume sperrt
    das gemeinsam genutzte Thermostat.
    """
    out: list[str] = []
    for rid in rooms_for_thermostat(cfg, entity_id):
        for eid in cfg["rooms"][rid]["window_sensors"]:
            if eid not in out:
                out.append(eid)
    return out


def thermostats_for_window(cfg: dict, entity_id: str) -> list[str]:
    """Alle Thermostate, die auf diesen Fensterkontakt reagieren müssen."""
    out: list[str] = []
    for room in cfg["rooms"].values():
        if entity_id in room["window_sensors"]:
            for eid in room["thermostats"]:
                if eid not in out:
                    out.append(eid)
    return out


def all_entities(cfg: dict) -> set[str]:
    """Alle referenzierten Entities (für State-Listener in späteren Modulen)."""
    ids: set[str] = set()
    for room in cfg["rooms"].values():
        ids.update(room["thermostats"], room["window_sensors"])
        if room["temperature_sensor"]:
            ids.add(room["temperature_sensor"])
    if cfg["settings"]["outdoor_sensor"]:
        ids.add(cfg["settings"]["outdoor_sensor"])
    return ids
