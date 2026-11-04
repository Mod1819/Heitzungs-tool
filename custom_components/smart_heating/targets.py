"""Zentrale Sollwertermittlung – die EINZIGE Stelle für die Prioritätenlogik.

Modul 3 berechnet die *gewünschte* Temperatur pro Raum:
    manueller Sollwert (gültig)  >  Wochenplan des wirksamen Profils  >  Standardtemperatur

Modul 4/5 ergänzen hier (und nur hier) die *effektive* Temperatur:
    Fehler-/Sicherheitszustand > Fenstersperre > gewünschte Temperatur (+ Außentemperatur)
Die gewünschte Temperatur bleibt dabei unverändert gespeichert; eine
manuelle Änderung kann die Fenstersperre so strukturell nicht umgehen.
"""
from __future__ import annotations

from datetime import datetime, timedelta, tzinfo
from typing import Any

from .schedule import current_slot, next_change

SOURCE_MANUAL = "manual"
SOURCE_SCHEDULE = "schedule"
SOURCE_DEFAULT = "default"


def room_week(cfg: dict, room_id: str) -> tuple[str | None, dict | None]:
    room = cfg["rooms"][room_id]
    pid = room["profile_override_id"] or cfg["settings"]["active_profile_id"]
    if pid is None or pid not in cfg["profiles"]:
        return None, None
    return pid, cfg["profiles"][pid]["schedules"].get(room_id)


def manual_until(cfg: dict, room_id: str, now_utc: datetime, tz: tzinfo) -> datetime | None:
    """Ablauf einer manuellen Änderung 'bis zum nächsten Zeitabschnitt'."""
    return next_change(room_week(cfg, room_id)[1], now_utc, tz)


def _active_override(cfg: dict, room_id: str, now_utc: datetime) -> dict | None:
    o = cfg["overrides"].get(room_id)
    if o is None:
        return None
    if o["until"] and datetime.fromisoformat(o["until"]) <= now_utc:
        return None
    return o


def resolve_desired(cfg: dict, room_id: str, now_utc: datetime, tz: tzinfo) -> dict[str, Any]:
    pid, week = room_week(cfg, room_id)
    slot = current_slot(week, now_utc.astimezone(tz))
    scheduled = slot["temp"] if slot else None
    override = _active_override(cfg, room_id, now_utc)
    if override:
        desired, source = override["temp"], SOURCE_MANUAL
    elif scheduled is not None:
        desired, source = scheduled, SOURCE_SCHEDULE
    else:
        desired, source = cfg["settings"]["default_temp"], SOURCE_DEFAULT
    return {
        "room_id": room_id,
        "desired_temp": desired,
        "source": source,
        "scheduled_temp": scheduled,
        "profile_id": pid,
        "profile_source": "room" if cfg["rooms"][room_id]["profile_override_id"] else "global",
        "slot": slot,
        "manual": override,
        "next_change": nc.isoformat() if (nc := next_change(week, now_utc, tz)) else None,
    }


def resolve_all(cfg: dict, now_utc: datetime, tz: tzinfo) -> dict[str, dict[str, Any]]:
    return {rid: resolve_desired(cfg, rid, now_utc, tz) for rid in cfg["rooms"]}


def next_wakeup(cfg: dict, targets: dict[str, dict], now_utc: datetime) -> datetime | None:
    """Frühester Zeitpunkt, an dem sich irgendein Sollwert ändern kann."""
    times = [datetime.fromisoformat(t["next_change"]) for t in targets.values() if t["next_change"]]
    times += [
        datetime.fromisoformat(o["until"]) for o in cfg["overrides"].values()
        if o["until"] and datetime.fromisoformat(o["until"]) > now_utc
    ]
    return min(times, default=None)


# --- Modul 4: effektive Temperatur (Fenstersperre) ----------------------------

WINDOW_OPEN = "open"
WINDOW_CLOSED = "closed"
WINDOW_UNKNOWN = "unknown"

