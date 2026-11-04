"""Datenmodell und Validierung.

Reines Python + voluptuous, keine HA-Laufzeitabhängigkeit -> isoliert testbar.
Die gesamte Konfiguration ist ein JSON-fähiges dict:

{
  "settings": {...},
  "rooms":    {room_id: {...}},
  "profiles": {profile_id: {"name": str, "schedules": {room_id: {weekday: [slot]}}}}
}
"""
from __future__ import annotations

import copy
import re
import uuid
from datetime import datetime
from typing import Any

import voluptuous as vol

from .const import (
    ABS_MAX_TEMP,
    ABS_MIN_TEMP,
    DEFAULT_COMFORT_TEMP,
    DEFAULT_OUTDOOR_FULL,
    DEFAULT_OUTDOOR_MAX_REDUCTION,
    DEFAULT_OUTDOOR_START,
    DEFAULT_WINDOW_OPEN_TEMP,
    OUTDOOR_MODE_GLOBAL,
    OUTDOOR_MODES,
    UNKNOWN_AS_OPEN,
    UNKNOWN_POLICIES,
    WEEKDAYS,
)

_ENTITY_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$|^24:00$")


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def _entity_id(domains: tuple[str, ...]):
    def validator(value: Any) -> str:
        if not isinstance(value, str) or not _ENTITY_RE.match(value):
            raise vol.Invalid(f"Ungültige Entity-ID: {value!r}")
        if value.split(".", 1)[0] not in domains:
            raise vol.Invalid(f"{value} muss zur Domain {', '.join(domains)} gehören")
        return value
    return validator


def _unique_list(validator):
    def validate(value: Any) -> list:
        if not isinstance(value, list):
            raise vol.Invalid("Liste erwartet")
        out: list = []
        for item in value:
            item = validator(item)
            if item not in out:
                out.append(item)
        return out
    return validate


def _name(value: Any) -> str:
    value = vol.Coerce(str)(value).strip()
    if not value:
        raise vol.Invalid("Name darf nicht leer sein")
    if len(value) > 64:
        raise vol.Invalid("Name zu lang (max. 64 Zeichen)")
    return value


TEMP = vol.All(vol.Coerce(float), vol.Range(min=ABS_MIN_TEMP, max=ABS_MAX_TEMP))
OUTDOOR_TEMP = vol.All(vol.Coerce(float), vol.Range(min=-40, max=50))
REDUCTION = vol.All(vol.Coerce(float), vol.Range(min=0, max=15))

CLIMATE_ID = _entity_id(("climate",))
WINDOW_ID = _entity_id(("binary_sensor", "input_boolean"))
TEMP_SENSOR_ID = _entity_id(("sensor",))
NOTIFY_ENTITY_ID = _entity_id(("notify",))
BATTERY_ID = _entity_id(("sensor", "binary_sensor"))
_SERVICE_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")


def _service(value: Any) -> str:
    """Dienst im Format domain.service, z. B. notify.telegram oder telegram_bot.send_message."""
    if not isinstance(value, str) or not _SERVICE_RE.match(value):
        raise vol.Invalid(f"Ungültiger Dienst: {value!r} (Format domain.service)")
    return value


DELAY_SECONDS = vol.All(vol.Coerce(int), vol.Range(min=0, max=3600))       # bis 60 min
COUPLED_DELAY = vol.All(vol.Coerce(int), vol.Range(min=0, max=4 * 3600))   # bis 4 h
ACTION_ID = _entity_id(("cover", "script", "scene"))
MAX_STAGES = 10

# Meldestufe: nach `after` Sekunden Nachricht (notify) und/oder Aktion
# (Rollo schließen/auf Position, Skript, Szene). Nichts wird automatisch zurückgefahren.
STAGE_SCHEMA = vol.Schema(
    {
        vol.Required("after"): vol.All(vol.Coerce(int), vol.Range(min=60, max=24 * 3600)),
        vol.Optional("notify", default=True): bool,
        vol.Optional("action_entity", default=None): vol.Any(None, ACTION_ID),
        vol.Optional("cover_position", default=None): vol.Any(
            None, vol.All(vol.Coerce(int), vol.Range(min=0, max=100))),
    }
)


