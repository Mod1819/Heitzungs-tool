"""Benachrichtigung bei zu lange geöffneten Fenstern.

Mehrstufig pro Raum über notify_stages, z. B. 5 / 10 / 20 / 30 min. Jede Stufe:
  * Nachricht (notify) und/oder Aktion: cover.* schließen bzw. auf Position fahren,
    script.* ausführen, scene.* aktivieren. Nichts wird automatisch zurückgefahren.
Ausgabewege:
  1. HA-Event  smart_heating_window_alert  – immer, für eigene Automationen
     (z. B. Telegram mit eigenem Text, Sprachausgabe, Licht blinken lassen).
  2. Direkter Dienstaufruf, wenn in den Einstellungen notify_service gesetzt ist,
     z. B. notify.telegram, telegram_bot.send_message oder notify.mobile_app_xyz.
     Optional notify_entity_id (für notify.send_message mit Notify-Entität).

Jede Stufe wird pro Öffnungsvorgang genau einmal ausgelöst; beim Schließen aller Kontakte optional
eine Entwarnung (notify_on_close). Ein nicht verfügbarer Kontakt gilt dabei
weder als offen noch als geschlossen (keine falsche Entwarnung).
"""
from __future__ import annotations

import logging
import random
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

EVENT_WINDOW_ALERT = "smart_heating_window_alert"
TYPE_OPEN = "open_too_long"
TYPE_CLOSED = "closed"
TITLE = "Heizung"


def _fmt_minutes(seconds: float) -> str:
    minutes = round(seconds / 60)
    return "1 Minute" if minutes == 1 else f"{minutes} Minuten"


def _fmt_temp(value: float | None) -> str:
    return "–" if value is None else f"{value:.1f} °C".replace(".", ",")


