"""Thermostat-Ansteuerung mit Fenstersperre (Modul 4).

Ablauf:
  Fenster-/Thermostat-Event oder neuer Sollwert vom Scheduler
    -> targets.resolve_effective (zentrale Prioritäten, rein)
    -> pro Thermostat: Zielwert runden/begrenzen, nur bei Abweichung senden.

Schutzmechanismen:
  * Start erst, wenn HA vollständig läuft (Fensterzustände bekannt);
    unbekannte Kontakte gelten je nach Einstellung als offen (Standard).
  * Echo-Schutz: eigene Befehle werden bis zur Bestätigung (max. CONFIRM_TIMEOUT)
    nicht als manuelle Bedienung gewertet.
  * Wiederholung nur begrenzt (MAX_RETRIES), danach Status "not_confirmed".
  * Manuelle Bedienung am Thermostat -> gewünschter Sollwert der Räume
    (Scheduler). Die Fenstersperre bleibt davon unberührt und wird erneut gesendet.
  * Thermostate im HVAC-Modus "off", ohne Einzelsollwert oder nicht verfügbar
    werden nicht angesteuert.
"""
from __future__ import annotations

import logging
from collections import deque
from datetime import datetime
from typing import Any

import voluptuous as vol

from homeassistant.const import (
    ATTR_TEMPERATURE, EVENT_HOMEASSISTANT_STARTED, STATE_UNAVAILABLE, STATE_UNKNOWN,
)
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, State, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import (
    async_call_later, async_track_point_in_utc_time, async_track_state_change_event,
)
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import TemperatureConverter

from .devices import battery_report
from .models import MANUAL_UNTIL_NEXT
from .notify import WindowNotifier
from .rooms import RoomError, rooms_for_thermostat
from .scheduler import Scheduler
from .store import ConfigStore
from .targets import resolve_effective, window_state

_LOGGER = logging.getLogger(__name__)

EPS = 0.05
CONFIRM_TIMEOUT = 30  # s bis ein gesendeter Wert bestätigt sein muss
MAX_RETRIES = 2
ACTION_LOG = 25
OUTDOOR_HYSTERESIS = 0.5  # K – kleinere Schwankungen lösen keine Neuberechnung aus
OUTDOOR_RANGE = (-50.0, 60.0)  # plausibler Messbereich in °C

ST_OK = "ok"
ST_PENDING = "pending"
ST_UNAVAILABLE = "unavailable"
ST_HVAC_OFF = "hvac_off"
ST_UNSUPPORTED = "unsupported"
ST_NOT_CONFIRMED = "not_confirmed"
ST_ERROR = "service_error"