def _stages(value: Any) -> list[dict]:
    if not isinstance(value, list):
        raise vol.Invalid("Liste von Meldestufen erwartet")
    if len(value) > MAX_STAGES:
        raise vol.Invalid(f"Höchstens {MAX_STAGES} Meldestufen")
    stages = sorted((STAGE_SCHEMA(v) for v in value), key=lambda st: st["after"])
    for a, b in zip(stages, stages[1:], strict=False):
        if a["after"] == b["after"]:
            raise vol.Invalid("Zwei Meldestufen mit derselben Zeit")
    for st in stages:
        if st["cover_position"] is not None and not (st["action_entity"] or "").startswith("cover."):
            raise vol.Invalid("Rollo-Position nur zusammen mit einer cover-Entität")
    return stages


# Kontakt eines anderen Raums, der diesen Raum nach eigener Verzögerung mitsperrt
COUPLED_SCHEMA = vol.Schema(
    {vol.Required("entity_id"): WINDOW_ID, vol.Optional("delay", default=0): COUPLED_DELAY}
)


def _coupled(value: Any) -> list[dict]:
    if not isinstance(value, list):
        raise vol.Invalid("Liste gekoppelter Kontakte erwartet")
    out, seen = [], set()
    for item in value:
        item = COUPLED_SCHEMA(item)
        if item["entity_id"] in seen:
            raise vol.Invalid(f"{item['entity_id']} ist doppelt gekoppelt")
        seen.add(item["entity_id"])
        out.append(item)
    return out


def _room_rules(room: dict) -> dict:
    # Migration v1.1 -> v1.2: einzelne Meldezeit wird zur ersten Meldestufe
    if room["notify_after"] and not room["notify_stages"]:
        room["notify_stages"] = _stages([{"after": max(room["notify_after"], 60)}])
    room["notify_after"] = 0
    own = set(room["window_sensors"])
    dup = [c["entity_id"] for c in room["coupled_sensors"] if c["entity_id"] in own]
    if dup:
        raise vol.Invalid(f"{', '.join(dup)} ist bereits eigener Fensterkontakt dieses Raums")
    return room
NOTIFY_SECONDS = vol.All(vol.Coerce(int), vol.Range(min=0, max=24 * 3600))  # 0 = aus


def _outdoor_curve(value: dict) -> dict:
    if value["full_at"] <= value["start_at"]:
        raise vol.Invalid("'full_at' muss größer als 'start_at' sein")
    return value


def _curve_points(value: Any) -> list[list[float]]:
    """Optionale Stützpunkte [[Außentemp, Absenkung], ...] – überschreiben die lineare Kurve.

    Außentemperaturen streng steigend, Absenkung nicht fallend (nachvollziehbar).
    """
    if not isinstance(value, list):
        raise vol.Invalid("Liste von [Außentemperatur, Absenkung] erwartet")
    points = []
    for item in value:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise vol.Invalid(f"Stützpunkt {item!r} muss [Außentemperatur, Absenkung] sein")
        points.append([OUTDOOR_TEMP(item[0]), REDUCTION(item[1])])
    if len(points) == 1:
        raise vol.Invalid("Mindestens zwei Stützpunkte nötig")
    for a, b in zip(points, points[1:], strict=False):
        if b[0] <= a[0]:
            raise vol.Invalid("Außentemperaturen der Stützpunkte müssen steigen")
        if b[1] < a[1]:
            raise vol.Invalid("Absenkung darf bei steigender Außentemperatur nicht kleiner werden")
    return points