REASON_WINDOW_OPEN = "window_open"
REASON_WINDOW_UNKNOWN = "window_unknown"
REASON_WINDOW_COUPLED = "window_coupled"  # Kontakt eines anderen Raums (z. B. Terrassentür)


def window_state(raw: str | None) -> str:
    """HA-Zustand -> open/closed/unknown. Nicht verfügbar ist NIE geschlossen."""
    if raw == "on":
        return WINDOW_OPEN
    if raw == "off":
        return WINDOW_CLOSED
    return WINDOW_UNKNOWN


def resolve_effective(
    cfg: dict, desired: dict[str, dict[str, Any]], windows: dict[str, str],
    outdoor: float | None = None,
    open_since: dict[str, datetime] | None = None, now: datetime | None = None,
) -> dict[str, dict[str, Any]]:
    """Fenstersperre auf die gewünschten Temperaturen anwenden.

    windows: entity_id -> open/closed/unknown (fehlende Einträge = unknown).
    Ergebnis:
      rooms[room_id]        : gewünschte + effektive Temperatur, Sperrgrund
      thermostats[entity_id]: Zieltemperatur = niedrigster effektiver Wert aller
                              Räume des Thermostats (gemeinsam genutzte Geräte).
    Die gewünschte Temperatur wird nie verändert.

    Reihenfolge (Modul 5): gewünscht -> Außentemperatur-Absenkung (nur Plan/Standard,
    nicht bei manuellem Sollwert) -> Fenstersperre. outdoor=None (Sensor ungültig
    oder nicht konfiguriert) bedeutet: keine Absenkung.

    Öffnungsverzögerung: open_since[entity] = seit wann der Kontakt offen ist. Ein
    offener Kontakt sperrt erst, wenn er länger als window_open_delay des Raums offen
    ist. Unbekannte Kontakte sperren sofort (Sicherheit). Ohne open_since/now keine
    Verzögerung.

    Kopplung: coupled_sensors eines Raums (Kontakte anderer Räume) sperren ihn nach
    ihrer eigenen Verzögerung mit. Ein unbekannter gekoppelter Kontakt sperrt NICHT
    (Komfortkopplung; der Raum mit dem Fenster ist selbst abgesichert). Gekoppelte
    Kontakte zählen nicht als "Fenster offen" des Raums (keine Meldungen dort).
    """
    unknown_locks = cfg["settings"]["unknown_window_policy"] == WINDOW_OPEN
    rooms: dict[str, dict[str, Any]] = {}
    for rid, room in cfg["rooms"].items():
        sensors = {e: windows.get(e, WINDOW_UNKNOWN) for e in room["window_sensors"]}
        open_ = [e for e, s in sensors.items() if s == WINDOW_OPEN]
        delay = room.get("window_open_delay", 0)
        lock_at: datetime | None = None
        since = [open_since[e] for e in open_ if open_since and e in open_since]
        room_open_since = min(since) if since else None
        if open_ and delay and now is not None and open_since is not None:
            # nur Kontakte, deren Verzögerung abgelaufen ist, sperren
            due = [e for e in open_ if e not in open_since
                   or (now - open_since[e]).total_seconds() >= delay]
            if not due:
                lock_at = min(open_since[e] for e in open_) + timedelta(seconds=delay)
            open_lock = due
        else:
            open_lock = open_
        coupled: dict[str, str] = {}
        coupled_lock: list[str] = []
        for c in room.get("coupled_sensors", []):
            ce = c["entity_id"]
            coupled[ce] = windows.get(ce, WINDOW_UNKNOWN)
            if coupled[ce] != WINDOW_OPEN:
                continue
            if c["delay"] and now is not None and open_since is not None and ce in open_since:
                c_due = open_since[ce] + timedelta(seconds=c["delay"])
                if now < c_due:
                    lock_at = c_due if lock_at is None else min(lock_at, c_due)
                    continue
            coupled_lock.append(ce)
        unknown = [e for e, s in sensors.items() if s == WINDOW_UNKNOWN]
        d = desired.get(rid) or {"desired_temp": cfg["settings"]["default_temp"], "source": SOURCE_DEFAULT}
        want = d["desired_temp"]
        reduction = room_reduction(cfg, rid, outdoor) if d["source"] != SOURCE_MANUAL else 0.0
        adjusted = max(want - reduction, min(want, OUTDOOR_MIN_RESULT)) if reduction else want
        if open_lock or coupled_lock or (unknown and unknown_locks):
            effective = min(room["window_open_temp"], adjusted)
            if open_lock:
                reason = REASON_WINDOW_OPEN
            elif unknown and unknown_locks:
                reason = REASON_WINDOW_UNKNOWN
            else:
                reason = REASON_WINDOW_COUPLED
            locked = True
        else:
            effective, reason, locked = adjusted, d["source"], False
        if open_:
            window = WINDOW_OPEN
        elif unknown:
            window = WINDOW_UNKNOWN
        else:
            window = WINDOW_CLOSED
        rooms[rid] = {
            "desired_temp": want, "outdoor_reduction": round(want - adjusted, 1),
            "adjusted_temp": adjusted, "effective_temp": effective, "locked": locked,
            "reason": reason, "window": window, "sensors": sensors,
            "open_since": room_open_since.isoformat() if room_open_since else None,
            "lock_pending_until": lock_at.isoformat() if lock_at and not locked else None,
            "coupled": coupled,
            "locked_by": (open_lock + coupled_lock) if locked else [],
        }
    thermostats: dict[str, dict[str, Any]] = {}
    for rid, room in cfg["rooms"].items():
        for eid in room["thermostats"]:
            t = thermostats.setdefault(eid, {"rooms": [], "target": None, "decisive_room": None,
                                             "locked": False})
            t["rooms"].append(rid)
            r = rooms[rid]
            t["locked"] = t["locked"] or r["locked"]
            if t["target"] is None or r["effective_temp"] < t["target"]:
                t["target"], t["decisive_room"] = r["effective_temp"], rid
    return {"rooms": rooms, "thermostats": thermostats}