class Controller:
    def __init__(self, hass: HomeAssistant, store: ConfigStore, scheduler: Scheduler) -> None:
        self.hass = hass
        self.store = store
        self.scheduler = scheduler
        self.started = False
        self.result: dict[str, Any] = {"rooms": {}, "thermostats": {}}
        self.thermostat_status: dict[str, dict[str, Any]] = {}
        self.actions: dict[str, deque] = {}
        self._pending: dict[str, dict[str, Any]] = {}
        self._commanded: dict[str, float] = {}
        # Gerät meldet dauerhaft leicht abweichend (z. B. +0,3 K): Ziel -> akzeptierter Gerätewert
        self._accepted: dict[str, tuple[float, float]] = {}
        self._failed: dict[str, float] = {}
        self._entities: frozenset[str] = frozenset()
        self._unsubs: list[CALLBACK_TYPE] = []
        self._unsub_events: CALLBACK_TYPE | None = None
        self._outdoor_used: float | None = None
        self.outdoor_status: dict[str, Any] = {"status": "not_configured"}
        self._status_listeners: list[CALLBACK_TYPE] = []
        self.notifier = WindowNotifier(hass, self._log)
        self._unsub_wakeup: CALLBACK_TYPE | None = None
        self.next_wakeup: datetime | None = None
        self._notify_scheduled = False

    # --- Lebenszyklus --------------------------------------------------------

    @callback
    def async_start(self) -> None:
        if self.hass.is_running:
            self._start()
        else:
            self._unsubs.append(
                self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, self._on_ha_started)
            )

    @callback
    def _on_ha_started(self, _event: Event) -> None:
        self._unsubs.clear()  # listen_once hat sich selbst abgemeldet
        self._start()

    @callback
    def _start(self) -> None:
        self.started = True
        self._unsubs.append(self.scheduler.async_add_listener(lambda _t: self.evaluate()))
        self._unsubs.append(self.store.async_add_listener(lambda _c: self._resubscribe()))
        self._resubscribe()
        _LOGGER.info("Smart Heating Regelung aktiv")

    @callback
    def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self._unsub_events:
            self._unsub_events()
            self._unsub_events = None
        for p in self._pending.values():
            p["cancel"]()
        self._pending.clear()
        self._cancel_wakeup()
        self.started = False

    def _cancel_wakeup(self) -> None:
        if self._unsub_wakeup:
            self._unsub_wakeup()
            self._unsub_wakeup = None

    @callback
    def _on_wakeup(self, _now: datetime) -> None:
        self._unsub_wakeup = None
        self.evaluate()

    @callback
    def _schedule_wakeup(self, now: datetime, notify_due: datetime | None) -> None:
        """EIN Timer für Öffnungsverzögerung und Meldezeitpunkt (kein Polling)."""
        times = [datetime.fromisoformat(r["lock_pending_until"])
                 for r in self.result["rooms"].values() if r.get("lock_pending_until")]
        if notify_due:
            times.append(notify_due)
        wake = min((t for t in times if t > now), default=None)
        if wake == self.next_wakeup and self._unsub_wakeup:
            return
        self._cancel_wakeup()
        self.next_wakeup = wake
        if wake:
            self._unsub_wakeup = async_track_point_in_utc_time(self.hass, self._on_wakeup, wake)

    @callback
    def _resubscribe(self) -> None:
        cfg = self.store.data
        ents: set[str] = set()
        for room in cfg["rooms"].values():
            ents.update(room["thermostats"], room["window_sensors"])
            ents.update(c["entity_id"] for c in room["coupled_sensors"])
        if cfg["settings"]["outdoor_sensor"]:
            ents.add(cfg["settings"]["outdoor_sensor"])
        ents_f = frozenset(ents)
        if ents_f != self._entities:
            if self._unsub_events:
                self._unsub_events()
                self._unsub_events = None
            self._entities = ents_f
            if ents_f:
                self._unsub_events = async_track_state_change_event(
                    self.hass, sorted(ents_f), self._on_state_event
                )
            for eid in list(self._pending):
                if eid not in ents_f:
                    self._pending.pop(eid)["cancel"]()
        self.evaluate()

    # --- Auswertung ------------------------------------------------------------

    def _log(self, rooms: list[str], msg: str) -> None:
        _LOGGER.info(msg)
        self._notify()
        entry = {"time": dt_util.utcnow().isoformat(), "message": msg}
        for rid in rooms:
            self.actions.setdefault(rid, deque(maxlen=ACTION_LOG)).appendleft(entry)

    @callback
    def evaluate(self) -> None:
        if not self.started:
            return
        cfg = self.store.data
        windows: dict[str, str] = {}
        open_since: dict[str, datetime] = {}
        for room in cfg["rooms"].values():
            for e in [*room["window_sensors"], *(c["entity_id"] for c in room["coupled_sensors"])]:
                if e in windows:
                    continue
                st = self.hass.states.get(e)
                windows[e] = window_state(st.state if st else None)
                if st is not None and windows[e] == "open":
                    open_since[e] = st.last_changed  # seit wann offen (übersteht Reload)
        now = dt_util.utcnow()
        old_rooms = self.result.get("rooms", {})
        outdoor = self._outdoor_value(cfg)
        self.result = resolve_effective(cfg, self.scheduler.targets, windows, outdoor, open_since, now)
        for rid, r in self.result["rooms"].items():
            prev = old_rooms.get(rid)
            if prev is None or prev["locked"] != r["locked"]:
                if r["locked"]:
                    self._log([rid], f"{cfg['rooms'][rid]['name']}: Fenstersperre aktiv – {self._why(r)}")
                elif prev is not None:
                    self._log([rid], f"{cfg['rooms'][rid]['name']}: Fenstersperre aufgehoben")
        for eid in list(self.thermostat_status):
            if eid not in self.result["thermostats"]:
                self.thermostat_status.pop(eid)
                self._commanded.pop(eid, None)
                self._failed.pop(eid, None)
                self._accepted.pop(eid, None)
        for eid, info in self.result["thermostats"].items():
            self._apply(eid, info)
        notify_due = self.notifier.process(cfg, self.result["rooms"], now)
        self._schedule_wakeup(now, notify_due)
        self._notify()

    def _outdoor_value(self, cfg: dict) -> float | None:
        """Gültige Außentemperatur in °C (mit Hysterese) oder None.

        Ungültig/nicht verfügbar -> None -> keine Absenkung (Heizen nach Plan).
        """
        eid, enabled = cfg["settings"]["outdoor_sensor"], cfg["settings"]["outdoor_enabled"]
        if not eid:
            self._outdoor_used = None
            self.outdoor_status = {"status": "not_configured", "enabled": enabled}
            return None
        st = self.hass.states.get(eid)
        raw = st.state if st else None
        value: float | None = None
        status = "ok"
        try:
            value = float(raw)  # type: ignore[arg-type]
            unit = st.attributes.get("unit_of_measurement") if st else None
            if unit and unit != "°C":
                value = TemperatureConverter.convert(value, unit, "°C")
            if not OUTDOOR_RANGE[0] <= value <= OUTDOOR_RANGE[1]:
                status, value = "implausible", None
        except (TypeError, ValueError, HomeAssistantError):  # kein Zahlenwert / unbekannte Einheit
            status, value = "invalid", None
        if value is None:
            if self._outdoor_used is not None:
                self._log(list(cfg["rooms"]), f"Außentemperatur ungültig ({raw}) – keine Absenkung")
            self._outdoor_used = None
        elif self._outdoor_used is None or abs(value - self._outdoor_used) >= OUTDOOR_HYSTERESIS:
            self._outdoor_used = round(value, 1)
        self.outdoor_status = {
            "entity_id": eid, "enabled": enabled, "status": status, "raw": raw,
            "value": round(value, 1) if value is not None else None, "used": self._outdoor_used,
        }
        return self._outdoor_used

    def _why(self, r: dict[str, Any]) -> str:
        names = []
        for eid in r.get("locked_by") or []:
            st = self.hass.states.get(eid)
            names.append((st.name if st else eid) + (" (gekoppelt)" if eid in r.get("coupled", {}) else ""))
        if names:
            return ", ".join(names)
        return "Kontakt nicht verfügbar" if r["reason"] == "window_unknown" else r["reason"]

    @staticmethod
    def _normalize(state: State, target: float) -> float:
        attrs = state.attributes
        step = attrs.get("target_temp_step") or 0.5
        value = round(round(target / step) * step, 2)
        lo, hi = attrs.get("min_temp"), attrs.get("max_temp")
        if isinstance(lo, (int, float)):
            value = max(value, float(lo))
        if isinstance(hi, (int, float)):
            value = min(value, float(hi))
        return value

    @callback
    def async_add_status_listener(self, listener: CALLBACK_TYPE) -> CALLBACK_TYPE:
        """Benachrichtigung bei jeder Status-/Konfigurationsänderung (für das Panel)."""
        self._status_listeners.append(listener)

        @callback
        def remove() -> None:
            if listener in self._status_listeners:
                self._status_listeners.remove(listener)

        return remove

    @callback
    def _notify(self) -> None:
        """Mehrere Änderungen in einem Loop-Durchlauf zu einer Meldung bündeln."""
        if self._notify_scheduled or not self._status_listeners:
            return
        self._notify_scheduled = True

        @callback
        def _send() -> None:
            self._notify_scheduled = False
            for listener in list(self._status_listeners):
                try:
                    listener()
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("Fehler in Status-Listener")

        self.hass.loop.call_soon(_send)

    def _status(self, eid: str, status: str, target: float | None, reported: Any) -> None:
        info = self.result["thermostats"].get(eid, {})
        self.thermostat_status[eid] = {
            "status": status, "target": target, "reported": reported,
            "locked": info.get("locked", False), "rooms": info.get("rooms", []),
            "decisive_room": info.get("decisive_room"),
        }
        self._notify()

    @callback
    def _apply(self, eid: str, info: dict[str, Any]) -> None:
        st = self.hass.states.get(eid)
        if st is None or st.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            self._status(eid, ST_UNAVAILABLE, info["target"], None)
            return
        if st.state == "off":
            self._status(eid, ST_HVAC_OFF, info["target"], st.attributes.get(ATTR_TEMPERATURE))
            return
        current = st.attributes.get(ATTR_TEMPERATURE)
        if not isinstance(current, (int, float)):
            self._status(eid, ST_UNSUPPORTED, info["target"], current)
            return
        target = self._normalize(st, info["target"])
        pending = self._pending.get(eid)
        if pending and abs(pending["temp"] - target) < EPS:
            return  # bereits unterwegs
        if pending:
            pending["cancel"]()
            self._pending.pop(eid)
        if abs(current - target) < EPS:
            self._commanded[eid] = target
            self._failed.pop(eid, None)
            self._status(eid, ST_OK, target, current)
            return
        acc = self._accepted.get(eid)
        if acc and abs(acc[0] - target) < EPS and abs(acc[1] - current) < EPS:
            self._status(eid, ST_OK, target, current)
            return  # bekannte Geräteabweichung -> nicht erneut senden (keine Aufschaukelung)
        if self._failed.get(eid) is not None and abs(self._failed[eid] - target) < EPS:
            self._status(eid, ST_NOT_CONFIRMED, target, current)
            return  # nicht endlos wiederholen
        self._send(eid, target, current, retries=0, reason=info)

    @callback
    def _send(self, eid: str, target: float, before: float, retries: int, reason: dict) -> None:
        self._commanded[eid] = target
        self._failed.pop(eid, None)

        @callback
        def _timeout(_now: datetime) -> None:
            self._confirm_timeout(eid)

        cancel = async_call_later(self.hass, CONFIRM_TIMEOUT, _timeout)
        self._pending[eid] = {"temp": target, "before": before, "retries": retries, "cancel": cancel}
        self._status(eid, ST_PENDING, target, before)
        why = "Fenstersperre" if reason.get("locked") else "Sollwert"
        self._log(reason.get("rooms", []), f"{eid}: {before} -> {target} °C ({why})")
        self.hass.async_create_task(self._async_call(eid, target), eager_start=True)

    async def _async_call(self, eid: str, target: float) -> None:
        try:
            await self.hass.services.async_call(
                "climate", "set_temperature", {"entity_id": eid, ATTR_TEMPERATURE: target}, blocking=True
            )
        except Exception as err:  # noqa: BLE001 – Gerätefehler dürfen die Regelung nicht stoppen
            pending = self._pending.pop(eid, None)
            if pending:
                pending["cancel"]()
            self._status(eid, ST_ERROR, target, None)
            self._log(self.result["thermostats"].get(eid, {}).get("rooms", []),
                      f"{eid}: Fehler beim Senden: {err}")

    @staticmethod
    def _tolerance(state: State | None) -> float:
        step = state.attributes.get("target_temp_step") if state else None
        return max(step if isinstance(step, (int, float)) else 0.5, 0.5) + EPS

    @callback
    def _accept(self, eid: str, target: float, reported: float) -> None:
        """Gerät hat den Befehl übernommen, meldet aber leicht abweichend (Rundung/Eigenheit)."""
        pending = self._pending.pop(eid, None)
        if pending:
            pending["cancel"]()
        self._accepted[eid] = (target, reported)
        self._commanded[eid] = reported
        self._failed.pop(eid, None)
        self._status(eid, ST_OK, target, reported)
        self._log(self.result["thermostats"].get(eid, {}).get("rooms", []),
                  f"{eid}: meldet {reported} °C statt {target} °C – als Geräteabweichung akzeptiert")

    @callback
    def _confirm_timeout(self, eid: str) -> None:
        pending = self._pending.pop(eid, None)
        if pending is None:
            return
        st = self.hass.states.get(eid)
        cur = st.attributes.get(ATTR_TEMPERATURE) if st else None
        info = self.result["thermostats"].get(eid)
        if info is None:
            return
        if isinstance(cur, (int, float)) and abs(cur - pending["temp"]) < EPS:
            self._status(eid, ST_OK, pending["temp"], cur)
            return
        if (isinstance(cur, (int, float)) and abs(cur - pending["temp"]) <= self._tolerance(st)
                and abs(cur - pending["before"]) >= EPS):
            self._pending[eid] = pending  # _accept räumt auf
            self._accept(eid, pending["temp"], float(cur))
            return
        if (isinstance(cur, (int, float)) and abs(cur - pending["before"]) >= EPS
                and st is not None and st.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)):
            # Weder alter noch neuer Wert -> Bedienung während der Wartezeit
            self._manual_from_device(eid, float(cur))
            return
        if pending["retries"] < MAX_RETRIES:
            self._send(eid, pending["temp"], pending["before"], pending["retries"] + 1, info)
            return
        self._failed[eid] = pending["temp"]
        self._status(eid, ST_NOT_CONFIRMED, pending["temp"], cur)
        self._log(info["rooms"], f"{eid}: Sollwert {pending['temp']} °C nicht bestätigt")

    # --- Events ----------------------------------------------------------------

    @callback
    def _on_state_event(self, event: Event) -> None:
        eid = event.data["entity_id"]
        new: State | None = event.data.get("new_state")
        old: State | None = event.data.get("old_state")
        if eid.startswith("climate.") and new is not None and old is not None:
            if self._thermostat_changed(eid, old, new):
                return
        self.evaluate()

    def _thermostat_changed(self, eid: str, old: State, new: State) -> bool:
        """True, wenn das Event vollständig behandelt ist (keine Auswertung nötig)."""
        offline = (STATE_UNAVAILABLE, STATE_UNKNOWN)
        if old.state in offline and new.state not in offline:
            self._failed.pop(eid, None)  # Gerät wieder da -> neuer Versuch
            return False
        if new.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return False  # Verfügbarkeit geändert -> Status aktualisieren
        if new.state != old.state:
            return False  # HVAC-Modus geändert
        new_t, old_t = new.attributes.get(ATTR_TEMPERATURE), old.attributes.get(ATTR_TEMPERATURE)
        if not isinstance(new_t, (int, float)) or new_t == old_t:
            return True  # z. B. nur Ist-Temperatur geändert
        pending = self._pending.get(eid)
        if pending:
            if abs(new_t - pending["temp"]) < EPS:
                self._pending.pop(eid)["cancel"]()
                self._status(eid, ST_OK, pending["temp"], new_t)
            elif (abs(new_t - pending["temp"]) <= self._tolerance(new)
                  and abs(new_t - pending["before"]) >= EPS):
                self._accept(eid, pending["temp"], float(new_t))
            return True  # sonst: Zwischenwerte während der Bestätigung ignorieren
        expected = self._commanded.get(eid)
        if expected is not None and abs(new_t - expected) < EPS:
            return True
        self._manual_from_device(eid, float(new_t))
        return True

    @callback
    def _manual_from_device(self, eid: str, temp: float) -> None:
        rooms = rooms_for_thermostat(self.store.data, eid)
        self._commanded[eid] = temp
        locked = self.result["thermostats"].get(eid, {}).get("locked", False)
        note = " – Fenstersperre bleibt aktiv" if locked else ""
        self._log(rooms, f"{eid}: manuell auf {temp} °C gestellt{note}")
        self.hass.async_create_task(self._async_store_manual(rooms, temp), eager_start=False)

    async def _async_store_manual(self, rooms: list[str], temp: float) -> None:
        for rid in rooms:
            try:
                await self.scheduler.async_set_manual(rid, temp, MANUAL_UNTIL_NEXT)
            except (RoomError, vol.Invalid) as err:
                _LOGGER.warning("Manueller Wert %s für %s verworfen: %s", temp, rid, err)
        self.evaluate()  # Sperre ggf. erneut durchsetzen

    # --- Status für API/Oberfläche --------------------------------------------

    def status(self) -> dict[str, Any]:
        return {
            "started": self.started,
            "rooms": self.result["rooms"],
            "outdoor": self.outdoor_status,
            "notifications": self.notifier.sent,
            "next_wakeup": self.next_wakeup.isoformat() if self.next_wakeup else None,
            "thermostats": self.thermostat_status,
            "actions": {rid: list(d) for rid, d in self.actions.items()},
            "batteries": battery_report(self.hass, self.store.data),
            "time": datetime.now(dt_util.get_default_time_zone()).isoformat(),
        }