OUTDOOR_CURVE_SCHEMA = vol.All(
    vol.Schema(
        {
            vol.Optional("start_at", default=DEFAULT_OUTDOOR_START): OUTDOOR_TEMP,
            vol.Optional("full_at", default=DEFAULT_OUTDOOR_FULL): OUTDOOR_TEMP,
            vol.Optional("max_reduction", default=DEFAULT_OUTDOOR_MAX_REDUCTION): REDUCTION,
            vol.Optional("points"): _curve_points,
        }
    ),
    _outdoor_curve,
)

SETTINGS_SCHEMA = vol.Schema(
    {
        vol.Optional("active_profile_id", default=None): vol.Any(None, str),
        vol.Optional("outdoor_sensor", default=None): vol.Any(None, TEMP_SENSOR_ID),
        vol.Optional("outdoor_enabled", default=False): bool,
        vol.Optional("outdoor_curve", default={}): OUTDOOR_CURVE_SCHEMA,
        vol.Optional("unknown_window_policy", default=UNKNOWN_AS_OPEN): vol.In(UNKNOWN_POLICIES),
        vol.Optional("default_temp", default=DEFAULT_COMFORT_TEMP): TEMP,
        # Benachrichtigung über einen beliebigen HA-Dienst (Telegram, Mobile App, ...)
        vol.Optional("notify_service", default=None): vol.Any(None, _service),
        vol.Optional("notify_entity_id", default=None): vol.Any(None, NOTIFY_ENTITY_ID),
    }
)

ROOM_SCHEMA = vol.All(vol.Schema(
    {
        vol.Required("name"): _name,
        vol.Optional("thermostats", default=[]): _unique_list(CLIMATE_ID),
        vol.Optional("window_sensors", default=[]): _unique_list(WINDOW_ID),
        vol.Optional("temperature_sensor", default=None): vol.Any(None, TEMP_SENSOR_ID),
        vol.Optional("window_open_temp", default=DEFAULT_WINDOW_OPEN_TEMP): TEMP,
        vol.Optional("profile_override_id", default=None): vol.Any(None, str),
        vol.Optional("outdoor_mode", default=OUTDOOR_MODE_GLOBAL): vol.In(OUTDOOR_MODES),
        vol.Optional("outdoor_curve", default={}): OUTDOOR_CURVE_SCHEMA,
        # Fenster muss so lange offen sein, bevor die Sperre greift (z. B. Terrassentür)
        vol.Optional("window_open_delay", default=0): DELAY_SECONDS,
        # Meldung, wenn ein Fenster länger offen ist (0 = keine Meldung)
        vol.Optional("notify_after", default=0): NOTIFY_SECONDS,
        vol.Optional("notify_on_close", default=True): bool,
        vol.Optional("notify_stages", default=[]): _stages,
        vol.Optional("coupled_sensors", default=[]): _coupled,
        # Manuell zugeordnete Batterie-Entitäten (Fallback, wenn die Automatik nichts findet)
        vol.Optional("battery_entities", default=[]): _unique_list(BATTERY_ID),
    }
), _room_rules)


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def norm_time(value: str) -> str:
    """Flexible Eingaben ("6", "6:00", "6.00", "6,00", "630") -> "HH:MM" (sonst unverändert)."""
    v = value.strip()
    m = re.fullmatch(r"(\d{1,2})(?:[:.,](\d{1,2}))?", v) or re.fullmatch(r"(\d{1,2})(\d{2})", v)
    if not m:
        return v
    return f"{int(m.group(1)):02d}:{int(m.group(2) or 0):02d}"


def _time(value: Any) -> str:
    if not isinstance(value, str):
        raise vol.Invalid(f"Ungültige Uhrzeit: {value!r}")
    out = norm_time(value)
    if not _TIME_RE.match(out):
        raise vol.Invalid(f"Ungültige Uhrzeit: {value!r}")
    return out


SLOT_SCHEMA = vol.Schema(
    {
        vol.Required("start"): _time,
        vol.Required("end"): _time,
        vol.Required("temp"): TEMP,
    }
)