# --- Modul 5: Außentemperaturregelung ------------------------------------------

OUTDOOR_MIN_RESULT = 5.0  # Absenkung führt nie unter diesen Wert (Frostschutz-Reserve)


def outdoor_reduction(curve: dict, outdoor: float) -> float:
    """Stufenlose Absenkung in K für eine Außentemperatur (auf 0,1 K gerundet).

    Mit 'points': stückweise linear durch die Stützpunkte, außerhalb konstant.
    Sonst linear: <= start_at -> 0, >= full_at -> max_reduction.
    """
    points = curve.get("points") or [[curve["start_at"], 0.0], [curve["full_at"], curve["max_reduction"]]]
    if outdoor <= points[0][0]:
        value = points[0][1]
    elif outdoor >= points[-1][0]:
        value = points[-1][1]
    else:
        value = 0.0
        for (x0, y0), (x1, y1) in zip(points, points[1:], strict=False):
            if x0 <= outdoor <= x1:
                value = y0 + (y1 - y0) * (outdoor - x0) / (x1 - x0)
                break
    return round(value, 1)


def room_reduction(cfg: dict, room_id: str, outdoor: float | None) -> float:
    """Absenkung für einen Raum; 0 bei deaktiviert, Raum 'off' oder ungültigem Sensorwert."""
    settings, room = cfg["settings"], cfg["rooms"][room_id]
    if outdoor is None or not settings["outdoor_enabled"] or not settings["outdoor_sensor"]:
        return 0.0
    if room["outdoor_mode"] == "off":
        return 0.0
    curve = room["outdoor_curve"] if room["outdoor_mode"] == "custom" else settings["outdoor_curve"]
    return outdoor_reduction(curve, outdoor)
