"""Profil-, Wochenplan- und Handbetriebs-Operationen.

Reine Funktionen auf dem Konfigurations-dict (Kopie rein, Kopie raus).
Gespeichert wird ausschließlich über ConfigStore.async_update.
Die Raum-Profilüberschreibung läuft über rooms.update_room
(Feld profile_override_id) – keine Doppelimplementierung.
"""
from __future__ import annotations

import copy
from datetime import datetime
from typing import Any

from .const import WEEKDAYS
from .models import (
    MANUAL_PERMANENT, OVERRIDE_SCHEMA, PROFILE_SCHEMA, WEEK_SCHEMA,
    _name, new_id, validate_day,
)
from .rooms import RoomError

SEASON_PRESETS = ("Winter", "Frühling", "Sommer", "Herbst")


def _profile(cfg: dict, profile_id: str) -> dict:
    try:
        return cfg["profiles"][profile_id]
    except KeyError:
        raise RoomError("profile_not_found", f"Profil {profile_id!r} existiert nicht") from None


def _room(cfg: dict, room_id: str) -> dict:
    try:
        return cfg["rooms"][room_id]
    except KeyError:
        raise RoomError("room_not_found", f"Raum {room_id!r} existiert nicht") from None


def _check_unique(cfg: dict, name: str, exclude: str | None = None) -> str:
    name = _name(name)
    for pid, p in cfg["profiles"].items():
        if pid != exclude and p["name"].casefold() == name.casefold():
            raise RoomError("duplicate_name", f"Ein Profil {name!r} existiert bereits")
    return name


def _day(day: str) -> str:
    if day not in WEEKDAYS:
        raise RoomError("invalid_day", f"Unbekannter Wochentag {day!r} ({', '.join(WEEKDAYS)})")
    return day


# --- Profile ---------------------------------------------------------------

def create_profile(cfg: dict, name: str, profile_id: str | None = None) -> tuple[dict, str]:
    name = _check_unique(cfg, name)
    profile_id = profile_id or new_id()
    if profile_id in cfg["profiles"]:
        raise RoomError("duplicate_id", f"Profil-ID {profile_id!r} existiert bereits")
    cfg["profiles"][profile_id] = PROFILE_SCHEMA({"name": name})
    return cfg, profile_id


def create_season_presets(cfg: dict) -> tuple[dict, list[str]]:
    """Winter/Frühling/Sommer/Herbst anlegen, sofern noch nicht vorhanden."""
    existing = {p["name"].casefold() for p in cfg["profiles"].values()}
    created = []
    for name in SEASON_PRESETS:
        if name.casefold() not in existing:
            cfg, pid = create_profile(cfg, name)
            created.append(pid)
    return cfg, created


def rename_profile(cfg: dict, profile_id: str, name: str) -> dict:
    _profile(cfg, profile_id)["name"] = _check_unique(cfg, name, exclude=profile_id)
    return cfg


def copy_profile(cfg: dict, source_id: str, name: str) -> tuple[dict, str]:
    source = _profile(cfg, source_id)
    cfg, pid = create_profile(cfg, name)
    cfg["profiles"][pid]["schedules"] = copy.deepcopy(source["schedules"])
    return cfg, pid


def delete_profile(cfg: dict, profile_id: str) -> dict:
    """Aktive oder raumweise verwendete Profile werden nicht gelöscht."""
    _profile(cfg, profile_id)
    if cfg["settings"]["active_profile_id"] == profile_id:
        raise RoomError("profile_in_use", "Profil ist global aktiv – zuerst anderes Profil aktivieren")
    users = [r["name"] for r in cfg["rooms"].values() if r["profile_override_id"] == profile_id]
    if users:
        raise RoomError("profile_in_use", f"Profil wird verwendet von: {', '.join(users)}")
    del cfg["profiles"][profile_id]
    return cfg


def set_active_profile(cfg: dict, profile_id: str | None) -> dict:
    if profile_id is not None:
        _profile(cfg, profile_id)
    cfg["settings"]["active_profile_id"] = profile_id
    return cfg


def effective_profile_id(cfg: dict, room_id: str) -> str | None:
    """Raum-Überschreibung, sonst globales Profil."""
    return _room(cfg, room_id)["profile_override_id"] or cfg["settings"]["active_profile_id"]


# --- Wochenplan ------------------------------------------------------------

def get_week(cfg: dict, profile_id: str, room_id: str) -> dict:
    _room(cfg, room_id)
    week = _profile(cfg, profile_id)["schedules"].get(room_id)
    return copy.deepcopy(week) if week else WEEK_SCHEMA({})


def set_week(cfg: dict, profile_id: str, room_id: str, week: dict[str, Any]) -> dict:
    _room(cfg, room_id)
    for day in week:
        _day(day)
    _profile(cfg, profile_id)["schedules"][room_id] = WEEK_SCHEMA(week)
    return cfg


def set_day(cfg: dict, profile_id: str, room_id: str, day: str, slots: list) -> dict:
    week = get_week(cfg, profile_id, room_id)
    week[_day(day)] = validate_day(slots)
    return set_week(cfg, profile_id, room_id, week)


def copy_day(cfg: dict, profile_id: str, room_id: str, source_day: str, target_days: list[str]) -> dict:
    week = get_week(cfg, profile_id, room_id)
    src = week[_day(source_day)]
    for day in target_days:
        week[_day(day)] = copy.deepcopy(src)
    return set_week(cfg, profile_id, room_id, week)


def copy_room_schedule(cfg: dict, profile_id: str, source_room: str, target_rooms: list[str]) -> dict:
    week = get_week(cfg, profile_id, source_room)
    for rid in target_rooms:
        cfg = set_week(cfg, profile_id, rid, copy.deepcopy(week))
    return cfg


# --- Manueller Sollwert ----------------------------------------------------

def set_manual(
    cfg: dict, room_id: str, temp: float, mode: str, now_utc: datetime, until_utc: datetime | None,
) -> dict:
    """Gewünschte Temperatur manuell setzen.

    until_utc: bei MANUAL_UNTIL_NEXT der nächste Planwechsel (vom Aufrufer
    berechnet). Ohne Zeitplan gibt es keinen Wechsel -> gilt bis zur nächsten
    manuellen Änderung. Hebt eine Fenstersperre nie auf (wird erst danach
    angewendet, siehe targets.py).
    """
    _room(cfg, room_id)
    if mode == MANUAL_PERMANENT:
        until_utc = None
    cfg["overrides"][room_id] = OVERRIDE_SCHEMA({
        "temp": temp, "mode": mode,
        "until": until_utc.isoformat() if until_utc else None,
        "set_at": now_utc.isoformat(),
    })
    return cfg


def clear_manual(cfg: dict, room_id: str) -> dict:
    _room(cfg, room_id)
    cfg["overrides"].pop(room_id, None)
    return cfg


def drop_expired_manual(cfg: dict, now_utc: datetime) -> dict:
    cfg["overrides"] = {
        rid: o for rid, o in cfg["overrides"].items()
        if not o["until"] or datetime.fromisoformat(o["until"]) > now_utc
    }
    return cfg