def validate_day(slots: Any) -> list[dict]:
    """Zeitabschnitte eines Tages: gültig, sortiert, ohne Überschneidung."""
    if not isinstance(slots, list):
        raise vol.Invalid("Liste von Zeitabschnitten erwartet")
    clean = [SLOT_SCHEMA(s) for s in slots]
    for s in clean:
        if s["start"] == "24:00":
            raise vol.Invalid("Startzeit 24:00 ist ungültig")
        if _minutes(s["end"]) <= _minutes(s["start"]):
            raise vol.Invalid(f"Ende muss nach Start liegen ({s['start']}–{s['end']})")
    clean.sort(key=lambda s: _minutes(s["start"]))
    for a, b in zip(clean, clean[1:], strict=False):
        if _minutes(b["start"]) < _minutes(a["end"]):
            raise vol.Invalid(
                f"Überschneidung: {a['start']}–{a['end']} und {b['start']}–{b['end']}"
            )
    return clean


WEEK_SCHEMA = vol.Schema({vol.Optional(day, default=[]): validate_day for day in WEEKDAYS})

PROFILE_SCHEMA = vol.Schema(
    {
        vol.Required("name"): _name,
        vol.Optional("schedules", default={}): {str: WEEK_SCHEMA},
    }
)


MANUAL_UNTIL_NEXT = "until_next"
MANUAL_PERMANENT = "permanent"


def _iso_utc(value: Any) -> str:
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError as err:
        raise vol.Invalid(f"Ungültiger Zeitstempel: {value!r}") from err
    if dt.tzinfo is None:
        raise vol.Invalid("Zeitstempel braucht eine Zeitzone")
    return dt.isoformat()


# Manueller Sollwert = gewünschte Temperatur (nicht die an das Thermostat
# gesendete). Die Fenstersperre (Modul 4) wird erst danach angewendet.
OVERRIDE_SCHEMA = vol.Schema(
    {
        vol.Required("temp"): TEMP,
        vol.Required("mode"): vol.In((MANUAL_UNTIL_NEXT, MANUAL_PERMANENT)),
        vol.Optional("until", default=None): vol.Any(None, _iso_utc),
        vol.Required("set_at"): _iso_utc,
    }
)


def default_config() -> dict[str, Any]:
    return {"settings": SETTINGS_SCHEMA({}), "rooms": {}, "profiles": {}, "overrides": {}}


def validate_config(data: Any) -> dict[str, Any]:
    """Komplette Konfiguration prüfen und Referenzen bereinigen.

    Wirft vol.Invalid bei strukturellen Fehlern. Verwaiste Verweise
    (gelöschte Räume/Profile) werden entfernt statt zu scheitern.
    """
    if not isinstance(data, dict):
        raise vol.Invalid("Konfiguration muss ein Objekt sein")
    data = copy.deepcopy(data)
    cfg = {
        "settings": SETTINGS_SCHEMA(data.get("settings") or {}),
        "rooms": {str(k): ROOM_SCHEMA(v) for k, v in (data.get("rooms") or {}).items()},
        "profiles": {str(k): PROFILE_SCHEMA(v) for k, v in (data.get("profiles") or {}).items()},
        "overrides": {str(k): OVERRIDE_SCHEMA(v) for k, v in (data.get("overrides") or {}).items()},
    }
    rooms, profiles = cfg["rooms"], cfg["profiles"]
    if cfg["settings"]["active_profile_id"] not in profiles:
        cfg["settings"]["active_profile_id"] = None
    for room in rooms.values():
        if room["profile_override_id"] not in profiles:
            room["profile_override_id"] = None
    for profile in profiles.values():
        profile["schedules"] = {
            rid: week for rid, week in profile["schedules"].items() if rid in rooms
        }
    cfg["overrides"] = {rid: o for rid, o in cfg["overrides"].items() if rid in rooms}
    return cfg
