"""Zeitsteuerung: hält die aktuellen Sollwerte aktuell – ohne Polling.

Es existiert immer höchstens EIN Timer (async_track_point_in_utc_time) auf
den nächsten Wechselzeitpunkt aller Räume bzw. den Ablauf einer manuellen
Änderung. Neuberechnung zusätzlich bei jeder Konfigurationsänderung und beim
Start (-> korrekter Abschnitt nach Neustart). Thermostate werden hier noch
NICHT angesteuert (Modul 4 abonniert die Ergebnisse).
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.util import dt as dt_util

from .models import MANUAL_UNTIL_NEXT
from .profiles import _room, clear_manual, drop_expired_manual, set_manual
from .store import ConfigStore
from .targets import manual_until, next_wakeup, resolve_all

_LOGGER = logging.getLogger(__name__)

TargetsListener = Callable[[dict[str, dict[str, Any]]], None]


class Scheduler:
    def __init__(self, hass: HomeAssistant, store: ConfigStore) -> None:
        self.hass = hass
        self.store = store
        self.targets: dict[str, dict[str, Any]] = {}
        self.next_wakeup: datetime | None = None
        self._unsub_timer: CALLBACK_TYPE | None = None
        self._unsub_store: CALLBACK_TYPE | None = None
        self._listeners: list[TargetsListener] = []
        self._cleanup_pending = False

    @callback
    def async_start(self) -> None:
        self._unsub_store = self.store.async_add_listener(lambda _cfg: self._recompute())
        self._recompute()

    @callback
    def async_stop(self) -> None:
        self._cancel_timer()
        if self._unsub_store:
            self._unsub_store()
            self._unsub_store = None

    @callback
    def async_add_listener(self, listener: TargetsListener) -> CALLBACK_TYPE:
        self._listeners.append(listener)

        @callback
        def remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return remove

    def _cancel_timer(self) -> None:
        if self._unsub_timer:
            self._unsub_timer()
            self._unsub_timer = None

    @callback
    def _recompute(self, _now: datetime | None = None) -> None:
        now = dt_util.utcnow()
        tz = dt_util.get_default_time_zone()
        cfg = self.store.data
        old = self.targets
        self.targets = resolve_all(cfg, now, tz)

        # Abgelaufene manuelle Sollwerte persistent entfernen (einmalig anstoßen)
        expired = any(
            o["until"] and datetime.fromisoformat(o["until"]) <= now for o in cfg["overrides"].values()
        )
        if expired and not self._cleanup_pending:
            self._cleanup_pending = True
            self.hass.async_create_task(self._async_drop_expired(), eager_start=False)

        self._cancel_timer()
        self.next_wakeup = next_wakeup(cfg, self.targets, now)
        if self.next_wakeup:
            self._unsub_timer = async_track_point_in_utc_time(self.hass, self._recompute, self.next_wakeup)

        for rid, t in self.targets.items():
            prev = old.get(rid)
            if prev is None or (prev["desired_temp"], prev["source"]) != (t["desired_temp"], t["source"]):
                _LOGGER.info("Raum %s: Sollwert %.1f °C (%s)", rid, t["desired_temp"], t["source"])
        for listener in list(self._listeners):
            try:
                listener(self.targets)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Fehler in Sollwert-Listener")

    async def _async_drop_expired(self) -> None:
        try:
            await self.store.async_update(lambda cfg: drop_expired_manual(cfg, dt_util.utcnow()))
        finally:
            self._cleanup_pending = False

    async def async_set_manual(self, room_id: str, temp: float, mode: str) -> dict[str, Any]:
        """Manuellen Sollwert setzen (auch von Modul 4 bei Thermostat-Bedienung genutzt)."""
        tz = dt_util.get_default_time_zone()

        def mutator(cfg: dict) -> dict:
            _room(cfg, room_id)  # unbekannter Raum -> RoomError statt KeyError
            now = dt_util.utcnow()
            until = manual_until(cfg, room_id, now, tz) if mode == MANUAL_UNTIL_NEXT else None
            return set_manual(cfg, room_id, temp, mode, now, until)

        await self.store.async_update(mutator)
        return self.targets[room_id]

    async def async_clear_manual(self, room_id: str) -> dict[str, Any]:
        await self.store.async_update(lambda cfg: clear_manual(cfg, room_id))
        return self.targets[room_id]