class WindowNotifier:
    def __init__(self, hass: HomeAssistant, log: Callable[[list[str], str], None]) -> None:
        self.hass = hass
        self._log = log
        self.sent: dict[str, dict[str, Any]] = {}  # room_id -> {"since", "at", "stage"}
        self._last_text: dict[tuple[str, str], int] = {}  # (Raum, Art) -> zuletzt benutzter Textindex

    def _names(self, entity_ids: list[str]) -> str:
        names = []
        for eid in entity_ids:
            st = self.hass.states.get(eid)
            names.append(st.name if st else eid)
        return ", ".join(names)

    def _custom_text(self, cfg: dict, rid: str, kind: str, values: dict[str, str]) -> str | None:
        """Zufälliger eigener Text (nie zweimal derselbe hintereinander) mit ersetzten Platzhaltern."""
        room = cfg["rooms"][rid]
        texts = room["notify_texts"] if kind == TYPE_OPEN else room["notify_close_texts"]
        if not texts:
            return None
        last = self._last_text.get((rid, kind))
        choices = [i for i in range(len(texts)) if i != last] if len(texts) > 1 else [0]
        idx = random.choice(choices)
        self._last_text[(rid, kind)] = idx
        message = texts[idx]
        for key, value in values.items():  # kein str.format: Klammern im Text bleiben unberührt
            message = message.replace("{" + key + "}", value)
        return message

    def process(self, cfg: dict, rooms: dict[str, dict[str, Any]], now: datetime) -> datetime | None:
        """Fällige Stufen auslösen; liefert den nächsten Fälligkeitszeitpunkt."""
        next_due: datetime | None = None
        for rid in list(self.sent):
            if rid not in cfg["rooms"]:
                self.sent.pop(rid)
        for key in [k for k in self._last_text if k[0] not in cfg["rooms"]]:
            self._last_text.pop(key)
        for rid, room in cfg["rooms"].items():
            r = rooms.get(rid)
            if r is None:
                continue
            stages = room["notify_stages"]
            since = datetime.fromisoformat(r["open_since"]) if r["open_since"] else None
            if since is not None:  # mindestens ein eigener Kontakt offen
                done = self.sent.get(rid, {}).get("stage", 0)
                for idx in range(done, len(stages)):
                    due = since + timedelta(seconds=stages[idx]["after"])
                    if now < due:
                        if next_due is None or due < next_due:
                            next_due = due
                        break
                    self.sent[rid] = {"since": since.isoformat(), "at": now.isoformat(), "stage": idx + 1}
                    self._stage(cfg, rid, r, idx, len(stages), (now - since).total_seconds())
            elif r["window"] == "closed" and rid in self.sent:
                info = self.sent.pop(rid)
                if room["notify_on_close"]:
                    duration = (now - datetime.fromisoformat(info["since"])).total_seconds()
                    self._alert(cfg, rid, r, TYPE_CLOSED, duration, notify=True)
        return next_due

    def _stage(self, cfg: dict, rid: str, r: dict[str, Any], idx: int, total: int, seconds: float) -> None:
        stage = cfg["rooms"][rid]["notify_stages"][idx]
        action = stage["action_entity"]
        note = ""
        if action:
            note = self._run_action(rid, action, stage["cover_position"])
        self._alert(cfg, rid, r, TYPE_OPEN, seconds, notify=stage["notify"], stage=idx + 1,
                    stages=total, action=action, note=note)

    def _run_action(self, rid: str, entity_id: str, position: int | None) -> str:
        domain = entity_id.split(".", 1)[0]
        if domain == "cover" and position is not None:
            service, data, note = "set_cover_position", {"entity_id": entity_id, "position": position}, \
                f" Rollo fährt auf {position} %."
        elif domain == "cover":
            service, data, note = "close_cover", {"entity_id": entity_id}, " Rollo wird geschlossen."
        else:  # script / scene
            service, data = "turn_on", {"entity_id": entity_id}
            note = f" {self._names([entity_id])} ausgeführt."
        self._log([rid], f"Aktion: {domain}.{service} {entity_id}")
        self.hass.async_create_task(self.async_send(f"{domain}.{service}", data, [rid]), eager_start=False)
        return note

    def _alert(self, cfg: dict, rid: str, r: dict[str, Any], kind: str, seconds: float, *,
               notify: bool, stage: int = 0, stages: int = 0, action: str | None = None,
               note: str = "") -> None:
        name = cfg["rooms"][rid]["name"]
        open_ids = [e for e, s in r["sensors"].items() if s == "open"]
        if kind == TYPE_OPEN:
            what = self._names(open_ids) or "Fenster"
            heat = (f" Heizung auf {_fmt_temp(r['effective_temp'])} abgesenkt." if r["locked"]
                    else " Heizung noch nicht abgesenkt.")
            icon, suffix = ("🪟", "") if stage <= 1 else ("⚠️", f" ({stage}. Erinnerung)")
            message = f"{icon} {name}: {what} seit {_fmt_minutes(seconds)} offen{suffix}.{heat}{note}"
        else:
            what = ""
            message = (f"✅ {name}: Fenster nach {_fmt_minutes(seconds)} wieder geschlossen. "
                       f"Heizung zurück auf {_fmt_temp(r['effective_temp'])}.")
        custom = self._custom_text(cfg, rid, kind, {
            "raum": name, "fenster": what or self._names(open_ids) or "Fenster",
            "minuten": str(round(seconds / 60)), "stufe": str(stage),
            "temperatur": _fmt_temp(r["effective_temp"]), "solltemperatur": _fmt_temp(r["desired_temp"]),
        })
        if custom is not None:
            message = custom + (note if kind == TYPE_OPEN else "")
        self.hass.bus.async_fire(EVENT_WINDOW_ALERT, {
            "type": kind, "room_id": rid, "room_name": name, "message": message,
            "stage": stage, "stages": stages, "action_entity": action,
            "open_sensors": open_ids, "open_minutes": round(seconds / 60, 1),
            "desired_temp": r["desired_temp"], "effective_temp": r["effective_temp"],
            "locked": r["locked"],
        })
        self._log([rid], f"Meldung: {message}")
        service = cfg["settings"]["notify_service"]
        if service and notify:
            data: dict[str, Any] = {"message": message, "title": TITLE}
            if cfg["settings"]["notify_entity_id"]:
                data["entity_id"] = cfg["settings"]["notify_entity_id"]
            self.hass.async_create_task(self.async_send(service, data, [rid]), eager_start=False)

    async def async_send(self, service: str, data: dict[str, Any], rooms: list[str]) -> bool:
        domain, name = service.split(".", 1)
        try:
            await self.hass.services.async_call(domain, name, data, blocking=True)
        except Exception as err:  # noqa: BLE001 – Meldefehler dürfen die Regelung nie stören
            _LOGGER.warning("Aufruf %s fehlgeschlagen: %s", service, err)
            self._log(rooms, f"Aufruf {service} fehlgeschlagen: {err}")
            return False
        return True
